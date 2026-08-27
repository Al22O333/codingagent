"""Minimal local CLI composition root for Coding Agent v1."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from .ask_user import AskUserTool
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
)
from .model_client import ModelClient
from .openai_client import OpenAICompatibleConfig, OpenAICompatibleModelClient
from .policy import PolicyEngine
from .read_file import ReadFileTool
from .runtime import AgentRuntime, RunState, RuntimeLimits
from .search_text import SearchTextTool
from .shell import ShellBackend, ShellTool
from .tooling import ToolRegistry
from .workspace import WorkspacePathResolver


STARTUP_MESSAGE = "Coding Agent v1"


@dataclass(frozen=True, slots=True)
class CLIConfig:
    workspace: Path
    model: str
    api_key: str = field(repr=False)
    base_url: str | None = None
    shell_executable: str = "cmd.exe" if os.name == "nt" else "/bin/sh"


class ConsoleUserInteraction:
    """Plain terminal implementation of Runtime user interaction contracts."""

    def __init__(self, stdin: TextIO, stdout: TextIO) -> None:
        self._stdin = stdin
        self._stdout = stdout

    def confirm(self, request: ConfirmationRequest) -> ConfirmationDecision:
        self._stdout.write(
            f"Permission required: {request.action_summary}\n"
            f"Risk: {request.risk_summary}\n"
            "Approve this exact action? [y/N/c] "
        )
        self._stdout.flush()
        answer = self._stdin.readline()
        if answer == "":
            return ConfirmationDecision.CANCEL
        normalized = answer.strip().casefold()
        if normalized in {"y", "yes"}:
            return ConfirmationDecision.APPROVE
        if normalized in {"c", "cancel"}:
            return ConfirmationDecision.CANCEL
        return ConfirmationDecision.REJECT

    def ask(self, request: ClarificationRequest) -> ClarificationResponse:
        self._stdout.write(f"{request.question}\n> ")
        self._stdout.flush()
        answer = self._stdin.readline()
        if answer == "":
            return ClarificationResponse(ClarificationStatus.CANCELLED)
        return ClarificationResponse(
            ClarificationStatus.ANSWERED,
            answer.rstrip("\r\n"),
        )


def load_config(
    workspace: str,
    environ: Mapping[str, str] | None = None,
) -> CLIConfig:
    """Load the minimal environment-backed provider and workspace config."""
    values = os.environ if environ is None else environ
    model = values.get("CODING_AGENT_MODEL", "").strip()
    api_key = values.get("CODING_AGENT_API_KEY", "").strip()
    if not model:
        raise ValueError("CODING_AGENT_MODEL is required")
    if not api_key:
        raise ValueError("CODING_AGENT_API_KEY is required")
    return CLIConfig(
        workspace=Path(workspace),
        model=model,
        api_key=api_key,
        base_url=values.get("CODING_AGENT_BASE_URL") or None,
        shell_executable=values.get("CODING_AGENT_SHELL")
        or ("cmd.exe" if os.name == "nt" else "/bin/sh"),
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
        ReadFileTool(resolver, max_lines=400, max_bytes=64 * 1024),
        ListDirectoryTool(resolver, max_entries=500),
        SearchFilesTool(resolver, max_results=500),
        SearchTextTool(resolver, max_matches=500, max_line_bytes=4096),
        EditFileTool(resolver),
        CreateFileTool(resolver),
        ShellTool(
            resolver,
            ShellBackend(config.shell_executable),
            default_timeout_seconds=60,
            max_stdout_bytes=64 * 1024,
            max_stderr_bytes=64 * 1024,
            excluded_environment_names=frozenset(
                {"CODING_AGENT_API_KEY", "OPENAI_API_KEY"}
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
    return AgentRuntime(
        concrete_model_client,
        ContextManager(),
        registry,
        RuntimeLimits(
            max_model_turns=50,
            max_tool_call_attempts=100,
            max_active_run_duration_seconds=30 * 60,
            max_transport_retries=2,
            max_consecutive_protocol_errors=3,
        ),
        workspace_resolver=resolver,
        policy_engine=PolicyEngine(),
        user_interaction=concrete_interaction,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="coding-agent")
    parser.add_argument(
        "--workspace",
        required=True,
        help="user-selected local workspace root",
    )
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
        config = load_config(args.workspace)
        runtime = build_runtime(config)
    except (OSError, ValueError) as error:
        print(f"Startup error: {error}", file=sys.stderr)
        return 2

    print(STARTUP_MESSAGE)
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
    run = runtime.run(task)
    if run.state is RunState.COMPLETED:
        stdout.write(f"{run.final_response}\n")
        return 0
    if run.state is RunState.CANCELLED:
        stdout.write("Run cancelled.\n")
        return 130
    stdout.write(
        "Run failed"
        + (f": {run.termination_reason.value}" if run.termination_reason else "")
        + "\n"
    )
    return 1


__all__ = [
    "CLIConfig",
    "ConsoleUserInteraction",
    "STARTUP_MESSAGE",
    "build_runtime",
    "load_config",
    "main",
]
