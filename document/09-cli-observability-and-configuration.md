# 09 CLI, Observability, and Configuration

## 1. Purpose

本文定义 v1 Coding Agent 的：

- configuration sources、precedence 与 validation；
- Runtime Secret 的配置入口；
- workspace / provider / budget 等 operational configuration；
- concrete v1 defaults；
- Normal CLI human-facing observability；
- Debug observability；
- interaction / permission presentation；
- interactive Session control；
- startup / exit behavior。

本文主要回答：

- 用户如何配置 model、Base URL、workspace 和 budgets；
- 哪些 configuration 可以由用户覆盖；
- 哪些 architecture / safety contract 不允许配置关闭；
- Normal CLI 应显示什么；
- Debug mode 额外显示什么；
- model-visible Context 与 human-visible CLI output 如何区分；
- Shell、read、search、edit 等 Tool 在 CLI 中如何呈现；
- Context truncation、provider retry、protocol correction 等内部事件是否对普通用户显示；
- 一个 interactive Session 中如何开始、结束和取消 Run；
- 07 委托给 09 的 concrete context / projection defaults 是什么。

本文不重新定义：

- safety / permission / Runtime Secret policy：由 `03-safety-and-execution-boundaries.md` 负责；
- Run / Session lifecycle、budget semantics、retry、termination：由 `04-agent-runtime-model.md` 负责；
- protocol types / ModelClient contracts：由 `05-component-and-protocol-contracts.md` 负责；
- Tool semantics、Shell execution/capture mechanism：由 `06-toolset-and-file-editing.md` 负责；
- model-visible Context / Prompt / ToolResult projection semantics：由 `07-context-and-prompt-policy.md` 负责；
- verification / evidence / testing / Demo acceptance：由 `08-verification-testing-and-demo.md` 负责。

核心原则：

> Configuration exposes operational knobs, not architecture switches.

以及：

> Human-facing observability and model-visible Context are different output surfaces.

---

## 2. Configuration Boundary

v1 configuration 只控制 operational parameters，例如：

- workspace；
- model；
- Base URL；
- Runtime Secret source；
- Run budgets；
- context budget；
- Debug mode。

configuration 不允许关闭或修改 normative architecture contracts，例如：

```text
workspace containment
ALLOW / CONFIRM / DENY semantics
Tool validation
batch fail-stop
Runtime state machine
ToolCall / ToolResult correspondence
mandatory Context protection
Secret filtering
```

不得提供类似：

```text
--disable-safety
--disable-fail-stop
--skip-tool-validation
--verification-required
--ignore-workspace-boundary
```

的 configuration。

因此：

> Normative safety, protocol, and lifecycle behavior is not user-configurable.

---

## 3. Configuration Sources and Precedence

v1 使用三层 configuration：

```text
built-in defaults
        ↓
environment variables
        ↓
CLI arguments
```

优先级：

```text
CLI
>
environment
>
built-in defaults
```

只有显式支持的 configuration key 参与该 precedence。

v1 不实现通用：

```text
config.yaml
config.toml
project-local agent config
user-home profile
configuration profile inheritance
```

因此：

> v1 has no general-purpose configuration file.

---

## 4. Runtime Secret Configuration

Runtime/provider credential 属于 03 定义的 Runtime Secret。

v1 使用环境变量：

```text
CODING_AGENT_API_KEY
```

提供 API credential。

不得提供普通 CLI 参数：

```text
--api-key <secret>
```

原因包括：

* shell history；
* process argument visibility；
* terminal recording；
* debug output；
* accidental copy/paste。

CLI / Debug output不得显示 secret value。

可以显示：

```text
API credential: configured
```

但不得显示：

```text
API credential: sk-...
```

---

## 5. Provider Configuration

v1 default concrete model client 是：

```text
OpenAICompatibleModelClient
```

它是默认 provider implementation；v1 不建立 provider registry 或 provider profile system。

该 ModelClient 使用：

```text
CODING_AGENT_API_KEY
CODING_AGENT_MODEL
CODING_AGENT_BASE_URL
```

对应 configuration：

* `api_key`：Runtime Secret；
* `model`：模型标识；
* `base_url`：OpenAI-compatible endpoint。

v1 没有 built-in default model。`model` 必须通过：

```text
CODING_AGENT_MODEL
```

或：

```text
--model
```

提供；最终 effective model 为空时 startup failure，不静默选择模型。

v1 没有 built-in default Base URL。`base_url` 必须通过：

```text
CODING_AGENT_BASE_URL
```

或：

```text
--base-url
```

提供；最终 effective Base URL 缺失时 startup failure，不静默采用 OpenAI endpoint 或其他外部 endpoint。

API Key required，且仅通过 `CODING_AGENT_API_KEY` 环境变量提供；不支持普通 `--api-key`，其值不得出现在 Normal 或 Debug output。

v1 不建立 provider profile system。

---

## 6. Workspace Configuration

CLI 支持：

```text
coding-agent --workspace <PATH> [other options]
```

`--workspace <PATH>` 必填；workspace 必须由用户显式指定，不存在任何 implicit workspace fallback。

在 Session 启动前必须通过 06 的 workspace binding / canonicalization 建立合法 workspace root。

invalid 或 unavailable workspace 导致 startup failure，不进入 Session。

Normal startup display 应明确展示 canonical bound workspace，例如：

```text
Workspace: D:\projects\example
```

用户看到的 workspace path 只用于 human observability，不改变 03 / 06 的 containment semantics。

---

## 7. User-Overridable Operational Budgets

v1 允许用户覆盖：

```text
max_model_turns
max_tool_call_attempts
max_active_run_duration
max_context_chars
```

建议 CLI options：

```text
--max-turns
--max-tool-calls
--max-duration
--max-context-chars
```

对应 environment override 可以使用：

```text
CODING_AGENT_MAX_MODEL_TURNS
CODING_AGENT_MAX_TOOL_CALL_ATTEMPTS
CODING_AGENT_MAX_ACTIVE_RUN_SECONDS
CODING_AGENT_MAX_CONTEXT_CHARS
```

CLI values 优先于 environment values。

Public configuration may widen a budget only within a finite validated range：

| Setting | Default | Allowed public range |
| --- | ---: | ---: |
| `max_model_turns` | 24 | 1..64 |
| `max_tool_call_attempts` | 64 | 1..256 |
| `max_active_run_duration` | 900 seconds | 1..3600 seconds |
| `max_context_chars` | 80,000 | 8,000..256,000 |

这些 ranges 同时适用于 CLI 与 environment override。超出范围属于 startup configuration validation failure。

Finite upper bound 是 architecture hard-bounded behavior 的一部分；用户不能通过超大 configuration effectively disable hard budget。Lower-level tests 可以通过专用 test seam 构造更小值，不要求 public CLI 接受超出上述 range 的测试配置。

---

## 8. Internal Defaults Need Not Be Public CLI Knobs

09 owning concrete default 不意味着所有 default 都必须成为 CLI option。

以下参数默认保持 implementation-level stable configuration：

```text
retained completed Run count
read_file size limits
search result limits
Shell capture limits
Shell projection limits
Shell head/tail split
provider retry backoff
Normal CLI Shell display cap
```

v1 不提供大量 tuning flags，例如：

```text
--shell-head-chars
--shell-tail-chars
--search-max-results
--read-max-lines
--retry-backoff
```

原则：

> Expose knobs that materially control a Run; keep internal projection and resource tuning stable.

---

## 9. Configuration Validation

Configuration 必须在 Session startup 前 deterministic validation。

至少验证：

* required provider configuration；
* non-empty model；
* required、non-empty、valid Base URL representation；
* valid workspace；
* Run budgets within their finite public ranges；
* context budget within its finite public range；
* valid timeout ranges。

关键 configuration 无效时：

```text
startup fails
```

不得启动一个 partially valid Session 后等待首次 Model Call 才发现配置错误。

具体 startup evidence 由 08 testing policy负责。

---

## Concrete v1 Defaults

### 10. Run Budget Defaults

v1 defaults：

```text
max_model_turns = 24

max_tool_call_attempts = 64

max_active_run_duration = 900 seconds
```

`WAITING_FOR_USER` 是否计入 active duration 继续遵守 04。

这些值用于：

* 支持正常 multi-step debugging；
* 支持合理 multi-tool batching；
* 防止 runaway Agent loop。

它们是 conservative operational defaults，不是 architecture constants。

---

### 11. Shell Timeout Defaults

06 的 Shell Tool 默认：

```text
default timeout = 120 seconds
```

模型可以在 ToolCall 中显式提出更高 timeout，但 v1 单条 Shell execution 的 configurable maximum：

```text
maximum timeout = 300 seconds
```

有效范围：

```text
1 <= timeout_seconds <= 300
```

Shell timeout semantics 和 process termination behavior仍由 06 owning。

#### 11.1 Platform Shell Backend

v1 按 platform 选择 Shell backend：

```text
Windows: COMSPEC
POSIX:   /bin/sh
```

v1 不提供 public `CODING_AGENT_SHELL` override，也不建立 shell profile / config system。测试实现所需的 injection seam 不属于 public configuration contract。具体 Shell execution semantics 仍由 06 owning。

---

### 12. Provider and Interaction Defaults

#### 12.1 Model Request Timeout

v1 default：

```text
model_request_timeout_seconds = 60
```

这是每次 provider transport attempt 的 timeout，不等于 whole Run active duration 或 Shell timeout。Transport Retry 继续遵守 04 / 05。

默认不提供 public CLI override。

#### 12.2 Provider Transport Retry

04 / 05 定义 transport retry 与 corrective re-prompt 的语义区别。

v1 transient transport retry default：

```text
initial request
+
maximum 2 automatic retries
```

即最多：

```text
3 transport attempts for one logical ModelRequest
```

retry delay采用简单 bounded exponential backoff：

```text
retry 1: 0.5 seconds
retry 2: 1.0 seconds
```

backoff cap：

```text
2.0 seconds
```

v1 不增加 jitter。

这些值默认不作为 public CLI option。

Transport Retry始终重用同一 logical ModelRequest snapshot。

#### 12.3 Protocol Corrective Limit

v1 default：

```text
max_consecutive_protocol_errors = 3
```

每次 response-level `ModelProtocolError` 使 consecutive counter 加一；获得合法 `ModelResponse` 时 counter 重置为 `0`。连续达到 3 次后进入 04 定义的 terminal failure，不再构造第 4 个 corrective `ModelRequest`。

04 owns corrective re-prompt semantics；09 只 owns concrete limit。默认不提供 public CLI option。

#### 12.4 User Wait Timeout

v1 default：

```text
user_wait_timeout = None
```

`ask_user` 等待用户回答，permission confirmation 等待用户决定；没有 automatic wall-clock timeout。用户通过 answer、cancel 或相关 EOF / interruption 结束等待。`WAITING_FOR_USER` 的 active-duration accounting 继续遵守 04，不新增 user-wait timer subsystem。

---

### 13. Model-Visible Working-Context Budget

07 使用 provider-neutral approximate context-size estimation。

v1 default：

```text
max_context_chars = 80_000
```

该值表示：

> approximate model-visible working-context budget

而不是：

> provider advertised context window.

Runtime working-context budget与 provider真正的 token/context capacity 是不同概念。

Runtime不依赖 provider-specific tokenizer进行精确 accounting。

---

### 14. Cross-Run Continuity Default

07 规定只保留 bounded recent COMPLETED Run continuity。

v1 default：

```text
max_retained_completed_runs = 1
```

每个 retained Run只包含：

```text
Initial User Task
+
Final Assistant Response
```

该 default 不作为普通 CLI option。

---

### 15. read_file Defaults

`read_file` 使用 06 要求的三个 bounded dimensions：

```text
default page size = 200 lines
absolute maximum requested line range = 400 lines
maximum returned content = 20_000 bytes
```

Default page size 是普通未指定更大范围时的读取粒度；即使显式请求，单次 line range 也不得超过 400 lines；returned-content byte limit 防止超长单行、minified 或 generated content。

命中任一 limit 时仍保持 bounded，并按照 06 / 07 contract提供：

```text
truncated
continuation information
```

该限制避免：

* 单次读取超大文件；
* minified / generated 超长单行内容；
* 一个 read observation占据过多 working Context。

UTF-8 boundary 与 byte truncation implementation 不由 09 重新定义；09 只 owns 上述 concrete default values。

---

### 16. Discovery Defaults

v1 defaults：

```text
list_directory:
    maximum direct entries = 200

search_files:
    maximum matching paths = 200

search_text:
    maximum matches = 100
```

结果超限时必须显式：

```text
truncated = true
```

模型应通过缩小 path / query / glob继续搜索，而不是要求 unbounded discovery。

---

### 17. search_text Content Default

除 match count 外，`search_text` model-visible projected matched content还使用：

```text
maximum projected content = 16_000 characters
```

达到 match-count 或 content-size任一边界即可截断。

07 owning projection semantics；09只 owning concrete default。

---

### 18. Shell Resource Capture Default

06 resource-level Shell capture default：

```text
stdout retained capacity = 64 KiB
stderr retained capacity = 64 KiB
```

每个 stream独立 bounded。

capture mechanism必须保留足够的前后信息，使07能够生成：

```text
head
+
omission marker
+
tail
```

的 model-visible projection。

09不重新定义具体 subprocess capture algorithm。

---

### 19. Model-Visible Shell Projection Defaults

07 Shell model-visible projection default：

```text
stdout visible limit = 8_000 characters
stderr visible limit = 8_000 characters
```

对单个超过 limit 的 stream采用：

```text
50% head
+
omission marker
+
50% tail
```

因此默认约为：

```text
4_000 characters head
+
marker
+
4_000 characters tail
```

stdout / stderr始终保持独立。

如果 command unsuccessful，可以在总体 observation organization上优先让 diagnostic stderr易于模型消费，但不得因此改变07的事实保存语义。

---

### 20. Normal CLI Shell Display Default

human-visible Normal CLI 与 model-visible projection是不同 surface。

Normal CLI 的 Shell详细输出默认上限：

```text
approximately 4_000 characters total
```

Normal renderer可以根据 operation outcome采用 deterministic、human-friendly selection：

* successful command：优先 concise completion / tail；
* unsuccessful command：优先 bounded diagnostic tail；
* truncation发生时显式标记。

该 human display limit不改变模型看到的 ToolResult projection。

---

### 21. Defaults Are Operational Choices

本节数字属于：

> conservative v1 operational defaults.

例如未来将：

```text
24 turns → 32 turns
80k chars → 100k chars
```

如果：

* ownership不变；
* deterministic bounding不变；
* normative semantics不变；

则不需要重新设计 architecture。

09 owning的是 concrete defaults与configuration surface，而不是声称这些数字具有理论最优性。

---

## Human-Facing CLI Observability

### 22. Observability Modes and Lean Seam

#### 22.1 Modes

v1 只提供两档：

```text
Normal
Debug
```

默认：

```text
Normal
```

通过：

```text
--debug
```

启用 Debug。

v1 不建立：

```text
quiet
minimal
verbose
trace
```

等多级 verbosity体系。

#### 22.2 Lean Observability Seam

> The Composition Root may provide an optional synchronous, read-only observability callback.

逻辑关系：

```text
Runtime / Context / provider boundary
        ↓
optional synchronous observability callback
        ↓
Normal / Debug CLI renderer
```

该 callback 只接收 bounded observability facts。它是 synchronous、optional、read-only；其 return value 不参与 Agent control flow。它不得决定下一步 action、修改 `ToolResult` 或 `PolicyDecision`、改变 Run lifecycle、触发 Tool、改变 permission、扩大 privilege，或承担 persistent state ownership。Callback failure 必须被隔离，不得成为 Agent control-flow failure。

“Optional”表示 Runtime 在 deterministic tests 或其他无 renderer 的 composition 中可以没有 observer；v1 CLI 为实现本文 Normal / Debug contract 时应提供该 callback。

该 seam 至少应能向 CLI 提供：

* Run started / terminal；
* Tool action proposed / about to execute；
* Tool result completed；
* relevant Tool validation / policy outcome；
* permission requested / resolved；
* provider transport retry；
* protocol corrective re-prompt；
* completion self-audit started / continued / finished；
* Context destructive eviction；
* `history_incomplete` transition或相关 context fact；
* budget exhaustion / relevant counter event；
* startup failure where applicable。

不要求每种 event 都是独立 class，也不建立 EventBus、ObservabilityManager、LoggingService、TracePipeline 或 subscriber hierarchy。一个 Lean `on_event(...)` 或等价 callback seam 即可。

Normal / Debug renderer 都必须 bounded，并遵守 03 Runtime Secret 与 Sensitive Data rules。禁止通过该 seam `print(repr(entire_internal_object))`，也不得无界打印 full Tool arguments、read/search content、provider response、configuration object 或 environment。

---

### 23. Normal Mode Goal

Normal mode面向：

* coding-agent最终用户；
* submission Demo观看者。

目标是让用户快速理解：

```text
Agent is doing what?
Did the action succeed?
Why is user input required?
How did the Run end?
```

Normal mode是：

> concise, action-oriented observability.

它不是完整 Runtime execution trace。

---

### 24. Normal Startup Display

正常启动后可以显示：

```text
Coding Agent
Workspace: D:\projects\example
Model: example-model
```

Normal mode至少应展示：

* bound workspace；
* model。

不得显示：

* API Key；
* Secret value；
* internal context budget；
* retry counters；
* Tool registry object；
* raw configuration object。

Base URL可以在 Debug中展示；Normal不要求。

---

### 25. Tool Action Rendering

Normal CLI不直接 dump完整 protocol object。

Normal rendering 必须 bounded、Secret-safe。默认不得完整显示 `edit_file.old_text`、`edit_file.new_text`、`create_file.content`、`read_file` content、full search result、unbounded Tool arguments、raw provider response 或 complete configuration object。

例如：

#### list_directory

```text
[list] src/
```

#### search_files

```text
[search files] **/*.py
```

#### search_text

```text
[search] "parse_token" in src/
```

#### read_file

```text
[read] src/parser.py:40-120
```

#### edit_file

```text
[edit] src/parser.py
```

#### create_file

```text
[create] tests/test_parser.py
```

#### shell

```text
[shell] pytest tests/test_parser.py -q
```

Shell command 可以在 Normal 中显示，但必须 bounded、对 known Secret 做 redaction，并安全截断 overly long arguments。

这些只是 human rendering，不改变 Tool schema。

v1 不要求新增 ToolRenderer subsystem；简单 CLI helper即可。

---

### 26. Read and Search Results in Normal Mode

模型需要看到 bounded file/search content，不代表 Normal CLI需要重复显示全部内容。

例如：

```text
[search] "parse_token" in src/
✓ 4 matches

[read] src/parser.py:40-120
✓ 81 lines
```

Normal默认不 dump：

* full read content；
* all search lines；
* provider serialization；
* raw ToolResult object。

因此：

```text
model-visible ToolResult
!=
human-visible Normal CLI output
```

---

### 27. Mutation Result Rendering

成功 edit：

```text
[edit] src/parser.py
✓ replaced 1 occurrence
```

成功 create：

```text
[create] tests/test_parser.py
✓ created
```

失败：

```text
[edit] src/parser.py
✗ expected 1 match, found 3
```

Normal优先展示 semantic outcome，而不是低价值 metadata。

例如 `bytes_written` 不要求默认展示。

---

### 28. Shell Result Rendering

#### Successful Short Command

例如：

```text
[shell] pytest tests/test_parser.py -q
✓ 8 passed in 0.31s
```

#### Successful Long Command

例如：

```text
[shell] pytest
✓ exit 0
328 passed in 12.4s
```

Normal不 dump完整长日志。

#### Unsuccessful Command

应展示更多 bounded diagnostic information，例如：

```text
[shell] pytest tests/test_auth.py -q
✗ exit 1

FAILED tests/test_auth.py::test_expired_token
AssertionError: ...
1 failed, 11 passed
```

Human CLI selection必须 deterministic，不调用 LLM生成 log summary。

---

### 29. Intermediate Model Commentary

如果一个 Assistant response同时包含：

```text
ordinary assistant text
+
ToolCalls
```

Normal mode不要求完整展示 tool-call turn中的 ordinary commentary。

例如模型返回：

```text
I'll inspect the parser first.
+
read_file(...)
```

Normal可以只展示：

```text
[read] src/parser.py:1-120
```

Final Assistant Response必须完整呈现。

v1 不依赖或展示 private chain-of-thought。

### 29.1 Completion Self-Audit Presentation

04/07 定义 eligible Run 的 hidden Candidate Final 与 bounded completion self-audit。Normal CLI 不展示 Candidate，也不把它渲染为普通 intermediate commentary。进入 self-audit 时只显示一次轻量提示：

```text
◆ 检查完成情况
```

Audit 中后续 Tool actions继续使用现有 Normal renderer；真正 Final只完整展示一次。不得在 Normal 中暴露 `completion_audit_active`、`pending_final_candidate`、内部 phase 名称或完整 Candidate 文本。

Debug 可以显示 bounded events：

```text
completion_audit_started
completion_audit_continued
completion_audit_finished
```

这些 event 只携带 safe metadata，例如 Model Turn、触发 eligibility 的 capability、是否继续 Tool Loop和是否产生 Final；不得重复打印完整 Candidate、raw Context 或 Runtime Secret。Observer仍然 read-only、failure-isolated，不参与 audit control flow。

### 29.2 Workspace Change Summary

当 04 的 terminal `WorkspaceChangeFacts` 可用且至少存在一个 relevant path或uncertainty时，Normal CLI可以在 Final 前显示一个 concise trust summary，例如：

```text
Workspace changes: 1 pre-existing, 1 Agent-touched, 0 new/other
Attribution uncertain: no
```

Normal只显示counts与uncertainty，不dump diff或完整path list。Debug可以显示bounded path lists和awareness state。non-Git / unavailable observer默认不在Normal制造warning noise；Debug仍可显示degraded state。

该summary不声称semantic task success，不改变Final，不构成Git Tool family，也不提供mutation command。

---

### 30. ask_user Presentation

`ask_user` 是 Task clarification。

Normal CLI应呈现为普通 Agent clarification，例如：

```text
Agent needs clarification:
Should the existing public API remain backward compatible?

> 
```

不得要求用户理解：

```text
ToolCall
call_id
InteractionTool
```

等内部协议概念。

---

### 31. Permission Confirmation Presentation

Runtime permission confirmation必须与 `ask_user` 明显区分。

例如：

```text
Permission required

Action:
  shell: pip install requests

Reason:
  Dependency installation requires confirmation.

Allow this exact action once? [y/N]
```

默认：

```text
No
```

直接回车等同 reject。

批准：

```text
✓ approved once
```

拒绝：

```text
✗ rejected
```

UI必须清楚表达：

> approval applies to this exact action once.

permission prompt不伪装成模型 clarification。

---

### 32. Policy DENY Presentation

被 Runtime DENY 的 action可以简洁显示：

```text
[shell] sudo ...
✗ denied by policy
```

Normal mode不需要显示完整 PolicyEngine内部 facts。

如果 action包含敏感内容，human rendering仍需遵守03 Secret / Sensitive Data边界。

---

### 33. Context Truncation Human Notice

07规定：

> 一旦 current Run `history_incomplete = true`，每个后续 ModelRequest都必须携带 request-local truncation notice。

human CLI不需要每个 Model Turn重复该 notice。

Normal mode可以在该 Run第一次发生 destructive Context eviction时显示一次：

```text
[context] older working history was trimmed
```

human-visible one-time notice不改变 model-visible sticky notice contract。

Debug可以显示更多 eviction detail。

---

### 34. Internal Recovery Events

以下事件默认不在 Normal mode逐次显示：

* RepeatedActionWarning；
* Protocol Corrective Instruction；
* successful transient transport retry；
* internal context-size calculation。

这些属于 Debug observability。

如果最终导致 terminal failure，Normal必须显示用户可理解的 terminal reason。

---

### 35. Budget and Terminal Result Presentation

Budget exhaustion必须在 Normal mode解释原因，例如：

```text
Run stopped: model-turn limit reached.
```

或：

```text
Run stopped: tool-call budget exhausted.
```

FAILED：

```text
Run failed: model provider unavailable.
```

CANCELLED：

```text
Run cancelled.
```

不得只显示模糊：

```text
FAILED
```

而不给用户原因。

---

### 36. COMPLETED Must Not Mean “Task Succeeded”

08规定：

```text
COMPLETED
```

只是 Runtime lifecycle result。

因此 CLI不得自动把它渲染为：

```text
✓ Task succeeded
```

因为合法 Final可能是：

```text
I could not complete the requested change because ...
```

Normal应优先完整展示：

```text
Final:
<assistant final response>
```

如需显示 lifecycle status，可以使用：

```text
Run completed.
```

但不得自行升级为 semantic success claim。

---

## Debug Observability

### 37. Debug Mode Purpose

Debug mode用于：

> diagnosing Runtime, protocol, Tool, policy, Context, and provider behavior.

Debug不是：

> disable safety

也不是：

> expose every in-memory object.

---

### 38. Debug Information

在 relevant 时，Debug可以额外展示：

* Run identifier / model-turn index；
* model-turn count；
* tool-call-attempt count；
* normalized ToolCall name / arguments；
* `call_id`；
* Tool validation result；
* PreparedToolCall summary；
* ToolCapability；
* PolicyDecision；
* ResolvedPath facts；
* Shell classifier facts；
* ToolOutcome / formal error code；
* approximate Context size；
* eviction count / unit category；
* `history_incomplete`；
* transport retry attempt；
* corrective re-prompt occurrence；
* 05 normalized provider usage fields（provider 提供时）；
* terminal failure category。

Normal mode 默认不显示 provider usage。Debug 可以显示 normalized input tokens、output tokens、total tokens 及 05 正式定义的其他 normalized usage fields，但不得显示 raw provider payload。

Debug 对 content-bearing Tool arguments 只能 omit、redact 或 bound。例如可以显示：

```text
edit_file:
  path=src/a.py
  expected_count=1
  old_text=<omitted, 428 chars>
  new_text=<omitted, 441 chars>
```

而不是打印全文。具体内部 representation不属于 public protocol contract。

---

### 39. Debug Secret Safety

Debug mode不得：

* 显示 Runtime Secret；
* 把 API Key打印进 terminal；
* 把 Secret重新注入 Tool arguments；
* 关闭 Shell environment filtering；
* 绕过 Sensitive Path policy；
* 打印 known credential values；
* dump full environment；
* dump credential-bearing raw provider data。

原则：

> Debug increases observability, not privilege.

v1 默认不显示 raw provider response、raw provider metadata、raw HTTP body 或 headers；Debug 也只使用 normalized safe fields，不自动持久化 raw provider response / metadata。

---

### 40. Normal, Debug, Model Context, and Logs Are Separate Surfaces

v1明确区分：

```text
Model-visible Context

Normal human CLI

Debug human CLI

Internal Runtime state
```

例如：

```text
model:
sees bounded read_file content

Normal CLI:
sees [read] path:range

Debug CLI:
may see projection / context metadata
```

同样：

```text
model:
safe projected error

Normal:
human-readable concise failure

Debug:
formal ToolOutcome / code / policy facts
```

不得因为一个 surface需要某信息，就默认复制到所有其他 surface。

---

### 41. No Automatic Persistent Debug Log

v1默认不自动创建：

```text
agent.log
debug.log
trace.jsonl
```

等 persistent log file。

原因：

* Runtime Secret风险；
* workspace content泄露风险；
* log lifecycle / rotation复杂度；
* submission artifact污染。

Debug默认输出到 terminal / stderr。

用户可以使用普通 shell redirection自行保存需要的输出。

---

### 42. stdout and stderr

v1建议：

```text
stdout:
normal user-facing interaction / Final

stderr:
debug diagnostics
startup failures
Runtime warnings
```

interactive terminal中两者都可能显示在同一终端。

formal separation服务于：

* scripting；
* redirection；
* debug isolation。

---

### 42.1 Machine-Readable One-Shot

CLI 支持：

```text
coding-agent --workspace <PATH> --json <one-shot task...>
```

`--json` 只适用于已有的 positional one-shot task；它不建立 JSON interactive Session、event stream 或 provider trace API。缺少 task 时在执行任何 Run 前返回 deterministic usage failure。

在该模式中，stdout 必须恰好包含一个 newline-terminated valid JSON document。startup banner、progress、permission UI、Debug diagnostics 和其他 human presentation 不得混入 stdout；必要的 interactive prompt 或 diagnostic 可以使用 stderr。JSON 不含 Markdown、raw provider response、完整 Tool history、content-bearing Tool arguments 或 Runtime Secret。

schema version 1 固定包含：

```text
schema_version
lifecycle_state
final_response
terminal_reason
normalized_error
model_turns
tool_attempts
limit_reached
```

其中：

* `lifecycle_state` 是 `COMPLETED` / `FAILED` / `CANCELLED`；Run 建立前的 startup / usage failure 使用 `STARTUP_FAILED`；
* `final_response` 只在 Runtime 产生真实 Final 时为 string，并完整保留（Runtime Secret redaction 优先）；
* `terminal_reason` 保存 normalized terminal code，正常 `COMPLETED` 为 `null`；
* `normalized_error` 只提供 bounded `{code, message}`，不序列化 exception、traceback 或 provider payload；
* counters 直接来自 terminal `AgentRun`，startup failure 为 `0`；
* provider usage 只有在现有 Runtime 已提供可信 normalized aggregate 时才允许新增 optional versioned field；首版不为此修改 Provider / Runtime protocol。

不得增加模糊的业务字段：

```text
success: true
task_succeeded: true
```

因为 `COMPLETED` 只表示 Runtime 得到合规 Final，不证明模型的业务结论正确。process exit code 只表达 CLI / Runtime lifecycle：`0` = COMPLETED，`1` = FAILED，`130` = CANCELLED，`2` = startup / usage failure。

---

### 42.2 Explicit Non-Interactive One-Shot

CLI 支持：

```text
coding-agent --workspace <PATH> --non-interactive <one-shot task...>
coding-agent --workspace <PATH> --json --non-interactive <one-shot task...>
```

`--non-interactive` 必须有 positional task，不进入 top-level prompt，不读取 stdin，不 auto-approve permission，也不为 clarification制造答案。Shell stdin继续使用现有 noninteractive contract。普通不需要交互的任务保持原 Agent loop。

若模型请求 `ask_user`，Run 终止为 `FAILED / CLARIFICATION_REQUIRED`；若一个 prepared action需要 `CONFIRM`，Run 在 action执行前终止为 `FAILED / PERMISSION_REQUIRED`。两者 process exit code均为 `3`，表示安全地需要外部输入；Policy `DENY` 仍是给模型的普通 rejection observation，不伪装成 permission request。

Machine result 在这两个终止原因下增加一个 bounded `required_interaction` object。Clarification只包含 kind与question；permission只包含 kind、tool/operation category、bounded action preview、reason code、risk和 exact one-action scope。它不包含 PendingAction、完整 Tool arguments、content-bearing edits、Runtime Secret或可重放 approval token。其他 JSON shape保持不变。

Interactive mode、无 `--non-interactive` 的 `--json` mode以及 ConsoleUserInteraction语义保持不变。

---

### 42.3 Explicit Persistent Session CLI

CLI 提供四个显式入口：

```text
coding-agent --workspace <PATH> --persist-session <task...>
coding-agent --workspace <PATH> --resume <SESSION_UUID> <follow-up task...>
coding-agent --workspace <PATH> --list-sessions
coding-agent --workspace <PATH> --delete-session <SESSION_UUID>
```

前两个入口也可省略 one-shot task而进入现有 interactive loop。`--persist-session` 创建新的 UUID session；`--resume` 只加载 exact canonical UUID，不创建 missing session。四者互斥。默认 CLI 行为不写 session checkpoint。

Session documents 存在用户级 session directory；测试或受控 automation 可以通过 `CODING_AGENT_SESSION_DIR` 选择独立目录。每个 document 使用 schema version 1，包含 exact session ID、canonical workspace identity、last-completed timestamp 和 bounded completed-run task/final pairs。Missing、corrupt、unknown version、invalid ID 和 wrong workspace 都在 ModelClient construction / Run start 前作为 deterministic startup failure 返回。

每个 `COMPLETED` Run 结束并清理 pending state后原子更新 checkpoint。`FAILED` / `CANCELLED` Run 不更新 checkpoint。写入使用同目录 exclusive temporary file + atomic replace；失败时保留旧 checkpoint并产生独立、Secret-safe persistence error。恢复不会复用旧 project instructions、dirty-workspace snapshot、hard constraints、Runtime counters或 pending state。

Human mode 显示 bounded session ID。`--json` 与 persistence/resume组合时保留现有 schema version和字段，并额外包含 `session_id`、`session_checkpoint_updated` 与 nullable bounded `session_error`；未请求 persistence 时现有 JSON document shape不变。

`--list-sessions` 与 `--delete-session` 是 model-free management operations：不接受 positional task或 `--non-interactive`，不进入 interactive loop，不需要 provider credentials，也不构造 ModelClient。List 只显示绑定当前 canonical workspace 的 session ID、validated UTC update time与 completed-run count；不显示 task/final continuity或其他 workspace sessions。损坏、不可读或 symbolic-link documents 只贡献 anonymous `skipped_invalid_entries` count。Delete 要求 exact canonical UUID，验证 document、regular file与 workspace binding后只 unlink该 checkpoint；missing与 wrong-workspace均 deterministic failure。

Management JSON 是独立 schema-versioned document。List 返回 `operation=list_sessions`、`workspace_identity`、metadata-only `sessions` array与 `skipped_invalid_entries`；delete返回 `operation=delete_session`、exact `session_id`与 `deleted=true`。管理失败沿用 no-Run startup failure document且 process exit code为 `2`。

### 42.4 Opt-In Change and Command Review

CLI支持：

```text
coding-agent --workspace <PATH> --review <task...>
coding-agent --workspace <PATH> --json --review <one-shot task...>
```

`--review` 也可与 `--non-interactive`、`--persist-session`或 `--resume`组合；它不适用于 model-free list/delete management。没有该 flag时，既有 human presentation与JSON document shape保持不变。

Human terminal在Run终止后增加一个“变更与命令证据”块。Machine result增加一个 bounded `review` object：

```text
workspace_changes
  awareness_state
  pre_existing_dirty_paths
  known_agent_touched_paths
  new_or_other_dirty_paths
  attribution_uncertain
  paths_truncated
command_evidence[]
  command
  cwd
  outcome
  exit_code
  error_code
  presentation_category
command_evidence_truncated
verification_sufficiency = NOT_INFERRED
```

Path每组最多投影50项；Runtime最多保留32条实际 Shell execution，command/cwd各500字符；已进入 execution后被取消的命令显示为 `INTERRUPTED`。所有文本在输出前再次做 Runtime Secret redaction，human terminal使用安全 quoting。Review不包含 stdout/stderr、edit content、未执行的 permission action、PendingAction或 provider payload。`presentation_category`只复用现有高置信 UI label，不是 verification判定；exit 0也不自动证明验证充分性。

### 42.5 Bounded Non-Interactive JSONL Events

CLI支持：

```text
coding-agent --workspace <PATH> --jsonl --non-interactive <one-shot task...>
```

`--jsonl` 与 `--json`互斥，必须同时使用`--non-interactive`与positional one-shot task；它不适用于interactive Session或model-free session management。它可以与`--review`、`--persist-session`或`--resume`组合。

stdout只包含newline-delimited schema version 1 JSON documents。每行拥有从1开始严格递增的`sequence`。零个或多个event lines形如：

```json
{"schema_version":1,"type":"event","sequence":1,"event":{"kind":"run_started","facts":{}}}
```

最后恰有一个`type=result` line，其`result`复用§42.1 terminal JSON document（包括显式请求的review/session optional fields）。Startup/usage failure没有event line，只返回sequence 1的terminal result。Process exit code继续使用现有lifecycle语义。

Event只投影Runtime已经bounded、Secret-redacted的normalized scalar facts，并在CLI再次redact。JSONL明确剔除`action`、`diagnostic`、`pre_existing_paths`、`known_touched_paths`与`new_or_other_paths`；因此不包含Shell stdout/stderr、content-bearing Tool arguments、exact path lists、provider payload或reasoning continuation。需要exact terminal path/command facts时由用户显式组合`--review`，只在result中返回。

JSONL是Runtime event stream，不是provider token streaming、partial Assistant/ToolCall protocol、Tool output streaming、persistent log或control channel。Event consumer不能approve permission、回答clarification或改变Runtime；required interaction仍只在terminal result中以exit 3表示。

---

## Interactive CLI and Session Control

### 43. Interactive Session Model

v1 CLI是 persistent in-process interactive Session。

一个进程可以包含：

```text
Run 1
Run 2
Run 3
...
```

每个用户提交的非空 task启动一个新的 Run。

Session continuity仍遵守04/07。

Cross-process resume 只通过上一节的显式 terminal-safe checkpoint入口提供；默认 interactive Session仍是纯进程内行为。

---

### 44. Top-Level Prompt

Session ready时进入类似：

```text
> 
```

的 task prompt。

用户输入：

```text
Fix the failing parser test
```

启动一个 Run。

Run结束后，如果 Session仍有效：

```text
COMPLETED
FAILED
CANCELLED
```

CLI返回顶层 prompt，允许启动新的 Run。

因此：

> A single failed or cancelled Run does not normally terminate the Session.

该 Session-recoverability requirement 由 §58 定义的 09-conformance evidence 验证。04 owning lifecycle 与 cancellation semantics，08 owning testing framework；本文 §58 列出 09 所需 evidence。

---

### 45. Empty Input

顶层 prompt收到纯 whitespace / empty input：

```text
→ no Run created
→ prompt again
```

不向 ModelClient发送空任务。

---

### 46. Session Exit Commands

v1支持：

```text
/exit
/quit
```

在顶层 prompt退出当前 Session。

这些是 CLI control commands，不作为 User Task发送给模型。

Normal Session exit的 process exit code：

```text
0
```

---

### 47. EOF

顶层 prompt收到 EOF：

```text
Ctrl+D / stream EOF
```

正常结束 Session。

不创建新 Run。

---

### 48. Ctrl+C During Active Run

用户在 active Run期间触发 Ctrl+C：

```text
→ cancel current Run
→ follow 04 CANCELLED semantics
→ safely clear pending Run-local execution / Context state
→ return to top-level Session prompt
```

如果 local Tool execution正在进行，仍按06的 best-effort interruption / process cleanup能力处理。

不得因为一个 cancelled Run留下 protocol/context pending state而永久毒化 Session。

---

### 49. Ctrl+C at Top-Level Prompt

在没有 active Run的顶层 prompt触发 Ctrl+C：

```text
→ exit Session
```

process正常退出。

v1不增加：

```text
press Ctrl+C twice to exit
```

等额外交互状态。

---

### 50. User Interaction Cancellation

如果用户在：

```text
ask_user
permission confirmation
```

阶段取消当前操作，Run按04规定进入对应 terminal cancellation path。

CLI必须确保：

* PendingAction不被错误执行；
* incomplete interaction state被清理；
* Session随后仍可继续。

09不重新定义04的 cancellation semantics。

---

### 51. Startup Failure

如果在进入 Session前发生：

* invalid configuration；
* missing required provider configuration；
* invalid workspace；
* Tool registry startup invariant failure；
* other composition-root failure；

CLI：

```text
prints concise startup error
→ does not enter interactive Session
→ exits non-zero
```

Debug mode可以提供额外 diagnostic detail，但仍遵守 Secret过滤。

---

### 52. Run Failure Does Not Equal Process Failure

interactive Session中：

```text
Run → FAILED
```

通常不会直接让 CLI process退出。

CLI展示 failure reason后：

```text
→ return to top-level prompt
```

只有：

* fatal Session/composition corruption；
* unrecoverable CLI/runtime infrastructure failure；
* explicit user exit / EOF；

才结束 process。

具体 Runtime terminal semantics仍由04 owning。

---

## Configuration and CLI Invariants

### 53. v1 Configuration Invariants

1. Configuration只控制 operational parameters，不关闭 architecture contract。
2. CLI configuration优先于 environment。
3. environment优先于 built-in defaults。
4. v1不使用 general-purpose config file。
5. Runtime Secret通过 environment提供。
6. v1不接受普通 `--api-key` flag。
7. Secret value不得出现在 Normal或Debug output。
8. Workspace必须通过 required `--workspace <PATH>` 由用户显式指定，不使用 implicit cwd fallback。
9. Workspace在 Session startup前 canonical binding。
10. Critical configuration在 startup前 validation。
11. Budgets可以被显式用户 override，但只能位于 finite validated public ranges 内。
12. Internal projection/resource tuning不要求全部成为 public CLI options。
13. Concrete defaults是 operational defaults，不是 architecture constants。
14. Default concrete ModelClient是`OpenAICompatibleModelClient`；model和Base URL没有built-in default，必须显式配置。
15. API Key required且environment-only。
16. Model request timeout default为60 seconds；protocol error consecutive limit为3；`user_wait_timeout = None`。
17. Shell backend由platform选择：Windows `COMSPEC`、POSIX `/bin/sh`，无public `CODING_AGENT_SHELL` override。

---

### 54. v1 Observability Invariants

1. v1只提供 Normal 与 Debug两种 observability mode。
2. Normal mode concise、action-oriented。
3. Normal展示 Tool action与 concise outcome，而不 dump完整 protocol object。
4. Normal默认不回显完整 read/search content。
5. edit/create默认不回显完整 file content。
6. Shell command在 Normal中可见。
7. Shell长输出 human-visible display必须 bounded。
8. Shell failure应展示 bounded actionable diagnostic information。
9. Human CLI Shell projection与 model-visible Shell projection相互独立。
10. tool-call turn中的普通 model commentary不要求在 Normal完整展示。
11. Final Assistant Response完整展示。
12. ask_user 与 permission confirmation必须在 UI上语义区分。
13. Permission confirmation默认 reject。
14. Context truncation human notice不需要像 model notice一样每轮重复。
15. RepeatedActionWarning默认 Debug-only。
16. Protocol corrective event默认 Debug-only。
17. successful transport retry默认 Debug-only。
18. terminal failure / budget exhaustion必须在 Normal展示原因。
19. `COMPLETED`不得自动渲染成“Task succeeded”。
20. Debug增加 observability，不增加 privilege。
21. Debug仍执行 Runtime Secret filtering。
22. v1不自动创建 persistent debug log。
23. model-visible Context与human-visible CLI是不同输出面。
24. Normal与Debug renderer都必须bounded且Secret-safe，不默认dump content-bearing arguments或raw internal objects。
25. Normal默认不显示provider usage；Debug只显示normalized usage与safe metadata。
26. Raw provider response、metadata、HTTP body和headers默认不显示且不自动持久化。
27. Optional observability callback是synchronous、read-only且control-flow independent；callback failure不得成为Agent control-flow failure。
28. Candidate Final 不进入 Normal output；eligible Run 进入 self-audit 时 Normal 最多显示一次轻量检查提示，真正 Final 只展示一次。
29. Debug completion-audit events 必须 bounded、Secret-safe，不打印完整 Candidate，并且不能改变 Runtime control flow。
30. `--json` 只支持 one-shot；stdout 恰好一个稳定 JSON document，human/debug surface 与 prompt 使用 stderr。
31. machine-readable output 只报告 lifecycle facts，不把 `COMPLETED` 伪装成 semantic task success。
32. machine-readable error bounded、normalized、Secret-safe，不包含 raw exception、provider payload或完整 Tool history。
33. Workspace change summary只展示04提供的conservative facts；Normal默认counts-only，Debug path lists bounded。

---

### 55. Interactive Session Invariants

1. 一个 process内可以包含多个 sequential Runs。
2. 每个非空 top-level task启动新 Run。
3. 空输入不创建 Run。
4. `/exit` 与 `/quit` 在 top level正常结束 Session。
5. EOF在 top level正常结束 Session。
6. active Run中的 Ctrl+C取消当前 Run。
7. top-level Ctrl+C结束 Session。
8. FAILED Run默认返回 top-level prompt。
9. CANCELLED Run默认返回 top-level prompt。
10. 一个 terminal Run不得留下阻止同一 Session后续 Run启动的 pending Context/protocol state。
11. startup invariant failure不进入 interactive Session。
12. cross-process resume只恢复 bounded COMPLETED task/final continuity，并创建新的 execution state。

---

## Implementation Boundary

### 56. Lean Implementation

09 的实现应尽量依赖现有：

```text
CLI / Composition Root
AgentRuntime
ContextManager
UserInteraction
Tool abstractions
ModelClient
```

v1不要求新增：

```text
ConfigService
ConfigManager hierarchy
ObservabilityBus
EventBus
ToolRenderer framework
LoggingService
TraceCollector
general SessionPersistence framework
TerminalUI framework
```

简单：

```text
AgentConfig
CLI parsing
small formatting helpers
debug conditional output
```

即可满足本文 contract。

---

### 57. Configuration Representation

实现可以使用一个 Lean、集中式 configuration object，例如：

```text
AgentConfig
```

包含主要 startup / operational configuration。

不要求为了组织字段而提前建立：

```text
ModelConfig hierarchy
ContextConfig hierarchy
BudgetConfig hierarchy
LoggingConfig hierarchy
```

如果单一 configuration object已经足够。

---

### 58. 09-Conformance Work and Evidence

09-conformance testing由08 owning。

09 freeze 后必须实现并测试以下 normative contracts：

* required explicit `--workspace`，无 implicit cwd workspace；
* default `OpenAICompatibleModelClient`；
* required model configuration；
* required Base URL configuration；
* API Key environment-only；
* 60-second model request timeout；
* maximum 3 consecutive protocol errors；
* no user wait timeout；
* Windows `COMSPEC` / POSIX `/bin/sh`，无 public `CODING_AGENT_SHELL` override；
* public budget finite ranges 与本文 concrete defaults；
* `read_file` 200 / 400 / 20,000-byte contract；
* discovery defaults：`list_directory` maximum direct entries = 200、`search_files` maximum matching paths = 200、`search_text` maximum matches = 100；这些 limits 不是 public CLI knobs；
* Shell timeout default = 120 seconds、absolute maximum = 300 seconds，且 argument / schema validation 必须 enforce `1 <= timeout_seconds <= 300`；超过上限的请求必须在 execution 前 validation failure；
* optional synchronous read-only observability callback seam；
* bounded Normal / Debug rendering；
* Secret-safe observability；
* normalized provider usage Debug rendering；
* raw provider metadata suppression；
* hidden Candidate Final、single Normal audit indicator 与 single true Final rendering；
* bounded completion-audit Debug events；
* empty input reprompt；
* `/exit` / `/quit`；
* active-Run Ctrl+C cancellation with Session recovery；
* `FAILED` / `CANCELLED` 后 subsequent same-Session Run；
* `--json` one-shot valid document、stdout isolation、stable lifecycle schema、exit codes、startup failure 和 Secret redaction；
* corresponding configuration、observability 与 CLI tests。

架构文档可以暂时领先实现，但最终提交前上述 09 normative contracts 必须具有对应 code 与 evidence。本文不修改 implementation plan，也不记录易 stale 的当前实现 snapshot table。

后续至少应有 evidence覆盖：

* configuration precedence；
* missing Secret / provider config startup failure；
* workspace startup binding；
* budget override / validation；
* Secret redaction；
* Normal Tool rendering；
* permission vs ask_user presentation；
* Debug additional observability；
* Context human notice；
* terminal/budget presentation；
* `COMPLETED` 不被渲染为 task success；
* Candidate Final 不提前显示、Audit Tool events正常显示、真正 Final仅显示一次；
* `/exit` / EOF / empty input；
* Ctrl+C Run cancellation；
* FAILED / CANCELLED 后 same-Session next Run；
* JSON COMPLETED / FAILED / CANCELLED / STARTUP_FAILED projections；
* no automatic persistent log。

最终提交前还应使用 representative real coding tasks 做一次 operational tuning check，确认默认 `max_context_chars = 80_000` 不会造成过度 destructive eviction 或反复读取相同材料。若 evidence 表明确有必要，可在现有 validated public range `8_000..256_000` 内调整 concrete default，而不改变 07 的 Context architecture。该检查不是 runtime contract，不要求固定 benchmark 数量，也不要求建立 tuner 或 benchmark framework。

上述清单同时 owning 当前已知的 09 document-to-code conformance gaps；在最终提交前，每项 normative contract 都必须由实现与相应 evidence 闭合，或在本文中显式调整其 normative 状态。

09只定义行为与 defaults，不重新建立测试框架。

---

### 59. Deferred

v1不实现：

```text
general config files
configuration profiles
provider profile registry
GUI / TUI
rich interactive panes
expandable Tool result UI
persistent trace database
log rotation
telemetry backend
remote observability
per-Tool user-configurable rendering policies
```

这些能力不属于当前 submission所需核心。

---

### 60. ADR Candidates

#### Operational Configuration, Not Architecture Switches

Decision：

用户可以覆盖 workspace、provider和budget等 operational parameters，但 normative safety / protocol / lifecycle contract不可通过配置关闭。

Canonical owner：

```text
09-cli-observability-and-configuration.md
```

#### Separate Model and Human Observability Surfaces

Decision：

model-visible Context、Normal CLI和Debug CLI是不同 projection surface；模型需要看到的信息不必完整回显给普通用户。

Canonical owner：

```text
09-cli-observability-and-configuration.md
```

#### Normal + Debug Only

Decision：

v1只提供 concise Normal 与 diagnostic Debug两档 observability，不建立多级 verbosity体系或持久 logging framework。

Canonical owner：

```text
09-cli-observability-and-configuration.md
```

#### Conservative Bounded Operational Defaults

Decision：

v1为 Run、Context、read/search和Shell提供明确 bounded defaults；这些数值是可调整 operational defaults，不是 architecture constants。

Canonical owner：

```text
09-cli-observability-and-configuration.md
```

#### Persistent Interactive Session

Decision：

一个 CLI process可以连续运行多个独立 Agent Runs；单个 Run 的 COMPLETED / FAILED / CANCELLED 默认不终止 Session，terminal cleanup必须允许后续 Run继续。

Canonical owner：

```text
09-cli-observability-and-configuration.md
```
