"""Deterministic tests for the v1 Risk Permission matrix."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coding_agent.constraints import ConstraintDecision, ExplicitConstraintSnapshot
from coding_agent.edit_file import EditFileArguments, EditFileTool
from coding_agent.policy import PermissionDecision, PolicyEngine
from coding_agent.read_file import ReadFileArguments, ReadFileTool
from coding_agent.shell import ShellArguments, ShellBackend, ShellTool
from coding_agent.tooling import PreparedToolCall
from coding_agent.workspace import WorkspacePathResolver


def _read_tool(resolver: WorkspacePathResolver) -> ReadFileTool:
    return ReadFileTool(resolver, max_lines=10, max_bytes=1024)


def _shell_tool(resolver: WorkspacePathResolver) -> ShellTool:
    executable = os.environ.get("COMSPEC", "cmd.exe") if os.name == "nt" else "/bin/sh"
    return ShellTool(
        resolver,
        ShellBackend(executable=executable),
        default_timeout_seconds=5,
        max_stdout_bytes=100,
        max_stderr_bytes=100,
    )


def _prepare(tool, call_id: str, arguments) -> PreparedToolCall:
    prepared = tool.prepare(call_id, arguments)
    assert isinstance(prepared, PreparedToolCall)
    return prepared


def test_normal_file_read_and_mutation_are_allowed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("old", encoding="utf-8")
    resolver = WorkspacePathResolver(workspace)
    read = _prepare(_read_tool(resolver), "read", ReadFileArguments(path="main.py"))
    edit = _prepare(
        EditFileTool(resolver),
        "edit",
        EditFileArguments(path="main.py", old_text="old", new_text="new"),
    )
    policy = PolicyEngine()

    assert policy.check_risk_permission(read).decision is PermissionDecision.ALLOW
    assert policy.check_risk_permission(edit).decision is PermissionDecision.ALLOW


def test_sensitive_read_requires_confirmation(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".env").write_text("TOKEN=secret", encoding="utf-8")
    resolver = WorkspacePathResolver(workspace)
    prepared = _prepare(_read_tool(resolver), "read", ReadFileArguments(path=".env"))

    result = PolicyEngine().check_risk_permission(prepared)

    assert result.decision is PermissionDecision.CONFIRM
    assert result.reason_code == "SENSITIVE_PATH_CONFIRMATION"
    assert result.matched_rules == ("SENSITIVE_PATH",)


def test_protected_read_confirms_and_protected_mutation_denies(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    git_directory = workspace / ".git"
    git_directory.mkdir(parents=True)
    (git_directory / "config").write_text("old", encoding="utf-8")
    resolver = WorkspacePathResolver(workspace)
    read = _prepare(
        _read_tool(resolver),
        "read",
        ReadFileArguments(path=".git/config"),
    )
    edit = _prepare(
        EditFileTool(resolver),
        "edit",
        EditFileArguments(path=".git/config", old_text="old", new_text="new"),
    )
    policy = PolicyEngine()

    read_result = policy.check_risk_permission(read)
    edit_result = policy.check_risk_permission(edit)

    assert read_result.decision is PermissionDecision.CONFIRM
    assert read_result.reason_code == "PROTECTED_PATH_READ_CONFIRMATION"
    assert edit_result.decision is PermissionDecision.DENY
    assert edit_result.reason_code == "PROTECTED_PATH_MUTATION"


def test_outside_workspace_file_fact_is_denied(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    resolver = WorkspacePathResolver(workspace)
    prepared = _prepare(
        _read_tool(resolver),
        "outside",
        ReadFileArguments(path=str(outside)),
    )

    result = PolicyEngine().check_risk_permission(prepared)

    assert result.decision is PermissionDecision.DENY
    assert result.reason_code == "WORKSPACE_BOUNDARY"


@pytest.mark.parametrize(
    "command",
    ["pytest -q", "cmake --build build", "my_project_test_runner"],
)
def test_normal_and_simple_unknown_shell_commands_are_allowed(
    tmp_path: Path,
    command: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = _shell_tool(WorkspacePathResolver(workspace))
    prepared = _prepare(tool, "shell", ShellArguments(command=command))

    result = PolicyEngine().check_risk_permission(prepared)

    assert result.decision is PermissionDecision.ALLOW


@pytest.mark.parametrize(
    "command",
    [
        "pip install requests",
        "curl https://example.invalid",
        "git commit -m change",
        "git push origin main",
        "rm generated.txt",
    ],
)
def test_recognizable_high_risk_shell_actions_require_confirmation(
    tmp_path: Path,
    command: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    prepared = _prepare(
        _shell_tool(WorkspacePathResolver(workspace)),
        "shell",
        ShellArguments(command=command),
    )

    result = PolicyEngine().check_risk_permission(prepared)

    assert result.decision is PermissionDecision.CONFIRM
    assert result.reason_code == "SHELL_ACTION_CONFIRMATION"
    assert result.matched_rules


@pytest.mark.parametrize(
    "command",
    [
        "sudo pytest",
        "shutdown /s",
        "systemctl restart service",
        "nano main.py",
        "nohup pytest",
    ],
)
def test_recognizable_prohibited_shell_actions_are_denied(
    tmp_path: Path,
    command: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    prepared = _prepare(
        _shell_tool(WorkspacePathResolver(workspace)),
        "shell",
        ShellArguments(command=command),
    )

    result = PolicyEngine().check_risk_permission(prepared)

    assert result.decision is PermissionDecision.DENY
    assert result.reason_code == "SHELL_ACTION_DENIED"


def test_compound_command_uses_highest_recognizable_risk(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = _shell_tool(WorkspacePathResolver(workspace))
    confirm = _prepare(
        tool,
        "confirm",
        ShellArguments(command="pytest && git push origin main"),
    )
    deny = _prepare(
        tool,
        "deny",
        ShellArguments(command="pytest && sudo reboot"),
    )
    policy = PolicyEngine()

    assert policy.check_risk_permission(confirm).decision is PermissionDecision.CONFIRM
    assert policy.check_risk_permission(deny).decision is PermissionDecision.DENY


def test_ambiguous_complex_shell_confirms_but_simple_unknown_allows(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = _shell_tool(WorkspacePathResolver(workspace))
    complex_action = _prepare(
        tool,
        "complex",
        ShellArguments(command="my_runner > result.txt"),
    )
    simple_action = _prepare(
        tool,
        "simple",
        ShellArguments(command="my_runner"),
    )
    policy = PolicyEngine()

    complex_result = policy.check_risk_permission(complex_action)
    simple_result = policy.check_risk_permission(simple_action)

    assert complex_result.decision is PermissionDecision.CONFIRM
    assert complex_result.reason_code == "AMBIGUOUS_COMPLEX_SHELL"
    assert simple_result.decision is PermissionDecision.ALLOW


def test_constraint_and_risk_results_remain_separate(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("old", encoding="utf-8")
    resolver = WorkspacePathResolver(workspace)
    prepared = _prepare(
        EditFileTool(resolver),
        "edit",
        EditFileArguments(path="main.py", old_text="old", new_text="new"),
    )
    snapshot = ExplicitConstraintSnapshot(forbid_file_mutation=True)
    policy = PolicyEngine()

    constraint_result = policy.check_explicit_constraints(prepared, snapshot)
    risk_result = policy.check_risk_permission(prepared)

    assert constraint_result.decision is ConstraintDecision.REJECT
    assert risk_result.decision is PermissionDecision.ALLOW
    assert snapshot.forbid_file_mutation is True
