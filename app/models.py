"""统一数据契约：接入者无需理解调度器内部实现。"""
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def new_id() -> str:
    return uuid4().hex


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


Status = Literal["pending", "running", "retrying", "succeeded", "failed", "skipped"]
Collaboration = Literal["independent", "sequential_debate", "aad"]
GraphId = Literal["single_marketing", "single_hesitant", "single_complaints", "sequential", "parallel", "aad"]


class TaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=4000)
    workflow: Literal["classic", "business"] = "classic"
    provider: Literal["mock", "deepseek"] = "mock"
    collaboration: Collaboration = "independent"
    rounds: int = Field(default=2, ge=1, le=10)
    aggregation: Literal["deterministic", "deepseek"] = "deterministic"
    routing: Literal["manual", "rule", "llm"] = "manual"
    graph_id: GraphId | None = None
    mode: Literal["parallel", "serial"] = "parallel"
    timeout_seconds: float = Field(default=5, ge=0.05, le=120)
    max_retries: int = Field(default=1, ge=0, le=10)
    demo_fault: Literal["none", "once", "always", "timeout"] = "none"
    # 只传递外部指定的版本标识；协作层不根据讨论结果修改策略或 Prompt。
    strategy_versions: dict[str, str] = Field(default_factory=dict, max_length=32)

    @field_validator("strategy_versions")
    @classmethod
    def valid_versions(cls, value):
        if any(not k.strip() or not v.strip() or len(k) > 100 or len(v) > 100 for k, v in value.items()):
            raise ValueError("Agent ID 和策略版本必须是非空且不超过100字符的标识")
        return value

    @model_validator(mode="after")
    def valid_provider(self):
        if self.workflow != "business" and (self.routing != "manual" or self.graph_id is not None):
            raise ValueError("动态图选择用于 business 流程")
        if self.routing == "llm" and self.provider != "deepseek":
            raise ValueError("LLMGraphSelector 需要 DeepSeek 模式；Mock 可使用规则选择")
        if self.routing != "manual" and self.graph_id is not None:
            raise ValueError("自动选择时不要同时指定固定 graph_id")
        if self.aggregation == "deepseek" and (self.provider != "deepseek" or self.workflow != "business"):
            raise ValueError("DeepSeek 汇总需要 business 流程与 DeepSeek 执行模式")
        if self.workflow != "business" and self.collaboration != "independent":
            raise ValueError("辩论协议用于 business 流程")
        if self.collaboration == "aad" and self.rounds < 2:
            raise ValueError("AAD 至少两轮：独立起草 + 交流修订")
        if self.collaboration != "independent" and self.mode != "parallel":
            raise ValueError("辩论顺序由协议固定，请保留 mode=parallel；mode 仅控制 Independent 调用顺序")
        if self.provider == "deepseek" and self.workflow != "business":
            raise ValueError("DeepSeek 模式用于三个业务 Agent，请选择 business 流程")
        if self.provider == "deepseek" and self.demo_fault != "none":
            raise ValueError("真实模型模式不启用 Mock 故障注入")
        return self

    @property
    def effective_rounds(self) -> int:
        """Independent 固定一轮；rounds 只作用于两种辩论协议。"""
        return 1 if self.collaboration == "independent" else self.rounds

    @field_validator("prompt")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("任务不能为空白")
        return value.strip()


class Message(BaseModel):
    """消息显式携带发送方、接收方、所属步骤与关联消息 ID。"""
    id: str = Field(default_factory=new_id)
    task_id: str
    step_id: str
    sender: str
    recipient: str
    kind: Literal["request", "result"]
    round_no: int = Field(default=1, ge=1)
    phase: Literal["draft", "debate", "aggregation"] = "draft"
    payload: dict[str, Any]
    correlation_id: str | None = None
    created_at: str = Field(default_factory=now)
    discussion: list["CollaborationMessage"] = Field(default_factory=list)
    # 观点 ID 与传输消息 ID 分离；旧消息没有此字段时由后端确定性补算。
    view_id: str | None = None


class AgentResult(BaseModel):
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)


DiscussionType = Literal["initial_view", "challenge", "evidence", "revision", "feedback"]


class DiscussionItem(BaseModel):
    """Agent 声明的讨论内容；身份、轮次和消息 ID 由协作层填充，禁止 Agent 伪造。"""
    model_config = ConfigDict(extra="forbid")
    type: DiscussionType
    content: str = Field(min_length=1, max_length=2000)
    target_agent_id: str | None = None
    source_message_ids: list[str] = Field(default_factory=list, max_length=12)
    reply_to: list[str] = Field(default_factory=list, max_length=12)
    reason: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def revision_reason(self):
        if self.type == "revision" and (not self.reason or not self.reason.strip()):
            raise ValueError("修订必须说明原因，包括选择保留原方案的原因")
        if self.type == "challenge" and not self.target_agent_id:
            raise ValueError("质疑必须指定被质疑的 Agent")
        return self


class CollaborationMessage(DiscussionItem):
    """经运行时校验和封装的公开讨论事件；记录公开不等于对所有 Agent 可见。"""
    id: str = Field(default_factory=new_id)
    task_id: str
    step_id: str
    sender: str
    round_no: int = Field(ge=1)
    result_message_id: str
    strategy_version: str
    created_at: str = Field(default_factory=now)


class VisibleView(BaseModel):
    """只来自当前 context_from 的成功结果，供 Agent 选择引用，不是全局观点索引。"""
    view_id: str
    message_id: str
    sender: str
    step_id: str
    round_no: int
    content: str


class AgentRequest(BaseModel):
    """新接入团队的输入契约：复用原消息信封，只附加受控上下文。"""
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"] = "1.0"
    message: Message
    strategy_version: str = Field(default="baseline", min_length=1, max_length=100)
    previous_result: AgentResult | None = None
    previous_result_message_id: str | None = None
    collaboration_messages: list[CollaborationMessage] = Field(default_factory=list)
    visible_views: list[VisibleView] = Field(default_factory=list)


class AgentResponse(BaseModel):
    """业务输出与协议输出分离。proposed_strategy_version 仅记录外部建议，不自动采用。"""
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"] = "1.0"
    request_id: str
    task_id: str
    agent_id: str
    strategy_version: str
    result: AgentResult
    discussion: list[DiscussionItem] = Field(default_factory=list, max_length=24)
    round_decision: str | None = Field(default=None, max_length=2000)
    # 兼容旧团队接口；此字段也只代表本角色阶段方案，不能当作任务最终报告。
    final_decision: str | None = Field(default=None, max_length=2000)
    proposed_strategy_version: str | None = Field(default=None, min_length=1, max_length=100)
    # 旧接口没有提供的字段不得被包装层冒充成真实修订原因。
    compatibility_notes: list[str] = Field(default_factory=list)


class Step(BaseModel):
    id: str
    capability: str
    depends_on: list[str] = Field(default_factory=list)
    # 调度依赖与信息可见范围分开：等某步骤成功，不等于可以看到它的私有历史。
    context_from: list[str] | None = None
    round_no: int = Field(default=1, ge=1)
    phase: Literal["draft", "debate", "aggregation"] = "draft"
    agent_id: str | None = None
    status: Status = "pending"
    attempts: int = 0
    result: AgentResult | None = None
    error: str | None = None
    response: AgentResponse | None = None


class TaskRecord(BaseModel):
    id: str = Field(default_factory=new_id)
    request: TaskRequest
    status: Status = "pending"
    steps: list[Step] = Field(default_factory=list)
    result: AgentResult | None = None
    error: str | None = None
    execution_phase: Literal["pending", "discussion", "aggregation", "completed", "failed"] = "pending"
    termination_reason: str | None = None
    created_at: str = Field(default_factory=now)
    graph_selection: "GraphSelection | None" = None


class GraphSelection(BaseModel):
    """全部执行参数由 Python 从注册图解析，模型仅能提出图 ID 和理由。"""
    graph_id: GraphId
    reason: str
    source: Literal["manual", "rule", "llm"]
    collaboration: Collaboration
    agents: list[str]
    rounds: int
    execution_mode: Literal["parallel", "serial"] = "parallel"
    fallback_reason: str | None = None


TaskRecord.model_rebuild()
Message.model_rebuild()


class EvolutionEvent(BaseModel):
    """提供给进化模块的可拉取事件，不包含学习、记忆更新或自动 Prompt 进化逻辑。"""
    schema_version: Literal["1.0"] = "1.0"
    id: str
    task_id: str
    type: Literal["task_completed", "task_failed"]
    created_at: str = Field(default_factory=now)
    trace_until_seq: int
    task_snapshot: TaskRecord
    strategy_versions: dict[str, str]
    proposed_strategy_versions: dict[str, str] = Field(default_factory=dict)
    evaluation: Literal["not_evaluated"] = "not_evaluated"
