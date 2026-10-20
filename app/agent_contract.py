"""调用边界的薄适配层：旧 Agent 无需修改，新 Agent 实现 run_request 即可接入。"""
from app.deepseek import ModelError
from pydantic import ValidationError
from app.models import AgentRequest, AgentResponse, AgentResult, CollaborationMessage, DiscussionItem


def normalize_response(raw, request: AgentRequest, *, native: bool) -> AgentResponse:
    """把旧返回值包装成新信封；不根据文字差异猜测质疑、修订原因或能力提升。"""
    msg = request.message
    try:
        if native:
            response = AgentResponse.model_validate(raw)
            # 是否兼容旧协议由运行时判断，新接入方不能用此字段绕过讨论契约。
            response = response.model_copy(update={"compatibility_notes": []})
        else:
            result = AgentResult.model_validate(raw)
            supplied = result.data.get("discussion")
            notes = []
            if supplied is None:
                # 旧任务/第三方接口只保证结论可追踪，缺少的信息明确标记，不能伪造。
                supplied = ([{"type": "initial_view", "content": result.summary}]
                            if msg.round_no == 1 and msg.phase != "aggregation" else [])
                notes = ["旧 Agent 未提供结构化讨论；不推断质疑、修订原因或最终决策。"]
            response = AgentResponse(
                request_id=msg.id, task_id=msg.task_id, agent_id=msg.recipient,
                strategy_version=request.strategy_version, result=result,
                discussion=supplied, final_decision=result.data.get("final_decision"),
                compatibility_notes=notes)
        response = response.model_copy(deep=True)
    except ValidationError as exc:
        # 只记录固定字段路径与错误类型，不记录 input/msg/ctx，以免回显模型或密钥原文。
        allowed = {"request_id", "task_id", "agent_id", "strategy_version", "schema_version", "result",
                   "summary", "data", "discussion", "type", "content", "reason", "target_agent_id",
                   "source_message_ids", "reply_to", "final_decision", "proposed_strategy_version"}
        errors = [{"field": ".".join(str(part) if isinstance(part, int) or part in allowed else "other_field"
                                       for part in item["loc"]), "type": item["type"]}
                  for item in exc.errors(include_input=False, include_url=False)[:6]]
        raise ModelError("invalid_agent_response", "AgentResponse 或讨论内容不符合接口契约", retryable=True,
                         diagnostics={"stage": "agent_response", "validation_errors": errors}) from None
    except (ValueError, TypeError):
        raise ModelError("invalid_agent_response", "AgentResponse 或讨论内容不符合接口契约", retryable=True) from None
    if (response.request_id != msg.id or response.task_id != msg.task_id
            or response.agent_id != msg.recipient or response.strategy_version != request.strategy_version):
        raise ModelError("response_identity_mismatch", "Agent 返回的请求、任务、身份或策略版本不匹配", retryable=False)
    return response


def validate_discussion(response: AgentResponse, request: AgentRequest) -> None:
    """检查来源确实可见、质疑确实有对象；模型观点的真实性仍需外部评估。"""
    msg = request.message
    visible = {m["id"]: m for m in msg.payload.get("inputs", [])}
    valid_sources = set(visible) | {"case"}
    if request.previous_result_message_id:
        valid_sources.add(request.previous_result_message_id)
    structured = not response.compatibility_notes
    if structured and msg.phase != "aggregation":
        required = "initial_view" if msg.round_no == 1 else "revision"
        if not any(item.type == required for item in response.discussion):
            raise ModelError("missing_discussion", "首轮需记录初始观点，后续轮需记录修订或保留原因", retryable=True)
    for item in response.discussion:
        if set(item.reply_to) - {m.id for m in request.collaboration_messages}:
            raise ModelError("invisible_discussion_reply", "回应关联了本次请求未收到的讨论消息", retryable=True)
        if set(item.source_message_ids) - valid_sources:
            raise ModelError("invisible_discussion_source", "讨论引用了本次请求不可见的消息", retryable=True)
        if item.target_agent_id and item.target_agent_id not in {m["sender"] for m in visible.values()}:
            raise ModelError("invalid_discussion_target", "讨论对象不是当前公开上下文中的 Agent", retryable=True)
        if item.type == "challenge":
            if not any(visible.get(s, {}).get("sender") == item.target_agent_id for s in item.source_message_ids):
                raise ModelError("missing_challenge_source", "质疑必须关联被质疑 Agent 的可见结果消息", retryable=True)
        if item.type == "evidence" and not item.source_message_ids:
            raise ModelError("missing_evidence_source", "证据必须关联案例或可见结果，且仍需验证其真实性", retryable=True)
        if item.type == "initial_view" and msg.round_no > 1:
            raise ModelError("invalid_discussion_round", "初始观点只能在首轮生成", retryable=True)
        if item.type == "revision" and msg.round_no == 1:
            raise ModelError("invalid_discussion_round", "首轮不存在可供修订的本角色上一轮方案", retryable=True)
        if item.type in ("challenge", "feedback") and not visible:
            raise ModelError("blind_discussion", "独立起草阶段不能声称收到或回应同伴观点", retryable=True)


def publish_discussion(response: AgentResponse, request: AgentRequest, result_id: str) -> list[CollaborationMessage]:
    """只有通过验证的成功返回值才发布；失败重试不会产生可供其他 Agent 消费的观点。"""
    validate_discussion(response, request)
    msg = request.message
    return [CollaborationMessage(**item.model_dump(), task_id=msg.task_id, step_id=msg.step_id,
                sender=msg.recipient, round_no=msg.round_no, result_message_id=result_id,
                strategy_version=request.strategy_version) for item in response.discussion]


async def call_agent(agent, request: AgentRequest) -> AgentResponse:
    """每次调用交付私有副本，防止接入方修改运行时校验所依赖的原始请求。"""
    native = callable(getattr(agent, "run_request", None))
    if native:
        raw = await agent.run_request(request.model_copy(deep=True))
    else:
        raw = await agent.run(request.message.model_copy(deep=True))
    return normalize_response(raw, request, native=native)
