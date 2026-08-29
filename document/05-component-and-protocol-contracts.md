# Component and Protocol Contracts

## 1. Purpose and Ownership

本文定义 v1 Coding Agent 的核心组件边界、依赖方向以及组件之间传递的内部协议。

本文负责回答：

* Agent Runtime 由哪些核心组件组成；
* 哪个组件拥有 Agent orchestration；
* Runtime 与 Model Provider 如何解耦；
* `ModelRequest / ModelResponse / ToolCall / ToolResult` 如何表示；
* Tool 如何声明、验证和执行；
* Explicit Task Constraint 与 Risk Permission 如何进入执行 pipeline；
* Permission Confirmation 与 Clarification 如何通过结构化协议完成；
* Context、Provider、Tool、Policy、UI 之间如何交互；
* expected domain outcome 与 Runtime exception 如何区分；
* 组件启动时必须满足哪些结构性 invariant。

本文不定义：

* File / Search / Edit / Shell 等具体 Tool schema 与详细行为：由 `06-toolset-and-file-editing.md` 负责；
* Prompt、Context 裁剪、摘要、跨 Run retention 与模型可见内容策略：由 `07-context-and-prompt-policy.md` 负责；
* Verification、测试矩阵、Fake component 的具体测试用例：由 `08-verification-testing-and-demo.md` 负责；
* 默认 Model、Provider、endpoint、budget、timeout、CLI UI 和日志配置：由 `09-cli-observability-and-configuration.md` 负责。

本文描述的是 v1 的 component contract，而不是通用 Agent Framework API。未来扩展不得以牺牲当前实现清晰度为代价。

---

## 2. Implementation Platform

### 2.1 Implementation Language

v1 使用：

> **Python 3.11+**

作为主要实现语言。

选择 Python 的主要原因包括：

* 原生适合 JSON、schema、filesystem、subprocess 与 CLI 编排；
* 主流模型 Provider SDK 与 OpenAI-compatible API 支持成熟；
* 可以使用类型标注、dataclass 与 Pydantic 保持内部协议清晰；
* 可以减少与 Agent 核心设计无关的底层基础设施代码；
* 有利于在有限实现周期内集中验证 Runtime、Tool、Policy 和 Context 逻辑。

Implementation Language 的选择应在 `10-architecture-decisions.md` 中记录对应 ADR。

---

### 2.2 Synchronous Orchestration

v1 Runtime 使用同步控制流。

概念上：

```text
Model Call
    ↓
Process Response
    ↓
Tool Call
    ↓
Tool Result
    ↓
Next Model Turn
```

v1 不要求：

* concurrent Agent Runs；
* parallel Tool execution；
* background asynchronous task graph；
* asynchronous Tool API；
* event-driven orchestration framework。

这与 04 已确定的以下设计一致：

* 一个 Session 同时只执行一个 active Agent Run；
* Multi-Tool batch 顺序执行；
* Tool execution 为同步受控操作；
* v1 不支持 unmanaged background workflow。

Model API 和 Shell command 可以包含阻塞等待，但必须受独立 timeout 和 Run budget 控制。

---

### 2.3 Allowed Engineering Libraries

v1 可以使用普通工程库处理非 Agent-orchestration 逻辑，例如：

* Provider 官方或兼容 SDK；
* Pydantic；
* 标准库 filesystem / subprocess / logging；
* 普通 CLI、testing、serialization 工具。

这些库不得拥有：

* Agent Loop；
* Tool selection；
* Tool dispatch；
* Permission decision；
* Context policy；
* Runtime lifecycle；
* Agent retry / recovery；
* termination decision。

Agent orchestration 必须由本项目 Runtime 自己实现。

---

## 3. Architectural Principles

### 3.1 AgentRuntime Is the Sole Orchestrator

`AgentRuntime` 是唯一拥有 Agent control flow 的组件。

它负责：

* Agent Run lifecycle；
* Model Turn；
* Tool Call Attempt；
* Multi-Tool batch 顺序；
* batch fail-stop；
* Explicit Task Constraint 检查调用；
* Risk Permission 检查调用；
* `WAITING_FOR_USER` 状态转换；
* Permission Confirmation；
* Clarification；
* Transport Retry decision；
* Corrective Re-prompt；
* Run budget；
* termination。

其他组件可以返回：

* facts；
* structured results；
* normalized errors；
* decisions；

但不得自行发起 Agent lifecycle transition。

核心原则：

> **Lower-level components report facts and outcomes; AgentRuntime owns control decisions.**

---

### 3.2 Provider-Neutral Internal Protocol

Runtime 不依赖任何特定 Provider 的 wire format。

例如 Runtime 不应直接处理：

```text
OpenAI assistant.tool_calls
Anthropic tool_use
provider-specific tool_result blocks
```

Runtime 只处理本项目内部的：

```text
ModelRequest
InternalMessage
ModelResponse
ToolCall
ToolResult
```

具体 `ModelClient` implementation 负责 Provider wire format 与内部协议之间的转换。

---

### 3.3 Expected Failures Use Structured Outcomes

Agent 运行中可以合理预期的失败应尽量使用结构化 result，而不是 Python exception 控制正常流程。

例如：

```text
Tool Validation Error
Explicit Task Constraint Rejection
Permission Rejection
Tool Operation Failure
Unsuccessful Command Outcome
User Rejected Confirmation
Batch NOT_EXECUTED
```

均属于 structured outcome。

Python exception 主要用于：

* Provider infrastructure failure；
* Model protocol normalization failure；
* unexpected component bug；
* Runtime invariant violation；
* unexpected User I/O failure。

---

### 3.4 Immutable Protocol Objects

以下组件间 value object 应尽量保持 immutable：

```text
ToolSpec
ToolCall
ToolResult
ToolError
ModelResponse
Policy Result
PreparedToolCall
PendingAction
InternalMessage
```

Python 实现可优先使用：

```python
@dataclass(frozen=True)
```

Mutable lifecycle state 则主要存在于：

```text
Session
AgentRun
Context storage
```

中。

---

## 4. Core Components

v1 的核心逻辑组件为：

```text
CLI / Composition Root
        │
        ▼
   AgentRuntime
   ├── ContextManager
   ├── SessionStore (only when explicit persistence is requested)
   ├── ModelClient
   ├── ToolRegistry
   ├── PolicyEngine
   └── UserInteraction

ModelClient Protocol
    │
    ▼
OpenAICompatibleModelClient
    │
    ▼
Vendor SDK / API

ToolRegistry
    │
    ▼
Tool Implementations
    ├── filesystem helpers
    └── subprocess helpers
```

有概念名字不表示必须创建独立 class hierarchy。v1 的预期实现形态是：AgentRuntime 为真实 orchestrator；ModelClient 为 Protocol；OpenAICompatibleModelClient 为 concrete implementation；ContextManager、PolicyEngine 与 UserInteraction 为小型 service / port；ToolRegistry 为 thin mapping wrapper；PreparedToolCall 与 PendingAction 为 frozen dataclass；Shell risk classifier、workspace resolver 和 constraint normalizer 分别只是其 owner 内部的 helper / shared module。

---

### 4.1 AgentRuntime

`AgentRuntime` 承载 04 定义的 Agent Runtime Model。

它组合其他组件，但不重复实现它们的内部职责。

Runtime 应主要包含：

```text
control flow
state transition
budget accounting
result assembly
error propagation
```

而不应包含大量：

```text
provider serialization
filesystem implementation
shell parser
tool argument schema
context compression
CLI rendering
```

逻辑。

---

### 4.2 ContextManager

`ContextManager` 管理 provider-neutral 的模型可见 Conversation Context。

它负责：

* 接收用户消息；
* 记录 AssistantMessage；
* 记录 ToolResultMessage；
* 构建当前 Model Turn 的 Internal Messages；
* 接入 07 定义的 Prompt / Context Policy。

它不负责：

* Tool execution；
* Runtime lifecycle；
* Workspace state；
* Provider wire serialization；
* termination。

---

### 4.3 SessionStore

`SessionStore` 是 composition-root 使用的 Lean terminal-safe persistence boundary。它只序列化 07 定义的 immutable completed-run continuity records 与最小 session metadata；它不参与 Agent loop、Run lifecycle、Context eviction、Policy 或 Tool dispatch。

它必须提供 deterministic load/save/list/delete failure codes，验证 canonical session ID、schema version 和 workspace identity，并以 sibling temporary file + atomic replace 更新单个 session document。List 只返回 current-workspace metadata summaries，不返回 continuity；其他 workspace 的合法 documents 不可见，损坏或 symbolic-link entries 只形成 anonymous skipped count。Delete 必须在 unlink 前完成 exact ID、regular-file 与 workspace 验证，且只删除单个 checkpoint。

List/delete 是 model-free composition-root operations，不要求 provider model、base URL 或 API key，也不得构造 `ModelClient`。它们不得解释对话语义、扫描 Workspace、序列化 ContextManager 内部状态，或持有 mutable `AgentRun`。

`ContextManager` 只暴露 bounded completed-run continuity 的 typed import/export；只有在没有 active Run 时才允许 restore。`AgentRuntime` resume 后仍拥有一个全新的 `Session` 和全新的 Runs。

---

### 4.4 ModelClient Protocol

`ModelClient` 是 `AgentRuntime` 面向模型调用的稳定 Protocol / interface。

概念接口：

```python
complete(request: ModelRequest) -> ModelResponse
```

v1 使用 non-streaming complete-response contract。`AgentRuntime` 每个 Model Turn 只消费一个完整、已经规范化的 `ModelResponse`，不处理 token streaming、partial assistant block、partial ToolCall accumulation 或 streamed Tool execution。该选择用于简化 response-level validation、保持 invalid response zero-side-effect，并降低 ToolCall correspondence、cancellation 与 retry 的复杂度。

Provider SDK 是否具备 streaming capability 不影响该 contract；v1 Runtime 不启用它。

它不拥有 Agent Loop。

`AgentRuntime` 不直接依赖具体 Vendor SDK。

---

### 4.5 OpenAICompatibleModelClient

v1 的具体实现 `OpenAICompatibleModelClient` implements `ModelClient` Protocol，并在内部负责：

```text
Internal ModelRequest
        ↓
Provider Request
        ↓
Provider SDK / API
        ↓
Provider Response
        ↓
Internal ModelResponse
```

它同时负责 Provider ToolCall identity mapping、response-level `ModelProtocolError` detection 和 Provider exception normalization。

它不得负责：

* Agent retry policy；
* Tool execution；
* Context policy；
* Permission；
* Run termination。

Provider-specific adaptation 是 concrete ModelClient 的内部职责，不构成独立顶层 Runtime component，也不建立 Adapter framework。未来如需不同 wire protocol，由另一个 concrete ModelClient implementation 实现同一 Protocol。

---

### 4.6 ToolRegistry

`ToolRegistry` 负责：

* Tool registration；
* Tool name lookup；
* ToolSpec enumeration；
* registration-time invariant validation。

概念接口：

```text
register(tool)
get(name)
specs()
```

ToolRegistry 不负责：

* Tool argument validation；
* Permission；
* Tool execution orchestration；
* Runtime state；
* retry。

---

### 4.7 Tool

Tool 负责：

* 描述自身；
* 声明 input schema；
* validation；
* operation semantics；
* 对 Local Tool 执行具体操作。

Tool 不拥有 Agent lifecycle。

---

### 4.8 PolicyEngine

`PolicyEngine` 负责两个独立阶段：

```text
Explicit Task Constraint Check
Risk Permission Check
```

这两个阶段只处理已经完成 local action preparation 的 `PreparedToolCall`。InteractionTool 在 validation 后进入 Runtime-mediated interaction branch，不进入 PolicyEngine。

Explicit Task Constraint Check 至少消费：

```text
PreparedToolCall
ToolSpec / capabilities
current Run normalized Explicit Task Constraints 的 immutable snapshot
```

Risk Permission Check 至少消费：

```text
PreparedToolCall
03 policy 所需的 prepared workspace / path / shell facts
```

它只返回判断结果，不：

* 执行 action；
* 调用用户 UI；
* 修改 AgentRun；
* 修改 Task Scope；
* 调模型；
* 访问 mutable AgentRun、ModelResponse、Provider object、ContextManager 或 UserInteraction。

---

### 4.9 UserInteraction

`UserInteraction` 是 Runtime 与用户之间的 I/O port。

它负责：

* render confirmation；
* 获取批准 / 拒绝；
* render clarification；
* 获取用户回答。

它不：

* 保存 PendingAction；
* 执行 Tool；
* 修改 Runtime lifecycle；
* 判断风险。

An explicit non-interactive implementation of this port never reads stdin. Instead, `confirm()` or `ask()` raises a typed `InteractionRequiredError` carrying the immutable `ConfirmationRequest` or `ClarificationRequest`. AgentRuntime catches this expected boundary before the generic `UserInteractionError` path, projects bounded Secret-safe terminal facts, terminates the Run with `PERMISSION_REQUIRED` or `CLARIFICATION_REQUIRED`, and clears PendingAction / wait state without producing a ToolResult or another Model Turn.

---

## 5. Internal Model Protocol

### 5.1 ModelRequest

内部 `ModelRequest` 至少表达：

```python
ModelRequest(
    messages: list[InternalMessage],
    tools: list[ToolSpec],
)
```

以下内容属于 ModelClient / 09 configuration，而不是每一轮由 Runtime Context 重复提供：

```text
provider
model
base_url
credential
request timeout
provider-specific settings
```

---

### 5.2 Internal Messages

v1 使用类型化 provider-neutral message，而不是直接保存某个 Provider 的 `role/content` 字典。

至少包括：

```python
SystemMessage(text)

UserMessage(text)

ProjectInstructionMessage(text)

RuntimeInstructionMessage(text)

AssistantMessage(
    text,
    tool_calls,
    provider_reasoning_content,
)

ToolResultMessage(
    results,
)
```

一个 `AssistantMessage` 可以同时包含：

```text
text
+
Tool Call(s)
```

其中 text 在存在 Tool Call 时仅属于当前 action commentary，不是 Final Response。

`provider_reasoning_content` 是一个可选、internal-only 的 provider continuation field。它只用于满足 concrete ModelClient 在同一 Agent Run 内继续对话时的协议回放要求；它不是普通 assistant text，不得展示给用户、写入日志、进入 Tool argument / ToolResult，或进入 completed-run continuity。v1 只定义这一条窄字段，不建立通用 raw provider metadata 容器。

`ProjectInstructionMessage` 是 07 定义的 current-Run、request-local、untrusted root `AGENTS.md` guidance。它不代表用户输入，不进入 trusted task-constraint update path、Conversation History 或 completed-run continuity。OpenAI-compatible ModelClient 将它映射为带有明确 provenance / priority wrapper 的 wire `user` role；内部类型区分必须始终保留，不能把它重新构造成 `UserMessage`、`SystemMessage` 或 `RuntimeInstructionMessage`。

`RuntimeInstructionMessage` 是 Runtime 合成的 request-local control message，不代表用户输入，不写入 Conversation History，不进入 trusted task-constraint update path。OpenAI-compatible ModelClient 可将它映射为带有明确 `not user-authored` 标记的 wire `user` role，以便在 Assistant Candidate 后形成 provider 能可靠响应的新一轮；内部类型区分必须始终保留，不能把它重新构造成 `UserMessage`。

---

### 5.3 ModelResponse

规范化后的模型响应至少包含：

```python
ModelResponse(
    text: str | None,
    tool_calls: list[ToolCall],
    usage: ModelUsage | None,
    provider_reasoning_content: str | None,
)
```

语义：

```text
tool_calls == []
AND text is not None
AND text.strip() is not empty
→ candidate Final Response

tool_calls != []
→ Tool Turn
```

`tool_calls == []` 但 `text` 为 `None`、空字符串或仅包含空白时，不得直接令 Run 进入 `COMPLETED`；它产生 response-level `ModelProtocolError`，并进入已有的 bounded corrective re-prompt。v1 不定义非文本 Final Response。

`ModelResponse` 只表示已经能够被 concrete ModelClient 可靠规范化的模型响应。

当 provider 返回同一 Run 后续请求必须原样回放的 reasoning continuation（例如 DeepSeek thinking-mode Tool workflow）时，concrete ModelClient 必须将其规范化到 `provider_reasoning_content`，Runtime 在记录对应 `AssistantMessage` 时保留该值，后续请求再由同一 concrete ModelClient 原样序列化。该字段是 opaque protocol continuation：Runtime、ContextManager 与 Prompt policy 不解释其内容，也不得把它当成用户可见推理或 semantic policy 输入。Context size accounting 必须计入其长度。

真正 Final 可以携带该字段以完成当前 response 的规范化，但 completed-run continuity 必须丢弃它；新 Run 不回放上一 Run 的 provider reasoning continuation。

整个响应无法可靠规范化时，不产生一个：

```text
ModelResponse(valid=False)
```

而应产生：

```text
ModelProtocolError
```

---

### 5.4 ToolCall

内部 Tool Call 至少包含：

```python
ToolCall(
    call_id: str,
    name: str,
    raw_arguments: object,
)
```

`raw_arguments` 明确表示：

> Provider 返回的参数尚未经过 Tool schema validation，不可信。

它可能是：

* JSON-like object；
* Provider 可恢复但 schema-invalid 的结构；
* 可明确归属到该 Tool Call 的 malformed argument representation。

Tool argument validation 由 Tool 层完成，而不是 concrete ModelClient。

---

### 5.5 Call Identity

每个 normalized Tool Call 必须拥有唯一 internal `call_id`。

若 Provider 原生提供可靠 call ID：

```text
provider call ID
→ internal call ID
```

若 Provider 缺少可直接使用的 ID，但 Tool Call correspondence 仍可可靠建立，则 concrete ModelClient 可以生成 internal ID。

Provider-specific correspondence state 只存在于 concrete ModelClient 内部，不进入 `AgentRuntime`。

同一个 ModelResponse 中出现不可消歧的 duplicate call identity，应产生 response-level `ModelProtocolError`。

---

### 5.6 ModelUsage

Usage 是 optional observability metadata。

概念上可以表示：

```python
ModelUsage(
    input_tokens: int | None,
    output_tokens: int | None,
    total_tokens: int | None,
)
```

Runtime 不依赖 provider-independent token accounting 保证终止。

---

## 6. Response-Level and Call-Level Validity

### 6.1 Response-Level ModelProtocolError

如果 Provider response 无法被可靠解释为明确的 assistant response，例如：

```text
response structure malformed
tool-call boundary ambiguous
call identity cannot be reconstructed
provider protocol invariant is violated
```

则 concrete ModelClient 产生：

```text
ModelProtocolError
```

AgentRuntime 按 04 处理：

```text
zero Tool side effects
bounded corrective re-prompt
```

该 invalid response 不记录为合法 AssistantMessage。

---

### 6.2 Call-Level Validation Failure

如果 Runtime 可以明确识别：

```text
call_id
tool name
raw arguments
```

但 Tool Call 本身非法，则属于 Tool Validation Error，而不是 ModelProtocolError。

例如：

```text
unknown tool
malformed argument encoding
missing field
wrong type
invalid enum
forbidden extra field
```

结果表示为：

```text
ToolResult(
    outcome = VALIDATION_ERROR
)
```

并触发当前 batch fail-stop。

---

### 6.3 Unknown Tool

Unknown Tool 属于 call-level validation failure。

例如：

```text
read_files(...)
```

而 Registry 中只有：

```text
read_file(...)
```

Runtime 应返回结构化 validation observation，而不是将整个 assistant response 判为 `ModelProtocolError`。

---

## 7. ToolResult Protocol

### 7.1 ToolResult

完整 Runtime-level ToolResult 概念结构：

```python
ToolResult(
    call_id: str,
    tool_name: str,
    outcome: ToolOutcome,
    content: object | None,
    error: ToolError | None,
)
```

---

### 7.2 ToolOutcome

v1 Runtime 只需要以下大类：

```text
SUCCESS
VALIDATION_ERROR
POLICY_REJECTED
OPERATION_FAILURE
UNSUCCESSFUL_COMMAND
NOT_EXECUTED
```

具体工具错误细节不继续扩展 ToolOutcome enum，而通过 `ToolError.code` 表示。

---

### 7.3 ToolError

概念结构：

```python
ToolError(
    code: str,
    message: str,
    details: object | None,
)
```

其中：

```text
outcome
```

供 Runtime 判断大类行为，

而：

```text
error.code
```

由具体 Tool / Policy 提供更细语义，例如：

```text
FILE_NOT_FOUND
EDIT_CONFLICT
COMMAND_TIMEOUT
WORKSPACE_BOUNDARY
USER_REJECTED_CONFIRMATION
```

---

### 7.4 Partial Result and Error

`content` 与 `error` 可以同时存在。

例如 Shell timeout：

```text
outcome = OPERATION_FAILURE

content:
    partial stdout
    partial stderr

error:
    COMMAND_TIMEOUT
```

Runtime 不要求：

> 一旦存在 error 就必须丢弃有效 partial output。

---

### 7.5 NOT_EXECUTED

Multi-Tool batch fail-stop 后尚未进入处理 pipeline 的 Tool Calls 使用：

```text
NOT_EXECUTED
```

例如：

```text
#1 SUCCESS
#2 OPERATION_FAILURE
#3 NOT_EXECUTED / BATCH_ABORTED
```

`NOT_EXECUTED` 主要用于：

* 完整 Tool Call / Tool Result correspondence；
* Provider conversation protocol compatibility；
* 明确告诉模型后续 action 并未发生。

这些 calls 不增加 `tool_call_attempts`。

---

### 7.6 Tool Result Correspondence Invariant

对于一个已经成功规范化的 AssistantMessage：

> **每个 ToolCall 在进入下一正常 Model Turn 前都必须具有对应 ToolResult。**

因 batch fail-stop 未被处理的 Tool Call 通过 `NOT_EXECUTED` 补全 correspondence。

ToolResult 顺序应与原 ToolCall 顺序一致，但逻辑关联以 `call_id` 为准。

---

### 7.7 ModelProtocolError Is Not ToolResult

Response-level ModelProtocolError 没有可靠 Tool Call 可以对应，因此不得伪装为：

```text
ToolResult(PROTOCOL_ERROR)
```

ModelProtocolError 属于 model/runtime protocol event。

---

## 8. Tool Contract

### 8.1 ToolSpec

每个 Tool 对 Runtime 暴露一个 `ToolSpec`：

```python
ToolSpec(
    name: str,
    description: str,
    input_schema: dict,
    kind: ToolKind,
    capabilities: frozenset[ToolCapability],
)
```

其中 Provider-visible 内容主要为：

```text
name
description
input_schema
```

`kind` 与 `capabilities` 属于 Runtime metadata，不要求直接发送给模型。

---

### 8.2 ToolKind

v1 使用：

```text
LOCAL
INTERACTION
```

两种 ToolKind。

`LOCAL` Tool 执行本地操作。

`INTERACTION` Tool 代表由 Runtime 介入的结构化用户交互，例如：

```text
ask_user
```

---

### 8.3 ToolCapability

v1 只定义满足当前 hard constraint 所需的最小 capability set：

```text
FILE_READ
FILE_MUTATION
COMMAND_EXECUTION
```

示例：

```text
read_file
→ FILE_READ

edit_file
→ FILE_MUTATION

shell
→ COMMAND_EXECUTION

ask_user
→ kind = INTERACTION
```

静态 action category 只通过 `ToolSpec.capabilities` 表达。`InteractionTool` 仅通过 `ToolKind.INTERACTION` 表达，不再重复声明 capability。v1 不建立大型 capability graph。

Git、dependency installation、network 等 Shell 风险由具体 command risk classification 负责，而不是继续扩张 ToolCapability。

---

### 8.4 Argument Validation

Tool 声明自己的 argument model，并负责：

```text
raw arguments
      ↓
Tool validation
      ↓
validated typed arguments
```

AgentRuntime 不根据 Tool name 手写大量参数 switch。

v1 推荐使用：

> **Pydantic 2**

定义 Tool argument models。

同一份 argument model用于：

* Python typed validation；
* Runtime schema validation；
* JSON Schema generation；
* Provider ToolSpec input schema。

这样避免分别维护：

```text
hand-written JSON schema
+
hand-written validation logic
```

造成 drift。

Pydantic 只提供 schema / validation，不承担 Agent orchestration。

---

### 8.5 affected_paths

支持文件修改范围约束的 File Tool 应能从 validated arguments 提供目标路径信息。

概念接口：

```python
affected_paths(validated_args) -> list[Path]
```

例如：

```text
edit_file("tests/a.py")
→ tests/a.py
```

该信息用于 Explicit Task Constraint enforcement。

Shell Tool 不声称能够通过该接口完整枚举内部 filesystem side effects。

---

### 8.6 LocalTool

Local Tool 才具有 operation execution 能力。

概念上：

```text
LocalTool
    validate(...)
    prepare(...) -> PreparedToolCall | ToolError for OPERATION_FAILURE
    execute(prepared_call)
```

Local Tool 不接收整个 AgentRuntime 或 mutable AgentRun State。

Expected local action preparation failure 不新增 ToolOutcome；preparation branch 返回具体 `ToolError`，Runtime 将其包装为现有 `OPERATION_FAILURE` observation。Unexpected preparation/helper exception 必须在 local Tool / action preparation isolation boundary 被捕获，并转换为 `OPERATION_FAILURE + INTERNAL_TOOL_ERROR`。具体 error codes 由 06 定义。

---

### 8.7 PreparedToolCall

`PreparedToolCall` 是只用于已完成 validation 的 `LOCAL` Tool Call 的轻量、provider-neutral immutable value object / frozen dataclass。它不是 subsystem、dispatcher、action framework 或 policy AST：

```python
PreparedToolCall(
    call_id,
    tool_identity,
    validated_arguments,
    operation_facts,
)
```

`tool_identity` 指向对应 Tool / ToolSpec identity。静态 read / mutation / command category 只来自 `ToolSpec.capabilities`，不在 `operation_facts` 中重复保存。`operation_facts` 不采用所有 Tool 共用的巨大固定字段集合，而只保存 Policy 与 exact execution 真正需要的 dynamic typed facts。

典型 File Tool facts 可以包括：

```text
canonical / resolved target paths
affected paths
workspace containment facts
Sensitive Path facts
Protected Path facts
```

典型 Shell facts 可以包括 validated command text 和 03 Shell risk policy implementation 所需的 surface command facts。

`PreparedToolCall` 不包含 Provider state、Runtime lifecycle、Session object、Context 或 retry state。

流程为：

```text
validated LOCAL ToolCall
→ prepare local action
→ PreparedToolCall
→ Explicit Task Constraint Check
→ Risk Permission Check
→ local execution
```

PolicyEngine 消费 `PreparedToolCall`，不得依赖 Tool name 对不同参数结构维护大型 switch。InteractionTool 不产生 `PreparedToolCall`。

---

### 8.8 InteractionTool

Interaction Tool 对模型而言表现为 Tool，但不直接执行本地操作。

例如：

```text
ask_user
```

Runtime 识别该 interaction action 后：

```text
WAITING_FOR_USER(CLARIFICATION)
```

并通过 UserInteraction 完成交互。

InteractionTool 在 ToolRegistry lookup 和 Tool validation 后按 `ToolKind` 分流，直接进入 Runtime-mediated User Interaction。它不经过面向本地动作的 Explicit Task Constraint / Risk Permission pipeline。

InteractionTool 不通过：

```text
execute() → input()
```

自行控制终端。

---

## 9. Tool Execution Results

### 9.1 ToolExecutionResult

Local Tool 不直接构造完整 Runtime `ToolResult`。

Local Tool 只返回 execution-level result，例如：

```python
ToolExecutionResult(
    outcome,
    content,
    error,
)
```

其合法 execution outcome 主要为：

```text
SUCCESS
OPERATION_FAILURE
UNSUCCESSFUL_COMMAND
```

Runtime 再结合：

```text
call_id
tool name
batch state
policy state
```

构造最终 ToolResult。

`ToolExecutionResult` 只属于 LocalTool execution boundary。InteractionTool 不调用 Local `execute()`，也不产生 `ToolExecutionResult`；UserInteraction 完成交互后，由 `AgentRuntime` 直接构造 Runtime-level `ToolResult`。例如：

```text
ask_user
→ WAITING_FOR_USER(CLARIFICATION)
→ user answer
→ ToolResult(outcome=SUCCESS, content={"answer": ...})
```

---

### 9.2 Operation Failure

Tool Operation Failure 表示：

> LOCAL Tool Call 已通过 validation，但请求的本地 operation 无法正常准备或完成。

它可以发生在 preparation 或 execution 阶段。Expected preparation failures 例如：

```text
filesystem metadata I/O failure
required local object disappeared
path resolution operation itself failed
preparation prerequisite became unavailable
```

Path resolver 成功得出 outside-workspace、Sensitive Path 或 Protected Path facts 不属于 `OPERATION_FAILURE`；这些是成功准备出的 policy facts，必须进入 PolicyEngine，由 03 的 policy 产生 `ALLOW / CONFIRM / DENY`。

例如：

```text
FILE_NOT_FOUND
EDIT_CONFLICT
PROCESS_START_FAILED
EXECUTABLE_NOT_FOUND
OS_IO_ERROR
COMMAND_TIMEOUT
INTERNAL_TOOL_ERROR
```

这些通常是 recoverable Agent Observation。

---

### 9.3 Unsuccessful Command Outcome

如果 Shell Tool 已正常启动并观察到 command 结束，但：

```text
exit code != 0
```

则属于：

```text
UNSUCCESSFUL_COMMAND
```

例如：

```text
pytest → failed tests
compiler → compile error
lint → lint failure
program → exit 1
```

Shell Tool execution mechanism 本身并未失败。

---

### 9.4 Expected Failure vs Exception

Expected operation failure 应返回 structured result。

Unexpected preparation helper 或 execution implementation bug 可以抛 exception，但必须在 local action preparation / Tool execution isolation boundary 被捕获并转换为：

```text
OPERATION_FAILURE
INTERNAL_TOOL_ERROR
```

同时详细 traceback 仅用于内部 observability，不默认暴露给模型。

---

## 10. Explicit Task Constraint Contract

### 10.1 Separate Constraint and Risk Phases

`LOCAL` Tool Call 完成 validation 与 local action preparation 后，Runtime 固定按顺序执行：

```text
Explicit Task Constraint Check
        ↓
Risk Permission Check
```

二者语义不同，不合并为一个模糊 policy judgment。

---

### 10.2 Constraint Decision

Explicit Task Constraint 只有：

```text
PASS
REJECT
```

不使用 `CONFIRM`。

原因是 Explicit Task Constraint 来自用户已经明确表达的限制。

违反当前约束时，应：

```text
REJECT
→ observation
→ model may request clarification
```

而不是通过 Risk Confirmation 绕过用户限制。

如果确实需要扩大 scope：

```text
ask_user
→ explicit user update
→ Runtime updates Task State
→ model proposes new action
```

---

### 10.3 Constraint Vocabulary Ownership

v1 normalized Explicit Task Constraint 的封闭 vocabulary、semantic meaning、Task State lifecycle、scope-update lifecycle 与 deterministic normalization contract 由 04 canonical owning；本文不重复定义该集合。

PolicyEngine 只消费 AgentRuntime 提供的 current normalized constraints immutable snapshot，并结合 `PreparedToolCall` 与 `ToolSpec.capabilities` 返回 `PASS / REJECT`。它不创建、更新、删除或重新解释 constraint vocabulary。

---

### 10.4 Trusted Constraint Update Path

只有 04 定义的 `AgentRuntime` owned trusted Task State update path 可以创建、更新或移除 normalized Explicit Task Constraints。本文只保持以下组件边界：

```text
用户直接输入中明确表达的限制
当前 Run 内 ask_user 返回的用户明确回答
```

概念流程以 04 为准：

```text
direct user input / clarification answer
        ↓
AgentRuntime trusted Task State update path
        ↓
closed deterministic normalization
├── normalized update → Runtime updates Task State
└── no deterministic update → semantic guidance / clarification
```

模型输出不能直接创建、解除或修改 hard Explicit Task Constraint；PolicyEngine 也不能修改 Task State。该 normalizer 只是 AgentRuntime internal helper，不构成通用 TaskConstraintParser、Intent Classifier、LLM-based hard constraint classifier 或 policy DSL。

---

### 10.5 Path-Scoped Constraint Enforcement

对于 04 定义的 path-scoped hard constraint，PolicyEngine 只对 Runtime 能通过明确 File Tool path semantics deterministic 识别的 mutation 实施检查。

Canonical path、semantic containment 与 workspace boundary 等 normative rules 的 canonical owner 是 03；共享 workspace path-resolution primitive / helper 的实现 owner 是 06。File Tools、local action preparation 与 Policy preparation 必须复用同一 primitive，PolicyEngine 不得另行实现第二套 path normalization、symlink containment 或 prefix checking。

该 constraint check 消费共享 primitive 产生的 canonical path / containment facts。

禁止仅使用：

```text
string prefix
```

判断路径是否位于允许范围。

Shell command 内部隐藏的文件写入不属于 04 所定义 path-scoped constraint 的 deterministic coverage，继续作为 03 Accepted Risk。

---

## 11. Risk Permission Contract

### 11.1 Permission Decision

Risk Permission 使用：

```text
ALLOW
CONFIRM
DENY
```

三种结果。

概念结构：

```python
PermissionCheckResult(
    decision,
    reason_code,
    message,
    risk_summary,
)
```

PolicyEngine只给出 decision 和结构化风险信息。

CLI如何展示由 09 决定。

---

### 11.2 Shell Risk Classification Contract

Shell operation 的执行语义属于 Shell Tool。Shell risk classification 的 normative policy 由 `03-safety-and-execution-boundaries.md` 定义；本文只定义组件与协议入口，不重复其风险矩阵。

输入为 `PreparedToolCall` 中已经 validation 的 command 与 typed Shell operation facts。`PolicyEngine` 根据这些 facts 返回 `ALLOW / CONFIRM / DENY`，不得使用 LLM 完成 hard risk decision。

具体 classification mechanism 由 `06-toolset-and-file-editing.md` 定义；默认 Shell backend、平台默认值与相关用户配置由 `09-cli-observability-and-configuration.md` 定义。

---

## 12. PendingAction and Confirmation

### 12.1 Immutable PendingAction

当 Risk Permission 返回：

```text
CONFIRM
```

AgentRuntime 必须保存不可变的 exact action snapshot。

概念结构：

```python
PendingAction(
    prepared_call: PreparedToolCall,
    permission_reason,
)
```

`PendingAction` 保留获批后执行 exact local action 所需的完整、不可变 `PreparedToolCall`。PendingAction 不由 UI 保存。

---

### 12.2 Exact Action Authorization

用户批准只授权当前展示的具体 PendingAction。

批准后 Runtime：

```text
execute stored exact action
```

不得：

```text
ask model to reconstruct action
allow changed arguments
convert approval into session-wide permission
```

---

### 12.3 Pending Permission Cleanup

PendingAction 由 AgentRuntime 保存，UserInteraction 不拥有它。Runtime 必须在以下任一时机清除 pending permission state：

```text
APPROVE → exact action 已进行一次 execution attempt
REJECT
CANCEL
Run termination
fatal Runtime termination
```

一次 approval 只是一项 exact action 的一次性授权；清除后的 PendingAction 不是 reusable permission token，不能转化为 Session permission 或 scope expansion。

---

### 12.4 Workspace Changes During Confirmation

用户批准代表：

> 允许 Runtime 尝试执行该 exact action。

它不代表执行前提永久成立。

如果等待期间 workspace 发生变化：

```text
edit conflict
path resolution changed
file disappeared
```

则 Tool 可以正常返回对应：

```text
OPERATION_FAILURE
```

---

### 12.5 Runtime Secret Is Not Confirmation Policy

Runtime自身的 Provider credential 不属于：

```text
CONFIRM → 用户同意后发给模型
```

的 Workspace Sensitive Data 逻辑。

Runtime Secret 必须按 01/03 的 invariant 过滤，不进入普通 Tool/Context permission flow。

---

## 13. User Interaction Contract

### 13.1 Confirmation

概念接口：

```python
confirm(request) -> ConfirmationResponse
```

Confirmation Request 至少包含：

```text
call_id
tool_name
action summary
reason code
risk summary
```

UserInteraction负责渲染，不重新判断风险。

---

### 13.2 Confirmation Result

结果至少区分：

```text
APPROVE
REJECT
CANCEL
```

语义：

```text
APPROVE
→ Runtime executes stored PendingAction

REJECT
→ POLICY_REJECTED
→ Run may continue

CANCEL
→ Run CANCELLED
```

---

### 13.3 Clarification

模型通过 structured interaction Tool：

```text
ask_user
```

发起澄清。

概念：

```text
ToolCall ask_user
→ ToolRegistry lookup
→ Tool validation
→ ToolKind = INTERACTION
→ WAITING_FOR_USER(CLARIFICATION)
→ UserInteraction.ask(...)
├── CANCELLED → Run CANCELLED; no next Model Turn
└── ANSWERED
       → AgentRuntime constructs ToolResult(SUCCESS, answer)
       → batch ends
       → next Model Turn
```

用户回答通过 ToolResult进入模型 observation，不额外重复写成普通 `UserMessage`。

Clarification completion status 只使用 `ANSWERED / CANCELLED`；不定义 `DECLINE`。用户选择“跳过”“不扩大范围”或其他否定性选项时，只要它是对问题的有效回答，就属于 `ANSWERED`，其具体内容进入 ToolResult。

---

### 13.4 Confirmation Is Not a Tool

Risk Permission `CONFIRM` 不是：

```text
confirm_tool
```

它是 Runtime 对另一个 Tool Call 的 permission control flow。

而：

```text
ask_user
```

是真正由模型主动产生的 structured Interaction Tool Call。

两者不得混淆。

---

### 13.5 Cancellation and I/O Failure

CLI EOF / Ctrl+C 应表示 user cancellation，而不是空字符串：

```text
Run → CANCELLED
no next Model Turn
```

真实 User I/O failure 则属于：

```text
UserInteractionError
```

不应伪装成：

```text
REJECT
```

`UserInteractionError` 进入 Runtime failure path，使当前 Run `FAILED`；它不产生新的 Model Turn。

`InteractionRequiredError` is not a channel failure. It is used only by explicit non-interactive composition and maps to the dedicated terminal reasons above. It never maps to APPROVE, REJECT, ANSWERED, or CANCELLED and therefore cannot silently choose on the user's behalf.

---

## 14. Context Contract

### 14.1 Record and Build

ContextManager 至少提供两类能力：

```text
record conversation event
build current model-visible messages
```

概念接口可以包括：

```text
record_user_message(...)
record_assistant_message(...)
record_tool_results(...)
build_messages(...)
```

具体存储结构归 07。

---

### 14.2 ContextBuildState

ContextManager 不直接获得整个 mutable AgentRun。

Runtime 应提供只读、最小必要的 context build state，例如：

```text
current task
explicit task constraints
relevant scope updates
runtime warning intended for model
```

避免 ContextManager逐渐依赖：

```text
provider config
tool executor
pending action
mutable lifecycle
```

---

### 14.3 AssistantMessage Before Tool Execution

合法 `ModelResponse` 包含 Tool Calls 时：

```text
normalize ModelResponse
        ↓
record AssistantMessage
        ↓
execute Tool Calls
        ↓
record ToolResultMessage
```

不能先生成 ToolResult 后才补 AssistantMessage。

这样能够保持：

```text
assistant tool call
↔
tool result
```

的 protocol correspondence。

---

### 14.4 Runtime-Mediated User Input

Permission Confirmation 的：

```text
y / n
```

不是普通 Conversation UserMessage。

模型真正需要看到的是：

```text
action executed
user rejected action
```

对应的 ToolResult / policy observation。

Clarification answer 同样通过：

```text
ask_user ToolResult
```

返回模型。

只有普通用户 task/follow-up 才成为 `UserMessage`。

---

### 14.5 Workspace Is Not Context State

ContextManager不得把保留的文件内容当成当前 Workspace Source of Truth。

它不：

```text
scan workspace and maintain authoritative mirror
```

Workspace变化必须通过真实 filesystem 和 Tool observation重新获得。

Context stale-data policy 由 07 定义。

---

## 15. ModelClient Implementations

### 15.1 Runtime-Facing Interface

AgentRuntime只依赖：

```text
ModelClient
```

而不是：

```text
OpenAI client
Kimi client
Anthropic SDK
```

---

### 15.2 Concrete ModelClient Responsibility

Concrete ModelClient implementation 负责：

```text
Internal Messages
ToolSpec
        ↓
Provider Wire Request
        ↓
Provider API
        ↓
Provider Response
        ↓
ModelResponse
```

它同时负责 Provider ToolSpec / InternalMessage serialization、ToolCall ID mapping、response normalization、`ModelProtocolError` detection 与 Provider SDK exception normalization。

---

### 15.3 Provider Error Taxonomy

v1 至少区分：

```text
TransientProviderError
FatalProviderError
ModelProtocolError
```

其中：

**TransientProviderError**

包括典型：

```text
temporary timeout
connection failure
429
5xx
temporary service unavailable
```

由 Runtime执行 bounded Transport Retry。

**FatalProviderError**

包括典型：

```text
invalid credential
unsupported model
invalid endpoint
non-retryable request rejection
provider authorization failure
```

一般不进行机械 retry。

**ModelProtocolError**

表示已取得模型 response，但其结构无法可靠规范化。

---

### 15.4 Runtime Owns Retry

Concrete ModelClient 和 SDK 不拥有 Agent retry policy。

Provider SDK 的自动 retry 在能够关闭时应关闭。

Transport Retry由 `AgentRuntime` 根据 04 的 policy决定。

若 Provider library 存在不可避免的隐式 retry，应被记录为 infrastructure limitation，不应再叠加无界 Runtime retry。

---

### 15.5 Transport Retry Uses the Same Logical Request

发生 transient provider failure 时：

```text
same logical ModelRequest
→ retry
```

Transport Retry 期间：

* 不重新构建 Context；
* 不新增 AssistantMessage；
* 不执行 Tool；
* 不新增语义 Model Turn。

未取得 assistant response 的 transport failure 不消耗 `max_model_turns`。只有取得一个新的 assistant response 后才产生并消耗 Model Turn。

---

### 15.6 Corrective Re-prompt Is Different

ModelProtocolError 后：

```text
zero side effects
→ protocol corrective observation
→ new ModelRequest
→ new Model Turn
```

无法规范化或未通过 response-level validation 的 assistant response 已经消耗一个 Model Turn；corrective re-prompt 所取得的新 assistant response 再消耗一个 Model Turn。Corrective Re-prompt 由 Runtime控制，不属于 Provider automatic retry。

---

### 15.7 Native Structured Tool Calling Only

v1 Provider backend 要求支持：

> structured/native Tool Calling 或 OpenAI-compatible equivalent。

v1 不支持：

```text
regex ReAct parser
JSON embedded in ordinary text
XML Tool tags
plain-text function protocol
```

这避免将模糊文本解析重新引入 Tool security boundary。

---

### 15.8 OpenAI-Compatible First Implementation

v1 首个实际 ModelClient implementation 是：

```text
OpenAICompatibleModelClient
```

通过：

```text
base_url
model
credential
```

适配兼容 Provider。

未来如果某 Provider 需要不同 wire protocol，由另一个 concrete ModelClient implementation 实现同一 Protocol；v1 不提前建设 Adapter framework，Runtime 不因此依赖 Provider wire format。

---

### 15.9 FakeModelClient

ModelClient contract 必须允许 deterministic fake implementation。

例如：

```text
FakeModelClient
→ predetermined ModelResponse sequence
```

以便未来验证：

* batch fail-stop；
* protocol recovery；
* clarification；
* permission flow；
* budget；
* termination。

具体测试用例由 08 定义。

---

## 16. End-to-End Component Protocol

### 16.1 Model Turn

```text
ContextManager.build_messages()
        +
ToolRegistry.specs()
        ↓
AgentRuntime builds ModelRequest
        ↓
ModelClient.complete()
        ↓
ModelResponse
```

---

### 16.2 Final Turn

只有满足以下全部条件时：

```text
tool_calls == []
AND text is not None
AND text.strip() is not empty
```

则：

```text
ModelResponse
→ AssistantMessage
→ record in ContextManager
→ emit final
→ COMPLETED
```

否则产生 response-level `ModelProtocolError`，进入 bounded corrective re-prompt，不得直接完成 Run。

---

### 16.3 Tool Turn

若：

```text
tool_calls != []
```

则：

```text
ModelResponse
→ AssistantMessage
→ record
→ process calls sequentially
```

每个 Tool Call：

```text
tool_call_attempts += 1

        ↓

ToolRegistry lookup

        ↓

Tool.validate(raw_arguments)

        ↓

ToolKind dispatch

├── INTERACTION
│      ↓
│   WAITING_FOR_USER
│      ↓
│   Runtime-mediated User Interaction
│      ↓
│   AgentRuntime constructs ToolResult
│      ↓
│   batch ends
│
└── LOCAL
       ↓
    prepare local action
       ├── expected preparation failure
       │      ↓
       │   OPERATION_FAILURE + ToolError.code
       │      ↓
       │   AgentRuntime constructs ToolResult
       │
       └── PreparedToolCall
              ↓
           Explicit Task Constraint Check
              ↓
           Risk Permission Check
              ↓
           LocalTool.execute(prepared_call)
              ↓
           ToolExecutionResult
              ↓
           AgentRuntime constructs ToolResult
```

`INTERACTION` branch 不被伪装为固定 `PASS / ALLOW`；它在 validation 后绕过只适用于 local action 的 constraint / risk pipeline，由 Runtime 按 interaction protocol 处理。

---

### 16.4 Batch Boundary and Terminal Outcomes

以下 recoverable observation 或 interaction 出现时停止当前 batch：

```text
Tool Validation Error
Explicit Task Constraint Rejection
Permission DENY
User Rejected Confirmation (REJECT path)
Permission Confirmation interaction (APPROVE path)
Clarification interaction (ANSWERED path)
Tool Operation Failure
Unsuccessful Command Outcome
```

已经成功产生的 results 保留。剩余未处理 calls 使用：

```text
NOT_EXECUTED / BATCH_ABORTED
```

Runtime 随后将完整的 ToolResultMessage 记录进 Context，并进入下一 Model Turn：

```text
ToolResultMessage
→ ContextManager
→ next Model Turn
```

以下 terminal outcome 同样终止当前 batch，但直接终止 Run，不再进入下一 Model Turn：

```text
Cancellation
Fatal Runtime Failure
Hard Budget Exhaustion
Unrecoverable Runtime Invariant Failure
FatalProviderError
Provider retry exhausted
ModelProtocolError corrective limit exhausted
UserInteractionError
```

如果终止前仍需要维护已规范化 Tool Call 的 correspondence，Runtime 应按既有 `NOT_EXECUTED / BATCH_ABORTED` 规则补全结果，再完成终止记录。

---

### 16.5 Permission Confirmation Flow

```text
ToolCall
   ↓
Validation
   ↓
Constraint PASS
   ↓
Risk = CONFIRM
   ↓
create immutable PendingAction
   ↓
WAITING_FOR_USER(PERMISSION_CONFIRMATION)
   ↓
UserInteraction.confirm()
   ├── CANCEL → clear pending state → Run CANCELLED; no next Model Turn
   ├── REJECT → clear pending state → POLICY_REJECTED
   └── APPROVE
          ↓
       execute exact PendingAction once
          ↓
       clear pending state after execution attempt
          ↓
       ToolResult
   ↓
batch ends
```

---

### 16.6 Clarification Flow

```text
ToolCall ask_user(...)
   ↓
validation
   ↓
ToolKind = INTERACTION
   ↓
WAITING_FOR_USER(CLARIFICATION)
   ↓
UserInteraction.ask()
   ├── CANCELLED → Run CANCELLED
   └── ANSWERED
          ↓
       AgentRuntime constructs ToolResult(SUCCESS, answer)
          ↓
       batch ends
          ↓
       next Model Turn
```

---

### 16.7 ModelProtocolError Flow

```text
Provider response
   ↓
cannot normalize
   ↓
ModelProtocolError
   ↓
invalid assistant response consumes one Model Turn
   ↓
zero side effects
   ↓
bounded corrective re-prompt
   ↓
new assistant response consumes another Model Turn
```

---

### 16.8 Transport Failure Flow

```text
ModelClient
   ↓
TransientProviderError
   ↓
AgentRuntime
   ↓
bounded Transport Retry
   ↓
same logical ModelRequest
   ├── assistant response obtained → consumes one Model Turn
   └── retry exhausted → FAILED / PROVIDER_FAILURE; no next Model Turn

FatalProviderError
   → FAILED / PROVIDER_FAILURE
   → no next Model Turn
```

---

## 17. Exception Boundaries

### 17.1 Structured Outcomes

以下不依赖 Python exception 表达正常控制流：

```text
VALIDATION_ERROR
POLICY_REJECTED
OPERATION_FAILURE
UNSUCCESSFUL_COMMAND
NOT_EXECUTED
user rejection
```

---

### 17.2 Exceptions

v1 可以使用 exception 表示：

```text
TransientProviderError
FatalProviderError
ModelProtocolError
UserInteractionError
RuntimeInvariantError
unexpected component implementation bug
```

---

### 17.3 Tool Isolation Boundary

Local Tool / preparation helper 的 unexpected implementation exception 应在 local action preparation / Tool execution isolation boundary 被 catch。

通常转换为：

```text
OPERATION_FAILURE
INTERNAL_TOOL_ERROR
```

这样单个 Tool implementation bug 不必直接导致整个 Python process 崩溃。

---

### 17.4 Runtime Failure Boundary

对于 ContextManager、PolicyEngine 或 Runtime 自身的 unexpected invariant corruption，不应一律伪装成 recoverable Tool failure。

无法安全继续时：

```text
Run → FAILED
termination_reason = RUNTIME_FAILURE
```

---

### 17.5 Top-Level Catch

Runtime 顶层可以捕获未预期异常用于：

```text
logging
cleanup
safe termination
```

但不得：

```text
except Exception:
    continue Agent Loop
```

未知 Runtime corruption 默认终止当前 Run。

---

## 18. Startup and Configuration Invariants

### 18.1 Startup Happens Before Agent Run

组件组装与配置验证发生在创建 Agent Run 之前：

```text
process startup
↓
load config
↓
validate configuration
↓
bind workspace
↓
construct components
↓
validate component invariants
↓
Session ready
↓
user task
↓
create Agent Run
```

因此明显启动配置错误不是：

```text
Run FAILED
```

而是 startup/configuration failure。

---

### 18.2 Workspace Invariants

启动时至少检查：

```text
workspace root exists
workspace root is directory
workspace root can be resolved/canonicalized
```

Workspace path 的 normative policy 由 03 定义；共享 path-resolution primitive / helper 的具体实现由 06 定义。

---

### 18.3 Tool Registry Invariants

注册完成后至少检查：

```text
Tool name non-empty
Tool name unique
ToolSpec can be generated
input schema valid
ToolKind valid
capabilities valid
```

Duplicate Tool name 属于 Runtime configuration error。

---

### 18.4 Model Configuration Invariants

启动时至少验证明显结构性问题：

```text
concrete ModelClient configured
model configured
base URL structurally valid when required
credential source available
request timeout valid
```

v1 不要求启动时主动调用 Provider 验证 credential。

---

### 18.5 Budget Configuration Invariants

至少要求：

```text
max_model_turns > 0
max_tool_call_attempts > 0
max_active_run_duration > 0
provider_retry_count >= 0
```

具体值归 09。

---

### 18.6 Constraint Enforcement Capability

如果某 File Mutation Tool 被 Runtime 用于执行：

```text
WRITE_SCOPE
```

hard constraint，则它必须能够提供足够的 affected-path information。

Runtime 不应声称对一个无法确定目标路径的 Tool 执行 deterministic path-scoped enforcement。

---

## 19. Composition Root

组件依赖只在 startup composition root 统一组装。

概念流程：

```text
CLI main
↓
load configuration
↓
bind workspace
↓
create OpenAICompatibleModelClient as ModelClient
↓
create ContextManager
↓
create Tools
↓
create ToolRegistry
↓
create PolicyEngine
↓
create UserInteraction
↓
create AgentRuntime
```

业务组件不得自行偷偷实例化其他顶层组件。

例如：

```text
Tool → creates ModelClient
PolicyEngine → creates UserInteraction
ContextManager → creates ToolRegistry
```

均不允许。

这种 construction discipline 同时提供自然的测试 seam。

---

## 20. Dependency Rules

v1 应保持单向 dependency：

```text
CLI
 │
 ▼
AgentRuntime
 ├────► ContextManager
 ├────► ModelClient
 ├────► ToolRegistry
 ├────► PolicyEngine
 └────► UserInteraction

ModelClient Protocol
 │
 ▼
OpenAICompatibleModelClient
 │
 ▼
Vendor SDK / API

ToolRegistry
 │
 ▼
Tools
 │
 ▼
Local OS primitives
```

应避免：

```text
Concrete ModelClient controls AgentRuntime lifecycle
Tool → AgentRuntime
ContextManager → ModelClient
PolicyEngine → UserInteraction
UserInteraction → Tool
```

Lower-level component 不反向拥有 orchestration。

---

## 21. Runtime and Protocol Invariants

v1 至少保持以下 Component / Protocol invariants：

1. `AgentRuntime` 是唯一 Agent lifecycle orchestrator。
2. Runtime 不依赖 Provider wire format。
3. Concrete ModelClient implementation 不拥有 Agent retry policy。
4. SDK automatic retry 在可配置时应关闭，由 Runtime控制 Transport Retry。
5. ContextManager 不以 retained context 代替 Workspace State。
6. ToolRegistry 只负责 registration / lookup / specs。
7. Tool 负责自己的 argument schema 与 validation。
8. Runtime 不根据 Tool name 内置大型 validation switch。
9. Tool Call argument 在 validation 前始终视为 untrusted。
10. Response-level `ModelProtocolError` 不产生 Tool side effect。
11. 可识别 Tool Call 的参数错误属于 call-level validation failure。
12. 每个 normalized Tool Call 在下一正常 Model Turn 前必须获得对应 ToolResult。
13. Batch-aborted calls 使用 `NOT_EXECUTED`，且不增加 Tool Call Attempt。
14. Local Tool 不直接生成 Runtime-level policy / batch outcome。
15. Explicit Task Constraint 使用 `PASS / REJECT`。
16. Risk Permission 使用 `ALLOW / CONFIRM / DENY`。
17. Permission approval 只执行 immutable PendingAction 对应的 exact action。
18. UserInteraction 不保存或执行 PendingAction。
19. `ask_user` 是 Interaction Tool；Permission Confirmation 不是 Tool。
20. Runtime只 hard-enforce能够 deterministic 表示的 Explicit Task Constraints。
21. Runtime不依赖 LLM 执行硬 safety policy。
22. Shell risk classification 遵循 03 的 normative policy，并由 PolicyEngine 从 `PreparedToolCall` 的 Shell operation facts 得出决定。
23. PolicyEngine 不使用 LLM 执行 hard risk decision；该 classification contract 不构成 OS sandbox。
24. 只有 validated `LOCAL` Tool Call 产生 `PreparedToolCall`；`INTERACTION` Tool Call 由 Runtime 直接进入用户交互分支。
25. ContextManager输出 provider-neutral Internal Messages。
26. Assistant Tool Call message 必须在对应 Tool Result 前进入 conversation state。
27. Expected Agent-domain failure优先表示为 structured result。
28. 未知 Runtime invariant failure不得被无限 recover。
29. Startup configuration failure 与 Agent Run `FAILED` 相互区分。
30. 组件通过显式 dependency injection / composition root 组装，而不是形成隐式循环依赖。
31. Explicit Task Constraint vocabulary 与 lifecycle 由 04 owning；只有 AgentRuntime owned 的 trusted Task State update path 可以创建、更新或移除 normalized constraints。
32. File Tool、local action preparation 与 Policy preparation 复用 06 定义的同一 workspace path-resolution primitive。
33. v1 ModelClient 使用 non-streaming complete-response contract，partial response 不触发 Tool execution。
34. Final Response 必须同时满足无 Tool Call 且具有非空白文本。
35. Recoverable batch boundary 进入下一 Model Turn；terminal outcome 直接终止 Run。
36. 已取得的 invalid assistant response 消耗一个 Model Turn，corrective response 另消耗一个；未取得 response 的 transport failure 不消耗 Model Turn。
37. ModelClient 是 Runtime-facing Protocol；Provider-specific adaptation 只存在于 concrete ModelClient implementation 内部，不构成独立顶层 component。
38. Expected local action preparation failure 使用现有 `OPERATION_FAILURE`；成功产生的 path classification facts 进入 PolicyEngine，不误报为 operation failure。
39. 静态 action category 只使用 `ToolSpec.capabilities`，PreparedToolCall 只保存动态 operation facts。
40. PendingAction 在一次 exact execution attempt、拒绝、取消或 Run termination 后清除；UserInteractionError 进入 terminal Runtime failure path。

---

## 22. Deferred Decisions

### `06-toolset-and-file-editing.md`

由 06 定义：

* v1 具体 Tool Set；
* Read / Search / Edit / Delete / Shell Tool schema；
* Pydantic argument model 的具体字段；
* File Tool result content；
* Tool-specific operation facts 与 local action preparation 的具体实现；
* Tool output bounding mechanism 与 result structure；
* 共享 workspace path-resolution primitive / helper；
* binary / large file policy；
* Edit conflict algorithm；
* Shell execution mechanism、subprocess backend abstraction、process handling 与 command invocation；
* Shell Tool-specific stdout / stderr 与 result structure；
* Tool-specific error codes；
* `ask_user` 的具体 Tool schema。

---

### `07-context-and-prompt-policy.md`

由 07 定义：

* System Prompt；
* Tool usage guidance；
* Semantic Relevance guidance；
* batching guidance；
* Prompt Injection policy；
* Context retention；
* ToolResult 的模型可见 projection；
* Session continuity；
* stale observation handling；
* summarization / truncation；
* protocol corrective feedback 如何进入下一 Model Turn。

---

### `08-verification-testing-and-demo.md`

由 08 定义：

* FakeModelClient 的具体测试场景；
* FakeUserInteraction；
* PolicyEngine unit tests；
* Concrete ModelClient contract tests；
* batch correspondence tests；
* startup invariant tests；
* v1 系统测试语言；
* requirement → implementation → evidence traceability。

---

### `09-cli-observability-and-configuration.md`

由 09 定义：

* 默认 Provider；
* 默认 Model；
* endpoint；
* credential loading；
* request timeout；
* provider retry count / backoff；
* Runtime budgets；
* 默认 Shell backend selection、平台默认值与相关用户配置；
* 默认 Tool output limits；
* User Confirmation UI；
* Clarification UI；
* CLI startup failure rendering；
* logging；
* usage display；
* raw provider debug metadata policy。

---

## 23. ADR References

以下决策应在 `10-architecture-decisions.md` 中记录对应 Lightweight ADR：

```text
Implementation Language: Python 3.11+

Synchronous Runtime Orchestration

AgentRuntime as Sole Orchestrator

Provider-Neutral Internal Model Protocol

Native Structured Tool Calling Only

LocalTool / InteractionTool Separation

Two-Stage Task Constraint and Risk Permission Policy

Runtime-Owned Transport Retry
```

ADR 只记录 Decision / Why / Main Alternative / Consequence / Owner doc，不复制本文的完整 contract。
