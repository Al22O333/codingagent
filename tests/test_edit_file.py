"""Tests for conflict-safe exact file editing."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from pydantic import ValidationError

from coding_agent.edit_file import EditFileArguments, EditFileContent, EditFileTool
from coding_agent.protocol import ToolOutcome
from coding_agent.tooling import PreparedToolCall
from coding_agent.workspace import WorkspacePathResolver


def _prepare(
    tool: EditFileTool,
    arguments: EditFileArguments,
) -> PreparedToolCall:
    prepared = tool.prepare("edit", arguments)
    assert isinstance(prepared, PreparedToolCall)
    return prepared


def test_exact_single_replacement_returns_summary(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "main.py"
    target.write_bytes(b"value = 1\n")
    tool = EditFileTool(WorkspacePathResolver(workspace))
    arguments = EditFileArguments(
        path="main.py",
        old_text="value = 1",
        new_text="value = 2",
    )

    result = tool.execute(_prepare(tool, arguments))

    assert result.outcome is ToolOutcome.SUCCESS
    assert result.error is None
    assert target.read_bytes() == b"value = 2\n"
    assert isinstance(result.content, EditFileContent)
    assert result.content.path == "main.py"
    assert result.content.replacement_count == 1
    assert result.content.bytes_before == len(b"value = 1\n")
    assert result.content.bytes_after == len(b"value = 2\n")


def test_zero_match_detects_stale_observation_without_mutation(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "main.py"
    target.write_text("value = 1\n", encoding="utf-8")
    tool = EditFileTool(WorkspacePathResolver(workspace))
    arguments = EditFileArguments(
        path="main.py",
        old_text="value = 1",
        new_text="value = 2",
    )
    prepared = _prepare(tool, arguments)
    target.write_text("value = 3\n", encoding="utf-8")

    result = tool.execute(prepared)

    assert result.outcome is ToolOutcome.OPERATION_FAILURE
    assert result.error is not None
    assert result.error.code == "EDIT_TARGET_NOT_FOUND"
    assert result.error.details["actual_count"] == 0
    assert target.read_text(encoding="utf-8") == "value = 3\n"


def test_ambiguous_match_count_fails_closed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "main.py"
    original = "item\nitem\nitem\n"
    target.write_text(original, encoding="utf-8")
    tool = EditFileTool(WorkspacePathResolver(workspace))
    arguments = EditFileArguments(
        path="main.py",
        old_text="item",
        new_text="changed",
        expected_count=1,
    )

    result = tool.execute(_prepare(tool, arguments))

    assert result.outcome is ToolOutcome.OPERATION_FAILURE
    assert result.error is not None
    assert result.error.code == "EDIT_MATCH_COUNT_MISMATCH"
    assert result.error.details == {
        "path": "main.py",
        "expected_count": 1,
        "actual_count": 3,
    }
    assert target.read_text(encoding="utf-8") == original


def test_expected_count_greater_than_one_replaces_all_exact_matches(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "main.py"
    target.write_text("item\nitem\nitem\n", encoding="utf-8")
    tool = EditFileTool(WorkspacePathResolver(workspace))
    arguments = EditFileArguments(
        path="main.py",
        old_text="item",
        new_text="changed",
        expected_count=3,
    )

    result = tool.execute(_prepare(tool, arguments))

    assert result.outcome is ToolOutcome.SUCCESS
    assert isinstance(result.content, EditFileContent)
    assert result.content.replacement_count == 3
    assert target.read_text(encoding="utf-8") == "changed\nchanged\nchanged\n"


def test_crlf_and_mixed_line_endings_are_preserved(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "main.py"
    target.write_bytes(b"first\r\nold value\r\nthird\nfourth\r\n")
    tool = EditFileTool(WorkspacePathResolver(workspace))
    arguments = EditFileArguments(
        path="main.py",
        old_text="old value",
        new_text="new value",
    )

    result = tool.execute(_prepare(tool, arguments))

    assert result.outcome is ToolOutcome.SUCCESS
    assert target.read_bytes() == b"first\r\nnew value\r\nthird\nfourth\r\n"


def test_write_failure_preserves_original_and_removes_temporary_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "main.py"
    original = b"value = 1\n"
    target.write_bytes(original)
    tool = EditFileTool(WorkspacePathResolver(workspace))
    arguments = EditFileArguments(
        path="main.py",
        old_text="value = 1",
        new_text="value = 2",
    )

    def fail_replace(source, destination) -> None:
        raise PermissionError("simulated replacement failure")

    monkeypatch.setattr("coding_agent.edit_file.os.replace", fail_replace)
    result = tool.execute(_prepare(tool, arguments))

    assert result.outcome is ToolOutcome.OPERATION_FAILURE
    assert result.error is not None
    assert result.error.code == "EDIT_WRITE_FAILED"
    assert target.read_bytes() == original
    assert list(workspace.glob(".main.py.*.tmp")) == []


@pytest.mark.skipif(os.name == "nt", reason="Windows mode bits are not equivalent")
def test_original_permission_mode_is_preserved(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "script.sh"
    target.write_text("old\n", encoding="utf-8")
    target.chmod(0o754)
    tool = EditFileTool(WorkspacePathResolver(workspace))
    arguments = EditFileArguments(
        path="script.sh",
        old_text="old",
        new_text="new",
    )

    result = tool.execute(_prepare(tool, arguments))

    assert result.outcome is ToolOutcome.SUCCESS
    assert stat.S_IMODE(target.stat().st_mode) == 0o754


@pytest.mark.parametrize(
    "kwargs",
    [
        {"path": "main.py", "old_text": "", "new_text": "new"},
        {
            "path": "main.py",
            "old_text": "old",
            "new_text": "new",
            "expected_count": 0,
        },
    ],
)
def test_edit_arguments_reject_empty_target_and_nonpositive_count(
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        EditFileArguments(**kwargs)
