"""有来源的策略汇总：原始结论不可覆盖，事实引用与模型判断分开。"""
import json
import re
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.deepseek import DeepSeekAdapter, ModelError
from app.models import AgentResult, Message


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str
    # 模型可仅选择来源；后端补齐原文。旧格式主动提供的引文仍必须逐字匹配。
    quote: str | None = Field(default=None, min_length=2, max_length=300)


class ReportItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=500)
    # 不接受 fact：模型生成的叙述只能是分析判断或建议，不能升级为已核实事实。
    nature: Literal["assessment", "recommendation"] = "assessment"
    evidence: list[Evidence] = Field(min_length=1, max_length=6)


class ActionItem(ReportItem):
    nature: Literal["recommendation"] = "recommendation"
    owner: Literal["marketing", "hesitant", "complaints"]
    priority: Literal["P0", "P1", "P2"]


class StrategyReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    core_issues: list[ReportItem] = Field(min_length=1, max_length=8)
    # 新字段有默认值，旧报告仍可读取；所有新发现同样需要来源，不是新增业务事实。
    key_findings: list[ReportItem] = Field(default_factory=list, max_length=8)
    connections: list[ReportItem] = Field(max_length=6)
    conflicts: list[ReportItem] = Field(max_length=6)
    actions: list[ActionItem] = Field(min_length=1, max_length=8)
    verification: list[ReportItem] = Field(min_length=1, max_length=8)


def source_text(value) -> str:
    """将原始字符串值提取出来，避免 JSON 转义影响逐字引用的校验。"""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "\n".join(source_text(v) for v in value.values())
    if isinstance(value, list):
        return "\n".join(source_text(v) for v in value)
    return str(value)


def validate_report(report: StrategyReport, sources: dict[str, str], owners: set[str]) -> None:
    """校验引用与数字来源；不把引用有效误称为语义/因果已被证明。"""
    cited = set()
    sections = {name: getattr(report, name) for name in (
        "core_issues", "key_findings", "connections", "conflicts", "actions", "verification")}
    for section_name, section in sections.items():
        for index, item in enumerate(section):
            supporting = []
            for evidence_index, evidence in enumerate(item.evidence):
                reason = ("unknown_source" if evidence.source_id not in sources else
                          "quote_mismatch" if evidence.quote is not None and evidence.quote not in sources[evidence.source_id] else None)
                if reason:
                    raise ModelError("invalid_evidence", "汇总引用不存在或不是来源中的原文", retryable=True,
                        diagnostics={"stage": "aggregation", "section": section_name, "item_index": index,
                                     "evidence_index": evidence_index, "reason": reason,
                                     "allowed_source_ids": list(sources)})
                if evidence.quote is None:
                    evidence.quote = sources[evidence.source_id][:300]
                cited.add(evidence.source_id)
                supporting.append(evidence.quote)
            if item.nature == "assessment":
                # 拒绝模型在分析判断中新增没有引用支持的数值。
                numbers = set(re.findall(r"\d+(?:\.\d+)?", item.text))
                supported = set(re.findall(r"\d+(?:\.\d+)?", " ".join(supporting)))
                if numbers - supported:
                    raise ModelError("unsupported_number", "分析判断包含引用未支持的数值", retryable=True)
    if not set(sources).difference({"case"}).issubset(cited):
        raise ModelError("missing_source", "汇总遗漏了部分参与角色的最终结论", retryable=True)
    if any(action.owner not in owners for action in report.actions):
        raise ModelError("invalid_owner", "行动分配给未参与的角色", retryable=True)


def local_report(agents: dict[str, dict], source_ids: dict[str, str]) -> dict:
    """离线汇总仅摘录已有观点，并给出显式标注的通用行动顺序建议。"""
    issues, findings, actions, conflicts, verification = [], [], [], [], []
    for role, result in agents.items():
        summary, data = result["summary"], result.get("data", {})
        evidence = [{"source_id": source_ids[role], "quote": summary[:200]}]
        issues.append({"text": summary, "nature": "assessment", "evidence": evidence})
        decision = data.get("round_decision") or data.get("final_decision")
        proposed = data.get("recommendations") or data.get("actions") or ([decision] if decision else ["核验本角色判断后制定下一步行动"])
        if isinstance(proposed, str):
            proposed = [proposed]
        actions.append({"text": str(proposed[0]), "nature": "recommendation", "owner": role,
                        "priority": {"complaints":"P0", "hesitant":"P1", "marketing":"P2"}[role],
                        "evidence": evidence})
        for disagreement in data.get("disagreements", [])[:2]:
            if len(conflicts) < 6:
                conflicts.append({"text": disagreement, "nature": "assessment",
                                  "evidence": [{"source_id": source_ids[role], "quote": disagreement[:200]}]})
        for item in data.get("discussion", []):
            content = item.get("content", "")
            if not content:
                continue
            supported = [{"source_id": source_ids[role], "quote": content[:200]}]
            if item.get("type") == "challenge" and len(conflicts) < 6:
                conflicts.append({"text": "待核验异议（不代表已确认冲突）：" + content[:400],
                                  "nature": "assessment", "evidence": supported})
            elif item.get("type") in ("evidence", "revision") and len(findings) < 8:
                findings.append({"text": content[:500], "nature": "assessment", "evidence": supported})
        verification.append({"text": f"验证 {role} 的主要判断与建议是否适用于当前案例。",
                             "nature": "recommendation", "evidence": evidence})
    connections = []
    if len(agents) > 1:
        connections.append({"text": "需协调获客、购买顾虑与服务承载的行动顺序；因果关系尚待验证。",
                            "nature":"assessment", "evidence":[
                                {"source_id":source_ids[r],"quote":v["summary"][:150]} for r,v in agents.items()]})
    return StrategyReport(core_issues=issues, key_findings=findings, connections=connections, conflicts=conflicts,
                          actions=actions, verification=verification).model_dump()


class DeepSeekAggregator:
    """普通 Agent 接口的末端汇总节点，不直接改写业务 Agent 的结论。"""
    id = "strategy_aggregator"
    name = "DeepSeek 策略汇总"
    capability = "aggregation"

    def __init__(self, adapter: DeepSeekAdapter, trace: Callable[..., None]):
        self.adapter, self.trace = adapter, trace

    async def run(self, message: Message) -> AgentResult:
        peers = [Message.model_validate(x) for x in message.payload["inputs"]]
        if any(p.task_id != message.task_id or p.kind != "result" for p in peers):
            raise ModelError("invalid_aggregation_source", "汇总输入不是本任务的成功结果消息", retryable=False)
        originals = {p.sender: p.payload for p in peers}
        history = message.payload.get("discussion_history", [])
        # 将本角色公开讨论并入其证据来源，仍要求引用逐字匹配，不能拿历史虚构共识。
        sources = {"case": message.payload["prompt"], **{p.step_id: source_text({
            "latest_result": p.payload, "discussion_history": [d for d in history if d["sender"] == p.sender]}) for p in peers}}
        system = (
            "你是经营策略整合助手。只基于输入的案例和专业结论制定协调一致但保留分歧的策略。"
            "不得新增业务事实、数据、已实施动作或未经授权的承诺。跨领域因果必须视为待验证判断。"
            "输出 JSON，顶层仅 summary 和 data。summary 只写一句不含数值的策略方向。"
            "data 仅含 report。report 结构严格遵循给定 schema。每条判断和建议必须引用 source_id 标识的"
            "来源。evidence 每项建议只写 {source_id}，由后端补齐原文；若主动写 quote，必须是对应 sources 的逐字连续子串。"
            "source_id 必须逐字取自 sources 的键（如 r3.marketing），不能用角色名代替。覆盖全部角色，勿把同伴观点视为已核实事实。"
            "actions 使用 P0/P1/P2 优先级和合法 owner；优先解决约束再分配行动。无依据不编造冲突。"
            "core_issues 写核心结论，key_findings 写关键发现，conflicts 明列仍存在的不同立场与未回应的质疑。"
            "这是固定轮次结束后的最终报告，不再要求继续讨论。没有明确证据不得声称全员同意或已达共识。"
            "每个部分一至三条，引用尽量简短。"
        )
        # 聚合重试仍使用同一来源集合，只反馈安全错误码，不丢弃事实引用校验。
        # 存量报告允许缺少新增字段；新模型报告必须交付关键发现，不能以空数组冒充完成。
        report_schema = StrategyReport.model_json_schema()
        report_schema["required"].append("key_findings")
        report_schema["properties"]["key_findings"]["minItems"] = 1
        messages = [{"role":"system", "content":system}, {"role":"user", "content":json.dumps({
            "sources":sources, "original_conclusions":originals,
            "discussion_history": history,
            "format_feedback": message.payload.get("format_feedback"),
            "report_schema":report_schema, "allowed_source_ids": list(sources),
            "output_example": {"summary":"总体策略方向，不宣称共识", "data":{"report":{
                "core_issues":[{"text":"有来源的核心结论", "evidence":[{"source_id":peers[0].step_id}]}],
                "key_findings":[{"text":"有来源的关键发现", "evidence":[{"source_id":peers[0].step_id}]}],
                "connections":[], "conflicts":[], "actions":[{"text":"待执行的建议", "nature":"recommendation",
                    "owner":peers[0].sender, "priority":"P0", "evidence":[{"source_id":peers[0].step_id}]}],
                "verification":[{"text":"待验证事项", "nature":"recommendation", "evidence":[{"source_id":peers[0].step_id}]}]}}},
            "allowed_owners":list(originals)}, ensure_ascii=False)}]
        common = dict(step_id=message.step_id, agent_id=self.id, purpose="aggregation",
                      round_no=message.round_no, phase="aggregation", correlation_id=message.id)
        self.trace("agent_context", **common, provider="deepseek", session_id=f"{message.task_id}:aggregation",
                   messages=messages, inputs=message.payload["inputs"])
        self.trace("model_request", **common, messages=messages)
        result, meta = await self.adapter.complete(messages, max_tokens=3200)
        try:
            if set(result.data) != {"report"}:
                raise ValueError("report envelope")
            report = StrategyReport.model_validate(result.data["report"])
            if not report.key_findings:
                raise ValueError("key_findings required")
        except (ValueError, ValidationError):
            raise ModelError("invalid_strategy_report", "策略报告不符合结构契约，核心结论、关键发现、行动和待验证事项均不能为空", retryable=True) from None
        validate_report(report, sources, set(originals))
        if re.search(r"\d", result.summary):
            raise ModelError("unsafe_summary", "策略摘要不得包含新数值断言", retryable=True)
        self.trace("model_response", **common, result=result.model_dump(), **meta)
        return AgentResult(summary=result.summary, data={"report":report.model_dump(), "agents":originals,
            "source_steps":[p.step_id for p in peers], "aggregation":"deepseek",
            "collaboration":message.payload["collaboration"], "consensus_claimed":False,
            "consensus_status":"not_established", "decision_scope":"final_report"})
