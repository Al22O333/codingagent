# Agent Runtime Model

## 1. Purpose

本文定义 v1 Coding Agent 的运行时生命周期，包括：

* Session 与 Agent Run 的关系
* Agent Loop 的执行模型
* Runtime 状态
* Model Turn 与 Tool Call 的处理方式
* 用户中途交互
* Run 预算与防死循环机制
* Error / Recovery Model
* Retry Policy
* Termination 与最终状态

本文不定义：

* Tool 的具体 schema 与组件接口：由 `05-component-and-protocol-contracts.md`、`06-toolset-and-file-editing.md` 负责
* Context 的裁剪、压缩和长期保留策略：由 `07-context-and-prompt-policy.md` 负责
* 完成结果是否经过充分验证：由 `08-verification-testing-and-demo.md` 负责
* 具体 timeout、budget 默认值及 CLI 展示：由 `09-cli-observability-and-configuration.md` 负责

---

## 2. Runtime Principles

v1 Runtime 遵循以下原则：

1. Agent 使用 **iterative tool-calling loop**，而不是强制的 Plan → Execute 工作流。
2. 模型根据最新 observation 动态决定下一步动作。
3. Runtime 负责控制生命周期、协议、安全、预算和错误传播，不负责替模型完成 Coding Task 的语义推理。
4. 一个 Agent Run 可以包含多轮 Model Turn 和 Tool Call，但始终受到确定性的运行预算约束。
5. Tool execution 的失败通常是 Agent 的 observation，而不是 Runtime fatal failure。
6. Session 提供连续对话体验，Agent Run 提供一次任务执行的生命周期隔离。
7. Runtime 使用确定性的硬预算保证 Run 最终停止，不依赖复杂的“语义进度判断”。
8. 无法安全、可靠或合理继续时，Agent 应停止并如实说明，而不是无限尝试或伪造成功。

---

## 3. Core Runtime Objects

### 3.1 Session

一个 Session 对应同一 workspace 中的一条连续交互线。它可以只存在于当前进程，也可以由 09 的显式 CLI 入口保存和恢复 terminal-safe continuity。

Session 至少包含：

```text
Session
├── session_id
├── workspace binding
├── shell/backend environment information
├── conversation continuity
├── retained session context
└── Agent Runs
```

一个 Session：

* 只绑定一个 workspace root
* 可以依次执行多个 Agent Run
* 默认仍只存在于当前进程
* 只有用户显式请求 persistence 时，才保存 07 定义的 bounded completed-run continuity
* resume 后创建全新的 Runtime execution state，不恢复任何 active Run 或 pending execution

Session 本身不是一次具体任务的执行状态。

---

### 3.2 Agent Run

Agent Run 是：

> Agent 为完成一个当前用户任务而执行的一次自主循环。

例如：

```text
User:
Fix the authentication bug.

          ↓

       Run #1
```

Run #1 完成后：

```text
User:
Now add tests for it.

          ↓

       Run #2
```

每个 Run 都拥有独立的：

* lifecycle state
* current task
* task scope updates
* explicit task constraints
* model turn counter
* tool call attempt counter
* run start time
* `wait_reason`
* `pending_user_request`
* `ModelProtocolError` counter
* temporary failure/repetition tracking
* completion self-audit eligibility / active flags
* pending Candidate Final [when applicable]
* termination reason

新 Run 不继承上一 Run 的执行计数、pending action 或 transient error state。

---

### 3.3 Conversation Context、Runtime State 与 Workspace State

v1 明确区分三类状态：

| State | 含义 | Source of Truth |
|---|---|---|
| **Conversation Context** | 当前可供构建模型输入的信息，例如任务、保留对话和 Tool observation | Runtime / Context Policy |
| **Runtime State** | lifecycle、budget、counter、pending request、scope update 和 termination 等结构化执行状态 | Agent Runtime |
| **Workspace State** | 当前真实文件、目录、项目及其外部可见状态 | Local filesystem / local environment |

Workspace State 是项目事实来源。Conversation Context 中的文件内容可能被截断、摘要、淘汰或已经过时；当它与当前 filesystem 状态冲突时，以实际 Workspace State 为准。

Runtime State 不复制整个 Workspace，也不保存全量文件镜像。它只保存驱动当前 Agent Run 和进程内 Session continuity 所需的结构化状态。

本项目中的 Memory 仅指当前 Agent Run、进程内 Session continuity，或显式保存的 terminal-safe completed-run continuity；它不包含 Runtime snapshot、Vector Database、Embedding Memory 或长期用户记忆。

Conversation Context 的具体保留、裁剪、摘要和 stale-context 处理由 07 定义。

---

## 4. Session Continuity and Run Isolation

### 4.1 Shared Across Runs

Session 可以为新的 Run 提供必要的连续性，例如：

* 之前用户讨论过什么
* 上一个任务最终做了什么
* 用户对项目提出过哪些仍然有效的要求
* workspace 当前绑定信息

因此新的 Run 可以理解：

```text
“刚才那个函数再优化一下。”
```

而不要求用户重新描述全部背景。

---

### 4.2 Not Automatically Shared Across Runs

新的 Run 不自动继承上一 Run 的完整 Tool History。

例如 Run #1：

```text
read auth.py
read user.py
edit auth.py
pytest → failed
edit auth.py
pytest → passed
final
```

Run #2 不应机械重新携带：

* 所有历史 Tool Call
* 所有旧 Shell stdout / stderr
* 第一次已经失效的测试错误
* 已经被修改前的旧文件全文
* 所有临时 observation

原因是这些信息：

* 会持续增加 Context
* 可能已经过时
* 可能与新任务无关
* 可能让模型把旧文件状态误认为当前状态

具体跨 Run 保留哪些信息、如何压缩和淘汰，由 `07-context-and-prompt-policy.md` 决定。

Runtime 只规定：

> **Session provides continuity; Agent Run provides execution isolation.**

---

### 4.3 Terminal-Safe Cross-Process Resume

Persistent Session resume 只恢复 conversational continuity，不恢复 execution state。一个可恢复 checkpoint 只由最近 bounded 数量的 `COMPLETED` Run 的 initial task 与真实 Final 组成，并绑定 stable session ID、schema version 与 canonical workspace identity。

新进程 resume 时必须：

```text
validate checkpoint + workspace identity
→ create a fresh AgentRuntime / Session execution state
→ restore bounded historical task/final continuity
→ re-read current project instructions and workspace truth
→ start a wholly new Agent Run from the new user task
```

不得持久化或恢复：pending ToolCall / ToolResult、PendingAction、permission、clarification、active process、candidate Final、completion-audit state、protocol correction、Runtime Secret、provider state、FAILED / CANCELLED Run 或任何 active budget/counter。Checkpoint write failure 不得损坏已有 checkpoint，也不得改变已经终止的 Run lifecycle；CLI 必须单独报告 persistence failure。

Historical continuity 始终可能 stale。它不能创建当前 Run hard constraints，不能证明当前 Workspace State，也不能阻止模型按需重新读取文件和运行验证。

Session listing 与 deletion 是 composition-root management operations，不是 Agent Run。它们不得构造 ModelClient、改变 Runtime lifecycle 或读取 continuity content；listing 只暴露当前 canonical workspace 下的 ID、更新时间与 retained completed-run count，deletion 只接受 exact canonical UUID 并再次验证 workspace binding。

---

## 5. Agent Execution Model

### 5.1 Iterative Tool-Calling Loop

v1 使用迭代式 Agent Loop：

```text
Current Task + Context
        ↓
     Model Call
        ↓
 ┌──────────────────────────────┐
 │                              │
Candidate Final             Tool Call(s)
 │                              │
 │                 Registry Lookup + Validation
 │                              │
 │                        ToolKind Dispatch
 │                      ┌───────┴────────┐
 │                INTERACTION          LOCAL
 │                      │                │
 │               User Interaction    Prepare Action
 │                      │                │
 │                      │          Constraints + Risk
 │                      │                │
 │                      └───────┬──── Execute
 │                              │
 │                         Tool Results
 │                              │
 └──────────────────────────────←
```

模型每次根据已经得到的 observation 决定下一步。

Runtime 不要求模型在执行前生成一个固定的完整计划。

模型可以自行进行规划或在必要时解释计划，但：

> **Planning is a model behavior, not a mandatory Runtime phase.**

---

### 5.2 Why No Mandatory Plan Phase

Coding Task 的后续动作往往依赖运行过程中得到的新信息：

```text
search
  ↓
发现真正相关文件
  ↓
read
  ↓
发现问题来源不同于预期
  ↓
edit
  ↓
test
  ↓
发现新的 failure
  ↓
继续修复
```

强制固定 Plan → Execute 会使 Runtime 需要不断维护、修改和同步计划状态，而不会明显提升 v1 的任务完成能力。

因此 v1 不定义：

```text
PLANNING
EXECUTING_PLAN
REPLANNING
```

等强制生命周期状态。

---

## 6. Model Turn and Tool Batch

### 6.1 Model Turn

一次 Model Turn 指 Runtime 向模型发送当前可见 Context，并取得一次 assistant response。

一个 response 可以包含：

* zero Tool Calls
* one Tool Call
* multiple Tool Calls

一次 response 无论包含多少 Tool Calls，都只消耗：

```text
1 model turn
```

每个进入 Runtime 处理 pipeline 的 Tool Call 则分别计入 Tool Call Attempt Budget。

---

### 6.2 Multiple Tool Calls

v1 支持一个 Model Turn 返回多个 Tool Calls。

例如：

```text
read_file("src/auth.py")
read_file("src/user.py")
read_file("tests/test_auth.py")
```

可以在一次模型决策后共同执行，而不要求每读取一个文件都重新调用一次模型。

这可以减少：

* 无意义的 model round trip
* latency
* token duplication
* 重复 Context processing

---

### 6.3 Dependency Principle

模型应只批量提出不依赖前序 observation 才能确定参数的动作。

适合批量：

```text
read A
read B
read C
```

不适合批量：

```text
search
→ 根据搜索结果决定 read 哪个文件
```

或者：

```text
edit
→ 根据 edit 结果决定下一步动作
```

Runtime 不尝试证明 Tool Calls 是否语义独立。

该原则主要由 Prompt Policy 引导。

---

### 6.4 Sequential Execution

v1 对同一 Model Turn 中的多个 Tool Calls：

> **按照模型返回顺序串行执行。**

即：

```text
Call #1
 ↓
Call #2
 ↓
Call #3
```

v1 不要求 Tool 并行执行。

原因包括避免：

* concurrent file edit conflict
* shell / edit race
* permission synchronization complexity
* output ordering ambiguity
* cancellation complexity
* workspace shared-state race

未来版本可以对明确 read-only、无共享副作用的 Tool 增加并行执行能力，但不属于 v1。

---

### 6.5 Per-Call Validation and Policy Checks

同一 batch 中每个 Tool Call 首先按统一顺序进入：

```text
ToolRegistry lookup
→ Schema / Protocol Validation
→ ToolKind dispatch
```

之后严格分支：

```text
INTERACTION
→ Runtime-mediated User Interaction

LOCAL
→ prepare local action
→ PreparedToolCall
→ Explicit Task Constraints
→ Risk Permission
→ Execution
```

例如：

```text
#1 read_file("src/a.py")   → ALLOW
#2 git push                → CONFIRM
#3 read_file("../secret")  → DENY
```

不能因为多个 call 属于同一个 response 就共享权限判断。

Structured clarification interaction 同样必须独立完成 Registry lookup 和 Schema / Protocol Validation 并消耗 Tool Call Attempt；validation 成功后它立即按 `ToolKind.INTERACTION` 进入 §9.3 的用户交互分支，不产生 PreparedToolCall，也不进入 Local Tool 的 Explicit Task Constraint / Risk Permission / Execution pipeline。

---

## 7. Tool Batch Failure Semantics

### 7.1 Recoverable Sequential Fail-stop

如果一个合法 Tool Call 执行失败，例如：

```text
#1 read_file("a.py")        → success
#2 read_file("missing.py")  → FILE_NOT_FOUND
#3 read_file("c.py")        → not executed / batch aborted
```

Recoverable Tool Operation Failure 或 Unsuccessful Command Outcome 默认不会终止整个 Agent Run，但会终止当前 batch。

Runtime 不依赖模型保证后续 calls 与失败动作语义独立。遇到以下任一 recoverable observation 或已成功完成的用户交互时，Runtime 停止处理剩余 batch：

* Tool Validation Failure
* Explicit Task Constraint Rejection
* Risk Permission `DENY`
* User Rejected Confirmation
* Tool Operation Failure
* Unsuccessful Command Outcome
* 当前 action 已尝试执行的 successful Permission Confirmation interaction
* 已获得回答的 successful Clarification interaction

Runtime 保留已经完成的 ToolResults；剩余尚未进入处理 pipeline 的 applicable normalized calls 由 05 定义为 `NOT_EXECUTED`，且不消耗 Tool Call Attempt。Runtime 记录完整 observation / ToolResultMessage，再由新的 Model Turn 根据最新 observation 重新决策。

---

### 7.2 User Interaction During Batch

如果某个 Tool Call 需要 `CONFIRM`：

```text
RUNNING
   ↓
WAITING_FOR_USER
```

Runtime 暂停当前执行流。

获得用户决定后：

* approved → 执行当前 action，结束当前 batch，进入新的 Model Turn
* rejected → 产生明确 rejection result，结束当前 batch，进入新的 Model Turn

无论批准还是拒绝，Runtime 都不继续执行旧 response 中尚未处理的 calls，因为用户交互和当前 action 已经产生了新的 observation。

Structured clarification interaction 遵循同一 batch boundary：Runtime 等待用户回答，将回答记录为该 interaction call 的 result / observation，并终止当前 batch；剩余 calls 不执行，由新的 Model Turn 基于该回答重新决策。

---

### 7.3 Fatal Interruption

以下 terminal interruption 立即停止当前处理：

* user cancellation
* Runtime fatal error
* unrecoverable protocol/runtime invariant violation
* hard Run budget reached
* provider failure after bounded Transport Retry
* unrecoverable `ModelProtocolError` after corrective limit exhaustion

Runtime 根据原因执行必要 cleanup，并进入：

```text
user cancellation → CANCELLED
other terminal interruption → FAILED
```

Terminal interruption 不产生下一 Model Turn。

---

## 8. Runtime Lifecycle States

v1 只定义对整个 Agent Run 生命周期有意义的顶层状态。

```text
RUNNING
   ↕
WAITING_FOR_USER

RUNNING
   ↓
COMPLETED
FAILED
CANCELLED
```

不将 Model Call、Tool Execution、Retry 等全部升级为独立顶层状态。

---

### 8.1 RUNNING

表示 Runtime 正在自主推进当前 Agent Run。

RUNNING 内部可以存在 phase，例如：

* preparing model context
* waiting for model response
* validating tool calls
* executing tool
* processing tool result
* performing provider retry

这些属于内部执行 phase，不是独立生命周期状态。

---

### 8.2 WAITING_FOR_USER

Runtime 需要用户提供当前 Run 继续所需的输入。

v1 主要包括：

```text
PERMISSION_CONFIRMATION
CLARIFICATION
```

不为每一种等待原因创建单独顶层状态。

Runtime 应保存明确的：

```text
wait_reason
pending_user_request
```

`wait_reason` 区分 `PERMISSION_CONFIRMATION` 与 `CLARIFICATION`；`pending_user_request` 保存恢复当前 Run 所需的具体确认或澄清请求。

用户输入被记录为相应 result / observation 后，Runtime 清除 `wait_reason` 和 `pending_user_request`，再恢复 `RUNNING`。

---

### 8.3 COMPLETED

Agent 正常产生最终回复并结束自主循环。

`COMPLETED` 表示：

> Runtime 正常结束了该 Agent Run。

它不自动等价于：

> 任务已经经过充分验证并客观成功完成。

成功、部分完成、未验证、受阻等更细的任务结果语义由 `08-verification-testing-and-demo.md` 进一步定义。

---

### 8.4 FAILED

Run 因 Runtime 无法合理继续而非正常结束。

例如：

* hard budget reached
* repeated unrecoverable protocol failure
* provider failure after bounded retry
* invalid Runtime configuration discovered during run
* internal invariant violation
* `UserInteractionError` caused by a real terminal I/O infrastructure failure
* an explicit non-interactive Run reaches a required clarification or exact-action permission boundary

FAILED 应带明确的：

```text
termination_reason
```

Non-interactive input requirements use the distinct terminal reasons `CLARIFICATION_REQUIRED` and `PERMISSION_REQUIRED`; they are expected automation outcomes, not terminal I/O failures. Runtime must retain only bounded safe request facts for the terminal result, clear every pending action/request, and never execute or auto-approve the blocked action.

---

### 8.5 CANCELLED

用户主动中断当前 Agent Run，例如：

```text
Ctrl+C
EOF while waiting for user input
```

应进入：

```text
CANCELLED
```

而不是 `FAILED`。

Runtime 应停止生成新的 Agent action，并对当前受控执行进行 best-effort termination。

---

## 9. User Interaction During a Run

### 9.1 Permission Confirmation

例如：

```text
Agent wants to run:

pip install flask

Allow? [y/N]
```

Runtime：

```text
RUNNING
   ↓
WAITING_FOR_USER
   ↓
RUNNING
```

用户允许只授权：

> 当前展示的具体 action。

Runtime 将 05 定义的 immutable PendingAction 作为当前 permission request 的 pending state 保存。该授权只能触发一次 exact action execution attempt；Runtime 必须在以下任一时机清除 pending permission state：

* `APPROVE` 后 exact action 已进行一次执行尝试；
* `REJECT`；
* `CANCEL`；
* Run termination；
* fatal Runtime termination。

它不会：

* 扩展整个 Task Scope
* 授权其他 dependency installation
* 创建永久权限
* 创建 Session 级 blanket permission

已经清除的 PendingAction 不能作为 reusable permission token 再次执行。

---

### 9.2 Rejected Confirmation

用户拒绝一个 `CONFIRM` action 时，Run 不自动失败。

例如：

```text
Agent:
pip install dependency?

User:
No.
```

Runtime 将 rejection 反馈给模型：

```text
Action rejected by user.
```

Agent 可以：

* 选择其他实现方案
* 使用已有依赖
* 修改方案
* 请求必要澄清
* 最终说明任务无法继续

Agent 不得通过明显等价变形重复绕过用户刚刚的拒绝。

---

### 9.3 Clarification and Scope Update

Agent 可以在当前任务语义不足时请求澄清。

v1 通过 structured clarification interaction 发起该请求，其语义可表示为 `ask_user(question)`。这里的名称只说明 Runtime 行为，不在 04 固定 Tool schema、Tool Call ID、Tool Result 格式或注册方式。

例如：

```text
Agent:
Should I only report the problem, or fix it too?

User:
Fix it.
```

这不会创建新的 Run。

Runtime 行为为：

1. 模型通过 structured `ask_user` action 发起 clarification；该 response 因包含 interaction / Tool Call，不会被 §11 误判为 Final Response。
2. 该 call 正常计入一次 `tool_call_attempt`。
3. Runtime 设置 `wait_reason = CLARIFICATION`、保存 `pending_user_request`，并进入 `WAITING_FOR_USER`；active run duration 按现有规则暂停。
4. 用户回答作为该 interaction call 的 result / observation 返回模型，并按需要更新当前 Task State；随后清除 `wait_reason` 和 `pending_user_request`。
5. 当前 batch 在该用户交互处 fail-stop；旧 batch 中剩余 calls 不执行，Runtime 恢复 `RUNNING` 后进入新的 Model Turn。

如果模型需要用户回答后才能继续当前 Run，就必须使用该 structured interaction；普通无 Tool Call 的文本仍按 §11 的 Final Response 规则处理。

当前 Run：

```text
RUNNING
   ↓
WAITING_FOR_USER
   ↓
update current task intent
   ↓
RUNNING
```

---

### 9.4 Task State, Explicit Constraints, and Scope Updates

v1 不使用复杂的静态 capability list 描述用户任务，例如：

```text
allowed_files = [...]
allowed_operations = [...]
allowed_functions = [...]
```

因为自然语言 Coding Task 的实际修改范围通常无法在 Run 开始前准确列举。

Runtime 维护：

```text
initial_task
explicit_user_clarifications
explicit_scope_updates
explicit_task_constraints
wait_reason
pending_user_request
```

这些字段共同构成当前 Run 的 Task State。`initial_task`、用户明确澄清和明确 scope update 是模型判断 Task Intent 与 Semantic Relevance 的依据，但一般任务意图和细粒度相关性不属于 Runtime 可以确定性证明的安全属性。

v1 的 normalized Explicit Task Constraint 是一个封闭集合：

| Constraint | 用户表达示例 | Runtime-enforced 语义 |
|---|---|---|
| `FORBID_FILE_MUTATION` | “不要修改文件” | 阻止 Runtime 能直接识别的 write、edit、delete 类操作 |
| `FORBID_COMMAND_EXECUTION` | “不要运行命令” | 阻止 Shell / command execution capability |
| `WRITE_SCOPE` | “只修改 `tests/`” | 文件修改仅允许发生在一个或多个明确的 workspace-relative path roots 下 |

上述 constraint vocabulary、semantic meaning、Task State lifecycle 与 scope-update lifecycle 的 canonical owner 是 04。其他文档只引用该集合或定义消费它的组件 contract。

`WRITE_SCOPE` 的路径判断必须复用 03 的 canonical path resolution 与 semantic containment 规则，不能使用简单字符串前缀判断。

只有当用户明确表达的限制能够被 Runtime 可靠规范化为上述形式时，它才成为 hard Explicit Task Constraint。AgentRuntime-owned trusted Task State update path 使用一个 Runtime 内部的封闭 deterministic helper：

```text
normalize_explicit_constraint_update(user_input)
→ normalized update | no deterministic update
```

Trusted input 只来自：

* direct user input；
* 当前 Run 内 structured `ask_user` clarification 的用户回答。

该 helper 只识别 v1 明确支持、能够高置信确定的少量形式，例如“不要修改文件”“不要运行命令”“只修改 `tests/`”及其少量明确中英文等价形式。它不是通用 NLP parser、Intent Classifier、LLM-based classifier 或 policy DSL，也不需要成为独立 class / subsystem。

无法可靠规范化时，Runtime 返回 `no deterministic update`，保留原始语义作为 semantic guidance，或通过 `ask_user` 取得明确边界；不得假装 hard enforcement 已经建立。09 若提供 structured CLI surface，只能生成同一 normalized update，不改变 04 的 Task State owner。

Runtime 不得依据模型生成的文本或 ToolCall 新增、解除或修改 hard constraint。模型可以发起 clarification，但只有用户回答可以进入 trusted normalization path。

一般任务不建立通用 `READ_ONLY | MUTATING` 强制二分类。诸如 `review this code` 应默认只分析、`review and fix` 可以修改等行为，属于模型根据任务语义遵循的 policy，由 07 的 Prompt guidance 表达。若模型无法合理判断潜在动作是否扩大用户意图，应请求澄清。

用户在同一 Run 中的明确澄清可以更新 scope 和 explicit constraints，并在更新后继续当前 Run。只有用户明确输入能够扩大 Task Scope 或增加、缩小、解除 explicit constraints；模型输出不能直接修改 Task State。

一次 Risk Permission confirmation 只批准展示的具体动作，不扩大 scope，也不修改 explicit constraints。

Explicit Task Constraints 不构成 Shell sandbox：它们只阻止 Runtime 能从 Tool 或 action 表面直接识别的冲突；获准执行的程序仍可能具有隐藏 side effect，该限制由 03 作为 Accepted Risk 记录。

Constraint 的具体数据结构、Tool metadata 与 dispatch 接口由 05/06 定义；v1 不建立完整权限语言、复杂 policy DSL 或任意自然语言 constraint parser。

04 是 Task State、explicit constraints 与 scope-update lifecycle 的 canonical owner：它定义哪些用户输入能够更新当前任务、更新何时生效，以及 Run 结束后该状态如何隔离。07 只负责 Prompt 中的 Task Intent / Semantic Relevance guidance 及这些状态如何呈现给模型。

---

### 9.5 Agent Cannot Self-Expand Scope

Agent 不能因为发现额外问题自行扩大用户任务。

例如用户要求：

```text
review this function
```

Agent 发现另一个模块存在设计问题时，可以：

* 在 Final Response 中报告
* 请求用户扩展任务范围

但不应自动解释成：

```text
refactor the entire module
```

只有用户能够明确扩展当前 Task Scope 或更新 explicit task constraints。

---

## 10. Run Boundary

当前 Agent Run 在产生符合 §11 的 user-facing Final Response 后结束。Eligible Run 的首次合法无 Tool 文本只是 Candidate Final；它必须先完成一次 bounded completion self-audit，不能提前结束 Run。

之后用户继续输入时：

```text
Run #1 → COMPLETED

User follow-up
       ↓

Run #2
```

不重新激活已经结束的 Run。

如果用户输入只是当前 Run 尚未结束时 Runtime 主动请求的：

* confirmation
* clarification

则仍属于原 Run。

---

## 11. Final Response Semantics

一个 Model Response 只有同时满足：

```text
tool_calls.length == 0
AND text is not None
AND text.strip() != ""
```

时才形成一个 syntactically valid Candidate Final。Normalized Model Response 的具体 contract 由 05 定义；Candidate 是否能够立即成为 user-facing Final，由本节的 Runtime control semantics 决定。

### 11.1 Deterministic Eligible Run

Runtime 不通过自然语言判断 Run 是否“属于 mutating task”。当一个已知 Tool Call 进入 Runtime processing pipeline 并具有以下任一 capability 时：

```text
FILE_MUTATION
OR
COMMAND_EXECUTION
```

当前 Run 变为 completion-self-audit eligible。这里的进入 processing pipeline 与 Tool Call Attempt accounting 使用同一边界：Registry 已识别该 Tool，Runtime 已开始处理且该 call 消耗一次 attempt。无论后续结果是 success、operation / command failure、constraint / permission rejection 或用户拒绝，eligibility 都不撤销；batch fail-stop 后未进入 pipeline 的 remainder 不产生 eligibility。

`EXECUTE_COMMAND` 被故意纳入这一保守 deterministic rule，因为 Shell 既可能只读也可能产生副作用，而 v1 不建立不完整的任意 Shell mutation classifier。本文统一称此类 Run 为 `eligible Run` 或 `action-bearing Run`，不声称它必然发生过 mutation。

### 11.2 Bounded Completion Self-Audit

如果 Run 不 eligible，合法 Candidate 可以直接成为 user-facing Final 并进入 `COMPLETED`。

如果 Run eligible 且尚未开始 self-audit，首次合法 Candidate 必须：

```text
save as pending Candidate Final
→ record as a hidden AssistantMessage in current-Run Context
→ do not set agent_run.final_response
→ do not expose to the user
→ set completion_audit_active = true
→ issue the next bounded Model Turn
```

Self-audit 使用同一模型、同一 Run、同一 Context policy、同一 Tool set 与同一 safety / permission boundary。它不是 independent reviewer、第二 Agent 或新的 top-level lifecycle state。Pending Candidate 以其真实 assistant role 进入当前 Run 的 Model Context，使 Audit 能直接复核上一答案；它仍对用户隐藏且不得进入 completed-run continuity。Audit 根据原始任务、Candidate、当前 retained Tool history、workspace evidence 与 transient CompletionAuditInstruction 重新检查完成情况并生成新的响应。一旦进入 self-audit，后续零个或多个普通 Tool Turns 都属于该次 audit 的延续，直到模型产生下一个合法 Candidate；该 Candidate 才成为真正的 user-facing Final，并使 Run 进入 `COMPLETED`。

如果 concrete provider 要求在同一 Run 内回放 reasoning continuation，05 定义的 internal-only field 必须随对应 AssistantMessage 保留和回传。Audit 的新对话轮次由 07 定义的 request-local `RuntimeInstructionMessage` 触发；provider continuation 不成为 user-facing reasoning，不允许日志或 observer 暴露，也不得跨 Run 保留。

每个 Run 最多进入一次 self-audit。Self-audit 后的合法 Final 不递归触发第二次 audit。Run 完成、失败或取消时必须清理 pending Candidate 和 audit control flags。

建议的最小 Run-local control state：

```text
completion_audit_required: bool
completion_audit_active: bool
pending_final_candidate: str | None
```

等价的小型表示允许，但不得新增 `REVIEWING` / `VERIFIED` lifecycle state、独立 Review protocol 或 persistent audit state。Candidate 的隐藏 Context 表示与 transient guidance 由 07 定义，provider continuation contract 由 05 定义，verification 与 completion-claim 语义由 08 定义。

`text=None`、空字符串或仅包含 whitespace 且没有 Tool Calls 时，属于 response-level `ModelProtocolError`：

```text
zero Tool side effects
→ bounded corrective re-prompt
```

不得直接进入 `COMPLETED`。

如果 response 同时包含：

```text
text
+
Tool Call(s)
```

则该文本只属于当前 Tool Turn 的 assistant commentary。

例如：

```text
"I have fixed the problem."

+
run_tests(...)
```

不能让 Runtime 提前进入 `COMPLETED`。

必须先完成 Tool Call，获得 observation，并继续 Agent Loop。

---

### 11.3 Conservative Git Workspace Change Awareness

当 composition root 为 Runtime 提供 bound-workspace Git observer 时，每个 Run 在首个 Model request 前与 terminal cleanup 时各执行一次 bounded、read-only snapshot。该观察只允许直接调用 Git read commands；不执行 stash、reset、checkout、clean、add、commit 或任何 workspace mutation。

首版只在：

```text
canonical Git top-level == canonical bound workspace root
```

时形成 Git facts。non-Git workspace、workspace 只是更大 repository 的子目录、Git unavailable、timeout 或 malformed output 都 normal degrade，不阻止 Run，也不猜测 workspace 外状态。

terminal `WorkspaceChangeFacts` 最小包含：

```text
awareness_state
pre_existing_dirty_paths
known_agent_touched_paths
new_or_other_dirty_paths
attribution_uncertain
truncated
```

所有 path 使用 workspace-relative bounded representation；每组最多保留 200 个 path，单个 path 与总 observer event 继续 bounded。语义为：

* `pre_existing_dirty_paths` 来自 Run-start Git snapshot；
* `known_agent_touched_paths` 只记录成功 structured `FILE_MUTATION` execution 的 declared affected paths；
* `new_or_other_dirty_paths` 是 terminal dirty paths 中既不在 start snapshot、也不属于 known touched paths 的部分；
* `attribution_uncertain` 在任一 Shell execution attempt、failed mutation execution、snapshot unavailable / truncated、pre-existing 与 touched path overlap、或 unexplained terminal dirty path 存在时为 true。

这些是 conservative trust facts，不是内容级 provenance。尤其：

```text
known touched != exclusively authored by Agent
new/other != proven authored by user or Agent
clean terminal != no meaningful action occurred
```

Runtime 不解析 diff hunks来归因同一文件不同区域，不自动改写模型 Final，也不把 Git awareness变成 safety boundary。Snapshot / attribution failure不得改变 Tool permission、Run lifecycle 或 workspace内容；只影响 terminal facts和human observability。

为支持显式 user review，`AgentRun` 还可以保留 bounded `CommandExecutionEvidence`：只记录实际进入 Shell `execute` boundary 的 command、workspace-relative cwd、normalized Tool outcome、exit code与 error code；已进入 execution但被用户中断的 attempt记录为 `INTERRUPTED / USER_CANCELLATION`。Validation failure、Policy deny/reject或尚待 permission 的 action不构成 execution evidence。Command evidence必须在存入 Run前进行 Runtime Secret redaction，不包含 stdout/stderr，最多保留32项、单项 command/cwd最多500字符，并以 truncation fact表示超限。它不进入跨进程 Session continuity。

这些 facts 不推导 `verification_success`。Exit 0、test-like presentation label或多条 command evidence都不能让 Runtime声称 verification充分；08的 evidence/claim discipline仍由模型负责。

---

## 12. Run Budgets

v1 使用确定性的硬预算保证 Agent Run 不会无限运行。

至少包含：

```text
max_model_turns
max_tool_call_attempts
max_active_run_duration
```

---

### 12.1 Model Turn Budget

每取得一次新的 assistant response：

```text
model_turns += 1
```

即使该 response 包含多个 Tool Calls，也只增加一次 Model Turn。

Candidate Final 与其后的 self-audit response 分别是新的 assistant response，因此分别消耗一个 Model Turn。Audit 中不存在免费或无限额外 Model Turn。如果 eligible Run 已取得 Candidate，但剩余 budget 不足以发起必需的 self-audit，Runtime 不得静默接受未经 audit 的 Candidate，而应沿用 hard budget exhaustion 终止语义。具体默认 turn limit 仍由 09 owning；没有 implementation evidence 前不预先调整默认值。

---

### 12.2 Tool Call Attempt Budget

每个开始进入 Runtime 处理 pipeline 的 Tool Call attempt：

```text
tool_call_attempts += 1
```

计数发生在 validation 之前，因此以下情况均消耗一次 attempt：

* unknown tool 或 invalid arguments
* Explicit Task Constraint rejection
* Permission `DENY`
* 用户拒绝 `CONFIRM`
* successful execution
* Tool Operation Failure
* Unsuccessful Command Outcome

Batch fail-stop 后尚未进入处理 pipeline 的剩余 calls 不计 attempt。它们的未执行状态由 05 定义。

例如一次 Model Turn 返回：

```text
read A
read B
read C
```

则：

```text
model_turns += 1
tool_call_attempts += 3
```

---

### 12.3 Active Run Duration Budget

除单个 Shell command timeout 外，整个 Agent Run 还应存在 active duration 上限。

`max_active_run_duration` 只在 Run 处于 `RUNNING` 时累计；进入 `WAITING_FOR_USER` 后暂停，恢复 `RUNNING` 时继续累计。用户等待是否需要独立 `user_wait_timeout` 由 09 决定，v1 可以不设置默认等待超时。

两者解决不同问题：

```text
Shell timeout
→ 防止单个 command 无限运行

Active run duration
→ 防止 Agent 不断执行短动作但整体长期运行
```

---

### 12.4 Budget Exhaustion

达到任意 hard budget 后：

```text
Run → FAILED
termination_reason = LIMIT_REACHED
```

Runtime 应明确告诉用户：

* 哪一种预算耗尽
* Agent 已经完成到什么程度
* 当前任务并未因为模型声称而自动视为成功

具体默认预算值由 09 决定。

---

### 12.5 Token and Monetary Cost

Token usage 和 provider usage 可以被记录用于 observability。

v1 不要求：

* 精确美元 cost budget
* provider-independent token accounting
* reasoning-token 精确成本控制

因为不同 Provider 的 usage 和定价模型可能不同。

如 Provider 提供可靠 usage 信息，可以记录，但不把精确 monetary budget 作为 v1 必需的 termination mechanism。

---

## 13. Repetition and No-Progress Handling

### 13.1 Repeated Action Guard

Runtime 可以维护轻量的近期动作记录，例如：

```text
tool name
normalized arguments
result category / fingerprint
```

如果 Agent 连续重复：

```text
same tool
+
same arguments
+
equivalent failure
```

Runtime 可以向下一轮模型提供 warning，例如：

```text
RepeatedActionWarning:
The same action recently produced the same failure.
Avoid repeating it unless relevant state has changed.
```

---

### 13.2 Warning, Not Hard Failure

Repeated Action Guard 默认只用于帮助模型收敛。

它不会因为发现一次或少量重复就自动：

```text
Run → FAILED
```

原因是表面相同的 action 可能发生在不同 workspace state 下。

例如：

```text
edit code
pytest
edit code
pytest
```

两次 `pytest` 命令相同，但中间代码已经发生变化，因此可能完全合理。

---

### 13.3 No Semantic Progress Detector

v1 不实现复杂的：

* semantic progress scoring
* stagnation classifier
* LLM-as-judge progress detection
* workspace mutation score
* automatic task novelty scoring

Runtime 的真正终止保证来自：

> **deterministic hard budgets**

而不是试图推断模型是否“正在取得进展”。

---

## 14. Error Model

Runtime 将错误和失败大致分为四类。

```text
Result / Error / Failure
│
├── Action-level
│   ├── Tool Validation Error
│   ├── Tool Operation Failure
│   └── Unsuccessful Command Outcome
│
├── Policy-level
│   ├── Explicit Task Constraint Rejection
│   ├── Permission Denied
│   └── User Rejected Confirmation
│
├── Protocol-level
│   └── ModelProtocolError
│
└── Infrastructure / Runtime
    ├── transient provider failure
    └── fatal runtime failure
```

这些分类用于确定：

> 错误应反馈给 Agent 继续处理，还是直接结束 Run。

---

## 15. Tool Validation Error

Tool Call 可以被协议解析，但不满足 Tool 定义。

例如：

```text
unknown tool
missing argument
wrong argument type
invalid enum
malformed tool argument object
```

这种错误通常是 recoverable。

Runtime 不应直接结束 Run，而应构造清晰的 validation result 返回模型。

例如：

```text
error_type: TOOL_VALIDATION_ERROR
message: Unknown tool "read_files".
```

模型随后可以修正：

```text
read_file(...)
```

Tool schema、validation error 的具体结构由 05 定义。

---

## 16. Tool Operation Failure and Command Outcome

Tool Call 通过 validation 后，需要区分请求的本地操作是否成功完成，以及 Shell 已正常运行的命令是否返回不成功的项目结果。

### 16.1 Tool Operation Failure

**Tool Operation Failure** 表示 LOCAL Tool Call 已通过 validation，但请求的本地 operation 无法正常准备或完成。它可以发生在 local action preparation 或 execution 阶段，例如：

```text
FILE_NOT_FOUND
edit conflict
process could not start
executable not found
OS / IO failure
command timeout
internal Tool exception
```

Path resolver 成功产生 outside-workspace、Sensitive Path 或 Protected Path facts 不属于 Tool Operation Failure；这些是成功准备出的 policy facts，随后由 03 的 policy 产生 `ALLOW / CONFIRM / DENY`。

这些默认属于 recoverable Agent Observation，而不是 Runtime fatal failure。模型可以根据明确失败原因修正参数、选择其他动作或如实停止。

### 16.2 Unsuccessful Command Outcome

Shell Tool 已成功启动并观察到命令完成，但命令返回了不成功的项目结果，例如：

```text
exit code != 0
test failure
compiler error
lint errors
program returned failure
```

此时 Shell Tool 已成功启动并观察到命令结束。Runtime 应返回 `exit_code`、stdout、stderr 等 observation，而不把它误报为 Tool Operation Failure。

Tool Operation Failure 与 Unsuccessful Command Outcome 都默认属于：

> **Agent Observation**

而不是 Runtime fatal failure。

例如：

```text
pytest
↓
3 tests failed
↓
Tool Result
↓
Model
↓
edit
↓
pytest
```

Runtime 不因为测试失败、非零退出码或 recoverable Tool Operation Failure 就自动结束 Agent Run。两类结果的具体内部结构由 05 定义；它们与 Verification 结论的关系由 08 定义。

---

## 17. Policy-Level Rejection

包括：

* Explicit Task Constraints 不允许当前动作
* fixed `DENY` policy
* workspace boundary violation
* 用户拒绝 `CONFIRM`

这些结果应明确反馈给模型。

例如：

```text
Action rejected:
File Tool access outside workspace is prohibited.
```

Agent 可以调整方案。

但模型不得通过：

* 参数变形
* 同义命令
* 重新包装相同行为

绕过 Runtime 的固定安全规则或刚刚发生的用户拒绝。

---

## 18. ModelProtocolError

### 18.1 Response-Level ModelProtocolError

如果整个 Model Response 无法被 Runtime 可靠解释为合法响应，例如：

* response structure malformed
* Tool Call ID 缺失且无法可靠对应
* provider response违反必要协议约束
* Runtime 无法确定模型实际请求的动作

则：

> **Runtime 不执行该 response 中的任何 side effect。**

这是 v1 的重要协议安全原则：

```text
Unparseable / invalid response
        ↓
    zero side effects
```

---

### 18.2 Corrective Re-prompt

第一次 `ModelProtocolError` 不必直接让 Run 失败。

Runtime 可以向模型提供 corrective feedback：

```text
Your previous response was invalid.
Produce a valid response using the provided tool protocol.
```

然后重新调用模型。这是一个新的语义 Model Turn，而不是对同一网络请求的 transport retry：

产生 invalid Model Response 的原始模型调用已经取得 assistant response，因此已经按 §12.1 消耗一个 Model Turn；corrective re-prompt 后取得的新 response 再消耗一个 Model Turn。

* 新 response 计入 `model_turns`
* 消耗 Model Turn 和 active duration budget
* 当前 invalid response 使 `consecutive_protocol_errors += 1`
* invalid response 保持 zero side effects

如果 corrective re-prompt 的模型请求发生 transient infrastructure failure，可以在该逻辑请求内部使用受限的 Transport Retry。

---

### 18.3 ModelProtocolError Limit

Runtime 维护类似：

```text
consecutive_protocol_errors
```

连续 `ModelProtocolError` 超过一个较小上限后：

```text
Run → FAILED
termination_reason = PROTOCOL_FAILURE
```

具体次数由配置决定，不在 04 写死。

合法 response 出现后，连续错误计数重置。

---

## 19. Tool-Level vs Response-Level Invalidity

如果整个 response 可以解析，但其中个别 Tool Call 非法：

```text
#1 read_file("a.py")       valid
#2 unknown_tool(...)       invalid
#3 read_file("c.py")       valid
```

则按顺序处理到第一个非法 call：

```text
#1 → execute
#2 → Tool Validation Error
#3 → not executed / batch aborted
```

一个 call 的 validation failure 不终止 Agent Run，但会终止当前 batch。Runtime 把已完成结果、validation failure 和剩余未执行状态返回模型，由新的 Model Turn 重新决策。

但如果：

> 整个 assistant response 本身无法可靠解析

则该 response 中：

```text
zero Tool Calls are executed
```

---

## 20. Runtime Retry Policy

### 20.1 Automatic Retry Scope

Runtime 自动 Retry 只处理：

> **transient infrastructure failure**

例如：

* temporary model API timeout
* provider 429
* provider 5xx
* transient network failure

Retry 必须：

* 有次数上限
* 使用合理 backoff
* 可被用户取消

Transport Retry 重试的是同一个 logical model request。未取得 assistant response 的失败尝试不产生新的 Model Turn，但仍受 provider retry limit 和 active duration budget 约束。

---

### 20.2 No Automatic Semantic Retry

Runtime 不自动重复：

```text
pytest failed
compiler failed
file not found
permission denied
invalid shell command
edit conflict
```

这些结果应该反馈模型，由 Agent 自己决定下一步。

Runtime 不替模型执行：

```text
pytest
pytest
pytest
```

这种没有新决策的机械 Retry。

`ModelProtocolError` 的 corrective re-prompt 不属于 Transport Retry，也不属于对 Tool Operation Failure 或 Unsuccessful Command Outcome 的机械重试；它是带有协议反馈的新 Model Turn。

---

### 20.3 Retry Exhaustion

Transient provider error 在有限自动 retry 后仍无法恢复：

```text
Run → FAILED
```

并记录对应 termination reason。

具体 retry count 与 backoff 参数由 09 决定。

---

## 21. Termination Sources

Agent Run 可以因为以下来源结束。

### 21.1 Normal Final

模型返回符合 §11 的真正 user-facing Final：

```text
no Tool Calls
+
final textual response
```

对于 non-eligible Run，该响应可以是首次 Candidate；对于 eligible Run，它必须是 self-audit active 后的合法 Final。Runtime：

```text
→ COMPLETED
```

---

### 21.2 User Cancellation

用户：

```text
Ctrl+C
```

Runtime：

```text
→ CANCELLED
```

同时：

* 停止新的 Model Call
* 停止新的 Tool Call
* 对受控执行进行 best-effort termination

---

### 21.3 Hard Budget Exhaustion

任一 Run Budget 达到上限：

```text
→ FAILED
termination_reason = LIMIT_REACHED
```

---

### 21.4 ModelProtocolError Limit Exhaustion

连续模型协议错误超过限制：

```text
→ FAILED
termination_reason = PROTOCOL_FAILURE
```

---

### 21.5 Provider / Infrastructure Failure

自动 retry 后仍无法恢复：

```text
→ FAILED
termination_reason = PROVIDER_FAILURE
```

---

### 21.6 Fatal Runtime Failure

出现无法安全恢复的内部错误：

```text
→ FAILED
termination_reason = RUNTIME_FAILURE
```

---

## 22. Agent-Declared Inability to Continue

Agent 不需要为了正常结束而声称任务成功。

例如遇到：

* 必要依赖不存在且用户拒绝安装
* 缺少凭据
* 无法复现问题
* 缺失必要项目文件
* 当前权限不允许必要操作

模型可以给出诚实的 Candidate / Final Response：

```text
I could not complete X because Y.
To continue, Z is required.
```

如果 Run eligible，该 inability response 仍先经过一次 bounded self-audit；self-audit 后的诚实 Final 可以正常进入 `COMPLETED`，因为：

> Agent Loop 正常结束了。

任务完成程度、验证程度和 blocked/partial 等结果语义由 08 定义，而不是通过不断增加 Runtime 顶层状态解决。

---

## 23. High-Level Runtime Algorithm

概念性执行流程：

```text
create Agent Run
set state = RUNNING

while state == RUNNING:

    check hard budgets
        └─ exhausted
             → FAILED
             → no next Model Turn
             → terminate loop

    build current model context

    call model
        ├─ transient provider failure
        │      → bounded Transport Retry of same logical ModelRequest
        │      └─ exhausted
        │           → FAILED / PROVIDER_FAILURE
        │           → no next Model Turn
        │           → terminate loop
        │
        └─ FatalProviderError
               → FAILED / PROVIDER_FAILURE
               → no next Model Turn
               → terminate loop

    normalize and validate model response
        ├─ ModelProtocolError
        │      → zero side effects
        │      → increment protocol error counter
        │      → bounded corrective re-prompt as a new Model Turn
        │      └─ corrective limit exhausted
        │           → FAILED / PROTOCOL_FAILURE
        │           → no next Model Turn
        │           → terminate loop
        │
        └─ valid response

    if tool_calls.length == 0
       AND text is not None
       AND text.strip() != "":
        if completion_audit_active:
            record AssistantMessage(final text)
            set agent_run.final_response
            clear pending Candidate / audit control state
            emit user-facing final response
            → COMPLETED

        else if completion_audit_required:
            record hidden Candidate AssistantMessage
            save pending Candidate Final reference/state
            set completion_audit_active = true
            do not emit Candidate to user
            → next bounded Model Turn with CompletionAuditInstruction

        else:
            record AssistantMessage(final text)
            set agent_run.final_response
            emit user-facing final response
            → COMPLETED

    else if tool_calls.length == 0:
        → ModelProtocolError
        → zero Tool side effects
        → bounded corrective re-prompt

    else:
        for tool_call in model order:

            check tool-call-attempt budget
                └─ exhausted
                     → FAILED
                     → no next Model Turn
                     → terminate loop

            tool_call_attempts += 1

            ToolRegistry lookup
            if known Tool capability contains FILE_MUTATION or EXECUTE_COMMAND:
                completion_audit_required = true
            validate tool arguments
                └─ invalid
                     → validation result
                     → stop current batch

            dispatch by ToolKind

            if INTERACTION:
                set wait_reason = CLARIFICATION
                save pending_user_request
                → WAITING_FOR_USER
                ├─ cancellation
                │    → CANCELLED
                │    → no next Model Turn
                │    → terminate loop
                └─ ANSWERED
                     → AgentRuntime constructs ToolResult
                     → trusted Task State update if applicable
                     → clear wait_reason and pending_user_request
                     → stop current batch
                     → resume RUNNING and enter a new Model Turn

            if LOCAL:
                prepare local action
                ├─ expected preparation failure
                │    → OPERATION_FAILURE
                │    → stop current batch
                └─ PreparedToolCall
                     ↓
                  check explicit task constraints
                     └─ rejected
                          → policy result
                          → stop current batch
                     ↓
                  check risk permission
                     ├─ ALLOW
                     ├─ CONFIRM
                     │    → save immutable PendingAction as pending state
                     │    → WAITING_FOR_USER
                     │    ├─ cancelled
                     │    │    → clear pending permission state
                     │    │    → CANCELLED; no next Model Turn
                     │    │    → terminate loop
                     │    ├─ rejected
                     │    │    → clear pending permission state
                     │    │    → policy result; stop current batch
                     │    └─ approved
                     │         → authorize stored exact action once
                     │         → mark stop_after_current = true
                     └─ DENY
                          → policy result
                          → stop current batch
                     ↓
                  execute allowed exact prepared action once
                     ├─ success
                     ├─ recoverable Tool Operation Failure
                     │    → stop current batch
                     └─ Unsuccessful Command Outcome
                          → stop current batch

                  if execution followed APPROVE:
                      clear pending permission state after attempt

            record result

            if stop_after_current:
                → stop current batch

        if state remains RUNNING after a recoverable batch boundary:
            mark remaining unprocessed normalized calls as NOT_EXECUTED
            return collected results and unexecuted status to next Model Turn
```

任何阶段用户取消：

```text
→ CANCELLED
→ no next Model Turn
```

任何无法恢复的 Runtime failure：

```text
→ FAILED
→ no next Model Turn
```

---

## 24. Runtime Invariants

v1 应保持以下运行时不变量：

1. 一个 Session 可以包含多个 Agent Run，但一个 Run 只服务于一个当前用户任务及其澄清。
2. 新 Run 不继承上一 Run 的 transient execution state。
3. 新 Run 不自动继承完整 Tool History。
4. Runtime 不强制模型生成或维护显式 Plan。
5. 一个 Model Turn 可以返回多个 Tool Calls。
6. v1 的 Multi-Tool batch 按模型提供的顺序串行执行。
7. 每个 call 必须按 ToolRegistry lookup → validation → ToolKind dispatch 处理；validated LOCAL call 再进入 prepare → Explicit Task Constraint → Risk Permission → execution，structured clarification interaction 则立即进入用户交互分支。
8. Recoverable Tool Operation Failure 和 Unsuccessful Command Outcome 作为 observation 返回模型，不直接终止 Run，但会终止当前 batch。
9. 用户拒绝一个 `CONFIRM` action 不自动终止 Run，但会终止当前 batch。
10. 用户确认一个 action 不自动扩大 Task Scope，也不修改 explicit task constraints。
11. Agent 自身不能扩大用户 Task Scope。
12. Runtime 只强制执行用户明确给出且能够直接判定的 task constraints；一般 Task Intent 和细粒度 Semantic Relevance 是 model-dependent soft policy，不被声称为 deterministic security isolation。
13. Final Response 必须同时满足无 Tool Call 且具有非空白 user-facing text；存在 Tool Call 的 Model Response 不能同时作为最终完成响应。
14. 整个 Model Response 无法可靠解析时，该 response 不产生任何 Tool side effect。
15. `ModelProtocolError` 可以通过有限 corrective re-prompt 自修；每次取得 corrective response 都产生新的 Model Turn并消耗预算。
16. Automatic Transport Retry 只用于 transient infrastructure failure，不产生新的语义 Model Turn。
17. Tool Operation Failure 与 Unsuccessful Command Outcome 不由 Runtime 机械 retry。
18. Agent Run 必须受到 Model Turn、Tool Call Attempt 和 active duration hard budget 限制。
19. Lightweight repetition detection 可以提供 warning，但不是 v1 的主要终止保证。
20. Runtime 不尝试通过复杂语义算法判断 Agent 是否“取得足够进展”。
21. Ctrl+C 属于用户取消，最终状态为 `CANCELLED`。
22. Agent 可以在无法完成任务时如实产生 Final Response，而不是必须声称成功。
23. Conversation Context 与实际 Workspace State 冲突时，以 local filesystem / environment 为事实来源。
24. terminal-safe resume只允许显式保存 bounded completed-run continuity；Memory 不包含任意 Runtime snapshot、Vector Database、Embedding Memory 或长期用户记忆。
25. `WAITING_FOR_USER` 不计入 active run duration；独立的用户等待超时由 09 决定。
26. 每个进入处理 pipeline 的 Tool Call 都消耗一次 Tool Call Attempt；batch 终止后未处理的 calls 不计 attempt。
27. Batch 遇到 validation error、policy rejection、Tool Operation Failure、Unsuccessful Command Outcome 或用户交互后 fail-stop，由新的 Model Turn 重新决策。
28. Clarification 必须通过 structured interaction call 发起；它消耗一次 Tool Call Attempt，进入 `WAITING_FOR_USER(CLARIFICATION)`，并在用户回答成为 observation 后于同一 Agent Run 中开启新的 Model Turn。
29. Terminal interruption 进入 `CANCELLED` 或 `FAILED`，不得产生下一 Model Turn。
30. Expected local action preparation failure 使用 `OPERATION_FAILURE`，不新增独立 ToolOutcome。
31. 已知 `FILE_MUTATION` 或 `COMMAND_EXECUTION` Tool Call 进入 processing pipeline 后，当前 Run 必须保持 completion-self-audit eligible；未进入 pipeline 的 batch remainder 不产生 eligibility。
32. Eligible Run 的首次合法无 Tool文本只是 hidden Candidate Final；它以真实 AssistantMessage 进入 current-Run Context，但不得设置 `agent_run.final_response`、展示给用户、进入 completed-run continuity 或令 Run 进入 `COMPLETED`。
33. 每个 eligible Run 最多进入一次 bounded same-model completion self-audit；audit 中的 Tool Turns 继续使用原 Agent Loop、预算、约束、权限与 cancellation semantics。
34. Self-audit active 后的下一个合法无 Tool 文本可以成为真正 Final；completion self-audit happened 不等于 verification succeeded，`COMPLETED` 仍不等于 task success。
35. Candidate、internal provider reasoning continuation、audit flags 和 pending audit state 必须在 Run terminal cleanup 中清除，不得进入下一 Run 的 transient state。
36. Optional Git workspace awareness 只执行 bounded read-only snapshot；non-Git、root mismatch或observer failure不得阻止Run。
37. 只有成功 structured File Mutation 的affected paths属于known touched；Shell和失败mutation只增加attribution uncertainty，不产生虚假精确归因。
38. Runtime不得stash、reset、checkout、clean、commit用户workspace，也不得用change awareness改写模型Final。

---

## 25. Cross-Document Ownership

04 只拥有 Runtime lifecycle、state、budget、batch、retry 与 termination semantics，不维护其他 owner 文档中事项的实时“未决 / 已决”状态。以下条目说明职责归属；当前定义始终以对应 canonical owner 文档为准。

### `05-component-and-protocol-contracts.md`

* ModelResponse、ToolCall、ToolResult、ToolError 与 `ModelProtocolError` 的内部表示
* Tool Call ID 与 Tool Result correspondence contract
* Runtime、ModelClient、ToolRegistry、PolicyEngine 与 UserInteraction 的组件接口
* Multi-tool batch 的内部 dispatch contract
* Provider response 到 provider-neutral internal response 的转换边界
* Structured clarification interaction 的 schema、Call ID 与 result / observation 关联方式

### `06-toolset-and-file-editing.md`

* v1 具体 Tool Set
* 各具体 Tool 对 05 capability vocabulary 的 annotation
* Edit conflict 的具体检测与错误形式
* Shell Tool 的具体输入、输出结构
* Structured clarification interaction 的具体 Tool surface 与注册方式

### `07-context-and-prompt-policy.md`

* Session 中哪些信息跨 Run 保留
* 上一 Run 如何生成 retained summary/context
* Tool Result 进入 Context 的策略
* RepeatedActionWarning 如何提供给模型
* Task State、scope update 与 Semantic Relevance guidance 如何呈现给模型
* CompletionAuditInstruction 的 request-local presentation与 hidden Candidate 的 current-Run Context lifecycle
* Tool dependency / batching 行为的 Prompt 指导
* 历史 observation 的淘汰和 stale-context 管理

### `08-verification-testing-and-demo.md`

* `COMPLETED` 后任务结果如何分类
* successful / partial / blocked / unverified 等语义是否需要显式表示
* Completion self-audit guidance、verification selection 与 Final claim discipline
* 任务完成声明与 Verification Evidence 的关系

### `09-cli-observability-and-configuration.md`

* `max_model_turns`
* `max_tool_call_attempts`
* `max_active_run_duration`
* optional `user_wait_timeout`
* protocol retry count
* provider retry count / backoff
* User Confirmation 与 Clarification UI
* Runtime phase/event 的日志展示
* Run termination reason 的 CLI 展示
