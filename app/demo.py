"""最小端到端命令行入口：python -m app.demo。"""
import asyncio
import sys

from app.agents import default_agents
from app.models import TaskRecord, TaskRequest
from app.orchestrator import Orchestrator
from app.store import Store


async def main():
    store = Store("data/demo.sqlite3")
    task = TaskRecord(request=TaskRequest(prompt="构建本地 Multi-Agent 协作演示"))
    store.save(task, "task_created")
    await Orchestrator(store, default_agents()).run(task)
    print(task.model_dump_json(indent=2))
    print(f"轨迹条数：{len(store.events(task.id))}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
