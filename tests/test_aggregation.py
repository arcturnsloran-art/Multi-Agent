"""汇总的来源约束与引擎集成，不需要真实 API Key。"""
import json
import tempfile
import time
import unittest
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from app.aggregation import StrategyReport, validate_report
from app.config import Settings
from app.deepseek import ModelError
from app.main import create_app
from app.models import TaskRequest


def fake_report(sources, owners):
    ids = [key for key in sources if key != "case"]
    evidence = lambda key: [{"source_id":key, "quote":sources[key][:20]}]
    return {"core_issues":[{"text":"专业观点仍需核验", "nature":"assessment", "evidence":evidence(key)} for key in ids],
        "key_findings":[{"text":"专业判断尚待验证", "nature":"assessment", "evidence":evidence(ids[0])}],
        "connections":[], "conflicts":[], "actions":[{"text":"先核验约束再开展行动", "nature":"recommendation",
        "owner":owners[0], "priority":"P0", "evidence":evidence(ids[0])}],
        "verification":[{"text":"验证判断与案例是否一致", "nature":"recommendation", "evidence":evidence(ids[0])}]}


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.sources = {"case":"纯虚构营销案例", "marketing":"营销内容待验证", "complaints":"投诉处理待核验"}
        self.value = fake_report(self.sources, ["marketing", "complaints"])

    def test_valid_evidence_and_coverage(self):
        validate_report(StrategyReport.model_validate(self.value), self.sources, {"marketing", "complaints"})

    def test_source_id_only_gets_backend_quote_and_invalid_source_keeps_diagnostics(self):
        report = StrategyReport.model_validate(self.value)
        for section in (report.core_issues, report.actions, report.verification):
            for item in section:
                for evidence in item.evidence:
                    evidence.quote = None
        validate_report(report, self.sources, {"marketing", "complaints"})
        self.assertEqual(report.core_issues[0].evidence[0].quote, self.sources["marketing"])
        report.core_issues[0].evidence[0].source_id = "r99.fake"
        with self.assertRaises(ModelError) as caught:
            validate_report(report, self.sources, {"marketing", "complaints"})
        self.assertEqual(caught.exception.diagnostics["reason"], "unknown_source")
        self.assertEqual(caught.exception.diagnostics["section"], "core_issues")
        self.assertEqual(caught.exception.diagnostics["allowed_source_ids"], list(self.sources))

    def test_invented_quote_and_new_number_rejected(self):
        for field,value in [("quote","不存在的原文"), ("source_id","not_registered")]:
            report = StrategyReport.model_validate(self.value)
            setattr(report.core_issues[0].evidence[0], field, value)
            with self.assertRaises(ModelError):
                validate_report(report, self.sources, {"marketing", "complaints"})
        report = StrategyReport.model_validate(self.value)
        report.core_issues[0].text = "转化率已经提升99.9%"
        with self.assertRaises(ModelError):
            validate_report(report, self.sources, {"marketing", "complaints"})

    def test_no_model_asserted_facts_or_inactive_owner(self):
        self.value["core_issues"][0]["nature"] = "fact"
        with self.assertRaises(ValidationError):
            StrategyReport.model_validate(self.value)
        self.value["core_issues"][0]["nature"] = "assessment"
        report = StrategyReport.model_validate(self.value)
        report.actions[0].owner = "hesitant"
        with self.assertRaises(ModelError):
            validate_report(report, self.sources, {"marketing", "complaints"})

    def test_mock_cannot_request_paid_aggregator(self):
        with self.assertRaises(ValidationError):
            TaskRequest(prompt="虚构", workflow="business", aggregation="deepseek")


class AggregationApiTests(unittest.TestCase):
    def run_task(self, invalid=False):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        def handler(request):
            payload = json.loads(request.content)
            if "经营策略整合助手" in payload["messages"][0]["content"]:
                data = json.loads(payload["messages"][-1]["content"])
                report = fake_report(data["sources"], data["allowed_owners"])
                if invalid:
                    report["core_issues"][0]["evidence"][0]["quote"] = "伪造指标"
                result = {"summary":"先协调约束再验证行动", "data":{"report":report}}
            else:
                result = {"summary":"虚构专业判断需要核验", "data":{"findings":["存在不确定性"],
                    "recommendations":["先核验再行动"], "references":[], "revisions":[], "disagreements":[]}}
            return httpx.Response(200, json={"choices":[{"finish_reason":"stop", "message":{"content":json.dumps(result)}}]})
        app = create_app(str(Path(temp.name)/"test.sqlite3"), settings_loader=lambda: Settings(api_key=SecretStr("fake-key")),
                         transport=httpx.MockTransport(handler))
        with TestClient(app) as client:
            task_id = client.post('/api/tasks', json={"prompt":"纯虚构经营案例", "workflow":"business",
                "provider":"deepseek", "collaboration":"aad", "aggregation":"deepseek", "max_retries":0}).json()["id"]
            deadline = time.monotonic()+8
            while time.monotonic()<deadline:
                detail = client.get('/api/tasks/'+task_id).json()
                if detail["task"]["status"] not in ("pending","running"):
                    return detail
                time.sleep(.02)
        self.fail("未进入终态")

    def test_aggregator_uses_latest_results_and_keeps_originals(self):
        detail = self.run_task()
        task = detail["task"]
        self.assertEqual(task["status"], "succeeded")
        self.assertEqual(len(task["steps"]), 7)
        self.assertEqual(task["result"]["data"]["aggregation"], "deepseek")
        self.assertEqual(len(task["result"]["data"]["agents"]), 3)
        self.assertTrue(all(s.startswith('r2.') for s in task["result"]["data"]["source_steps"]))
        self.assertEqual(sum(e["kind"]=='model_request' for e in detail["events"]),7)

    def test_invalid_aggregator_does_not_destroy_professional_results(self):
        task = self.run_task(invalid=True)["task"]
        self.assertEqual(task["status"], "failed")
        self.assertIsNone(task["result"])
        self.assertTrue(all(s["status"]=='succeeded' for s in task["steps"][:-1]))
        self.assertIn('invalid_evidence',task["steps"][-1]["error"])
