"""协议行为测试：不依赖真实模型是否碰巧给出不同答案。"""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from app.business import BusinessMockAgent, DeepSeekAgent, ROLES, prepare_user_message
from app.config import Settings
from app.deepseek import DeepSeekAdapter, ModelError
from app.main import create_app
from app.models import Message, TaskRecord, TaskRequest
from app.orchestrator import Orchestrator
from app.store import Store


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def run_protocol(self, protocol, *, provider="mock", rounds=2, fault="none"):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = Store(str(Path(temp.name) / "protocol.sqlite3"))
        task = TaskRecord(request=TaskRequest(prompt="纯虚构案例：星芽文具店销售笔记本，顾客担心透墨及包装。",
            workflow="business", provider=provider, collaboration=protocol, rounds=rounds,
            demo_fault=fault, max_retries=0))
        store.save(task, "task_created")

        def trace(kind, **body):
            store.save(task, kind, **body)

        if provider == "deepseek":
            def handler(request):
                return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
                    "content": '{"summary":"虚构模型结果","data":{"note":"仅用于测试"}}'}}]})
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            self.addAsyncCleanup(client.aclose)
            adapter = DeepSeekAdapter(Settings(api_key=SecretStr("fake-test-key")), client)
            agents = [DeepSeekAgent(r, task.id, adapter, trace, collaboration=protocol) for r in ROLES]
        else:
            agents = [BusinessMockAgent(r, task.id, trace, collaboration=protocol) for r in ROLES]
        await Orchestrator(store, agents).run(task)
        return task, store.events(task.id)

    def requests(self, events):
        return [e["body"]["message"] for e in events if e["kind"] == "message"
                and e["body"]["message"]["kind"] == "request"]

    def assert_final(self, task, count, last_round):
        self.assertEqual(task.status, "succeeded")
        self.assertEqual(len(task.steps), count)
        self.assertEqual(task.result.data["rounds"], last_round)
        self.assertFalse(task.result.data["consensus_claimed"])
        for role in ROLES:
            latest = [s for s in task.steps if s.capability == role.id][-1]
            self.assertEqual(task.result.data["agents"][role.id], latest.result.model_dump())

    async def test_minimal_independent(self):
        task, events = await self.run_protocol("independent")
        self.assert_final(task, 3, 1)
        self.assertEqual([len(m["payload"]["inputs"]) for m in self.requests(events)], [0, 0, 0])

    async def test_minimal_sequential_debate(self):
        task, events = await self.run_protocol("sequential_debate")
        self.assert_final(task, 6, 2)
        requests = self.requests(events)
        self.assertEqual([len(m["payload"]["inputs"]) for m in requests], [0, 1, 2, 2, 3, 4])
        # 每个公开输入都必须是当前请求之前已保存的成功结果，不得读取未来或自己的公开结果。
        result_seq = {e["body"]["message"]["id"]: e["seq"] for e in events
                      if e["kind"] == "message" and e["body"]["message"]["kind"] == "result"}
        for event in events:
            if event["kind"] != "message" or event["body"]["message"]["kind"] != "request":
                continue
            message = event["body"]["message"]
            for peer in message["payload"]["inputs"]:
                self.assertLess(result_seq[peer["id"]], event["seq"])
                self.assertNotEqual(peer["sender"], message["recipient"])
                self.assertEqual(peer["task_id"], task.id)

    async def test_minimal_aad(self):
        task, events = await self.run_protocol("aad")
        self.assert_final(task, 6, 2)
        requests = self.requests(events)
        self.assertEqual([len(m["payload"]["inputs"]) for m in requests], [0, 0, 0, 2, 3, 4])
        self.assertEqual([m["phase"] for m in requests], ["draft"] * 3 + ["debate"] * 3)
        first_results = [e["seq"] for e in events if e["kind"] == "message"
                         and e["body"]["message"]["kind"] == "result"
                         and e["body"]["message"]["round_no"] == 1]
        second_requests = [e["seq"] for e in events if e["kind"] == "message"
                           and e["body"]["message"]["kind"] == "request"
                           and e["body"]["message"]["round_no"] == 2]
        self.assertLess(max(first_results), min(second_requests))
        # AAD 不是只读上一轮快照的 CI：第二位发言者能看到当前轮第一位的公开修订。
        self.assertIn("r2.marketing", [m["step_id"] for m in requests[4]["payload"]["inputs"]])

    async def test_deepseek_actual_context_for_all_protocols(self):
        for protocol, counts in [("independent", [0,0,0]),
                                 ("sequential_debate", [0,1,2,2,3,4]), ("aad", [0,0,0,2,3,4])]:
            task, events = await self.run_protocol(protocol, provider="deepseek")
            self.assertEqual(task.status, "succeeded")
            calls = [e["body"] for e in events if e["kind"] == "model_request"]
            actual = [json.loads(c["messages"][-1]["content"]) for c in calls]
            self.assertEqual([len(c["visible_peer_messages"]) for c in actual], counts)
            self.assertTrue(all(len(c["messages"]) == (2 if c["round_no"] == 1 else 4) for c in calls))
            contexts = {e["body"]["correlation_id"]: e["body"]["messages"] for e in events
                        if e["kind"] == "agent_context"}
            self.assertTrue(all(contexts[c["correlation_id"]] == c["messages"] for c in calls))

    async def test_aad_failure_keeps_drafts_and_blocks_exchange(self):
        task, events = await self.run_protocol("aad", fault="always")
        self.assertEqual(task.status, "failed")
        self.assertIsNone(task.result)
        self.assertTrue(all(s.status == "skipped" for s in task.steps if s.round_no == 2))
        self.assertTrue(all(m["round_no"] == 1 for m in self.requests(events)))
        self.assertEqual(sum(s.status == "succeeded" for s in task.steps), 2)

    async def test_sequential_failure_blocks_later_turns(self):
        task, events = await self.run_protocol("sequential_debate", fault="always")
        self.assertEqual(task.status, "failed")
        self.assertEqual(len(self.requests(events)), 2)
        self.assertTrue(all(s.status == "skipped" for s in task.steps[2:]))

    async def test_three_rounds_and_last_round_aggregation(self):
        task, events = await self.run_protocol("aad", rounds=3)
        self.assert_final(task, 9, 3)
        self.assertTrue(all(s.startswith("r3.") for s in task.result.data["source_steps"]))


class ProtocolValidationTests(unittest.TestCase):
    def test_invalid_settings(self):
        for values in [{"collaboration": "unknown"}, {"collaboration": "aad", "rounds": 1},
                       {"collaboration": "aad", "rounds": 4},
                       {"collaboration": "aad", "mode": "serial"}]:
            with self.assertRaises(ValidationError):
                TaskRequest(prompt="虚构", workflow="business", **values)

    def test_blind_draft_rejects_peer_message(self):
        peer = Message(task_id="task", step_id="r1.marketing", sender="marketing", recipient="orchestrator",
                       kind="result", payload={"summary": "草案", "data": {}})
        for protocol in ("independent", "aad"):
            message = Message(task_id="task", step_id="r1.hesitant", sender="orchestrator", recipient="hesitant",
                              kind="request", payload={"prompt": "虚构", "collaboration": protocol,
                                                       "inputs": [peer.model_dump()]})
            with self.assertRaises(ModelError):
                prepare_user_message(message, protocol)

    def test_cross_task_message_rejected(self):
        peer = Message(task_id="other-task", step_id="r1.marketing", sender="marketing", recipient="orchestrator",
                       kind="result", payload={"summary": "另一任务秘密", "data": {}})
        message = Message(task_id="task", step_id="r1.hesitant", sender="orchestrator", recipient="hesitant",
                          kind="request", phase="debate", payload={"prompt": "虚构",
                          "collaboration": "sequential_debate", "inputs": [peer.model_dump()]})
        with self.assertRaises(ModelError):
            prepare_user_message(message, "sequential_debate")

    def test_http_protocol_and_round_contexts(self):
        with tempfile.TemporaryDirectory() as temp:
            with TestClient(create_app(str(Path(temp) / "web.sqlite3"), settings_loader=lambda: Settings())) as client:
                for protocol, expected in [("independent",3), ("sequential_debate",6), ("aad",6)]:
                    response = client.post("/api/tasks", json={"prompt":"虚构文具店案例", "workflow":"business",
                        "collaboration":protocol, "rounds":2, "provider":"mock"})
                    self.assertEqual(response.status_code, 202)
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        detail = client.get('/api/tasks/'+response.json()["id"]).json()
                        if detail["task"]["status"] not in ("pending", "running"):
                            break
                        time.sleep(0.02)
                    self.assertEqual(detail["task"]["status"], "succeeded")
                    self.assertEqual(sum(e["kind"] == "agent_context" for e in detail["events"]), expected)
                self.assertEqual(client.get("/api/tasks").json()[0]["collaboration"], "aad")
