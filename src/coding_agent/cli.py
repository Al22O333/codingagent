"""Minimal local CLI composition root for Coding Agent v1."""

from __future__ import annotations

import argparse
import os
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

    def __init__(self, stdin: TextIO, stdout: TextIO) -> None:
        self._stdin = stdin
        self._stdout = stdout

    def confirm(self, request: ConfirmationRequest) -> ConfirmationDecision:
        try:
            self._stdout.write(
                f"Permission required: {request.action_summary}\n"
                f"Risk: {request.risk_summary}\n"
                "Approve this exact action? [y/N/c] "
            )
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
            self._stdout.write(f"{request.question}\n> ")
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
    )
    observer = _event_writer(stdout) if stdout is not None else None
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
    )


def _event_writer(stdout: TextIO) -> Callable[[RuntimeEvent], None]:
    def report(event: RuntimeEvent) -> None:
        if event.kind != "tool_proposed":
            return
        try:
            stdout.write(
                f"[tool] {event.facts['tool_name']}: {event.facts['action']}\n"
            )
            stdout.flush()
        except OSError as error:
            raise UserInteractionError("terminal activity output failed") from error

    return report


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
        print(f"Startup error: {error}", file=sys.stderr)
        return 2

    print(STARTUP_MESSAGE)
    print(f"Workspace: {config.workspace.resolve(strict=True)}")
    print(f"Model: {config.model}")
    if args.task:
        return _run_task(runtime, " ".join(args.task), sys.stdout)

    while True:
        try:
            task = input("Task> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not task.strip():
            return 0
        exit_code = _run_task(runtime, task, sys.stdout)
        if exit_code == 130:
            return exit_code


def _run_task(runtime: AgentRuntime, task: str, stdout: TextIO) -> int:
    stdout.write("Agent: running\n")
    stdout.flush()
    run = runtime.run(task)
    if run.state is RunState.COMPLETED:
        stdout.write(f"{run.final_response}\n")
        return 0
    if run.state is RunState.CANCELLED:
        stdout.write("Run cancelled.\n")
        return 130
    stdout.write(_failure_message(run.termination_reason, run.limit_reached) + "\n")
    return 1


def _failure_message(
    reason: TerminationReason | None,
    limit_reached: str | None,
) -> str:
    if reason is TerminationReason.PROVIDER_FAILURE:
        return "Provider error: request failed; check credentials, endpoint, and availability."
    if reason is TerminationReason.PROTOCOL_FAILURE:
        return "Model response error: the provider returned an unusable response."
    if reason is TerminationReason.LIMIT_REACHED:
        detail = f" ({limit_reached})" if limit_reached else ""
        return f"Run limit reached{detail}."
    if reason is TerminationReason.USER_INTERACTION_FAILURE:
        return "Interaction error: terminal input or output failed."
    if reason is TerminationReason.RUNTIME_FAILURE:
        return "Runtime error: the Agent could not continue safely."
    return "Run failed."


__all__ = [
    "CLIConfig",
    "ConsoleUserInteraction",
    "STARTUP_MESSAGE",
    "build_runtime",
    "load_config",
    "main",
]
