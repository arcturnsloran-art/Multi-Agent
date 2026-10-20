# 协作接口与进化事件（1.0）

## 职责边界与兼容方式

- 协作层：选择已注册图、能力路由、消息可见范围、依赖与轮次、有限重试、状态、轨迹和事件导出。
- Agent 团队：根据输入版本解析自身策略、业务分析、生成观点/质疑/修订及原因、支持异步取消。协作层不替团队实现策略加载。
- 进化团队：消费轨迹、评估效果、记忆优化、学习算法及策略产物管理。策略版本建议不会自动改变运行中的 Agent。
- 不因讨论结束宣称 Agent 成长，也不把模型自报的修订原因视为因果证据。`EvolutionEvent.evaluation` 固定为 `not_evaluated`。
- 旧 `run(Message) -> AgentResult`、Mock / DeepSeek、三种协作模式及历史任务继续可用；旧输出缺少结构化字段时明确显示缺失，不通过文本差异生成虚假的原因。

## 从入口逐步阅读

1. `app/main.py:create_task()` 保存 TaskRequest，按任务创建角色对象，启动 `Orchestrator.run()`。
2. `run()` 沿用图选择、`build_plan()` 与依赖屏障。AAD 首轮三个 Agent 互不可见；首轮全部成功后按既定顺序交流。
3. `execute_step()` 仅从 `context_from` 取公开结果，加入该 Agent 自己上一轮的结果，形成 `AgentRequest`，同时保存 `message` 和 `agent_request` 轨迹。
4. `invoke() -> agent_contract.call_agent()` 优先调用新 `run_request(request)`，否则调用旧 `run(message)`。每次交付请求的私有副本。
5. `normalize_response()` 校验任务、Agent、请求 ID 和输入版本；`publish_discussion()` 校验轮次、来源、目标、修订原因和回应关联。失败按原有预算重试，失败尝试不发布观点。
6. 校验成功后保存 `agent_response`、`collaboration_message`、原始 `result` 消息与步骤状态。讨论附着于成功结果，下一轮仍受原 `context_from` 约束，不读取全局事件表作为上下文。
7. 本地模板或 DeepSeek Aggregator 汇总末轮结果，原始各轮结果保留。`Store.save()` 在任务终态事务内写入唯一 `EvolutionEvent`。
8. `app/static/rounds.js:projectRound()` 读取已保存快照与实际请求，按轮次展示每个 Agent 的结论、发现、策略、质疑、证据、修订原因、反馈和决策。字段逐项列出，重复内容合并；Prompt、上下文与原始 JSON 放在折叠区。`collaboration.js` 保留策略版本输入和旧投影接口。

原执行状态仍为 `pending/running/retrying/succeeded/failed/skipped`。本阶段没有增加“已学会”状态。

## AgentRequest / AgentResponse

机器可读 JSON Schema：`GET /api/contracts`。类型定义在 `app/models.py`，新接口协议在 `app/agents.py:RequestAgent`。

AgentRequest：

| 字段 | 含义 |
| --- | --- |
| schema_version | 当前为 `1.0` |
| message | 沿用原信封：任务/步骤 ID、发送方、接收方、轮次、阶段、prompt、inputs |
| strategy_version | 本任务固定的策略版本标识，默认 `baseline`；不由协作层加载策略 |
| previous_result / previous_result_message_id | 当前 Agent 自己上一轮的成功结果与消息 ID，首轮为空 |
| collaboration_messages | 仅来自本次可见结果的结构化讨论；独立首轮为空 |

AgentResponse：回显 `request_id/task_id/agent_id/strategy_version`，并提供 `result: AgentResult`、`discussion: list[DiscussionItem]`、`final_decision`。可选 `proposed_strategy_version` 是外部 Agent 声明的候选版本标识，仅记录，不自动应用。输出版本不能替代输入版本。

旧 Agent 返回 `data.discussion` 和 `data.final_decision` 时，兼容层会提升到统一输出；没有这些字段时 `compatibility_notes` 说明缺失。原始 `Step.result` 不被新接口覆盖，统一输出另存于 `Step.response`。

新团队最小示例（使用现有 Python 类型）：

```python
from app.models import AgentRequest, AgentResponse, AgentResult, DiscussionItem

class TeamMarketingAgent:
    id = "marketing"
    name = "团队营销 Agent"
    capability = "marketing"  # 替换注册表中同能力的 Agent，不重复注册

    async def run_request(self, request: AgentRequest) -> AgentResponse:
        # 由接入团队在此解析 request.strategy_version 并执行自身算法。
        # 此示例只演示契约，没有实现学习或声称已应用某个外部策略。
        msg = request.message
        first = msg.round_no == 1
        opinion = DiscussionItem(
            type="initial_view" if first else "revision",
            content="数据不足，先确认客群与转化口径，再制定实验方案。",
            reason=None if first else "当前证据不足，保留原方案并明确验证条件。",
        )
        return AgentResponse(
            request_id=msg.id, task_id=msg.task_id, agent_id=self.id,
            strategy_version=request.strategy_version,
            result=AgentResult(summary=opinion.content, data={"recommendations": ["先确认数据口径"]}),
            discussion=[opinion], final_decision=opinion.content,
        )
```

在 `create_task()` 创建 Agent 列表处替换同能力实现；不用修改调度器。一个任务内不得更换策略版本。如需对照外部学习结果，提交下一任务并传入新的版本。调用 `POST /api/tasks` 时可以添加：

```json
{"strategy_versions":{"marketing":"junior-v1","hesitant":"junior-v1","complaints":"junior-v1"}}
```

此片段需与原有 prompt、workflow、provider 等字段一起提交。未知 Agent ID 会被拒绝，版本不是文件路径或任意 Prompt。

## CollaborationMessage 的五种类型

| type | 含义与约束 |
| --- | --- |
| initial_view | 首轮初始观点；后续轮不能冒充初稿 |
| challenge | 质疑；必须指定当前可见的 target_agent_id，并关联该 Agent 的结果消息 ID |
| evidence | 证据线索；必须关联 `case` 或可见结果，来源存在不表示事实或因果已被证明 |
| revision | 后续轮的修改或保留方案；必须有非空 reason |
| feedback | 对已公开观点的反馈；不能在盲起草时虚构互动 |

Agent 只声明 `type/content/target_agent_id/source_message_ids/reply_to/reason`。运行时生成 `id/task_id/step_id/sender/round_no/result_message_id/strategy_version/created_at`，禁止 Agent 伪造身份。

- `source_message_ids` 可引用本次 `inputs` 的结果消息 ID、自己的 `previous_result_message_id`，或 `case`。
- `reply_to` 关联本次确实收到的 CollaborationMessage ID；用于说明回应了哪条具体质疑。没有回应则为空，不强制采纳观点。
- 每个成功的结构化返回：首轮至少一条 initial_view，后续轮至少一条有原因的 revision；保留原案也必须解释原因。
- 公开不等于送达。在固定顺序的 AAD 中，最后一个 Agent 发出的质疑可能要到下一轮才被前面的 Agent 看到。页面会明确标注，不能把未送达的质疑说成已回应。
- `final_decision` 是各 Agent 的本轮决策，页面取最后成功轮展示。综合报告属于汇总节点，不代表所有 Agent 已达成共识。

## EvolutionEvent：拉取、游标与重放

`GET /api/evolution-events?after_seq=0&limit=50` 返回 `{items:[{seq,event}],next_cursor}`。消费者自行持久化 next_cursor，并按稳定 `event.id` 幂等处理；无新事件时游标不变。

每个新任务终态最多一条事件，事件与终态快照同事务落库；服务重启恢复中断任务时也导出失败事件。字段包括：

- schema_version / id / task_id / type（task_completed 或 task_failed）；
- task_snapshot：请求、各轮结果、状态、错误、统一输出和最终结果；
- strategy_versions：版本标识记录；proposed_strategy_versions：外部候选版本声明；
- trace_until_seq：终态轨迹边界；evaluation：not_evaluated。

`GET /api/tasks/{task_id}/evolution` 返回 `{event,trajectory,availability}`。trajectory 包含该任务截至边界的调用顺序、AgentRequest/Response、公开讨论、实际 Prompt、状态与错误，不包含导出事件本身，避免递归膨胀。模型内部思维过程不在轨迹中。

旧任务没有新事件时 event 为 null，availability 为 legacy_or_in_progress，仍可导出已有历史轨迹；不回填虚构的结构化讨论或策略版本。失败任务保留已成功的观点和错误，不生成虚假的最终决策。

接口仅供本地研究使用，无鉴权，不应暴露到公网。外部模块主动拉取数据；本项目不会主动向第三方推送轨迹。

## 运行与验证

### 模型格式失败的诊断与有限重试

`json_object` 保证的是 JSON 语法，不能替代 AgentResult 的业务字段校验；供应商也可能偶发返回空 content。当前提示词提供按轮次生成的 `output_example`，取消了与多组业务/讨论字段不相容的“总输出 500 字”要求。

新的 `attempt_error` 保留 `error_code` 与安全的 `diagnostics.reason`：`empty_content`、`invalid_json`、`missing_fields`、`extra_fields`、`invalid_summary`、`invalid_data` 等；响应结构错误与截断另有分类。诊断不存原响应、未知字段名、密钥或底层异常文本，历史通用错误无法据此反推出当时的确切返回内容。

重试继续由 Orchestrator 统一控制；格式失败的下一次输入包含 `format_feedback`，要求根据具体分类纠正结构。失败内容不进入私有会话，不发布为观点，也不作为其他 Agent 的输入。不通过宽松解析或丢弃未知字段绕过契约。

业务 Agent 常规输出预算为 3200 tokens；截断重试时增加到固定上限 4800，并明确要求压缩重复表述、完整闭合 JSON。图选择与汇总仍使用各自预算，不引入无限增长的重试。

网页切换到 DeepSeek 时默认设置最多重试 1 次（即最多 2 次调用），高级设置可改为 0–3 次；旧任务和明确指定的预算不变。认证失败仍不重试。格式示例和有限重试能减少失败，但不保证模型输出始终合规。

启动方式保持不变：`.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000`。使用 DeepSeek 时，启动进程必须有正常外网访问权限；受限环境可能产生 Windows 10013 错误。

网页选择 Mock → AAD → 两轮，执行默认虚构案例。展开“策略版本”可填写实验标识；执行后先点击“第 1 轮 / 第 2 轮”，再选 Agent，逐项查看其本轮输出。“执行依据与详细轨迹”内保留事件导出入口。

自动化验证：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
# 使用本机 Node.js 执行：
node --test tests/test_workbench.cjs
```

测试覆盖旧 Agent、新接口专用 Agent、三种协议上下文、版本回显、不可见来源、无原因修订、失败不发布观点、终态事件幂等、游标、进程恢复与历史任务兼容。测试以虚构数据和假 HTTP 服务为主，不读取真实 Key 或实现进化算法。
