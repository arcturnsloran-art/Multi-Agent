"""注册图选择、回退和越权拒绝的行为测试。"""
import json
import tempfile
import time
import unittest
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from app.config import Settings
from app.deepseek import ModelError
from app.graph_selector import GraphProposal, RuleGraphSelector, resolve_graph, validate_plan
from app.main import create_app
from app.models import Step, TaskRecord, TaskRequest
from app.orchestrator import build_plan


class GraphValidationTests(unittest.TestCase):
    def test_unknown_graph_and_extra_authority_rejected(self):
        for value in [{"graph_id":"hacker", "reason":"选它"},
                      {"graph_id":"aad", "reason":"选它", "agents":["hacker"]},
                      {"graph_id":"parallel", "reason":"选它", "edges":[["marketing","complaints"]]}]:
            with self.assertRaises(ValidationError):
                GraphProposal.model_validate(value)

    def test_missing_registered_capability_rejected(self):
        with self.assertRaises(ModelError):
            resolve_graph(GraphProposal(graph_id="aad",reason="多领域"), TaskRequest(prompt="虚构"),
                          {"marketing"},source="rule")

    def test_illegal_plan_context_and_agent_rejected(self):
        req=TaskRequest(prompt="虚构",workflow="business",collaboration="aad")
        choice=resolve_graph(GraphProposal(graph_id="aad",reason="测试"),req,
                             {"marketing","hesitant","complaints"},source="manual")
        plan=build_plan("parallel","business","aad",2)
        validate_plan(plan,choice,{"marketing","hesitant","complaints"},"deterministic")
        plan[1].context_from=[plan[0].id]
        plan[1].depends_on=[plan[0].id]
        with self.assertRaises(ModelError):
            validate_plan(plan,choice,{"marketing","hesitant","complaints"},"deterministic")
        plan=[Step(id="bad",capability="admin")]
        with self.assertRaises(ModelError):
            validate_plan(plan,choice,{"marketing","hesitant","complaints","admin"},"deterministic")

    def test_mock_and_classic_cannot_request_llm_routing(self):
        for values in [{"workflow":"business","routing":"llm"},
                       {"workflow":"classic","routing":"rule"},
                       {"workflow":"business","routing":"rule","graph_id":"aad"}]:
            with self.assertRaises(ValidationError):
                TaskRequest(prompt="虚构",**values)


class RuleTests(unittest.IsolatedAsyncioTestCase):
    async def test_rules_choose_explainable_graphs(self):
        for prompt,expected in [("只写营销内容","single_marketing"),("处理投诉","single_complaints"),
                                ("回访犹豫客户","single_hesitant"),("共同协调营销与投诉承载","aad"),
                                ("依次逐步复核方案","sequential"),("一般案例分析","parallel")]:
            proposal=await RuleGraphSelector().select(TaskRecord(request=TaskRequest(prompt=prompt)),[])
            self.assertEqual(proposal.graph_id,expected)
            self.assertTrue(proposal.reason)


class GraphApiTests(unittest.TestCase):
    def run_task(self,proposal=None,*,routing="llm",graph_id=None,prompt="虚构：共同协调营销、犹豫客户流失与投诉承载"):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        def handler(request):
            body=json.loads(request.content)
            if "协作图选择助手" in body["messages"][0]["content"]:
                result={"summary":"图选择", "data":proposal or {"graph_id":"aad","reason":"多个领域有协调约束"}}
            else:
                result={"summary":"虚构专业判断", "data":{"findings":["需核验"],"actions":["先验证"],"references":[]}}
            return httpx.Response(200,json={"choices":[{"finish_reason":"stop","message":{"content":json.dumps(result)}}]})
        with TestClient(create_app(str(Path(temp.name)/"test.sqlite3"),settings_loader=lambda:Settings(api_key=SecretStr("fake-key")),
                                   transport=httpx.MockTransport(handler))) as client:
            self.assertEqual(len(client.get('/api/graphs').json()),6)
            body={"prompt":prompt,"workflow":"business","provider":"deepseek","routing":routing,"max_retries":0}
            if graph_id:body["graph_id"]=graph_id
            response=client.post('/api/tasks',json=body);self.assertEqual(response.status_code,202)
            deadline=time.monotonic()+8
            while time.monotonic()<deadline:
                detail=client.get('/api/tasks/'+response.json()["id"]).json()
                if detail["task"]["status"] not in ("pending","running"):
                    return detail,client.get('/api/tasks').json()[0]
                time.sleep(.02)
        self.fail("任务未完成")

    def test_llm_selection_drives_validated_aad(self):
        detail,row=self.run_task()
        self.assertEqual(detail["task"]["status"],"succeeded")
        self.assertEqual(detail["task"]["graph_selection"]["source"],"llm")
        self.assertEqual(len(detail["task"]["steps"]),6)
        self.assertEqual(row["collaboration"],"aad")
        self.assertEqual(sum(e["kind"]=='model_request' for e in detail["events"]),7)

    def test_invalid_llm_graph_falls_back_without_unauthorized_calls(self):
        for proposal in [{"graph_id":"unregistered","reason":"恶意建议"},
                         {"graph_id":"parallel","reason":"增加通信","edges":[["complaints","admin"]]}]:
            detail,_=self.run_task(proposal)
            choice=detail["task"]["graph_selection"]
            self.assertEqual(detail["task"]["status"],"succeeded")
            self.assertEqual(choice["source"],"rule")
            self.assertTrue(choice["fallback_reason"])
            self.assertTrue(any(e["kind"]=='graph_selection_rejected' for e in detail["events"]))
            self.assertTrue(all(s["capability"] in choice["agents"] for s in detail["task"]["steps"]))

    def test_manual_single_agent_is_a_real_single_call(self):
        detail,_=self.run_task(routing="manual",graph_id="single_complaints")
        self.assertEqual(detail["task"]["status"],"succeeded")
        self.assertEqual([s["capability"] for s in detail["task"]["steps"]],["complaints"])
        self.assertEqual(set(detail["task"]["result"]["data"]["agents"]),{"complaints"})
        self.assertEqual(sum(e["kind"]=='model_request' for e in detail["events"]),1)

    def test_rule_selection_has_no_selector_model_call(self):
        detail,_=self.run_task(routing="rule",prompt="只需回复投诉")
        self.assertEqual(detail["task"]["graph_selection"]["graph_id"],"single_complaints")
        self.assertFalse(any(e["kind"]=='model_request' and e["body"].get('purpose')=='graph_selection'
                             for e in detail["events"]))
