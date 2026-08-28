# 08 Verification, Testing, and Demo Policy

## 1. Purpose

本文定义 v1 Coding Agent 的：

- Task Verification；
- verification evidence 与 Final claim discipline；
- Agent 自身的 testing strategy；
- deterministic Runtime / Tool / Context contract tests；
- real-provider smoke testing；
- end-to-end acceptance；
- Demo 的能力要求与选择原则。

本文主要回答：

- Agent 修改代码后什么时候应该验证；
- verification 是 Runtime hard phase 还是模型行为；
- 如何选择 targeted / broader verification；
- verification failure 后 Agent 应如何继续；
- 什么 evidence 足以支持什么 Final claim；
- verification evidence 何时 stale；
- Coding Agent 自身应该如何测试；
- FakeModelClient 与真实模型分别用于什么；
- 最终 Demo 应证明什么，而不是现在固定具体 demo task。

本文不重新定义：

- Tool execution / shell semantics：由 `06-toolset-and-file-editing.md` 负责；
- Prompt / Context / ToolResult projection：由 `07-context-and-prompt-policy.md` 负责；
- permission / safety boundary：由 `03-safety-and-execution-boundaries.md` 负责；
- Run lifecycle / failure / retry / termination：由 `04-agent-runtime-model.md` 负责；
- protocol types：由 `05-component-and-protocol-contracts.md` 负责；
- CLI display、logging 和具体默认值：由 `09-cli-observability-and-configuration.md` 负责；
- 最终具体 Demo fixture、视频脚本和录制方案：留到 M4 Submission Polish。

核心原则：

> Verification is model-directed coding behavior, not a mandatory Runtime phase.

以及：

> The strength of a completion claim must not exceed the strength, scope, and freshness of the observed evidence.

---

## 2. Verification Is Not a Fixed Runtime Phase

v1 不建立强制：

```text
EDITED
→ VERIFYING
→ VERIFIED
→ FINAL
```

生命周期。

Runtime 不要求：

```text
任何 edit
→ 必须 shell(test)
→ exit 0
→ 才允许 Final
```

原因：

* read-only task 可能不需要验证；
* README / documentation mutation 不一定需要测试；
* 不同语言和项目使用不同 verification mechanism；
* Runtime 不应承担复杂 language-specific test selection；
* Coding Agent 的下一步仍由模型根据任务和最新 observation 决定。

因此：

> Verification selection and sufficiency are model-dependent coding judgments.

Runtime只负责：

* 执行模型提出的 ToolCall；
* enforce constraints / permission；
* 返回真实 ToolResult；
* enforce budgets / termination。

### 2.1 `COMPLETED` Is a Lifecycle Result

> `COMPLETED` means that AgentRuntime accepted a legal non-empty Final Response and ended the Run normally.

`COMPLETED` 是 Runtime lifecycle result，不是 task outcome quality 或 verification grade。它不等于：

* task fully succeeded；
* task fully verified；
* all tests passed；
* no remaining limitation；
* no blocked work。

v1 不新增 `VERIFIED`、`UNVERIFIED`、`PARTIAL`、`BLOCKED`、`SUCCESS_WITH_WARNINGS` 或 `PARTIALLY_COMPLETED` 等 Runtime lifecycle states。任务成果的 completion claim、observed evidence、verification scope、remaining limitation 与 blocked reason 通过诚实的 Final Response 表达。

> A Run may be `COMPLETED` even when the user task is only partially completed, blocked, or unverified, provided the Final Response describes that outcome honestly.

因此：

```text
Runtime lifecycle state
!=
task outcome quality / verification grade
```

---

## 3. Verification Guidance

虽然 verification 不是 Runtime hard gate，但模型应遵循以下 guidance：

1. 对 mutating software-engineering task，在 practical 时执行与修改最相关的 verification。
2. Verification strength 应与修改范围和最终 claim 强度相匹配。
3. 优先使用 repository 已支持的 verification mechanism。
4. 优先从最小但有意义的 targeted verification 开始。
5. 修改影响面较大或风险较高时，在 practical 时扩大 verification scope。
6. verification failure 作为新的 diagnostic observation继续推理。
7. 如果无法完成 meaningful verification，应在 Final 中明确说明。
8. 不得把未经验证的推断描述成已验证事实。

---

## 4. Typical Verification by Task Shape

以下只是模型 guidance，不建立 Runtime TaskType enum 或 deterministic classifier。

### 4.1 Read-Only / Analytical Task

例如：

* code review；
* code explanation；
* repository exploration；
* bug localization。

通常不要求 mutation verification。

必要时模型可以使用：

* read/search；
* non-mutating diagnostics；
* tests/build commands用于理解问题。

但不存在强制 verification phase。

### 4.2 Code Mutation

例如：

* bug fix；
* feature implementation；
* refactoring；
* test authoring。

在 practical 时应优先执行：

* relevant targeted test；
* small reproduction；
* build；
* type check；
* lint；
* project-specific verification command。

### 4.3 Non-Code Mutation

例如：

* README；
* documentation；
* simple configuration。

verification 可以是：

* re-read changed content；
* syntax/config parser；
* formatter；
* lightweight project check。

不要求为了无关文档修改执行昂贵 full suite。

---

## 5. Prefer Repository-Supported Verification

模型应优先发现并使用项目已有的 verification convention，例如：

```text
README
CONTRIBUTING
pyproject.toml
package.json
Makefile
Cargo.toml
pom.xml
CI configuration
existing test files
existing scripts
```

例如项目已有：

```text
npm test
pytest
cargo test
make test
```

则优先使用这些已有机制，而不是无必要地引入新的工具或 dependency。

这属于 semantic guidance。

Runtime 不解析 repository 文件以自动决定 test command。

---

## 6. Smallest Meaningful Verification First

默认 guidance：

```text
smallest meaningful targeted verification
        ↓
observe result
        ↓
broaden verification if change scope/risk justifies it
```

例如修改单一 parser edge case：

```text
pytest tests/test_parser.py
```

通常比一开始运行超大 full suite 更适合作为快速 feedback。

原因：

* feedback 更快；
* 更容易定位 failure；
* 节约 Tool budget；
* 更适合 iterative Agent loop。

如果修改：

* shared core logic；
* public interface；
* cross-cutting behavior；

则 targeted verification通过后，可以在 practical 时执行 broader suite。

---

## 7. Verification Failure Is an Observation

如果：

```text
edit
→ shell(test)
→ non-zero exit
```

这不自动意味着：

```text
Agent Run → FAILED
```

而是：

```text
verification failure
→ ToolResult observation
→ model reasons from failure
→ read / search / edit / rerun / final
```

04 的 Unsuccessful Command semantics保持不变。

因此：

> Verification failure is diagnostic evidence, not automatic Runtime failure.

模型不得机械重复完全相同的 failing command，除非：

* relevant state 已改变；
* 有明确理由重新执行。

---

## 8. Final Is Allowed Without Successful Verification

Runtime 不因为：

```text
tests still failing
verification unavailable
```

而禁止 syntactically valid Final。

Agent可以诚实结束：

```text
Implemented X, but Y could not be verified because Z.
```

例如：

* missing external service；
* unavailable dependency；
* permission denied；
* environment mismatch；
* timeout；
* unresolved test failure。

因此：

> Verification status constrains what completion claims are honest; it does not create a mandatory Final gate.

---

## 9. Verification Actions Receive No Permission Exemption

verification intent 不绕过 03。

例如为了测试需要：

```text
pip install ...
npm install ...
network access
Git mutation
```

这些 action仍按正常：

```text
Explicit Task Constraints
Risk Permission
```

进行处理。

因此：

> Verification intent does not expand permission.

---

## Evidence and Claim Discipline

### 10. Evidence-Bounded Claims

Final Response中的 claim 强度不得超过实际 evidence。

例如只观察到：

```text
edit_file → SUCCESS
```

可以支持：

> `auth.py` was modified.

但不能单独支持：

> The authentication bug is fixed.

如果观察到：

```text
pytest tests/test_auth.py -q
→ 12 passed
```

可以支持：

> The targeted auth tests pass.

但不能支持：

> All tests pass.

只有真实观察到 full relevant suite 成功，才能做相应 broader claim。

---

### 11. Evidence Scope

Verification evidence只支持其实际覆盖的 scope。

例如：

```text
pytest tests/test_parser.py
→ 8 passed
```

支持：

> The targeted parser tests pass.

不支持：

> The entire repository test suite passes.

类似地：

```text
python reproduction.py
→ expected result
```

证明该 reproduction scenario成功，不自动证明所有相关 edge case。

因此：

> Verification evidence only supports claims within the scope actually exercised.

---

### 12. Evidence Freshness

Verification evidence绑定于实际被验证的 Workspace State。

例如：

```text
edit A
test → pass
edit A again
final
```

此前 test pass不能自动支持最后一次 edit 后的最终状态。

因此：

> Later relevant mutations can make earlier verification evidence stale.

如果在 successful verification之后又进行了 relevant mutation，模型在声称最终状态已验证前，应重新运行适当 verification。

“relevant”是 model-dependent semantic judgment。

例如修改 README 通常不会让之前的 auth test evidence失效。

Runtime 不维护 dependency graph 或 automatic evidence invalidation graph。

---

### 13. Failed Verification Is Still Evidence

失败结果仍然是有效 observation。

例如：

```text
pytest
→ 1 failed
```

可以支持：

> The full suite is not currently passing.

如果模型进一步检查后拥有足够依据，可以准确描述 failure location / reason。

但不能把 failure改写成成功。

---

### 14. Missing Verification Must Be Reported Honestly

如果没有 meaningful verification evidence：

模型不得推断：

> therefore verified.

而应明确：

```text
not verified
could not verify
only inspected statically
only targeted check was performed
```

例如：

> Implemented the parser change. I could not run the integration tests because the required service is unavailable.

---

### 15. Pre-Existing Failure Claims Require Evidence

模型只有在有实际 baseline evidence 时，才能明确声称某 failure：

> pre-existing.

例如：

```text
before mutation:
test X fails

after mutation:
same test X fails
```

可以支持其 pre-existing 性质。

如果模型只在修改后第一次观察到 failure：

不得武断说：

> This is a pre-existing failure.

应更准确地说：

> The broader suite currently fails in X; I did not establish whether this predates the change.

---

### 16. Baseline Verification Is Useful but Not Mandatory

Bug fix任务中：

```text
run failing test
→ observe red
→ fix
→ observe green
```

通常是高质量 evidence。

但 v1 不规定所有 mutating task必须先跑 baseline。

原因：

* 用户可能已经给出足够 reproduction；
* test suite可能昂贵；
* feature task不一定存在 failing baseline；
* bug可能通过代码 inspection即可定位。

因此：

> Baseline verification is recommended when it materially improves diagnosis or evidence, but is not a mandatory Runtime phase.

---

### 17. Command Success Is Not Verification Sufficiency

Runtime知道：

```text
shell exit_code == 0
```

但这只表示该 command执行成功。

例如：

```text
echo hello
→ exit 0
```

显然不能证明 coding task完成。

同样：

```text
compileall
```

只支持 syntax / compilation相关 claim。

```text
lint
```

只支持对应 lint claim。

因此：

> Execution success and semantic verification sufficiency are different concepts.

Runtime不维护：

```text
verification_success = true
```

这类通用状态。

verification sufficiency由模型结合 command语义与用户任务判断。

---

### 18. Final Response Evidence Closure

对于 mutating task，Final应让用户可以理解：

1. 改了什么；
2. 实际验证了什么；
3. 还有什么未验证、失败或未解决。

不要求固定 Markdown template。

例如：

```text
Fixed the pagination boundary bug in `src/pagination.py` and added a regression
test. `pytest tests/test_pagination.py -q` passes with 6 tests. I did not run
the full suite.
```

Final不得：

* 声称未执行的 command已执行；
* 声称 failed test通过；
* 把 targeted verification称为 full-suite verification；
* 隐藏已知的重要 limitation。

---

## Agent Testing

### 19. Testing Strategy

Coding Agent自身的 correctness主要通过：

> deterministic automated testing

验证。

真实模型具有：

* nondeterminism；
* provider变化；
* latency；
* network dependence；
* API cost；

因此不适合作为主要 regression oracle。

v1 testing strategy：

```text
Unit Tests
Tool Contract Tests
Deterministic Runtime Integration Tests
End-to-End Deterministic Tests
Optional Real-Provider Smoke
Manual / Demo Acceptance
```

#### 19.1 Systematic v1 Test Language

v1 的系统测试范围明确为：

```text
Architecture:
language-neutral

Systematic v1 E2E / acceptance coverage:
small Python projects

Other primarily text-based code projects:
best-effort
```

Small Python projects are the primary systematically tested v1 coding environment. 这不改变产品与 Runtime 的 language-neutral architecture；Runtime、Tool 与 Context contracts 不绑定 Python。其他以文本源码为主的项目继续 best-effort，不宣称不同语言具有相同成功率或测试覆盖，也不为此新增 Runtime language detection 或 TaskType classifier。

#### 19.2 Startup Invariant Tests

Composition Root / startup 必须在关键 invariant 不满足时 fail closed，而不是进入部分有效的 Agent Session。Deterministic startup tests 至少覆盖：

* duplicate Tool name；
* invalid `ToolSpec` / invalid tool schema；
* invalid critical model configuration；
* missing / invalid required provider configuration where applicable；
* invalid workspace root；
* unavailable workspace；
* workspace binding / canonicalization failure；
* 03–06 已 normatively required 的其他 startup invariants。

该范围只覆盖阻止 valid and safe Agent composition / startup 的条件，不扩展成全部 CLI 输入错误测试。

---

### 20. Model Client Test Seams

#### 20.1 FakeModelClient Is the Primary Runtime Test Seam

`FakeModelClient` 用于 scripted deterministic ModelResponses。

例如：

```text
Turn 1 → read_file
Turn 2 → edit_file
Turn 3 → shell(test)
Turn 4 → Final
```

测试可以断言：

* ModelRequest内容；
* Tool execution顺序；
* ToolResult correspondence；
* message ordering；
* Runtime state；
* batch semantics；
* budgets；
* final state。

FakeModelClient不模拟“模型聪明程度”。

它用于：

> deterministically drive the Runtime through defined protocol paths.

#### 20.2 Concrete ModelClient Contract Tests

Concrete `OpenAICompatibleModelClient` 必须具有 required deterministic contract tests。它们默认不访问真实网络，可使用 fake SDK client、stub response、monkeypatch 或 controlled provider-shaped objects。

至少覆盖：

* `ModelRequest` serialization；
* `ModelRequest.tools` serialization；
* System / User / Assistant / ToolResult message serialization；
* native Tool schema serialization；
* provider response → `ModelResponse` normalization；
* response text normalization；
* ToolCall normalization；
* ToolCall `call_id` preservation / synthesis according to 05；
* usage normalization where the provider exposes usage；
* transient provider error normalization；
* fatal provider error normalization；
* malformed / unsupported provider response handling。

> Concrete ModelClient contract tests are required v1 regression evidence and do not require a live API.

Live provider smoke 不能替代这些 deterministic contract tests。

---

### 21. Prefer Observable Contract Assertions

测试优先断言：

* observable ModelRequest；
* ToolResult；
* Run state；
* actual filesystem effect；
* permission effect；
* call ordering；
* no-side-effect guarantees。

避免无必要耦合：

```text
private helper names
private collection layout
temporary implementation counters
```

除非某项本身就是正式 architecture contract。

原则：

> Prefer contract-level assertions over private implementation coupling.

---

### 22. Pure Unit Tests

适合直接 unit test 的逻辑包括：

* path containment helper；
* task-constraint normalization；
* Shell surface classification；
* context-size estimation；
* ToolResult projection；
* Shell head/tail truncation；
* other deterministic helpers。

这些测试应：

* 快；
* deterministic；
* 尽量无网络；
* 不依赖真实模型。

---

### 23. Tool Contract Tests

每个 Local Tool应直接测试其 06 contract。

典型覆盖：

#### File Tools

* valid read；
* paging；
* binary rejection；
* outside-workspace resolution；
* exact replacement；
* expected_count mismatch；
* stale edit；
* exclusive create；
* line-ending preservation；
* sensitive/protected facts。

#### Search Tools

* path scope；
* literal / regex；
* result bounds；
* sensitive exclusion；
* deterministic ordering where promised。

#### Shell

* exit 0；
* non-zero exit；
* stdout / stderr；
* timeout；
* cwd；
* bounded capture；
* noninteractive behavior。

Tool tests不依赖真实模型。

---

### 24. Deterministic Runtime Integration Tests

Runtime核心行为使用：

```text
FakeModelClient
+
real or controlled Tools
+
temporary Workspace
```

执行完整 Agent loop。

至少覆盖：

#### Direct Final

```text
User
→ Final
→ COMPLETED
```

包括：

* nonblank final；
* blank/no-tool response → protocol error。

#### Single Tool Vertical Slice

```text
User
→ ToolCall
→ ToolResult
→ Final
```

#### Sequential Multi-Tool Batch

* model order preserved；
* each result correspondence correct。

#### Fail-Stop

例如：

```text
A → SUCCESS
B → OPERATION_FAILURE
C → NOT_EXECUTED
```

验证 C 没有真实 side effect。

#### Validation Error

无效 Tool arguments：

```text
→ VALIDATION_ERROR
→ no execution
```

#### Policy Rejection

```text
→ POLICY_REJECTED
→ no execution
```

#### Terminal Interruption Recovery

Deterministic Runtime integration evidence 至少覆盖：

```text
ToolCall accepted
→ local Tool execution begins / is pending
→ execution is interrupted by cancellation or unexpected Runtime failure
→ current Run reaches the terminal state required by 04
→ incomplete current-run ToolCall / ToolResult correspondence is repaired
  or safely discarded
→ the same Session can start a subsequent Run normally
```

核心 contract：

> A terminal interruption in one Run must not leave protocol or Context state that poisons the Session or prevents a later Run from starting normally.

测试必须断言：

* interrupted Run 按 04 的既有 terminal semantics 正确结束；
* 不产生虚假的 successful ToolResult；
* 不留下会破坏下一 Run message / Context ordering 的 incomplete correspondence；
* 同一 Session 的后续 Run 可以正常启动；
* 新 Run 可以正常继续到 ModelRequest / Final 或 Tool loop；
* `FAILED` / `CANCELLED` Run 的 continuity 仍遵守 §31。

04 继续 owning cancellation 与 unexpected Runtime failure 的具体 `FAILED` / `CANCELLED` mapping。08 只要求无论 04 指定哪种 terminal semantics，cleanup 后 Session 都保持可复用，并仅断言 observable contract。

---

### 25. Confirmation Tests

`FakeUserInteraction` 是 confirmation 与 clarification lifecycle 的 deterministic test seam；它用于提供 scripted approve、reject、cancel 与 answer，不模拟真实终端 UI。

至少覆盖：

#### Approve

```text
CONFIRM
→ user approves
→ exact stored action executes once
```

#### Reject

```text
CONFIRM
→ user rejects
→ no execution
→ POLICY_REJECTED
```

测试必须保证：

> confirmation applies only to the exact stored action.

不能因为用户确认一次而扩大 permission范围。

---

### 26. ask_user Tests

至少覆盖：

```text
model calls ask_user
→ WAITING_FOR_USER
→ user answer
→ SUCCESS ToolResult
→ next Model Turn
```

断言：

* answer作为 ToolResult返回；
* 不额外复制成 `UserMessage`；
* remaining batch按 04 rules处理；
* cancellation正确结束 Run。

---

### 27. Protocol Recovery Tests

对于 response-level invalid model response，例如：

```text
tool_calls = []
text = ""
```

测试：

```text
ModelProtocolError
→ invalid response not stored as valid AssistantMessage
→ corrective re-prompt
→ new Model Turn
```

并断言：

* corrective instruction属于 Effective System Prefix；
* Model Turn accounting正确；
* no Tool side effects from malformed response。

---

### 28. Transport Retry Tests

Transient provider failure：

```text
ModelRequest A
→ transient failure
→ retry
→ response
```

测试应证明：

* retry使用 same logical ModelRequest snapshot；
* normal conversation history未改变；
* 没有 protocol corrective instruction；
* transport retry 与 corrective re-prompt保持语义区分。

不需要真实网络故障。

---

### 29. Budget Tests

至少覆盖：

* max model turns；
* max tool-call attempts；
* active-duration budget。

前两项应完全 deterministic。

active-duration测试不应依赖长时间 `sleep`。

如果需要 test seam，应保持 Lean，例如小型 injected monotonic clock callable，而不是建立复杂 timing framework。

预算耗尽后应验证：

```text
→ terminal Run state
→ no next Model Turn
```

---

## Context and Projection Testing

### 30. Base System Prompt Tests

每个正常 ModelRequest必须：

* 包含 Stable Base System Prompt；
* Effective System Prefix位于 message sequence最前；
* Base Prompt不作为普通 history item被 compact / evict。

---

### 31. Session Continuity Tests

完成 Run 1：

```text
task
tool history
final
```

启动 Run 2后应验证：

保留：

```text
Run 1 task
Run 1 final
```

不保留：

```text
Run 1 ToolCalls
Run 1 ToolResults
old Shell output
old read content
```

FAILED / CANCELLED Run默认不建立 continuity record。

---

### 32. Atomic Eviction Tests

在小 Context budget下验证：

```text
Assistant ToolCall
+
corresponding ToolResult
```

永远一起 retained 或 evicted。

不得出现：

```text
orphan ToolCall
orphan ToolResult
```

Multi-tool grouped batch保持 05 定义的 grouped representation。

---

### 33. Eviction Priority Tests

构造：

```text
old completed-run continuity
old current-run Tool Unit
latest current-run Tool Unit
```

验证：

1. oldest completed-run continuity先淘汰；
2. 然后 oldest removable current-run unit；
3. latest protected observation不因普通 eviction删除。

---

### 34. history_incomplete Tests

至少覆盖：

```text
new Run
→ history_incomplete = false
```

发生一次 destructive eviction：

```text
→ true
```

之后即使下一次 build没有发生新的 eviction：

```text
→ still true
→ truncation notice still present
```

Run结束并开始新的 Run：

```text
→ false
```

该 state由 07 指定的 ContextManager owner管理。

---

### 35. Mandatory-Context Overflow Tests

构造极小 budget，使所有 removable units删除后：

```text
Base Prompt
+
Current User Task
+
protected mandatory content
```

仍无法 fit。

验证：

```text
ContextManager reports unrecoverable context-construction failure
→ AgentRuntime → FAILED
→ no next Model Turn
```

实现可以使用内部 exception type，例如 `ContextLimitError`，作为 ContextManager 向 AgentRuntime 报告该 condition 的内部机制。这不扩展 05 定义的 public / protocol-level taxonomy；`ContextLimitError` 不是 `ToolOutcome`、`ToolError` code、`ModelProtocolError` subtype contract、provider error taxonomy 或新的 public lifecycle result。

Architecture-level semantics 保持为：

```text
mandatory context cannot fit
→ unrecoverable context-construction failure
→ existing terminal Runtime failure path
→ Run FAILED according to 04
→ no next Model Turn
```

---

### 36. ToolResult Projection Tests

Projection tests应验证：

* outcome不改变；
* call_id correspondence不改变；
* necessary semantic information保留；
* redundant metadata可以移除；
* truncation显式。

`read_file` bounded content保持 faithful。

`edit_file` / `create_file`成功结果不重复完整 content。

`NOT_EXECUTED`保持简洁且不伪造 execution failure。

---

### 37. Shell Projection Tests

至少覆盖：

#### Short Output

```text
short stdout/stderr
→ preserved
→ not marked truncated
```

#### Long Output

```text
long stream
→ head retained
→ omission marker
→ tail retained
→ truncated=true
```

#### Separate Streams

stdout / stderr保持独立。

#### Outcome

projection不得改变：

```text
SUCCESS
UNSUCCESSFUL_COMMAND
OPERATION_FAILURE
```

语义。

---

### 38. Safety and Policy Tests

03 仍是 safety normative rules 的唯一 canonical owner；08 不复制或重定义其 risk matrix。Every v1 normative safety invariant owned by 03 must have traceable verification evidence.

#### 38.1 Explicit Task Constraints

Evidence 至少覆盖：

* `FORBID_FILE_MUTATION`；
* `FORBID_COMMAND_EXECUTION`；
* `WRITE_SCOPE`；
* constraint rejection 不产生 forbidden side effect。

#### 38.2 Workspace Containment

Evidence 至少覆盖：

* valid inside existing path；
* outside path；
* `..` escape；
* absolute outside path；
* new target resolution；
* nearest-existing-parent behavior；
* symlink escape；
* Windows junction equivalent where platform / test support permits。

#### 38.3 Protected / Sensitive Path

Evidence 至少覆盖 `.git/**` protected path behavior、Sensitive Path classification、sensitive discovery exclusion，以及按 03 执行的 access / confirmation semantics。08 不重新定义具体 sensitive patterns。

#### 38.4 Runtime Secret Isolation

Evidence 至少覆盖：

* Runtime Secret 不进入 model-visible Context；
* Runtime Secret 不进入 ToolResult；
* Shell environment filtering 默认不把 provider / API credential 传给 child process；
* implemented known-secret redaction / filtering behavior。

Runtime Secret 与 Workspace Sensitive Data 保持 03 定义的区别。

#### 38.5 Shell Execution Bounds

Evidence 至少覆盖 timeout、bounded stdout、bounded stderr、noninteractive stdin，以及 testable 范围内的 process termination / best-effort process-tree cleanup。

#### 38.6 Shell Risk Classification

Evidence 至少覆盖 recognized risky action、dependency install、network、Git mutation、Git remote write、privileged / system action、compound command 的 highest recognizable risk、03 规则下的 unknown simple executable、ambiguous complex / side-effectful command 的 conservative `CONFIRM`，以及 03 要求为 prohibited 的类别映射到 `DENY`。

08 只要求验证 03 的既有规则，不在这里重新定义 risk matrix。

#### 38.7 Permission No-Side-Effect Guarantees

Evidence 至少覆盖：

* `ALLOW`：allowed action executes；
* `CONFIRM`：approval 前不执行，rejection 不产生 side effect，approval 只执行 exact stored action，且不扩大后续 permission；
* `DENY`：no execution、no side effect。

Safety tests 应优先使用 fake executor、sentinel 或 temporary workspace，并断言：

```text
policy decision
+
executor was / was not invoked
+
side effect did / did not occur
```

不得为了测试真实执行 `git push`、`sudo` 或 system modification 等危险动作。

---

### 39. Temporary Workspace Usage

Filesystem-related测试适合使用真实 temporary workspace。

例如：

* create/edit/read；
* symlink；
* path resolution；
* search；
* simple safe subprocess。

测试不得依赖开发者机器固定 absolute path。

---

### 40. Provider Evidence Hierarchy

Provider evidence 分为三个不同层次，不得互相替代。

#### 40.1 Layer 1 — Deterministic Concrete ModelClient Contract Tests

状态：`REQUIRED for v1 regression`。

该层不使用 live network 或 API key，必须 deterministic，覆盖 serialization、normalization 与 provider error contract。具体范围见 §20.2。

#### 40.2 Layer 2 — Live Provider Smoke Test

状态：`OPTIONAL in ordinary CI`。

用于验证真实 provider transport / API compatibility、native Tool Calling integration 和 live response normalization：

```text
real ModelClient
→ native tool call
→ local Tool
→ ToolResult
→ Final
```

该层需要显式 API configuration，可以因 network / provider unavailable 而 skip，且不作为 Runtime correctness 的唯一 evidence。

#### 40.3 Layer 3 — Pre-Submission Real-Model Acceptance

状态：`REQUIRED before final delivery`。

该层必须走：

```text
real model
+
real ModelClient
+
real AgentRuntime
+
real local Tools
+
real local workspace
```

正式 Demo 必须使用同一 real Agent path。普通 CI 中 live smoke 可选，不表示最终 real-model acceptance 可省略。

---

### 41. End-to-End Agent Acceptance

E2E分两种。

#### 41.1 Deterministic E2E

```text
FakeModelClient
+
real temporary workspace
+
real Local Tools
+
real AgentRuntime
```

用于 regression。

#### 41.2 Real-Model Acceptance

真实模型：

```text
real user-style coding task
+
real local workspace
+
real Runtime / Tools
```

用于：

* manual acceptance；
* provider smoke；
* Demo candidate evaluation。

不要求每次 CI运行真实 coding task。

---

### 42. What Automated Tests Do Not Claim

自动化 contract tests不证明：

> 某个真实 LLM 一定足够聪明，可以解决任意 coding task。

它们证明：

* Runtime提供正确 observations；
* Tool行为正确；
* constraints和permissions正确；
* context正确；
* state transition正确；
* failure/retry semantics正确。

真实模型的整体 coding performance：

> 通过 manual / end-to-end acceptance 和 Demo评估。

---

## Demo Policy

### 43. Demo Purpose

最终 Demo 的目标不是展示某个特定算法题，而是证明：

> This is a functioning local Coding Agent with an LLM-directed iterative tool loop.

Demo应让评审清楚看到：

```text
User Task
↓
Model chooses action
↓
Runtime validates / executes local Tool
↓
Workspace observation
↓
Model chooses next action
↓
Relevant mutation
↓
Verification evidence
↓
Honest Final
```

---

### 44. Demo Uses the Real Agent Path

正式 Demo必须使用：

* real model；
* real AgentRuntime；
* real registered Tools；
* real local filesystem；
* real Shell where used。

FakeModelClient只用于 automated testing，不作为最终 capability Demo。

---

### 45. Demo Task Selection Criteria

08 不固定具体 Demo task。

最终 task在 M4根据成熟 Agent能力选择。

候选任务应优先满足：

* software-engineering task真实且容易理解；
* scope bounded；
* 能在视频时间内稳定完成；
* 修改结果容易验证；
* 不依赖复杂网络服务；
* 不要求危险操作；
* 不要求安装大量 dependency；
* verification速度快；
* 能体现至少一次真实 Tool-driven iteration；
* Final claim有清楚 evidence。

---

### 46. Preferred Demo Shape

优先选择：

> small but non-trivial bug fix or feature task with clear verification.

例如可具有：

```text
existing failure
→ inspect
→ modify
→ targeted verification
→ pass
```

但08不规定：

```text
必须先 test
必须 search
必须某个具体 Tool sequence
```

因为 workflow仍然由模型动态决定。

---

### 47. Demo Acceptance Is Outcome-Oriented

Demo不以固定 Tool顺序作为 acceptance。

例如以下都可能合法：

```text
read → edit → test
```

或：

```text
search → read → edit → test
```

或其他合理动态路径。

Acceptance主要看：

1. Agent确实根据 observation迭代；
2. intended workspace change实际发生；
3. verification evidence与 Final claim匹配；
4. Runtime没有被外部 Agent framework替代；
5. 整个过程使用本地 Tool execution。

---

### 48. Demo Should Surface Meaningful Observability

视频中应优先展示：

* Tool名称；
* relevant path / command；
* concise result；
* verification result；
* Final。

不需要大量展示：

* 内部 Python object；
* 完整 Tool JSON；
* 长 old_text/new_text；
* verbose internal reasoning；
* 全部 Runtime state。

具体 CLI rendering由09 owning。

08只要求：

> Demo should make the agent's actions, observations, mutation, and verification evidence understandable within the submission time limit.

---

### 49. Demo Does Not Need to Exercise Every Feature

主 Demo不要求强行展示：

* permission confirmation；
* ask_user；
* Context truncation；
* budget exhaustion；
* policy denial；
* transport retry；
* protocol recovery。

这些复杂 contract应由 automated tests / interview explanation证明。

Demo优先展示：

> stable, representative coding success.

---

### 50. Demo Task Is Deferred

08 只 freeze：

* Demo capability requirements；
* task-selection criteria；
* evidence requirements；
* outcome-oriented acceptance。

以下内容留到 M4：

```text
exact demo repository
exact bug / feature
exact model
exact prompt
exact verification command
video script
screen layout
recording workflow
backup demo candidate
```

这样后续 Agent能力提升不会被过早选择的 fixture限制。

---

## Implementation and Acceptance

### 51. Minimum v1 Testing Evidence

最终 v1 至少应具有以下 testing / acceptance evidence：

| Area           | Required evidence                                  |
| -------------- | -------------------------------------------------- |
| Core protocol  | schema / value-object tests                        |
| Workspace      | resolver and containment tests                     |
| Tools          | direct Tool contract tests                         |
| Runtime loop   | FakeModelClient integration + terminal interruption cleanup tests |
| Batch          | sequential execution + fail-stop                   |
| Policy         | ALLOW / CONFIRM / DENY path tests                  |
| Interaction    | ask_user + confirmation tests                      |
| Model recovery | protocol corrective + transport retry              |
| Budgets        | deterministic exhaustion tests                     |
| Context        | retention / atomic eviction / `history_incomplete` |
| Projection     | per-tool projection + Shell head/tail              |
| Session        | task+final continuity + subsequent-Run recoverability |
| E2E            | deterministic full Agent workflow                  |
| Concrete ModelClient | required deterministic contract tests       |
| Live Provider  | ordinary CI smoke optional; pre-submission real-model acceptance required |

08不要求每一行对应一个单独 test file。

#### 51.1 Requirement and Evidence Traceability

最终每项 v1 normative requirement 至少必须能够追踪为：

```text
Requirement
→ Implementation location
→ Evidence
```

Evidence 可以是 automated test、deterministic integration test、manual acceptance、real-model acceptance、Demo evidence、Accepted Limitation，或仅针对明确不属于 v1 scope 的能力使用 Deferred。

Freeze / submission 前，每项 v1 normative requirement 必须属于以下一种：

1. Implemented + Automated Evidence；
2. Implemented + Manual / Acceptance Evidence；
3. Accepted Limitation explicitly documented and compatible with v1 claims；
4. Deferred only because the capability is explicitly outside v1 scope。

An unresolved v1 normative requirement may not be marked Deferred merely to satisfy traceability. 已属于 v1 的 normative contract 如果最终没有实现，必须实现它，或在提交前修改对应 canonical design，正式将其移出或降级为非 v1 requirement。

禁止：

* unimplemented v1 requirement silently ignored；
* unimplemented v1 requirement falsely marked Deferred；
* requirement 只有文档、没有 implementation / evidence owner。

Traceability 使用简单 Markdown checklist / mapping 即可，不建立 requirements database、spreadsheet subsystem 或复杂 matrix framework。

---

### 52. 07-Conformance Verification

由于07 freeze时明确存在 implementation obligations，后续 conformance work至少应通过测试证明：

* Stable Base System Prompt；
* Effective System Prefix ordering；
* Protocol Corrective Instruction placement；
* Semantic Relevance prompt guidance；
* `history_incomplete` lifecycle；
* persistent request-local truncation notice；
* ToolResult projection；
* RepeatedActionWarning presentation；
* Shell head/tail projection；
* mandatory-context overflow → terminal Runtime failure；
* no next Model Turn after terminal failure。

08 owns这些行为的测试/evidence要求，不重新定义07的语义。

---

### 53. Test Independence

默认 automated test suite应：

* 不要求真实 API Key；
* 不要求公网；
* 不要求外部 database/service；
* 不执行危险 remote side effects；
* 可重复；
* 在普通开发环境中快速运行。

需要 real-provider configuration 的 ordinary-CI test 应明确标为 optional / smoke；pre-submission real-model acceptance 仍是最终交付前的 required evidence。

---

### 54. Verification and Testing Invariants

1. Verification不是 Runtime mandatory phase。
2. 模型决定 verification是否需要以及使用什么 mechanism。
3. Runtime不维护通用 `verification_success` state。
4. Code mutation在 practical 时应进行 relevant verification。
5. 优先使用 repository-supported verification。
6. 默认优先 smallest meaningful targeted verification。
7. change scope / risk较大时可以扩大 verification。
8. verification failure是 observation，不自动导致 Run FAILED。
9. valid Final不要求所有 verification成功。
10. incomplete verification必须诚实报告。
11. verification action不获得 permission exemption。
12. Completion claim不得强于 observed evidence。
13. Evidence只支持实际覆盖的 scope。
14. Evidence绑定于被验证的 Workspace State。
15. later relevant mutation可能使已有 evidence stale。
16. failed verification本身也是 diagnostic evidence。
17. pre-existing failure claim需要 baseline evidence。
18. command exit 0不自动等于 task verified。
19. Runtime记录 execution truth；模型判断 semantic verification sufficiency。
20. Agent核心 correctness主要通过 deterministic tests验证。
21. FakeModelClient是 Runtime orchestration的主要 test seam。
22. automated tests优先断言 observable contract。
23. Tool contract tests不依赖真实模型。
24. policy tests不要求执行真实危险动作。
25. Concrete ModelClient contract tests是required deterministic regression evidence；ordinary CI中的live-provider smoke可选，pre-submission real-model acceptance必需。
26. deterministic E2E使用 FakeModelClient + real Runtime / Tools / temp workspace。
27. real-model coding能力由 manual/E2E/Demo评估。
28. Demo必须走真实 Agent execution path。
29. Demo acceptance是 outcome/evidence-oriented，不要求固定 Tool sequence。
30. 具体 Demo task留到 M4选择。
31. `COMPLETED`只表示Runtime接受合法非空Final并正常结束Run，不表示task fully succeeded或fully verified。
32. v1不新增VERIFIED、PARTIAL、BLOCKED等Runtime lifecycle state；task outcome quality通过诚实Final表达。
33. Startup关键invariant不满足时必须fail closed，并有deterministic evidence。
34. Small Python projects是v1主要systematic E2E / acceptance环境，但architecture保持language-neutral。
35. 03拥有的每项v1 normative safety invariant必须具有traceable verification evidence。
36. 每项v1 normative requirement必须可追踪到implementation location与evidence；不得以虚假Deferred掩盖未实现的v1 contract。
37. A terminal interruption while Tool execution is pending must not leave Context or protocol state that prevents a subsequent Run in the same Session from starting normally.

---

### 55. Implementation Boundary

08 不要求新增：

```text
VerificationManager
EvidenceManager
EvidenceGraph
ClaimValidator
TaskTypeClassifier
TestCommandDetector
DemoRuntime
LLM Judge
```

verification仍然主要由：

```text
Base Prompt / semantic guidance
+
existing Tool surface
+
AgentRuntime iterative loop
+
truthful ToolResults
```

实现。

Agent testing使用现有：

```text
FakeModelClient
Tool interfaces
Runtime
temporary workspace
pytest
```

即可。

---

### 56. Deferred to 09

09负责：

* test / Tool output在 CLI 中如何显示；
* Shell output display defaults；
* logging；
* debug mode；
* Context truncation的human-visible提示；
* configuration defaults；
* optional real-provider configuration；
* Demo-friendly observability presentation。

08只规定应展示什么 evidence，不规定具体颜色、格式或 CLI layout。

---

### 57. Deferred to M4

Submission Polish阶段再决定：

* final Demo task；
* demo fixture / repository；
* primary / backup candidate；
* exact model；
* exact CLI settings；
* video timing；
* implementation explanation script；
* final evidence shown in video。

Demo选择应基于届时真实 Agent能力和稳定性，而不是反向限制当前 Agent设计。

---

### 58. ADR Candidates

#### Verification Is Model-Directed

Decision：

Verification selection and sufficiency are model-dependent coding judgments，而不是 mandatory Runtime phase。

Canonical owner：

```text
08-verification-testing-and-demo.md
```

#### Evidence-Bounded Completion Claims

Decision：

Final completion claims不得超过实际 observation的 scope、strength 和 freshness。

Canonical owner：

```text
08-verification-testing-and-demo.md
```

#### Deterministic Agent Testing

Decision：

Runtime correctness主要通过 FakeModelClient驱动的 deterministic contract / integration tests验证；真实 provider只作为 optional smoke / manual acceptance。

Canonical owner：

```text
08-verification-testing-and-demo.md
```

#### Outcome-Oriented Demo Acceptance

Decision：

Demo根据真实 workspace outcome和 verification evidence验收，不要求固定 Tool sequence；具体 task留到 M4选择。

Canonical owner：

```text
08-verification-testing-and-demo.md
```
