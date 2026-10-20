"""Web 入口：uvicorn app.main:app --host 127.0.0.1 --port 8000。"""
import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable, Literal

import httpx

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.agents import default_agents
from app.aggregation import DeepSeekAggregator
from app.business import BusinessMockAgent, DeepSeekAgent, ROLES
from app.config import Settings, load_settings
from app.deepseek import DeepSeekAdapter
from app.graph_selector import GRAPHS, LLMGraphSelector
from app.models import AgentRequest, AgentResponse, CollaborationMessage, EvolutionEvent, TaskRecord, TaskRequest
from app.orchestrator import Orchestrator
from app.store import Store

ROOT = Path(__file__).resolve().parent.parent


def create_app(db_path: str | None = None, *, settings_loader: Callable[[], Settings] = load_settings,
               transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    """工厂让测试使用临时数据库，不污染本地演示历史。"""
    store = Store(db_path or os.getenv("DEMO_DB", str(ROOT / "data/tasks.sqlite3")))
    orchestrator = Orchestrator(store, default_agents())
    running: set[asyncio.Task] = set()

    @asynccontextmanager
    async def lifespan(app):
        store.recover_interrupted()
        # 连接池共享，认证头按调用传入；客户端不保存 Agent 会话。
        async with httpx.AsyncClient(timeout=120, trust_env=False, follow_redirects=False,
                                     transport=transport) as client:
            app.state.model_client = client
            try:
                yield
            finally:
                for worker in running:
                    worker.cancel()
                await asyncio.gather(*running, return_exceptions=True)

    api = FastAPI(title="透明 Multi-Agent Demo", lifespan=lifespan)
    # 仅暴露展示资源目录，.env 与数据库不位于静态文件目录。
    api.mount("/static", StaticFiles(directory=ROOT / "app/static"), name="static")

    @api.get("/")
    async def index():
        return FileResponse(ROOT / "app/static/index.html")

    @api.get("/api/agents")
    async def agents(workflow: Literal["classic", "business"] = "classic"):
        entries = [BusinessMockAgent(r) for r in ROLES] if workflow == "business" else orchestrator.agents
        return [dict(id=a.id, name=a.name, capability=a.capability,
                     description=getattr(a, "description", "")) for a in entries]

    @api.get("/api/config")
    async def config():
        # 明确挑选安全字段，绝不返回 settings.model_dump()。
        settings = settings_loader()
        return {"deepseek_configured": settings.configured, "model": settings.model}

    @api.get("/api/contracts")
    async def contracts():
        """给接入团队提供机器可读的契约；不要求使用相同的 Python 实现。"""
        return {model.__name__: model.model_json_schema()
                for model in (AgentRequest, AgentResponse, CollaborationMessage, EvolutionEvent)}

    @api.get("/api/graphs")
    async def graphs():
        return [{"id":g.id, "name":g.name, "collaboration":g.collaboration,
                 "agents":list(g.agents)} for g in GRAPHS.values()]

    @api.post("/api/tasks", status_code=202)
    async def create_task(request: TaskRequest):
        """先保存任务再启动后台执行，页面可立即开始查询执行轨迹。"""
        settings = settings_loader() if request.provider == "deepseek" else None
        if settings is not None and not settings.configured:
            raise HTTPException(503, "DeepSeek 未配置，请在本机 .env 填写 DEEPSEEK_API_KEY")
        task = TaskRecord(request=request)
        store.save(task, "task_created", request=request.model_dump())
        def trace(kind, **body):
            store.save(task, kind, **body)

        runner = orchestrator
        if request.workflow == "business":
            if request.provider == "deepseek":
                adapter = DeepSeekAdapter(settings, api.state.model_client)

                # 每次提交新建三个对象：相同 Key/连接池不会变成共享 messages。
                business_agents = [DeepSeekAgent(role, task.id, adapter, trace,
                                   collaboration=request.collaboration) for role in ROLES]
                if request.aggregation == "deepseek":
                    business_agents.append(DeepSeekAggregator(adapter, trace))
            else:
                business_agents = [BusinessMockAgent(role, task.id, trace,
                                   collaboration=request.collaboration) for role in ROLES]
            selector = LLMGraphSelector(adapter, trace) if request.routing == "llm" else None
            runner = Orchestrator(store, business_agents, selector)
        worker = asyncio.create_task(runner.run(task))
        running.add(worker)
        worker.add_done_callback(running.discard)
        return {"id": task.id, "status": "pending"}

    @api.get("/api/tasks")
    async def tasks():
        return [dict(id=t.id, prompt=t.request.prompt, status=t.status, created_at=t.created_at,
                     workflow=t.request.workflow, provider=t.request.provider,
                     collaboration=t.graph_selection.collaboration if t.graph_selection else t.request.collaboration,
                     graph_id=t.graph_selection.graph_id if t.graph_selection else None)
                for t in store.list_tasks()]

    @api.get("/api/tasks/{task_id}")
    async def task_detail(task_id: str):
        task = store.get(task_id)
        if task is None:
            raise HTTPException(404, "任务不存在")
        return {"task": task.model_dump(), "events": store.events(task_id)}

    @api.get("/api/evolution-events")
    async def evolution_events(after_seq: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
        """本地拉取接口，不主动发送客户数据，不接受进化模块修改运行中任务。"""
        items = store.evolution_events(after_seq, limit)
        return {"items": items, "next_cursor": items[-1]["seq"] if items else after_seq}

    @api.get("/api/tasks/{task_id}/evolution")
    async def evolution_export(task_id: str):
        if store.get(task_id) is None:
            raise HTTPException(404, "任务不存在")
        events = store.events(task_id)
        terminal = next((e["body"] for e in events if e["kind"] == "evolution_event"), None)
        # 历史任务没有新事件，不伪造策略版本、结构化讨论或已测得的成长效果。
        return {"event": terminal, "trajectory": [e for e in events if terminal is None or e["seq"] <= terminal["trace_until_seq"]],
                "availability": "ready" if terminal else "legacy_or_in_progress"}

    return api


app = create_app()
