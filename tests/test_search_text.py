"""Tests for the Python-baseline search_text Tool."""

from __future__ import annotations

from pathlib import Path

import pytest

from coding_agent.protocol import ToolOutcome
from coding_agent.search_text import (
    SearchTextArguments,
    SearchTextContent,
    SearchTextTool,
)
from coding_agent.tooling import PreparedToolCall
from coding_agent.workspace import WorkspacePathResolver


def _tool(
    workspace: Path,
    *,
    max_matches: int = 20,
    max_line_bytes: int = 200,
) -> SearchTextTool:
    return SearchTextTool(
        WorkspacePathResolver(workspace),
        max_matches=max_matches,
        max_line_bytes=max_line_bytes,
    )


def _execute(tool: SearchTextTool, arguments: SearchTextArguments):
    prepared = tool.prepare("search", arguments)
    assert isinstance(prepared, PreparedToolCall)
    return tool.execute(prepared)


def test_literal_search_is_case_insensitive_by_default(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text(
        "first line\nVerify_Token(value)\nlast line\n",
        encoding="utf-8",
    )

    result = _execute(_tool(workspace), SearchTextArguments(query="verify_token"))

    assert result.outcome is ToolOutcome.SUCCESS
    assert isinstance(result.content, SearchTextContent)
    assert len(result.content.matches) == 1
    match = result.content.matches[0]
    assert match.relative_path == "main.py"
    assert match.line_number == 2
    assert match.line_text == "Verify_Token(value)"
    assert match.line_truncated is False


def test_case_sensitive_literal_search(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("Needle\nneedle\n", encoding="utf-8")

    result = _execute(
        _tool(workspace),
        SearchTextArguments(query="Needle", case_sensitive=True),
    )

    assert isinstance(result.content, SearchTextContent)
    assert [(match.line_number, match.line_text) for match in result.content.matches] == [
        (1, "Needle")
    ]


def test_regex_search_and_invalid_regex(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text(
        "def alpha():\ndef beta(value):\n",
        encoding="utf-8",
    )
    tool = _tool(workspace)

    valid = _execute(
        tool,
        SearchTextArguments(query=r"def\s+\w+\(", regex=True),
    )
    invalid = _execute(tool, SearchTextArguments(query="[", regex=True))

    assert isinstance(valid.content, SearchTextContent)
    assert [match.line_number for match in valid.content.matches] == [1, 2]
    assert invalid.outcome is ToolOutcome.OPERATION_FAILURE
    assert invalid.error is not None
    assert invalid.error.code == "INVALID_SEARCH_PATTERN"


def test_file_glob_and_subtree_limit_search_scope(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "src" / "nested").mkdir(parents=True)
    (workspace / "tests").mkdir()
    (workspace / "src" / "main.py").write_text("target\n", encoding="utf-8")
    (workspace / "src" / "nested" / "note.txt").write_text(
        "target\n", encoding="utf-8"
    )
    (workspace / "tests" / "test_main.py").write_text(
        "target\n", encoding="utf-8"
    )

    result = _execute(
        _tool(workspace),
        SearchTextArguments(query="target", path="src", file_glob="**/*.py"),
    )

    assert isinstance(result.content, SearchTextContent)
    assert result.content.path == "src"
    assert [match.relative_path for match in result.content.matches] == ["src/main.py"]


def test_ignored_sensitive_binary_and_undecodable_files_are_skipped(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "visible.txt").write_text("target\n", encoding="utf-8")
    (workspace / "ignored.txt").write_text("target\n", encoding="utf-8")
    (workspace / ".env").write_text("target\n", encoding="utf-8")
    (workspace / "binary.dat").write_bytes(b"target\x00rest")
    (workspace / "legacy.txt").write_bytes(b"target\xff")
    (workspace / "build").mkdir()
    (workspace / "build" / "generated.txt").write_text("target\n", encoding="utf-8")
    (workspace / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")

    result = _execute(_tool(workspace), SearchTextArguments(query="target"))

    assert isinstance(result.content, SearchTextContent)
    assert [match.relative_path for match in result.content.matches] == ["visible.txt"]


def test_matches_and_line_text_are_bounded(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.txt").write_text(
        "target-" + ("x" * 100) + "\ntarget-two\n",
        encoding="utf-8",
    )
    (workspace / "b.txt").write_text("target-three\n", encoding="utf-8")

    result = _execute(
        _tool(workspace, max_matches=2, max_line_bytes=12),
        SearchTextArguments(query="target"),
    )

    assert isinstance(result.content, SearchTextContent)
    assert len(result.content.matches) == 2
    assert result.content.matches[0].line_truncated is True
    assert len(result.content.matches[0].line_text.encode("utf-8")) <= 12
    assert result.content.truncated is True


@pytest.mark.parametrize(
    ("max_matches", "max_line_bytes"),
    [(0, 10), (10, 0)],
)
def test_search_text_rejects_nonpositive_limits(
    tmp_path: Path,
    max_matches: int,
    max_line_bytes: int,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(ValueError):
        SearchTextTool(
            WorkspacePathResolver(workspace),
            max_matches=max_matches,
            max_line_bytes=max_line_bytes,
        )
