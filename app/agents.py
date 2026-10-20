"""Agent 只处理消息；调度、重试和持久化由外部负责。"""
import asyncio
from typing import Protocol

from app.models import AgentRequest, AgentResponse, AgentResult, Message


class RequestAgent(Protocol):
    """新团队推荐接口；旧 Agent.run(Message) 由兼容层继续支持。"""
    id: str
    name: str
    capability: str

    async def run_request(self, request: AgentRequest) -> AgentResponse: ...


class Agent(Protocol):
    """其他团队实现这三个属性和异步 run 方法即可接入。"""
    id: str
    name: str
    capability: str

    async def run(self, message: Message) -> AgentResult: ...


class MockAgent:
    """确定性输出用于观察协作，不模拟真实模型的推理能力。"""
    def __init__(self, agent_id: str, name: str, capability: str):
        self.id, self.name, self.capability = agent_id, name, capability

    async def run(self, message: Message) -> AgentResult:
        await asyncio.sleep(0.35)
        prompt = message.payload["prompt"]
        if self.capability == "requirements":
            return AgentResult(summary=f"需求分析：{prompt}", data={
                "actions": ["确定使用者与验收目标", "建立最小输入到输出闭环", "记录可复现的验收样例"]})
        if self.capability == "risks":
            return AgentResult(summary="风险分析完成", data={
                "risks": ["外部服务超时", "Agent 输出契约不一致", "重试可能造成重复副作用"],
                "mitigations": ["限制调用时间", "统一 Pydantic 数据契约", "接入方实现幂等处理"]})
        # 汇总者只消费实际收到的上游消息，不读取调度器的共享状态。
        inputs = message.payload["inputs"]
        return AgentResult(summary=f"任务：{prompt}\n\n建议：先完成最小闭环，再扩展接入能力。\n"
                           + "\n".join(f"• {m['sender']}：{m['payload']['summary']}" for m in inputs),
                           data={"sources": [m["sender"] for m in inputs],
                                 "findings": [m["payload"]["data"] for m in inputs]})


def default_agents() -> list[Agent]:
    return [MockAgent("analyst", "需求分析 Agent", "requirements"),
            MockAgent("reviewer", "风险分析 Agent", "risks"),
            MockAgent("writer", "方案汇总 Agent", "synthesis")]
