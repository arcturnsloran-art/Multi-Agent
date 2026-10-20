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
    quote: str = Field(min_length=2, max_length=300)


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
    for section in (report.core_issues, report.connections, report.conflicts, report.actions, report.verification):
        for item in section:
            supporting = []
            for evidence in item.evidence:
                if evidence.source_id not in sources or evidence.quote not in sources[evidence.source_id]:
                    raise ModelError("invalid_evidence", "汇总引用不存在或不是来源中的原文", retryable=True)
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
    issues, actions, conflicts, verification = [], [], [], []
    for role, result in agents.items():
        summary, data = result["summary"], result.get("data", {})
        evidence = [{"source_id": source_ids[role], "quote": summary[:200]}]
        issues.append({"text": summary, "nature": "assessment", "evidence": evidence})
        proposed = data.get("recommendations") or data.get("actions") or ["核验本角色判断后制定下一步行动"]
        if isinstance(proposed, str):
            proposed = [proposed]
        actions.append({"text": str(proposed[0]), "nature": "recommendation", "owner": role,
                        "priority": {"complaints":"P0", "hesitant":"P1", "marketing":"P2"}[role],
                        "evidence": evidence})
        for disagreement in data.get("disagreements", [])[:2]:
            conflicts.append({"text": disagreement, "nature": "assessment",
                              "evidence": [{"source_id": source_ids[role], "quote": disagreement[:200]}]})
        verification.append({"text": f"验证 {role} 的主要判断与建议是否适用于当前案例。",
                             "nature": "recommendation", "evidence": evidence})
    connections = []
    if len(agents) > 1:
        connections.append({"text": "需协调获客、购买顾虑与服务承载的行动顺序；因果关系尚待验证。",
                            "nature":"assessment", "evidence":[
                                {"source_id":source_ids[r],"quote":v["summary"][:150]} for r,v in agents.items()]})
    return StrategyReport(core_issues=issues, connections=connections, conflicts=conflicts,
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
        sources = {"case": message.payload["prompt"], **{p.step_id: source_text(p.payload) for p in peers}}
        system = (
            "你是经营策略整合助手。只基于输入的案例和专业结论制定协调一致但保留分歧的策略。"
            "不得新增业务事实、数据、已实施动作或未经授权的承诺。跨领域因果必须视为待验证判断。"
            "输出 JSON，顶层仅 summary 和 data。summary 只写一句不含数值的策略方向。"
            "data 仅含 report。report 结构严格遵循给定 schema。每条判断和建议必须引用 source_id 与"
            "逐字 quote，quote 必须出自对应 sources；覆盖全部角色，勿把同伴观点视为已核实事实。"
            "actions 使用 P0/P1/P2 优先级和合法 owner；优先解决约束再分配行动。无依据不编造冲突。"
            "每个部分一至三条，引用尽量简短。"
        )
        messages = [{"role":"system", "content":system}, {"role":"user", "content":json.dumps({
            "sources":sources, "original_conclusions":originals,
            "report_schema":StrategyReport.model_json_schema(), "allowed_owners":list(originals)}, ensure_ascii=False)}]
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
        except (ValueError, ValidationError):
            raise ModelError("invalid_strategy_report", "策略报告不符合结构契约", retryable=True) from None
        validate_report(report, sources, set(originals))
        if re.search(r"\d", result.summary):
            raise ModelError("unsafe_summary", "策略摘要不得包含新数值断言", retryable=True)
        self.trace("model_response", **common, result=result.model_dump(), **meta)
        return AgentResult(summary=result.summary, data={"report":report.model_dump(), "agents":originals,
            "source_steps":[p.step_id for p in peers], "aggregation":"deepseek",
            "collaboration":message.payload["collaboration"], "consensus_claimed":False})
