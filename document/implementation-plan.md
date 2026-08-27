# Coding Agent v1 Implementation Plan

## Purpose and Authority

本文是 Coding Agent v1 的开发执行路线和进度 canonical source，负责：

- 记录 v1 的具体实现顺序；
- 让开发过程按小步 vertical slice 推进；
- 防止一次生成大量代码或越过当前阶段实现后续能力；
- 为 Git commit history 提供自然、可解释的演进路径；
- 记录每一步的 Goal、Scope、Out of Scope、Acceptance 和 Suggested Commit；
- 明确当前应该实现哪一步。

本文不拥有架构决策。架构决策仍以 01–06 的 canonical owner 文档为准。若本文与 01–06 冲突：

> 01–06 优先，implementation plan 必须修改以符合 architecture。

本文不得重新定义 safety、Runtime、protocol 或 Tool contract。

## Progress Summary

~~~text
Current Step: Step 0
Completed: 0 / 24
In Progress: None
Next: Step 0 — Repository / Python Skeleton
~~~

更新任一 Step 状态时，必须同步更新本节。Step 0 至 Step 23 共 24 Steps。

## Development Principles

1. 一次只实现一个 Step。
2. 当前 Step 的测试与实现一起完成。
3. 当前 Step 验收通过后再进入下一 Step。
4. 不提前实现后续 `TODO` Step。
5. 不为未来需求提前增加 abstraction。
6. 优先 vertical slice，而不是先建立大量空框架。
7. 每个 Step 应尽量形成独立、可解释的 Git commit。
8. 如果实现过程中发现 architecture contract 无法实现，应先记录具体 contradiction，再修改 canonical owner 文档；不能静默绕过。
9. `TODO`、`IN PROGRESS`、`DONE` 是唯一进度状态。
10. Step 顺序可以因为真实 implementation blocker 做小范围调整，但必须记录原因；不能因为“顺手”大规模并行实现。

## Commit Discipline

- 默认一个 Step 对应一个主要 commit。
- 如果 Step 自然需要两个独立 commit，可以拆分。
- 不人为 squash 成一个大 commit。
- 不为了“commit 多”机械拆成一行一个 commit。
- commit 应代表可解释、可测试的增量。
- 每次 commit 前运行当前相关 tests。
- 不修改 Git 历史。
- 不提前 commit 未完成的大量后续代码。
- docs-only implementation plan 可以单独 commit。

当前文档建议 commit：

`docs: add incremental implementation plan`

## How to Execute a Step

以后给 Coding Agent / Codex 的实现指令遵循：

1. Read the current Step from `document/implementation-plan.md`.
2. Read only the architecture owner documents relevant to that Step.
3. Implement only the current Step.
4. Do not implement later `TODO` Steps.
5. Add or update tests for the current Step.
6. Run relevant tests.
7. Report:
   - changed files；
   - tests；
   - unresolved blockers。
8. Do not mark a Step `DONE` automatically unless explicitly instructed.
9. After human review and commit, update plan status separately.

实现代码的 Agent 不得擅自执行：

~~~text
TODO → DONE
~~~

进度状态由开发者确认后更新。

## Architecture Reopen Rule

01–06 当前视为已批准 architecture。实现过程中只有出现以下情况，才允许 reopen canonical owner 文档：

- concrete implementation contradiction；
- impossible contract；
- two owner documents truly conflict；
- safety invariant cannot be implemented as written。

以下理由不足以 reopen：

- “另一种设计更优雅”；
- “未来扩展更方便”；
- “可以顺便支持更多 provider”；
- “可以抽象成 framework”；
- “可能以后有用”。

发现真实 contradiction 时：

1. stop current Step；
2. record exact blocker；
3. identify canonical owner；
4. resolve design；
5. update owner document；
6. resume Step。

---

## Step 0 — Repository / Python Skeleton

**Status:** TODO

### Goal

建立最小可运行、可测试的 Python 3.11+ 项目骨架。

### Scope

- Python 3.11+；
- `src/` package structure；
- `tests/`；
- CLI entry point；
- `pyproject.toml`；
- `.gitignore`；
- minimal smoke test；
- CLI 暂时只需能启动并输出简单信息。

### Out of Scope

- ModelClient；
- AgentRuntime；
- Tool；
- Policy；
- Context；
- real LLM。

### Acceptance

- package 可以正常 import；
- CLI 可以启动；
- `pytest` 可以运行并通过至少一个 smoke test；
- 没有 Agent 业务逻辑。

### Suggested Commit

`chore: initialize Python project skeleton`

---

## Step 1 — Core Protocol Value Objects

**Status:** TODO

### Goal

实现 05 定义的 provider-neutral core protocol/value objects，不实现 orchestration。

### Scope

- `ToolCall`；
- `ToolResult`；
- `ToolError`；
- `ToolOutcome`；
- `ToolKind`；
- `ToolCapability`；
- `ToolSpec`；
- `ModelRequest`；
- `ModelResponse`；
- `ModelUsage`；
- InternalMessage types：
  - `SystemMessage`；
  - `UserMessage`；
  - `AssistantMessage`；
  - `ToolResultMessage`；
- 根据 05 当前正式定义实现，不使用旧 draft；
- 优先使用 enum、dataclass / frozen dataclass 和 small immutable value objects。

### Out of Scope

- Tool execution；
- Model SDK；
- Runtime loop；
- Context trimming；
- permission。

### Acceptance

- core objects 可构造；
- immutable contract 按 05 落实；
- enum 和 basic invariants 有 unit test。

### Suggested Commit

`feat: define core model and tool protocols`

---

## Step 2 — Tool Abstraction and Registry

**Status:** TODO

### Goal

实现 05 的 Tool abstraction 和最小 ToolRegistry。

### Scope

- Tool argument typed validation；
- Pydantic v2 schema generation；
- ToolSpec exposure；
- `ToolRegistry.register`；
- `ToolRegistry.get`；
- `ToolRegistry.specs`；
- unique name startup invariant；
- invalid ToolSpec / duplicate registration fail fast；
- test-only DummyTool。

### Out of Scope

- real File Tool；
- Shell；
- PolicyEngine；
- Runtime dispatch；
- plugin framework。

### Acceptance

- valid dummy Tool 可以注册和 lookup；
- model-visible JSON Schema 可由 typed model 生成；
- duplicate name 产生 startup failure；
- unknown Tool 可被 registry 层识别。

### Suggested Commit

`feat: add tool abstraction and registry`

---

## Step 3 — ModelClient Protocol and FakeModelClient

**Status:** TODO

### Goal

实现 Runtime-facing ModelClient seam，但暂时不接真实 LLM。

### Scope

- ModelClient Protocol / interface；
- `complete(ModelRequest) -> ModelResponse`；
- FakeModelClient；
- FakeModelClient 可以按预设 response sequence 返回结果；
- provider error test doubles as needed；
- 保持正确结构：

~~~text
AgentRuntime
→ ModelClient Protocol
→ concrete ModelClient
~~~

当前架构已经取消独立 ProviderAdapter。

### Out of Scope

- OpenAI-compatible SDK；
- network request；
- real provider config；
- retry orchestration。

### Acceptance

- FakeModelClient 可以 deterministic 返回 Final response；
- FakeModelClient 可以 deterministic 返回 ToolCall response；
- Runtime tests 后续无需真实网络。

### Suggested Commit

`feat: add model client interface and fake client`

---

## Step 4 — Minimal ContextManager

**Status:** TODO

### Goal

实现最小 provider-neutral conversation context storage/building。

### Scope

- record UserMessage；
- record AssistantMessage；
- record ToolResultMessage；
- build current InternalMessage sequence；
- 保证 Assistant ToolCall message 位于对应 ToolResult 前；
- 保持实现极小。

### Out of Scope

- summarization；
- token counting policy；
- trimming；
- stale-context strategy；
- retained session summary；
- 07 的完整 Context policy。

### Acceptance

- message order deterministic；
- AssistantMessage → ToolResultMessage correspondence 顺序正确；
- basic unit tests 通过。

### Suggested Commit

`feat: add minimal conversation context manager`

---

## Step 5 — Minimal AgentRuntime: Model to Final

**Status:** TODO

### Goal

第一次实现最小 AgentRuntime loop，但只支持：

~~~text
User
→ Model
→ Final
~~~

### Scope

- Session / AgentRun 最小 Runtime State；
- `RUNNING`；
- `COMPLETED`；
- `FAILED`；
- `CANCELLED`；
- model turn counter；
- Runtime 调用 ContextManager；
- Runtime 调用 ModelClient；
- no-tool + non-empty text → `COMPLETED`；
- empty / whitespace no-tool response → `ModelProtocolError` path 的基础处理。

### Out of Scope

- Tool execution；
- Tool batch；
- Policy；
- Shell；
- real provider；
- complete retry framework。

### Acceptance

- FakeModelClient 返回合法 Final → `COMPLETED`；
- empty Final 不会 `COMPLETED`；
- Runtime 是 sole orchestrator。

### Suggested Commit

`feat: implement minimal agent runtime loop`

---

## Step 6 — Shared Workspace Resolver

**Status:** TODO

### Goal

实现 03 / 06 定义的 shared workspace path-resolution primitive。

### Scope

- workspace root binding；
- canonical workspace root；
- `EXISTING` path resolution；
- `NEW` path resolution；
- canonical / semantic containment facts；
- `ResolvedPath`；
- `is_within_workspace`；
- `workspace_relative_path: str | None`；
- sensitive / protected classification 基础；
- inside / outside policy fact distinction。

### Out of Scope

- `read_file`；
- PolicyEngine decision；
- Shell；
- File mutation。

### Acceptance

至少测试：

- relative inside path；
- absolute inside path；
- `..`；
- absolute outside；
- symlink / equivalent supported platform case；
- new target under existing parent；
- outside workspace 可成功形成 policy fact，而不是误报 `OPERATION_FAILURE`。

### Suggested Commit

`feat: add workspace path resolution`

---

## Step 7 — First Real Tool: read_file

**Status:** TODO

### Goal

实现第一个真实 LOCAL Tool。

### Scope

- `read_file`；
- UTF-8 text model；
- binary heuristic；
- existing path resolution；
- bounded line-range paging；
- `start_line` / `end_line`；
- `total_lines`；
- `truncated`；
- `next_start_line`；
- model-facing line number representation；
- structured Tool operation errors。

### Out of Scope

- search；
- edit；
- shell；
- full Risk Permission system。

### Acceptance

- normal text read；
- bounded large-file read；
- continuation；
- missing file；
- binary file；
- UTF-8 decode failure；
- outside-workspace 不被当作 `OPERATION_FAILURE`。

### Suggested Commit

`feat: add bounded read_file tool`

---

## Step 8 — Runtime Tool Dispatch Vertical Slice

**Status:** TODO

### Goal

第一次跑通完整 Tool Loop：

~~~text
Fake Model
→ read_file ToolCall
→ Runtime
→ ToolResult
→ Fake Model
→ Final
~~~

### Scope

- ToolRegistry lookup；
- argument validation；
- ToolCall attempt accounting；
- LOCAL Tool dispatch basic path；
- Tool execution；
- Runtime creates ToolResult；
- `call_id` correspondence；
- AssistantMessage before ToolResultMessage；
- next Model Turn；
- 暂时只有 `read_file` 也可以。

### Out of Scope

- complex Policy；
- multi-tool fail-stop；
- shell；
- edit；
- confirmation。

### Acceptance

至少一个 integration test 完整证明：

~~~text
User
→ FakeModel
→ read_file
→ ToolResult
→ FakeModel
→ Final
→ COMPLETED
~~~

### Suggested Commit

`feat: execute tool calls through runtime loop`

---

## Step 9 — Sequential Batch Semantics

**Status:** TODO

### Goal

实现 04 的 multi-tool sequential batch + fail-stop semantics。

### Scope

- multiple ToolCalls in one ModelResponse；
- sequential execution；
- fail-stop；
- `NOT_EXECUTED`；
- remaining calls correspondence；
- untouched calls do not consume Tool Call Attempt；
- recoverable result returns to next Model Turn。

### Out of Scope

- dependency analysis；
- parallel Tool execution；
- continue-after-failure heuristic；
- later Policy and confirmation behavior beyond the existing batch contract。

### Acceptance

必须包含：

~~~text
Call 1:
read existing file
→ SUCCESS

Call 2:
read missing file
→ OPERATION_FAILURE

Call 3:
valid read
→ NOT_EXECUTED
~~~

并验证：

- Call 3 未进入 validation / execution；
- Call 3 不增加 attempt；
- ToolResult correspondence 完整。

### Suggested Commit

`feat: implement sequential tool batch semantics`

---

## Step 10 — Runtime Budgets and Model Error Recovery

**Status:** TODO

### Goal

闭合 04 / 05 的 deterministic Runtime budgets 与 model error semantics。

### Scope

- `max_model_turns`；
- `max_tool_call_attempts`；
- active-duration tracking 基础；
- `ModelProtocolError`；
- bounded corrective re-prompt；
- `TransientProviderError`；
- `FatalProviderError`；
- Transport Retry semantics；
- retry same logical `ModelRequest`；
- provider retry exhausted → `FAILED`；
- protocol retry exhausted → `FAILED`。

必须保持：

~~~text
assistant response obtained
→ consumes Model Turn

invalid assistant response
→ already consumed Model Turn

corrective response
→ another Model Turn

transport failure before assistant response
→ no semantic Model Turn
~~~

### Out of Scope

- real provider SDK；
- complex backoff config；
- 09 default values。

### Acceptance

- unit / integration tests 覆盖上述计数语义；
- exhausted provider retry 和 corrective limit 均不会产生下一 Model Turn。

### Suggested Commit

`feat: add runtime budgets and model error recovery`

---

## Step 11 — Shell Tool

**Status:** TODO

### Goal

实现 06 的 bounded local Shell execution mechanism。

### Scope

- `shell(command, cwd, timeout_seconds)`；
- full command string；
- resolved workspace cwd；
- noninteractive stdin；
- filtered environment 基础；
- stdout；
- stderr；
- exit code；
- timeout；
- bounded output；
- process launch error；
- best-effort cleanup。

Outcome：

~~~text
exit 0
→ SUCCESS

exit nonzero
→ UNSUCCESSFUL_COMMAND

timeout / launch / I/O failure
→ OPERATION_FAILURE
~~~

### Out of Scope

- complete Risk Permission engine；
- complex Shell classifier integration；
- background execution framework；
- async。

### Acceptance

- exit 0；
- exit nonzero；
- timeout；
- stderr；
- output bound；
- invalid cwd；
- launch failure。

### Suggested Commit

`feat: add bounded shell execution tool`

---

## Step 12 — Workspace Discovery Tools

**Status:** TODO

### Goal

实现轻量 workspace navigation。

### Scope

- `list_directory`；
- `search_files`；
- one-level directory listing；
- glob path search；
- deterministic ordering；
- `.gitignore`；
- default noise ignores；
- sensitive discovery exclusion；
- bounded results。

### Out of Scope

- `search_text`；
- symbol index；
- recursive directory tree Tool；
- rg backend optimization。

### Acceptance

- `list_directory` 只列一层；
- `search_files` glob 工作；
- ignored / noise paths 被排除；
- sensitive paths 默认不返回；
- result bounded。

### Suggested Commit

`feat: add workspace discovery tools`

---

## Step 13 — Text Search Tool

**Status:** TODO

### Goal

实现 `search_text`。

### Scope

- literal search default；
- optional regex；
- `case_sensitive`；
- optional `file_glob`；
- path subtree；
- line number；
- line text；
- binary / undecodable skip；
- ignore rules；
- sensitive exclusion；
- bounded matches；
- Python baseline implementation。

### Out of Scope

- ripgrep dual backend；
- ranking；
- symbol analysis；
- search cursor。

### Acceptance

- literal；
- regex；
- case sensitivity；
- `file_glob`；
- ignored files；
- bounded output。

### Suggested Commit

`feat: add text search tool`

---

## Step 14 — Conflict-Safe edit_file

**Status:** TODO

### Goal

实现 v1 primary mutation mechanism：

> Exact Text Replacement with Expected Match Count.

### Scope

- `edit_file(path, old_text, new_text, expected_count)`；
- `old_text` non-empty；
- current file re-read at execution；
- exact occurrence count；
- match mismatch；
- stale observation detection；
- `expected_count > 1`；
- line-ending preservation；
- temporary sibling write；
- replace original；
- partial-write avoidance；
- structured edit result。

### Out of Scope

- fuzzy matching；
- line-number edit；
- `apply_patch`；
- AST edit；
- generic overwrite。

### Acceptance

至少覆盖：

- exact one match；
- zero match；
- multiple ambiguous matches；
- `expected_count > 1`；
- concurrent / stale content；
- CRLF preservation；
- write failure。

### Suggested Commit

`feat: add conflict-safe exact file editing`

---

## Step 15 — create_file

**Status:** TODO

### Goal

实现 create-only text file creation。

### Scope

- `create_file(path, content)`；
- NEW path resolution；
- exclusive-create；
- existing target → `FILE_ALREADY_EXISTS`；
- parent must already exist；
- UTF-8 write；
- `bytes_written` result。

### Out of Scope

- overwrite；
- `mkdir -p`；
- directory Tool；
- whole-file rewrite existing file。

### Acceptance

- create success；
- existing target rejection；
- missing parent rejection；
- race-safe exclusive create semantic。

### Suggested Commit

`feat: add create-only file tool`

---

## Step 16 — PreparedToolCall and Local Preparation

**Status:** TODO

### Goal

将已有 LOCAL execution flow 正式收敛到 05 / 06 的：

~~~text
validation
→ preparation
→ PreparedToolCall
→ later policy / execution
~~~

### Scope

- PreparedToolCall frozen value object；
- File dynamic facts；
- resolved paths；
- `affected_paths`；
- sensitive / protected / containment facts；
- Shell validated command / cwd facts；
- `ShellSurfaceFacts`；
- deterministic best-effort Shell surface classifier；
- preparation failure → `OPERATION_FAILURE`；
- unexpected preparation bug → `INTERNAL_TOOL_ERROR`。

Shell classifier 必须保持：

- deterministic；
- lexical / surface based；
- best effort；
- no LLM；
- no full Shell AST parser；
- facts only, no `ALLOW / CONFIRM / DENY` decision。

例如可表达：

- `recognized_actions`；
- `has_compound_syntax`；
- `has_unknown_segment`；
- 03 policy 真正需要识别的 action facts。

### Out of Scope

- PolicyEngine final decision；
- permission UI；
- generic action framework。

### Acceptance

- prepared exact action 与后续 execution 使用同一 validated data；
- outside workspace 是 policy fact；
- preparation operational failure 与 policy fact 区分；
- Shell classifier 能产出 typed surface facts。

### Suggested Commit

`refactor: prepare validated local tool actions`

---

## Step 17 — Explicit Task Constraint Enforcement

**Status:** TODO

### Goal

实现 04 定义的三个封闭 hard constraints。

### Scope

- `FORBID_FILE_MUTATION`；
- `FORBID_COMMAND_EXECUTION`；
- `WRITE_SCOPE`；
- Runtime-owned normalized constraint state；
- immutable constraint snapshot；
- deterministic closed normalizer；
- direct user input；
- `ask_user` clarification answer trusted update path；
- `PASS / REJECT` constraint check；
- canonical path facts for `WRITE_SCOPE`。

Normalizer 保持极小，只识别明确支持的形式，例如：

- 不要修改文件；
- 不要运行命令；
- 只修改 `tests/`。

无法可靠识别：

~~~text
→ semantic guidance
or
→ clarification
~~~

### Out of Scope

- NLP parser；
- LLM classifier；
- policy DSL；
- general task mode；
- arbitrary authorization language。

### Acceptance

- file mutation forbidden；
- command execution forbidden；
- `WRITE_SCOPE` inside / outside；
- model cannot modify hard constraints；
- Risk Confirmation cannot override constraint rejection。

### Suggested Commit

`feat: enforce explicit task constraints`

---

## Step 18 — Risk Permission Engine

**Status:** TODO

### Goal

实现 03 的 deterministic Risk Permission：

~~~text
ALLOW
CONFIRM
DENY
~~~

### Scope

- PolicyEngine；
- File containment facts；
- Sensitive / Protected path policy；
- `ShellSurfaceFacts`；
- recognizable Shell risk；
- compound highest recognizable risk；
- unknown / complex action behavior according to 03；
- structured PermissionCheckResult。

PolicyEngine 只判断，不：

- execute；
- interact with user；
- mutate Run State；
- call LLM。

### Out of Scope

- confirmation UI lifecycle；
- new safety framework；
- LLM safety judge；
- Shell AST parser。

### Acceptance

- 针对 03 permission matrix 建立 deterministic tests；
- constraint result 与 risk permission result 保持分层；
- PolicyEngine 只返回 decision / facts。

### Suggested Commit

`feat: enforce runtime risk permissions`

---

## Step 19 — Exact-Action Permission Confirmation

**Status:** TODO

### Goal

实现 `CONFIRM` 的 `WAITING_FOR_USER` lifecycle 与 exact-action authorization。

### Scope

- PendingAction frozen value object；
- exact validated action snapshot；
- `WAITING_FOR_USER(PERMISSION_CONFIRMATION)`；
- FakeUserInteraction；
- `APPROVE`；
- `REJECT`；
- `CANCEL`；
- one-time authorization；
- cleanup；
- approved action execution；
- old batch fail-stop；
- remaining calls `NOT_EXECUTED`。

必须保证：

~~~text
approval executes stored exact action
~~~

而不是：

~~~text
approval
→ ask model again
→ model reconstructs command
~~~

### Out of Scope

- `ask_user` clarification；
- reusable permission grants；
- session-wide approval。

### Acceptance

- approve；
- reject；
- cancel；
- PendingAction cleanup；
- changed / new action requires new policy evaluation。

### Suggested Commit

`feat: add exact-action permission confirmation`

---

## Step 20 — ask_user InteractionTool

**Status:** TODO

### Goal

实现 same-Run clarification。

### Scope

- `ask_user` Tool；
- `ToolKind.INTERACTION`；
- validation；
- `WAITING_FOR_USER(CLARIFICATION)`；
- `UserInteraction.ask`；
- `ANSWERED`；
- `CANCELLED`；
- Runtime constructs `SUCCESS` ToolResult；
- clarification ends old batch；
- remaining calls `NOT_EXECUTED`；
- clarification answer may enter trusted Task State update path when applicable。

必须证明 `ask_user` 不经过：

- PreparedToolCall；
- Explicit Task Constraint；
- Risk Permission；
- `LocalTool.execute`；
- ToolExecutionResult。

### Out of Scope

- permission confirmation as Tool；
- `DECLINE` state。

### Acceptance

- answer → ToolResult → next Model Turn；
- cancellation → `CANCELLED`；
- same Run retained；
- no duplicate UserMessage + ToolResult representation。

### Suggested Commit

`feat: add runtime clarification tool`

---

## Step 21 — OpenAI-Compatible Model Client

**Status:** TODO

### Goal

在 Runtime 已经能够完全通过 FakeModelClient 测试后，再接真实 LLM。

### Scope

- OpenAICompatibleModelClient；
- ModelClient Protocol implementation；
- SDK / API request；
- InternalMessage → provider wire；
- ToolSpec → native tool schema；
- provider response → ModelResponse；
- ToolCall normalization；
- `call_id` preservation / generation as required by 05；
- provider exception normalization；
- `TransientProviderError`；
- `FatalProviderError`；
- `ModelProtocolError` boundary；
- SDK automatic retry disabled where practical；
- non-streaming complete response；
- 不重新引入 ProviderAdapter。

### Out of Scope

- Runtime control flow changes；
- streaming；
- text ReAct parsing；
- provider framework；
- multiple-provider hierarchy。

### Acceptance

1. real provider no-tool Final smoke test；
2. real provider `read_file` ToolCall smoke test；
3. FakeModelClient tests 仍全部通过。

### Suggested Commit

`feat: add OpenAI-compatible model client`

---

## Step 22 — CLI Composition Root

**Status:** TODO

### Goal

将已有组件组装成真实可运行的本地 Coding Agent CLI。

### Scope

- config loading 基础；
- workspace binding；
- ToolRegistry；
- concrete Tools；
- ContextManager；
- ModelClient；
- PolicyEngine；
- UserInteraction；
- AgentRuntime；
- composition root；
- user task input；
- Run execution；
- final output；
- 保持 CLI 极简。

### Out of Scope

- polished observability；
- full 09 config matrix；
- advanced terminal UI；
- streaming display。

### Acceptance

用户可以在真实 workspace 启动：

~~~text
CLI
→ AgentRuntime
→ real model
→ Tool
→ final
~~~

### Suggested Commit

`feat: wire agent components into CLI`

---

## Step 23 — First Real Coding Task / M1 Completion

**Status:** TODO

### Goal

用一个真实但小型的 coding task 验证 M1 vertical slice。

目标 flow：

~~~text
inspect
→ search/read
→ edit/create if needed
→ shell verification
→ observe failure if any
→ iterate
→ final
~~~

### Scope

- realistic small bug fix or small feature；
- real LLM；
- real workspace；
- Tool loop；
- verification command；
- regression tests for bugs discovered during end-to-end run。

这一步不是增加新架构。发现问题时优先修：

- concrete bug；
- schema mismatch；
- Tool ergonomics blocker；
- incorrect observation；
- contract implementation bug。

不要因为一次失败立刻增加：

- planner；
- patch framework；
- plugin system；
- async；
- new abstraction。

### Out of Scope

- 新的 architecture subsystem；
- 超出 01–06 的功能扩张；
- 为单次失败引入未经证据支持的通用框架。

### Acceptance

至少一个真实任务能够完成完整：

~~~text
User
→ Model
→ inspect
→ mutation
→ verification
→ Final
~~~

并且 Git diff / command evidence 可解释。

### Suggested Commit

`test: cover end-to-end coding agent workflow`

---

## Milestone Mapping

### M0 — Skeleton

**Steps:** Step 0–4

**Goal:** 建立基础 package、provider-neutral protocol、test seams 和最小 Context。

### M1 — Vertical Slice

**Steps:** Step 5–23

最早 vertical slice 从 Step 8 就开始形成，之后持续增加真实能力；Step 23 表示 M1 的真实 coding-task 验收。M1 不意味着必须等到 Step 23 才第一次运行 Agent。

### M2 — Core Runtime / Capabilities

M1 完成后，根据 03–07 补齐尚未实现的 core contract。当前不提前细分 M2 的未来 Steps，也不制定新的 roadmap。

### M3 — Hardening

后续依据 implementation evidence 规划：

- error hardening；
- safety tests；
- context limits；
- permissions；
- robustness。

当前不展开具体 Steps。

### M4 — Submission Polish

后续对应：

- README；
- demo；
- video；
- submission checklist。

当前不展开新的 Step 24+。

