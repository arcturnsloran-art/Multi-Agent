"""固定轮次、模型会话中的单一观点 ID，以及有分歧的最终聚合回归测试。"""
import json
import tempfile
import time
import unittest
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import Settings
from app.main import create_app


class FinalizationTests(unittest.TestCase):
    def execute(self, protocol="aad", *, conflict=None, invalid_report=False, missing_findings=False, aggregation="deepseek"):
        calls, invalid_count = [], 0

        def handler(request):
            nonlocal invalid_count
            messages = json.loads(request.content)["messages"]
            user = json.loads(messages[-1]["content"])
            calls.append((messages, user))
            if "sources" in user:
                ids = [k for k in user["sources"] if k != "case"]
                proof = lambda key: [{"source_id": key, "quote": "营销主张小范围试投，投诉主张先验证服务承载"}]
                report = {
                    "core_issues": [{"text": "先核验约束，再决定是否试投", "evidence": proof(k)} for k in ids],
                    "key_findings": [{"text": "行动时序仍有不同立场", "evidence": proof(ids[0])}],
                    "connections": [],
                    "conflicts": [{"text": "试投与服务验证的先后顺序尚未一致", "evidence": proof(ids[0])}],
                    "actions": [{"text": "先验证服务承载", "owner": "complaints", "priority": "P0", "evidence": proof(ids[0])}],
                    "verification": [{"text": "核验服务承载是否足以支持试投", "nature": "recommendation", "evidence": proof(ids[0])}],
                }
                if invalid_report and not user.get("format_feedback"):
                    report["core_issues"][0]["evidence"][0]["quote"] = "虚构证据"
                if missing_findings and not user.get("format_feedback"):
                    report["key_findings"] = []
                output = {"summary": "保留行动时序分歧，先核验经营约束", "data": {"report": report}}
            else:
                views = user["visible_views"]
                refs = [{"view_id": views[0]["view_id"], "relation": "challenge"}] if views else []
                if conflict and views and (conflict == "always" or not invalid_count):
                    invalid_count += 1
                    refs[0]["message_id"] = "unrelated-transport-message"
                item = {"type": "initial_view" if user["round_no"] == 1 else "revision",
                        "content": "营销主张小范围试投，投诉主张先验证服务承载"}
                if user["round_no"] > 1:
                    item["reason"] = "证据不足，保留对行动顺序的异议"
                discussion = [item]
                if views:
                    discussion.append({"type": "challenge", "content": "试投前是否必须完成服务验证？",
                        "target_agent_id": views[0]["sender"], "source_message_ids": [views[0]["view_id"]]})
                output = {"summary": item["content"], "data": {"references": refs, "discussion": discussion,
                          "round_decision": "先核验，保留行动顺序分歧"}}
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(output)}}]})

        with tempfile.TemporaryDirectory() as folder:
            app = create_app(str(Path(folder) / "test.sqlite3"),
                settings_loader=lambda: Settings(api_key=SecretStr("fake-key")), transport=httpx.MockTransport(handler))
            with TestClient(app) as client:
                task_id = client.post("/api/tasks", json={"prompt": "纯虚构经营案例；行动顺序尚有分歧",
                    "workflow": "business", "provider": "deepseek", "collaboration": protocol,
                    "rounds": 3, "aggregation": aggregation, "max_retries": 1}).json()["id"]
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    detail = client.get("/api/tasks/" + task_id).json()
                    if detail["task"]["status"] in ("failed", "succeeded"):
                        return detail, calls
                    time.sleep(.02)
        self.fail("有限计划未按时终止")

    def test_three_rounds_terminate_and_aggregate_once_despite_disagreement(self):
        for protocol in ("aad", "sequential_debate"):
            detail, calls = self.execute(protocol)
            task, events = detail["task"], detail["events"]
            self.assertEqual(task["status"], "succeeded")
            self.assertEqual(task["execution_phase"], "completed")
            self.assertEqual(task["termination_reason"], "round_limit")
            self.assertEqual(len(calls), 10)
            self.assertEqual(sum(e["kind"] == "aggregation_started" for e in events), 1)
            self.assertEqual(sum(e["kind"] == "results_aggregated" for e in events), 1)
            final = task["result"]["data"]
            self.assertTrue(final["report"]["conflicts"])
            self.assertFalse(final["consensus_claimed"])
            self.assertEqual(final["consensus_status"], "not_established")
            self.assertTrue(all(s.startswith("r3.") for s in final["source_steps"]))
            self.assertTrue(all(final["report"][k] for k in ("core_issues", "key_findings", "conflicts", "actions", "verification")))
            begin = next(e["seq"] for e in events if e["kind"] == "aggregation_started")
            professional = [e for e in events if e["kind"] == "state_changed" and (e["body"].get("step_id") or "").startswith("r")]
            self.assertTrue(all(e["seq"] < begin for e in professional))
            aggregation_request = next(u for _, u in calls if "sources" in u)
            self.assertTrue(any(d["type"] == "challenge" and d["round_no"] == 2
                                for d in aggregation_request["discussion_history"]))
            terminal = next(e["body"] for e in events if e["kind"] == "evolution_event")
            self.assertEqual(terminal["task_snapshot"]["result"], task["result"])

    def test_model_history_has_only_view_ids_but_audit_keeps_source_mapping(self):
        detail, calls = self.execute()
        references = []
        for messages, user in calls:
            if "sources" in user:
                continue
            for assistant in (m for m in messages if m["role"] == "assistant"):
                for ref in json.loads(assistant["content"])["data"]["references"]:
                    references.append(ref)
                    self.assertEqual(set(ref), {"view_id", "relation", "viewpoint"})
        self.assertTrue(references)  # 第三轮确实携带第二轮引用，不能用空历史让测试虚假通过。
        canonical = detail["task"]["steps"][3]["result"]["data"]["references"][0]
        self.assertIn("message_id", canonical)
        self.assertIn("quote", canonical)

    def test_conflicting_ids_are_rejected_then_explicitly_repaired(self):
        detail, calls = self.execute(conflict="once")
        self.assertEqual(detail["task"]["status"], "succeeded")
        errors = [e["body"] for e in detail["events"] if e["kind"] == "attempt_error"]
        self.assertEqual(errors[0]["diagnostics"]["reason"], "conflicting_ids")
        repaired = [u for _, u in calls if (u.get("format_feedback") or {}).get("error_code") == "invalid_view_reference"]
        self.assertEqual(len(repaired), 1)
        self.assertNotIn("unrelated-transport-message", json.dumps(repaired))

    def test_exhausted_reference_retries_terminate_without_false_final_report(self):
        detail, calls = self.execute(conflict="always")
        task = detail["task"]
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["execution_phase"], "failed")
        self.assertIsNone(task["result"])
        self.assertEqual(task["steps"][-1]["status"], "skipped")
        self.assertTrue(all(s["attempts"] <= 2 for s in task["steps"]))
        self.assertFalse(any("sources" in u for _, u in calls))

    def test_aggregator_repairs_invalid_evidence_with_same_sources(self):
        detail, calls = self.execute(invalid_report=True)
        self.assertEqual(detail["task"]["status"], "succeeded")
        aggregate = [u for _, u in calls if "sources" in u]
        self.assertEqual(len(aggregate), 2)
        self.assertEqual(aggregate[0]["sources"], aggregate[1]["sources"])
        self.assertEqual(aggregate[1]["format_feedback"]["error_code"], "invalid_evidence")
        self.assertEqual(sum(e["kind"] == "aggregation_started" for e in detail["events"]), 1)

    def test_local_report_also_enters_aggregation_and_keeps_disagreement_metadata(self):
        detail, _ = self.execute(aggregation="deterministic")
        self.assertEqual(detail["task"]["status"], "succeeded")
        self.assertEqual(len(detail["task"]["steps"]), 9)
        self.assertEqual(detail["task"]["result"]["data"]["decision_scope"], "final_report")
        self.assertEqual(sum(e["kind"] == "aggregation_started" for e in detail["events"]), 1)

    def test_new_report_cannot_finish_with_empty_findings_but_can_repair(self):
        detail, calls = self.execute(missing_findings=True)
        self.assertEqual(detail["task"]["status"], "succeeded")
        self.assertTrue(detail["task"]["result"]["data"]["report"]["key_findings"])
        aggregate = [u for _, u in calls if "sources" in u]
        self.assertEqual(len(aggregate), 2)
        self.assertEqual(aggregate[1]["format_feedback"]["error_code"], "invalid_strategy_report")
