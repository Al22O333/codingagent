from __future__ import annotations

import errno
import os
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

import coding_agent.file_lifecycle as file_lifecycle_module
from coding_agent.file_lifecycle import (
    CreateDirectoryArguments,
    CreateDirectoryTool,
    DeletePathArguments,
    DeletePathTool,
    MovePathArguments,
    MovePathTool,
    _NoReplaceUnavailableError,
    _linux_rename_no_replace,
    _rename_no_replace,
)
from coding_agent.policy import PermissionDecision, PolicyEngine
from coding_agent.protocol import ToolCapability, ToolError, ToolKind, ToolOutcome
from coding_agent.tooling import PreparedToolCall
from coding_agent.workspace import (
    FileOperationFacts,
    FileOperationKind,
    PathResolutionMode,
    WorkspacePathResolver,
)


def _prepared(tool, call_id: str, arguments) -> PreparedToolCall:  # type: ignore[no-untyped-def]
    prepared = tool.prepare(call_id, arguments)
    assert isinstance(prepared, PreparedToolCall)
    return prepared


def test_lifecycle_schemas_are_strict_and_have_no_force_or_recursive_flags() -> None:
    with pytest.raises(ValidationError):
        CreateDirectoryArguments.model_validate({"path": "build", "parents": True})
    with pytest.raises(ValidationError):
        MovePathArguments.model_validate(
            {"source": "old", "destination": "new", "overwrite": True}
        )
    with pytest.raises(ValidationError):
        DeletePathArguments.model_validate({"path": "old", "recursive": True})


def test_lifecycle_tools_are_typed_local_file_mutations(tmp_path: Path) -> None:
    resolver = WorkspacePathResolver(tmp_path)
    tools = (
        CreateDirectoryTool(resolver),
        MovePathTool(resolver),
        DeletePathTool(resolver),
    )

    assert [tool.spec.name for tool in tools] == [
        "create_directory",
        "move_path",
        "delete_path",
    ]
    assert all(tool.spec.kind is ToolKind.LOCAL for tool in tools)
    assert all(
        tool.spec.capabilities == frozenset({ToolCapability.FILE_MUTATION})
        for tool in tools
    )
    assert "recursive" in tools[2].spec.description
    assert "rather than create plus delete" in tools[1].spec.description


def test_create_directory_creates_one_level_and_reports_summary(tmp_path: Path) -> None:
    tool = CreateDirectoryTool(WorkspacePathResolver(tmp_path))
    prepared = _prepared(tool, "mkdir", CreateDirectoryArguments(path="src"))

    result = tool.execute(prepared)

    assert result.outcome is ToolOutcome.SUCCESS
    assert (tmp_path / "src").is_dir()
    assert result.content is not None
    assert result.content.path == "src"  # type: ignore[union-attr]
    assert result.content.entry_type == "directory"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("setup", "path", "error_code"),
    [
        ("directory", "existing", "DIRECTORY_ALREADY_EXISTS"),
        ("file", "existing", "PATH_ALREADY_EXISTS"),
        ("none", "missing/child", "PARENT_DIRECTORY_NOT_FOUND"),
    ],
)
def test_create_directory_rejects_existing_or_missing_parent(
    tmp_path: Path,
    setup: str,
    path: str,
    error_code: str,
) -> None:
    existing = tmp_path / "existing"
    if setup == "directory":
        existing.mkdir()
    elif setup == "file":
        existing.write_text("occupied", encoding="utf-8")
    tool = CreateDirectoryTool(WorkspacePathResolver(tmp_path))

    error = tool.prepare("mkdir", CreateDirectoryArguments(path=path))

    assert isinstance(error, ToolError)
    assert error.code == error_code
    assert not (tmp_path / "missing").exists()


def test_create_directory_destination_race_does_not_overwrite(tmp_path: Path) -> None:
    tool = CreateDirectoryTool(WorkspacePathResolver(tmp_path))
    prepared = _prepared(tool, "mkdir", CreateDirectoryArguments(path="raced"))
    target = tmp_path / "raced"
    target.write_text("other process", encoding="utf-8")

    result = tool.execute(prepared)

    assert result.outcome is ToolOutcome.OPERATION_FAILURE
    assert result.error is not None and result.error.code == "PATH_ALREADY_EXISTS"
    assert target.read_text(encoding="utf-8") == "other process"


@pytest.mark.parametrize("entry_type", ["file", "directory"])
def test_move_path_renames_file_or_directory(
    tmp_path: Path,
    entry_type: str,
) -> None:
    source = tmp_path / "old"
    if entry_type == "file":
        source.write_text("content", encoding="utf-8")
    else:
        source.mkdir()
        (source / "child.txt").write_text("content", encoding="utf-8")
    tool = MovePathTool(WorkspacePathResolver(tmp_path))
    prepared = _prepared(
        tool,
        "move",
        MovePathArguments(source="old", destination="new"),
    )

    result = tool.execute(prepared)

    assert result.outcome is ToolOutcome.SUCCESS
    assert not source.exists()
    assert (tmp_path / "new").exists()
    assert result.content is not None
    assert result.content.entry_type == entry_type  # type: ignore[union-attr]


def test_move_path_facts_cover_source_and_destination(tmp_path: Path) -> None:
    (tmp_path / "old.txt").write_text("old", encoding="utf-8")
    tool = MovePathTool(WorkspacePathResolver(tmp_path))

    prepared = _prepared(
        tool,
        "move",
        MovePathArguments(source="old.txt", destination="new.txt"),
    )

    facts = prepared.operation_facts
    assert isinstance(facts, FileOperationFacts)
    assert facts.operation is FileOperationKind.MOVE
    assert [path.workspace_relative_path for path in facts.affected_paths] == [
        "old.txt",
        "new.txt",
    ]
    assert facts.secondary_target == facts.affected_paths[1]
    assert facts.target_identity is not None


def test_move_path_existing_destination_is_never_overwritten(tmp_path: Path) -> None:
    source = tmp_path / "old.txt"
    destination = tmp_path / "new.txt"
    source.write_text("source", encoding="utf-8")
    destination.write_text("destination", encoding="utf-8")
    tool = MovePathTool(WorkspacePathResolver(tmp_path))

    error = tool.prepare(
        "move", MovePathArguments(source="old.txt", destination="new.txt")
    )

    assert isinstance(error, ToolError)
    assert error.code == "DESTINATION_ALREADY_EXISTS"
    assert source.read_text(encoding="utf-8") == "source"
    assert destination.read_text(encoding="utf-8") == "destination"


def test_move_path_destination_before_execute_is_non_destructive(tmp_path: Path) -> None:
    source = tmp_path / "old.txt"
    destination = tmp_path / "new.txt"
    source.write_text("source", encoding="utf-8")
    tool = MovePathTool(WorkspacePathResolver(tmp_path))
    prepared = _prepared(
        tool,
        "move",
        MovePathArguments(source="old.txt", destination="new.txt"),
    )
    destination.write_text("racer", encoding="utf-8")

    result = tool.execute(prepared)

    assert result.outcome is ToolOutcome.OPERATION_FAILURE
    assert result.error is not None
    assert result.error.code == "DESTINATION_ALREADY_EXISTS"
    assert source.read_text(encoding="utf-8") == "source"
    assert destination.read_text(encoding="utf-8") == "racer"


def test_move_path_final_native_race_never_overwrites_destination(
    tmp_path: Path,
) -> None:
    source = tmp_path / "old.txt"
    destination = tmp_path / "new.txt"
    source.write_bytes(b"source")

    def race_then_rename(current_source: Path, current_destination: Path) -> None:
        current_destination.write_bytes(b"racer")
        _rename_no_replace(current_source, current_destination)

    tool = MovePathTool(
        WorkspacePathResolver(tmp_path), rename_no_replace=race_then_rename
    )
    prepared = _prepared(
        tool,
        "move",
        MovePathArguments(source="old.txt", destination="new.txt"),
    )

    result = tool.execute(prepared)

    assert result.outcome is ToolOutcome.OPERATION_FAILURE
    assert result.error is not None
    assert result.error.code == "DESTINATION_ALREADY_EXISTS"
    assert source.read_bytes() == b"source"
    assert destination.read_bytes() == b"racer"


def test_move_path_source_disappears_after_prepare(tmp_path: Path) -> None:
    source = tmp_path / "old.txt"
    source.write_text("source", encoding="utf-8")
    tool = MovePathTool(WorkspacePathResolver(tmp_path))
    prepared = _prepared(
        tool,
        "move",
        MovePathArguments(source="old.txt", destination="new.txt"),
    )
    source.unlink()

    result = tool.execute(prepared)

    assert result.outcome is ToolOutcome.OPERATION_FAILURE
    assert result.error is not None and result.error.code == "MOVE_CONFLICT"
    assert not (tmp_path / "new.txt").exists()


def test_move_path_source_disappears_in_final_native_window(
    tmp_path: Path,
) -> None:
    source = tmp_path / "old.txt"
    source.write_text("source", encoding="utf-8")

    def remove_then_rename(current_source: Path, current_destination: Path) -> None:
        current_source.unlink()
        _rename_no_replace(current_source, current_destination)

    tool = MovePathTool(
        WorkspacePathResolver(tmp_path), rename_no_replace=remove_then_rename
    )
    prepared = _prepared(
        tool,
        "move",
        MovePathArguments(source="old.txt", destination="new.txt"),
    )

    result = tool.execute(prepared)

    assert result.outcome is ToolOutcome.OPERATION_FAILURE
    assert result.error is not None and result.error.code == "MOVE_CONFLICT"
    assert not (tmp_path / "new.txt").exists()


@pytest.mark.parametrize("destination_type", ["file", "directory"])
def test_move_path_destination_type_appears_after_prepare(
    tmp_path: Path,
    destination_type: str,
) -> None:
    source = tmp_path / "old.txt"
    destination = tmp_path / "new"
    source.write_text("source", encoding="utf-8")
    tool = MovePathTool(WorkspacePathResolver(tmp_path))
    prepared = _prepared(
        tool,
        "move",
        MovePathArguments(source="old.txt", destination="new"),
    )
    if destination_type == "file":
        destination.write_text("racer", encoding="utf-8")
    else:
        destination.mkdir()

    result = tool.execute(prepared)

    assert result.outcome is ToolOutcome.OPERATION_FAILURE
    assert result.error is not None
    assert result.error.code == "DESTINATION_ALREADY_EXISTS"
    assert source.read_text(encoding="utf-8") == "source"
    if destination_type == "directory":
        assert destination.is_dir()
    else:
        assert destination.read_text(encoding="utf-8") == "racer"


@pytest.mark.skipif(os.name != "nt", reason="Windows native rename semantics")
def test_windows_native_rename_never_replaces_existing_destination(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_bytes(b"source")
    destination.write_bytes(b"destination")

    with pytest.raises(FileExistsError):
        _rename_no_replace(source, destination)

    assert source.read_bytes() == b"source"
    assert destination.read_bytes() == b"destination"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux renameat2")
def test_linux_native_renameat2_never_replaces_existing_destination(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_bytes(b"source")
    destination.write_bytes(b"destination")

    with pytest.raises(FileExistsError):
        _rename_no_replace(source, destination)

    assert source.read_bytes() == b"source"
    assert destination.read_bytes() == b"destination"


def test_linux_no_replace_binding_maps_native_eexist_without_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []

    class FakeRenameAt2:
        argtypes: object = None
        restype: object = None

        def __call__(self, *arguments: object) -> int:
            calls.append(arguments)
            file_lifecycle_module.ctypes.set_errno(errno.EEXIST)
            return -1

    class FakeLibc:
        renameat2 = FakeRenameAt2()

    monkeypatch.setattr(
        file_lifecycle_module.ctypes,
        "CDLL",
        lambda *_args, **_kwargs: FakeLibc(),
    )

    with pytest.raises(FileExistsError):
        _linux_rename_no_replace(tmp_path / "source", tmp_path / "destination")

    assert len(calls) == 1
    assert calls[0][-1] == 1  # RENAME_NOREPLACE


def test_platform_dispatch_fails_closed_without_supported_native_primitive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    monkeypatch.setattr(file_lifecycle_module.os, "name", "posix")
    monkeypatch.setattr(file_lifecycle_module.sys, "platform", "darwin")

    with pytest.raises(_NoReplaceUnavailableError):
        _rename_no_replace(source, destination)


def test_move_path_fails_closed_when_no_replace_is_unavailable(
    tmp_path: Path,
) -> None:
    source = tmp_path / "old.txt"
    source.write_text("source", encoding="utf-8")

    def unavailable(_source: Path, _destination: Path) -> None:
        raise _NoReplaceUnavailableError("unsupported test platform")

    tool = MovePathTool(
        WorkspacePathResolver(tmp_path), rename_no_replace=unavailable
    )
    prepared = _prepared(
        tool,
        "move",
        MovePathArguments(source="old.txt", destination="new.txt"),
    )

    result = tool.execute(prepared)

    assert result.outcome is ToolOutcome.OPERATION_FAILURE
    assert result.error is not None
    assert result.error.code == "MOVE_NO_REPLACE_UNAVAILABLE"
    assert source.read_text(encoding="utf-8") == "source"
    assert not (tmp_path / "new.txt").exists()


def test_move_path_rejects_workspace_root_and_destination_inside_source(
    tmp_path: Path,
) -> None:
    (tmp_path / "package").mkdir()
    tool = MovePathTool(WorkspacePathResolver(tmp_path))

    root_error = tool.prepare(
        "root", MovePathArguments(source=".", destination="renamed")
    )
    nested_error = tool.prepare(
        "nested",
        MovePathArguments(source="package", destination="package/nested"),
    )

    assert isinstance(root_error, ToolError)
    assert root_error.code == "MOVE_WORKSPACE_ROOT_DENIED"
    assert isinstance(nested_error, ToolError)
    assert nested_error.code == "MOVE_DESTINATION_INSIDE_SOURCE"


def test_move_path_source_identity_race_does_not_move_replacement(tmp_path: Path) -> None:
    source = tmp_path / "old.txt"
    displaced = tmp_path / "displaced.txt"
    source.write_text("original", encoding="utf-8")
    tool = MovePathTool(WorkspacePathResolver(tmp_path))
    prepared = _prepared(
        tool,
        "move",
        MovePathArguments(source="old.txt", destination="new.txt"),
    )
    source.rename(displaced)
    source.write_text("replacement", encoding="utf-8")

    result = tool.execute(prepared)

    assert result.outcome is ToolOutcome.OPERATION_FAILURE
    assert result.error is not None and result.error.code == "MOVE_CONFLICT"
    assert source.read_text(encoding="utf-8") == "replacement"
    assert not (tmp_path / "new.txt").exists()


@pytest.mark.parametrize("entry_type", ["file", "directory"])
def test_delete_path_requires_confirmation_then_deletes_exact_target(
    tmp_path: Path,
    entry_type: str,
) -> None:
    target = tmp_path / "obsolete"
    if entry_type == "file":
        target.write_text("obsolete", encoding="utf-8")
    else:
        target.mkdir()
    tool = DeletePathTool(WorkspacePathResolver(tmp_path))
    prepared = _prepared(tool, "delete", DeletePathArguments(path="obsolete"))

    permission = PolicyEngine().check_risk_permission(prepared)
    result = tool.execute(prepared)

    assert permission.decision is PermissionDecision.CONFIRM
    assert permission.reason_code == "FILE_DELETE_CONFIRMATION"
    assert result.outcome is ToolOutcome.SUCCESS
    assert not target.exists()
    assert result.content is not None
    assert result.content.entry_type == entry_type  # type: ignore[union-attr]


def test_delete_path_nonempty_directory_and_workspace_root_are_denied(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "package"
    directory.mkdir()
    (directory / "keep.txt").write_text("keep", encoding="utf-8")
    tool = DeletePathTool(WorkspacePathResolver(tmp_path))

    nonempty = _prepared(
        tool, "nonempty", DeletePathArguments(path="package")
    )
    root = _prepared(tool, "root", DeletePathArguments(path="."))

    assert (
        PolicyEngine().check_risk_permission(nonempty).decision
        is PermissionDecision.DENY
    )
    assert PolicyEngine().check_risk_permission(root).decision is PermissionDecision.DENY
    assert directory.exists()


def test_delete_path_empty_directory_becoming_nonempty_is_not_deleted(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "empty"
    directory.mkdir()
    tool = DeletePathTool(WorkspacePathResolver(tmp_path))
    prepared = _prepared(tool, "delete", DeletePathArguments(path="empty"))
    (directory / "raced.txt").write_text("keep", encoding="utf-8")

    result = tool.execute(prepared)

    assert result.outcome is ToolOutcome.OPERATION_FAILURE
    assert result.error is not None and result.error.code == "DIRECTORY_NOT_EMPTY"
    assert (directory / "raced.txt").read_text(encoding="utf-8") == "keep"


def test_delete_path_identity_race_does_not_delete_replacement(tmp_path: Path) -> None:
    target = tmp_path / "obsolete.txt"
    displaced = tmp_path / "displaced.txt"
    target.write_text("original", encoding="utf-8")
    tool = DeletePathTool(WorkspacePathResolver(tmp_path))
    prepared = _prepared(
        tool, "delete", DeletePathArguments(path="obsolete.txt")
    )
    target.rename(displaced)
    target.write_text("replacement", encoding="utf-8")

    result = tool.execute(prepared)

    assert result.outcome is ToolOutcome.OPERATION_FAILURE
    assert result.error is not None
    assert result.error.code == "DELETE_TARGET_CHANGED"
    assert target.read_text(encoding="utf-8") == "replacement"


def test_missing_delete_path_is_deterministic(tmp_path: Path) -> None:
    tool = DeletePathTool(WorkspacePathResolver(tmp_path))

    error = tool.prepare("delete", DeletePathArguments(path="missing.txt"))

    assert isinstance(error, ToolError)
    assert error.code == "FILE_NOT_FOUND"


def test_lifecycle_symlink_final_entries_are_rejected_when_supported(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.txt"
    link = tmp_path / "link.txt"
    target.write_text("target", encoding="utf-8")
    try:
        link.symlink_to(target)
    except OSError as error:
        pytest.skip(f"file symlinks are unavailable: {error}")
    resolver = WorkspacePathResolver(tmp_path)

    move_error = MovePathTool(resolver).prepare(
        "move", MovePathArguments(source="link.txt", destination="renamed.txt")
    )
    delete_error = DeletePathTool(resolver).prepare(
        "delete", DeletePathArguments(path="link.txt")
    )

    assert isinstance(move_error, ToolError) and move_error.code == "SYMLINK_UNSUPPORTED"
    assert isinstance(delete_error, ToolError) and delete_error.code == "SYMLINK_UNSUPPORTED"
    assert target.read_text(encoding="utf-8") == "target"
    assert link.exists()


def test_outside_lifecycle_paths_are_policy_denied(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    resolver = WorkspacePathResolver(workspace)

    move = _prepared(
        MovePathTool(resolver),
        "move",
        MovePathArguments(source="../outside.txt", destination="inside.txt"),
    )
    delete = _prepared(
        DeletePathTool(resolver),
        "delete",
        DeletePathArguments(path="../outside.txt"),
    )

    for prepared in (move, delete):
        result = PolicyEngine().check_risk_permission(prepared)
        assert result.decision is PermissionDecision.DENY
        assert result.reason_code == "WORKSPACE_BOUNDARY"
    assert outside.read_text(encoding="utf-8") == "outside"


def test_sensitive_destination_or_delete_still_requires_confirmation(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.txt"
    secret = tmp_path / ".env"
    source.write_text("source", encoding="utf-8")
    secret.write_text("secret", encoding="utf-8")
    resolver = WorkspacePathResolver(tmp_path)
    move = _prepared(
        MovePathTool(resolver),
        "move",
        MovePathArguments(source="source.txt", destination="credentials.json"),
    )
    delete = _prepared(
        DeletePathTool(resolver),
        "delete",
        DeletePathArguments(path=".env"),
    )

    assert PolicyEngine().check_risk_permission(move).decision is PermissionDecision.CONFIRM
    assert PolicyEngine().check_risk_permission(delete).decision is PermissionDecision.CONFIRM


def test_file_operation_facts_reject_invalid_lifecycle_combinations(
    tmp_path: Path,
) -> None:
    target = WorkspacePathResolver(tmp_path).resolve_workspace_path(
        "new", PathResolutionMode.NEW
    )

    with pytest.raises(ValueError, match="secondary target"):
        FileOperationFacts(target=target, operation=FileOperationKind.MOVE)
    with pytest.raises(ValueError, match="directory_nonempty"):
        FileOperationFacts(target=target, directory_nonempty=True)
