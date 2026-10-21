# 协作接口与进化事件（1.0）

## ②组、协作层与④组的对接边界

这里按②组负责专业 Agent、④组负责评估与进化模块划分职责。现有接口不实现记忆优化、训练或 Prompt 自动进化。

| 接口 / 模块 | 负责什么 | 谁需要对接 |
| --- | --- | --- |
| `RequestAgent.run_request(AgentRequest) -> AgentResponse` | 每次调用的输入/输出契约；异步执行，支持取消。身份、请求 ID、版本必须回显 | ②组主要入口。在 `main.create_task()` 注册同能力实现，目前不是网络注册服务 |
| `AgentRequest.visible_views` | 本次允许引用的观点白名单；历史见过不代表本次可见 | ②组只能从此选择 `view_id`；不自行生成观点 ID |
| `DiscussionItem` / `CollaborationMessage` | ②组声明观点、质疑、证据、修订原因和反馈；协作层生成身份和讨论 ID，并验证回应对象 | ②组输出前者；④组读取后者分析互动与修订 |
| `AgentResponse.round_decision` | 当前角色的阶段方案；不是整项任务最终版本 | ②组新实现建议使用；旧 `final_decision` 字段继续接受，含义同样是阶段方案 |
| `strategy_version` / `proposed_strategy_version` | 本任务输入版本与可选候选版本标识，不加载或自动切换策略 | ②组解析自身版本；④组读取及管理候选产物，下一任务显式传入新版本 |
| `Orchestrator` / `GraphSelector` / `Message` / `Step` | 有限计划、受控消息传递、引用验证、状态、超时、重试、最终聚合 | 协作机制层维护；②组不能通过输出扩大通信范围 |
| `StrategyReport` / `TaskRecord.result` | 核心结论、关键发现、跨域关联、分歧、行动及待验证事项；保留来源与原始结果 | ④组读取最终报告；引用有效不代表业务判断正确 |
| `EvolutionEvent` | 终态快照、轨迹边界、版本元数据；`evaluation=not_evaluated` | ④组消费，并自行评价效果；不能据讨论结束宣称 Agent 能力提升 |
| `GET /api/contracts` | JSON Schema 契约发现 | ②组、④组均可读取 |
| `POST /api/tasks`、`GET /api/tasks/{id}` | 提交实验与查询任务、步骤、事件 | ②组联调、④组对照实验 |
| `GET /api/evolution-events?after_seq=...` | 按游标增量拉取终态事件；按事件 ID 去重 | ④组主要读取入口，自行持久化 `next_cursor` |
| `GET /api/tasks/{id}/evolution` | 完整执行轨迹和终态事件，用于离线评估 | ④组读取；不用于自动扩大下一轮模型上下文 |

## 固定轮次结束与最终报告

调用路径：`main.create_task → Orchestrator.run → build_plan → execute_step → call_agent → run_request/run → validate_references → publish_discussion`，重复执行有限的已计划步骤。全部讨论成功后，`begin_aggregation()` 记录聚合入口；DeepSeek 模式执行 `DeepSeekAggregator.run()`，本地模式执行 `aggregate_business() → local_report()`。最后记录 `results_aggregated`、任务成功和 `EvolutionEvent`。

`execution_phase` 明确区分 pending、discussion、aggregation、completed、failed；`termination_reason=round_limit` 表示固定多轮计划结束，不表示共识。`consensus_status=not_established`、`consensus_claimed=false` 是保守标记：即使没有发现明确冲突，也不自动宣布所有角色同意。最终报告中有依据的分歧、未回应问题应保留。

`max_retries` 是每个步骤的额外重试次数。引用冲突或无来源聚合结论会得到明确反馈并有限重试；失败不会污染会话或发布观点。预算耗尽则任务失败、后续步骤跳过，保留已成功的阶段方案，不生成假冒完整性的最终报告。不会无限追加讨论轮数。

引用错误曾由双 ID 的模型历史放大：校验后补齐的 `message_id/quote` 曾写回 assistant 历史。现在 `references.model_history_result()` 仅向模型返回 `view_id/relation/viewpoint`；完整映射留在审计结果。`validate_references()` 继续拒绝真实冲突，不删除非法字段来绕过校验。

最终聚合可读取本任务已完成的公开讨论，避免遗漏早前质疑；这个权限不授予普通业务 Agent。报告证据可只提供合法 `source_id`，后端补齐来源原文；主动填写的 `quote` 仍必须逐字匹配。不存在的来源、编造引文、未引用支持的新数字都会拒绝，并提供具体位置和有限修复机会。引用合法只是可追溯性检查，语义真实性和是否真的解决分歧仍由④组评估。

网页选择 DeepSeek 时，新任务默认启用 DeepSeek 聚合，可手动改为本地报告。API 的 `aggregation` 默认值仍为 `deterministic`，兼容已有调用；需要模型整合时显式传 `"aggregation":"deepseek"`。每轮只显示阶段方案，真正的最终报告完成后自动展开，原始 JSON 和来源仍可折叠查看。

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
| visible_views | 后端从本次 context_from 生成的观点目录：view_id、来源消息、Agent、轮次与核心观点 |

AgentResponse：回显 `request_id/task_id/agent_id/strategy_version`，并提供 `result: AgentResult`、`discussion: list[DiscussionItem]`、`round_decision`（兼容旧名 `final_decision`）。可选 `proposed_strategy_version` 是外部 Agent 声明的候选版本标识，仅记录，不自动应用。输出版本不能替代输入版本。

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

- `source_message_ids` 推荐引用本次 `visible_views` 中的观点 ID；旧接口仍支持本次 `inputs` 的结果消息 ID、自己的 `previous_result_message_id`，或 `case`。别名必须指向同一可见来源。
- `reply_to` 关联本次确实收到的 CollaborationMessage ID；用于说明回应了哪条具体质疑。没有回应则为空，不强制采纳观点。
- 每个成功的结构化返回：首轮至少一条 initial_view，后续轮至少一条有原因的 revision；保留原案也必须解释原因。
- 公开不等于送达。在固定顺序的 AAD 中，最后一个 Agent 发出的质疑可能要到下一轮才被前面的 Agent 看到。页面会明确标注，不能把未送达的质疑说成已回应。
- `final_decision` 是各 Agent 的本轮决策，页面取最后成功轮展示。综合报告属于汇总节点，不代表所有 Agent 已达成共识。

## 稳定观点引用与修复

`app/references.py` 集中处理观点目录和引用校验，`business.validate_references` 保留原导入入口。`view_id = view:{task_id}:{step_id}` 由后端生成，一个成功步骤对应一个观点；传输用 `Message.id` 继续独立生成，不能混为观点身份。请求重试只产生新的请求 ID，目录中的成功观点 ID 不变。

目录只从当前请求的真实 `inputs/context_from` 建立，不查询全局任务历史。AAD 盲起草目录为空；随后按固定顺序开放此前成功结果。Sequential 从首轮开始按同样顺序开放。允许引用当前目录中包含的旧轮观点；历史看过但本次目录没有的观点、自己当前观点、未来或跨任务观点均不合法。

模型推荐返回：

```json
{"references":[{"view_id":"从当前目录复制的ID","relation":"coordinate","viewpoint":"该观点如何影响本轮方案"}]}
```

无引用可返回 `references: []`，也可省略该字段。模型无需复写引文；后端确认 ID 在本次目录后，从对应成功结果解析来源原文，并补充传输消息 ID 以兼容旧页面和轨迹。若接入方提供了旧式 `message_id/quote`，仍严格校验 ID 与逐字引文；同一引用提供两种 ID 时必须指向同一来源，绝不静默改写错引文。

`invalid_view_reference` 会细分 `unknown_view/quote_mismatch/conflicting_ids/invalid_structure`，记录引用索引及本次合法 ID，不回显未知 ID 或错引文。出现非法引用时，Orchestrator 在已有 max_retries 预算内，把分类、错误位置和当前目录提供给同一 Agent，要求重新选择合法 ID 或明确不引用。失败返回不提交私有历史、不发布为观点、不提供给其他 Agent；预算耗尽正常失败，不扩大目录或自动删除非法引用。

内置 Mock、DeepSeek 与外部 `run_request` 接入均在成功发布前经过同一引用检查。模型上下文不再递归重复同伴结果里的旧引用链，默认仅输出核心观点、必要讨论与结论；完整分析可放 `data.details`。前端首屏保留核心观点、质疑、修订和决策，其余业务条目、引用依据、JSON/Prompt/Trace 折叠保留。

自动化专项测试见 `tests/test_references.py`，覆盖 ID 稳定性、正常与伪造引用、可见旧轮/当前轮观点、不可见/未来/跨任务观点、旧引文校验、引用批次原子性、AAD/Sequential 目录一致性及有限修复。

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
