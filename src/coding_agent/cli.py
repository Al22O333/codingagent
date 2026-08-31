"""Minimal local CLI composition root for Coding Agent v1."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TextIO

from .ask_user import AskUserTool
from .config import (
    AgentConfig,
    load_agent_config,
    session_directory_from_environment,
)
from .context import CompletedRunContinuity, ContextManager
from .create_file import CreateFileTool
from .discovery import ListDirectoryTool, SearchFilesTool
from .edit_file import ApplyEditsTool, EditFileTool
from .file_lifecycle import CreateDirectoryTool, DeletePathTool, MovePathTool
from .interaction import (
    ClarificationRequest,
    ClarificationResponse,
    ClarificationStatus,
    ConfirmationDecision,
    ConfirmationRequest,
    NonInteractiveUserInteraction,
    UserInteraction,
    UserInteractionError,
)
from .model_client import ModelClient
from .openai_client import OpenAICompatibleConfig, OpenAICompatibleModelClient
from .policy import PolicyEngine
from .project_instructions import RootProjectInstructions
from .read_file import ReadFileTool
from .runtime import (
    AgentRun,
    AgentRuntime,
    RuntimeEvent,
    RunState,
    RuntimeLimits,
    TerminationReason,
)
from .search_text import SearchTextTool
from .session_store import SessionStore, SessionStoreError
from .shell import ShellBackend, ShellTool
from .tooling import ToolRegistry
from .workspace import WorkspacePathResolver
from .workspace_awareness import GitWorkspaceChangeObserver


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
_MAX_REVIEW_PATHS = 50


class _MachineUsageError(ValueError):
    """An argparse usage failure that must be projected as machine output."""


class _CLIArgumentParser(argparse.ArgumentParser):
    """Keep normal argparse UX while allowing machine-safe usage failures."""

    def __init__(self, *args: object, machine_errors: bool = False, **kwargs: object) -> None:
        self._machine_errors = machine_errors
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> None:
        if self._machine_errors:
            raise _MachineUsageError(message)
        super().error(message)


def _machine_output_intent(argv: Sequence[str]) -> str | None:
    """Detect only explicit machine flags; argparse still owns all parsing."""

    for argument in argv:
        if argument == "--":
            break
        if argument == "--jsonl" or argument.startswith("--jsonl="):
            return "jsonl"
        if argument == "--json" or argument.startswith("--json="):
            return "json"
    return None


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
    event_observer: Callable[[RuntimeEvent], None] | None = None,
    session_id: str | None = None,
    restored_continuity: tuple[CompletedRunContinuity, ...] = (),
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
        ApplyEditsTool(resolver),
        CreateFileTool(resolver),
        CreateDirectoryTool(resolver),
        MovePathTool(resolver),
        DeletePathTool(resolver),
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
    if stdout is not None and event_observer is not None:
        raise ValueError("stdout and event_observer are mutually exclusive")
    observer = (
        event_observer
        if event_observer is not None
        else _event_writer(stdout, debug=config.debug)
        if stdout is not None
        else None
    )
    context_manager = ContextManager(
        max_context_chars=config.max_context_chars,
        root_project_instructions=RootProjectInstructions(
            resolver,
            runtime_secret_values=(config.api_key,),
        ),
    )
    context_manager.restore_completed_run_continuity(restored_continuity)
    return AgentRuntime(
        concrete_model_client,
        context_manager,
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
        workspace_change_observer=GitWorkspaceChangeObserver(
            resolver.workspace_root
        ),
        session_id=session_id,
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


_JSONL_EXCLUDED_EVENT_FACTS = frozenset(
    {
        "action",
        "diagnostic",
        "pre_existing_paths",
        "known_touched_paths",
        "new_or_other_paths",
    }
)


class _JsonlEventStream:
    """Synchronous bounded JSONL projection of normalized Runtime events."""

    __slots__ = ("_secret_values", "_sequence", "_stdout")

    def __init__(self, stdout: TextIO, secret_values: tuple[str, ...]) -> None:
        self._stdout = stdout
        self._secret_values = secret_values
        self._sequence = 0

    def __call__(self, event: RuntimeEvent) -> None:
        safe_facts = {
            key: (
                _redact_values(value, self._secret_values)
                if isinstance(value, str)
                else value
            )
            for key, value in event.facts.items()
            if key not in _JSONL_EXCLUDED_EVENT_FACTS
        }
        self._write(
            {
                "schema_version": 1,
                "type": "event",
                "sequence": self._next_sequence(),
                "event": {"kind": event.kind, "facts": safe_facts},
            }
        )

    def write_result(self, document: Mapping[str, object]) -> None:
        self._write(
            {
                "schema_version": 1,
                "type": "result",
                "sequence": self._next_sequence(),
                "result": dict(document),
            }
        )

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    def _write(self, document: Mapping[str, object]) -> None:
        _write_json_document(document, self._stdout)


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
            "apply_edits": "批量修改文件",
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
            if code not in {
                "USER_REJECTED_CONFIRMATION",
                "USER_CANCELLED_CONFIRMATION",
            }:
                lines.append(f"  ✗ {_human_error(str(code))}")
    elif event.kind == "context_truncated":
        lines.append("! 已裁剪较早的工作上下文")
    elif event.kind == "completion_audit_started":
        lines.extend(("", "◆ 检查完成情况", ""))
    elif event.kind == "workspace_change_summary":
        state = event.facts.get("awareness_state")
        pre_existing = int(event.facts.get("pre_existing_count") or 0)
        known_touched = int(event.facts.get("known_touched_count") or 0)
        new_or_other = int(event.facts.get("new_or_other_count") or 0)
        uncertain = event.facts.get("attribution_uncertain") is True
        if state == "AVAILABLE" and (
            pre_existing or known_touched or new_or_other or uncertain
        ):
            lines.extend(
                (
                    "",
                    "◆ 工作区变更",
                    (
                        f"  运行前已有 {pre_existing}，Agent 已触及 "
                        f"{known_touched}，其他新增 {new_or_other}"
                    ),
                    (
                        "  归因：不完全确定"
                        if uncertain
                        else "  归因：未发现未解释的变更"
                    ),
                )
            )
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
    if tool_name in {
        "edit_file",
        "apply_edits",
        "create_file",
        "create_directory",
        "move_path",
        "delete_path",
    }:
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
    if facts.get("created_directory"):
        return "目录已创建"
    if facts.get("moved"):
        return "路径已移动或重命名"
    if facts.get("deleted"):
        return "路径已删除"
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
    if tool_name == "ask_user":
        return ("  ✓ 已收到补充信息",)
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
    unquoted = [token.strip('"\'') for token in tokens]
    normalized = [token.casefold() for token in unquoted]
    if not normalized:
        return None
    executable = os.path.basename(normalized[0])
    target = executable
    if executable in {"python", "python.exe", "python3", "python3.exe"}:
        module_index = 1
        while (
            module_index < len(unquoted)
            and unquoted[module_index] in {"-B", "-u"}
        ):
            module_index += 1
        if (
            len(normalized) <= module_index + 1
            or normalized[module_index] != "-m"
        ):
            return None
        target = normalized[module_index + 1]
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
        "EDIT_OVERLAP": "多个修改目标在原始文件中发生重叠",
        "EDIT_CONFLICT": "文件已经变化，本次修改未应用",
        "FILE_NOT_FOUND": "文件不存在",
        "FILE_ALREADY_EXISTS": "目标文件已经存在",
        "DIRECTORY_ALREADY_EXISTS": "目标目录已经存在",
        "PATH_ALREADY_EXISTS": "目标路径已经存在",
        "DESTINATION_ALREADY_EXISTS": "移动目标已经存在",
        "DIRECTORY_NOT_EMPTY": "目录不是空目录",
        "SYMLINK_UNSUPPORTED": "不支持对符号链接或重解析点执行此操作",
        "MOVE_CONFLICT": "移动前路径状态已经变化",
        "DELETE_TARGET_CHANGED": "删除前目标状态已经变化",
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
        "apply_edits": "批量修改文件",
        "create_file": "创建文件",
        "create_directory": "创建目录",
        "move_path": "移动或重命名路径",
        "delete_path": "删除路径",
    }.get(request.tool_name, "执行本地操作")


def _permission_reason(reason_code: str) -> str:
    return {
        "AMBIGUOUS_COMPLEX_SHELL": "命令结构较复杂，无法可靠判断全部副作用",
        "SHELL_ACTION_CONFIRMATION": "该命令可能产生需要确认的本地或外部影响",
        "SENSITIVE_PATH_CONFIRMATION": "操作涉及可能包含敏感信息的文件",
        "PROTECTED_PATH_READ_CONFIRMATION": "操作需要读取受保护的项目元数据",
        "FILE_DELETE_CONFIRMATION": "删除操作需要你批准这个精确目标",
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


def _parser(*, machine_errors: bool = False) -> argparse.ArgumentParser:
    parser = _CLIArgumentParser(
        prog="coding-agent", machine_errors=machine_errors
    )
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
    machine_output = parser.add_mutually_exclusive_group()
    machine_output.add_argument(
        "--json",
        action="store_true",
        help="emit one machine-readable document for a one-shot task",
    )
    machine_output.add_argument(
        "--jsonl",
        action="store_true",
        help="stream safe normalized events and one terminal result",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="run one task without ever reading user input",
    )
    parser.add_argument(
        "--review",
        action="store_true",
        help="include bounded factual workspace-change and command evidence",
    )
    persistence = parser.add_mutually_exclusive_group()
    persistence.add_argument(
        "--persist-session",
        action="store_true",
        help="persist bounded completed-run continuity for later resume",
    )
    persistence.add_argument(
        "--resume",
        metavar="SESSION_UUID",
        help="resume one exact terminal-safe persisted session",
    )
    persistence.add_argument(
        "--list-sessions",
        action="store_true",
        help="list persisted sessions for the selected workspace",
    )
    persistence.add_argument(
        "--delete-session",
        metavar="SESSION_UUID",
        help="delete one exact persisted session for the selected workspace",
    )
    parser.add_argument(
        "task",
        nargs="*",
        help="optional one-shot task; omit for an interactive Session",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one task or a minimal multi-Run interactive Session."""
    raw_argv = tuple(sys.argv[1:] if argv is None else argv)
    machine_intent = _machine_output_intent(raw_argv)
    startup_secret_values = tuple(
        value
        for name in _DEFAULT_SECRET_ENVIRONMENT_NAMES
        if (value := os.environ.get(name))
    )
    try:
        args = _parser(machine_errors=machine_intent is not None).parse_args(raw_argv)
    except _MachineUsageError as error:
        message = _redact_values(str(error), startup_secret_values)
        document = _startup_failure_document("CLI_USAGE_ERROR", message)
        if machine_intent == "jsonl":
            _JsonlEventStream(sys.stdout, startup_secret_values).write_result(document)
        else:
            _write_json_document(document, sys.stdout)
        return 2
    jsonl_stream = (
        _JsonlEventStream(sys.stdout, startup_secret_values)
        if args.jsonl
        else None
    )
    session_management = args.list_sessions or args.delete_session is not None
    if session_management:
        if args.task or args.non_interactive or args.review or args.jsonl:
            code = "SESSION_MANAGEMENT_ARGUMENT_CONFLICT"
            message = (
                "session management does not accept a task, --non-interactive, "
                "--review, or --jsonl"
            )
            if args.jsonl:
                assert jsonl_stream is not None
                jsonl_stream.write_result(_startup_failure_document(code, message))
            elif args.json:
                _write_json_document(
                    _startup_failure_document(code, message),
                    sys.stdout,
                )
            else:
                print(f"启动失败：{message}", file=sys.stderr)
            return 2
        return _run_session_management(
            workspace=Path(args.workspace),
            list_sessions=args.list_sessions,
            delete_session=args.delete_session,
            json_output=args.json,
            secret_values=startup_secret_values,
        )
    if args.jsonl and not args.non_interactive:
        code = "JSONL_NON_INTERACTIVE_REQUIRED"
        message = "--jsonl requires --non-interactive"
        assert jsonl_stream is not None
        jsonl_stream.write_result(_startup_failure_document(code, message))
        return 2
    if (args.json or args.jsonl or args.non_interactive) and not args.task:
        code = (
            "NON_INTERACTIVE_ONE_SHOT_REQUIRED"
            if args.non_interactive
            else "JSON_ONE_SHOT_REQUIRED"
        )
        message = (
            "--non-interactive requires a one-shot task"
            if args.non_interactive
            else "--json requires a one-shot task"
        )
        if args.jsonl:
            assert jsonl_stream is not None
            jsonl_stream.write_result(_startup_failure_document(code, message))
        elif args.json:
            _write_json_document(
                _startup_failure_document(code, message),
                sys.stdout,
            )
        else:
            print(f"启动失败：{message}", file=sys.stderr)
        return 2
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
        session_store = (
            SessionStore(config.session_directory)
            if args.persist_session or args.resume is not None
            else None
        )
        restored_continuity: tuple[CompletedRunContinuity, ...] = ()
        resumed_session_id: str | None = None
        if args.resume is not None:
            assert session_store is not None
            checkpoint = session_store.load(args.resume, config.workspace)
            restored_continuity = checkpoint.continuity
            resumed_session_id = checkpoint.session_id
        runtime = build_runtime(
            config,
            user_interaction=(
                NonInteractiveUserInteraction()
                if args.non_interactive
                else None
            ),
            stdout=(
                None
                if args.jsonl
                else sys.stderr
                if args.json
                else sys.stdout
            ),
            event_observer=jsonl_stream,
            session_id=resumed_session_id,
            restored_continuity=restored_continuity,
        )
    except (OSError, ValueError, SessionStoreError) as error:
        code = error.code if isinstance(error, SessionStoreError) else "STARTUP_FAILURE"
        if args.jsonl:
            assert jsonl_stream is not None
            message = _redact_values(str(error), startup_secret_values)
            jsonl_stream.write_result(_startup_failure_document(code, message))
            return 2
        if args.json:
            message = _redact_values(str(error), startup_secret_values)
            _write_json_document(
                _startup_failure_document(code, message),
                sys.stdout,
            )
            return 2
        print(f"启动失败：{error}", file=sys.stderr)
        return 2

    if args.json or args.jsonl:
        run = runtime.run(" ".join(args.task))
        checkpoint_updated, session_error = _checkpoint_session(
            runtime,
            run,
            session_store,
            config,
            secret_values=(config.api_key, *startup_secret_values),
        )
        document = _run_json_document(
            run,
            secret_values=(config.api_key, *startup_secret_values),
            include_review=args.review,
            session_id=(
                runtime.session.session_id if session_store is not None else None
            ),
            session_checkpoint_updated=checkpoint_updated,
            session_error=session_error,
        )
        if args.jsonl:
            assert jsonl_stream is not None
            jsonl_stream.write_result(document)
        else:
            _write_json_document(document, sys.stdout)
        return 1 if session_error is not None else _run_exit_code(run)

    print(STARTUP_MESSAGE)
    print(f"{_UI_TEXT['workspace']}  {config.workspace.resolve(strict=True)}")
    print(f"{_UI_TEXT['model']}    {config.model}")
    if session_store is not None:
        print(f"Session  {runtime.session.session_id}")
    if args.task:
        return _run_task(
            runtime,
            " ".join(args.task),
            sys.stdout,
            session_store=session_store,
            config=config,
            secret_values=(config.api_key, *startup_secret_values),
            include_review=args.review,
        )

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
        _run_task(
            runtime,
            task,
            sys.stdout,
            session_store=session_store,
            config=config,
            secret_values=(config.api_key, *startup_secret_values),
            include_review=args.review,
        )


def _run_session_management(
    *,
    workspace: Path,
    list_sessions: bool,
    delete_session: str | None,
    json_output: bool,
    secret_values: tuple[str, ...],
) -> int:
    """Execute one model-free Session management operation."""

    try:
        store = SessionStore(session_directory_from_environment())
        if list_sessions:
            listing = store.list_summaries(workspace)
            if json_output:
                _write_json_document(
                    {
                        "schema_version": 1,
                        "operation": "list_sessions",
                        "workspace_identity": listing.workspace_identity,
                        "sessions": [
                            {
                                "session_id": summary.session_id,
                                "updated_at": summary.updated_at,
                                "completed_run_count": summary.completed_run_count,
                            }
                            for summary in listing.sessions
                        ],
                        "skipped_invalid_entries": listing.skipped_invalid_entries,
                        "truncated": listing.truncated,
                    },
                    sys.stdout,
                )
            else:
                if listing.sessions:
                    print("Persisted sessions:")
                    for summary in listing.sessions:
                        print(
                            f"{summary.session_id}  {summary.updated_at}  "
                            f"completed_runs={summary.completed_run_count}"
                        )
                else:
                    print("No persisted sessions for this workspace.")
                if listing.skipped_invalid_entries:
                    print(
                        "Skipped invalid session entries: "
                        f"{listing.skipped_invalid_entries}"
                    )
                if listing.truncated:
                    print("Session listing was truncated at the bounded scan/result limit.")
            return 0

        assert delete_session is not None
        checkpoint = store.delete(delete_session, workspace)
        if json_output:
            _write_json_document(
                {
                    "schema_version": 1,
                    "operation": "delete_session",
                    "session_id": checkpoint.session_id,
                    "deleted": True,
                },
                sys.stdout,
            )
        else:
            print(f"Deleted session {checkpoint.session_id}")
        return 0
    except (OSError, ValueError, SessionStoreError) as error:
        code = error.code if isinstance(error, SessionStoreError) else "STARTUP_FAILURE"
        message = _redact_values(str(error), secret_values)
        if json_output:
            _write_json_document(
                _startup_failure_document(code, message),
                sys.stdout,
            )
        else:
            print(f"启动失败：{message}", file=sys.stderr)
        return 2


def _run_task(
    runtime: AgentRuntime,
    task: str,
    stdout: TextIO,
    *,
    session_store: SessionStore | None = None,
    config: CLIConfig | None = None,
    secret_values: tuple[str, ...] = (),
    include_review: bool = False,
) -> int:
    stdout.write(_UI_TEXT["running"] + "\n")
    stdout.flush()
    run = runtime.run(task)
    _, session_error = _checkpoint_session(
        runtime,
        run,
        session_store,
        config,
        secret_values=secret_values,
    )
    if run.state is RunState.COMPLETED:
        stdout.write(f"\n{_DIVIDER}\n◆ 运行结束\n{_DIVIDER}\n\n")
        stdout.write(f"{run.final_response}\n")
        if session_error is not None:
            stdout.write(f"\nSession checkpoint failed: {session_error['message']}\n")
            if include_review:
                _write_run_review(run, stdout, secret_values=secret_values)
            return 1
        if include_review:
            _write_run_review(run, stdout, secret_values=secret_values)
        return 0
    if run.state is RunState.CANCELLED:
        stdout.write(f"\n{_DIVIDER}\n— 已取消本次运行\n{_DIVIDER}\n")
        if include_review:
            _write_run_review(run, stdout, secret_values=secret_values)
        return 130
    stdout.write(f"\n{_DIVIDER}\n✗ 本次运行失败\n{_DIVIDER}\n\n")
    stdout.write("原因：" + _failure_message(run.termination_reason, run.limit_reached) + "\n")
    if run.failure_diagnostic is not None:
        diagnostic = run.failure_diagnostic
        stdout.write(
            "诊断："
            + _redact_values(
                f"{diagnostic.code} ({diagnostic.error_type})；阶段：{diagnostic.phase}",
                secret_values,
            )
            + "\n"
        )
        if diagnostic.context_chars is not None:
            stdout.write(
                f"上下文：{diagnostic.context_chars}/{diagnostic.context_limit} 字符"
                f"（含 provider reasoning {diagnostic.reasoning_chars} 字符）\n"
            )
        explanations = {
            "CONTEXT_LIMIT_EXCEEDED": "必须保留的模型上下文超过上限；不是任务时长或工具次数限制。",
            "MODEL_RESPONSE_JSON_INVALID": "模型请求阶段发生 JSON 解析错误；原始响应内容未输出。",
            "UNEXPECTED_RUNTIME_ERROR": "发生未分类的运行时异常；可用 --debug 查看脱敏代码位置。",
        }
        stdout.write(explanations[diagnostic.code] + "\n")
    if run.required_interaction is not None:
        stdout.write(_render_required_interaction(run) + "\n")
    if include_review:
        _write_run_review(run, stdout, secret_values=secret_values)
    return _run_exit_code(run)


def _run_json_document(
    run: AgentRun,
    *,
    secret_values: tuple[str, ...],
    include_review: bool = False,
    session_id: str | None = None,
    session_checkpoint_updated: bool = False,
    session_error: dict[str, str] | None = None,
) -> dict[str, object]:
    """Project one terminal Run into the stable machine-readable schema."""

    final_response = (
        _redact_values(run.final_response, secret_values)
        if run.final_response is not None
        else None
    )
    normalized_error: dict[str, object] | None = None
    if run.state is RunState.FAILED:
        code = (
            run.termination_reason.value
            if run.termination_reason is not None
            else TerminationReason.RUNTIME_FAILURE.value
        )
        normalized_error = {
            "code": code,
            "message": _bounded_head_tail(
                _failure_message(run.termination_reason, run.limit_reached),
                1_000,
            ),
        }
        if run.failure_diagnostic is not None:
            normalized_error["diagnostic"] = {
                key: _redact_values(value, secret_values) if isinstance(value, str) else value
                for key, value in run.failure_diagnostic.facts().items()
            }
    document: dict[str, object] = {
        "schema_version": 1,
        "lifecycle_state": run.state.value,
        "final_response": final_response,
        "terminal_reason": (
            run.termination_reason.value
            if run.termination_reason is not None
            else None
        ),
        "normalized_error": normalized_error,
        "model_turns": run.model_turns,
        "tool_attempts": run.tool_call_attempts,
        "limit_reached": run.limit_reached,
    }
    if session_id is not None:
        document.update(
            session_id=session_id,
            session_checkpoint_updated=session_checkpoint_updated,
            session_error=session_error,
        )
    if run.required_interaction is not None:
        required = run.required_interaction
        document["required_interaction"] = {
            "kind": required.kind.value,
            "question": _redact_values(required.question, secret_values)
            if required.question is not None
            else None,
            "tool_name": required.tool_name,
            "operation_category": required.tool_name,
            "action_preview": _redact_values(required.action_preview, secret_values)
            if required.action_preview is not None
            else None,
            "reason_code": required.reason_code,
            "risk": _redact_values(required.risk, secret_values)
            if required.risk is not None
            else None,
            "exact_scope": required.exact_scope,
        }
    if include_review:
        document["review"] = _run_review_document(
            run,
            secret_values=secret_values,
        )
    return document


def _run_review_document(
    run: AgentRun,
    *,
    secret_values: tuple[str, ...],
) -> dict[str, object]:
    """Project bounded facts without inferring semantic verification success."""

    workspace_changes: dict[str, object] | None = None
    facts = run.workspace_change_facts
    if facts is not None:
        pre_existing = _review_paths(facts.pre_existing_dirty_paths, secret_values)
        known_touched = _review_paths(facts.known_agent_touched_paths, secret_values)
        new_or_other = _review_paths(facts.new_or_other_dirty_paths, secret_values)
        workspace_changes = {
            "awareness_state": facts.awareness_state.value,
            "pre_existing_dirty_paths": list(pre_existing),
            "known_agent_touched_paths": list(known_touched),
            "new_or_other_dirty_paths": list(new_or_other),
            "attribution_uncertain": facts.attribution_uncertain,
            "paths_truncated": (
                facts.truncated
                or len(facts.pre_existing_dirty_paths) > _MAX_REVIEW_PATHS
                or len(facts.known_agent_touched_paths) > _MAX_REVIEW_PATHS
                or len(facts.new_or_other_dirty_paths) > _MAX_REVIEW_PATHS
            ),
        }
    command_evidence = []
    for evidence in run.command_execution_evidence:
        presentation_command = evidence.command.replace("<redacted>", "REDACTED")
        command = _bounded_head_tail(
            _redact_values(evidence.command, secret_values),
            500,
        )
        command_evidence.append(
            {
                "command": command,
                "cwd": _bounded_head_tail(
                    _redact_values(evidence.cwd, secret_values),
                    500,
                ),
                "outcome": (
                    "INTERRUPTED"
                    if evidence.interrupted
                    else evidence.outcome.value
                    if evidence.outcome is not None
                    else "OPERATION_FAILURE"
                ),
                "exit_code": evidence.exit_code,
                "error_code": evidence.error_code,
                "presentation_category": (
                    _classify_shell_presentation(presentation_command) or "command"
                ),
            }
        )
    return {
        "workspace_changes": workspace_changes,
        "command_evidence": command_evidence,
        "command_evidence_truncated": run.command_evidence_truncated,
        "verification_sufficiency": "NOT_INFERRED",
    }


def _review_paths(
    paths: Sequence[str],
    secret_values: tuple[str, ...],
) -> tuple[str, ...]:
    return tuple(
        _bounded_head_tail(_redact_values(path, secret_values), 500)
        for path in paths[:_MAX_REVIEW_PATHS]
    )


def _write_run_review(
    run: AgentRun,
    stdout: TextIO,
    *,
    secret_values: tuple[str, ...],
) -> None:
    """Write one terminal-safe opt-in factual review after a terminal Run."""

    review = _run_review_document(run, secret_values=secret_values)
    stdout.write(f"\n{_DIVIDER}\n◆ 变更与命令证据\n{_DIVIDER}\n")
    changes = review["workspace_changes"]
    if isinstance(changes, dict):
        state = changes["awareness_state"]
        if state == "AVAILABLE":
            path_groups = (
                ("运行前已有", changes["pre_existing_dirty_paths"]),
                ("Agent 已触及", changes["known_agent_touched_paths"]),
                ("其他新增", changes["new_or_other_dirty_paths"]),
            )
            rendered_any = False
            for label, raw_paths in path_groups:
                if isinstance(raw_paths, list) and raw_paths:
                    rendered_any = True
                    rendered = ", ".join(
                        json.dumps(path, ensure_ascii=False) for path in raw_paths
                    )
                    stdout.write(f"{label}：{rendered}\n")
            if not rendered_any:
                stdout.write("工作区：未观察到 dirty path\n")
            attribution = (
                "不完全确定"
                if changes["attribution_uncertain"]
                else "未发现未解释的变更"
            )
            stdout.write(f"变更归因：{attribution}\n")
            if changes["paths_truncated"]:
                stdout.write("路径列表：已按上限截断\n")
        else:
            stdout.write(f"工作区变更：Git awareness {state}，无法精确列出\n")
    else:
        stdout.write("工作区变更：未启用 awareness\n")

    commands = review["command_evidence"]
    if isinstance(commands, list) and commands:
        stdout.write("实际命令：\n")
        for item in commands:
            if not isinstance(item, dict):
                continue
            exit_part = (
                f" exit={item['exit_code']}"
                if item.get("exit_code") is not None
                else ""
            )
            error_part = (
                f" error={item['error_code']}"
                if item.get("error_code") is not None
                else ""
            )
            command = json.dumps(item["command"], ensure_ascii=False)
            cwd = json.dumps(item["cwd"], ensure_ascii=False)
            stdout.write(
                f"  [{item['outcome']}{exit_part}{error_part}] "
                f"cwd={cwd} command={command}\n"
            )
    else:
        stdout.write("实际命令：无\n")
    if review["command_evidence_truncated"]:
        stdout.write("命令证据：已按上限截断\n")
    stdout.write("说明：命令结果是执行证据，不自动证明验证充分性。\n")


def _checkpoint_session(
    runtime: AgentRuntime,
    run: AgentRun,
    session_store: SessionStore | None,
    config: CLIConfig | None,
    *,
    secret_values: tuple[str, ...],
) -> tuple[bool, dict[str, str] | None]:
    """Persist only terminal-safe completed continuity after Runtime cleanup."""

    if session_store is None or config is None or run.state is not RunState.COMPLETED:
        return False, None
    try:
        session_store.save(
            session_id=runtime.session.session_id,
            workspace=config.workspace,
            continuity=runtime.completed_run_continuity,
            runtime_secret_values=secret_values,
        )
    except SessionStoreError as error:
        return False, {
            "code": error.code,
            "message": _bounded_head_tail(_redact_values(str(error), secret_values), 1_000),
        }
    return True, None


def _startup_failure_document(code: str, message: str) -> dict[str, object]:
    """Return the no-Run form of the versioned JSON result schema."""

    return {
        "schema_version": 1,
        "lifecycle_state": "STARTUP_FAILED",
        "final_response": None,
        "terminal_reason": "STARTUP_FAILURE",
        "normalized_error": {
            "code": code,
            "message": _bounded_head_tail(_single_line(message), 1_000),
        },
        "model_turns": 0,
        "tool_attempts": 0,
        "limit_reached": None,
    }


def _write_json_document(document: Mapping[str, object], stdout: TextIO) -> None:
    json.dump(document, stdout, ensure_ascii=False, separators=(",", ":"))
    stdout.write("\n")
    stdout.flush()


def _run_exit_code(run: AgentRun) -> int:
    if run.state is RunState.COMPLETED:
        return 0
    if run.state is RunState.CANCELLED:
        return 130
    if run.termination_reason in {
        TerminationReason.CLARIFICATION_REQUIRED,
        TerminationReason.PERMISSION_REQUIRED,
    }:
        return 3
    return 1


def _redact_values(value: str, secret_values: tuple[str, ...]) -> str:
    redacted = value
    for secret in secret_values:
        if secret:
            redacted = redacted.replace(secret, "<redacted>")
    return redacted


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
    if reason is TerminationReason.CLARIFICATION_REQUIRED:
        return "任务需要用户补充信息；non-interactive 模式未读取输入。"
    if reason is TerminationReason.PERMISSION_REQUIRED:
        return "操作需要用户明确授权；non-interactive 模式未执行该操作。"
    if reason is TerminationReason.RUNTIME_FAILURE:
        return "Agent 无法安全地继续运行。"
    return "运行失败。"


def _render_required_interaction(run: AgentRun) -> str:
    required = run.required_interaction
    if required is None:
        return ""
    if required.question is not None:
        return "问题：" + _bounded_head_tail(_single_line(required.question), 1_000)
    parts = [
        f"操作类别：{required.tool_name or 'unknown'}",
        "精确范围：" + (required.exact_scope or "one exact action"),
    ]
    if required.action_preview:
        parts.append(
            "操作预览："
            + _bounded_head_tail(_single_line(required.action_preview), 1_000)
        )
    if required.risk:
        parts.append("风险：" + _bounded_head_tail(_single_line(required.risk), 1_000))
    return "\n".join(parts)


__all__ = [
    "CLIConfig",
    "ConsoleUserInteraction",
    "STARTUP_MESSAGE",
    "build_runtime",
    "load_config",
    "main",
]
