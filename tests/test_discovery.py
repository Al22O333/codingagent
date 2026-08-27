"""Tests for bounded workspace discovery Tools."""

from __future__ import annotations

from pathlib import Path

import pytest

from coding_agent.discovery import (
    ListDirectoryArguments,
    ListDirectoryContent,
    ListDirectoryTool,
    SearchFilesArguments,
    SearchFilesContent,
    SearchFilesTool,
)
from coding_agent.protocol import ToolError, ToolOutcome
from coding_agent.tooling import PreparedToolCall
from coding_agent.workspace import WorkspacePathResolver


def _execute(tool, arguments):
    prepared = tool.prepare("discovery", arguments)
    assert isinstance(prepared, PreparedToolCall)
    return tool.execute(prepared)


def test_list_directory_is_one_level_filtered_and_deterministic(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "z-dir").mkdir()
    (workspace / "a-dir").mkdir()
    (workspace / "a-dir" / "nested.py").write_text("nested", encoding="utf-8")
    (workspace / "b.py").write_text("b", encoding="utf-8")
    (workspace / "A.py").write_text("a", encoding="utf-8")
    (workspace / ".env").write_text("SECRET=value", encoding="utf-8")
    (workspace / "node_modules").mkdir()
    (workspace / "node_modules" / "package.js").write_text("x", encoding="utf-8")
    (workspace / "ignored.txt").write_text("ignored", encoding="utf-8")
    (workspace / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    tool = ListDirectoryTool(WorkspacePathResolver(workspace), max_entries=20)

    result = _execute(tool, ListDirectoryArguments())

    assert result.outcome is ToolOutcome.SUCCESS
    assert isinstance(result.content, ListDirectoryContent)
    assert [(entry.relative_path, entry.type) for entry in result.content.entries] == [
        ("a-dir", "directory"),
        ("z-dir", "directory"),
        (".gitignore", "file"),
        ("A.py", "file"),
        ("b.py", "file"),
    ]
    assert result.content.truncated is False
    assert all("nested.py" not in entry.relative_path for entry in result.content.entries)


def test_list_directory_result_is_bounded(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for index in range(5):
        (workspace / f"file-{index}.txt").write_text("x", encoding="utf-8")
    tool = ListDirectoryTool(WorkspacePathResolver(workspace), max_entries=2)

    result = _execute(tool, ListDirectoryArguments())

    assert isinstance(result.content, ListDirectoryContent)
    assert [entry.relative_path for entry in result.content.entries] == [
        "file-0.txt",
        "file-1.txt",
    ]
    assert result.content.truncated is True


def test_search_files_glob_filters_ignored_noise_and_sensitive_paths(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "src" / "nested").mkdir(parents=True)
    (workspace / "src" / "main.py").write_text("main", encoding="utf-8")
    (workspace / "src" / "nested" / "helper.py").write_text("helper", encoding="utf-8")
    (workspace / "src" / "ignored.py").write_text("ignored", encoding="utf-8")
    (workspace / "src" / ".env.py").write_text("secret", encoding="utf-8")
    (workspace / "build").mkdir()
    (workspace / "build" / "generated.py").write_text("generated", encoding="utf-8")
    (workspace / ".gitignore").write_text("src/ignored.py\n", encoding="utf-8")
    tool = SearchFilesTool(WorkspacePathResolver(workspace), max_results=20)

    result = _execute(tool, SearchFilesArguments(pattern="**/*.py"))

    assert result.outcome is ToolOutcome.SUCCESS
    assert isinstance(result.content, SearchFilesContent)
    assert result.content.matches == ("src/main.py", "src/nested/helper.py")
    assert result.content.truncated is False


def test_search_files_pattern_is_relative_to_requested_subtree(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "main.py").write_text("main", encoding="utf-8")
    (workspace / "root.py").write_text("root", encoding="utf-8")
    tool = SearchFilesTool(WorkspacePathResolver(workspace), max_results=20)

    result = _execute(
        tool,
        SearchFilesArguments(pattern="*.py", path="src"),
    )

    assert isinstance(result.content, SearchFilesContent)
    assert result.content.path == "src"
    assert result.content.matches == ("src/main.py",)


def test_search_files_result_is_bounded_and_ordered(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for name in ("z.py", "B.py", "a.py"):
        (workspace / name).write_text(name, encoding="utf-8")
    tool = SearchFilesTool(WorkspacePathResolver(workspace), max_results=2)

    result = _execute(tool, SearchFilesArguments(pattern="*.py"))

    assert isinstance(result.content, SearchFilesContent)
    assert result.content.matches == ("a.py", "B.py")
    assert result.content.truncated is True


def test_discovery_prepare_reports_missing_and_non_directory_paths(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_text("content", encoding="utf-8")
    resolver = WorkspacePathResolver(workspace)
    list_tool = ListDirectoryTool(resolver, max_entries=10)
    search_tool = SearchFilesTool(resolver, max_results=10)

    missing = list_tool.prepare("missing", ListDirectoryArguments(path="missing"))
    file_target = search_tool.prepare(
        "file-target",
        SearchFilesArguments(pattern="*", path="file.txt")
    )

    assert isinstance(missing, ToolError)
    assert missing.code == "DIRECTORY_NOT_FOUND"
    assert isinstance(file_target, ToolError)
    assert file_target.code == "NOT_A_DIRECTORY"


def test_search_does_not_follow_symlink_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "leak.py").write_text("leak", encoding="utf-8")
    link = workspace / "external"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlink unavailable: {error}")
    tool = SearchFilesTool(WorkspacePathResolver(workspace), max_results=10)

    result = _execute(tool, SearchFilesArguments(pattern="**/*.py"))

    assert isinstance(result.content, SearchFilesContent)
    assert result.content.matches == ()


@pytest.mark.parametrize(
    ("tool_type", "limit_name"),
    [
        (ListDirectoryTool, "max_entries"),
        (SearchFilesTool, "max_results"),
    ],
)
def test_discovery_tools_reject_nonpositive_limits(
    tmp_path: Path,
    tool_type,
    limit_name: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(ValueError):
        tool_type(WorkspacePathResolver(workspace), **{limit_name: 0})
