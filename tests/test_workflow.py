"""行为测试：验证依赖、消息、失败传播、超时、隔离与 HTTP 入口。"""
import asyncio
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.agents import default_agents
from app.main import create_app
from app.models import AgentResult, TaskRecord, TaskRequest
from app.orchestrator import Orchestrator
from app.store import Store


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(str(Path(self.temp.name) / "test.sqlite3"))
        self.runner = Orchestrator(self.store, default_agents())

    async def run_task(self, **options):
        task = TaskRecord(request=TaskRequest(prompt="测试方案", **options))
        self.store.save(task, "task_created")
        await self.runner.run(task)
        return task

    async def test_parallel_messages_and_join(self):
        task = await self.run_task()
        self.assertEqual(task.status, "succeeded")
        self.assertEqual(task.result.data["sources"], ["analyst", "reviewer"])
        events = self.store.events(task.id)
        changes = [e["body"] for e in events if e["kind"] == "state_changed"]
        # 两个 running 都必须在首个 succeeded 前出现，证明同轮并行调度。
        self.assertEqual([e["after"] for e in changes[:4]], ["running", "running", "running", "succeeded"])
        requests = [e["body"]["message"] for e in events if e["kind"] == "message"
                    and e["body"]["message"]["kind"] == "request"]
        sources = requests[-1]["payload"]["inputs"]
        self.assertEqual([m["sender"] for m in sources], ["analyst", "reviewer"])
        self.assertTrue(all(m["correlation_id"] for m in sources))
        self.assertEqual(self.store.get(task.id).result, task.result)

    async def test_serial_dependency(self):
        task = await self.run_task(mode="serial")
        events = [e["body"] for e in self.store.events(task.id) if e["kind"] == "state_changed"]
        req_end = next(i for i,e in enumerate(events) if e["step_id"] == "requirements" and e["after"] == "succeeded")
        risk_start = next(i for i,e in enumerate(events) if e["step_id"] == "risks" and e["after"] == "running")
        self.assertLess(req_end, risk_start)

    async def test_retry_success(self):
        task = await self.run_task(demo_fault="once")
        self.assertEqual(task.status, "succeeded")
        self.assertEqual(task.steps[1].attempts, 2)
        self.assertEqual(sum(e["kind"] == "attempt_error" for e in self.store.events(task.id)), 1)

    async def test_retry_exhaustion_skips_summary(self):
        task = await self.run_task(demo_fault="always", max_retries=1)
        self.assertEqual(task.status, "failed")
        self.assertEqual(task.steps[1].attempts, 2)
        self.assertEqual(task.steps[2].status, "skipped")
        self.assertIsNone(task.result)

    async def test_timeout_cancellation(self):
        task = await self.run_task(demo_fault="timeout", timeout_seconds=0.4, max_retries=0)
        self.assertEqual(task.steps[1].error, "TimeoutError")
        self.assertEqual(task.steps[1].attempts, 1)
        self.assertEqual(task.status, "failed")

    async def test_concurrent_tasks_do_not_share_fault_attempts(self):
        tasks = await asyncio.gather(self.run_task(demo_fault="once"), self.run_task())
        self.assertEqual([t.status for t in tasks], ["succeeded", "succeeded"])
        self.assertEqual([t.steps[1].attempts for t in tasks], [2, 1])
        for task in tasks:
            messages = [e["body"]["message"] for e in self.store.events(task.id) if e["kind"] == "message"]
            self.assertTrue(all(m["task_id"] == task.id for m in messages))

    async def test_missing_route(self):
        self.runner.routes.pop("risks")
        task = await self.run_task()
        self.assertEqual(task.status, "failed")
        self.assertIn("KeyError", task.steps[1].error)

    async def test_actual_overlap_with_barrier(self):
        # 用同步屏障验证两个 Agent 真正同时进入 run，避免靠耗时阈值猜测。
        entered = set()
        both = asyncio.Event()

        class Probe:
            name = "测试探针"
            def __init__(self, capability):
                self.id = self.capability = capability
            async def run(self, message):
                entered.add(self.id)
                if len(entered) == 2:
                    both.set()
                await asyncio.wait_for(both.wait(), 1)
                return AgentResult(summary=self.id)

        self.runner = Orchestrator(self.store, [Probe("requirements"), Probe("risks"), default_agents()[-1]])
        task = await self.run_task(max_retries=0)
        self.assertEqual(task.status, "succeeded")


class ApiTests(unittest.TestCase):
    def test_web_task_and_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            with TestClient(create_app(str(Path(temp) / "web.sqlite3"))) as client:
                self.assertEqual(client.get("/").status_code, 200)
                self.assertEqual(len(client.get("/api/agents").json()), 3)
                self.assertEqual(client.post("/api/tasks", json={"prompt": " "}).status_code, 422)
                self.assertEqual(client.get("/api/tasks/missing").status_code, 404)
                response = client.post("/api/tasks", json={"prompt": "网页测试"})
                self.assertEqual(response.status_code, 202)
                task_id = response.json()["id"]
                import time
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    detail = client.get(f"/api/tasks/{task_id}").json()
                    if detail["task"]["status"] not in ("pending", "running"):
                        break
                    time.sleep(0.02)
                self.assertEqual(detail["task"]["status"], "succeeded")
                self.assertTrue(detail["events"])

    def test_restart_marks_interrupted_task(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(str(Path(temp) / "restart.sqlite3"))
            task = TaskRecord(request=TaskRequest(prompt="中断任务"))
            store.save(task, "task_created")
            store.recover_interrupted()
            self.assertEqual(store.get(task.id).status, "failed")


if __name__ == "__main__":
    unittest.main()
