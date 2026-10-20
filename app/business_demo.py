"""三个业务 Agent 的端到端 CLI，固定使用虚构案例。"""
import argparse
import asyncio
import json
import sys

import httpx

from app.business import BusinessMockAgent, DeepSeekAgent, FICTIONAL_BUSINESS_CASE, ROLES
from app.config import ROOT, load_settings
from app.deepseek import DeepSeekAdapter
from app.aggregation import DeepSeekAggregator
from app.graph_selector import LLMGraphSelector
from app.models import TaskRecord, TaskRequest
from app.orchestrator import Orchestrator
from app.store import Store


async def main(provider: str, mode: str, collaboration: str = "independent", rounds: int = 2,
               aggregation: str = "deterministic", routing: str = "manual", graph_id: str | None = None) -> int:
    store = Store(str(ROOT / "data/tasks.sqlite3"))
    task = TaskRecord(request=TaskRequest(prompt=FICTIONAL_BUSINESS_CASE, workflow="business",
                      provider=provider, mode=mode, collaboration=collaboration, rounds=rounds,
                      aggregation=aggregation, routing=routing, graph_id=graph_id,
                      timeout_seconds=60, max_retries=0))
    store.save(task, "task_created", request=task.request.model_dump())
    async with httpx.AsyncClient(timeout=60, trust_env=False, follow_redirects=False) as client:
        adapter = DeepSeekAdapter(load_settings(), client)

        def trace(kind, **body):
            store.save(task, kind, **body)

        agents = ([DeepSeekAgent(role, task.id, adapter, trace, collaboration=collaboration) for role in ROLES]
                  if provider == "deepseek" else [BusinessMockAgent(role, task.id, trace,
                                                  collaboration=collaboration) for role in ROLES])
        if aggregation == "deepseek":
            agents.append(DeepSeekAggregator(adapter,trace))
        selector = LLMGraphSelector(adapter,trace) if routing == "llm" else None
        await Orchestrator(store, agents, selector).run(task)
    report = {"task_id": task.id, "provider": provider, "status": task.status,
              "collaboration": task.graph_selection.collaboration if task.graph_selection else collaboration,
              "graph_selection": task.graph_selection.model_dump() if task.graph_selection else None,
              "steps": [{"agent_id": s.agent_id, "round_no": s.round_no, "phase": s.phase,
                         "status": s.status, "attempts": s.attempts,
                         "error": s.error} for s in task.steps],
              "result": task.result.model_dump() if task.result else None,
              "contexts": [{"step_id": e["body"]["step_id"], "round_no": e["body"]["round_no"],
                            "visible_senders": [m["sender"] for m in e["body"]["inputs"]],
                            "message_count": len(e["body"]["messages"])}
                           for e in store.events(task.id) if e["kind"] == "agent_context"],
              "model_calls": [e["body"] for e in store.events(task.id) if e["kind"] == "model_response"]}
    # 完整消息轨迹保存在 SQLite；此报告只保存结果和安全元数据。
    suffix = f"-{routing}" if routing != "manual" else ("" if collaboration == "independent" else f"-{collaboration}")
    (ROOT / f"data/business-{provider}{suffix}-check.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if task.status == "succeeded" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="固定虚构数据的业务 Agent 演示")
    parser.add_argument("--provider", choices=["mock", "deepseek"], default="mock")
    parser.add_argument("--mode", choices=["parallel", "serial"], default="parallel")
    parser.add_argument("--collaboration", choices=["independent", "sequential_debate", "aad"], default="independent")
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--aggregation", choices=["deterministic","deepseek"], default="deterministic")
    parser.add_argument("--routing", choices=["manual","rule","llm"], default="manual")
    parser.add_argument("--graph-id", default=None)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(asyncio.run(main(args.provider, args.mode, args.collaboration, args.rounds,
                                    args.aggregation, args.routing, args.graph_id)))
