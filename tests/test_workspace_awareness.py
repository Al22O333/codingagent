"""Tests for conservative read-only Git workspace change awareness."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coding_agent.context import ContextManager
from coding_agent.edit_file import EditFileTool
from coding_agent.interaction import FakeUserInteraction
from coding_agent.model_client import FakeModelClient
from coding_agent.policy import PolicyEngine
from coding_agent.protocol import ModelResponse, ToolCall
from coding_agent.runtime import AgentRuntime, RuntimeEvent, RuntimeLimits, RunState
from coding_agent.shell import ShellBackend, ShellTool
from coding_agent.tooling import ToolRegistry
from coding_agent.workspace import WorkspacePathResolver
from coding_agent.workspace_awareness import (
    GitWorkspaceChangeObserver,
    WorkspaceAwarenessState,
    WorkspaceSnapshot,
    build_workspace_change_facts,
)


LIMITS = RuntimeLimits(8, 8, 30, 0, 2)


def _git(workspace: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True,
    )


def _initialize_repository(workspace: Path) -> None:
    workspace.mkdir()
    _git(workspace, "init")
    (workspace / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _git(workspace, "add", "tracked.txt")
    _git(
        workspace,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "baseline",
    )


def test_git_observer_reports_tracked_and_untracked_paths_without_mutation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    _initialize_repository(workspace)
    (workspace / "tracked.txt").write_text("user change\n", encoding="utf-8")
    (workspace / "new file.txt").write_text("new\n", encoding="utf-8")
    status_before = _git(workspace, "status", "--porcelain=v1").stdout

    snapshot = GitWorkspaceChangeObserver(workspace).snapshot()

    assert snapshot.state is WorkspaceAwarenessState.AVAILABLE
    assert snapshot.dirty_paths == ("new file.txt", "tracked.txt")
    assert snapshot.truncated is False
    assert _git(workspace, "status", "--porcelain=v1").stdout == status_before


def test_non_git_workspace_degrades_without_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setattr(
        "coding_agent.workspace_awareness.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 128, b"", b""),
    )
    assert (
        GitWorkspaceChangeObserver(plain).snapshot().state
        is WorkspaceAwarenessState.NOT_GIT
    )


def test_nested_workspace_degrades_without_inspecting_parent_scope(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    _initialize_repository(repository)
    nested = repository / "nested"
    nested.mkdir()
    nested_snapshot = GitWorkspaceChangeObserver(nested).snapshot()

    assert nested_snapshot.state is WorkspaceAwarenessState.UNAVAILABLE
    assert nested_snapshot.dirty_paths == ()


def test_git_observer_bounds_large_dirty_path_sets(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _initialize_repository(workspace)
    for index in range(205):
        (workspace / f"untracked-{index:03}.txt").write_text("x", encoding="utf-8")

    snapshot = GitWorkspaceChangeObserver(workspace).snapshot()

    assert snapshot.state is WorkspaceAwarenessState.AVAILABLE
    assert len(snapshot.dirty_paths) == 200
    assert snapshot.truncated is True


def test_git_observer_reports_both_rename_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _initialize_repository(workspace)
    _git(workspace, "mv", "tracked.txt", "renamed.txt")

    snapshot = GitWorkspaceChangeObserver(workspace).snapshot()

    assert snapshot.state is WorkspaceAwarenessState.AVAILABLE
    assert snapshot.dirty_paths == ("renamed.txt", "tracked.txt")


def test_change_facts_distinguish_preexisting_touched_and_other_paths() -> None:
    start = WorkspaceSnapshot(
        WorkspaceAwarenessState.AVAILABLE,
        ("user.txt",),
    )
    end = WorkspaceSnapshot(
        WorkspaceAwarenessState.AVAILABLE,
        ("agent.txt", "other.txt", "user.txt"),
    )

    facts = build_workspace_change_facts(
        start,
        end,
        ("agent.txt",),
        execution_uncertain=False,
    )

    assert facts.pre_existing_dirty_paths == ("user.txt",)
    assert facts.known_agent_touched_paths == ("agent.txt",)
    assert facts.new_or_other_dirty_paths == ("other.txt",)
    assert facts.attribution_uncertain is True


def test_change_facts_are_precise_only_for_disjoint_explained_paths() -> None:
    start = WorkspaceSnapshot(
        WorkspaceAwarenessState.AVAILABLE,
        ("user.txt",),
    )
    explained_end = WorkspaceSnapshot(
        WorkspaceAwarenessState.AVAILABLE,
        ("agent.txt", "user.txt"),
    )
    overlap_end = WorkspaceSnapshot(
        WorkspaceAwarenessState.AVAILABLE,
        ("user.txt",),
    )

    explained = build_workspace_change_facts(
        start,
        explained_end,
        ("agent.txt",),
        execution_uncertain=False,
    )
    overlap = build_workspace_change_facts(
        start,
        overlap_end,
        ("user.txt",),
        execution_uncertain=False,
    )

    assert explained.attribution_uncertain is False
    assert overlap.attribution_uncertain is True
    shell_uncertain = build_workspace_change_facts(
        WorkspaceSnapshot(WorkspaceAwarenessState.AVAILABLE),
        WorkspaceSnapshot(WorkspaceAwarenessState.AVAILABLE),
        (),
        execution_uncertain=True,
    )
    assert shell_uncertain.attribution_uncertain is True


class _SnapshotSequence:
    def __init__(self, *snapshots: WorkspaceSnapshot) -> None:
        self._snapshots = list(snapshots)

    def snapshot(self) -> WorkspaceSnapshot:
        return self._snapshots.pop(0)


def test_runtime_preserves_user_dirty_file_and_records_successful_file_touch(
    tmp_path: Path,
) -> None:
    user_file = tmp_path / "user.txt"
    agent_file = tmp_path / "agent.txt"
    user_file.write_text("user-owned change\n", encoding="utf-8")
    agent_file.write_text("old\n", encoding="utf-8")
    resolver = WorkspacePathResolver(tmp_path)
    registry = ToolRegistry()
    registry.register(EditFileTool(resolver))
    events: list[RuntimeEvent] = []
    observer = _SnapshotSequence(
        WorkspaceSnapshot(WorkspaceAwarenessState.AVAILABLE, ("user.txt",)),
        WorkspaceSnapshot(
            WorkspaceAwarenessState.AVAILABLE,
            ("agent.txt", "user.txt"),
        ),
    )
    runtime = AgentRuntime(
        FakeModelClient(
            [
                ModelResponse(
                    None,
                    (
                        ToolCall(
                            "edit",
                            "edit_file",
                            {
                                "path": "agent.txt",
                                "old_text": "old",
                                "new_text": "new",
                            },
                        ),
                    ),
                ),
                ModelResponse("Candidate."),
                ModelResponse("Audited final."),
            ]
        ),
        ContextManager(),
        registry,
        LIMITS,
        workspace_resolver=resolver,
        policy_engine=PolicyEngine(),
        user_interaction=FakeUserInteraction(),
        observer=events.append,
        workspace_change_observer=observer,
    )

    run = runtime.run("Update agent.txt without changing user.txt")

    assert run.state is RunState.COMPLETED
    assert user_file.read_text(encoding="utf-8") == "user-owned change\n"
    assert agent_file.read_text(encoding="utf-8") == "new\n"
    assert run.workspace_change_facts is not None
    assert run.workspace_change_facts.pre_existing_dirty_paths == ("user.txt",)
    assert run.workspace_change_facts.known_agent_touched_paths == ("agent.txt",)
    assert run.workspace_change_facts.new_or_other_dirty_paths == ()
    assert run.workspace_change_facts.attribution_uncertain is False
    summary = next(event for event in events if event.kind == "workspace_change_summary")
    assert summary.facts["pre_existing_count"] == 1
    assert summary.facts["known_touched_count"] == 1
    assert summary.facts["new_or_other_count"] == 0


def test_observer_failure_isolated_and_reported_as_uncertain(tmp_path: Path) -> None:
    class BrokenObserver:
        def snapshot(self) -> WorkspaceSnapshot:
            raise OSError("Git unavailable")

    runtime = AgentRuntime(
        FakeModelClient([ModelResponse("Final.")]),
        ContextManager(),
        ToolRegistry(),
        LIMITS,
        workspace_resolver=WorkspacePathResolver(tmp_path),
        policy_engine=PolicyEngine(),
        user_interaction=FakeUserInteraction(),
        workspace_change_observer=BrokenObserver(),
    )

    run = runtime.run("Answer without tools")

    assert run.state is RunState.COMPLETED
    assert run.workspace_change_facts is not None
    assert (
        run.workspace_change_facts.awareness_state
        is WorkspaceAwarenessState.UNAVAILABLE
    )
    assert run.workspace_change_facts.attribution_uncertain is True


def test_runtime_marks_any_shell_execution_as_attribution_uncertain(
    tmp_path: Path,
) -> None:
    resolver = WorkspacePathResolver(tmp_path)
    registry = ToolRegistry()
    shell_executable = os.environ.get("COMSPEC", "cmd.exe") if os.name == "nt" else "/bin/sh"
    registry.register(
        ShellTool(
            resolver,
            ShellBackend(shell_executable),
            default_timeout_seconds=5,
            max_timeout_seconds=10,
            max_stdout_bytes=1_024,
            max_stderr_bytes=1_024,
        )
    )
    observer = _SnapshotSequence(
        WorkspaceSnapshot(WorkspaceAwarenessState.AVAILABLE),
        WorkspaceSnapshot(WorkspaceAwarenessState.AVAILABLE),
    )
    runtime = AgentRuntime(
        FakeModelClient(
            [
                ModelResponse(
                    None,
                    (ToolCall("shell", "shell", {"command": "python --version"}),),
                ),
                ModelResponse("Candidate."),
                ModelResponse("Final."),
            ]
        ),
        ContextManager(),
        registry,
        LIMITS,
        workspace_resolver=resolver,
        policy_engine=PolicyEngine(),
        user_interaction=FakeUserInteraction(),
        workspace_change_observer=observer,
    )

    run = runtime.run("Check Python")

    assert run.state is RunState.COMPLETED
    assert run.workspace_change_facts is not None
    assert run.workspace_change_facts.known_agent_touched_paths == ()
    assert run.workspace_change_facts.attribution_uncertain is True
