"""跨团队接入、可见消息校验、版本传递与进化事件导出的行为测试。"""
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.agent_contract import normalize_response, publish_discussion
from app.business import BusinessMockAgent, ROLES
from app.config import Settings
from app.deepseek import ModelError
from app.main import create_app
from app.models import AgentRequest, AgentResponse, AgentResult, Message, TaskRecord, TaskRequest
from app.orchestrator import Orchestrator
from app.store import Store


class TeamAgent:
    """模拟外部团队实现：仅提供新接口，不实现 run，也不包含学习算法。"""
    def __init__(self, role):
        self.id = self.capability = role.id
        self.name = role.name
        self.requests = []

    async def run_request(self, request: AgentRequest) -> AgentResponse:
        self.requests.append(request)
        msg = request.message
        item = {"type": "initial_view", "content": "先确认数据与业务口径"}
        if msg.round_no > 1:
            item = {"type": "revision", "content": "保留原方案，增加跨领域验证步骤", "reason": "可见观点提示服务约束，但缺少因果证据"}
        return AgentResponse(request_id=msg.id, task_id=msg.task_id, agent_id=self.id,
            strategy_version=request.strategy_version, proposed_strategy_version="external-candidate-v2",
            result=AgentResult(summary=item["content"], data={"recommendations": ["确认口径后做对照验证"]}),
            discussion=[item], final_decision=item["content"])


class ContractValidationTests(unittest.TestCase):
    def request(self):
        peer = Message(task_id="t", step_id="r1.other", sender="other", recipient="orchestrator",
                       kind="result", payload={"summary": "待验证的建议", "data": {}})
        msg = Message(task_id="t", step_id="r2.marketing", sender="orchestrator", recipient="marketing",
                      kind="request", round_no=2, phase="debate", payload={"prompt": "虚构案例", "inputs": [peer.model_dump()]})
        return AgentRequest(message=msg, strategy_version="junior-v1"), peer

    def response(self, request, **changes):
        fields = dict(request_id=request.message.id, task_id="t", agent_id="marketing",
                      strategy_version="junior-v1", result=AgentResult(summary="保留方案"),
                      discussion=[{"type": "revision", "content": "保留方案", "reason": "证据不足，先验证"}])
        fields.update(changes)
        return AgentResponse(**fields)

    def test_identity_and_version_cannot_be_changed_by_agent(self):
        request, _ = self.request()
        for changed in ({"task_id": "other"}, {"agent_id": "other"}, {"request_id": "fake"}, {"strategy_version": "v2"}):
            with self.assertRaises(ModelError):
                normalize_response(self.response(request, **changed), request, native=True)

    def test_invisible_challenge_and_missing_revision_reason_are_rejected(self):
        request, peer = self.request()
        invalid = [
            [{"type": "revision", "content": "改了", "reason": "收到消息", "source_message_ids": ["secret"]}],
            [{"type": "revision", "content": "改了", "reason": "收到消息", "reply_to": ["secret"]}],
            [{"type": "revision", "content": "改了", "reason": "待验证"},
             {"type": "challenge", "content": "不确定", "target_agent_id": "other", "source_message_ids": ["case"]}],
            [{"type": "revision", "content": "改了", "reason": "待验证"},
             {"type": "challenge", "content": "不确定", "target_agent_id": "unregistered", "source_message_ids": [peer.id]}],
            [{"type": "revision", "content": "改了"}],
        ]
        for items in invalid:
            with self.assertRaises((ModelError, ValueError)):
                publish_discussion(self.response(request, discussion=items), request, "result")

    def test_native_cannot_skip_required_revision_using_compatibility_flag(self):
        request, _ = self.request()
        raw = self.response(request, discussion=[], compatibility_notes=["pretend legacy"])
        with self.assertRaises(ModelError):
            publish_discussion(normalize_response(raw, request, native=True), request, "r")

    def test_legacy_result_does_not_invent_revision_reason(self):
        request, _ = self.request()
        response = normalize_response(AgentResult(summary="新结论", data={"revisions": ["文字变了"]}), request, native=False)
        self.assertEqual(response.discussion, [])
        self.assertTrue(response.compatibility_notes)
        self.assertIsNone(response.final_decision)

    def test_invalid_discussion_has_safe_field_diagnostics(self):
        request, _ = self.request()
        raw = AgentResult(summary="虚构结论", data={"discussion": [
            {"type": "revision", "content": "修改方案", "reason": "缺少数据", "do-not-leak-this-key": "secret"}]})
        with self.assertRaises(ModelError) as caught:
            normalize_response(raw, request, native=False)
        diag = caught.exception.diagnostics
        self.assertEqual(diag["validation_errors"][0]["type"], "extra_forbidden")
        self.assertEqual(diag["validation_errors"][0]["field"], "discussion.0.other_field")
        self.assertNotIn("do-not-leak-this-key", str(diag))
        self.assertNotIn("secret", str(diag))


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = str(Path(temp.name) / "contracts.sqlite3")
        self.store = Store(self.path)

    async def run_case(self, agents=None, **changes):
        fields = dict(prompt="纯虚构经营案例，需共同讨论行动顺序", workflow="business", collaboration="aad", max_retries=0)
        fields.update(changes)
        task = TaskRecord(request=TaskRequest(**fields))
        agents = agents or [BusinessMockAgent(role, task.id, collaboration=task.request.collaboration) for role in ROLES]
        self.store.save(task, "task_created")
        await Orchestrator(self.store, agents).run(task)
        return task

    async def test_new_team_agent_and_strategy_version_do_not_require_old_run(self):
        agents = [TeamAgent(role) for role in ROLES]
        task = await self.run_case(agents, strategy_versions={r.id: "junior-v1" for r in ROLES})
        self.assertEqual(task.status, "succeeded")
        for agent in agents:
            first, second = agent.requests
            self.assertEqual(first.collaboration_messages, [])
            self.assertIsNone(first.previous_result)
            self.assertIsNotNone(second.previous_result)
            self.assertTrue(second.collaboration_messages)
            self.assertEqual([r.strategy_version for r in agent.requests], ["junior-v1", "junior-v1"])
        event = self.store.evolution_events()[0]["event"]
        self.assertEqual(event["proposed_strategy_versions"]["marketing"], "external-candidate-v2")
        self.assertEqual(event["strategy_versions"]["marketing"], "junior-v1")
        self.assertEqual(event["evaluation"], "not_evaluated")

    async def test_aad_keeps_initial_views_and_links_received_challenges_to_revisions(self):
        task = await self.run_case()
        self.assertEqual(task.status, "succeeded")
        events = self.store.events(task.id)
        messages = [e["body"]["message"] for e in events if e["kind"] == "collaboration_message"]
        self.assertEqual(sum(m["type"] == "initial_view" for m in messages), 3)
        revisions = [m for m in messages if m["type"] == "revision"]
        self.assertEqual(len(revisions), 3)
        self.assertTrue(all(m["reason"] for m in revisions))
        requests = [e["body"]["request"] for e in events if e["kind"] == "agent_request"]
        self.assertTrue(all(not r["collaboration_messages"] for r in requests[:3]))
        for revision in revisions:
            request = next(r for r in requests if r["message"]["step_id"] == revision["step_id"])
            visible = {m["id"] for m in request["collaboration_messages"]}
            self.assertTrue(set(revision["reply_to"]).issubset(visible))
        self.assertTrue(any(m["reply_to"] for m in revisions))

    async def test_failed_attempt_publishes_no_success_discussion_and_exports_partial_trace(self):
        task = await self.run_case(demo_fault="always")
        self.assertEqual(task.status, "failed")
        event = self.store.evolution_events()[0]["event"]
        self.assertEqual(event["type"], "task_failed")
        self.assertIsNone(event["task_snapshot"]["result"])
        messages = [e["body"]["message"] for e in self.store.events(task.id) if e["kind"] == "collaboration_message"]
        self.assertTrue(all(m["sender"] != "hesitant" for m in messages))
        self.assertTrue(all(m["round_no"] == 1 for m in messages))

    async def test_terminal_export_pagination_recovery_and_old_task_loading(self):
        task = await self.run_case(collaboration="independent")
        # 同一终态重复保存不重复发布；消费游标可以跨进程保存。
        self.store.save(task, "state_changed", step_id=None, after="succeeded")
        with TestClient(create_app(self.path, settings_loader=lambda: Settings())) as client:
            feed = client.get('/api/evolution-events?limit=1').json()
            self.assertEqual(len(feed["items"]), 1)
            self.assertEqual(client.get('/api/evolution-events', params={"after_seq": feed["next_cursor"]}).json()["items"], [])
            export = client.get(f'/api/tasks/{task.id}/evolution').json()
            self.assertEqual(export["availability"], "ready")
            self.assertTrue(all(e["seq"] <= export["event"]["trace_until_seq"] for e in export["trajectory"]))
            self.assertTrue(any(e["kind"] == "agent_request" for e in export["trajectory"]))
            self.assertIn("AgentResponse", client.get('/api/contracts').json())
            self.assertEqual(client.get('/api/evolution-events?after_seq=-1').status_code, 422)
        pending = TaskRecord(request=TaskRequest(prompt="中断样例"))
        self.store.save(pending, "task_created")
        with TestClient(create_app(self.path, settings_loader=lambda: Settings())) as client:
            self.assertEqual(client.get(f'/api/tasks/{pending.id}/evolution').json()["event"]["type"], "task_failed")
        legacy = TaskRecord(request=TaskRequest(prompt="历史任务"), status="succeeded")
        self.store.save(legacy, "historical_snapshot")
        with TestClient(create_app(self.path, settings_loader=lambda: Settings())) as client:
            old = client.get(f'/api/tasks/{legacy.id}/evolution').json()
            self.assertIsNone(old["event"])
            self.assertEqual(old["availability"], "legacy_or_in_progress")
