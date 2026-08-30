# Product Scope

## 1. Product Vision

本产品是一个运行在用户指定本地 workspace 中的广义 Coding Agent，目标形态类似 Codex、Claude Code 和 OpenCode，而不是只执行单次代码生成或文本替换的代码修改器。

长期产品能力可以覆盖：

- Bug Fix
- Feature Implementation
- Code Review
- Refactoring
- Test Authoring and Execution
- Debugging
- Codebase Exploration
- Code Explanation
- Greenfield Development
- 其他直接服务于编程与软件工程的任务

这些是长期产品方向，不代表 v1 必须全部实现，也不构成对所有场景相同成功率的承诺。v1 首先建立能够稳定完成简单真实任务的产品闭环，同时避免把产品永久限制在单一任务、单一语言或单一项目类型上。

## 2. Product Form

v1 是一个 **Local CLI**：

- 在用户本机运行。
- 面向用户明确指定的本地 workspace。
- 支持 interactive Session，也支持 one-shot task invocation。
- 为 automation 提供显式 machine-readable 与 non-interactive surface。
- interactive mode 支持在同一次程序运行期间连续提出和完成多个相关任务。

CLI 的具体命令、参数、输出布局和交互细节不在本文定义。

## 3. Primary Use Case

v1 的 Primary Use Case 是：

> 用户在一个已有的小型本地代码项目中，提出范围明确的 Bug 修复或小功能任务；Agent 检查相关代码，进行有限修改，执行非交互式测试或验证命令，根据结果迭代，并最终报告结果。

该场景是后续产品取舍和 v1 验证的首要依据。发生范围冲突时，应优先保证这一场景简单、可靠且可验证，而不是优先扩大功能覆盖面。

Primary Use Case 的典型特征包括：

- 任务目标明确，可以根据代码状态或验证结果判断是否完成。
- 项目规模较小，完成任务不依赖大范围、长期或跨项目协调。
- 修改集中在与任务直接相关的有限范围内。
- 验证过程可以通过非交互式命令完成。
- Agent 可以在一次 Agent Run 中完成检查、修改、验证、必要调整和结果报告。

## 4. Secondary Use Cases

以下场景属于产品范围，也是 v1 可以接受的任务类型，但不作为 v1 的主要可靠性目标。v1 不承诺它们达到与 Primary Use Case 相同的覆盖范围、任务复杂度或成功率，也不为每个 Secondary Use Case 提前建设专用能力：

- 在空目录中创建小型项目、脚本或 CLI 程序。
- 对已有项目进行小范围重构。
- 编写、补充或调整测试。
- 对代码或局部项目进行 Code Review。
- 探索代码结构、定位相关实现或分析问题。
- 解释代码、模块职责或局部架构。
- 通过阅读代码、运行非交互式命令、分析错误输出、修改代码并重新验证来进行 Debugging。

已有项目与 Greenfield Development 都属于产品正式范围，但 v1 不要求两者达到相同的任务复杂度或成功率。Greenfield 的正式产品范围不意味着 v1 需要为其专门建设 project scaffolding。Secondary Use Cases 的存在不能削弱 Primary Use Case 的可靠性目标。

## 5. Workspace Scope

每次启动时，Agent 绑定一个由用户明确指定的本地 **workspace root**。该 workspace 可以是：

- Git Repository
- 非 Git 的已有普通目录
- 空目录

Git 不是 Agent 启动或运行的前提。一个 Session 只面向启动时绑定的 workspace，不以同时操作多个 workspace 为 v1 使用场景。

本文不拥有 workspace 外访问、symbolic link、安全隔离或路径权限规则；这些事项的当前定义以 `03-safety-and-execution-boundaries.md` 为准。

## 6. Task Scope

产品同时面向两类软件工程任务。

### 6.1 Read-only / Analytical Tasks

这类任务以理解和分析为目标，不要求改变 workspace，例如：

- Code Review
- Codebase Exploration
- Code Explanation
- 问题定位
- 局部架构分析

Read-only 任务的默认结果是分析与报告，不因 Agent 具备修改能力而自动产生文件变更。

### 6.2 Mutating / Execution Tasks

这类任务要求改变项目状态或通过执行过程完成目标，例如：

- Bug Fix
- Feature Implementation
- Refactoring
- Test Authoring
- 配置修改
- Greenfield Development
- 需要修改和重新验证的 Debugging

是否修改文件由用户任务语义决定。例如，`review this code` 属于分析型任务，`review and fix the issues` 才包含修改要求。

### 6.3 Debugging Scope

v1 中的 Debugging 限定为：

- 阅读和理解代码
- 执行非交互式命令
- 分析程序、构建或测试输出
- 修改相关代码
- 重新运行验证

交互式 debugger、断点控制、进程 attach 和 IDE debugger integration 不属于 v1 承诺。

## 7. Interaction Model

产品区分以下三个概念：

- **Agent Run：**Agent 为完成一条用户任务而进行的一次自主执行过程，以完成、失败、被中断或触发限制结束。
- **Session：**同一程序进程中的连续交互，可以包含多个 Agent Run。后续任务可以基于当前 Session 中此前的交互和项目变化继续提出要求。
- **Persistent Session：**程序退出后保存并在未来恢复 bounded conversational continuity 的 Session；它不等于恢复 active Run 或执行状态。

v1 支持 Agent Run、进程内持续 Session，以及显式 opt-in 的 terminal-safe Persistent Session。Persistent checkpoint 只保留少量已完成 Run 的 task / Final continuity，并绑定 exact session ID 与 canonical workspace；恢复时仍以当前 workspace 和当前项目指令为真实来源。v1 不保存或恢复 active Run、PendingAction、ToolResult、provider response、完整 transcript 或执行状态。

同一 Session 中的后续任务可以与前一任务相关，也可以形成增量要求，例如先修复问题，再重构相关实现，最后补充测试。

## 8. Language Scope

Agent 采用 **language-neutral** 产品定位：

- Agent Runtime 和通用产品能力不绑定某一种编程语言。
- 核心产品流程不以某个编译器、包管理器或测试框架作为运行前提。
- v1 只对有限的语言和项目类型进行系统测试。
- 其他以文本文件为主的代码项目属于 best-effort 支持。
- 不宣称不同语言、工具链和项目类型具有相同的支持质量、成功率或能力深度。

language-neutral 表示产品核心不被特定语言锁定，不表示 v1 已具备所有语言的专用知识、工具链适配和深度工程能力。

## 9. Domain Boundary

产品领域限定为 **Programming and Software Engineering**。

属于产品领域的任务包括：

- 修复、实现、重构和测试代码
- 阅读、解释、Review 和探索代码库
- 创建小型软件项目
- 修改与软件运行、构建或测试直接相关的配置
- 分析与软件开发直接相关的错误和执行结果

以下任务不属于产品领域：

- 查询天气、新闻或一般生活信息
- 操作与软件项目无关的个人文件
- 一般办公自动化
- 代替用户操作任意桌面应用
- 与编程或软件工程无直接关系的通用电脑任务

本产品不是通用电脑操作 Agent。

## 10. v1 Success Criteria

v1 首先追求稳定完成简单、范围明确、结果可验证的真实 Coding Tasks，而不是最大化功能数量或以大型复杂项目作为成功标准。

一个符合 v1 目标的 Primary Use Case 应体现：

1. 正确理解用户给出的局部任务目标。
2. 检查完成任务所需的相关项目内容。
3. 对相关文件进行有限且有针对性的修改。
4. 执行与任务相关的非交互式测试或验证命令。
5. 根据验证结果进行必要的调整，而不是在首次修改后直接宣称完成。
6. 在完成、失败或无法继续时向用户给出清晰、真实的结果报告。

当缺少必要的本地条件、无法验证任务结果、任务超出当前能力或无法可靠继续时，Agent 必须明确区分已完成、未完成和未验证的部分，并向用户报告限制或失败原因，不得虚假宣称任务已经成功完成。

v1 的成功不要求：

- 对大型代码库进行全面理解。
- 处理长时间、跨项目或高度开放的工程目标。
- 在所有语言和工具链上达到相同质量。
- 让 Primary 与所有 Secondary Use Cases 达到相同可靠性。

## 11. Extensibility Goal

作为 v1 的可扩展性目标，产品应保持清晰的能力边界。增加符合既有交互范式的普通 Coding Capability、基础能力或模型适配时，不应要求重写核心执行流程：

- 普通 Tool
- 新的 Coding Capability
- ModelClient implementation
- 更强的 Review、Debugging 或 Verification 能力
- Git 相关能力
- 语言或工具链辅助能力

这里的可扩展性是产品演进目标，不是对任意未来需求的绝对兼容保证。引入全新的 Agent 交互范式不在此承诺范围内。该目标不要求 v1 提前建设完整插件系统，也不要求现在实现尚未进入 v1 范围的能力；当前 component boundary 以 05 为准，v1 不另行承诺扩展机制。

## 12. v1 Non-goals

v1 明确不以以下目标作为产品承诺：

- 成为通用电脑操作 Agent。
- 保证完成大型或高度复杂的软件项目任务。
- 保证所有编程语言具有相同成功率、支持深度或专用能力。
- full-fidelity Session persistence，或在程序退出后恢复 active Run、pending interaction 与执行状态。
- 以长期无人值守任务作为主要使用场景。
- 同时操作多个 workspace。
- 提供 IDE 或 GUI 自动化。
- 提供交互式 debugger、断点、进程 attach 或 IDE debugger integration。
- 提前建设语言专用的深度能力。
- 因可扩展性目标而提前建设完整插件系统。
- 在 v1 中完整复制 Codex、Claude Code、OpenCode 等成熟产品的全部能力。

## 13. Product Decisions Owned Outside This Document

以下事项不由 Product Scope 文档定义，本文也不跟踪其实时“未决 / 已决”状态；当前结论始终以对应 canonical owner 文档为准：

| Topic | Canonical Owner |
|---|---|
| Git 专用能力及具体覆盖范围 | 06 |
| 网络访问、workspace 外访问、Sandbox 强度、Permission Model 与确认规则 | 03 |
| one-shot task invocation 与 interactive Session / REPL 的具体 CLI 入口 | 09 |
| Persistent Session checkpoint / resume 的 lifecycle、内容与 CLI contract | 04、05、07、09 |

## 14. Final Product Definition

一个运行在用户明确指定的本地 workspace 中、通过交互式 CLI 提供进程内持续 Session，并可显式保存和恢复 terminal-safe bounded continuity 的 language-neutral Coding Agent。它面向 Programming / Software Engineering，既支持已有项目，也支持小型 Greenfield Development，并可处理分析型与修改型任务。v1 的首要场景是在已有的小型代码项目中完成范围明确的 Bug 修复或小功能任务：检查相关代码、进行有限修改、执行非交互式验证，并根据结果迭代后报告结论。小型 Greenfield、局部重构、测试编写、Code Review、Code Exploration 和 Code Explanation 属于 Secondary Use Cases；v1 不承诺大型复杂项目、所有语言的同等能力、full-fidelity 或 active-Run Session 恢复、通用电脑操作或交互式 IDE / Debugger 自动化。
