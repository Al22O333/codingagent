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
from coding_agent.create_file import CreateFileTool
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
    policy_engine: PolicyEngine | None = None,
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
        policy_engine=policy_engine or PolicyEngine(),
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


@pytest.mark.parametrize("tool_name", ["edit_file", "create_file"])
def test_audit_file_prohibition_rejects_mutation_tools_at_runtime(
    tmp_path: Path,
    tool_name: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "main.py"
    resolver = WorkspacePathResolver(workspace)
    if tool_name == "edit_file":
        target.write_bytes(b"old")
        tool = EditFileTool(resolver)
        arguments = {"path": "main.py", "old_text": "old", "new_text": "new"}
    else:
        tool = CreateFileTool(resolver)
        arguments = {"path": "main.py", "content": "new"}
    call = ToolCall(tool_name, tool_name, arguments)

    _, run, result = _runtime(
        workspace,
        tool,
        call,
        "找出测试为什么失败，不要修改任何文件。",
    )

    assert result.outcome is ToolOutcome.POLICY_REJECTED
    assert result.error is not None
    assert result.error.code == "FORBID_FILE_MUTATION"
    assert run.explicit_task_constraints.forbid_file_mutation is True
    if tool_name == "edit_file":
        assert target.read_bytes() == b"old"
    else:
        assert not target.exists()


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


def test_audit_command_prohibition_rejects_before_risk_permission(
    tmp_path: Path,
) -> None:
    class RiskPermissionMustNotRun(PolicyEngine):
        def check_risk_permission(self, prepared_call):  # type: ignore[no-untyped-def]
            raise AssertionError("Risk Permission ran after hard-constraint rejection")

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
        "shell",
        "shell",
        {"command": f"echo forbidden > {marker.name}"},
    )

    _, run, result = _runtime(
        workspace,
        tool,
        call,
        "只看代码，不要运行任何命令。",
        policy_engine=RiskPermissionMustNotRun(),
    )

    assert result.outcome is ToolOutcome.POLICY_REJECTED
    assert result.error is not None
    assert result.error.code == "FORBID_COMMAND_EXECUTION"
    assert run.explicit_task_constraints.forbid_command_execution is True
    assert not marker.exists()


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


@pytest.mark.parametrize(
    ("user_input", "field"),
    [
        ("不要修改文件", "forbid_file_mutation"),
        ("不要改文件", "forbid_file_mutation"),
        ("不要改任何文件", "forbid_file_mutation"),
        ("do not modify files", "forbid_file_mutation"),
        ("do not modify any files", "forbid_file_mutation"),
        ("don't modify files", "forbid_file_mutation"),
        (
            "Find the failing test, but do not modify any files.",
            "forbid_file_mutation",
        ),
        ("don't modify any files", "forbid_file_mutation"),
        ("不要运行命令", "forbid_command_execution"),
        ("不要运行任何命令", "forbid_command_execution"),
        ("不要执行命令", "forbid_command_execution"),
        ("不要执行任何命令", "forbid_command_execution"),
        ("do not run commands", "forbid_command_execution"),
        ("do not run any commands", "forbid_command_execution"),
        (
            "Inspect the project, but don't run any commands.",
            "forbid_command_execution",
        ),
        ("don't run commands", "forbid_command_execution"),
        ("DON'T RUN ANY COMMANDS!", "forbid_command_execution"),
    ],
)
def test_supported_explicit_forbid_forms_remain_closed_and_deterministic(
    tmp_path: Path,
    user_input: str,
    field: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    update = normalize_explicit_constraint_update(
        user_input,
        WorkspacePathResolver(workspace),
    )

    assert update is not None
    assert getattr(update, field) is True


@pytest.mark.parametrize(
    "user_input",
    [
        "不要修改 tests/ 之外的文件",
        "如果不需要就不要修改文件",
        "模型说不要修改文件",
        '模型说“不要修改文件”',
    ],
)
def test_conditional_scoped_or_reported_text_is_not_a_global_file_prohibition(
    tmp_path: Path,
    user_input: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    update = normalize_explicit_constraint_update(
        user_input,
        WorkspacePathResolver(workspace),
    )

    assert update is None or update.forbid_file_mutation is not True


def test_negative_chinese_forms_are_not_mistaken_for_constraint_removal(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resolver = WorkspacePathResolver(workspace)

    update = normalize_explicit_constraint_update("不可以修改文件", resolver)

    assert update is not None
    assert update.forbid_file_mutation is True
