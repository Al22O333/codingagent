# Agent Design Requirements & Compliance Boundaries

> 任务 1 最终产物。本文只记录会直接影响 Coding Agent 能力、架构、Runtime 职责、依赖选择、安全边界和设计可解释性的要求。

## 0. 文档目的与范围

本文为后续设计提供统一的合规基线，回答以下问题：

1. Agent 最低限度必须具备哪些能力？
2. 哪些核心职责必须由项目自己的代码承担？
3. 哪些能力可以使用模型 SDK、普通工程库或本地 CLI？
4. 哪些职责不得交给 Agent Framework、现成 Agent 或服务端托管工具？
5. 哪些结论是题目或邮件明文，哪些只是合规解释或设计原则？
6. 后续方案如何判断是否越过题目边界？

一项要求只有在会影响以下至少一项内容时，才纳入本文：

- Agent 的必要能力
- Runtime 的职责归属
- 依赖选择
- 本地与服务端执行边界
- Secret 处理方式
- 设计复杂度与可解释性
- 后续架构方案的合规判断

### 0.1 信息来源标签

| 标签 | 含义 |
|---|---|
| `[PDF 明文]` | 题目 PDF 直接提出或明确列举的要求 |
| `[邮件明文]` | 通知邮件直接提出的要求 |
| `[合规解释]` | 为落实明文要求所作的保守解释，不是原文逐字表述 |
| `[设计原则]` | 根据考核目标形成的设计取向，不是外部强制规定 |

同时带有多个标签时，表示该条目由明文要求和必要解释共同构成。

### 0.2 不纳入本文的事项

以下事项不属于当前 Agent 设计约束，由独立且不编号的 `submission-checklist.md` 管理：

- README.txt 的内容和字数
- 演示视频的时长、格式和大小
- ZIP 的结构与命名
- 截止时间和上传方式
- 双盲、个人身份信息和英文介绍
- 公开仓库的提交操作流程

它们不因被排除在本文之外而失效，只是不参与当前 Agent 架构设计。

---

## 1. 术语与责任主体

### 1.1 Agent Runtime

项目中负责驱动 Agent 运行的本地程序。它接收用户任务，与模型交互，解释模型响应，调度本地 Tool，维护上下文和运行状态，并决定继续或终止。

### 1.2 Model Client

负责与模型 API 通信的组件。它可以建立在模型厂商 API Client 或 OpenAI-compatible Client 之上，但不能拥有 Agent 的控制流程或直接执行 Tool。

### 1.3 Tool

由项目暴露给模型的本地能力。本文只规定其定义和本地执行必须受自己的 Runtime 控制；具体抽象与协议以 05、具体 Tool 集合与行为以 06 为准。

### 1.4 Agent Orchestration

决定 Agent 如何循环、何时调用模型、如何处理 Tool Call、如何更新上下文与状态、如何恢复错误以及何时终止的逻辑。

### 1.5 现成 Agent 与 Agent Framework

- **现成 Agent 产品：**Claude Code、Codex、OpenCode、Aider 等能够独立接收并完成编程任务的产品或程序。
- **Agent Framework / SDK：**为应用提供 Agent Loop、Tool Orchestration、Memory、Planning、状态推进或 Termination 等编排能力的框架或 SDK。

是否越界以依赖实际承担的职责为准，而不只看名称。

---

## 2. Minimum Functional Requirements

### FR-01：与大语言模型交互

**来源：**`[PDF 明文]`

Agent 必须通过与大语言模型交互来完成编程任务。运行时模型、模型厂商和具体模型能力不受题目限制。

### FR-02：读取本地文件

**来源：**`[PDF 明文]`

Agent 必须能够读取完成编程任务所需的本地文件。本文不固定读取范围、路径规则、文件大小和二进制文件策略；安全与 workspace 语义以 03 为准，具体 Tool 行为以 06 为准。

### FR-03：写入本地文件

**来源：**`[PDF 明文]`

Agent 必须能够创建、写入或修改本地文件。本文不固定具体编辑机制、冲突处理和验证方式；其 canonical owner 是 06。

### FR-04：执行本地命令

**来源：**`[PDF 明文]`

Agent 必须能够执行本地命令。本文不固定命令表示方式、Shell、超时、权限、工作目录和输出限制；安全语义以 03、Runtime lifecycle 以 04、具体执行机制以 06、默认配置以 09 为准。

### FR-05：自主推进编程任务

**来源：**`[PDF 明文]` `[合规解释]`

题目要求 Agent 能够“自主”完成编程任务，并明确要求自行负责上下文、Tool、本地执行和循环终止。因此 Agent 不能只生成一次代码文本；它必须能够根据模型决策执行本地动作、观察结果并继续推进，直到完成或触发终止条件。

“自主”不等于不受限制。权限确认与安全策略以 03 为准，用户中断与 Runtime lifecycle 以 04 为准。

### FR-06：完成真实编程任务

**来源：**`[PDF 明文]`

Agent 的目标是完成用户交给它的编程任务，而不只是回答编程知识、生成代码片段或演示 Tool Calling。具体支持的任务类型和产品范围由 Product Scope 决定。

### FR-07：形成可终止的执行闭环

**来源：**`[PDF 明文]` `[合规解释]`

Agent 必须能够在模型交互、本地执行和结果反馈之间形成闭环，并拥有由自己 Runtime 控制的终止机制。具体终止条件不在本文提前决定。

---

## 3. Required Self-implemented Responsibilities

题目使用“包括但不限于”列举重要逻辑，因此下表是最低责任集合，不代表只需自行负责这些逻辑。

| ID | 必须由自己的项目掌握的职责 | 来源 | 边界说明 |
|---|---|---|---|
| SR-01 | Conversation History Management | `[PDF 明文]` | 决定保存哪些对话消息及其顺序，不能由 Agent Framework 代管 |
| SR-02 | Context Management | `[PDF 明文]` | 决定哪些信息进入模型上下文、如何保留、裁剪或失效 |
| SR-03 | Tool Definition | `[PDF 明文]` | 自己定义模型可调用的本地能力及其语义 |
| SR-04 | Tool Local Execution | `[PDF 明文]` | Tool 必须由自己的本地程序执行，不能交给模型服务端 |
| SR-05 | Model Output Interpretation | `[PDF 明文]` | 识别文本、Tool Call 和异常响应，并转换为 Runtime 可处理的结果 |
| SR-06 | Agent Control Loop | `[PDF 明文]` `[合规解释]` | 自己决定模型调用、动作执行、观察反馈和下一步推进 |
| SR-07 | Termination Control | `[PDF 明文]` | 自己定义并执行语义终止和强制终止机制 |
| SR-08 | Error Handling | `[PDF 明文]` | 自己决定错误分类、反馈、重试、恢复或终止 |

### 3.1 “自行实现”的含义

**来源：**`[合规解释]`

“自行实现”指项目自己的代码必须拥有相关决策权和控制流程，不表示必须重新实现所有底层基础设施。例如：

- 可以使用 HTTP Client 发送 API 请求，但何时调用模型由自己的 Runtime 决定。
- 可以使用 SDK 将响应解析为厂商对象，但文本与 Tool Call 的语义解释、内部转换和状态推进由自己的 Runtime 决定。
- 可以使用 JSON Schema 库验证数据，但 Tool 参数规则和验证失败后的行为由自己的 Runtime 决定。
- 可以调用本地 Git、搜索或测试程序，但调用时机、参数、权限和结果处理由自己的 Runtime 决定。
- 可以使用数据结构、日志、配置和测试库，但不能让它们接管 Agent Lifecycle。

### 3.2 模型输出解析边界

**来源：**`[PDF 明文]` `[合规解释]`

自行负责模型输出解析，不要求自己实现 HTTP 协议、JSON Parser 或模型 SDK 已提供的底层反序列化。项目至少必须自行负责：

- 区分普通文本、Tool Call 和无效响应
- 提取并检查 Tool 名称、Call ID 和参数
- 将厂商响应转换为自己的内部语义
- 决定无效或不支持的输出如何进入错误处理
- 决定响应如何影响上下文、状态和下一轮执行

---

## 4. Allowed Model Capabilities

### AL-01：模型厂商 API Client

**来源：**`[PDF 明文]`

允许使用模型厂商提供的 API Client。Client 应负责通信和协议适配，不应拥有 Agent Orchestration。

### AL-02：OpenAI-compatible API 网关

**来源：**`[PDF 明文]`

允许通过 OpenAI-compatible 网关访问模型。网关不能替项目执行本地文件操作、代码执行或 Agent Lifecycle。

### AL-03：模型原生 Tool / Function Calling

**来源：**`[PDF 明文]`

允许使用模型原生 Tool Calling 或 Function Calling 产生结构化动作。模型可以提出 Tool Call，但调用的解释、检查、调度、本地执行和结果回传必须经过自己的 Runtime。

模型原生 Tool Calling 是模型输出协议，不等同于 Agent Runtime。

### AL-04：运行时模型与编程语言

**来源：**`[PDF 明文]`

运行时模型和项目编程语言不限。Implementation Language 由 05 决定并由 10 记录 ADR；Model Interface 由 05 决定；具体默认 Provider / Model 和配置方式由 09 决定。

---

## 5. Forbidden Runtime Delegation

### FB-01：在现成 Agent 产品上封装界面

**来源：**`[PDF 明文]`

不得在现成 Agent 产品外增加 CLI、GUI 或 Web 界面后，将其作为自己的 Agent 提交。项目必须拥有独立运行的 Agent Runtime。

### FB-02：使用 Agent Framework / Agent SDK

**来源：**`[PDF 明文]`

不得使用任何承担 Agent Orchestration 的现成 Agent Framework / SDK。题目列举的示例包括：

- LangChain
- LlamaIndex
- OpenAI Agents SDK
- Claude Agent SDK
- AutoGen
- CrewAI
- 其他承担同类职责的框架或 SDK

如果一个依赖替项目承担以下任一核心职责，应视为高风险或禁止：

- Agent Loop
- Tool Orchestration
- Context / Memory Orchestration
- Planning / Execution Lifecycle
- Agent 状态推进
- Termination Control
- 面向 Agent 的自动错误恢复流程

### FB-03：服务端托管的代码执行或文件工具

**来源：**`[PDF 明文]`

不得依赖 API 服务端托管的代码执行或文件工具，例如 Code Interpreter、Files API，以及其他替代本地 Tool Runtime 的托管执行能力。

禁止的核心不是“使用网络 API”，而是让模型服务端替自己的 Runtime 操作文件或执行代码。

### FB-04：将任务转交给现成 Agent

**来源：**`[合规解释]`

提交项目运行时不得把用户交给自己 Agent 的编程任务转发或委托给 Claude Code、Codex、OpenCode、Aider 等现成 Agent 完成。否则即使外层存在自己的接口，也不再拥有题目要求的核心 Runtime。

该限制只针对提交项目的运行时，不限制开发阶段使用现成 AI 工具辅助设计、实现、测试和 review。

---

## 6. Responsibility Boundary Matrix

本表只确定责任归属，不提前决定组件数量、类、接口或数据结构。

| 能力或职责 | 自己的 Runtime 必须负责 | 可以委托的底层能力 | 不得委托给 |
|---|---|---|---|
| 模型调用时机 | 是 | HTTP 连接、认证、重试原语 | Agent Framework 的 Lifecycle |
| API 协议反序列化 | 否 | 模型 SDK、JSON 库 | - |
| 响应语义解释 | 是 | Schema 校验原语 | Agent Framework 的自动执行器 |
| History 与 Context 策略 | 是 | Tokenizer、数据结构库 | Agent Memory / Context Framework |
| Tool 语义与定义 | 是 | Schema 描述库 | 现成 Agent Tool Runtime |
| Tool Call 调度 | 是 | 通用注册表或映射数据结构 | Agent Framework Orchestrator |
| Tool 本地执行控制 | 是 | OS API、普通库、本地 CLI | 模型服务端执行工具 |
| 文件、进程底层操作 | 否 | 标准库、OS API、本地程序 | 服务端托管文件或代码工具 |
| Agent 状态推进 | 是 | 通用状态数据结构 | Agent Framework |
| 错误恢复决策 | 是 | 通信库的有限底层重试 | Agent Framework 的自动恢复流程 |
| 终止决策与强制限制 | 是 | Timer、计数器、取消原语 | Agent Framework |

“可以委托”只表示可以复用底层工程能力，最终控制权仍受自己的 Runtime 约束。

---

## 7. Ordinary Third-party Dependencies

### 7.1 允许原则

**来源：**`[PDF 明文]` `[合规解释]`

题目明确允许模型 API Client，并禁止的是 Agent Framework / SDK，而不是所有第三方库。因此可以使用不承担 Agent Orchestration 的普通工程依赖，例如：

- CLI / Terminal UI 库
- 日志库
- 配置与环境变量库
- Schema 与参数校验库
- Tokenizer
- Diff / Patch 处理库
- 代码解析库
- 测试库
- HTTP Client
- 数据结构和序列化库
- 本地 Git、搜索、编译、测试和格式化程序
- 操作系统提供的文件与进程能力

### 7.2 依赖合规判断

引入依赖前，应检查：

1. 它是否决定 Agent 下一步做什么？
2. 它是否维护或裁剪 Agent Context / Memory？
3. 它是否自动注册、调度、执行 Tool 并回传结果？
4. 它是否推进 Agent 状态或管理 Planning / Execution Lifecycle？
5. 它是否决定 Agent 何时终止或如何从错误中恢复？
6. 它是否把文件操作或代码执行迁移到 API 服务端？

任一问题回答“是”，都需要进一步证明该依赖没有接管题目要求自行实现的重要逻辑；无法证明时不应使用。

“删除依赖后是否仍拥有完整 Agent Control Loop”可以作为辅助检查，但不是唯一判断标准。最终应以职责归属和实际调用链为准。

---

## 8. Tool and Local Execution Boundary

### 8.1 当前阶段确定的边界

**来源：**`[PDF 明文]` `[合规解释]`

- Tool 的语义和定义由自己的项目掌握。
- Tool Call 必须进入自己的 Runtime，而不能绕过 Runtime 直接执行。
- Tool 的本地执行由自己的程序发起并控制。
- 可以使用普通库、标准库、OS API 或本地 CLI 完成底层操作。
- Tool 执行结果必须返回自己的 Runtime，由 Runtime 决定如何进入后续模型交互。
- 任何服务端托管 Tool 都不能替代本地文件和命令执行能力。

### 8.2 不由本 Requirements 文档固定的具体设计

以下事项不是题目直接固定的设计答案，其具体方案由对应 canonical owner 文档决定。本文不追踪这些事项的实时决策状态；其中 Permission、Shell Execution Boundary 与 Workspace Path / Workspace Safety Policy 的当前结论以 `03-safety-and-execution-boundaries.md` 为准。

- Tool 统一接口和内部类型
- Tool Schema 的具体表示
- ToolResult / ToolError 的数据结构
- Tool 注册方式
- Tool 集合及粒度
- Tool Call 并行或串行
- 参数校验技术方案
- 权限等级和用户确认机制
- 文件编辑方式
- Shell 的安全策略
- Workspace 的路径限制

---

## 9. AI-assisted Development and Explainability

### 9.1 AI 辅助开发

**来源：**`[PDF 明文]` `[邮件明文]`

允许并鼓励使用任何 AI 工具辅助开发，包括使用 Claude Code、Codex、ChatGPT、Cursor、OpenCode 等进行：

- 设计讨论
- 编码
- Debug
- 重构
- 测试
- Code Review
- 缺陷分析
- 文档整理

开发工具可以参与开发过程，但不能成为提交项目的运行时依赖或替代项目自己的 Agent Runtime。

### 9.2 设计责任

**来源：**`[PDF 明文]` `[邮件明文]`

最终必须能够理解并为项目中的设计负责，至少包括：

- Agent 为什么按所选方式运行
- 各组件的职责和边界
- Agent Control Flow
- Context 与 State 策略
- Tool 行为与安全边界
- 错误处理与终止机制
- 关键方案的替代选项和 trade-off
- AI 辅助生成代码中最终保留的关键实现

### 9.3 复杂度原则

**来源：**`[PDF 明文]` `[邮件明文]` `[设计原则]`

题目允许功能简单或完善；面试明确关注对运行机制的理解和对设计决策的辩护。因此：

> 在功能数量与可解释性发生冲突时，优先保证核心闭环完整、职责清晰、行为可验证且能够充分解释。

这不表示功能数量没有价值，而表示新增复杂度必须有清晰收益，并且不能削弱对核心 Runtime 的掌握。

---

## 10. Secret Boundary

### SEC-01：Secret 来源

**来源：**`[PDF 明文]`

API key、token 等 Secret 必须通过环境变量或未纳入版本控制的本地配置提供，不得硬编码进源代码。

### SEC-02：Runtime 中的必要使用

**来源：**`[合规解释]`

Runtime 可以在调用模型 API 时读取 Secret。Secret 进入 Runtime 进程内存是正常且必要的，但其可见范围必须限制在需要认证的组件。

### SEC-03：禁止泄露

**来源：**`[PDF 明文]` `[合规解释]`

本文区分：

- **Runtime Secret：**Agent 自身用于调用模型或其他服务的 API key、Provider token 和认证凭据。
- **Workspace Sensitive Data：**项目 workspace 中可能存在的 `.env`、私钥、credentials 等敏感内容。

Runtime Secret 不得：

- 写入受版本控制的文件或 Git 历史
- 出现在源代码默认值或示例中
- 输出到用户日志或 Debug 日志
- 进入发送给模型的 Conversation Context
- 进入 Tool 参数、ToolResult 或错误消息
- 因异常堆栈、配置打印或环境转储而泄露

Workspace Sensitive Data 不等同于 Runtime Secret。其 discovery、访问和模型可见性受 03 的 Sensitive Path Policy 约束；只有在 Agent 合理判断任务需要且用户对具体访问显式确认后，相关内容才可发送给模型。

具体 Runtime Secret 加载与配置优先级由 09 定义。Workspace Sensitive Path 的安全语义以 03、具体 pattern 与 discovery 行为以 06、相关用户配置与展示以 09 为准。

---

## 11. Ambiguities and Interpretive Boundaries

### A-01：单 Agent 与多 Agent

**性质：**`[合规解释]`

题目要求实现“一个编程智能体”，没有明确说明内部是否可以包含多个 Agent、角色或模型调用者。因此不能把 Multi-agent 直接视为题目明确开放的能力。

本文不负责选择具体 Runtime execution model；当前 v1 结论以 02 与 04 为准。若对应 owner 文档改变为 Multi-agent，仍必须满足：

- 所有 Agent Orchestration 仍由自己的代码实现
- 不使用 Multi-agent Framework / SDK
- 能说明其相对单 Agent 的必要收益
- 不因复杂度增加而削弱可解释性

是否采用由 Product Scope 与 Agent Execution Model 决定。

### A-02：参考现有 Agent 的实现

**状态：**`[合规解释]`

题目以多个现有 Agent 为目标参照，允许并鼓励使用 AI 工具辅助开发，且未禁止阅读公开资料。因此可以研究其架构、源码和设计思想，并在理解后自行实现类似方案。

不能据此复制无法解释的核心实现，也不能把参考对象变成提交项目的运行时核心。

### A-03：普通库与 Agent Framework 的分界

**状态：**`[合规解释]`

库名、宣传方式和包分类不能单独决定合规性。最终判断依据是它在实际调用链中是否承担 Agent Orchestration 或替代本地 Tool Runtime。

### A-04：“包括但不限于”的范围

**状态：**`[合规解释]`

PDF 对必须自行实现的重要逻辑使用“包括但不限于”，意味着不能通过只实现列举项目、再把其他核心控制职责交给框架来规避限制。

后续若出现新的核心职责，应判断它是否决定 Agent 的循环、状态、上下文、动作或终止；若是，默认应由自己的 Runtime 掌握。

### A-05：Tool Calling 是否等于模型输出解析

**状态：**`[合规解释]`

模型 SDK 可以完成协议层反序列化，原生 Tool Calling 可以提供结构化输出，但它们不能替 Runtime 决定调用是否合法、如何执行、如何更新状态以及是否继续。

---

## 12. Design Questions Not Decided by This Requirements Document

下列内容未被题目固定，不由本 Requirements 文档决定。此处只记录 canonical owner，不跟踪问题的实时决策状态；当前结论始终以对应 owner 文档为准。

| 设计问题 | Canonical Owner |
|---|---|
| Product Scope 与主要使用场景 | 02 |
| Permission、Trust、Workspace 与 Shell 边界 | 03 |
| Runtime、State、Termination 与 Recovery | 04 |
| Implementation Language、Component 与 Protocol | 05 |
| Tool Set 与 File Editing | 06 |
| Context 与 Prompt Policy | 07 |
| Verification、Testing 与 Demo | 08 |
| CLI、Provider Configuration、Observability 与 Secret Loading | 09 |
| Lightweight Architecture Decision Records | 10 |

开放设计不表示可以违反本文的 Runtime Ownership 和禁止依赖边界。

---

## 13. Design Compliance Checklist

后续每个重要架构方案完成后，应使用本表检查。任一硬性问题回答“否”，方案都不能进入实现。

| ID | 检查问题 | 性质 |
|---|---|---|
| C-01 | Agent 是否通过 LLM 参与任务决策？ | 必须 |
| C-02 | Agent 是否具备本地文件读取能力？ | 必须 |
| C-03 | Agent 是否具备本地文件写入或修改能力？ | 必须 |
| C-04 | Agent 是否具备本地命令执行能力？ | 必须 |
| C-05 | Agent 是否能根据执行结果继续推进任务，而非只生成一次文本？ | 必须 |
| C-06 | Conversation History 是否由自己的 Runtime 管理？ | 必须 |
| C-07 | Context 的选择、保留和裁剪是否由自己的 Runtime 管理？ | 必须 |
| C-08 | Tool 的定义是否由自己的项目掌握？ | 必须 |
| C-09 | Tool 是否由自己的程序在本地发起并控制执行？ | 必须 |
| C-10 | 模型响应是否由自己的 Runtime 转换为内部语义并推进状态？ | 必须 |
| C-11 | Agent Loop 是否由自己的代码实现？ | 必须 |
| C-12 | Termination 是否由自己的 Runtime 决定并强制执行？ | 必须 |
| C-13 | Error Handling 和恢复决策是否由自己的 Runtime 掌握？ | 必须 |
| C-14 | 是否完全避免使用 Agent Framework / Agent SDK？ | 必须 |
| C-15 | 是否避免在现成 Agent 产品上封装界面或把任务转交给它？ | 必须 |
| C-16 | 是否避免使用服务端托管代码执行或文件工具？ | 必须 |
| C-17 | 每个第三方依赖是否只承担允许委托的底层工程能力？ | 必须 |
| C-18 | 模型 SDK 和 Tool Calling 是否未接管 Agent Lifecycle？ | 必须 |
| C-19 | Runtime Secret 是否只从环境变量或未入库配置加载？ | 必须 |
| C-20 | Runtime Secret 是否不会进入源码、仓库、日志、模型上下文或 Tool 输出？ | 必须 |
| C-21 | 是否能够解释所有核心模块、执行流程和重要 trade-off？ | 必须 |
| C-22 | 新增复杂度是否有明确收益，并保持可测试与可解释？ | 设计原则 |
| C-23 | 对题目未明确的问题，是否标记为设计决策而非外部要求？ | 设计原则 |

---

## 14. Task 1 Completion Statement

本文确定了后续设计不可违反的最低能力、Runtime Ownership、允许依赖、禁止委托、本地执行、Secret 和可解释性边界，同时将未被题目规定的架构问题分配给对应 canonical owner 文档。

任务 1 不选择具体架构，也不定义 Tool、Context、Shell、Workspace 或 CLI 的实现方案。后续所有设计产物都必须能够通过第 13 节的 Compliance Checklist。
