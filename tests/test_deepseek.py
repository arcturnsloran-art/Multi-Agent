"""默认不联网、不读真实密钥：用 HTTPX MockTransport 验证真实 HTTP 请求契约。"""
import asyncio
import json
import tempfile
import unittest
import time
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.business import DeepSeekAgent, ROLES
from app.config import Settings, load_settings
from app.deepseek import DeepSeekAdapter, ModelError
from app.models import Message
from app.main import create_app

FAKE_KEY = "test-only-not-a-real-api-key"


def response(content=None, **overrides):
    body = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(
        content or {"summary": "虚构测试结果", "data": {"actions": ["测试建议"]}})}}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}}
    body.update(overrides)
    return httpx.Response(200, json=body)


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def adapter(self, handler, key=FAKE_KEY):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        return DeepSeekAdapter(Settings(api_key=SecretStr(key)), client)

    async def test_request_contract(self):
        def handler(request):
            self.assertEqual(str(request.url), "https://api.deepseek.com/chat/completions")
            self.assertEqual(request.headers["Authorization"], f"Bearer {FAKE_KEY}")
            body = json.loads(request.content)
            self.assertEqual(body["response_format"], {"type": "json_object"})
            self.assertEqual(body["thinking"], {"type": "disabled"})
            self.assertFalse(body["stream"])
            return response()
        adapter = await self.adapter(handler)
        result, meta = await adapter.complete([{"role": "user", "content": "虚构案例"}])
        self.assertEqual(result.summary, "虚构测试结果")
        self.assertEqual(meta["usage"]["total_tokens"], 30)

    async def test_no_network_without_key(self):
        def handler(request):
            self.fail("缺密钥时不应联网")
        adapter = await self.adapter(handler, key="")
        with self.assertRaises(ModelError) as caught:
            await adapter.complete([])
        self.assertFalse(caught.exception.retryable)

    async def test_safe_error_and_retry_classification(self):
        for status, retryable in [(400,False), (401,False), (402,False), (422,False), (429,True), (500,True), (503,True)]:
            adapter = await self.adapter(lambda request: httpx.Response(status, text=FAKE_KEY))
            with self.assertRaises(ModelError) as caught:
                await adapter.complete([])
            self.assertEqual(caught.exception.retryable, retryable)
            self.assertNotIn(FAKE_KEY, str(caught.exception))

    async def test_invalid_json_contract_and_truncation(self):
        for result in [httpx.Response(200, text="not json"),
                       response({"data": {}}), response({"summary": "", "data": {}}),
                       response(choices=[{"finish_reason": "length"}])]:
            adapter = await self.adapter(lambda request: result)
            with self.assertRaises(ModelError):
                await adapter.complete([])

    async def test_network_errors_do_not_leak_headers(self):
        for error_type in [httpx.ConnectError, httpx.ReadTimeout]:
            def handler(request):
                raise error_type(FAKE_KEY, request=request)
            adapter = await self.adapter(handler)
            with self.assertRaises(ModelError) as caught:
                await adapter.complete([])
            self.assertTrue(caught.exception.retryable)
            self.assertNotIn(FAKE_KEY, str(caught.exception))

    async def test_role_and_task_isolation_and_private_followup(self):
        requests = []
        def handler(request):
            requests.append(json.loads(request.content)["messages"])
            return response()
        adapter = await self.adapter(handler)
        agents = [DeepSeekAgent(role, "task-a", adapter) for role in ROLES]
        def msg(agent, prompt, task_id="task-a"):
            return Message(task_id=task_id, step_id=agent.id, sender="orchestrator", recipient=agent.id,
                           kind="request", payload={"prompt": prompt, "inputs": []})
        await asyncio.gather(*(a.run(msg(a, f"虚构案例-{a.id}")) for a in agents))
        self.assertEqual(len({m[0]["content"] for m in requests}), 3)
        self.assertTrue(all(len(m) == 2 for m in requests))
        await agents[0].run(msg(agents[0], "营销私有后续问题"))
        self.assertEqual([m["role"] for m in requests[-1]], ["system", "user", "assistant", "user"])
        self.assertTrue(all(len(a.history) == 3 for a in agents[1:]))
        other = DeepSeekAgent(ROLES[0], "task-b", adapter)
        await other.run(msg(other, "另一个任务", "task-b"))
        self.assertEqual(len(requests[-1]), 2)
        self.assertNotIn("营销私有后续问题", json.dumps(requests[-1], ensure_ascii=False))
        with self.assertRaises(ModelError):
            await other.run(msg(other, "不属于这个会话"))

    async def test_failed_attempt_does_not_commit_history(self):
        calls = []
        def handler(request):
            calls.append(json.loads(request.content)["messages"])
            return httpx.Response(503) if len(calls) == 1 else response()
        adapter = await self.adapter(handler)
        agent = DeepSeekAgent(ROLES[0], "task", adapter)
        message = Message(task_id="task", step_id=agent.id, sender="orchestrator", recipient=agent.id,
                          kind="request", payload={"prompt": "虚构案例"})
        with self.assertRaises(ModelError):
            await agent.run(message)
        self.assertEqual(len(agent.history), 1)
        await agent.run(message)
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(len(agent.history), 3)

    async def test_professional_roles_reach_model_for_delegated_tasks(self):
        """检查真实适配器发出的请求，保证专业职责与委派说明没有在调用链中丢失。"""
        sent = []
        def handler(request):
            sent.append(json.loads(request.content)["messages"])
            return response()
        adapter = await self.adapter(handler)
        for role in ROLES:
            agent = DeepSeekAgent(role, "professional-task", adapter)
            message = Message(task_id="professional-task", step_id=role.id,
                              sender="orchestrator", recipient=role.id, kind="request",
                              payload={"prompt": "营销 Agent 委派：分析本领域问题，输出策略及验证计划；仅有虚构案例，缺少明细。",
                                       "inputs": []})
            await agent.run(message)
            system = sent[-1][0]["content"]
            user = json.loads(sent[-1][-1]["content"])
            self.assertIn(role.description, system)
            self.assertIn("validation_plan", system)
            self.assertIn("经调度器转交", system)
            self.assertIn("不得虚构数据", system)
            self.assertEqual(user["requested_by"], "orchestrator")
            self.assertEqual(user["case"], message.payload["prompt"])
            self.assertEqual(user["visible_peer_messages"], [])
        hesitant_system = sent[1][0]["content"]
        self.assertIn("识别口径尚待确认", hesitant_system)
        self.assertIn("不扮演客户", hesitant_system)
        self.assertNotIn("再作购买决定", hesitant_system)
        self.assertEqual(len({id(a) for a in sent}), 3)


class ConfigTests(unittest.TestCase):
    def test_env_read_and_secret_repr(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / ".env"
            path.write_text(f'DEEPSEEK_API_KEY="{FAKE_KEY}"\nDEEPSEEK_MODEL=testing-model\n', encoding="utf-8-sig")
            settings = load_settings(path)
            self.assertTrue(settings.configured)
            self.assertEqual(settings.model, "testing-model")
            self.assertEqual(settings.api_key.get_secret_value(), FAKE_KEY)
            self.assertNotIn(FAKE_KEY, repr(settings))


class BusinessApiTests(unittest.TestCase):
    """从 HTTP 入口到 SQLite 和模型适配器，供应商网络使用假服务。"""
    def wait_terminal(self, client, task_id):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            detail = client.get(f"/api/tasks/{task_id}").json()
            if detail["task"]["status"] not in ("pending", "running"):
                return detail
            time.sleep(0.02)
        self.fail("任务未在限定时间进入终态")

    def test_business_deepseek_parallel_and_serial_isolation(self):
        requests = []
        def handler(request):
            requests.append(json.loads(request.content)["messages"])
            return response()
        with tempfile.TemporaryDirectory() as temp:
            app = create_app(str(Path(temp) / "test.sqlite3"),
                             settings_loader=lambda: Settings(api_key=SecretStr(FAKE_KEY)),
                             transport=httpx.MockTransport(handler))
            with TestClient(app) as client:
                self.assertEqual(set(client.get("/api/config").json()), {"deepseek_configured", "model"})
                for mode in ["parallel", "serial"]:
                    created = client.post("/api/tasks", json={"prompt": f"虚构案例-{mode}",
                        "workflow": "business", "provider": "deepseek", "mode": mode,
                        "timeout_seconds": 2, "max_retries": 0})
                    self.assertEqual(created.status_code, 202)
                    detail = self.wait_terminal(client, created.json()["id"])
                    self.assertEqual(detail["task"]["status"], "succeeded")
                    self.assertEqual(set(detail["task"]["result"]["data"]["agents"]),
                                     {"marketing", "hesitant", "complaints"})
                    model_calls = [e["body"] for e in detail["events"] if e["kind"] == "model_request"]
                    self.assertEqual(len(model_calls), 3)
                    self.assertEqual(len({m["session_id"] for m in model_calls}), 3)
                    self.assertTrue(all(len(m["messages"]) == 2 for m in model_calls))
                    self.assertNotIn(FAKE_KEY, json.dumps(detail))
                self.assertEqual(len(requests), 6)
                self.assertTrue(all(len(m) == 2 for m in requests))
                self.assertNotIn(FAKE_KEY, json.dumps(client.get("/api/config").json()))
            self.assertNotIn(FAKE_KEY.encode(), (Path(temp) / "test.sqlite3").read_bytes())

    def test_authentication_errors_not_retried_or_leaked(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(401, text=FAKE_KEY)
        with tempfile.TemporaryDirectory() as temp:
            with TestClient(create_app(str(Path(temp) / "test.sqlite3"),
                settings_loader=lambda: Settings(api_key=SecretStr(FAKE_KEY)),
                transport=httpx.MockTransport(handler))) as client:
                task_id = client.post("/api/tasks", json={"prompt": "虚构案例", "workflow": "business",
                    "provider": "deepseek", "max_retries": 3}).json()["id"]
                detail = self.wait_terminal(client, task_id)
                self.assertEqual(detail["task"]["status"], "failed")
                self.assertTrue(all(s["attempts"] == 1 for s in detail["task"]["steps"]))
                self.assertEqual(len(calls), 3)
                self.assertNotIn(FAKE_KEY, json.dumps(detail))

    def test_transient_errors_retry_and_then_succeed(self):
        calls = 0
        def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(503) if calls == 1 else response()
        with tempfile.TemporaryDirectory() as temp:
            with TestClient(create_app(str(Path(temp) / "test.sqlite3"),
                settings_loader=lambda: Settings(api_key=SecretStr(FAKE_KEY)),
                transport=httpx.MockTransport(handler))) as client:
                task_id = client.post("/api/tasks", json={"prompt": "虚构案例", "workflow": "business",
                    "provider": "deepseek", "max_retries": 1}).json()["id"]
                detail = self.wait_terminal(client, task_id)
                self.assertEqual(detail["task"]["status"], "succeeded")
                self.assertEqual(calls, 4)
                self.assertEqual(sum(s["attempts"] for s in detail["task"]["steps"]), 4)

    def test_unconfigured_and_unsupported_modes(self):
        with tempfile.TemporaryDirectory() as temp:
            with TestClient(create_app(str(Path(temp) / "test.sqlite3"),
                                       settings_loader=lambda: Settings())) as client:
                self.assertFalse(client.get("/api/config").json()["deepseek_configured"])
                self.assertEqual(client.post("/api/tasks", json={"prompt": "虚构案例", "workflow": "business",
                                 "provider": "deepseek"}).status_code, 503)
                self.assertEqual(client.post("/api/tasks", json={"prompt": "虚构案例",
                                 "provider": "deepseek"}).status_code, 422)
                self.assertEqual(client.post("/api/tasks", json={"prompt": "虚构案例", "workflow": "business",
                                 "provider": "deepseek", "demo_fault": "once"}).status_code, 422)
                # 不读密钥也能离线执行相同业务流程。
                task_id = client.post("/api/tasks", json={"prompt": "虚构案例", "workflow": "business"}).json()["id"]
                self.assertEqual(self.wait_terminal(client, task_id)["task"]["status"], "succeeded")
