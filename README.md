# 透明 Multi-Agent 协作 Demo

## 浅色分析工作台、策略汇总与图路由

当前版本在原调度器上增量加入以下功能，仍使用 HTML/CSS/JavaScript，没有前端框架或构建步骤。

### 按阶段运行和验证

1. UI 阶段：浅灰背景、白色卡片、橙色强调；`index.html` 负责结构，
   `static/workbench.css` 负责样式，`static/workbench.js` 的 `projectResult / compareResults / drawRounds / drawReport`
   把旧结果投影为可读卡片。原始 JSON、Prompt、消息和完整执行轨迹均折叠保留。
   第一阶段验证了原 33 项 Python 测试、3 项展示函数测试，并在浏览器运行 Mock。
2. Aggregator 阶段：`business.py` 加入跨领域虚构经营案例、专业发现、建议、分歧、修订与引用。
   `aggregation.py` 提供本地模板报告与 DeepSeekAggregator；汇总作为普通末端 Step，
   复用消息、超时、重试和状态机制。第二阶段 39 项 Python 测试通过。
3. 路由阶段：`graph_selector.py` 注册六个固定图，提供 GraphSelector 接口、RuleGraphSelector、
   LLMGraphSelector；由 Python 解析执行参数并校验计划，调度仍由原 Orchestrator 完成。
   前端 `controls.js` 增加路由、单角色对照与汇总方式，展示选择理由和模型调用分项。

### 一次完整请求经过哪里

```text
main.create_task()
  创建同任务的专业 Agent、可选 DeepSeekAggregator 与 LLMGraphSelector
  Orchestrator.run()
    selector.select() / manual_graph()      提出注册图 ID 和理由
    resolve_graph()                        Python 查表重建协议、角色与轮数
    configure_protocol()                   在会话开始前配置获准协议
    build_plan() → validate_plan()          复用显式计划并检查角色/通信边
    execute_step() → Agent.run()            原有路由、消息、超时、重试与状态更新
      prepare_user_message()               只注入该节点获准的公开结果
      validate_references()                校验 Agent 声明引用的消息与逐字原文
    可选 DeepSeekAggregator.run()           读取各角色最后一轮，不覆盖原始结论
      StrategyReport → validate_report()   校验字段、来源、数字与行动负责人
    保存最终 AgentResult
```

### 图选择与手动对照

网页可选择“手动固定”“规则选择”或“DeepSeek 选择”。Mock 可以使用手动/规则；
只有 DeepSeek 执行模式可以请求 LLMGraphSelector 或模型汇总。
手动保留 Independent、Sequential Debate、AAD，也可只运行营销、摇摆户或投诉 Agent。

| 注册图                                                 | 角色与通信                                   |
| ------------------------------------------------------ | -------------------------------------------- |
| single_marketing / single_hesitant / single_complaints | 只调用指定专业角色                           |
| parallel                                               | 三角色 Independent；旧请求可继续依次独立调用 |
| sequential                                             | 三角色顺序交流、可读取此前公开观点           |
| aad                                                    | 全员盲起草，屏障通过后顺序交流               |

`routing=manual` 为默认值，`graph_id=null` 时继续由旧 collaboration/mode 参数确定流程。
自动路由只使用任务与能力目录，不读取其他任务或角色私有历史。
模型输出必须严格为 `{graph_id, reason}`，额外 agents/edges/tools 字段一律拒绝。
Python 从不可变注册表重新解析参数，拒绝未注册能力、非法可见边、未来依赖和盲起草阶段通信。
非法模型选择、选择超时或网络错误会记录 `graph_selection_rejected` 并回退规则，
`task.graph_selection` 保留最终图、理由、实际协议、角色、轮数与回退原因。
`GET /api/graphs` 只读返回候选图。

该路由仅借鉴 AMAS 的按任务选择拓扑思路，没有训练、强化学习或声称复现论文性能。
参考：[AMAS](https://aclanthology.org/2025.emnlp-industry.144/)。

### 有来源的统一策略

本地模式输出明确标注的模板报告。DeepSeek 模式可额外开启一次策略汇总，包含：
核心问题、跨领域关联、冲突、带 owner / P0–P2 的行动建议与待验证事项。
所有自由叙述只能标为 assessment 或 recommendation，不允许模型生成 fact 类型。
每条必须有 source_id 和逐字 quote；Python 检查引用实际存在、覆盖所有参与者，
判断中的数字有对应引用支持，行动负责人属于本次参与角色。
summary 不允许新增数字断言；模型原始响应和验证失败可从轨迹查看。

引用校验不是语义证明：可能的因果关系仍显示为“分析判断、尚待验证”。
行动是建议，未被工具执行。模型可能误解有效引用，仍需要人工审阅。
`result.data.agents` 保留所有参与角色最终原始结论；初稿、中间修订与所有消息保留在 Step / events。
汇总失败时最终任务失败，但专业步骤及原始结果仍在。
页面的“可见观点”与“声明引用观点”分开，引用声明必须通过消息 ID 与原文校验。

### 本地运行与测试

```powershell
# 无需密钥：规则根据经营案例选择图，并生成本地报告
.\.venv\Scripts\python.exe -m app.business_demo --provider mock --routing rule
# 对照：只调用投诉 Agent
.\.venv\Scripts\python.exe -m app.business_demo --provider mock --graph-id single_complaints
# 真实模型：选图 + 业务协作 + 有来源汇总，会产生 API 用量
.\.venv\Scripts\python.exe -m app.business_demo --provider deepseek --routing llm --aggregation deepseek
# 全量后端测试，全部使用虚构数据和模拟服务，不联网
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
# 可选 Node 内置测试：旧任务投影与差异展示，不需要 npm 安装
node --test tests/test_workbench.cjs
```

模型请求计数来自 `model_request` 事件，包含失败的请求尝试；页面分为图选择、专业分析和策略汇总。
规则/手动选择不产生选择器模型请求。默认两轮 AAD 加模型选图与汇总，共 8 次请求，重试会增加次数。
旧任务没有 graph_selection / report / references 时，页面按旧字段展示，不补造选择理由或引用。

本次完整验证：48 项 Python 测试、3 项展示测试通过；虚构案例真实调用由 LLM 选为 AAD，
完成 1 次选图、6 次专业调用、1 次策略汇总，无重试、无回退。
报告保留 3 个角色原始结论，包含 3 项核心问题、3 项关联、2 项分歧、6 项行动、4 项待验证事项。
验证摘要在 `data/verification-summary.json`；完整真实报告在 `data/business-deepseek-llm-check.json`。

第一阶段成果：用中心调度器把一个目标拆成两个分析步骤，再交给汇总 Agent。
默认完全本地运行，无 API 密钥。Mock 内容是确定性的示例，并非模型自主分析。

第二阶段已加入三个业务角色和 DeepSeek 真实模式；默认页面仍选择 Mock，避免误触发调用。
原始流程继续保留，旧任务和原有接口参数仍可读取。

## 第三阶段：可观察的协作协议

新增 `TaskRequest.collaboration`，默认 `independent`，保持旧请求行为。
本阶段不替换 Agent 接口、SQLite 表、Message 类型或状态集合。
`mode` 仍表示 Independent 的并行/依次调用；它与讨论协议不同。
两种辩论协议使用固定顺序，需保留 `mode=parallel`，实际发言顺序由步骤依赖控制。

| 协议              | 第一轮                                             | 后续轮                                       | 最小调用数         |
| ----------------- | -------------------------------------------------- | -------------------------------------------- | ------------------ |
| Independent       | 三者各看案例，不看其他输出                         | 无                                           | 3                  |
| Sequential Debate | 营销 → 摇摆户 → 投诉，读取此前其他角色的公开结果 | 按相同顺序继续修订                           | 单轮 3；默认两轮 6 |
| AAD               | 三者全部独立起草，等待全部成功                     | 按固定顺序交流，读取此前其他角色已公开的结果 | 两轮 6             |

AAD 依据论文 §4.3 的 All-Agents Drafting 原则：第一轮独立产生草案后再允许交流。
本 Demo 的“后续固定顺序 + 可见已公开结果 + 本地合并”是明确选定的教学实现。
第二轮后发言者能看到本轮更早的修订结果，并非始终只看上一轮答案快照的 CI。
没有实现论文的七种投票/共识协议，也不复现其性能结论。
论文：[Voting or Consensus? Decision-Making in Multi-Agent Debate](https://aclanthology.org/2025.findings-acl.606/)。

### 从 HTTP 入口读一次 AAD

1. `app/main.py::create_task()` 接收 `workflow=business, collaboration=aad, rounds=2`。
   为每任务新建三个 Agent，继续调用同一个 `Orchestrator.run()`。
2. `app/orchestrator.py::build_plan()` 生成六个 Step：`r1.marketing`、`r1.hesitant`、
   `r1.complaints`、`r2.marketing`、`r2.hesitant`、`r2.complaints`。
3. `depends_on` 只控制调度屏障；`context_from` 单独控制允许读取的公开结果。
   首轮两者均为空，首个第二轮节点等待全部首轮成功，其余节点按序执行。
4. `execute_step()` 仍负责路由、超时、有限重试及状态更新，把允许的 result 消息放入 request.inputs。
   Message 和 Step 新增 `round_no`、`phase`，旧记录缺这些字段时使用默认值。
5. `app/business.py::prepare_user_message()` 验证协议和消息任务归属，禁止 Independent/AAD 草案读取同伴答案。
   只将显式公开的结果加入 user 内容，不读取其他 Agent 的私有 history。
6. `DeepSeekAgent.run()` 将本角色历史与当前 user 消息拼成实际模型 messages，
   记录 `agent_context` 和 `model_request` 后调用原 `DeepSeekAdapter.complete()`。
   Mock 记录同形状的 `agent_context`，但只生成可复现示例，不宣称调用了模型。
7. 成功后继续记录 result 消息和状态；失败重试不提交半轮历史。
   `aggregate_business()` 只合并每角色最后一轮的成功输出；全部初稿、讨论消息留在步骤与事件中。

同一角色后续轮次保留自己的历史。其他角色只获得已授权的公开 result；不获得该角色的 System Prompt、
原始 user 消息或私有会话。公开观点可能错误，提示词要求按观点而非事实处理，并保留分歧。
若草案失败，AAD 不开放第二轮；若顺序发言失败，之后依赖它的步骤跳过，不发布完整最终结果。

### 页面怎么观察

选择协议、总轮数和 Mock/DeepSeek。Independent 固定一轮，AAD 至少两轮，总轮数上限三轮。
“每轮上下文与实际消息”按轮次和角色展示：收到的公开消息来源与数量、
每次尝试的完整 messages、实际结构化 request/result、当轮输出。
Mock 明确标注为消费上下文；DeepSeek 的快照对应实际发送给适配器的 messages。
未执行或跳过的步骤不虚构上下文。旧任务没有新增快照时，页面明确提示查看原有消息轨迹。
最终合并结果包含 `source_steps`、`rounds`、`collaboration` 和 `consensus_claimed=false`。

对照默认两轮时的公开输入数，角色顺序固定为营销、摇摆户、投诉：

```text
Independent        R1: 0, 0, 0
Sequential Debate  R1: 0, 1, 2   R2: 2, 3, 4
AAD                R1: 0, 0, 0   R2: 2, 3, 4
```

第二轮不重复把自己的公开输出注入 user，因为它已经位于自己的私有 assistant 历史中。
公开输入按生成时间累计保留，因此能观察同一角色的草案与修订差异。

三个最小协议行为测试（不联网）：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_protocols.py -k test_minimal -v
```

带页面可读取的轨迹生成三种虚构案例记录：

```powershell
.\.venv\Scripts\python.exe -m app.business_demo --provider mock --collaboration independent
.\.venv\Scripts\python.exe -m app.business_demo --provider mock --collaboration sequential_debate --rounds 2
.\.venv\Scripts\python.exe -m app.business_demo --provider mock --collaboration aad --rounds 2
```

把 provider 改为 deepseek 可验证真实调用；默认两轮辩论需六次模型调用，重试会增加次数。
报告路径分别为 `data/business-mock-check.json`、`data/business-mock-sequential_debate-check.json`、
`data/business-mock-aad-check.json`。全量测试还验证真实适配器的请求快照（使用假服务）、
AAD 屏障、当前轮信息交换、失败阻断、最后一轮合并与参数校验。

## 第二阶段：DeepSeek 与三个独立业务 Agent

先在本机 `.env` 填写（可参照 `.env.example`）：

```dotenv
DEEPSEEK_API_KEY=在本机填写
DEEPSEEK_MODEL=deepseek-flash
```

`.env` 已忽略，不提交源码。配置只由 `app/config.py::load_settings()` 读取；
不把 Key 放入 TaskRequest、Message、AgentResult、SQLite 或前端。
`GET /api/config` 仅返回是否配置及模型名。每次提交真实任务重读 `.env`，更换 Key 无需重启。

按顺序验证，以下两个真实模式命令会调用 API 并产生用量：

```powershell
# 1. 先验证一次真实请求（固定虚构案例；无自动重试）
.\.venv\Scripts\python.exe -m app.check_deepseek
# 2. 对照离线三角色协作
.\.venv\Scripts\python.exe -m app.business_demo --provider mock
# 3. 验证三角色真实协作（固定虚构案例；每角色至多一次请求）
.\.venv\Scripts\python.exe -m app.business_demo --provider deepseek
```

单次成功报告写入 `data/deepseek-check.json`；CLI 三角色报告写入
`data/business-{provider}-check.json`。这些报告不含密钥。
三角色 CLI 和 Web 共用 `data/tasks.sqlite3`，页面历史中可以查看调用记录。

页面选择“三个业务 Agent 独立分析”与“DeepSeek / 真实 API”即可调用。
三个角色均为专业业务分析 Agent：营销 Agent 负责营销数据分析、效果评估、客群策略制定与优化；摇摆户 Agent 负责摇摆户客群分析、客户行为识别、经营策略制定与迭代；客户投诉 Agent 负责投诉数据分析、问题识别、原因归纳及改进策略制定。

三者均遵循“目标与数据口径 → 分析与问题识别 → 策略 → 验证与迭代”的闭环。摇摆户的定义优先采用任务中的业务口径；缺少定义时提出待验证的识别方法，不直接等同于犹豫购买者或流失客户。结果的 `data` 包含 `analysis_basis`、`findings`、`recommendations`、`validation_plan`、`dependencies`，历史结果仍按原字段展示。

独立任务与委派任务都通过 `Agent.run(Message)` 接收，`payload.prompt` 承载目标、数据、约束和期望交付物；模型输入中的 `requested_by` 保留消息发送方。其他 Agent 的子任务说明可经调度器转交，角色按本领域交付分析和策略。提示词不新增自动委派或任意通信权限，实际任务转发仍按已校验的协作图执行。

以下为默认 Independent 协议的行为。
它们默认并行分析同一虚构案例，由调度器在三者全部成功后确定性合并，无第四次汇总模型调用。
切为依次调用时只改变调度顺序，不传递角色私有历史，三者依旧独立。
如果任一角色失败，保留其他成功角色的步骤结果，但不发布完整合并结果。

### 新调用路径与修改文件

```text
main.create_task(TaskRequest(workflow="business", provider="deepseek"))
  config.load_settings()                      从本机 .env 读取同一个 Key
  DeepSeekAdapter(settings, shared_client)    无会话状态的 HTTP 适配器
  为本任务构造三个 business.DeepSeekAgent       三个对象、三个 System Prompt、三个 history
  Orchestrator.run(task)
    build_plan(..., workflow="business")      三个无上游依赖的步骤
    execute_step() → DeepSeekAgent.run(message)
      校验 task_id / recipient / inputs
      构造本角色的 system + user 消息
      trace(model_request)                   保存无认证头的实际模型输入
      DeepSeekAdapter.complete(messages)     单次 POST /chat/completions
        HTTP 错误分类 → JSON 解析 → AgentResult 校验
      成功后提交本角色 history
      trace(model_response)                  保存结果、模型名和 token 用量
    aggregate_business(steps)                在本地合并三份结果
    保存任务成功状态
```

`app/business.py` 集中定义角色、中文提示词、业务 Mock、私有会话与确定性合并。
`app/deepseek.py` 只负责供应商传输与校验；`app/main.py` 选择实现并管理连接池。
`app/models.py` 增加 workflow/provider 字段；原 `run(message) → AgentResult` 接口保持相同。
`app/orchestrator.py` 复用调度，只增加业务计划、依次调用和错误分类判断。

### 消息隔离的实际边界

每次任务创建新 Agent 对象，`session_id = task_id + ':' + agent_id`。
System Prompt 与 history 存在各个实例中，适配器不保存 messages。
不同任务无法复用该对象（task_id 不匹配立即拒绝），Independent 和 AAD 首轮拒绝非空上游 inputs。
每个实例有自己的锁，后续同会话轮次不会互相覆盖；失败或取消不会提交半轮历史。
重试只重发相同输入，不把失败的 user 消息重复追加到历史。
三个业务 Agent 不互相阅读结果，只有调度器合并成功输出。
会话锁和历史是本进程内存状态，不是供应商账号或多进程隔离机制。
页面目前每次提交是新任务，不提供跨任务连续聊天；单会话后续轮次支持由代码测试验证。

### 模型错误与验证

适配器固定访问 `https://api.deepseek.com/chat/completions`，使用 TLS，禁止自动重定向，
不继承环境代理。API Key 只出现在服务端发往官方 API 的 Authorization 请求头。
HTTPX 超时和调度器单次尝试超时均有上限，调度器的上限覆盖整个 Agent.run。
JSON 输出模式仍经过业务契约校验；截断输出被视为失败，不把半截结果发布成成功。

| 错误                              | 行为                                    |
| --------------------------------- | --------------------------------------- |
| 未配置 Key                        | HTTP 创建任务接口返回 503，不发模型请求 |
| 400 / 401 / 402 / 403 / 404 / 422 | 固定错误说明，不重试                    |
| 408 / 429 / 5xx、网络异常、超时   | 受 max_retries 限制重试                 |
| 非法 JSON、字段不匹配、截断       | 受预算限制重试，历史不提交              |

适配器没有隐式重试；业务真实模式页面默认重试 0 次，选择重试后才会增加调用次数。
模型类暂时错误使用 1×尝试次数秒退避，原 Mock 仍是 0.1×尝试次数秒。
服务不保存原始供应商错误体或异常请求对象，只保存安全错误码和说明。
实际请求的 System Prompt、案例和输出会保存到本地 SQLite 并可在页面展开。
当前仍以虚构数据演示；“独立会话”不表示模型在业务意义上具备独立知识库或工具权限。

默认自动化测试不会联网，使用假 Key 和 HTTPX MockTransport，包含：请求契约、角色与任务隔离、
失败历史回滚、HTTP 错误分类、401 不重试、503 重试、两种调用顺序、
前端接口与 SQLite 不含测试 Key，以及全部原有流程回归。

协议依据：[Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)、
[会话历史](https://api-docs.deepseek.com/guides/multi_round_chat/)、
[错误码](https://api-docs.deepseek.com/quick_start/error_codes/)。

## 运行

当前电脑已经创建 `.venv` 并安装依赖。在 PowerShell 中：

```powershell
cd D:\32Codex\Multi-Agent
.\.venv\Scripts\python.exe -m app.demo
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

浏览器打开 http://127.0.0.1:8000 ，接口文档 http://127.0.0.1:8000/docs 。
也可执行 `powershell -ExecutionPolicy Bypass -File .\start.ps1`；Ctrl+C 停止。

其他安装了 Python 3.11+ 的电脑：

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## 先理解一次调用

```text
浏览器 POST /api/tasks
  main.create_task(request)           校验输入、保存 pending 任务、启动后台协程
    Orchestrator.run(task)
      build_plan(mode)               规则生成步骤和依赖
      transition(..., running)       状态变化与快照在同一 SQLite 事务中保存
      找到全部依赖成功的 ready 步骤
      asyncio.gather(execute_step...) 并行执行同一轮，等待它们结束
        routes[capability]           按能力路由到已注册 Agent
        构造 Message(request)        包含任务、步骤、来源与上游结果消息
        Store.save(message)          先记录请求消息
        asyncio.wait_for(invoke())   限制单次尝试时长
          Agent.run(message)         实际业务处理
        构造 Message(result)         用 correlation_id 关联请求
        保存结果，更新状态
      下一轮执行 synthesis           接收两个分析结果消息
      保存 task.result 与成功状态
浏览器 GET /api/tasks/{id}            每 400ms 查询任务快照与有序事件
```

并行计划：`[requirements, risks] → [synthesis]`。
串行计划：`[requirements] → [risks] → [synthesis]`。
串行模式中的风险 Agent 会收到需求分析结果；当前 Mock 风险规则仍是固定内容。

## 文件与阅读顺序

| 文件                       | 职责                                               |
| -------------------------- | -------------------------------------------------- |
| `app/models.py`          | Pydantic 输入、任务、步骤、消息、结果契约          |
| `app/agents.py`          | Agent Protocol、三个 Mock Agent、默认注册表        |
| `app/orchestrator.py`    | 计划、能力路由、依赖屏障、超时重试、消息转交       |
| `app/store.py`           | SQLite 快照与追加事件，按事件序号确定调用顺序      |
| `app/demo.py`            | 最小命令行入口，不经过 HTTP                        |
| `app/main.py`            | FastAPI Web 入口、后台任务生命周期、查询接口       |
| `app/static/index.html`  | 无构建依赖的本地页面                               |
| `app/deepseek.py`        | 统一 DeepSeek HTTP 适配器、JSON 校验和安全错误分类 |
| `app/business.py`        | 三个业务角色、独立会话、业务 Mock 和本地结果合并   |
| `app/config.py`          | 仅服务端读取 .env                                  |
| `app/check_deepseek.py`  | 固定虚构数据的单次真实调用入口                     |
| `app/business_demo.py`   | 三角色端到端命令行入口                             |
| `tests/test_workflow.py` | 自动化行为测试                                     |

先读 `models.py` 的 Message 和 Step，再读 `agents.py` 的 run，最后读
`orchestrator.py` 的 build_plan → run → execute_step，即可串起底层实现。

## 状态与消息的设计

- 任务：pending → running → succeeded / failed。
- 步骤：pending → running → succeeded；失败时 running → retrying → running，耗尽预算后 failed。
- 上游失败使下游 skipped，最终任务失败，不发布不完整汇总。
- `max_retries=1` 表示最多调用两次，每次超时独立计算；重试前等待 0.1×尝试次数秒。
- 为便于演示，普通调用异常都有限重试。真实接入时应区分永久错误与暂时错误。
- 消息由调度器中转，无独立队列。请求含上游结果消息；结果用 correlation_id 关联请求。
- 每次正常状态变化、调用请求、结果、失败均追加到 events 表；tasks 表保存最新完整快照。
- 快照与对应事件同事务提交。消息及状态是两个显式事件，不宣称整个步骤具备原子性。
- seq 为 SQLite 全局递增序号；并发事件的顺序反映落库顺序，不代表 CPU 指令级时间。

## 自己验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

页面操作：

1. 正常并行：需求和风险同时运行，之后汇总。
2. 切到串行：风险必须在需求结束后开始。
3. 首次失败 + 重试 1：风险调用两次后成功，展开 attempt_error 与消息查看关联。
4. 持续失败 + 重试 1：风险失败，汇总跳过，无最终结果。
5. 超时：将超时设置为 0.5 秒，风险 Agent 会被取消并按预算重试。
6. 刷新页面后点击历史任务，检查记录仍可读取。

测试包含真实并发屏障、串行顺序、消息来源、有限重试、超时、失败传播、任务隔离、
路由缺失、HTTP 完整流程、输入校验及重启中断标记。

## 新增：讨论轨迹与进化模块接入

统一输入输出、五种讨论消息、策略版本与进化事件的完整说明见 [协作接口说明](docs/collaboration-contracts.md)。
本阶段沿用现有协作图、状态机和 SQLite 表；增加 `agent_contract.py` 兼容层与按 Agent 展示的讨论视图。
刷新网页，选择 Mock / AAD / 两轮，即可观察初稿、实际收到的质疑、修订原因与最终决策。
高级设置可填写策略版本标识；本地 Demo 只传递标识，不加载外部策略、不训练、不自动修改 Prompt。

## 接入其他团队 Agent

```python
class TeamAgent:
    id = "team-analyst"
    name = "团队需求分析"
    capability = "requirements"

    async def run(self, message: Message) -> AgentResult:
        return AgentResult(summary="实际分析", data={"items": []})
```

替换 `default_agents()` 中相同能力的实现即可。相同能力重复注册会被拒绝，避免路由歧义。
Agent 不应修改任务状态；应遵守 asyncio 取消，不使用阻塞 I/O。
调度器在输出边界再执行一次 Pydantic 校验。
如果 Agent 会发送邮件、扣款等，接入方必须自行实现幂等：本 Demo 不保证 exactly-once。

DeepSeek 接入和三角色会话实现见前面的第二阶段说明；原始需求分析流程继续使用 Mock。

## 本阶段边界与下一阶段

本阶段先运行 CLI，随后加入 Web 观察、SQLite 与异常测试；核心调度只有一份。
没有引入 LangGraph、消息中间件或分布式任务队列。
当前是单进程本地教学工具：请仅使用一个 Uvicorn worker，不用 reload 演示持久任务。
SQLite 使用短同步事务，适合少量本地任务；尚无背压、归档、鉴权或多进程协调。
进程关闭或重启后未完成任务标记失败，保留已有轨迹，不自动恢复执行。
历史列表显示最近 50 条，数据库保留全部任务；CLI 与 Web 使用两个独立数据库。
轨迹保存结构化输出和错误类型/文本，不保存模型内部思维过程或 Python 堆栈。
轮次屏障实现简单，但与任意复杂 DAG 的按节点即时唤醒调度不同。

后续可先逐行解释真实调用轨迹，再讨论角色间协商、多轮对话入口或通用计划验证。
