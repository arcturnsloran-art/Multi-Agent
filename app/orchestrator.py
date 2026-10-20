"""核心协作机制：显式计划、能力路由、依赖屏障和结构化消息。"""
import asyncio

from app.agents import Agent, RequestAgent
from app.agent_contract import call_agent, publish_discussion
from app.business import ROLES, aggregate_business
from app.models import AgentRequest, CollaborationMessage, Message, Step, TaskRecord
from app.graph_selector import GraphSelector, RuleGraphSelector, manual_graph, resolve_graph, validate_plan
from app.store import Store


def build_plan(mode: str, workflow: str = "classic", collaboration: str = "independent",
               rounds: int = 2, capabilities: list[str] | None = None) -> list[Step]:
    """只增加协议对应的显式计划，仍交给原来的依赖调度器执行。"""
    if workflow == "business":
        role_ids = capabilities if capabilities is not None else [r.id for r in ROLES]
        if collaboration == "independent":
            return [Step(id=role_id, capability=role_id, context_from=[]) for role_id in role_ids]
        plan: list[Step] = []
        for round_no in range(1, rounds + 1):
            for role_id in role_ids:
                blind = collaboration == "aad" and round_no == 1
                # AAD 首轮三个节点没有任何消息或调度依赖，全部完成才开放第二轮。
                # 之后每一发言等此前全部发言成功，可读取其他角色此前的公开结果。
                dependencies = [] if blind else [s.id for s in plan]
                visible = [] if blind else [s.id for s in plan if s.capability != role_id]
                plan.append(Step(id=f"r{round_no}.{role_id}", capability=role_id,
                                 round_no=round_no, phase="draft" if blind else "debate",
                                 depends_on=dependencies, context_from=visible))
        return plan
    return [Step(id="requirements", capability="requirements"),
            Step(id="risks", capability="risks", depends_on=["requirements"] if mode == "serial" else []),
            Step(id="synthesis", capability="synthesis", depends_on=["requirements", "risks"])]


class Orchestrator:
    def __init__(self, store: Store, agents: list[Agent | RequestAgent], selector: GraphSelector | None = None):
        self.store, self.agents = store, agents
        self.selector = selector
        self.routes = {}
        for agent in agents:
            if agent.capability in self.routes:
                raise ValueError(f"能力重复注册：{agent.capability}")
            self.routes[agent.capability] = agent

    def transition(self, task, target, status, **details):
        """所有正常状态变化统一记录旧值、新值和当时的完整快照。"""
        before = target.status
        target.status = status
        self.store.save(task, "state_changed", step_id=target.id if isinstance(target, Step) else None,
                        round_no=target.round_no if isinstance(target, Step) else None,
                        before=before, after=status, **details)

    async def execute_step(self, task: TaskRecord, step: Step, outputs: dict[str, Message]):
        """单步骤拥有独立重试预算。只有成功结果才会成为下游输入。"""
        try:
            agent = self.routes[step.capability]
            step.agent_id = agent.id
            for attempt in range(1, task.request.max_retries + 2):
                step.attempts = attempt
                self.transition(task, step, "running", attempt=attempt)
                # 下游输入保留上游 result 消息的 sender 和 ID，可追溯消息来源。
                message = Message(task_id=task.id, step_id=step.id, sender="orchestrator",
                                  recipient=agent.id, kind="request", round_no=step.round_no,
                                  phase=step.phase, payload={
                                      "prompt": task.request.prompt, "attempt": attempt,
                                      "collaboration": task.graph_selection.collaboration if task.graph_selection else task.request.collaboration,
                                      "inputs": [outputs[d].model_dump() for d in (
                                          step.context_from if step.context_from is not None else step.depends_on)]})
                previous = [m for m in outputs.values() if m.sender == agent.id and m.round_no < step.round_no]
                prior = max(previous, key=lambda m: m.round_no) if previous else None
                request = AgentRequest(message=message,
                    strategy_version=task.request.strategy_versions.get(agent.id, "baseline"),
                    previous_result=prior.payload if prior else None,
                    previous_result_message_id=prior.id if prior else None,
                    collaboration_messages=[CollaborationMessage.model_validate(item)
                        for peer in message.payload["inputs"] for item in peer.get("discussion", [])])
                # 版本只是接入方的输入；本地旧 Agent 不会据此自动更换 Prompt。
                message.payload["strategy_version"] = request.strategy_version
                message.payload["previous_result"] = request.previous_result.model_dump() if request.previous_result else None
                message.payload["previous_result_message_id"] = request.previous_result_message_id
                self.store.save(task, "message", message=message.model_dump())
                self.store.save(task, "agent_request", request=request.model_dump())
                try:
                    response = await asyncio.wait_for(self.invoke(task, step, agent, request),
                                                    timeout=task.request.timeout_seconds)
                    result = response.result
                    reply = Message(task_id=task.id, step_id=step.id, sender=agent.id,
                                    recipient="orchestrator", kind="result",
                                    round_no=step.round_no, phase=step.phase,
                                    payload=result.model_dump(), correlation_id=message.id)
                    discussion = publish_discussion(response, request, reply.id)
                    # 公开讨论附在对应成功结果上，下一轮仍只从 context_from 白名单取上下文。
                    reply.discussion = discussion
                    outputs[step.id] = reply
                    step.result, step.response, step.error = result, response, None
                    self.store.save(task, "agent_response", step_id=step.id, response=response.model_dump())
                    for item in discussion:
                        self.store.save(task, "collaboration_message", message=item.model_dump())
                    self.store.save(task, "message", message=reply.model_dump())
                    self.transition(task, step, "succeeded")
                    return
                except Exception as exc:
                    step.error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
                    self.store.save(task, "attempt_error", step_id=step.id, attempt=attempt, error=step.error)
                    # 供应商认证/余额/参数错误重试没有意义；暂时错误沿用有限预算。
                    if attempt > task.request.max_retries or not getattr(exc, "retryable", True):
                        raise
                    self.transition(task, step, "retrying", error=step.error)
                    await asyncio.sleep((1 if hasattr(exc, "retryable") else 0.1) * attempt)
        except Exception as exc:
            step.error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            self.transition(task, step, "failed", error=step.error)

    async def invoke(self, task, step, agent, request):
        """仅用于教学的故障注入，局限于本任务的风险分析步骤。"""
        if step.capability in ("risks", "hesitant") and task.request.provider == "mock":
            fault = task.request.demo_fault
            if fault == "always" or (fault == "once" and step.attempts == 1):
                raise RuntimeError("演示：第二个 Agent 临时故障")
            if fault == "timeout":
                await asyncio.sleep(task.request.timeout_seconds + 1)
        return await call_agent(agent, request)

    async def run(self, task: TaskRecord):
        """每轮选择依赖全部成功的步骤，并行执行后等待整轮结束。"""
        outputs: dict[str, Message] = {}
        try:
            if set(task.request.strategy_versions) - {a.id for a in self.agents}:
                raise ValueError("策略版本指定了未注册的 Agent")
            protocol, rounds = task.request.collaboration, task.request.effective_rounds
            execution_mode, capabilities = task.request.mode, None
            if task.request.workflow == "business":
                self.transition(task, task, "running")
                # 可选说明使路由理解专业范围；不改变第三方 Agent 的必要接口。
                catalog = [{"capability":a.capability, "name":a.name,
                            "description":getattr(a, "description", "")} for a in self.agents
                           if a.capability != "aggregation"]
                source, fallback = task.request.routing, None
                if source == "manual":
                    proposal = manual_graph(task.request)
                else:
                    selector = self.selector if source == "llm" else RuleGraphSelector()
                    try:
                        if selector is None:
                            raise ValueError("LLM selector not configured")
                        proposal = await asyncio.wait_for(selector.select(task, catalog),
                                                          timeout=min(task.request.timeout_seconds,60))
                        # 先校验模型提案，非法提案不得进入 build_plan。
                        resolve_graph(proposal, task.request, set(self.routes), source=source)
                    except Exception as exc:
                        if source != "llm":
                            raise
                        fallback = f"{type(exc).__name__}：模型图选择未通过，已回退规则选择。"
                        self.store.save(task,"graph_selection_rejected",reason=fallback)
                        proposal = await RuleGraphSelector().select(task,catalog)
                        source = "rule"
                task.graph_selection = resolve_graph(proposal, task.request, set(self.routes),
                                                     source=source, fallback_reason=fallback)
                self.store.save(task,"graph_selected",selection=task.graph_selection.model_dump())
                protocol, rounds = task.graph_selection.collaboration, task.graph_selection.rounds
                execution_mode, capabilities = task.graph_selection.execution_mode, task.graph_selection.agents
                for agent in self.agents:
                    configure = getattr(agent,"configure_protocol",None)
                    if configure:
                        configure(protocol)
            task.steps = build_plan(execution_mode, task.request.workflow, protocol, rounds, capabilities)
            if task.request.aggregation == "deepseek":
                # 汇总仍是一个普通步骤，复用现有依赖、超时、重试、消息和失败传播。
                latest = {}
                for step in task.steps:
                    latest[step.capability] = step.id
                task.steps.append(Step(id="aggregate", capability="aggregation", phase="aggregation",
                    round_no=max(s.round_no for s in task.steps), depends_on=[s.id for s in task.steps],
                    context_from=list(latest.values())))
            if task.graph_selection:
                validate_plan(task.steps,task.graph_selection,set(self.routes),task.request.aggregation)
            self.store.save(task, "plan_created", collaboration=protocol,
                            effective_rounds=rounds,
                            steps=[s.model_dump() for s in task.steps])
            if task.status != "running":
                self.transition(task, task, "running")
            while any(s.status == "pending" for s in task.steps):
                states = {s.id: s.status for s in task.steps}
                for step in task.steps:
                    if step.status == "pending" and any(states[d] in ("failed", "skipped") for d in step.depends_on):
                        self.transition(task, step, "skipped", reason="上游失败")
                ready = [s for s in task.steps if s.status == "pending"
                         and all(states[d] == "succeeded" for d in s.depends_on)]
                if (task.request.workflow == "business" and protocol == "independent"
                        and execution_mode == "serial"):
                    # 仅限制调用顺序，不增加消息依赖，保证三者仍是独立分析。
                    ready = ready[:1]
                if not ready:
                    if any(s.status == "pending" for s in task.steps):
                        raise RuntimeError("计划无法继续：依赖不可满足")
                    break
                # execute_step 吸收普通调用异常；gather 等待同轮其他步骤结束，避免孤儿任务。
                await asyncio.gather(*(self.execute_step(task, s, outputs) for s in ready))
            if any(s.status == "failed" for s in task.steps):
                task.error = "一个或多个步骤失败，未生成最终结果"
                self.transition(task, task, "failed", error=task.error)
            else:
                if task.request.workflow == "business":
                    task.result = (task.steps[-1].result if task.request.aggregation == "deepseek"
                                   else aggregate_business(task.steps, protocol))
                    self.store.save(task, "results_aggregated", method=task.request.aggregation,
                                    collaboration=protocol, sources=task.result.data.get("source_steps",[]))
                else:
                    task.result = task.steps[-1].result
                self.transition(task, task, "succeeded")
        except asyncio.CancelledError:
            self.abort(task, "执行被取消（例如服务关闭）")
            raise
        except Exception as exc:
            self.abort(task, f"{type(exc).__name__}: {exc}")

    def abort(self, task, reason):
        """中断时使所有步骤进入终态，避免页面永远显示运行中。"""
        for step in task.steps:
            if step.status in ("running", "retrying"):
                step.error = reason
                self.transition(task, step, "failed", error=reason)
            elif step.status == "pending":
                self.transition(task, step, "skipped", reason=reason)
        task.error = reason
        self.transition(task, task, "failed", error=reason)
