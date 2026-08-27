"""Tests for Step 16 prepared local actions and Shell surface facts."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from coding_agent.context import ContextManager
from coding_agent.edit_file import EditFileArguments, EditFileTool
from coding_agent.model_client import FakeModelClient
from coding_agent.protocol import (
    ModelResponse,
    ToolCall,
    ToolCapability,
    ToolKind,
    ToolOutcome,
)
from coding_agent.read_file import ReadFileArguments, ReadFileTool
from coding_agent.runtime import AgentRuntime, RuntimeLimits
from coding_agent.shell import (
    ShellArguments,
    ShellBackend,
    ShellOperationFacts,
    ShellRiskAction,
    ShellTool,
    classify_shell_surface,
)
from coding_agent.tooling import (
    PreparedToolCall,
    Tool,
    ToolArguments,
    ToolExecutionResult,
    ToolRegistry,
)
from coding_agent.workspace import FileOperationFacts, WorkspacePathResolver


TEST_LIMITS = RuntimeLimits(
    max_model_turns=5,
    max_tool_call_attempts=5,
    max_active_run_duration_seconds=30,
    max_transport_retries=0,
    max_consecutive_protocol_errors=1,
)


def test_mutation_preparation_binds_exact_arguments_and_affected_path(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("old", encoding="utf-8")
    tool = EditFileTool(WorkspacePathResolver(workspace))
    arguments = EditFileArguments(
        path="main.py",
        old_text="old",
        new_text="new",
    )

    prepared = tool.prepare("edit-1", arguments)

    assert isinstance(prepared, PreparedToolCall)
    assert prepared.call_id == "edit-1"
    assert prepared.tool_identity == tool.spec
    assert prepared.validated_arguments is arguments
    assert isinstance(prepared.operation_facts, FileOperationFacts)
    assert prepared.operation_facts.target.workspace_relative_path == "main.py"
    assert prepared.operation_facts.affected_paths == (
        prepared.operation_facts.target,
    )
    with pytest.raises(FrozenInstanceError):
        prepared.call_id = "changed"  # type: ignore[misc]


def test_outside_workspace_is_prepared_as_policy_fact(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    tool = ReadFileTool(
        WorkspacePathResolver(workspace),
        max_lines=10,
        max_bytes=1024,
    )

    prepared = tool.prepare(
        "outside",
        ReadFileArguments(path=str(outside)),
    )

    assert isinstance(prepared, PreparedToolCall)
    assert isinstance(prepared.operation_facts, FileOperationFacts)
    assert prepared.operation_facts.target.is_within_workspace is False


def test_shell_preparation_contains_exact_execution_and_surface_facts(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = ShellTool(
        WorkspacePathResolver(workspace),
        ShellBackend(executable="unused-shell"),
        default_timeout_seconds=9,
        max_stdout_bytes=10,
        max_stderr_bytes=10,
    )
    arguments = ShellArguments(command="pytest && git push origin main")

    prepared = tool.prepare("shell-1", arguments)

    assert isinstance(prepared, PreparedToolCall)
    assert isinstance(prepared.operation_facts, ShellOperationFacts)
    facts = prepared.operation_facts
    assert facts.command == arguments.command
    assert facts.cwd.workspace_relative_path == "."
    assert facts.effective_timeout_seconds == 9
    assert facts.surface_facts.has_compound_syntax is True
    assert facts.surface_facts.has_unknown_segment is False
    assert facts.surface_facts.recognized_actions == frozenset(
        {
            ShellRiskAction.GIT_MUTATION,
            ShellRiskAction.GIT_REMOTE_WRITE,
            ShellRiskAction.NETWORK_ACCESS,
        }
    )


@pytest.mark.parametrize(
    ("command", "actions", "compound", "unknown"),
    [
        ("pytest -q", frozenset(), False, False),
        ("my_project_test_runner", frozenset(), False, True),
        (
            "pip install pydantic",
            frozenset({ShellRiskAction.DEPENDENCY_INSTALL}),
            False,
            False,
        ),
        (
            "rm generated.txt",
            frozenset({ShellRiskAction.FILE_DELETION}),
            False,
            False,
        ),
        (
            "custom_runner | curl https://example.invalid",
            frozenset({ShellRiskAction.NETWORK_ACCESS}),
            True,
            True,
        ),
        ("pytest -q > result.txt", frozenset(), True, True),
        (
            "nano main.py",
            frozenset({ShellRiskAction.INTERACTIVE_COMMAND}),
            False,
            False,
        ),
    ],
)
def test_shell_surface_classifier_returns_facts_only(
    command: str,
    actions: frozenset[ShellRiskAction],
    compound: bool,
    unknown: bool,
) -> None:
    facts = classify_shell_surface(command)

    assert facts.recognized_actions == actions
    assert facts.has_compound_syntax is compound
    assert facts.has_unknown_segment is unknown
    assert not hasattr(facts, "decision")


class BuggyArguments(ToolArguments):
    value: str


class BuggyPreparationTool(Tool[BuggyArguments]):
    def __init__(self) -> None:
        super().__init__(
            name="buggy",
            description="Test-only preparation failure",
            argument_model=BuggyArguments,
            kind=ToolKind.LOCAL,
            capabilities=frozenset({ToolCapability.FILE_READ}),
        )

    def prepare(self, call_id: str, arguments: BuggyArguments):
        raise RuntimeError("unexpected preparation bug")

    def execute(self, prepared_call: PreparedToolCall) -> ToolExecutionResult:
        raise AssertionError("execution must not be reached")


def test_unexpected_preparation_bug_becomes_internal_tool_error() -> None:
    call = ToolCall(call_id="buggy-1", name="buggy", raw_arguments={"value": "x"})
    client = FakeModelClient(
        [
            ModelResponse(text=None, tool_calls=(call,)),
            ModelResponse(text="Observed the tool failure."),
        ]
    )
    registry = ToolRegistry()
    registry.register(BuggyPreparationTool())
    context = ContextManager()
    runtime = AgentRuntime(client, context, registry, TEST_LIMITS)

    runtime.run("Trigger preparation")

    result = context.build_messages()[2].results[0]  # type: ignore[union-attr]
    assert result.outcome is ToolOutcome.OPERATION_FAILURE
    assert result.error is not None
    assert result.error.code == "INTERNAL_TOOL_ERROR"
