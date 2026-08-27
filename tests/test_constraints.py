"""Tests for closed Explicit Task Constraint normalization and enforcement."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coding_agent.constraints import (
    ConstraintDecision,
    ExplicitConstraintSnapshot,
    check_explicit_constraints,
    normalize_explicit_constraint_update,
)
from coding_agent.context import ContextManager
from coding_agent.interaction import FakeUserInteraction
from coding_agent.edit_file import EditFileArguments, EditFileTool
from coding_agent.model_client import FakeModelClient
from coding_agent.policy import PolicyEngine
from coding_agent.protocol import ModelResponse, ToolCall, ToolOutcome
from coding_agent.runtime import AgentRun, AgentRuntime, RuntimeLimits
from coding_agent.shell import ShellBackend, ShellTool
from coding_agent.tooling import PreparedToolCall, ToolRegistry
from coding_agent.workspace import WorkspacePathResolver


TEST_LIMITS = RuntimeLimits(
    max_model_turns=5,
    max_tool_call_attempts=5,
    max_active_run_duration_seconds=30,
    max_transport_retries=0,
    max_consecutive_protocol_errors=1,
)


def _runtime(
    workspace: Path,
    tool,
    call: ToolCall,
    task: str,
    *,
    assistant_text: str = "Proposed action.",
):
    registry = ToolRegistry()
    registry.register(tool)
    context = ContextManager()
    client = FakeModelClient(
        [
            ModelResponse(text=assistant_text, tool_calls=(call,)),
            ModelResponse(text="Finished."),
        ]
    )
    resolver = WorkspacePathResolver(workspace)
    runtime = AgentRuntime(
        client,
        context,
        registry,
        TEST_LIMITS,
        workspace_resolver=resolver,
        policy_engine=PolicyEngine(),
        user_interaction=FakeUserInteraction(),
    )
    run = runtime.run(task)
    result = context.build_messages()[2].results[0]  # type: ignore[union-attr]
    return runtime, run, result


def test_explicit_file_mutation_prohibition_is_hard_enforced(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "main.py"
    target.write_bytes(b"old")
    resolver = WorkspacePathResolver(workspace)
    tool = EditFileTool(resolver)
    call = ToolCall(
        call_id="edit",
        name="edit_file",
        raw_arguments={"path": "main.py", "old_text": "old", "new_text": "new"},
    )

    _, run, result = _runtime(
        workspace,
        tool,
        call,
        "检查问题，但不要修改文件",
    )

    assert result.outcome is ToolOutcome.POLICY_REJECTED
    assert result.error is not None
    assert result.error.code == "FORBID_FILE_MUTATION"
    assert target.read_bytes() == b"old"
    assert run.explicit_task_constraints.forbid_file_mutation is True


def test_explicit_command_prohibition_stops_shell_before_launch(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = workspace / "must-not-exist"
    resolver = WorkspacePathResolver(workspace)
    tool = ShellTool(
        resolver,
        ShellBackend(executable=str(workspace / "missing-shell")),
        default_timeout_seconds=2,
        max_stdout_bytes=100,
        max_stderr_bytes=100,
    )
    call = ToolCall(
        call_id="shell",
        name="shell",
        raw_arguments={"command": f"echo forbidden > {marker.name}"},
    )

    _, run, result = _runtime(workspace, tool, call, "不要运行命令")

    assert result.outcome is ToolOutcome.POLICY_REJECTED
    assert result.error is not None
    assert result.error.code == "FORBID_COMMAND_EXECUTION"
    assert not marker.exists()
    assert run.explicit_task_constraints.forbid_command_execution is True


def test_write_scope_allows_inside_and_rejects_outside(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    allowed = workspace / "tests"
    allowed.mkdir(parents=True)
    (workspace / "src").mkdir()
    inside = allowed / "test_main.py"
    outside = workspace / "src" / "main.py"
    inside.write_bytes(b"old")
    outside.write_bytes(b"old")

    inside_resolver = WorkspacePathResolver(workspace)
    inside_call = ToolCall(
        call_id="inside",
        name="edit_file",
        raw_arguments={
            "path": "tests/test_main.py",
            "old_text": "old",
            "new_text": "new",
        },
    )
    _, inside_run, inside_result = _runtime(
        workspace,
        EditFileTool(inside_resolver),
        inside_call,
        "只修改 tests/",
    )

    outside_resolver = WorkspacePathResolver(workspace)
    outside_call = ToolCall(
        call_id="outside",
        name="edit_file",
        raw_arguments={"path": "src/main.py", "old_text": "old", "new_text": "new"},
    )
    _, _, outside_result = _runtime(
        workspace,
        EditFileTool(outside_resolver),
        outside_call,
        "只修改 tests/",
    )

    assert inside_result.outcome is ToolOutcome.SUCCESS
    assert inside.read_bytes() == b"new"
    assert len(inside_run.explicit_task_constraints.write_scopes) == 1
    assert outside_result.outcome is ToolOutcome.POLICY_REJECTED
    assert outside_result.error is not None
    assert outside_result.error.code == "WRITE_SCOPE"
    assert outside.read_bytes() == b"old"


@pytest.mark.skipif(os.name == "nt", reason="symlink creation is commonly restricted")
def test_write_scope_uses_canonical_symlink_facts(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    actual = workspace / "actual"
    actual.mkdir(parents=True)
    alias = workspace / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    target = actual / "main.py"
    target.write_bytes(b"old")
    resolver = WorkspacePathResolver(workspace)
    tool = EditFileTool(resolver)
    arguments = EditFileArguments(
        path="actual/main.py",
        old_text="old",
        new_text="new",
    )
    prepared = tool.prepare("edit", arguments)
    assert isinstance(prepared, PreparedToolCall)
    update = normalize_explicit_constraint_update("只修改 alias/", resolver)
    assert update is not None and update.write_scopes is not None
    snapshot = ExplicitConstraintSnapshot(write_scopes=update.write_scopes)

    result = check_explicit_constraints(prepared, snapshot)

    assert result.decision is ConstraintDecision.PASS


def test_assistant_text_cannot_create_a_hard_constraint(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "main.py"
    target.write_bytes(b"old")
    resolver = WorkspacePathResolver(workspace)
    tool = EditFileTool(resolver)
    call = ToolCall(
        call_id="edit",
        name="edit_file",
        raw_arguments={"path": "main.py", "old_text": "old", "new_text": "new"},
    )

    _, run, result = _runtime(
        workspace,
        tool,
        call,
        "Update main.py",
        assistant_text="不要修改文件",
    )

    assert result.outcome is ToolOutcome.SUCCESS
    assert target.read_bytes() == b"new"
    assert run.explicit_task_constraints == ExplicitConstraintSnapshot()


def test_trusted_clarification_update_changes_same_run_snapshot(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()
    runtime = AgentRuntime(
        FakeModelClient([]),
        ContextManager(),
        registry,
        TEST_LIMITS,
        workspace_resolver=WorkspacePathResolver(workspace),
        policy_engine=PolicyEngine(),
        user_interaction=FakeUserInteraction(),
    )
    run = AgentRun(run_id="active", current_task="Inspect the project")
    runtime.session._add_run(run)

    normalized = runtime.apply_user_clarification(run, "只修改 tests/")

    assert normalized is True
    assert run.explicit_user_clarifications == ["只修改 tests/"]
    assert run.explicit_scope_updates == ["只修改 tests/"]
    assert run.explicit_task_constraints.write_scopes[0].workspace_relative_path == "tests"


def test_unrecognized_semantics_do_not_become_hard_constraints(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resolver = WorkspacePathResolver(workspace)

    update = normalize_explicit_constraint_update(
        "看看这个实现有没有问题，处理一下",
        resolver,
    )

    assert update is None


def test_negative_chinese_forms_are_not_mistaken_for_constraint_removal(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resolver = WorkspacePathResolver(workspace)

    update = normalize_explicit_constraint_update("不可以修改文件", resolver)

    assert update is not None
    assert update.forbid_file_mutation is True
