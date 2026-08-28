# Coding Agent v1 — 07–09 Conformance Implementation Plan

## Purpose and Authority

本文是 Coding Agent v1 当前开发执行路线与进度的 canonical source，覆盖 07–09 freeze 后的 conformance work。

本计划不拥有架构决策。01–09 的 canonical owner 文档优先于本计划；本轮 normative targets 以以下内容为核心：

- `07-context-and-prompt-policy.md` §57 Implementation Obligations；
- `08-verification-testing-and-demo.md` 的 requirement/evidence traceability 与 §52 07-Conformance Verification；
- `09-cli-observability-and-configuration.md` §58 09-Conformance Work and Evidence。

若本计划与 owner document 冲突，必须停止当前 Step、记录具体 contradiction，并先修正 owner 或本计划；不得静默绕过 contract。

## Progress Summary

```text
Current Phase: M2 — 07/08/09 Conformance
Current Step: Step 0
Completed: 0 / 19
In Progress: None
Next: Step 0 — Baseline and Conformance Snapshot
```

Step 0 到 Step 18 共 19 Steps。进度状态只有 `TODO`、`IN PROGRESS`、`DONE`。实现 Agent 不得自动把 Step 标记为 `DONE`；状态由开发者 review 后单独更新。

## Execution Rules

1. 一次只实现一个 Step。
2. 当前 Step 的实现与测试一起完成，验收通过后才进入下一 Step。
3. 不提前实现后续 TODO Step，不为未来需求增加 abstraction。
4. 优先小步 vertical slice，不建立空 framework。
5. 每个 Step 尽量形成独立、可测试、可解释的 commit。
6. 已经由此前 hardening 实现的行为，在对应 Step 先验证；若 contract 与 evidence 均已闭合，不重复改写生产代码。
7. 只修复当前 Step 暴露的真实 contract gap，不顺手扩大范围。
8. 发现 concrete implementation contradiction、impossible contract、owner conflict 或无法实现的 safety invariant 时，停止并按 Architecture Reopen Rule 处理。
9. 不因“更优雅”“以后可能有用”或“方便扩展”而 reopen architecture。
10. 未经开发者明确要求，不自动 commit、push 或更新 Step 状态。

## Commit Discipline

- 默认一个 Step 对应一个主要 commit；自然独立的变更可以拆分。
- commit 必须代表可解释、可测试的增量。
- commit 前运行当前相关 tests；合理时运行完整 regression suite。
- 不修改 Git 历史，不提交后续 Step 的未完成代码。
- 不为了增加 commit 数量机械拆分变更。

## How to Execute a Step

1. 从本文读取 Current Step。
2. 只读取与该 Step 有关的 architecture owner documents。
3. 只实现当前 Step 的 Goal 和 Scope。
4. 添加或更新当前 Step 所需 tests。
5. 运行相关 tests，合理时运行完整 `pytest`。
6. 报告 changed files、tests、verification 与 blockers。
7. 不自动开始下一 Step，不自动将当前 Step 改为 `DONE`。

## Architecture Reopen Rule

只有以下情况允许 reopen owner document：

- concrete implementation contradiction；
- impossible contract；
- two canonical owner documents truly conflict；
- safety invariant cannot be implemented as written。

发现真实 contradiction 时：

1. stop current Step；
2. record exact blocker；
3. identify canonical owner；
4. resolve design；
5. update owner document；
6. resume Step。

以下理由不足以 reopen：

- 另一种设计更优雅；
- 未来扩展更方便；
- 可以顺便支持更多 provider；
- 可以抽象成 framework；
- 可能以后有用。

---

## M2-A — Configuration and Resource Foundation

## Step 0 — Baseline and Conformance Snapshot

**Status:** TODO

### Goal

确定当前代码与测试基线，不改变产品行为。

### Scope

- 运行完整 deterministic test suite；
- 记录 baseline test 数量与任何失败原因；
- 核对 07 §57、08 traceability/§52、09 §58 的 implementation obligations；
- 记录当前工作树状态，并区分既有用户文件与本轮产生的变更；
- 验证此前 hardening 已覆盖的 contract，不重复实现。

### Out of Scope

- 功能修复；
- 生产代码重构；
- 新 abstraction；
- 修改 architecture documents。

### Acceptance

- `pytest -q` 结果与测试数量已记录；
- 所有 baseline failure 都有明确归因；
- 既有非本轮文件未被修改或纳入提交；
- 后续 Step 的真实 gap 已可从 owner contract 与 evidence 判断。

### Suggested Commit

无需 commit；若只更新本文，可使用 `docs: start 07-09 conformance track`。

---

## Step 1 — Centralize v1 AgentConfig

**Status:** TODO

### Goal

把 09 的 operational defaults、required values、precedence 与 validated ranges 收敛为单一 Lean configuration source。

### Scope

- 新增或收敛一个 `AgentConfig`；
- required：`model`、`base_url`、explicit workspace、environment-only API key；
- defaults：model turns 24、tool attempts 64、active duration 900 seconds、context chars 80,000、debug false；
- public ranges：turns 1..64、attempts 1..256、duration 1..3600、context 8,000..256,000；
- precedence：CLI > environment > built-in default；
- `model`、`base_url`、workspace、API key 无 built-in default；
- secret-bearing field 不进入 `repr`。

### Out of Scope

- ConfigManager / ConfigService；
- provider profiles；
- configuration file system；
- budget hierarchy。

### Acceptance

- precedence tests；
- missing model/base URL/API key/workspace 在 startup/config boundary 失败；
- 所有 public range 上下界与越界 tests；
- API key 不出现在 config representation；
- Runtime、Context 与 CLI 不再各自维护冲突的 public defaults。

### Suggested Commit

`feat: centralize validated agent configuration`

---

## Step 2 — Provider Startup Defaults

**Status:** TODO

### Goal

落实 09 的 provider startup contract，并在 Session 创建前验证完整配置。

### Scope

- default client 为 `OpenAICompatibleModelClient`；
- required model、base URL 与 environment API key；
- per-attempt provider request timeout 60 seconds；
- SDK automatic retry disabled/zero where practical；
- Runtime 继续 owning retry semantics。

### Out of Scope

- Runtime retry backoff；
- multi-provider hierarchy；
- provider profile registry；
- streaming。

### Acceptance

- valid config 可构造 concrete client；
- missing model/base URL/key 在 Session 前失败；
- request timeout 为 60 seconds；
- SDK 不与 Runtime 重复 retry。

### Suggested Commit

`feat: enforce provider startup configuration`

---

## Step 3 — Shell Timeout and Platform Backend

**Status:** TODO

### Goal

落实 09 的 bounded Shell timeout 与 platform backend contract。

### Scope

- default timeout 120 seconds；
- absolute maximum 300 seconds；
- argument/schema validation enforce `1 <= timeout_seconds <= 300`；
- 越界在 execution 前产生 validation failure，不 clamp；
- Windows 使用 `COMSPEC`，POSIX 使用 `/bin/sh`；
- 移除 public `CODING_AGENT_SHELL` override；
- 保留 test-only injection seam。

### Out of Scope

- Shell execution semantic redesign；
- shell profile/config system；
- new sandbox；
- background execution。

### Acceptance

- default 120；1 与 300 valid；0 与 301 invalid；
- Windows/POSIX backend selection tests；
- public environment 不能切换 Shell backend。

### Suggested Commit

`fix: enforce bounded shell timeout and backend`

---

## Step 4 — Align File and Discovery Resource Limits

**Status:** TODO

### Goal

落实 09 concrete resource defaults，并使内部工作本身有界。

### Scope

- `read_file` default 200 lines、absolute max 400 lines、returned content max 20,000 bytes；
- `list_directory` max 200 direct entries；
- `search_files` max 200 paths；
- `search_text` max 100 matches；
- `search_text` model projection max 16,000 chars；
- 使用 bounded/streamed reading、bounded discovery 与 early stop；
- 保持 continuation/truncated semantics。

### Out of Scope

- 通用 streaming framework；
- symbol index；
- ranking；
- 新的 search backend abstraction。

### Acceptance

- large-file、large-directory、large-match tests 证明内部与返回结果有界；
- `truncated` 与 continuation 正确；
- line/byte absolute limits 正确；
- 已符合 contract 的实现只补 evidence，不重复重写。

### Suggested Commit

`fix: align bounded file and discovery limits`

---

## Step 5 — Preserve Shell Head and Tail at Capture Boundary

**Status:** TODO

### Goal

让 06 resource capture 保留足够的 bounded head/tail 信息，供 07 model projection 使用。

### Scope

- stdout 与 stderr 各自 capture 不超过 64 KiB；
- truncated stream 同时保留 head、tail 与 truncation fact；
- capture 与最终 model-visible 8,000-char projection 保持分层；
- 使用小型 bounded collector 或等价局部实现。

### Out of Scope

- 通用 stream subsystem；
- model projection；
- async/background process framework。

### Acceptance

- 大输出中的 `HEAD_MARKER` 与 `TAIL_MARKER` 均被 capture；
- 每个 stream capture 独立且不超过 resource cap；
- short output 保持 faithful。

### Suggested Commit

`fix: preserve bounded shell head and tail`

---

## M2-B — Context and Prompt Conformance

## Step 6 — Context Run Lifecycle and Session Continuity

**Status:** TODO

### Goal

建立正确的 current-Run、completed-run continuity 与 pending correspondence 生命周期；本 Step 不实现 context budget。

### Scope

- 明确区分 completed-run continuity、current-run history、pending ToolCall correspondence；
- 新 Run 重置 current-run transient state 与 `history_incomplete = false`；
- COMPLETED 只保留 initial user task + final assistant response，最多 1 个 completed Run；
- FAILED/CANCELLED 不进入 continuity；
- 集中式 `end_run` 或等价 cleanup boundary；
- terminal path 清理 pending ToolCall、ask_user transient state 与 incomplete unit。

### Out of Scope

- context budget/eviction；
- Base Prompt；
- ToolResult projection；
- persistent Session。

### Acceptance

- COMPLETED Run1 → Run2 只看到 task + final continuity；
- FAILED/CANCELLED Run 不进入 continuity；
- pending tool 时 interruption 后 Run2 可正常开始；
- terminal cleanup 不留下 orphan correspondence。

### Suggested Commit

`fix: make context run lifecycle recoverable`

---

## Step 7 — Atomic Tool Units and Bounded Context Eviction

**Status:** TODO

### Goal

实现 07 的 provider-neutral bounded working context 与 atomic eviction。

### Scope

- Assistant ToolCall message 与 grouped ToolResult message 作为 atomic Tool Unit；
- eviction order：oldest completed continuity，再 oldest removable current-run unit；
- 保护 Base Prompt、current initial task、current ask_user unit、latest completed Tool Unit 与 required transient instructions；
- default budget 80,000 chars，从 AgentConfig 注入；
- destructive eviction 后 `history_incomplete = true`，sticky until Run end；
- mandatory content 无法 fit 时抛出 internal `ContextLimitError` 或等价错误。

### Out of Scope

- summarization；
- provider tokenizer；
- semantic memory；
- 新 public error taxonomy。

### Acceptance

- atomic eviction 无 orphan ToolCall/ToolResult；
- completed continuity 先淘汰；
- latest required unit 受保护；
- `history_incomplete` false→true、sticky、next Run reset；
- mandatory overflow 产生 internal failure。

### Suggested Commit

`feat: add bounded atomic context eviction`

---

## Step 8 — Stable Base Prompt and Effective System Prefix

**Status:** TODO

### Goal

实现每次 request 重建的 Stable Base Prompt 与 deterministic Effective System Prefix。

### Scope

- `BASE_SYSTEM_PROMPT` 不作为 history message；
- prefix order：Base Prompt → truncation notice → optional repeated-action warning → optional protocol corrective instruction；
- corrective instruction 始终是 Effective System Prefix 的最后一项；
- retained continuity 与 current-run history 位于 prefix 之后；
- `ModelRequest.tools` 保持独立字段；
- 使用小型 pure helper，不建立 PromptManager。

### Out of Scope

- Prompt framework/pipeline；
- prompt templating system；
- ToolResult projection；
- Runtime retry integration。

### Acceptance

- Base 永远第一；
- conditional notice/warning 顺序正确；
- corrective 始终最后；
- transient instructions 不进入 history 或 continuity。

### Suggested Commit

`feat: assemble stable system prefix`

---

## Step 9 — ToolResult Model Projection

**Status:** TODO

### Goal

实现 bounded、faithful、secret-safe 的 model-visible ToolResult projection。

### Scope

- pure `project_tool_result` helper 或等价局部模块；
- discovery/search 输出相对路径、必要行号/文本与 truncation；
- `read_file` faithful bounded content；
- `edit_file` 只投影 path + replacement count；
- `create_file` concise result；
- `ask_user` faithful answer；
- error 投影 outcome + formal code + safe concise details，无 traceback；
- preserve `call_id` 与 `ToolResult.outcome`。

### Out of Scope

- ToolResultProjector subsystem；
- human CLI rendering；
- provider-specific formatting；
- Shell head/tail projection。

### Acceptance

- 每个 v1 Tool 至少一个 projection test；
- mutation input/content 不被完整回显；
- errors 不暴露 traceback 或 secrets；
- projection 不改变 call correspondence/outcome。

### Suggested Commit

`feat: add bounded model tool-result projection`

---

## Step 10 — Shell Model Projection

**Status:** TODO

### Goal

在 bounded capture 基础上实现 07/09 的 Shell model-visible projection。

### Scope

- stdout/stderr 各自最多 8,000 chars；
- 长 stream 使用 bounded head + explicit omission marker + bounded tail；
- streams 独立投影；
- preserve exit code、truncated state 与 Tool outcome；
- short output faithful。

### Out of Scope

- Shell capture/process changes；
- CLI renderer；
- streaming UI。

### Acceptance

- short stdout/stderr unchanged；
- long stdout/stderr 各自保留 HEAD + TAIL + omission marker；
- streams 不混合；
- SUCCESS / UNSUCCESSFUL_COMMAND / OPERATION_FAILURE 不变。

### Suggested Commit

`feat: project shell output with head and tail`

---

## M2-C — Runtime Integration

## Step 11 — Make Runtime Use the New Context Contract

**Status:** TODO

### Goal

让 Runtime 每个 semantic Model Turn 通过 ContextManager 构造最终 immutable `ModelRequest` snapshot。

### Scope

- Runtime 不自行拼接 history；
- ContextManager build final request snapshot；
- `ContextLimitError` 映射到现有 fatal Runtime failure/FAILED，且不发送下一 Model Turn；
- transport retry 重用同一个 immutable logical request snapshot；
- protocol corrective 是新 Model Turn，重新 build request 并加入 corrective prefix。

### Out of Scope

- transport backoff values；
- terminal cleanup redesign；
- observer events；
- CLI changes。

### Acceptance

- normal request assembly；
- mandatory overflow → FAILED 且无 next turn；
- transport retry request snapshot 相同；
- corrective 产生新 request/turn，prefix ordering 正确。

### Suggested Commit

`feat: integrate bounded context into runtime`

---

## Step 12 — Terminal Failure Boundary and Session Recovery

**Status:** TODO

### Goal

集中收口 Runtime terminal paths，确保 interrupted/failed Run 不毒化 Session。

### Scope

- normal terminal、KeyboardInterrupt、unexpected runtime/context/policy error 的统一 boundary；
- KeyboardInterrupt → CANCELLED；
- unexpected fatal Runtime failure → FAILED；
- centralized terminal path 清理 AgentRun pending state 与 ContextManager run state；
- no pending ToolCall/ask_user correspondence after terminal；
- same Session 可启动下一 Run。

### Out of Scope

- persistent Session；
- new terminal state taxonomy；
- retry backoff；
- CLI presentation polish。

### Acceptance

- Run1 在 pending Tool execution 时 KeyboardInterrupt → CANCELLED；
- 同一 Session 的 Run2 正常发送 request 并可 COMPLETED；
- unexpected exception → FAILED 后 Run2 仍可用；
- Runtime/Context cleanup 不各留一半状态。

### Suggested Commit

`fix: recover session after terminal run interruption`

---

## Step 13 — Transport Retry and Protocol Limits

**Status:** TODO

### Goal

落实 deterministic transport retry 与 consecutive protocol-error limits。

### Scope

- max transport retries 2；
- delays 0.5s、1.0s，cap 2s，无 jitter；
- 用 Lean `sleep_fn`/clock seam 测试，不建 time framework；
- retry 重用相同 logical request，且不增加 semantic Model Turn；
- max consecutive protocol errors 3；
- invalid #1/#2 corrective，invalid #3 terminal，无第 4 个 corrective request；
- valid response 重置 protocol-error counter。

### Out of Scope

- configurable backoff framework；
- jitter；
- provider SDK retry；
- new protocol taxonomy。

### Acceptance

- recorded sleeps 为 `[0.5, 1.0]`；
- transport retry snapshot unchanged/no turn increment；
- corrective increments turn；
- third consecutive protocol failure terminal；
- valid response resets counter。

### Suggested Commit

`fix: bound provider retry and protocol recovery`

---

## M2-D — Observability and CLI

## Step 14 — Generalize Existing Tool Activity into Lean Observer Seam

**Status:** TODO

### Goal

把现有 narrow callback 收敛为 09 要求的 optional synchronous read-only observer seam，不引入 EventBus。

### Scope

- optional `observer/on_event` callback；
- events 至少覆盖 run start/terminal、tool proposal/result、policy outcome、permission request/result、provider retry、protocol corrective、context eviction/history transition、budget exhaustion；
- payload small、bounded、normalized、secret-safe；
- callback return value ignored；
- callback exception isolated，不影响 Runtime semantic behavior。

### Out of Scope

- EventBus；
- persistent logging；
- telemetry backend；
- observer-controlled Runtime decisions。

### Acceptance

- observer receives representative events；
- observer raises exception 时 Agent 仍按原语义完成；
- payload 不含 secrets 或 unbounded raw data；
- absent observer 无行为差异。

### Suggested Commit

`feat: add isolated runtime observability hook`

---

## Step 15 — Normal and Debug CLI Rendering

**Status:** TODO

### Goal

实现 09 的 bounded、secret-safe Normal/Debug terminal rendering。

### Scope

- Normal 显示 workspace/model、concise Tool activity/result、Final；
- 默认不打印 read/search full content、old/new text、create content、raw ToolResult/provider response/full config；
- Shell command rendering bounded + redacted；
- Debug 增加 turn/tool counts、call_id、outcome/code、policy facts、context state、eviction、history flag、retry/corrective 与 normalized usage；
- raw provider metadata/HTTP payload、environment 与 secret 仍隐藏；
- no persistent log。

### Out of Scope

- rich TUI；
- streaming display；
- persistent trace database；
- renderer changing Runtime behavior。

### Acceptance

- Normal/Debug capture tests；
- secret、old/new text、raw provider payload 不出现在输出；
- Normal hides usage，Debug only shows normalized usage；
- renderer output bounded。

### Suggested Commit

`feat: add bounded normal and debug cli rendering`

---

## Step 16 — Interactive Session Semantics

**Status:** TODO

### Goal

把 CLI 收敛为 09 定义的 same-process persistent interactive Session。

### Scope

- empty input → reprompt/no Run；
- `/exit`、`/quit` 与 top-level EOF → exit 0；
- top-level Ctrl+C → exit Session；
- active-Run Ctrl+C → CANCELLED、display、reprompt；
- FAILED/COMPLETED Run 后 reprompt；
- `COMPLETED` 不自动呈现为 task success；
- ask_user 与 exact-action permission 使用不同提示；
- permission default No；
- user wait timeout = None。

### Out of Scope

- persistent Session resume；
- GUI/TUI；
- user-wait timer；
- reusable permission grants。

### Acceptance

- fake stdin/interaction tests 覆盖 empty、exit、EOF、top-level Ctrl+C；
- active Run cancellation 后 Session 可继续；
- FAILED/COMPLETED 后新 Run 可启动；
- ask_user/permission presentation 不混淆。

### Suggested Commit

`feat: make cli session persist across runs`

---

## M3 — 08 Evidence Closure

## Step 17 — Fill Deterministic Contract-Test Gaps

**Status:** TODO

### Goal

按 01–09 normative requirements 补齐 deterministic evidence；已实现行为只补证据，真实 contract failure 才修生产代码。

### Scope

- concrete ModelClient serialization/normalization/error tests；
- startup invariants and invalid config/workspace tests；
- Base Prompt、prefix ordering、atomic eviction、history flag、overflow、Shell head/tail tests；
- terminal interruption/recovery、retry snapshot、corrective semantics tests；
- explicit constraints、workspace escape、protected/sensitive path、Runtime Secret、Shell timeout、risk decision no-side-effect tests；
- CLI precedence、bounded rendering、redaction 与 Session control tests；
- 对此前 hardening 项逐项验证，不因已有 commit 而跳过 evidence。

### Out of Scope

- 新 production subsystem；
- architecture redesign；
- 为测试建立 framework；
- 把未实现 normative contract 直接标记 Deferred。

### Acceptance

- 每个发现的 gap 有 owner requirement、implementation 与 deterministic test；
- malformed provider response、finish/completion terminal semantics、credential isolation 等边界均有 evidence；
- full deterministic `pytest -q` green。

### Suggested Commit

`test: close v1 conformance evidence gaps`

---

## Step 18 — Deterministic E2E and Traceability Closure

**Status:** TODO

### Goal

用少量高价值 full-path tests 与 Lean Markdown mapping 完成 deterministic acceptance。

### Scope

- FakeModelClient + real Runtime + real Tools + real temporary workspace；
- E2E A：inspect/search/read → edit → verification failure → adjust → verification pass → Final；
- E2E B：Run1 interrupted → CANCELLED；same Session Run2 normal tool/final → COMPLETED；
- 建立 `Requirement → Implementation → Test` 的 Lean traceability checklist；
- 记录可解释的 workspace diff 与 command evidence。

### Out of Scope

- requirements database/spreadsheet system；
- large E2E matrix；
- real-model nondeterministic acceptance；
- new architecture。

### Acceptance

- 两条 deterministic E2E green；
- lifecycle、mutation、verification observation 与 Session recovery 均通过真实组件路径；
- 07–09 normative obligations 可追踪到 code + evidence；
- full deterministic suite green。

### Suggested Commit

`test: complete deterministic agent acceptance`

---

## M4 — Real-Model Acceptance and Operational Tuning

M2/M3 完成后，使用 real ModelClient、Runtime、Tools 与代表性小型 workspace 做少量普通用户风格 coding tasks。

重点观察：

- `max_context_chars = 80_000` 是否导致过度 eviction、`history_incomplete` 与重复读取；
- 24 model turns / 64 Tool attempts 是否适合 v1 primary use case；
- ToolResult projection 是否保留完成任务所需信息；
- Normal CLI 是否适合短演示。

如 evidence 表明 80,000 需要调整，只能在 09 已有 validated public range 8,000..256,000 内调整 concrete default，不改变 Context architecture。此阶段不建立 tuner、benchmark framework 或新的 Runtime subsystem。

M4 当前不是新的 numbered Step；完成 Step 18 后再依据 deterministic evidence 与真实任务结果制定最小验收记录。

## Dependency Order

```text
Step 0   baseline

Step 1   AgentConfig
Step 2   Provider startup
Step 3   Shell timeout/backend
Step 4   File/discovery bounds
Step 5   Shell capture head+tail

Step 6   Context Run lifecycle
Step 7   Atomic eviction + history_incomplete
Step 8   Base Prompt + Effective System Prefix
Step 9   ToolResult projection
Step 10  Shell model projection

Step 11  Runtime ↔ Context integration
Step 12  terminal cleanup / Session recovery
Step 13  retry backoff / protocol limit

Step 14  observability seam
Step 15  Normal/Debug CLI
Step 16  interactive Session

Step 17  deterministic evidence closure
Step 18  E2E + traceability

M4       real-model acceptance / operational tuning
```

顺序原则：先使 resource/config facts 有界，再建立 Context truth；随后接入 Runtime lifecycle，最后由 observer facts 驱动 CLI rendering，并由 08 evidence closure 验证完整 contract。不得为了先做易见的 Prompt 或 CLI 改动而绕过其依赖。
