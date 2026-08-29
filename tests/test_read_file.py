"""Tests for the bounded read_file LOCAL Tool."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from coding_agent.protocol import ToolCapability, ToolError, ToolKind, ToolOutcome
from coding_agent.read_file import ReadFileArguments, ReadFileContent, ReadFileTool
from coding_agent.tooling import PreparedToolCall
from coding_agent.workspace import FileOperationFacts, WorkspacePathResolver


def _tool(workspace: Path, *, max_lines: int = 3, max_bytes: int = 1024) -> ReadFileTool:
    return ReadFileTool(
        WorkspacePathResolver(workspace),
        max_lines=max_lines,
        max_bytes=max_bytes,
    )


def _prepare_inside(
    tool: ReadFileTool,
    arguments: ReadFileArguments,
) -> PreparedToolCall:
    prepared = tool.prepare("test-call", arguments)
    assert isinstance(prepared, PreparedToolCall)
    assert isinstance(prepared.operation_facts, FileOperationFacts)
    assert prepared.operation_facts.target.is_within_workspace is True
    return prepared


def test_read_file_spec_and_argument_validation(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = _tool(workspace)

    assert tool.spec.name == "read_file"
    assert tool.spec.kind is ToolKind.LOCAL
    assert tool.spec.capabilities == frozenset({ToolCapability.FILE_READ})
    assert tool.validate({"path": "main.py"}) == ReadFileArguments(path="main.py")

    with pytest.raises(ValidationError):
        tool.validate({"path": "main.py", "start_line": 0})
    with pytest.raises(ValidationError):
        tool.validate({"path": "main.py", "start_line": 3, "end_line": 2})
    with pytest.raises(ValidationError):
        tool.validate({"path": "main.py", "unknown": True})


def test_normal_text_read_has_model_facing_line_numbers(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text(
        "import os\n\ndef main():\n    pass\n",
        encoding="utf-8",
    )
    tool = _tool(workspace, max_lines=10)
    arguments = ReadFileArguments(path="main.py", start_line=2, end_line=3)

    result = tool.execute(_prepare_inside(tool, arguments))

    assert result.outcome is ToolOutcome.SUCCESS
    assert result.error is None
    assert result.content == ReadFileContent(
        path="main.py",
        start_line=2,
        end_line=3,
        total_lines=4,
        content="2 | \n3 | def main():",
        truncated=True,
        next_start_line=4,
    )


def test_empty_text_file_returns_successful_zero_line_observation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "empty.py").write_text("", encoding="utf-8")
    tool = _tool(workspace, max_lines=10)
    arguments = ReadFileArguments(path="empty.py")

    result = tool.execute(_prepare_inside(tool, arguments))

    assert result.outcome is ToolOutcome.SUCCESS
    assert result.content == ReadFileContent(
        path="empty.py",
        start_line=1,
        end_line=None,
        total_lines=0,
        content="",
        truncated=False,
        next_start_line=None,
    )


def test_large_file_read_is_bounded_and_can_continue(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "large.txt").write_text(
        "\n".join(f"line {number}" for number in range(1, 8)),
        encoding="utf-8",
    )
    tool = _tool(workspace, max_lines=3)

    first_arguments = ReadFileArguments(path="large.txt")
    first = tool.execute(_prepare_inside(tool, first_arguments))
    assert first.content == ReadFileContent(
        path="large.txt",
        start_line=1,
        end_line=3,
        total_lines=7,
        content="1 | line 1\n2 | line 2\n3 | line 3",
        truncated=True,
        next_start_line=4,
    )

    continuation_arguments = ReadFileArguments(path="large.txt", start_line=4)
    continuation = tool.execute(
        _prepare_inside(tool, continuation_arguments),
    )
    assert continuation.content == ReadFileContent(
        path="large.txt",
        start_line=4,
        end_line=6,
        total_lines=7,
        content="4 | line 4\n5 | line 5\n6 | line 6",
        truncated=True,
        next_start_line=7,
    )

    final_arguments = ReadFileArguments(path="large.txt", start_line=7)
    final = tool.execute(_prepare_inside(tool, final_arguments))
    assert isinstance(final.content, ReadFileContent)
    assert final.content.end_line == 7
    assert final.content.truncated is False
    assert final.content.next_start_line is None


def test_returned_content_is_bounded_by_utf8_bytes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "unicode.txt").write_text("你" * 20, encoding="utf-8")
    tool = _tool(workspace, max_lines=5, max_bytes=10)
    arguments = ReadFileArguments(path="unicode.txt")

    result = tool.execute(_prepare_inside(tool, arguments))

    assert isinstance(result.content, ReadFileContent)
    assert len(result.content.content.encode("utf-8")) <= 10
    assert result.content.truncated is True
    assert result.content.next_start_line == 2


def test_missing_file_returns_structured_preparation_error(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = _tool(workspace)

    prepared = tool.prepare("missing", ReadFileArguments(path="missing.txt"))

    assert isinstance(prepared, ToolError)
    assert prepared.code == "FILE_NOT_FOUND"


def test_directory_returns_not_a_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    directory = workspace / "directory"
    directory.mkdir(parents=True)
    tool = _tool(workspace)

    prepared = tool.prepare("directory", ReadFileArguments(path="directory"))

    assert isinstance(prepared, ToolError)
    assert prepared.code == "NOT_A_FILE"


def test_binary_file_returns_structured_operation_failure(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "binary.dat").write_bytes(b"text\x00binary")
    tool = _tool(workspace)
    arguments = ReadFileArguments(path="binary.dat")

    result = tool.execute(_prepare_inside(tool, arguments))

    assert result.outcome is ToolOutcome.OPERATION_FAILURE
    assert result.error is not None
    assert result.error.code == "BINARY_FILE_UNSUPPORTED"


def test_invalid_utf8_returns_structured_operation_failure(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "invalid.txt").write_bytes(b"invalid: \xff")
    tool = _tool(workspace)
    arguments = ReadFileArguments(path="invalid.txt")

    result = tool.execute(_prepare_inside(tool, arguments))

    assert result.outcome is ToolOutcome.OPERATION_FAILURE
    assert result.error is not None
    assert result.error.code == "TEXT_DECODING_FAILED"


def test_outside_workspace_is_a_resolved_policy_fact(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    tool = _tool(workspace)

    prepared = tool.prepare("outside", ReadFileArguments(path=str(outside)))

    assert isinstance(prepared, PreparedToolCall)
    assert isinstance(prepared.operation_facts, FileOperationFacts)
    assert prepared.operation_facts.target.is_within_workspace is False
    assert prepared.operation_facts.target.workspace_relative_path is None


def test_constructor_requires_positive_output_limits(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resolver = WorkspacePathResolver(workspace)

    with pytest.raises(ValueError, match="max_lines"):
        ReadFileTool(resolver, max_lines=0, max_bytes=100)
    with pytest.raises(ValueError, match="max_bytes"):
        ReadFileTool(resolver, max_lines=10, max_bytes=0)


def test_large_file_read_streams_without_whole_file_read_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "large.txt"
    target.write_text("first\n" + ("x" * (2 * 1024 * 1024)) + "\nlast\n", encoding="utf-8")
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda self: (_ for _ in ()).throw(AssertionError("whole-file read")),
    )

    tool = _tool(workspace, max_lines=1, max_bytes=32)
    result = tool.execute(
        _prepare_inside(tool, ReadFileArguments(path="large.txt"))
    )

    assert result.outcome is ToolOutcome.SUCCESS
    assert isinstance(result.content, ReadFileContent)
    assert result.content.total_lines == 3
    assert result.content.content == "1 | first"
    assert result.content.truncated is True
