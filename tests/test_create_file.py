"""Tests for race-safe create-only file creation."""

from __future__ import annotations

from pathlib import Path

from coding_agent.create_file import (
    CreateFileArguments,
    CreateFileContent,
    CreateFileTool,
)
from coding_agent.protocol import ToolError, ToolOutcome
from coding_agent.workspace import ResolvedPath, WorkspacePathResolver


def test_create_file_writes_utf8_and_reports_bytes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = CreateFileTool(WorkspacePathResolver(workspace))
    arguments = CreateFileArguments(path="hello.txt", content="你好\n")
    prepared = tool.prepare(arguments)
    assert isinstance(prepared, ResolvedPath)

    result = tool.execute(arguments, prepared)

    expected = "你好\n".encode("utf-8")
    assert result.outcome is ToolOutcome.SUCCESS
    assert result.error is None
    assert (workspace / "hello.txt").read_bytes() == expected
    assert isinstance(result.content, CreateFileContent)
    assert result.content.path == "hello.txt"
    assert result.content.bytes_written == len(expected)


def test_existing_target_is_rejected_during_preparation(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "existing.txt"
    target.write_text("original", encoding="utf-8")
    tool = CreateFileTool(WorkspacePathResolver(workspace))

    prepared = tool.prepare(
        CreateFileArguments(path="existing.txt", content="replacement")
    )

    assert isinstance(prepared, ToolError)
    assert prepared.code == "FILE_ALREADY_EXISTS"
    assert target.read_text(encoding="utf-8") == "original"


def test_missing_direct_parent_is_rejected_without_mkdir(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = CreateFileTool(WorkspacePathResolver(workspace))

    prepared = tool.prepare(
        CreateFileArguments(path="missing/child.txt", content="content")
    )

    assert isinstance(prepared, ToolError)
    assert prepared.code == "PARENT_DIRECTORY_NOT_FOUND"
    assert not (workspace / "missing").exists()


def test_exclusive_create_rejects_target_created_after_preparation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "raced.txt"
    tool = CreateFileTool(WorkspacePathResolver(workspace))
    arguments = CreateFileArguments(path="raced.txt", content="agent")
    prepared = tool.prepare(arguments)
    assert isinstance(prepared, ResolvedPath)
    target.write_text("other process", encoding="utf-8")

    result = tool.execute(arguments, prepared)

    assert result.outcome is ToolOutcome.OPERATION_FAILURE
    assert result.error is not None
    assert result.error.code == "FILE_ALREADY_EXISTS"
    assert target.read_text(encoding="utf-8") == "other process"


def test_empty_file_creation_is_supported(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = CreateFileTool(WorkspacePathResolver(workspace))
    arguments = CreateFileArguments(path="empty.txt", content="")
    prepared = tool.prepare(arguments)
    assert isinstance(prepared, ResolvedPath)

    result = tool.execute(arguments, prepared)

    assert result.outcome is ToolOutcome.SUCCESS
    assert isinstance(result.content, CreateFileContent)
    assert result.content.bytes_written == 0
    assert (workspace / "empty.txt").read_bytes() == b""
