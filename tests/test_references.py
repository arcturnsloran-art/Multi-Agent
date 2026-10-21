"""观点 ID、协议可见范围、引用合法性与有限修复；全部使用虚构数据。"""
import json
import tempfile
import time
import unittest
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.business import prepare_user_message
from app.config import Settings
from app.deepseek import ModelError
from app.main import create_app
from app.models import AgentResult, Message
from app.references import stable_view_id, validate_references, visible_views


def peer(task="t", step="r1.marketing", sender="marketing", round_no=1):
    return Message(task_id=task, step_id=step, sender=sender, recipient="orchestrator", kind="result",
                   round_no=round_no, view_id=stable_view_id(task, step),
                   payload={"summary": "先核实数据，再小范围验证。", "data": {"findings": ["因果待验证"]}})


def request(peers=(), protocol="aad", round_no=2, recipient="complaints", context_from=None):
    return Message(task_id="t", step_id=f"r{round_no}.{recipient}", sender="orchestrator", recipient=recipient,
                   kind="request", round_no=round_no, phase="draft" if protocol=="aad" and round_no==1 else "debate",
                   payload={"prompt": "虚构案例", "collaboration": protocol, "inputs": [p.model_dump() for p in peers],
                            "context_from": [p.step_id for p in peers] if context_from is None else context_from})


def result(view_id=None, **fields):
    ref={"relation": "coordinate", "viewpoint": "据此调整行动顺序", **fields}
    if view_id is not None:
        ref["view_id"] = view_id
    return AgentResult(summary="本轮判断", data={"references": [ref]})


class ReferenceTests(unittest.TestCase):
    def test_stable_view_id_and_retry_catalog_are_deterministic(self):
        p=peer()
        first, retry=request([p]), request([p])
        self.assertNotEqual(first.id, retry.id)
        self.assertEqual(visible_views(first), visible_views(retry))
        self.assertEqual(stable_view_id("t", p.step_id), p.view_id)
        self.assertNotEqual(p.view_id, stable_view_id("other",p.step_id))
        self.assertNotEqual(p.view_id, stable_view_id("t","r2.marketing"))

    def test_normal_id_only_reference_is_resolved_by_backend_and_is_idempotent(self):
        p=peer(); msg=request([p]); value=result(p.view_id)
        validate_references(value,msg)
        resolved=value.data["references"][0]
        self.assertEqual(resolved["quote"],p.payload["summary"])
        self.assertEqual(resolved["message_id"],p.id)
        before=value.model_dump()
        validate_references(value,msg)
        self.assertEqual(value.model_dump(),before)

    def test_legacy_id_and_verbatim_quote_remain_supported(self):
        p=peer(); value=result(message_id=p.id,quote="因果待验证")
        validate_references(value,request([p]))
        self.assertEqual(value.data["references"][0]["view_id"],p.view_id)

    def test_fabricated_invisible_self_and_other_task_references_are_rejected(self):
        p=peer(); msg=request([p])
        for identifier in ["made-up-id",stable_view_id("t","r2.hesitant"),
                           stable_view_id("t",msg.step_id),stable_view_id("other",p.step_id)]:
            with self.subTest(identifier=identifier):
                with self.assertRaises(ModelError) as caught:
                    validate_references(result(identifier),msg)
                self.assertEqual(caught.exception.diagnostics["reason"],"unknown_view")

    def test_visible_prior_round_and_visible_same_round_are_legal(self):
        a=peer(); b=peer(step="r2.hesitant",sender="hesitant",round_no=2)
        for protocol in ("aad","sequential_debate"):
            msg=request([a,b],protocol=protocol)
            for p in [a,b]:
                validate_references(result(p.view_id),msg)
        validate_references(result(a.view_id),request([a,b],round_no=3))

    def test_future_cross_task_blind_and_outside_context_inputs_are_rejected(self):
        cases=[request([peer(round_no=3)]),request([peer(task="other")]),
               request([peer()],round_no=1),request([peer()],protocol="independent"),
               request([peer()],context_from=[])]
        for msg in cases:
            with self.assertRaises(ModelError):
                visible_views(msg)

    def test_known_but_not_currently_visible_view_is_rejected(self):
        old=peer(); current=peer(step="r2.marketing",round_no=2)
        # 即使历史会话曾见过 old，本次 context_from 没有它也不能引用。
        with self.assertRaises(ModelError):
            validate_references(result(old.view_id),request([current],round_no=3))

    def test_wrong_quote_is_distinct_from_invisible_source_and_not_automatically_rewritten(self):
        p=peer(); value=result(p.view_id,quote="这不是来源原文")
        before=value.model_dump()
        with self.assertRaises(ModelError) as caught:
            validate_references(value,request([p]))
        self.assertEqual(caught.exception.diagnostics["reason"],"quote_mismatch")
        self.assertEqual(value.model_dump(),before)

    def test_conflicting_aliases_and_tampered_backend_view_id_are_rejected(self):
        p=peer()
        with self.assertRaises(ModelError) as caught:
            validate_references(result(p.view_id,message_id="another-message"),request([p]))
        self.assertEqual(caught.exception.diagnostics["reason"],"conflicting_ids")
        p.view_id="forged-view"
        with self.assertRaises(ModelError):
            visible_views(request([p]))

    def test_no_reference_is_allowed_and_failed_batch_is_atomic(self):
        p=peer()
        for data in ({},{"references":[]}):
            validate_references(AgentResult(summary="独立判断",data=data),request([]))
            validate_references(AgentResult(summary="保留原判断",data=data),request([p]))
        value=result(p.view_id)
        value.data["references"].append({"view_id":"fake","relation":"agree"})
        before=value.model_dump()
        with self.assertRaises(ModelError):
            validate_references(value,request([p]))
        self.assertEqual(value.model_dump(),before)

    def test_model_gets_only_current_view_catalog_without_nested_reference_noise(self):
        p=peer(); p.payload["data"]["references"]=[{"message_id":"old-nested-id"}]
        content=json.loads(prepare_user_message(request([p]),"aad")["content"])
        self.assertEqual(content["visible_views"][0]["view_id"],p.view_id)
        self.assertNotIn("old-nested-id",json.dumps(content))
        self.assertNotIn("quote 必须逐字",content["instruction"])
        self.assertIn("不引用就填 []",content["instruction"])


class ProtocolIntegrationTests(unittest.TestCase):
    def run_protocol(self, protocol, *, retry_budget=1, failure=None):
        sent=[]; invalid_sent=0
        def handler(http_request):
            nonlocal invalid_sent
            messages=json.loads(http_request.content)["messages"]
            user=json.loads(messages[-1]["content"]); sent.append((messages,user))
            views=user["visible_views"]
            refs=[] if not views else [{"view_id":views[0]["view_id"],"relation":"coordinate","viewpoint":"参考该角色调整验证顺序"}]
            if views and failure and (failure=="always" or invalid_sent==0):
                invalid_sent+=1; refs=[{"view_id":"fabricated-id","relation":"agree"}]
            elif user.get("format_feedback",{}):
                # 接入方明确选择不再引用；后端不能通过删除非法引用伪装修复。
                refs=[]
            item={"type":"initial_view" if user["round_no"]==1 else "revision", "content":"先核验数据再制定行动"}
            if user["round_no"]>1:item["reason"]="参考本轮可见观点，增加跨领域约束验证"
            discussion=[item]
            if views:
                discussion.append({"type":"challenge","target_agent_id":views[0]["sender"],
                    "source_message_ids":[views[0]["view_id"]],"content":"该行动的证据是否充分？"})
            value={"summary":"虚构专业判断：先核验数据", "data":{"references":refs,"discussion":discussion,"final_decision":"先核验再行动"}}
            return httpx.Response(200,json={"choices":[{"finish_reason":"stop","message":{"content":json.dumps(value,ensure_ascii=False)}}]})
        with tempfile.TemporaryDirectory() as temp:
            with TestClient(create_app(str(Path(temp)/"refs.sqlite3"), settings_loader=lambda:Settings(api_key=SecretStr("test-key")),
                                       transport=httpx.MockTransport(handler))) as client:
                task_id=client.post('/api/tasks',json={"prompt":"虚构多领域经营策略","workflow":"business","provider":"deepseek",
                    "collaboration":protocol,"rounds":2,"max_retries":retry_budget}).json()["id"]
                deadline=time.monotonic()+12
                while time.monotonic()<deadline:
                    detail=client.get('/api/tasks/'+task_id).json()
                    if detail["task"]["status"] in ("succeeded","failed"):return detail,sent
                    time.sleep(.02)
                self.fail("任务未进入终态")

    def test_aad_and_sequential_use_exact_visible_catalog_and_publish_stable_ids(self):
        for protocol,counts in [("aad",[0,0,0,2,3,4]),("sequential_debate",[0,1,2,2,3,4])]:
            detail,sent=self.run_protocol(protocol)
            self.assertEqual(detail["task"]["status"],"succeeded")
            self.assertEqual([len(u["visible_views"]) for _,u in sent],counts)
            requests=[e["body"]["request"] for e in detail["events"] if e["kind"]=="agent_request"]
            for request in requests:
                actual={p["view_id"] for p in request["message"]["payload"]["inputs"]}
                self.assertEqual({v["view_id"] for v in request["visible_views"]},actual)
            replies=[e["body"]["message"] for e in detail["events"] if e["kind"]=="message" and e["body"]["message"]["kind"]=="result"]
            for reply in replies:self.assertEqual(reply["view_id"],stable_view_id(detail["task"]["id"],reply["step_id"]))

    def test_illegal_reference_repair_is_explicit_bounded_and_has_no_failed_history(self):
        for budget,failure,expected in [(0,"once","failed"),(1,"once","succeeded"),(1,"always","failed")]:
            detail,sent=self.run_protocol("aad",retry_budget=budget,failure=failure)
            self.assertEqual(detail["task"]["status"],expected)
            errors=[e["body"] for e in detail["events"] if e["kind"]=="attempt_error"]
            self.assertEqual(errors[0]["diagnostics"]["reason"],"unknown_view")
            self.assertTrue(all(s["attempts"]<=budget+1 for s in detail["task"]["steps"]))
            if budget:
                before,after=sent[3],sent[4]
                self.assertEqual(before[1]["visible_views"],after[1]["visible_views"])
                self.assertEqual(after[1]["format_feedback"]["error_code"],"invalid_view_reference")
                self.assertEqual(len(before[0]),len(after[0]))
                self.assertNotIn("fabricated-id",json.dumps(after))
            responses=[e for e in detail["events"] if e["kind"]=="model_response"]
            self.assertTrue(all("fabricated-id" not in json.dumps(e) for e in responses))
