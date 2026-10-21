"""观点引用目录与校验；不改变调度图，也不从历史数据库补入当前不可见观点。"""
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.deepseek import ModelError
from app.models import AgentResult, Message, VisibleView


def stable_view_id(task_id: str, step_id: str) -> str:
    """一个任务的一个成功步骤对应一个观点；接收方重试不会改变上游观点 ID。"""
    return f"view:{task_id}:{step_id}"


def visible_views(message: Message) -> list[VisibleView]:
    """从此次请求实际携带的 inputs 建目录；历史见过不等于本次允许引用。"""
    peers = message.payload.get("inputs", [])
    protocol = message.payload.get("collaboration", "independent")
    if message.phase != "aggregation" and peers and (
            protocol == "independent" or message.phase == "draft" or (protocol == "aad" and message.round_no == 1)):
        raise ModelError("unexpected_context", "独立起草阶段禁止读取其他 Agent 的答案", retryable=False)
    allowed_steps = message.payload.get("context_from")
    views = []
    for raw in peers:
        peer = Message.model_validate(raw)
        expected = stable_view_id(peer.task_id, peer.step_id)
        if (peer.task_id != message.task_id or peer.kind != "result" or peer.sender == message.recipient
                or peer.round_no > message.round_no or (allowed_steps is not None and peer.step_id not in allowed_steps)
                or (peer.view_id is not None and peer.view_id != expected)):
            raise ModelError("invalid_peer_message", "公开消息不属于当前任务或协议可见范围", retryable=False)
        views.append(VisibleView(view_id=expected, message_id=peer.id, sender=peer.sender,
                                 step_id=peer.step_id, round_no=peer.round_no, content=peer.payload["summary"]))
    return views


class ViewReference(BaseModel):
    """新格式只需 view_id；旧 message_id/quote 格式仍校验，不静默接受错引文。"""
    model_config = ConfigDict(extra="forbid")
    view_id: str | None = None
    message_id: str | None = None
    quote: str | None = Field(default=None, min_length=1, max_length=300)
    relation: Literal["agree", "challenge", "coordinate"]
    viewpoint: str = Field(default="", max_length=300)


def model_history_result(result: AgentResult) -> str:
    """模型只维护观点 ID；传输 ID 和后端补齐的引文只保留在审计结果中。

    必须在引用校验成功后调用。不能靠删除冲突字段让非法输出通过校验。
    每轮都从实际 visible_views 重新选来源，历史中的引用不授予新通信权限。
    """
    value = result.model_dump()
    if "references" in value["data"]:
        value["data"]["references"] = [
            {key: ref[key] for key in ("view_id", "relation", "viewpoint") if key in ref}
            for ref in value["data"]["references"]]
    return json.dumps(value, ensure_ascii=False)


def validate_references(result: AgentResult, message: Message) -> None:
    """先验证所有引用，全部通过后才补充来源原文；失败时不部分修改或自动删除引用。"""
    from app.aggregation import source_text
    views = visible_views(message)
    by_view = {v.view_id: v for v in views}
    by_message = {v.message_id: v for v in views}
    texts = {p["id"]: source_text(p["payload"]) for p in message.payload.get("inputs", [])}

    def reject(reason, index=None):
        # 不记录未知 ID 或错引文原文；目录只含后端已知、确实可见的 ID。
        raise ModelError("invalid_view_reference", {
            "invalid_structure": "观点引用结构不符合契约",
            "unknown_view": "引用 ID 不在本次可见观点目录中",
            "conflicting_ids": "观点 ID 与消息 ID 不对应同一来源",
            "quote_mismatch": "引用来源可见，但引文不是该来源中的逐字原文",
        }[reason], retryable=True, diagnostics={"stage": "view_reference", "reason": reason,
            "reference_index": index, "allowed_view_ids": list(by_view)})

    refs = result.data.get("references", [])
    if not isinstance(refs, list) or len(refs) > 8:
        reject("invalid_structure")
    resolved = []
    for index, raw in enumerate(refs):
        try:
            ref = ViewReference.model_validate(raw)
        except (ValueError, ValidationError):
            reject("invalid_structure", index)
        source = by_view.get(ref.view_id) if ref.view_id is not None else by_message.get(ref.message_id)
        if source is None:
            reject("unknown_view", index)
        if ref.message_id is not None and ref.message_id != source.message_id:
            reject("conflicting_ids", index)
        if ref.quote is not None and ref.quote not in texts[source.message_id]:
            reject("quote_mismatch", index)
        resolved.append({"view_id": source.view_id, "message_id": source.message_id,
            "quote": ref.quote if ref.quote is not None else source.content[:300],
            "relation": ref.relation, "viewpoint": ref.viewpoint})
    # 可不引用；没有引用时不要求模型填充空壳，不替 Agent 宣称采纳任何观点。
    if "references" in result.data:
        result.data["references"] = resolved
