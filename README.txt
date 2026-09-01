Coding Agent

这是一个本地运行的 Coding Agent，不是简单的对话界面或 LLM API 包装。大语言模型不能直接操作电脑文件，而是根据当前任务和观察结果提出 Tool Call；Runtime 负责参数校验、路径解析、权限判断和本地执行，再把真实结果返回模型，形成持续决策的动态循环。

项目不依赖 Agent 框架，也不依赖服务端代码执行或文件工具。对话与上下文管理、工具注册与本地执行、权限控制、运行状态、预算、错误处理、终止、Session 以及 JSON/JSONL 自动化接口等核心逻辑均自行实现。

仓库：
https://github.com/Al22O333/codingagent

运行：
需要 Python 3.11+ 和兼容 OpenAI Chat Completions 接口的模型 API。Windows PowerShell 示例：

git clone https://github.com/Al22O333/codingagent.git
cd codingagent
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .

$env:CODING_AGENT_API_KEY="<your-api-key>"
$env:CODING_AGENT_BASE_URL="<openai-compatible-base-url>"
$env:CODING_AGENT_MODEL="<model-name>"

.\.venv\Scripts\python.exe -m coding_agent --workspace "C:\path\to\project" "检查这个项目中的测试失败，定位原因并修复后运行相关验证。"

Linux/macOS 将 Python 路径换为 .venv/bin/python。--workspace 必须指向已存在的项目目录。

特色：
1. 动态工具循环：模型根据每轮真实执行结果自主决定下一步，而非执行写死的固定工作流。
2. 结构化本地工具：支持读取搜索、精确和原子多处修改、文件创建/移动/删除、Shell 与用户澄清，文件操作不完全依赖 Shell。
3. 权限与安全边界：Runtime 独立执行 ALLOW / CONFIRM / DENY，模型提出动作不等于获得执行权限。
4. 有界上下文管理：大型工具结果受控裁剪，较旧历史按固定规则淘汰，并优先保护当前任务、项目指导和最新工具状态。
5. 完成前自检：发生修改或命令执行后，在结束前提供额外检查机会，降低遗漏验证和过早宣布完成的概率。
6. 项目指导：读取工作区根目录的 AGENTS.md，支持项目特定开发规范，同时不能覆盖用户约束和 Runtime 安全规则。
7. 跨进程 Session：保存最近成功任务的有限连续性，重新启动后可继续自然追问并重新读取当前工作区状态。
8. 自动化与事实报告：支持 one-shot、JSON、JSONL 和 non-interactive，并支持基于实际执行结果的可选事实型运行报告。

设计特点：模型负责推理和动作选择，Runtime 负责安全边界、状态、预算、错误处理与终止，使执行行为能够被测试、复现和审查。

说明：Shell 在本机执行并受策略控制，但当前不提供操作系统级强沙箱。建议仅用于已备份或受 Git 管理的可信工作区，并核对确认提示。
