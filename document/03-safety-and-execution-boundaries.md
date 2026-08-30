# Safety and Execution Boundaries

## 1. Safety Model

v1 采用 **risk-based permission model**，并区分三类不同强度的约束：

1. **Runtime-Enforced Explicit Task Constraints：**Runtime 保存用户明确给出的限制，并阻止与这些已记录约束直接冲突的动作。
2. **Task Intent and Semantic Relevance：**一般任务意图和动作是否与开放式自然语言任务相关，主要依赖模型进行语义判断，不被声称为 deterministic security boundary。
3. **Risk Permission：**动作的风险等级是 `ALLOW`、`CONFIRM` 还是 `DENY`，由 Runtime policy 强制实施。

本文将此前容易混淆的 **Task Authorization** 拆为前两层：Runtime 只对用户明确给出的 task constraints 作确定性强制；一般任务意图和细粒度相关性保留为 model-dependent semantic policy。

LLM 不直接拥有本地执行权限。模型产生的 Tool Call 或 Shell Command 均被视为：

> **Untrusted Action Proposal**

对于已经由 Runtime 完成 validation 和 local action preparation 的 `LOCAL` action，安全决策顺序为：

```text
Runtime-enforced Explicit Task Constraints
              ↓
Runtime-enforced Risk Permission
              ↓
      permitted local execution
```

本文不拥有 ToolRegistry、ToolKind、PreparedToolCall 或完整 dispatch algorithm。完整 Runtime lifecycle / dispatch order 由 `04-agent-runtime-model.md` 定义，组件与数据 contract 由 `05-component-and-protocol-contracts.md` 定义。

### 1.1 Runtime-Enforced Explicit Task Constraints

Runtime 至少维护：

```text
initial_task
explicit_user_clarifications
explicit_scope_updates
explicit_task_constraints
```

`explicit_task_constraints` 只记录用户明确表达、能够被 Runtime 具体执行的限制，例如：

```text
只 review，不要修改文件
不要运行命令
只修改 tests/ 目录
```

Runtime 对已记录的明确限制实施 Hard Task Constraint，例如阻止直接违反“不要修改文件”的 write / edit / delete，或拒绝超出明确路径限制的 File Tool 操作。只有用户明确输入可以增加、扩大、缩小或解除这些限制；模型不能自行修改 Runtime Task State。

Runtime 不得仅依据模型自行推断而新增 hard constraint。无法可靠规范化的用户意图应保留为 semantic policy，或通过澄清取得明确边界。

一般自然语言任务不被强制归类为通用的 `READ_ONLY | MUTATING` enum。诸如 `review this code` 的默认行为由任务语义和 Prompt Policy 约束，而不是由 Runtime 声称拥有可靠的自然语言权限分类器。语义不清且潜在动作可能扩大用户意图时，Agent 应请求澄清。

一次 Risk Permission confirmation 只授权展示的具体动作，不自动扩大 Task Scope，也不修改 `explicit_task_constraints`。

04 是 Task State、explicit constraints 与 scope-update lifecycle 的 canonical owner。03 只规定已记录的明确限制必须形成 Runtime 可执行的硬约束。

该边界仍不构成 Shell sandbox。Runtime 能阻止直接识别的 mutation，但获准执行的程序可能包含隐藏 side effect；这一限制保留在 Shell Accepted Risks 中。

### 1.2 Semantic Relevance

Semantic Relevance 回答：

> **Is this action reasonably relevant to the current user task?**

它涉及对自然语言任务意图、动作目的和文件相关性的语义判断。Runtime 不能严格证明 `database.py` 是否与“修复登录问题”等开放式编程任务相关，也不能可靠地把任意用户请求确定性分类为“允许修改”或“只读”。因此该判断是 model-dependent soft policy，不与 `DENY`、workspace containment 或 explicit task constraints 等 Runtime-enforced boundary 声称同等强度。

用户是任务意图的 authority。原始任务、明确澄清和明确 scope update 共同构成模型判断相关性的依据。Agent 不确定某项工作是否构成范围扩展时，应询问用户，而不是自行扩大任务。

07 负责 Semantic Relevance 的 Prompt 行为指导；它不负责定义或修改 Runtime Task State。

例如：

- `review this code` 应被模型理解为只进行分析，不主动修改文件。
- `review and fix the issues` 在任务语义上包含与修复直接相关的修改。
- `add tests for this function` 在任务语义上包含创建或修改相关测试文件。

用户是任务意图的 authority，但不是 Safety Policy 的 authority。用户任务本身不能绕过固定 `DENY` 规则。例如，即使用户请求使用管理员权限修改系统文件，v1 仍不得执行。

风险确认只表示用户同意执行所展示的具体高风险动作，不应被自动解释为对其他动作或整个 Session 的范围授权。

### 1.3 Risk Permission

Risk Permission 在 Explicit Task Constraints 和通用 validation 通过后判断：

Risk Permission 是由 Runtime 强制实施的 policy boundary。`DENY`、workspace containment、确认状态和可确定的参数规则不能由 LLM 语义判断覆盖。

- `ALLOW`：从风险权限角度不需要额外确认，Runtime 可以自主执行。
- `CONFIRM`：必须获得用户对当前具体动作的一次性确认。
- `DENY`：v1 不允许执行。

`CONFIRM` 必须遵守：

- 展示将要执行的完整动作及其关键参数。
- 对 Shell 展示完整 command string。
- 默认只授权当前动作，不产生永久授权或 Session 级授权规则。
- 动作或参数发生变化后，原确认失效，必须重新确认。
- 用户拒绝后不得以等价变形绕过拒绝；后续行为由 Runtime Model 决定。

## 2. Trust Boundary

### 2.1 Assets

v1 需要保护的主要资产包括：

- Workspace integrity
- User data outside workspace
- Git repository state and history
- Runtime secrets
- Local system integrity
- External and network side effects

### 2.2 Trusted Control Plane

以下部分属于 Runtime 的可信控制面：

- Agent Runtime 自身代码
- 固定的安全策略
- Runtime-enforced Permission 与 Validation 逻辑
- Runtime 维护的用户任务、确认结果和显式范围变更记录
- 用户启动时明确指定并通过验证的 workspace root
- Runtime 自己维护的权限与执行状态
- 通过受控确认界面取得的用户当前动作决定

“可信”表示这些组件负责实施策略，不表示它们不存在实现缺陷。

### 2.3 Untrusted Inputs

以下内容均不能直接获得执行权限：

- LLM 输出
- Tool arguments
- Shell command
- 用户任务中的命令或数据内容
- Workspace 内代码
- README、注释及其他项目文本
- Shell stdout / stderr
- 外部网络返回内容
- Git Repository 中已有内容

用户输入可以定义任务意图，但不能修改 Runtime Safety Policy。Workspace 位于允许访问的目录内，也不表示其中内容可信。

### 2.4 Sensitive Data

以下内容属于敏感数据：

- 模型 API key
- Access token
- Password
- Runtime 使用的其他 Secret
- Workspace 内可能包含的项目凭据或私钥

敏感数据不应主动进入模型 Context，也不应无条件传递给 Agent 启动的子进程。

## 3. Permission Matrix

本表在 Explicit Task Constraints 和通用边界验证通过后适用。`ALLOW` 只表示该动作不因风险级别要求额外确认，并不替代 Task Intent 或 Semantic Relevance 判断。

| Action | Policy | Conditions |
|---|---|---|
| 查看 workspace 目录 | `ALLOW` | 受 discovery、protected path 和 output limit 约束 |
| 搜索 workspace 内容 | `ALLOW` | 不返回 Sensitive Path 内容 |
| 读取 workspace 内普通文本文件 | `ALLOW` | 仅限 resolved workspace boundary 内 |
| 读取 Sensitive Path | `CONFIRM` | Agent 应仅在合理判断任务需要时提出；必须提示内容可能发送给模型 |
| 创建普通文件或目录 | `ALLOW` | 仅限 workspace 内且符合当前任务 |
| 修改 workspace 内普通文件 | `ALLOW` | 仅限 workspace 内且符合当前任务 |
| 移动或重命名普通 workspace path | `ALLOW` | source 与 destination 都通过 canonical boundary、Protected / Sensitive 和 WRITE_SCOPE 检查；destination 不得存在 |
| 创建或修改 Sensitive Path | `CONFIRM` | Agent 应仅在合理判断任务需要时提出；必须提示敏感内容及覆盖风险 |
| File Tool 读取 `.git` 内部文件 | `CONFIRM` | Agent 应仅在合理判断任务需要时提出；仍受 Sensitive Path 规则约束 |
| File Tool 修改 `.git` 内部文件 | `DENY` | Git 状态修改通过受控 Git 操作完成 |
| 删除普通文件或空目录 | `CONFIRM` | `delete_path` 展示一个精确目标并只授权一次 execution attempt；不提供批量或 recursive 参数 |
| 删除 workspace root 或 non-empty directory | `DENY` | 不形成 recursive delete / `rm -rf` 等价能力 |
| 普通测试命令 | `ALLOW` | 仍受 Shell Accepted Risks 约束 |
| 普通编译 / 构建命令 | `ALLOW` | 仍受 Shell Accepted Risks 约束 |
| 普通程序执行 | `ALLOW` | 仍受 Shell Accepted Risks 约束 |
| 安装或升级依赖 | `CONFIRM` | 系统级提权安装仍为 `DENY` |
| 明显网络访问 | `CONFIRM` | 仅能 best-effort 识别 |
| Git `status` / `diff` / `log` 等只读操作 | `ALLOW` | 不修改工作区或仓库状态 |
| Git 修改工作区或仓库状态 | `CONFIRM` | 展示完整 Git 操作 |
| Git 远程写操作 | `CONFIRM` | 包括 `push` 等操作 |
| File Tool 访问 workspace 外路径 | `DENY` | 包括 symlink escape |
| sudo / 管理员提权 | `DENY` | 包括可直接识别的等价提权请求 |
| 修改系统级配置 | `DENY` | 直接识别的系统级修改 |
| shutdown / reboot 等系统操作 | `DENY` | 直接识别的系统控制动作 |
| 无界等待 stdin 的交互式命令 | `DENY` | v1 Shell 为 non-interactive |
| 显式创建无人管理的长期后台进程 | `DENY` | 受控子进程清理由 Runtime best-effort 管理 |

产品层面的默认体验应使 Primary Use Case 中常见的“检查代码 → 有限修改 → 非交互式验证”闭环能够自主进行。`CONFIRM` 应集中用于具有显著更高的 destructive、external、persistent 或 sensitive impact 的动作，而不是普通 Coding 流程中的每一步。

### 3.1 Shell Risk Classification

Shell 中的高风险动作识别属于 **best-effort risk classification**，不是 security sandbox。

Runtime 可以识别 action 直接表达的明显行为，例如：

```text
pip install ...
git push ...
rm ...
curl ...
```

但不能仅根据：

```text
python script.py
```

可靠推断脚本内部全部 side effect。

### 3.2 Compound Command

对于能够解析出的复合命令，整体采用各组成动作中的最高风险等级：

```text
pytest       → ALLOW
git push     → CONFIRM
whole command → CONFIRM
```

若任一可识别组成动作属于 `DENY`，则整条命令为 `DENY`。确认时必须展示完整 command string。任何改写，包括参数、重定向、管道或子命令变化，都会使原确认失效。

复杂 Shell 语法、间接调用和程序内部行为可能无法被完整分类，该限制属于 Accepted Risk。

### 3.3 Conservative Fallback

当 Runtime 无法可靠完成风险分类，并且 command 明显包含复合 Shell 语义或可能产生外部 side effect 时，整体提升为 `CONFIRM`，而不是乐观地按 `ALLOW` 执行。

典型情况包括无法可靠分析的：

- command chaining、pipe 或重定向
- subshell、command substitution 或嵌套 Shell
- 复杂 PowerShell / Shell 表达式
- 同时混合多个动作且边界不清晰的 command

未知但形式简单的普通可执行程序不因为名称未知而自动进入 `CONFIRM`；它仍可以按普通程序执行处理，并受 Shell 非 sandbox 的 Accepted Risks 约束。

分类结果遵循：

```text
明确识别为普通执行                → ALLOW
明确识别为高风险行为              → CONFIRM / DENY
复杂组合且无法可靠完成风险分类      → CONFIRM
简单未知程序                      → ALLOW + Accepted Risk
```

## 4. Workspace Boundary

### 4.1 Workspace Root

每次启动时，用户必须明确指定一个已经存在的本地目录作为 workspace root。它可以是：

- Git Repository
- 非 Git 普通目录
- 空目录

Runtime 在启动时使用平台路径 API 将该目录解析为稳定的 canonical workspace root。用户指定不存在的路径时，v1 不静默创建 workspace。

Agent 可以在绑定的 workspace 内创建项目文件和目录。一个 Session 只绑定一个 workspace root。

### 4.2 Existing Target Resolution

访问已经存在的目标时：

1. 使用平台路径 API 解析目标的 canonical / resolved path。
2. 使用路径组件或 `relative-to` 等路径语义进行 containment check。
3. 只有最终目标仍位于 canonical workspace root 内部时才允许访问。

不得使用字符串前缀判断 containment。例如 `/home/project-evil` 不能因为字符串以 `/home/project` 开头而被视为 workspace 内路径。

相对路径以 workspace root 为基准。绝对路径只有在最终解析后仍位于 workspace 内时才允许。`..` 不通过字符串黑名单判断，而通过最终路径 containment 判断。

### 4.3 New Target Resolution

创建尚不存在的文件或目录时：

1. 找到待创建路径最近的已存在父目录。
2. 解析该父目录的 canonical path。
3. 确认父目录位于 canonical workspace root 内。
4. 使用路径组件语义验证剩余待创建路径不会逃逸 workspace。
5. 在执行创建前再次使用可用的平台路径检查。

该规则避免依赖对不存在目标执行 canonicalization，也避免使用不可靠的字符串前缀判断。

### 4.4 Symlink

Symlink 不被全面禁止，但不能绕过 File Tool 的 workspace containment。

对已存在目标，安全边界以 resolved target path 为准。Workspace 内指向外部位置的 symlink 不能用于读取、修改或删除外部目标。

对新建目标，必须验证最近存在父目录的 resolved path。正常 symlink 使用平台路径 API 处理。

恶意并发替换引发的 TOCTOU、复杂 Windows junction / reparse point edge case 不在 v1 完整解决范围内，列为 Accepted Risk。

## 5. Workspace File Model

### 5.1 Path Classes

Workspace Boundary 只回答“路径是否位于 workspace 内”，不表示所有 workspace 内容都可以无条件进入模型 Context。

v1 区分：

- **Normal Path：**普通项目文件，按 Permission Matrix 访问。
- **Protected Path：**可能破坏工具不变量或绕过受控操作的内部路径。
- **Sensitive Path：**可能包含凭据、私钥或其他不应自动发送给模型的内容。

#### Protected Path

`.git` 内部文件属于 Protected Path：

- Discovery 和 Search 默认跳过 `.git`。
- File Tool 直接修改 `.git` 内部文件为 `DENY`。
- Git 状态变更通过受控 Git 命令或后续 Git Tool 执行。
- 对 `.git` 内部文件的直接读取不获得普通文本文件的 blanket `ALLOW`；如确有任务需要，应按具体内容和敏感性处理。

#### Sensitive Path

可能的 Sensitive Path 包括：

```text
.env
*.pem
*.key
id_rsa
credentials*
```

默认行为：

- Search 不返回其内容。
- Discovery 可以隐藏，或只报告文件存在而不返回内容。
- `read_file` 不自动 `ALLOW`。
- 创建、读取或修改均不自动 `ALLOW`。
- Agent 只有在合理判断当前任务需要访问该路径时才应请求 `CONFIRM`；这一“是否相关”属于模型的 soft semantic policy。
- Runtime 的安全边界是任何实际 Sensitive Path 访问都必须获得对当前具体动作的显式确认，而不是 Runtime 能确定性证明其任务相关性。
- 确认必须提示该文件可能包含 Secret，读取内容可能被发送给模型。
- 写入确认还必须提示可能覆盖现有敏感配置或引入新的 Secret。

具体 pattern、例外和展示方式由 06 与 09 决定。Shell 仍可能读取或修改这些路径，该限制属于 Accepted Risk。

### 5.2 Text and Binary Files

通用 File Tool 主要面向文本形式的软件项目文件。Binary 内容默认不直接返回给模型；检测到 binary 后返回明确状态，而不是把原始二进制放入 Context。

Binary detection 的具体实现和是否提供特殊处理能力由 06 决定。v1 不为 binary reverse engineering 建立专用能力。

### 5.3 Large Files

任何单次文件读取都必须有界。大型文件不得无界进入 Tool Result 或模型 Context。

具体单次字节限制、行数限制和 range read 接口由 06 决定。03 只规定：

> **任何一次文件读取都不得产生无界输出。**

### 5.4 Discovery and Ignore Rules

Recursive Listing、Search 和 Code Discovery 默认应避免明显噪声或生成内容：

- 尊重项目 `.gitignore`
- 默认跳过 `.git`
- 可以跳过少量明显缓存、依赖或构建目录

具体 ignore 集合由 06 决定。

Discovery ignore 是性能和相关性优化，不是 Permission Boundary。Sensitive / Protected Path Policy 是独立安全规则，不能用 `.gitignore` 替代。

## 6. Shell Execution Model

### 6.1 Security Model

v1 使用：

> **Risk-controlled local shell without strong OS-level sandboxing**

Shell 直接运行在用户本地开发环境中，以复用现有 Compiler、Runtime、Package Manager、Virtual Environment、Build Tool、Test Framework 和项目依赖。

v1 不使用 Docker 或其他 OS-level sandbox 作为 Shell 的强制运行前提。

### 6.2 Security Claim

File Tool 可以实施严格的 workspace containment，Shell 不能提供同等 filesystem isolation。

即使 `cwd = workspace root`，Shell 中运行的程序仍可能在当前用户 OS 权限范围内访问 workspace 外资源。因此：

> `cwd=workspace` 是默认工作目录，不是 sandbox。

v1 不声称 Generic Shell Execution 被完全限制在 workspace 内。

### 6.3 Command Model

Shell Tool 接受完整 command string，以支持真实开发环境中的正常命令，例如：

```bash
pytest -q
cmake -S . -B build && cmake --build build
npm test
git diff
python scripts/check.py
```

v1 不采用覆盖全部语言生态的 command allowlist 作为主要安全机制，因为它会限制 language-neutral 能力，同时无法为允许的解释器或项目程序提供真正的 filesystem isolation。

### 6.4 Shell Backend

每个 Session 使用一个明确的本地 Shell backend。默认 backend 根据操作系统选择，Runtime 必须知道当前 backend，并能让模型使用与环境一致的命令语法。

v1 不自行定义跨平台统一 Shell 语言。Windows / Unix 的具体默认 backend 和配置方式由 09 决定。

### 6.5 Working Directory

Shell command 默认使用：

```text
cwd = workspace root
```

默认 cwd 不改变 Session 绑定的 workspace，也不构成 filesystem sandbox。

## 7. Shell Environment

Shell 子进程需要尽量复用用户真实开发环境，因此 v1 不采用极端 environment allowlist。

环境变量过滤分为三层：

1. **Runtime 明确知道的 Secret：**必须移除，例如当前模型 API key。
2. **用户显式配置的 sensitive env names：**必须移除。
3. **名称包含 TOKEN / SECRET / PASSWORD 等特征的启发式检测：**只能作为可配置的 defense-in-depth，不是可靠的 Secret detector。

在过滤以上内容后，Runtime 尽量保留正常开发需要的环境，例如 `PATH`、临时目录、虚拟环境和语言工具链配置。

Secret filtering 无法保证发现任意命名的全部 Secret，也可能影响确实依赖敏感变量的项目命令。额外环境传递和配置方式由 09 决定。

## 8. Shell Resource and Lifecycle Limits

所有 Shell command 必须由 Runtime 控制生命周期。

### 8.1 Timeout

每条 command 都必须存在最长执行时间。超时后 Runtime 应终止当前受控执行，并把 timeout 作为明确结果返回。

具体默认 timeout 由 09 定义；timeout 在 Runtime Error Model 中的语义以 04 为准。

### 8.2 Output Limit

stdout 和 stderr 不得无界返回。Runtime 必须设置输出上限，并在截断时明确标记 truncated。

03 只拥有“Shell output 必须有界”的 normative requirement。stdout / stderr capture、截断机制与 Tool result shape 由 06 定义；进入模型 Context 的 projection、retention 与 compression 由 07 定义；默认 output limits、用户配置及日志/展示默认值由 09 定义。

### 8.3 stdin

v1 Shell 以 non-interactive execution 为主要模型，不支持命令无限等待 stdin。需要持续交互式输入的程序不属于 v1 标准执行能力。

### 8.4 Process Cleanup

v1 不支持显式创建无人管理的长期后台进程。

Runtime 应将自己直接启动的命令置于可管理的 process group、job object 或等价 process-tree 生命周期中。在 timeout、用户中断或 Runtime 终止时，Runtime 对受控进程树进行 best-effort cleanup。

程序主动创建并脱离 Runtime 管理的 detached process 无法被 v1 可靠阻止，属于 Accepted Risk。

### 8.5 User Interrupt

用户必须能够中断当前 Agent Run。触发中断后，Runtime 应停止产生新动作，并对当前受控执行进行 best-effort termination。

中断后的 Runtime 状态以 04 的 lifecycle 定义为准。

## 9. High-risk Operations

### 9.1 File Deletion

删除文件或目录需要用户确认。确认必须展示精确目标；批量删除需要展示其整体范围。

普通文件修改仍为 `ALLOW`，但不得违反 explicit task constraints，并须遵循 Task Intent / Semantic Relevance policy。大范围覆盖、批量修改与编辑冲突的具体策略由 06 决定。

### 9.2 Dependency Installation

安装或升级依赖需要用户确认，包括 `pip install`、`npm install`、`cargo add` 等。

涉及 sudo、管理员权限或系统级配置的安装直接进入 `DENY`。通过普通测试或项目程序间接触发的下载行为无法完整识别，属于 Accepted Risk。

### 9.3 Network Access

v1 不提供独立 Web / Search Agent 能力作为基础要求。通过 Shell 直接表达的明显网络访问需要用户确认，例如 `curl`、`wget`、package download 或 remote API request。

Runtime 无法仅根据 command string 识别任意程序内部产生的全部网络行为，属于 Accepted Risk。

### 9.4 Git

只读操作可以自主执行，例如 `git status`、`git diff`、`git log`。

会修改工作区或仓库状态的操作需要确认，例如 `git add`、`git commit`、`git switch`、`git merge`、`git rebase`、`git reset`。

远程写操作同样需要确认，例如 `git push`。如果参数变化导致风险变化，必须重新确认。

是否提供 Git 专用 Tool 由 06 决定。

### 9.5 Privilege and System Operations

v1 对直接识别出的以下动作执行 `DENY`：

- sudo 或管理员提权
- 系统级配置修改
- shutdown / reboot
- 明显超出软件 workspace 范围的系统控制行为

普通程序内部隐藏的等价行为无法通过 command inspection 完整阻止，属于 Accepted Risk。

## 10. Threat Model

### 10.1 Path Traversal

**Threat：**模型生成 `../../secret.txt`，试图访问 workspace 外路径。

**Mitigation：**File Tool 使用 canonical path 和 component-based containment，不使用字符串前缀或简单 `../` 黑名单。

### 10.2 Symlink Escape

**Threat：**Workspace 内 symlink 指向外部路径。

**Mitigation：**访问现有目标前 resolve symlink；创建新目标时 resolve 最近存在父目录，并检查其仍在 workspace 内。

### 10.3 Protected or Sensitive Path Access

**Threat：**模型通过 File Tool 修改 `.git`，或把 `.env`、私钥等内容发送给模型。

**Mitigation：**Protected Path 写入 `DENY`；Sensitive Path 不进入普通 Search，Agent 仅在合理判断任务需要时请求访问，任何实际创建、读取或修改都需要用户明确确认。

### 10.4 Repository Prompt Injection

**Threat：**代码、README 或注释中包含诱导模型执行危险行为的指令。

**Mitigation and Limitation：**Workspace 内容始终视为不可信数据，Runtime Policy 不会因仓库内容改变。Permission controls 可以限制部分影响，但 Prompt Injection 仍可能影响 LLM 在允许动作集合中的选择，不能由 Permission Boundary 完全消除。Prompt 层缓解由 07 继续设计。

### 10.5 Secret Leakage Through Child Process

**Threat：**Runtime 模型凭据被继承到 Shell child process。

**Mitigation：**移除 Runtime 明确知道的 Secret 和用户配置的 sensitive env names；启发式过滤只作为可配置的 defense-in-depth。

### 10.6 Infinite or Excessive Command Execution

**Threat：**命令死循环、测试挂起或产生巨量输出。

**Mitigation：**timeout、bounded output、non-interactive execution、user interruption 和 best-effort process-tree cleanup。

### 10.7 Destructive or External Side Effect

**Threat：**模型删除文件、修改 Git 历史、安装依赖、发起网络请求或执行系统操作。

**Mitigation and Limitation：**对 Runtime 直接识别或 action 明确表达的高风险行为执行 `CONFIRM` 或 `DENY`。隐藏在已获准程序内部的行为无法完整分类，属于 Accepted Risk。

## 11. Accepted Risks

### 11.1 Shell Is Not a Strong Sandbox

Generic Shell 运行在用户本地账户权限下。Runtime 无法保证获得执行许可的程序不会读取或修改 workspace 外文件、发起未被识别的网络请求，或执行其他隐藏 side effect。

这是复用用户真实开发环境、保持 language-neutral 和较强 Coding 能力所接受的 trade-off。

### 11.2 Command Classification Is Best-effort

Runtime 可以识别直接表达的常见高风险命令，但不能可靠静态分析解释器、脚本、构建程序或测试程序内部的全部行为。

Risk Classification 不是 security sandbox。

### 11.3 Sensitive Path Policy Can Be Bypassed by Shell

Protected / Sensitive Path Policy 约束 File Tool 和 discovery。获得执行许可的 Shell 程序仍可能访问这些内容。

### 11.4 Secret Filtering Is Not Complete Discovery

Runtime 会过滤自己明确知道的凭据和用户配置的敏感变量，但不能证明能够发现任意命名的全部 Secret。

### 11.5 Repository Prompt Injection Remains Possible

Permission Boundary 不会被仓库内容直接改写，但无法阻止 Prompt Injection 影响模型在允许动作集合中的选择。

### 11.6 Executing Workspace Code Is Inherently Risky

Tests、build scripts 和项目程序属于不可信 workspace code。没有 OS-level sandbox 时，它们拥有 child process 在当前用户账户下可获得的权限。

### 11.7 Filesystem Race and Platform Edge Cases

v1 使用平台路径 API 正常处理 canonical path 和 symlink，但不保证消除恶意并发替换导致的 TOCTOU，也不完整覆盖所有复杂 Windows junction / reparse point edge case。

### 11.8 Detached Processes

Runtime 对直接启动的进程树进行 best-effort cleanup，但不能可靠阻止程序创建脱离 Runtime 管理的 detached process。

## 12. Safety Invariants

本文描述 v1 的 normative Safety and Execution Boundaries，不维护独立的 design reserve。本文中的 v1 normative requirements 应在最终实现中得到落实，并由 `08-verification-testing-and-demo.md` 通过测试、验收或其他 evidence 覆盖。若某项最终不进入 v1 实现，应在提交前将其明确降级为 Accepted Limitation 或 Deferred，而不是继续保留为未实现的 v1 invariant。

v1 始终保持：

1. LLM 输出只是 action proposal，不直接拥有本地执行权。
2. Runtime 保存并强制执行用户明确给出的 `explicit_task_constraints`；只有用户能够更新这些硬约束。
3. 一般任务意图、是否默认允许修改及细粒度任务相关性属于 model-dependent semantic policy，不被声称为 deterministic 安全隔离；范围不确定时应询问用户。
4. Explicit Task Constraints 只约束 Runtime 能直接识别的动作；Shell 中隐藏的 side effect 仍属于 Accepted Risk。
5. 用户任务不能绕过 Runtime 固定的 `DENY` 规则。
6. File Tool 不能访问 resolved workspace root 之外的目标。
7. 对新建目标必须验证最近存在父目录，不能依赖不存在目标的 canonicalization。
8. Symlink 不能绕过 File Tool 的 workspace containment；TOCTOU 和复杂平台 edge case 除外并已列为 Accepted Risk。
9. `.gitignore` 与 discovery ignore 不是 Permission Boundary。
10. File Tool 不得直接修改 `.git` 内部文件；Sensitive Path 的创建、读取或修改不得套用普通路径的自动 `ALLOW`。
11. Shell cwd 是默认工作目录，不是 filesystem sandbox。
12. Runtime 明确知道的模型 Secret 不得默认暴露给 Shell child process。
13. Shell command 必须有 timeout 和 bounded output。
14. Runtime 对受控进程树执行 best-effort cleanup，不声称能够阻止 detached process。
15. Runtime 直接识别或 action 明确表达的高风险 side effect 必须经过 `CONFIRM` 或 `DENY`。
16. 复合命令采用可识别组成动作中的最高风险等级，动作变化后必须重新确认。
17. 复杂组合命令无法可靠分类时采用 conservative fallback，提升为 `CONFIRM`；简单未知程序不因此自动确认。
18. Runtime 不声称能够通过 command inspection 完全推断程序实际 side effect。
19. 无法安全或可靠继续时，应停止并如实报告，而不是绕过规则完成任务。

## 13. Cross-Document Ownership

本文只拥有 Safety and Execution Boundaries，不维护其他 owner 文档中事项的实时“未决 / 已决”状态。以下条目说明职责归属；当前语义始终以对应 canonical owner 文档为准。

### `04-agent-runtime-model.md`

- 用户拒绝确认后的 Runtime lifecycle
- Ctrl+C 后 Agent Run 的最终状态
- Timeout、Permission Denied 和 Explicit Task Constraint Rejection 的 Runtime Error / Recovery 语义

### `05-component-and-protocol-contracts.md`

- Explicit Task Constraint Check、Permission Check 与 Tool Dispatch 的组件接口
- Tool Error、Permission Error 和 Task Constraint Error 的内部协议

### `06-toolset-and-file-editing.md`

- v1 具体 Tool Set
- File Tool schema
- range read 具体接口
- binary detection 具体实现
- discovery 默认 ignore 集
- Protected / Sensitive Path 的具体文件 pattern
- 新建 Sensitive Path 与覆盖已有 Sensitive Path 是否采用不同策略
- `.git` 内部文件的 generic read 默认策略及极少数显式例外
- 是否提供 Git 专用 Tool
- 文件编辑机制与批量修改策略

### `07-context-and-prompt-policy.md`

- Tool output 截断后的 Context 表示
- Command output 的保留策略
- Repository Prompt Injection 的 Prompt 层缓解
- Sensitive Path 内容获准读取后的 Context 生命周期
- Semantic Relevance 的语义指导，以及模型无法判断任务相关性时的行为

### `09-cli-observability-and-configuration.md`

- 默认 Shell backend
- 默认 command timeout 与 output limit
- 用户确认 UI 与完整动作展示方式
- Permission action 的可观察性
- 用户配置的 sensitive env names
- 启发式 Secret 过滤开关
- 配置项和覆盖方式
