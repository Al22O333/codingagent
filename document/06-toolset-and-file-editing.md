# Toolset and File Editing

## 1. Purpose

本文定义 v1 Coding Agent 实际提供的本地 Tool Set，以及这些 Tool 的具体职责、输入输出、路径解析、文件发现、文本读取、文件编辑与 Shell 执行机制。

本文主要回答：

- v1 实际提供哪些 Tool；
- 模型如何发现、搜索和读取项目文件；
- Agent 如何安全地创建和修改文本文件；
- File Tool 如何统一解析 workspace path；
- 文件编辑如何检测 stale observation 和 edit conflict；
- Shell Tool 如何执行本地命令并返回结构化 observation；
- LOCAL Tool 如何准备 `PreparedToolCall` 所需的 operation facts；
- Tool-specific operation failure 如何表示；
- Tool 输出如何保持 bounded。

本文不重新定义：

- Safety Policy、Workspace Boundary、Sensitive Path 的权限语义、Risk Permission Matrix：由 `03-safety-and-execution-boundaries.md` 负责；
- Runtime lifecycle、batch fail-stop、budget、termination：由 `04-agent-runtime-model.md` 负责；
- ToolCall、ToolResult、ToolError、PreparedToolCall 等通用协议：由 `05-component-and-protocol-contracts.md` 负责；
- Prompt、Context retention、模型可见 ToolResult projection：由 `07-context-and-prompt-policy.md` 负责；
- Verification policy 与测试策略：由 `08-verification-testing-and-demo.md` 负责；
- timeout、output limit、Shell backend 等默认值和用户配置：由 `09-cli-observability-and-configuration.md` 负责。

核心原则：

> 06 定义 Tool 如何完成具体本地操作，但不拥有 Agent orchestration、Runtime lifecycle 或 safety policy。

---

## 2. Design Goals

v1 Tool Set 主要服务于 Coding Agent 的核心闭环：

```text
inspect
→ locate
→ read
→ edit / create
→ verify with command
→ observe
→ iterate
````

Tool 设计遵循以下原则：

1. 每个 Tool 只有一个清楚的主要职责。
2. Tool name 与 description 应足够明确，使模型不需要复杂 routing policy 即可选择。
3. Tool arguments 必须能够 deterministic validation。
4. Tool result 必须是 provider-neutral structured result。
5. 所有 File Tool 必须复用同一个 workspace path-resolution primitive。
6. 文件 mutation 必须尽可能检测 stale observation，而不是静默猜测模型想修改的位置。
7. 可能产生大量文本的 Tool 必须 bounded。
8. Shell 是通用 escape hatch，但常见文件操作优先使用结构化 File Tool。
9. 不为未来扩展建立 Tool plugin framework、capability graph 或通用 action DSL。
10. v1 优先支持小型本地软件项目，不追求大仓库搜索和大型代码索引能力。

---

## 3. v1 Tool Set

v1 固定提供以下 8 个 Tool：

```text
list_directory
search_files
search_text
read_file
edit_file
create_file
shell
ask_user
```

| Tool             | Kind        | Capability          | Primary Purpose               |
| ---------------- | ----------- | ------------------- | ----------------------------- |
| `list_directory` | LOCAL       | `FILE_READ`         | 查看一个目录的一层内容                   |
| `search_files`   | LOCAL       | `FILE_READ`         | 根据路径 / 文件名 glob 定位文件          |
| `search_text`    | LOCAL       | `FILE_READ`         | 在 workspace 文本文件中搜索内容         |
| `read_file`      | LOCAL       | `FILE_READ`         | bounded 地读取文本文件指定行范围          |
| `edit_file`      | LOCAL       | `FILE_MUTATION`     | 使用 exact replacement 修改已有文本文件 |
| `create_file`    | LOCAL       | `FILE_MUTATION`     | 创建新的文本文件                      |
| `shell`          | LOCAL       | `COMMAND_EXECUTION` | 运行测试、构建、编译和其他项目命令             |
| `ask_user`       | INTERACTION | —                   | 在当前 Run 中向用户请求澄清              |

v1 不提供 dedicated：

```text
delete_file
delete_directory
move_file
rename_file
git_* tools
apply_patch DSL
whole-file overwrite tool
binary editor
PDF / image reader
interactive debugger
language-specific execution tools
```

这些能力不属于 M1 主流程的必要条件。

其中一部分操作可以在 03 的 Risk Permission 允许时通过 `shell` 完成，但 Shell 的存在不改变 File Tool 的安全边界，也不意味着 Runtime 能将 Shell 当作 filesystem sandbox。

---

## 4. Tool Argument Models

Tool argument schema 应从 typed argument model 单源生成。

v1 推荐使用：

```text
Pydantic v2 model
      ↓
runtime validation
      +
JSON Schema
```

避免分别维护：

```text
hand-written JSON Schema
+
hand-written argument validator
```

而产生 schema drift。

Tool argument model 只描述模型可以提出的参数，不包含：

* permission；
* Runtime State；
* PendingAction；
* provider metadata；
* Context；
* retry information；
* lifecycle information。

---

## 5. Shared Workspace Path Resolver

### 5.1 Single Resolution Primitive

所有 File Tool 和需要 filesystem facts 的 LOCAL action preparation 必须复用同一套 workspace path-resolution implementation。

概念接口：

```python
resolve_workspace_path(
    raw_path: str,
    mode: PathResolutionMode,
) -> ResolvedPath
```

其中：

```text
PathResolutionMode:
    EXISTING
    NEW
```

禁止出现：

```text
read_file 自己 resolve 一套
edit_file 自己 resolve 一套
create_file 自己 resolve 一套
PolicyEngine 再 resolve 一套
```

03 是 canonical / semantic containment 规则的 canonical owner。

06 只实现这些规则所需的共享 primitive。

---

### 5.2 ResolvedPath

`ResolvedPath` 是一个轻量 immutable value object。

概念字段：

```python
ResolvedPath(
    raw_path: str,
    resolved_path: Path,
    exists: bool,
    is_within_workspace: bool,
    workspace_relative_path: str | None,
    is_sensitive: bool,
    is_protected: bool,
)
```

它表达 filesystem facts，不表达 permission decision。

containment 字段必须满足：

```text
inside workspace:
    is_within_workspace = true
    workspace_relative_path != null

outside workspace:
    is_within_workspace = false
    workspace_relative_path = null
```

`resolved_path` 表示 resolver 成功得到的 canonical existing target，或根据 canonical existing parent 与 remaining suffix 构造的 candidate new target。resolver 无法完成所需 filesystem operation，与 resolver 成功识别 outside-workspace target 是两种不同结果：

```text
resolution operation failed
→ OPERATION_FAILURE

resolution succeeded and is_within_workspace = false
→ policy fact
→ POLICY_REJECTED
```

例如：

```text
is_sensitive = true
```

只意味着：

> 当前目标满足 Sensitive Path classification。

是否：

```text
ALLOW
CONFIRM
DENY
```

仍由 03 / PolicyEngine 决定。

---

### 5.3 Workspace Root

Runtime startup 已按照 05 的 startup invariant：

* 接收一个 existing workspace root；
* 确认其为 directory；
* canonicalize workspace root。

06 的 File Tool 始终以该 canonical workspace root 为根。

用户或模型提供的相对 path：

```text
src/auth.py
```

语义始终是：

```text
<workspace_root>/src/auth.py
```

而不是 process 当前任意 working directory。

---

### 5.4 Existing Path Resolution

对于必须存在的目标，例如：

```text
list_directory
read_file
edit_file
```

resolver 应：

1. 将相对 path 绑定到 workspace root；
2. canonicalize / resolve existing target；
3. 解析 filesystem 能看到的 symlink / junction 等 indirection；
4. 根据 03 的 semantic containment rule 判断最终 target 是否位于 workspace root；
5. 生成稳定 workspace-relative representation；
6. 收集 Sensitive / Protected classification facts。

不得使用：

```python
str(target).startswith(str(workspace_root))
```

判断 containment。

---

### 5.5 New Path Resolution

对于：

```text
create_file
```

target 可能尚不存在。

resolver 应：

1. 将 raw path 绑定到 workspace root；
2. 向上寻找最近 existing parent；
3. canonicalize existing parent；
4. 判断该 parent 是否位于 workspace root；
5. 保留尚不存在的 path suffix；
6. 生成新目标的 workspace-relative representation；
7. 对能够从现有 filesystem 状态识别的 protected / sensitive pattern 做分类。

不得依赖：

```text
对完整不存在 target 做一次 resolve()
```

然后假装获得可靠 containment。

---

### 5.6 Workspace Boundary Fact

如果 resolver 能成功确定：

```text
target resolves outside workspace
```

这不是普通 filesystem operation failure。

它是成功获得的 security fact。

后续由 Runtime / Policy 根据 03：

```text
DENY
```

处理。

同理：

```text
is_sensitive
is_protected
```

是 prepared facts，不是 preparation error。

---

## 6. Sensitive and Protected Path Classification

### 6.1 Protected Paths

v1 至少将：

```text
.git/**
```

视为 File Tool mutation 的 Protected Path。

因此：

```text
edit_file(".git/...")
create_file(".git/...")
```

不得作为普通 File Tool mutation 执行。

Git 操作应通过 Shell / Git command 进入 03 定义的风险策略。

06 不重新定义 Git permission matrix。

---

### 6.2 Sensitive Path Patterns

v1 默认 Sensitive Path pattern 至少包括：

```text
.env
.env.*
*.pem
*.key
*.p12
*.pfx
id_rsa
id_ed25519
credentials
credentials.*
credentials*
```

classification 主要基于 workspace-relative path / basename。

该集合属于 File Tool 的 concrete sensitive-pattern implementation。

Sensitive Path 的权限语义仍由 03 定义。

---

### 6.3 Discovery Behavior

默认：

```text
search_files
search_text
```

不得主动把 Sensitive Path 内容暴露给模型。

其中：

* `search_text` 跳过 Sensitive Path；
* `search_files` 默认也不返回 Sensitive Path match。

但如果模型明确请求：

```text
read_file(".env")
```

resolver 可以生成：

```text
is_sensitive = true
```

然后进入 03 定义的 Risk Permission flow。

这使：

```text
default discovery exclusion
```

与：

```text
explicit sensitive access
```

保持区分。

---

## 7. Discovery Ignore Rules

### 7.1 `.gitignore`

默认 discovery：

```text
list_directory
search_files
search_text
```

应在适用时遵守 workspace `.gitignore`。

`.gitignore` 只是：

> discovery optimization / noise reduction。

不是 Permission Boundary。

因此：

```text
read_file("ignored/generated.py")
```

只要该 path 位于 workspace 且不违反其他规则，仍然可以正常访问。

---

### 7.2 Default Noise Ignore

即使 workspace 不包含 `.gitignore`，discovery 也默认跳过明显高噪声目录：

```text
.git/
node_modules/
.venv/
venv/
__pycache__/
.pytest_cache/
.mypy_cache/
dist/
build/
```

此列表保持小而明确。

不建立庞大的语言 / framework-specific ignore database。

---

### 7.3 Implementation

v1 可以使用普通工程 library，例如：

```text
pathspec
```

实现 `.gitignore` matching。

这是允许的工程依赖，因为：

* 它不执行 Agent orchestration；
* 不执行 Tool selection；
* 不拥有 permission；
* 不控制 Agent Loop。

没有必要重新实现完整 Git ignore syntax。

---

## 8. Text File Model

v1 File Tool 主要支持：

> UTF-8 text workflow。

读取文件时：

1. 以 bytes 读取；
2. 检测明显 binary indicator；
3. 尝试 UTF-8 decode；
4. 若不适合作为 v1 Text Tool 输入，则返回 structured operation failure。

---

### 8.1 Binary Heuristic

至少：

```text
contains NUL byte
```

可以作为明显 binary indicator。

v1 不需要完整 MIME detection framework。

---

### 8.2 Decode Failure

如果 UTF-8 decode 失败：

```text
TEXT_DECODING_FAILED
```

而不是：

* 自动猜 GBK；
* 自动猜 UTF-16；
* 调用 encoding detection framework；
* 返回乱码给模型。

v1 可以把非 UTF-8 text file 作为 Accepted Limitation。

---

### 8.3 Unsupported Content

普通 Text Tool 不支持：

```text
images
PDF
archives
compiled binaries
database files
media
binary patching
OCR
```

典型 error：

```text
BINARY_FILE_UNSUPPORTED
```

---

## 9. `list_directory`

### 9.1 Purpose

`list_directory` 用于查看一个目录的直接子项。

它只列一层。

不提供：

```text
depth
recursive
max_depth
```

参数。

模型如需继续探索：

```text
list_directory("src")
list_directory("src/runtime")
```

即可。

递归定位文件由：

```text
search_files
```

负责。

---

### 9.2 Schema

```python
list_directory(
    path: str = ".",
)
```

`path` 必须指向 workspace 内 existing directory。

---

### 9.3 Result

内部 result 至少包含：

```text
path
entries:
    - relative_path
    - type: file | directory
truncated
```

推荐 deterministic ordering：

```text
directories first
files second

within each category:
lexicographical order
```

相同 Workspace State 下尽量获得稳定 observation。

---

### 9.4 Ignore Behavior

普通 directory listing：

* 不显示 `.git/` 内部内容；
* 默认应用 discovery noise filtering；
* 默认隐藏 Sensitive Path；
* 不跟随可能逃逸 workspace 的 directory symlink 进行递归，因为本 Tool 本身不递归。

如果 path 本身明确指向不允许访问的目标，则交由 path/policy contract 处理。

---

### 9.5 Failure Examples

```text
DIRECTORY_NOT_FOUND
NOT_A_DIRECTORY
DIRECTORY_LIST_FAILED
```

---

## 10. `search_files`

### 10.1 Purpose

`search_files` 根据 workspace-relative path / filename glob 定位文件。

它不读取文件内容。

---

### 10.2 Schema

```python
search_files(
    pattern: str,
    path: str = ".",
)
```

`pattern` 使用 glob-style pattern。

例如：

```text
**/*.py
**/*.cpp
**/test_*.py
**/CMakeLists.txt
**/*auth*
```

`path` 表示搜索起点。

---

### 10.3 Behavior

`search_files`：

* 递归搜索指定 workspace subtree；
* 遵守 `.gitignore`；
* 应用默认 noise ignore；
* 默认排除 Sensitive Path；
* 不读取文件内容；
* 不跟随能够逃逸 workspace 的 symlink；
* 结果使用 workspace-relative path；
* 结果数量 bounded；
* ordering deterministic。

---

### 10.4 Result

```text
pattern
path
matches:
    - relative_path
truncated
```

如果：

```text
truncated = true
```

模型可以缩小：

```text
path
pattern
```

重新搜索。

v1 不为 search 建立 cursor / pagination protocol。

---

### 10.5 Failure Examples

```text
DIRECTORY_NOT_FOUND
INVALID_SEARCH_PATTERN
SEARCH_FAILED
```

---

## 11. `search_text`

### 11.1 Purpose

`search_text` 在 workspace 的 text files 中搜索代码或文本内容。

---

### 11.2 Schema

```python
search_text(
    query: str,
    path: str = ".",
    file_glob: str | None = None,
    regex: bool = False,
    case_sensitive: bool = False,
)
```

默认：

```text
regex = false
```

意味着：

> literal substring search。

模型只有明确需要 pattern matching 时才设置：

```text
regex = true
```

这样降低无意义 regex escaping / malformed pattern。

---

### 11.3 Search Scope

搜索范围：

```text
workspace text files only
```

并遵守：

* workspace containment；
* `.gitignore`；
* default noise ignore；
* Sensitive Path exclusion；
* optional `file_glob`。

binary / unsupported text file：

```text
skip
```

而不是让整个 workspace search 失败。

---

### 11.4 Result

每个 match 至少包含：

```text
relative_path
line_number
line_text
```

例如：

```text
src/auth.py:42
def verify_token(token):
```

完整内部 result：

```text
query
path
matches
truncated
```

---

### 11.5 Output Bound

结果达到上限：

```text
truncated = true
```

模型应该通过：

```text
更窄 path
更具体 query
file_glob
```

继续搜索。

v1 不需要复杂：

```text
search cursor
result page token
ranking engine
symbol index
```

---

### 11.6 Implementation

v1 baseline 可以直接使用 Python filesystem traversal + text search。

理由：

* primary workspace 是 small local project；
* 行为容易控制；
* 不依赖评测环境额外安装 `ripgrep`；
* 易于保持与 shared ignore/path rules 一致。

未来可以使用 `rg` 作为优化 backend，但：

> `search_text` semantic contract 不依赖 `rg` 的存在。

M1 不需要实现双 backend。

---

## 12. `read_file`

### 12.1 Purpose

`read_file` 读取一个明确的 UTF-8 text file。

读取采用：

> bounded line-range paging。

---

### 12.2 Schema

```python
read_file(
    path: str,
    start_line: int = 1,
    end_line: int | None = None,
)
```

行号为：

```text
1-indexed
inclusive
```

必须满足：

```text
start_line >= 1
```

如果提供 `end_line`：

```text
end_line >= start_line
```

---

### 12.3 Bounded Read

即使：

```text
end_line = None
```

也不意味着：

> 把整个任意大小文件全部塞进 ToolResult。

06 定义读取必须 bounded。

09 定义具体：

```text
default maximum lines
absolute maximum lines
maximum returned bytes
```

如果用户 / 模型请求范围超过最大值，Tool 可以截断到允许范围。

---

### 12.4 Result

至少包含：

```text
path
start_line
end_line
total_lines
content
truncated
next_start_line
```

如果已经读取 EOF：

```text
truncated = false
next_start_line = null
```

否则：

```text
truncated = true
next_start_line = <first unread line>
```

---

### 12.5 Model-Facing Line Numbers

`read_file` 的 observation 可以向模型展示：

```text
1 | import os
2 | import sys
3 |
4 | def foo():
5 |     ...
```

以方便 navigation 和 discussion。

但：

> 行号只属于 observation metadata，不属于 edit identity。

不得设计：

```text
edit_file(path, line=42, ...)
```

基于旧行号直接 mutation。

---

### 12.6 Failure Examples

```text
FILE_NOT_FOUND
NOT_A_FILE
BINARY_FILE_UNSUPPORTED
TEXT_DECODING_FAILED
FILE_READ_FAILED
```

Sensitive read 本身若能正常 resolve，不属于 Tool Operation Failure；是否需要确认由 03 决定。

---

## 13. `edit_file`

### 13.1 Purpose

`edit_file` 修改一个 existing UTF-8 text file。

v1 主编辑机制固定为：

> **Exact Text Replacement with Expected Match Count**

而不是：

* line-number mutation；
* fuzzy matching；
* custom patch DSL；
* unconstrained whole-file overwrite。

---

### 13.2 Schema

```python
edit_file(
    path: str,
    old_text: str,
    new_text: str,
    expected_count: int = 1,
)
```

约束：

```text
old_text must not be empty
expected_count >= 1
```

允许：

```text
new_text = ""
```

因为删除一段已知文本属于 replacement。

但它仍然只是：

```text
删除匹配文本内容
```

而不是 dedicated file deletion。

---

### 13.3 Edit Algorithm

在 action 已经过：

```text
validation
→ local preparation
→ Explicit Task Constraint
→ Risk Permission
```

并进入实际 execution 后，Tool：

1. 重新读取 target 的当前真实 bytes；
2. 验证其仍是支持的 UTF-8 text file；
3. decode 时不做 newline normalization；
4. 统计 `old_text` 的 exact occurrence；
5. 比较：

```text
actual_count
==
expected_count
```

6. 若不相等，拒绝修改；
7. 若一致，对所有这 `expected_count` 个 exact occurrence 做 replacement；
8. 写入新内容；
9. 返回结构化 edit summary。

---

### 13.4 Conflict Detection

模型通常先：

```text
read_file
```

获得 observation。

之后：

```text
edit_file(old_text=...)
```

必须验证 observation 仍然与当前 Workspace State 相符。

例如之前模型看到：

```python
foo = 1
```

但文件后来变为：

```python
foo = 2
```

此时：

```text
old_text = "foo = 1"
```

找不到。

返回：

```text
EDIT_TARGET_NOT_FOUND
```

不：

* 猜测最相似文本；
* 根据附近行号编辑；
* 自动修改 `foo = 2`；
* 尝试 fuzzy patch。

---

### 13.5 Ambiguous Match

模型希望修改一次：

```text
expected_count = 1
```

但当前文件存在三次 `old_text`。

返回：

```text
EDIT_MATCH_COUNT_MISMATCH
```

并告诉模型：

```text
expected_count
actual_count
```

模型应：

```text
re-read / use larger anchor
→ propose new edit
```

而不是 Runtime 选择“最像”的一个 occurrence。

---

### 13.6 Multiple Exact Replacements

如果模型明确需要对多个完全相同 occurrence 统一替换：

```text
expected_count > 1
```

只有：

```text
actual_count == expected_count
```

时才执行。

这避免：

```text
replace_all=true
```

导致模型低估 mutation scope。

---

### 13.7 Why Exact Replacement

该设计形成自然 optimistic concurrency behavior：

```text
Model observes old content
        ↓
Model proposes old_text → new_text
        ↓
Runtime verifies old_text still exists exactly as expected
        ↓
Mutation
```

相比 line-number edit：

```text
line positions stale
但 edit 仍可能成功到错误位置
```

Exact replacement 更容易 fail closed。

---

### 13.8 Write Mechanism

File mutation 应尽量避免 partial write。

推荐：

1. 在同目录生成 temporary sibling file；
2. 写入完整新 bytes；
3. flush / close；
4. 尽量保留 original file permission metadata；
5. 使用平台可用的 replace operation 替换 original。

目标是：

```text
old complete file
or
new complete file
```

而不是：

```text
truncated / half-written file
```

如果 replacement 失败：

```text
OPERATION_FAILURE
```

---

### 13.9 Line Ending Preservation

普通代码修改不应因为 Agent Tool：

```text
LF → CRLF
```

或：

```text
CRLF → LF
```

改变整文件。

因此实现应尽量：

```text
read bytes
→ UTF-8 decode without newline translation
→ exact string replacement
→ UTF-8 encode
```

不要使用会自动 universal-newline normalize 再重写整文件的默认 text I/O path。

如果文件原本混合 newline，v1 不主动规范化。

---

### 13.10 Result

成功结果至少包含：

```text
path
replacement_count
```

可以附带简短：

```text
bytes_before
bytes_after
```

等 observability metadata，但不要返回完整修改后文件作为 edit result。

如果模型需要确认，应重新：

```text
read_file
```

或执行 verification。

---

### 13.11 Failure Examples

```text
FILE_NOT_FOUND
NOT_A_FILE
BINARY_FILE_UNSUPPORTED
TEXT_DECODING_FAILED
EDIT_TARGET_NOT_FOUND
EDIT_MATCH_COUNT_MISMATCH
EDIT_WRITE_FAILED
```

---

## 14. `create_file`

### 14.1 Purpose

`create_file` 创建新的 UTF-8 text file。

---

### 14.2 Schema

```python
create_file(
    path: str,
    content: str,
)
```

---

### 14.3 Create-Only Semantics

`create_file` 固定为：

> create-only。

如果 target 已经存在：

```text
FILE_ALREADY_EXISTS
```

不得静默覆盖。

修改 existing file 必须使用：

```text
edit_file
```

这避免模型将：

```text
create / write
```

混成危险的 generic overwrite Tool。

---

### 14.4 Parent Directory

Lean v1 要求：

> parent directory 已经存在。

如果不存在：

```text
PARENT_DIRECTORY_NOT_FOUND
```

不：

* 自动 `mkdir -p`；
* 自动创建多层目录；
* 猜测用户希望的新项目结构。

若模型确实需要创建目录，可以在后续真实需求出现时决定是否增加 dedicated Tool；当前 v1 不提前实现。

---

### 14.5 Write Mechanism

由于目标不存在，可以直接以 exclusive-create semantic 创建。

必须避免 race：

```text
check not exists
→ another process creates
→ overwrite
```

因此实现应优先使用能够表达：

> create only if absent

的 OS file creation mode。

如果执行时 target 已存在：

```text
FILE_ALREADY_EXISTS
```

---

### 14.6 Result

成功至少返回：

```text
path
bytes_written
```

---

### 14.7 Failure Examples

```text
FILE_ALREADY_EXISTS
PARENT_DIRECTORY_NOT_FOUND
NOT_A_DIRECTORY
FILE_WRITE_FAILED
```

---

## 15. Whole-File Rewrite

v1 不提供：

```text
overwrite_file
write_file
replace_entire_file
```

用于覆盖 existing file。

原因：

* stale-content 风险高；
* 容易覆盖模型未观察到的 concurrent change；
* 对 primary bug-fix / small-feature workflow 非必要；
* exact replacement 已能覆盖主要 mutation。

如果确实需要整体替换 existing file，模型可以：

1. `read_file` 获取完整当前文件；
2. 将完整旧文本作为 `old_text`；
3. 将新文件作为 `new_text`；
4. `expected_count = 1`；
5. 调用 `edit_file`。

只要原文件在此期间发生变化：

```text
old_text mismatch
→ edit rejected
```

因此仍保留 stale-content protection。

如果后续真实任务证明这种方式成本明显过高，再考虑加入：

```text
version/hash protected whole-file replace
```

而不是无保护 overwrite。

---

## 16. `shell`

### 16.1 Purpose

`shell` 用于 Coding Agent 无法通过结构化 File Tool 完成的本地开发操作，例如：

```text
test
build
compile
lint
run small program
inspect project tooling
Git command
project-specific script
```

Shell 是通用 escape hatch。

---

### 16.2 Schema

```python
shell(
    command: str,
    cwd: str = ".",
    timeout_seconds: int | None = None,
)
```

`command` 是完整 shell command string。

v1 不使用：

```text
argv-only Tool
executable allowlist Tool
one command per Tool family
```

因为 Coding Agent 需要支持：

```text
pipes
redirection
chaining
project-specific scripts
```

实际语义由当前 Shell backend 提供。

---

### 16.3 `cwd`

`cwd` 必须：

* 指向 workspace 内 existing directory；
* 经过 shared workspace resolver；
* 默认 workspace root。

但：

> Shell cwd 只是 working directory，不是 filesystem sandbox。

获准启动的程序仍可能访问 cwd 外部资源。

该风险及权限语义由 03 owning。

---

### 16.4 Shell Backend Contract

06 定义 Shell execution mechanism 至少必须提供：

```text
full command execution
working directory
bounded timeout
noninteractive stdin
stdout capture
stderr capture
exit-code capture
filtered environment
best-effort process-tree termination
```

具体：

```text
Windows 默认 backend
POSIX 默认 backend
用户是否可以覆盖
```

由 09 负责。

---

### 16.5 `stdin`

v1：

```text
stdin = disabled / DEVNULL
```

Shell command 不得无限等待：

```text
password
confirmation
interactive prompt
editor
REPL input
```

需要真实用户回答时应使用：

```text
ask_user
```

而不是让 subprocess 与终端直接交互。

---

### 16.6 Environment

Shell child process 可以继承普通开发环境变量，但必须遵守 03 的 Runtime Secret filtering。

Shell Tool 不得自行决定：

```text
把 Agent Provider API key 注入 child
```

Environment filtering 发生在 Runtime / Shell execution boundary。

---

### 16.7 Timeout

每个 Shell execution 必须有有效 timeout。

如果模型没有提供：

```text
timeout_seconds
```

使用 09 的 configured default。

如果模型提供超出允许最大值的 timeout：

实现可以：

```text
clamp
```

或：

```text
validation reject
```

具体选择与默认值由 09 定义。

绝不允许：

```text
infinite timeout
```

---

### 16.8 Process Termination

Command timeout、Run cancellation 等情况下：

Runtime 对自己启动并能够识别的 process tree 做：

```text
best-effort termination
```

不声称能可靠阻止：

```text
detached background process
daemonized process
external process
```

03 已将该限制作为 Accepted Risk。

---

### 16.9 Output Capture

Shell 内部 result 至少包含：

```text
command
cwd
exit_code
stdout
stderr
stdout_truncated
stderr_truncated
```

06 owns：

* capture mechanism；
* truncation mechanism；
* Shell Tool result shape。

09 owns：

* 默认 byte / line limits。

07 owns：

* 哪部分最终进入模型 Context。

---

### 16.10 Truncation Mechanism

Shell 不应无限累计 stdout / stderr。

实现可以分别对：

```text
stdout
stderr
```

设置 bounded capture。

达到上限后：

```text
*_truncated = true
```

Runtime 必须继续确保 process 本身不会因为 Agent 不消费 pipe 而无界阻塞。

具体实现可以：

* bounded in-memory capture；
* streaming drain + bounded retained tail/head；
* temporary capture file + bounded projection。

M1 选择最简单且不会死锁的实现即可。

---

### 16.11 Outcome Semantics

如果 process 成功启动并正常结束：

```text
exit_code == 0
→ SUCCESS
```

如果 process 成功启动并观察到结束，但：

```text
exit_code != 0
→ UNSUCCESSFUL_COMMAND
```

例如：

```text
pytest test failure
compiler error
lint failure
program exit 1
```

以下属于：

```text
OPERATION_FAILURE
```

例如：

```text
PROCESS_START_FAILED
COMMAND_TIMEOUT
PROCESS_IO_ERROR
```

这里：

> test failed ≠ Tool failed。

失败的 test 是 Agent 后续迭代的重要 observation。

---

### 16.12 Shell Risk Policy

06 不维护一份独立 Shell risk matrix。

完整 normative risk policy 见 03。

06 只负责产生 03 policy 所需的 deterministic surface facts，并执行最终被允许的 exact action；它不维护第二张 permission matrix。

#### 16.12.1 Surface Classifier Contract

Shell LOCAL action preparation 使用一个小型、deterministic、best-effort helper：

```python
classify_shell_surface(
    command: str,
) -> ShellSurfaceFacts
```

`ShellSurfaceFacts` 是 immutable value object，概念字段为：

```text
recognized_actions
has_compound_syntax
has_unknown_segment
```

`recognized_actions` 只覆盖 03 当前 Risk Permission 实际关心、能够从 command surface 识别的动作：

```text
DEPENDENCY_INSTALL
NETWORK_ACCESS
GIT_MUTATION
GIT_REMOTE_WRITE
PRIVILEGE_ESCALATION
SYSTEM_CONFIGURATION
SHUTDOWN_OR_REBOOT
BACKGROUND_OR_DETACHED_PROCESS
INTERACTIVE_COMMAND
```

classifier 可以使用明确的 lexical / command-pattern recognition 识别常见 command、subcommand 和明显的 chaining / pipe / redirection 等 composition。对 compound command，它汇总各可识别 segment 的 facts；无法可靠识别的部分设置 `has_unknown_segment = true`。

classifier：

* 不返回 `ALLOW / CONFIRM / DENY`；
* 不执行 Shell expansion；
* 不尝试证明程序真实运行时 side effect；
* 不使用 LLM；
* 不实现完整 Shell grammar 或 AST parser。

最终 preparation 至少向 `PreparedToolCall` 提供：

```text
validated command
resolved cwd
ShellSurfaceFacts
```

PolicyEngine 再依据 03 对 recognizable action、compound command 和 unknown segment 的 normative policy 返回 `ALLOW / CONFIRM / DENY`。

---

## 17. `ask_user`

### 17.1 Purpose

`ask_user` 是 v1 唯一的 `INTERACTION` Tool。

用于：

* Task Intent 不够清楚；
* Agent需要用户选择；
* hard constraint 需要进一步澄清；
* 用户必须提供缺失信息。

---

### 17.2 Schema

```python
ask_user(
    question: str,
)
```

`question`：

```text
non-empty
```

并受到普通 Tool argument size limit。

---

### 17.3 Execution Semantics

`ask_user` 不直接执行：

```python
input()
```

ToolCall 经 Runtime：

```text
lookup
→ validation
→ ToolKind.INTERACTION
→ WAITING_FOR_USER(CLARIFICATION)
→ UserInteraction.ask()
```

用户回答后 Runtime 构造：

```text
ToolResult(
    outcome = SUCCESS,
    content = {
        "answer": <user text>
    }
)
```

然后当前 batch 结束，由下一 Model Turn 使用该 observation。

---

### 17.4 Not a Local Action

`ask_user`：

* 不产生 `PreparedToolCall`；
* 不进入 LOCAL Explicit Task Constraint pipeline；
* 不进入 LOCAL Risk Permission pipeline；
* 不执行 LocalTool `execute()`；
* 不产生 `ToolExecutionResult`。

Permission Confirmation：

```text
CONFIRM
```

也不是 `ask_user`。

它是 Runtime 对一个 LOCAL ToolCall 的控制流程。

---

## 18. Local Action Preparation

### 18.1 General Flow

通过 validation 的 LOCAL ToolCall：

```text
Validated ToolCall
        ↓
prepare local action
        ↓
PreparedToolCall
        ↓
Explicit Task Constraint
        ↓
Risk Permission
        ↓
execution
```

`PreparedToolCall` 的通用 contract 由 05 定义。

06 只定义每类 Tool 需要产生哪些 dynamic facts。

---

### 18.2 File Tool Facts

File Tool preparation 至少可以提供：

```text
ResolvedPath
canonical / resolved target
workspace-relative path
affected_paths
sensitive facts
protected facts
```

mutation Tool：

```text
affected_paths
```

必须足够支持 04 定义的 `WRITE_SCOPE` enforcement。

---

### 18.3 Static Capability Is Not Repeated

静态 Tool category 由：

```text
ToolSpec.capabilities
```

表达。

例如：

```text
read_file
→ FILE_READ

edit_file
→ FILE_MUTATION

shell
→ COMMAND_EXECUTION
```

`PreparedToolCall.operation_facts` 不再重复保存：

```text
READ
MUTATION
COMMAND
```

这种静态分类。

---

### 18.4 Shell Facts

Shell preparation 至少生成：

```text
validated command text
resolved cwd
ShellSurfaceFacts surface_facts
```

供：

* current Explicit Task Constraint；
* 03 Shell Risk Permission implementation；
* execution

共同消费。

Shell 不声称能够列举 command 内部所有实际 filesystem side effect。

---

## 19. Preparation Failure

LOCAL action preparation 不是 guaranteed success。

例如：

```text
filesystem metadata I/O error
target disappears during preparation
resolver I/O failure
required local prerequisite unavailable
```

这些属于 expected local operational failure。

v1 不新增：

```text
PREPARATION_ERROR
```

ToolOutcome。

统一使用：

```text
OPERATION_FAILURE
```

并通过 `ToolError.code` 提供细节。

---

### 19.1 Preparation vs Policy Fact

以下情况：

```text
target outside workspace
target is sensitive
target is protected
```

如果 resolver 已成功获得该事实：

> 不属于 preparation failure。

它们进入后续 constraint / policy decision。

---

### 19.2 Unexpected Preparation Exception

unexpected helper / Tool implementation bug：

```text
unexpected exception
        ↓
local-action isolation boundary
        ↓
OPERATION_FAILURE
ToolError.code = INTERNAL_TOOL_ERROR
```

详细 traceback 只用于内部 observability。

不默认把 Python traceback 原样发送给模型。

---

## 20. Tool-Specific Error Codes

以下为 v1 concrete Tool layer 应支持的主要错误 code。

通用 Runtime outcome 仍由 05 owning。

---

### 20.1 File and Path

```text
FILE_NOT_FOUND
DIRECTORY_NOT_FOUND
FILE_ALREADY_EXISTS
PARENT_DIRECTORY_NOT_FOUND
NOT_A_FILE
NOT_A_DIRECTORY


BINARY_FILE_UNSUPPORTED
TEXT_DECODING_FAILED

FILE_READ_FAILED
FILE_WRITE_FAILED
DIRECTORY_LIST_FAILED
```

---

### 20.2 Edit

```text
EDIT_TARGET_NOT_FOUND
EDIT_MATCH_COUNT_MISMATCH
EDIT_WRITE_FAILED
```

---

### 20.3 Search

```text
INVALID_SEARCH_PATTERN
SEARCH_FAILED
```

---

### 20.4 Shell

```text
PROCESS_START_FAILED
COMMAND_TIMEOUT
PROCESS_IO_ERROR
```

`exit_code != 0`：

不是 error code 驱动的 OPERATION_FAILURE，

而是：

```text
UNSUCCESSFUL_COMMAND
```

---

### 20.5 Internal

```text
INTERNAL_TOOL_ERROR
```

---

### 20.6 Policy Errors

`WORKSPACE_BOUNDARY` 不是 Tool Operation Failure，也不是 File / Path Tool-specific operation error。resolver 成功产生 `is_within_workspace = false` 时，该事实进入 PolicyEngine，并由 Runtime 表示为：

```text
ToolResult(
    outcome = POLICY_REJECTED,
    reason_code = WORKSPACE_BOUNDARY
)
```

其 normative policy semantics 由 03 / 05 owning。Sensitive / Protected classification 同样是 policy fact，仅识别出这些 facts 不产生 `OPERATION_FAILURE`。

其他 policy reason 例如：

```text
USER_REJECTED_CONFIRMATION
SENSITIVE_PATH_CONFIRMATION
protected-path rejection
explicit constraint rejection
```

属于 03 / 05 Policy contract。

06 不重新建立完整 policy error taxonomy。

---

## 21. Tool Output Bounding

以下 Tool 可能产生较大输出：

```text
list_directory
search_files
search_text
read_file
shell
```

必须 bounded。

结构化 result 应在发生 truncation 时显式包含：

```text
truncated = true
```

对于：

```text
read_file
```

还提供：

```text
next_start_line
```

用于 deterministic continuation。

搜索 Tool 不需要 v1 cursor。

模型可以通过缩小：

```text
path
glob
query
file_glob
```

进行下一次搜索。

---

## 22. Internal Tool Result vs Model-Visible Observation

06 定义 Tool 的完整内部 result。

例如 Shell result：

```text
stdout
stderr
exit_code
truncation metadata
```

但：

> Internal Tool Result ≠ 模型永久可见的完整 Context。

完整路径：

```text
Tool execution
      ↓
internal Tool result
      ↓
Runtime ToolResult
      ↓
07 Context / ToolResult projection
      ↓
model-visible observation
```

07 可以进一步：

* truncate；
* summarize；
* retain only relevant part；
* drop old large observations。

06 不 owning Context policy。

---

## 23. Tool Registration

所有 Tool 在 startup 通过 05 的 `ToolRegistry` 注册。

Registry startup validation 至少保证：

```text
name unique
ToolSpec valid
input schema generated
ToolKind valid
capabilities valid
```

06 的 concrete tools 只提供：

```text
ToolSpec
argument validation
preparation behavior
execution behavior
```

Registry 不变成 plugin manager。

---

## 24. Rejected Tools

### 24.1 Dedicated Delete Tool

v1 不提供：

```text
delete_file
delete_directory
```

主要原因：

* 不属于 M1 核心 inspect-edit-verify flow；
* 删除在 03 中默认需要 CONFIRM；
* 可以避免第一版增加 destructive Tool surface。

如后续加入，必须继续使用 03 的 Risk Permission。

---

### 24.2 Git Tool Family

v1 不提供：

```text
git_status
git_diff
git_commit
git_checkout
git_push
```

专用 Tool family。

Git 通过：

```text
shell
```

执行，并遵守 03 对：

```text
read-only Git
local mutation
remote write
```

的 permission policy。

---

### 24.3 Custom Patch DSL

v1 不实现：

```text
apply_patch DSL
unified-diff patch engine
fuzzy hunk matcher
AST patch engine
```

原因：

* parser / application semantics 显著增加 M1 implementation surface；
* failure / multi-hunk atomicity 需要额外设计；
* exact replacement 已足够覆盖 primary v1 tasks。

真实任务如果证明 exact replacement 明显阻碍 Agent 成功率，再基于 evidence 决定是否增加 patch mechanism。

---

### 24.4 Generic Existing-File Write Tool

不提供：

```text
write_file(path, content)
```

这种同时：

```text
create
+
overwrite
```

的 generic Tool。

采用：

```text
create_file → create only
edit_file   → mutate existing with expected old content
```

使 mutation intent 更明确。

---

### 24.5 Language-Specific Tools

不建立：

```text
python_tool
cpp_tool
java_tool
node_tool
```

语言专用 Tool。

语言相关：

```text
test
compile
run
lint
```

通过 Shell + project context 完成。

---

### 24.6 Binary / Media Tools

v1 不提供：

```text
image reader
PDF reader
archive editor
binary editor
OCR
```

---

## 25. v1 Tool Invariants

v1 Tool layer必须保持：

1. 所有 File Tool 共用一个 workspace path resolver。
2. Path containment 不使用字符串 prefix 判断。
3. Existing 与 New Path 使用适合各自语义的 resolution strategy。
4. `.gitignore` / discovery ignore 不是 Permission Boundary。
5. Sensitive Path 不进入默认 content discovery。
6. `.git` internal metadata 不允许普通 File Tool mutation。
7. File Tool 主要处理 UTF-8 text workflow。
8. Binary / unsupported encoding 不以乱码形式发送给模型。
9. `list_directory` 永远只列一层。
10. `search_files` 只根据 path / filename glob 定位。
11. `search_text` 默认 literal search，regex 必须显式开启。
12. 所有 discovery result 都必须 bounded。
13. `read_file` 使用 bounded line-range paging。
14. Read observation 可以带行号，但行号不是 mutation identity。
15. `edit_file` 使用 exact text replacement。
16. `edit_file` 要求 `actual_count == expected_count`。
17. Edit conflict 时 Runtime 不进行 fuzzy guess。
18. `create_file` 为 create-only。
19. `create_file` 不静默 overwrite existing file。
20. `create_file` 不自动创建 parent directory tree。
21. File mutation 尽可能避免 partial write。
22. 普通 text edit 不应自动改变整个文件的 line-ending style。
23. Shell 接收完整 command string。
24. Shell cwd 必须位于 workspace 内。
25. Shell cwd 不被描述为 filesystem sandbox。
26. Shell stdin 为 noninteractive。
27. Shell execution 必须有 timeout。
28. Shell stdout / stderr 必须 bounded。
29. Shell process 成功结束但 exit code 非零属于 `UNSUCCESSFUL_COMMAND`。
30. Command timeout / process launch failure 属于 `OPERATION_FAILURE`。
31. `ask_user` 是 InteractionTool。
32. `ask_user` 在 validation 后离开 LOCAL pipeline。
33. Permission Confirmation 不是 Tool。
34. `PreparedToolCall` 是 immutable value object，不是 subsystem。
35. Static operation category 由 ToolSpec capability 表达。
36. Dynamic operation facts 不重复静态 Tool category。
37. Expected preparation failure 使用现有 `OPERATION_FAILURE` taxonomy。
38. Tool 不拥有 Agent retry。
39. Tool 不拥有 batch semantics。
40. Tool 不拥有 Context policy。
41. Tool 不拥有 Runtime lifecycle。
42. Tool 不自行扩大用户 Task Scope。
43. Safety decision 始终由 Runtime / PolicyEngine 依据 03 contract 处理。

---

## 26. M1 Implementation Scope

M1 的目标不是一次实现完整成熟 Tool subsystem，而是尽快跑通真实 vertical slice：

```text
User
↓
Model
↓
ToolCall
↓
Tool execution
↓
ToolResult
↓
Model
↓
Final
```

在 Tool 层，推荐实现顺序：

```text
1. shared workspace resolver

2. read_file

3. shell

4. list_directory

5. search_files

6. search_text

7. edit_file

8. create_file

9. ask_user
```

其中第一条最小 vertical slice 可以只有：

```text
Model
→ read_file
→ ToolResult
→ Model
→ Final
```

之后立即接：

```text
shell
```

验证完整 observation loop。

随后实现：

```text
search
→ read
→ edit
→ shell verification
→ iterate
```

形成真实 Coding Agent 闭环。

---

## 27. Implementation Discipline

M1 implementation 应只实现本文批准的 contract。

不要提前实现：

```text
Tool plugin API
event hooks
tool middleware
generic Action abstraction
Git Tool family
patch engine
fuzzy edit
AST edit
binary support
advanced search ranking
symbol index
transactional multi-file edit
directory mutation family
parallel Tool execution
background Tool execution
language-specific Tool framework
```

如果实现过程中发现当前 Tool contract 无法完成真实 primary coding task，应先记录具体失败案例，再决定是否修改 06。

不得仅因为：

> “以后可能需要”

提前增加 abstraction。

---

## 28. Deferred Decisions

### `07-context-and-prompt-policy.md`

由 07 定义：

* Tool descriptions 中的模型行为 guidance；
* 什么时候优先 search / read；
* 如何鼓励 stale edit 后重新读取；
* ToolResult 的 model-visible projection；
* 大量 Shell / Search observation 如何进入 Context；
* Session 中哪些 Tool observation 跨 Run 保留。

---

### `08-verification-testing-and-demo.md`

由 08 定义：

* 修改代码后何时应该运行 verification；
* Agent 在什么 evidence 下可以声称完成；
* Tool 的 unit / integration tests；
* edit conflict test；
* path resolver security test；
* Shell timeout / output-bound test；
* realistic bug-fix demo task。

---

### `09-cli-observability-and-configuration.md`

由 09 定义具体默认值，包括：

```text
read maximum lines
search maximum matches
directory listing maximum entries
Shell stdout limit
Shell stderr limit
default command timeout
maximum command timeout
default Shell backend
platform-specific backend selection
Tool event CLI rendering
```

06 不提前固定这些数值。

---

## 29. ADR References

如果以下决定最终保留到提交版本，应在 `10-architecture-decisions.md` 记录 Lightweight ADR：

### Exact Text Replacement as Primary Edit Mechanism

Owner doc:

```text
06-toolset-and-file-editing.md
```

### Structured File Tools Plus Shell Escape Hatch

Owner doc:

```text
06-toolset-and-file-editing.md
```

### Create-Only File Creation

Owner doc:

```text
06-toolset-and-file-editing.md
```

### Single Shared Workspace Path Resolver

Normative path semantics owner:

```text
03-safety-and-execution-boundaries.md
```

Implementation owner:

```text
06-toolset-and-file-editing.md
```

ADR 格式遵循：

```text
Decision
Why
Main Alternative
Consequence
Owner doc
```
