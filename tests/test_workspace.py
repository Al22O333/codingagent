"""Tests for the shared workspace path resolver."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coding_agent.workspace import (
    PathResolutionMode,
    ResolvedPath,
    WorkspacePathResolver,
)


def _create_directory_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except OSError as symlink_error:
        if os.name != "nt":
            pytest.skip(
                f"directory symlinks are unavailable on this platform: {symlink_error}"
            )

    junction = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if junction.returncode != 0:
        pytest.skip(
            "directory symlinks and junctions are unavailable on this platform"
        )


def _remove_directory_link(link: Path) -> None:
    if link.is_symlink():
        link.unlink()
    else:
        os.rmdir(link)


def test_workspace_root_must_be_an_existing_directory(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(FileNotFoundError, match="does not exist"):
        WorkspacePathResolver(missing)

    file_path = tmp_path / "file.txt"
    file_path.write_text("content", encoding="utf-8")
    with pytest.raises(NotADirectoryError, match="not a directory"):
        WorkspacePathResolver(file_path)


def test_workspace_root_is_canonicalized(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    resolver = WorkspacePathResolver(workspace / ".")

    assert resolver.workspace_root == workspace.resolve(strict=True)
    assert resolver.workspace_root.is_absolute()


def test_bind_workspace_path_is_lexical_and_workspace_relative(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resolver = WorkspacePathResolver(workspace)

    assert resolver.bind_workspace_path("src/main.py") == workspace / "src/main.py"
    absolute = tmp_path / "outside.txt"
    assert resolver.bind_workspace_path(str(absolute)) == absolute


def test_existing_relative_inside_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    target = workspace / "src" / "main.py"
    target.parent.mkdir(parents=True)
    target.write_text("print('hello')", encoding="utf-8")
    resolver = WorkspacePathResolver(workspace)

    result = resolver.resolve_workspace_path(
        "src/main.py", PathResolutionMode.EXISTING
    )

    assert result == ResolvedPath(
        raw_path="src/main.py",
        resolved_path=target.resolve(strict=True),
        exists=True,
        is_within_workspace=True,
        workspace_relative_path="src/main.py",
        is_sensitive=False,
        is_protected=False,
    )


def test_existing_absolute_inside_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "README.md"
    target.write_text("read me", encoding="utf-8")
    resolver = WorkspacePathResolver(workspace)

    result = resolver.resolve_workspace_path(
        str(target), PathResolutionMode.EXISTING
    )

    assert result.is_within_workspace is True
    assert result.workspace_relative_path == "README.md"


def test_parent_traversal_and_absolute_outside_are_policy_facts(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    resolver = WorkspacePathResolver(workspace)

    traversal = resolver.resolve_workspace_path(
        "../outside.txt", PathResolutionMode.EXISTING
    )
    absolute = resolver.resolve_workspace_path(
        str(outside), PathResolutionMode.EXISTING
    )

    for result in (traversal, absolute):
        assert result.resolved_path == outside.resolve(strict=True)
        assert result.exists is True
        assert result.is_within_workspace is False
        assert result.workspace_relative_path is None


def test_existing_missing_path_is_a_resolution_failure(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resolver = WorkspacePathResolver(workspace)

    with pytest.raises(FileNotFoundError):
        resolver.resolve_workspace_path(
            "missing.txt", PathResolutionMode.EXISTING
        )


def test_existing_symlink_escape_is_outside_policy_fact(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside_directory = tmp_path / "outside"
    outside_directory.mkdir()
    outside = outside_directory / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = workspace / "linked-directory"
    _create_directory_link(link, outside_directory)
    resolver = WorkspacePathResolver(workspace)

    try:
        result = resolver.resolve_workspace_path(
            "linked-directory/outside.txt", PathResolutionMode.EXISTING
        )

        assert result.resolved_path == outside.resolve(strict=True)
        assert result.is_within_workspace is False
        assert result.workspace_relative_path is None
    finally:
        _remove_directory_link(link)


def test_new_target_under_existing_parent(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    parent = workspace / "src"
    parent.mkdir(parents=True)
    resolver = WorkspacePathResolver(workspace)

    result = resolver.resolve_workspace_path(
        "src/generated.py", PathResolutionMode.NEW
    )

    assert result.resolved_path == parent.resolve(strict=True) / "generated.py"
    assert result.exists is False
    assert result.is_within_workspace is True
    assert result.workspace_relative_path == "src/generated.py"


def test_new_target_outside_is_policy_fact_not_resolution_failure(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resolver = WorkspacePathResolver(workspace)

    result = resolver.resolve_workspace_path(
        "../new-outside.txt", PathResolutionMode.NEW
    )

    assert result.resolved_path == tmp_path.resolve(strict=True) / "new-outside.txt"
    assert result.exists is False
    assert result.is_within_workspace is False
    assert result.workspace_relative_path is None


def test_new_remaining_suffix_parent_components_cannot_escape(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resolver = WorkspacePathResolver(workspace)

    result = resolver.resolve_workspace_path(
        "missing/../../escaped.txt", PathResolutionMode.NEW
    )

    assert result.resolved_path == tmp_path.resolve(strict=True) / "escaped.txt"
    assert result.is_within_workspace is False
    assert result.workspace_relative_path is None


def test_new_target_through_directory_symlink_uses_resolved_parent(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside_directory = tmp_path / "outside"
    outside_directory.mkdir()
    link = workspace / "linked-directory"
    _create_directory_link(link, outside_directory)
    resolver = WorkspacePathResolver(workspace)

    try:
        result = resolver.resolve_workspace_path(
            "linked-directory/new.txt", PathResolutionMode.NEW
        )

        assert result.resolved_path == (
            outside_directory.resolve(strict=True) / "new.txt"
        )
        assert result.is_within_workspace is False
        assert result.workspace_relative_path is None
    finally:
        _remove_directory_link(link)


@pytest.mark.parametrize(
    ("raw_path", "is_sensitive", "is_protected"),
    [
        (".env", True, False),
        ("config/.env.local", True, False),
        ("certs/server.pem", True, False),
        ("id_ed25519", True, False),
        ("credentials.json", True, False),
        (".git/config", False, True),
        ("src/main.py", False, False),
    ],
)
def test_new_path_sensitive_and_protected_classification(
    tmp_path: Path,
    raw_path: str,
    is_sensitive: bool,
    is_protected: bool,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resolver = WorkspacePathResolver(workspace)

    result = resolver.resolve_workspace_path(raw_path, PathResolutionMode.NEW)

    assert result.is_sensitive is is_sensitive
    assert result.is_protected is is_protected


def test_new_path_beneath_existing_file_fails_resolution(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    existing_file = workspace / "file.txt"
    existing_file.write_text("content", encoding="utf-8")
    resolver = WorkspacePathResolver(workspace)

    with pytest.raises(NotADirectoryError, match="not a directory"):
        resolver.resolve_workspace_path(
            "file.txt/child.txt", PathResolutionMode.NEW
        )


def test_resolved_path_rejects_inconsistent_containment_fields() -> None:
    with pytest.raises(ValueError, match="present exactly for inside paths"):
        ResolvedPath(
            raw_path="file.txt",
            resolved_path=Path(os.path.abspath("file.txt")),
            exists=False,
            is_within_workspace=False,
            workspace_relative_path="file.txt",
            is_sensitive=False,
            is_protected=False,
        )
