"""Minimal local CLI composition root for Coding Agent v1."""

from __future__ import annotations

import argparse
import os
import re
import shlex
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import TextIO

from .ask_user import AskUserTool
from .config import AgentConfig, load_agent_config
from .context import ContextManager
from .create_file import CreateFileTool
from .discovery import ListDirectoryTool, SearchFilesTool
from .edit_file import EditFileTool
from .interaction import (
    ClarificationRequest,
    ClarificationResponse,
    ClarificationStatus,
    ConfirmationDecision,
    ConfirmationRequest,
    UserInteraction,
    UserInteractionError,
)
from .model_client import ModelClient
from .openai_client import OpenAICompatibleConfig, OpenAICompatibleModelClient
from .policy import PolicyEngine
from .read_file import ReadFileTool
from .runtime import (
    AgentRuntime,
    RuntimeEvent,
    RunState,
    RuntimeLimits,
    TerminationReason,
)
from .search_text import SearchTextTool
from .shell import ShellBackend, ShellTool
from .tooling import ToolRegistry
from .workspace import WorkspacePathResolver


STARTUP_MESSAGE = "Coding Agent v1"
_DIVIDER = "━" * 28
_UI_TEXT = {
    "workspace": "工作区",
    "model": "模型",
    "task_prompt": "任务 > ",
    "running": "正在运行…",
    "permission": "⚠ 需要确认",
    "clarification": "需要你补充信息",
}
_DEFAULT_SECRET_ENVIRONMENT_NAMES = frozenset(
    {"CODING_AGENT_API_KEY", "OPENAI_API_KEY"}
)
_MAX_SHELL_TIMEOUT_SECONDS = 5 * 60


def _platform_shell_executable() -> str:
    if os.name == "nt":
        return os.environ["COMSPEC"]
    return "/bin/sh"


CLIConfig = AgentConfig


class ConsoleUserInteraction:
    """Plain terminal implementation of Runtime user interaction contracts."""

    def __init__(self, stdin: TextIO, stdout: TextIO, *, debug: bool = False) -> None:
        self._stdin = stdin
        self._stdout = stdout
        self._debug = debug

    def confirm(self, request: ConfirmationRequest) -> ConfirmationDecision:
        try:
            operation = _permission_operation(request)
            reason = _permission_reason(request.reason_code)
            detail = _bounded_head_tail(_single_line(request.action_summary), 240)
            lines = [
                "",
                _UI_TEXT["permission"],
                "",
                f"  操作：{operation}",
                f"  原因：{reason}",
                "  范围：仅授权下面这一次精确操作",
            ]
            if detail:
                label = "命令" if request.tool_name == "shell" else "对象"
                lines.extend(("", f"  {label}：{detail}"))
            if self._debug:
                lines.extend(
                    (
                        "",
                        f"  [调试] tool={request.tool_name}",
                        f"  [调试] reason_code={request.reason_code}",
                        "  [调试] action="
                        + _bounded_head_tail(_single_line(request.action_summary), 1_000),
                    )
                )
            lines.extend(("", "  允许执行？ [y/N/c] "))
            self._stdout.write("\n".join(lines))
            self._stdout.flush()
            answer = self._stdin.readline()
        except OSError as error:
            raise UserInteractionError("terminal confirmation I/O failed") from error
        if answer == "":
            return ConfirmationDecision.CANCEL
        normalized = answer.strip().casefold()
        if normalized in {"y", "yes"}:
            return ConfirmationDecision.APPROVE
        if normalized in {"c", "cancel"}:
            return ConfirmationDecision.CANCEL
        return ConfirmationDecision.REJECT

    def ask(self, request: ClarificationRequest) -> ClarificationResponse:
        try:
            self._stdout.write(
                f"{_UI_TEXT['clarification']}：\n{request.question}\n> "
            )
            self._stdout.flush()
            answer = self._stdin.readline()
        except OSError as error:
            raise UserInteractionError("terminal clarification I/O failed") from error
        if answer == "":
            return ClarificationResponse(ClarificationStatus.CANCELLED)
        return ClarificationResponse(
            ClarificationStatus.ANSWERED,
            answer.rstrip("\r\n"),
        )


def load_config(
    workspace: str,
    environ: Mapping[str, str] | None = None,
    *,
    model: str | None = None,
    base_url: str | None = None,
    max_model_turns: int | None = None,
    max_tool_call_attempts: int | None = None,
    max_active_run_duration_seconds: int | None = None,
    max_context_chars: int | None = None,
    debug: bool | None = None,
) -> CLIConfig:
    """Compatibility entry point for the centralized AgentConfig loader."""
    return load_agent_config(
        workspace,
        environ,
        model=model,
        base_url=base_url,
        max_model_turns=max_model_turns,
        max_tool_call_attempts=max_tool_call_attempts,
        max_active_run_duration_seconds=max_active_run_duration_seconds,
        max_context_chars=max_context_chars,
        debug=debug,
    )


def build_runtime(
    config: CLIConfig,
    *,
    model_client: ModelClient | None = None,
    user_interaction: UserInteraction | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> AgentRuntime:
    """Construct all v1 components and bind them to one workspace."""
    resolver = WorkspacePathResolver(config.workspace)
    registry = ToolRegistry()
    tools = (
        ReadFileTool(resolver, max_lines=400, max_bytes=20_000),
        ListDirectoryTool(resolver, max_entries=200),
        SearchFilesTool(resolver, max_results=200),
        SearchTextTool(resolver, max_matches=100, max_line_bytes=4096),
        EditFileTool(resolver),
        CreateFileTool(resolver),
        ShellTool(
            resolver,
            ShellBackend(_platform_shell_executable()),
            default_timeout_seconds=120,
            max_timeout_seconds=_MAX_SHELL_TIMEOUT_SECONDS,
            max_stdout_bytes=64 * 1024,
            max_stderr_bytes=64 * 1024,
            excluded_environment_names=(
                _DEFAULT_SECRET_ENVIRONMENT_NAMES
                | {config.api_key_environment_name}
            ),
        ),
        AskUserTool(),
    )
    for tool in tools:
        registry.register(tool)

    concrete_model_client = model_client or OpenAICompatibleModelClient(
        OpenAICompatibleConfig(
            model=config.model,
            api_key=config.api_key,
            base_url=config.base_url,
        )
    )
    concrete_interaction = user_interaction or ConsoleUserInteraction(
        stdin or sys.stdin,
        stdout or sys.stdout,
        debug=config.debug,
    )
    observer = (
        _event_writer(stdout, debug=config.debug) if stdout is not None else None
    )
    return AgentRuntime(
        concrete_model_client,
        ContextManager(max_context_chars=config.max_context_chars),
        registry,
        RuntimeLimits(
            max_model_turns=config.max_model_turns,
            max_tool_call_attempts=config.max_tool_call_attempts,
            max_active_run_duration_seconds=config.max_active_run_duration_seconds,
            max_transport_retries=2,
            max_consecutive_protocol_errors=3,
        ),
        workspace_resolver=resolver,
        policy_engine=PolicyEngine(),
        user_interaction=concrete_interaction,
        observer=observer,
        runtime_secret_values=(config.api_key,),
    )


def _event_writer(
    stdout: TextIO,
    *,
    debug: bool = False,
) -> Callable[[RuntimeEvent], None]:
    proposed_actions: dict[str, tuple[str, str]] = {}
    current_group: str | None = None

    def report(event: RuntimeEvent) -> None:
        nonlocal current_group
        try:
            call_id = event.facts.get("call_id")
            if event.kind == "run_started":
                proposed_actions.clear()
                current_group = None
            group_heading: str | None = None
            if event.kind == "tool_proposed" and isinstance(call_id, str):
                tool_name = str(event.facts.get("tool_name", "tool"))
                action = str(event.facts.get("action", "requested"))
                proposed_actions[call_id] = (tool_name, action)
                next_group = _activity_group(tool_name, action)
                if next_group is None:
                    current_group = None
                elif next_group != current_group:
                    current_group = next_group
                    group_heading = next_group
            lines = _render_event(
                event,
                debug=debug,
                proposed_actions=proposed_actions,
                group_heading=group_heading,
            )
            if event.kind == "tool_result" and isinstance(call_id, str):
                proposed_actions.pop(call_id, None)
            for line in lines:
                stdout.write(line[:2_000] + "\n")
            if lines:
                stdout.flush()
        except OSError as error:
            raise UserInteractionError("terminal activity output failed") from error

    return report


def _render_event(
    event: RuntimeEvent,
    *,
    debug: bool,
    proposed_actions: Mapping[str, tuple[str, str]] | None = None,
    group_heading: str | None = None,
) -> tuple[str, ...]:
    lines: list[str] = []
    proposed_actions = proposed_actions or {}
    if event.kind == "tool_proposed":
        tool_name = str(event.facts.get("tool_name", "tool"))
        raw_action = str(event.facts.get("action", "requested"))
        action = _bounded_head_tail(_single_line(raw_action), 240)
        labels = {
            "list_directory": "查看目录",
            "search_files": "查找文件",
            "search_text": "搜索代码",
            "read_file": "读取文件",
            "edit_file": "修改文件",
            "create_file": "创建文件",
            "shell": "执行本地命令",
            "ask_user": "请求补充信息",
        }
        if tool_name == "shell":
            labels[tool_name] = _shell_action_label(raw_action)
        if group_heading is not None:
            lines.extend(("", f"◆ {group_heading}", ""))
        suffix = (
            ""
            if tool_name in {"shell", "ask_user"} or action in {"requested", "command"}
            else f" {action}"
        )
        lines.append(f"● {labels.get(tool_name, tool_name)}{suffix}")
        if debug and tool_name == "shell":
            lines.append(
                "  [调试] command="
                + _bounded_head_tail(_single_line(raw_action), 1_000)
            )
    elif event.kind == "tool_result":
        outcome = event.facts.get("outcome")
        call_id = event.facts.get("call_id")
        proposal = proposed_actions.get(str(call_id), ("", ""))
        tool_name, action = proposal
        if outcome == "SUCCESS":
            lines.extend(_normal_success_lines(event, tool_name=tool_name, action=action))
        elif outcome == "UNSUCCESSFUL_COMMAND":
            exit_code = event.facts.get("exit_code")
            lines.append(f"  ✗ 命令返回失败状态（exit {exit_code}）")
            diagnostic = event.facts.get("diagnostic")
            if isinstance(diagnostic, str) and diagnostic:
                lines.extend(f"    {line}" for line in _failure_excerpt(diagnostic))
        elif outcome != "NOT_EXECUTED":
            code = event.facts.get("error_code") or outcome
            lines.append(f"  ✗ {_human_error(str(code))}")
    elif event.kind == "context_truncated":
        lines.append("! 已裁剪较早的工作上下文")
    elif event.kind == "budget_exhausted":
        lines.append("! 运行达到资源限制")
    elif event.kind == "permission_resolved":
        decision = event.facts.get("decision")
        if decision == "APPROVE":
            lines.append("  ✓ 已批准，仅限本次操作")
        elif decision == "REJECT":
            lines.append("  ✗ 已拒绝本次操作")
        else:
            lines.append("  — 已取消本次操作")

    if debug:
        diagnostic = event.facts.get("diagnostic")
        if isinstance(diagnostic, str) and diagnostic:
            lines.append(
                "  [调试] diagnostic="
                + _bounded_head_tail(_single_line(diagnostic), 1_600)
            )
        safe_facts = " ".join(
            f"{key}={value}"
            for key, value in event.facts.items()
            if key not in {"action", "diagnostic"} and value is not None
        )
        lines.append(f"  [调试] {event.kind}{' ' + safe_facts if safe_facts else ''}")
    return tuple(lines)


def _activity_group(tool_name: str, action: str) -> str | None:
    if tool_name in {"list_directory", "search_files", "search_text", "read_file"}:
        return "查看项目"
    if tool_name in {"edit_file", "create_file"}:
        return "修改文件"
    if tool_name == "shell":
        if _classify_shell_presentation(action) in {"test", "build", "check"}:
            return "测试与检查"
        return "执行命令"
    if tool_name == "ask_user":
        return None
    return "执行操作"


def _shell_action_label(command: str) -> str:
    return {
        "test": "运行测试",
        "build": "执行构建",
        "check": "运行检查",
    }.get(_classify_shell_presentation(command), "执行本地命令")


def _normal_result_detail(event: RuntimeEvent) -> str:
    facts = event.facts
    if facts.get("replacement_count") is not None:
        return f"已替换 {facts['replacement_count']} 处"
    if facts.get("created"):
        return "文件已创建"
    if facts.get("line_count") is not None:
        return f"已读取 {facts['line_count']} 行"
    if facts.get("result_count") is not None:
        return f"找到 {facts['result_count']} 项"
    if facts.get("exit_code") is not None:
        diagnostic = facts.get("diagnostic")
        if isinstance(diagnostic, str) and diagnostic:
            return _bounded_head_tail(_single_line(diagnostic), 500)
        return "命令执行成功"
    return "操作完成"


def _normal_success_lines(
    event: RuntimeEvent,
    *,
    tool_name: str,
    action: str,
) -> tuple[str, ...]:
    if tool_name != "shell":
        return (f"  ✓ {_normal_result_detail(event)}",)
    diagnostic = event.facts.get("diagnostic")
    category = _classify_shell_presentation(action)
    if isinstance(diagnostic, str) and category == "test":
        count = _successful_test_count(diagnostic)
        if count is not None:
            return (f"  ✓ {count} 个测试全部通过",)
        return ("  ✓ 测试命令执行成功",)
    if category == "build":
        return ("  ✓ 构建完成",)
    if category == "check":
        return ("  ✓ 检查通过",)
    if not isinstance(diagnostic, str) or not diagnostic.strip():
        return ("  ✓ 命令执行成功",)
    if _is_high_confidence_internal_noise(action, diagnostic):
        return ("  ✓ 本地检查完成",)
    excerpt = _success_excerpt(diagnostic)
    return ("  ✓ 命令执行成功", *(f"    {line}" for line in excerpt))


def _classify_shell_presentation(command: str) -> str | None:
    """Classify only simple, high-confidence command structures."""

    if not command.strip() or re.search(r"&&|\|\||[|;<>`]|\$\(|[\r\n]", command):
        return None
    try:
        tokens = shlex.split(command, posix=False)
    except ValueError:
        return None
    normalized = [token.strip('"\'').casefold() for token in tokens]
    if not normalized:
        return None
    executable = os.path.basename(normalized[0])
    target = executable
    if executable in {"python", "python.exe", "python3", "python3.exe"}:
        if len(normalized) < 3 or normalized[1] != "-m":
            return None
        target = normalized[2]
    if target in {"pytest", "unittest", "tox"}:
        return "test"
    if target in {"ruff", "mypy", "pyright", "eslint"}:
        return "check"
    if target in {"cargo", "go"} and len(normalized) > 1:
        subcommand = normalized[1]
        if subcommand == "test":
            return "test"
        if subcommand in {"check", "vet"}:
            return "check"
        if subcommand == "build":
            return "build"
    if target in {"npm", "npm.cmd"} and normalized[1:3] == ["run", "build"]:
        return "build"
    return None


def _successful_test_count(diagnostic: str) -> int | None:
    unittest_match = re.search(r"\bRan\s+(\d+)\s+tests?\b", diagnostic)
    if unittest_match and re.search(r"(?m)^OK\s*$", diagnostic):
        return int(unittest_match.group(1))
    pytest_match = re.search(r"\b(\d+)\s+passed\b", diagnostic)
    return int(pytest_match.group(1)) if pytest_match else None


def _success_excerpt(diagnostic: str) -> tuple[str, ...]:
    lines = [line.strip() for line in diagnostic.splitlines() if line.strip()]
    return tuple(_bounded_head_tail(line, 500) for line in lines[:3])


def _failure_excerpt(diagnostic: str) -> tuple[str, ...]:
    lines = [line.strip() for line in diagnostic.splitlines() if line.strip()]
    patterns = (
        re.compile(r"AssertionError|Permission denied|FileNotFoundError|error:", re.I),
        re.compile(r"\bFAILED\b|\bERROR\b|\bException\b"),
    )
    selected = [line for line in lines if any(pattern.search(line) for pattern in patterns)]
    if selected:
        return tuple(_bounded_head_tail(line, 500) for line in selected[:4])
    rendered = "\n".join(lines)
    return tuple(_bounded_head_tail(rendered, 800).splitlines())


def _is_high_confidence_internal_noise(command: str, diagnostic: str) -> bool:
    if not re.match(r"^\s*(?:\"[^\"]*python(?:\.exe)?\"|python(?:3)?(?:\.exe)?)\s+-c\b", command, re.I):
        return False
    return (
        ("repr(" in command or "read_bytes(" in command)
        and (diagnostic.lstrip().startswith("b'") or "CRLF count:" in diagnostic)
    )


def _human_error(code: str) -> str:
    return {
        "EDIT_TARGET_NOT_FOUND": "未找到预期的原始文本",
        "EDIT_MATCH_COUNT_MISMATCH": "预期文本的匹配数量不符合要求",
        "EDIT_CONFLICT": "文件已经变化，本次修改未应用",
        "FILE_NOT_FOUND": "文件不存在",
        "FILE_ALREADY_EXISTS": "目标文件已经存在",
        "VALIDATION_ERROR": "工具参数无效",
        "POLICY_REJECTED": "操作被安全策略拒绝",
        "USER_REJECTED_CONFIRMATION": "你已拒绝本次操作",
        "OPERATION_FAILURE": "本地操作未能完成",
        "COMMAND_TIMEOUT": "命令执行超时",
        "INTERNAL_TOOL_ERROR": "工具执行发生内部错误",
    }.get(code, "操作未能完成")


def _permission_operation(request: ConfirmationRequest) -> str:
    return {
        "shell": "运行本地命令",
        "read_file": "读取文件",
        "edit_file": "修改文件",
        "create_file": "创建文件",
    }.get(request.tool_name, "执行本地操作")


def _permission_reason(reason_code: str) -> str:
    return {
        "AMBIGUOUS_COMPLEX_SHELL": "命令结构较复杂，无法可靠判断全部副作用",
        "SHELL_ACTION_CONFIRMATION": "该命令可能产生需要确认的本地或外部影响",
        "SENSITIVE_PATH_CONFIRMATION": "操作涉及可能包含敏感信息的文件",
        "PROTECTED_PATH_READ_CONFIRMATION": "操作需要读取受保护的项目元数据",
    }.get(reason_code, "该操作需要你的明确批准")


def _single_line(value: str) -> str:
    return " ".join(value.replace("\r", " ").replace("\n", " ").split())


def _bounded_head_tail(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    marker = " …[中间已省略]… "
    retained = max(0, limit - len(marker))
    head = retained * 2 // 3
    tail = retained - head
    return value[:head] + marker + (value[-tail:] if tail else "")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="coding-agent")
    parser.add_argument(
        "--workspace",
        required=True,
        help="user-selected local workspace root",
    )
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--max-turns", type=int)
    parser.add_argument("--max-tool-calls", type=int)
    parser.add_argument("--max-duration", type=int)
    parser.add_argument("--max-context-chars", type=int)
    parser.add_argument("--debug", action="store_true", default=None)
    parser.add_argument(
        "task",
        nargs="*",
        help="optional one-shot task; omit for an interactive Session",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one task or a minimal multi-Run interactive Session."""
    args = _parser().parse_args(argv)
    try:
        config = load_config(
            args.workspace,
            model=args.model,
            base_url=args.base_url,
            max_model_turns=args.max_turns,
            max_tool_call_attempts=args.max_tool_calls,
            max_active_run_duration_seconds=args.max_duration,
            max_context_chars=args.max_context_chars,
            debug=args.debug,
        )
        runtime = build_runtime(config, stdout=sys.stdout)
    except (OSError, ValueError) as error:
        print(f"启动失败：{error}", file=sys.stderr)
        return 2

    print(STARTUP_MESSAGE)
    print(f"{_UI_TEXT['workspace']}  {config.workspace.resolve(strict=True)}")
    print(f"{_UI_TEXT['model']}    {config.model}")
    if args.task:
        return _run_task(runtime, " ".join(args.task), sys.stdout)

    while True:
        try:
            task = input(_UI_TEXT["task_prompt"])
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        task = task.strip()
        if not task:
            continue
        if task.casefold() in {"/exit", "/quit"}:
            return 0
        _run_task(runtime, task, sys.stdout)


def _run_task(runtime: AgentRuntime, task: str, stdout: TextIO) -> int:
    stdout.write(_UI_TEXT["running"] + "\n")
    stdout.flush()
    run = runtime.run(task)
    if run.state is RunState.COMPLETED:
        stdout.write(f"\n{_DIVIDER}\n◆ 运行结束\n{_DIVIDER}\n\n")
        stdout.write(f"{run.final_response}\n")
        return 0
    if run.state is RunState.CANCELLED:
        stdout.write(f"\n{_DIVIDER}\n— 已取消本次运行\n{_DIVIDER}\n")
        return 130
    stdout.write(f"\n{_DIVIDER}\n✗ 本次运行失败\n{_DIVIDER}\n\n")
    stdout.write("原因：" + _failure_message(run.termination_reason, run.limit_reached) + "\n")
    return 1


def _failure_message(
    reason: TerminationReason | None,
    limit_reached: str | None,
) -> str:
    if reason is TerminationReason.PROVIDER_FAILURE:
        return "模型服务请求失败，请检查凭据、服务地址或稍后重试。"
    if reason is TerminationReason.PROTOCOL_FAILURE:
        return "模型返回了无法使用的响应。"
    if reason is TerminationReason.LIMIT_REACHED:
        detail = f" ({limit_reached})" if limit_reached else ""
        return f"运行达到资源限制{detail}。"
    if reason is TerminationReason.USER_INTERACTION_FAILURE:
        return "终端交互失败。"
    if reason is TerminationReason.RUNTIME_FAILURE:
        return "Agent 无法安全地继续运行。"
    return "运行失败。"


__all__ = [
    "CLIConfig",
    "ConsoleUserInteraction",
    "STARTUP_MESSAGE",
    "build_runtime",
    "load_config",
    "main",
]
