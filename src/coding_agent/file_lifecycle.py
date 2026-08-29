"""Lean structured directory creation, path move, and exact deletion Tools."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from pydantic import Field

from .protocol import ToolCapability, ToolError, ToolKind, ToolOutcome
from .tooling import PreparedToolCall, Tool, ToolArguments, ToolExecutionResult
from .workspace import (
    FileOperationFacts,
    FileOperationKind,
    PathResolutionMode,
    ResolvedPath,
    WorkspacePathResolver,
)


class CreateDirectoryArguments(ToolArguments):
    """Validated arguments for one-level directory creation."""

    path: str = Field(min_length=1)


class MovePathArguments(ToolArguments):
    """Validated arguments for one workspace-contained rename or move."""

    source: str = Field(min_length=1)
    destination: str = Field(min_length=1)


class DeletePathArguments(ToolArguments):
    """Validated arguments for deleting one regular file or empty directory."""

    path: str = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class CreateDirectoryContent:
    path: str
    entry_type: str = "directory"


@dataclass(frozen=True, slots=True)
class MovePathContent:
    source: str
    destination: str
    entry_type: str


@dataclass(frozen=True, slots=True)
class DeletePathContent:
    path: str
    entry_type: str


class CreateDirectoryTool(Tool[CreateDirectoryArguments]):
    """Create exactly one absent directory without creating parent trees."""

    __slots__ = ("_resolver",)

    def __init__(self, resolver: WorkspacePathResolver) -> None:
        super().__init__(
            name="create_directory",
            description=(
                "Create exactly one new directory whose direct parent already "
                "exists; never overwrite a path or create parent trees"
            ),
            argument_model=CreateDirectoryArguments,
            kind=ToolKind.LOCAL,
            capabilities=frozenset({ToolCapability.FILE_MUTATION}),
        )
        object.__setattr__(self, "_resolver", resolver)

    def prepare(
        self,
        call_id: str,
        arguments: CreateDirectoryArguments,
    ) -> PreparedToolCall | ToolError:
        try:
            resolved = self._resolver.resolve_workspace_path(
                arguments.path, PathResolutionMode.NEW
            )
        except FileNotFoundError:
            return _error(
                "PARENT_DIRECTORY_NOT_FOUND",
                "create_directory parent directory does not exist",
                path=arguments.path,
            )
        except NotADirectoryError:
            return _error(
                "NOT_A_DIRECTORY",
                "create_directory parent path is not a directory",
                path=arguments.path,
            )
        except (OSError, ValueError):
            return _error(
                "DIRECTORY_CREATE_FAILED",
                "create_directory target could not be resolved",
                path=arguments.path,
            )

        facts = FileOperationFacts(
            target=resolved,
            affected_paths=(resolved,),
            operation=FileOperationKind.CREATE_DIRECTORY,
        )
        if not resolved.is_within_workspace:
            return self.prepared_call(call_id, arguments, facts)
        if resolved.exists:
            try:
                is_directory = resolved.resolved_path.is_dir()
            except OSError:
                is_directory = False
            return _error(
                "DIRECTORY_ALREADY_EXISTS" if is_directory else "PATH_ALREADY_EXISTS",
                (
                    "create_directory target directory already exists"
                    if is_directory
                    else "create_directory target is occupied by a non-directory"
                ),
                path=arguments.path,
            )
        if not resolved.resolved_path.parent.exists():
            return _error(
                "PARENT_DIRECTORY_NOT_FOUND",
                "create_directory direct parent does not exist",
                path=arguments.path,
            )
        if not resolved.resolved_path.parent.is_dir():
            return _error(
                "NOT_A_DIRECTORY",
                "create_directory direct parent is not a directory",
                path=arguments.path,
            )
        return self.prepared_call(call_id, arguments, facts)

    def execute(self, prepared_call: PreparedToolCall) -> ToolExecutionResult:
        if not _matches_prepared(
            prepared_call,
            self.name,
            CreateDirectoryArguments,
            FileOperationKind.CREATE_DIRECTORY,
        ):
            return _failure(
                "INTERNAL_TOOL_ERROR",
                "prepared call does not match create_directory",
                path="<unknown>",
            )
        arguments = prepared_call.validated_arguments
        facts = prepared_call.operation_facts
        assert isinstance(arguments, CreateDirectoryArguments)
        assert isinstance(facts, FileOperationFacts)
        if facts.affected_paths != (facts.target,) or facts.target.raw_path != arguments.path:
            return _failure(
                "INTERNAL_TOOL_ERROR",
                "prepared create_directory facts are invalid",
                path=arguments.path,
            )
        if not facts.target.is_within_workspace:
            raise ValueError("outside-workspace path requires policy evaluation")

        try:
            current = self._resolver.resolve_workspace_path(
                arguments.path, PathResolutionMode.NEW
            )
        except (OSError, ValueError):
            return _failure(
                "DIRECTORY_CREATE_FAILED",
                "create_directory target could not be re-resolved",
                path=arguments.path,
            )
        if current.resolved_path != facts.target.resolved_path:
            return _failure(
                "DIRECTORY_CREATE_CONFLICT",
                "create_directory target changed after preparation",
                path=arguments.path,
            )
        if current.exists:
            return _failure(
                "PATH_ALREADY_EXISTS",
                "create_directory target appeared before execution",
                path=arguments.path,
            )
        try:
            os.mkdir(current.resolved_path)
        except FileExistsError:
            return _failure(
                "PATH_ALREADY_EXISTS",
                "create_directory target appeared before execution",
                path=arguments.path,
            )
        except FileNotFoundError:
            return _failure(
                "PARENT_DIRECTORY_NOT_FOUND",
                "create_directory parent disappeared before execution",
                path=arguments.path,
            )
        except NotADirectoryError:
            return _failure(
                "NOT_A_DIRECTORY",
                "create_directory parent is not a directory",
                path=arguments.path,
            )
        except OSError as error:
            return _failure(
                "DIRECTORY_CREATE_FAILED",
                "create_directory could not create the target",
                path=arguments.path,
                reason=type(error).__name__,
            )
        return ToolExecutionResult(
            ToolOutcome.SUCCESS,
            CreateDirectoryContent(
                path=current.workspace_relative_path or arguments.path
            ),
        )


class MovePathTool(Tool[MovePathArguments]):
    """Move or rename one regular file or directory without overwrite."""

    __slots__ = ("_resolver",)

    def __init__(self, resolver: WorkspacePathResolver) -> None:
        super().__init__(
            name="move_path",
            description=(
                "Move or rename one regular file or directory inside the "
                "workspace; source and destination are both constrained and "
                "the destination must not exist"
            ),
            argument_model=MovePathArguments,
            kind=ToolKind.LOCAL,
            capabilities=frozenset({ToolCapability.FILE_MUTATION}),
        )
        object.__setattr__(self, "_resolver", resolver)

    def prepare(
        self,
        call_id: str,
        arguments: MovePathArguments,
    ) -> PreparedToolCall | ToolError:
        try:
            source = self._resolver.resolve_workspace_path(
                arguments.source, PathResolutionMode.EXISTING
            )
        except FileNotFoundError:
            return _error("FILE_NOT_FOUND", "move_path source does not exist", source=arguments.source)
        except (OSError, ValueError):
            return _error("MOVE_FAILED", "move_path source could not be resolved", source=arguments.source)
        try:
            destination = self._resolver.resolve_workspace_path(
                arguments.destination, PathResolutionMode.NEW
            )
        except (FileNotFoundError, NotADirectoryError):
            return _error(
                "PARENT_DIRECTORY_NOT_FOUND",
                "move_path destination parent does not exist or is not a directory",
                destination=arguments.destination,
            )
        except (OSError, ValueError):
            return _error("MOVE_FAILED", "move_path destination could not be resolved", destination=arguments.destination)

        base_facts = dict(
            target=source,
            affected_paths=(source, destination),
            operation=FileOperationKind.MOVE,
            secondary_target=destination,
        )
        if not source.is_within_workspace or not destination.is_within_workspace:
            return self.prepared_call(
                call_id, arguments, FileOperationFacts(**base_facts)
            )
        if source.is_protected or destination.is_protected:
            return self.prepared_call(
                call_id, arguments, FileOperationFacts(**base_facts)
            )
        if source.workspace_relative_path == ".":
            return _error(
                "MOVE_WORKSPACE_ROOT_DENIED",
                "move_path cannot move the workspace root",
                source=arguments.source,
            )
        source_bound = self._resolver.bind_workspace_path(arguments.source)
        if _is_final_reparse(source_bound):
            return _error(
                "SYMLINK_UNSUPPORTED",
                "move_path does not move a final symlink, junction, or reparse entry",
                source=arguments.source,
            )
        if destination.exists:
            return _error(
                "DESTINATION_ALREADY_EXISTS",
                "move_path destination already exists",
                destination=arguments.destination,
            )
        if not destination.resolved_path.parent.exists() or not destination.resolved_path.parent.is_dir():
            return _error(
                "PARENT_DIRECTORY_NOT_FOUND",
                "move_path destination direct parent does not exist",
                destination=arguments.destination,
            )
        try:
            source_stat = source.resolved_path.stat()
        except OSError:
            return _error("MOVE_FAILED", "move_path source metadata could not be read", source=arguments.source)
        entry_type = _entry_type(source_stat.st_mode)
        if entry_type is None:
            return _error(
                "UNSUPPORTED_FILE_TYPE",
                "move_path supports only regular files and directories",
                source=arguments.source,
            )
        if entry_type == "directory" and _is_relative_to(
            destination.resolved_path, source.resolved_path
        ):
            return _error(
                "MOVE_DESTINATION_INSIDE_SOURCE",
                "move_path cannot move a directory inside itself",
                source=arguments.source,
                destination=arguments.destination,
            )
        facts = FileOperationFacts(
            **base_facts,
            target_identity=_identity(source_stat),
        )
        return self.prepared_call(call_id, arguments, facts)

    def execute(self, prepared_call: PreparedToolCall) -> ToolExecutionResult:
        if not _matches_prepared(
            prepared_call,
            self.name,
            MovePathArguments,
            FileOperationKind.MOVE,
        ):
            return _failure(
                "INTERNAL_TOOL_ERROR",
                "prepared call does not match move_path",
                source="<unknown>",
            )
        arguments = prepared_call.validated_arguments
        facts = prepared_call.operation_facts
        assert isinstance(arguments, MovePathArguments)
        assert isinstance(facts, FileOperationFacts)
        destination = facts.secondary_target
        if (
            destination is None
            or facts.affected_paths != (facts.target, destination)
            or facts.target.raw_path != arguments.source
            or destination.raw_path != arguments.destination
        ):
            return _failure(
                "INTERNAL_TOOL_ERROR",
                "prepared move_path facts are invalid",
                source=arguments.source,
                destination=arguments.destination,
            )
        if not facts.target.is_within_workspace or not destination.is_within_workspace:
            raise ValueError("outside-workspace path requires policy evaluation")
        try:
            current_source = self._resolver.resolve_workspace_path(
                arguments.source, PathResolutionMode.EXISTING
            )
            current_destination = self._resolver.resolve_workspace_path(
                arguments.destination, PathResolutionMode.NEW
            )
            source_stat = current_source.resolved_path.stat()
        except FileNotFoundError:
            return _failure("MOVE_CONFLICT", "move_path source or parent disappeared", source=arguments.source, destination=arguments.destination)
        except (OSError, ValueError):
            return _failure("MOVE_CONFLICT", "move_path paths could not be revalidated", source=arguments.source, destination=arguments.destination)
        if (
            current_source.resolved_path != facts.target.resolved_path
            or current_destination.resolved_path != destination.resolved_path
            or _identity(source_stat) != facts.target_identity
            or _is_final_reparse(self._resolver.bind_workspace_path(arguments.source))
        ):
            return _failure("MOVE_CONFLICT", "move_path source or destination changed after preparation", source=arguments.source, destination=arguments.destination)
        if current_destination.exists:
            return _failure("DESTINATION_ALREADY_EXISTS", "move_path destination appeared before execution", destination=arguments.destination)
        entry_type = _entry_type(source_stat.st_mode)
        if entry_type is None:
            return _failure("UNSUPPORTED_FILE_TYPE", "move_path source type changed", source=arguments.source)
        try:
            os.rename(current_source.resolved_path, current_destination.resolved_path)
        except (FileExistsError, IsADirectoryError, NotADirectoryError):
            return _failure("DESTINATION_ALREADY_EXISTS", "move_path destination appeared before execution", destination=arguments.destination)
        except OSError as error:
            return _failure(
                "MOVE_FAILED",
                "move_path could not install the destination",
                source=arguments.source,
                destination=arguments.destination,
                reason=type(error).__name__,
            )
        return ToolExecutionResult(
            ToolOutcome.SUCCESS,
            MovePathContent(
                source=facts.target.workspace_relative_path or arguments.source,
                destination=destination.workspace_relative_path or arguments.destination,
                entry_type=entry_type,
            ),
        )


class DeletePathTool(Tool[DeletePathArguments]):
    """Delete one regular file or empty directory after exact confirmation."""

    __slots__ = ("_resolver",)

    def __init__(self, resolver: WorkspacePathResolver) -> None:
        super().__init__(
            name="delete_path",
            description=(
                "Delete exactly one regular file or empty directory after "
                "confirmation; never delete recursively, by pattern, or by force"
            ),
            argument_model=DeletePathArguments,
            kind=ToolKind.LOCAL,
            capabilities=frozenset({ToolCapability.FILE_MUTATION}),
        )
        object.__setattr__(self, "_resolver", resolver)

    def prepare(
        self,
        call_id: str,
        arguments: DeletePathArguments,
    ) -> PreparedToolCall | ToolError:
        try:
            target = self._resolver.resolve_workspace_path(
                arguments.path, PathResolutionMode.EXISTING
            )
        except FileNotFoundError:
            return _error("FILE_NOT_FOUND", "delete_path target does not exist", path=arguments.path)
        except (OSError, ValueError):
            return _error("DELETE_FAILED", "delete_path target could not be resolved", path=arguments.path)

        base_facts = dict(
            target=target,
            affected_paths=(target,),
            operation=FileOperationKind.DELETE,
        )
        if not target.is_within_workspace or target.is_protected:
            return self.prepared_call(
                call_id, arguments, FileOperationFacts(**base_facts)
            )
        if _is_final_reparse(self._resolver.bind_workspace_path(arguments.path)):
            return _error(
                "SYMLINK_UNSUPPORTED",
                "delete_path does not delete a symlink, junction, or reparse entry",
                path=arguments.path,
            )
        try:
            target_stat = target.resolved_path.stat()
        except OSError:
            return _error("DELETE_FAILED", "delete_path target metadata could not be read", path=arguments.path)
        entry_type = _entry_type(target_stat.st_mode)
        if entry_type is None:
            return _error(
                "UNSUPPORTED_FILE_TYPE",
                "delete_path supports only regular files and directories",
                path=arguments.path,
            )
        directory_nonempty = (
            _directory_nonempty(target.resolved_path)
            if entry_type == "directory"
            else False
        )
        facts = FileOperationFacts(
            **base_facts,
            target_identity=_identity(target_stat),
            directory_nonempty=directory_nonempty,
        )
        return self.prepared_call(call_id, arguments, facts)

    def execute(self, prepared_call: PreparedToolCall) -> ToolExecutionResult:
        if not _matches_prepared(
            prepared_call,
            self.name,
            DeletePathArguments,
            FileOperationKind.DELETE,
        ):
            return _failure(
                "INTERNAL_TOOL_ERROR",
                "prepared call does not match delete_path",
                path="<unknown>",
            )
        arguments = prepared_call.validated_arguments
        facts = prepared_call.operation_facts
        assert isinstance(arguments, DeletePathArguments)
        assert isinstance(facts, FileOperationFacts)
        if facts.affected_paths != (facts.target,) or facts.target.raw_path != arguments.path:
            return _failure(
                "INTERNAL_TOOL_ERROR",
                "prepared delete_path facts are invalid",
                path=arguments.path,
            )
        if not facts.target.is_within_workspace:
            raise ValueError("outside-workspace path requires policy evaluation")
        try:
            current = self._resolver.resolve_workspace_path(
                arguments.path, PathResolutionMode.EXISTING
            )
            current_stat = current.resolved_path.stat()
        except FileNotFoundError:
            return _failure("FILE_NOT_FOUND", "delete_path target disappeared before execution", path=arguments.path)
        except (OSError, ValueError):
            return _failure("DELETE_TARGET_CHANGED", "delete_path target could not be revalidated", path=arguments.path)
        if (
            current.resolved_path != facts.target.resolved_path
            or _identity(current_stat) != facts.target_identity
            or _is_final_reparse(self._resolver.bind_workspace_path(arguments.path))
        ):
            return _failure("DELETE_TARGET_CHANGED", "delete_path target changed after confirmation", path=arguments.path)
        entry_type = _entry_type(current_stat.st_mode)
        if entry_type is None:
            return _failure("DELETE_TARGET_CHANGED", "delete_path target type changed after confirmation", path=arguments.path)
        if entry_type == "directory" and _directory_nonempty(current.resolved_path):
            return _failure("DIRECTORY_NOT_EMPTY", "delete_path directory is no longer empty", path=arguments.path)
        try:
            if entry_type == "directory":
                os.rmdir(current.resolved_path)
            else:
                os.unlink(current.resolved_path)
        except FileNotFoundError:
            return _failure("FILE_NOT_FOUND", "delete_path target disappeared before execution", path=arguments.path)
        except OSError as error:
            code = (
                "DIRECTORY_NOT_EMPTY"
                if entry_type == "directory" and current.resolved_path.exists()
                else "DELETE_FAILED"
            )
            return _failure(
                code,
                "delete_path could not remove the exact target",
                path=arguments.path,
                reason=type(error).__name__,
            )
        return ToolExecutionResult(
            ToolOutcome.SUCCESS,
            DeletePathContent(
                path=facts.target.workspace_relative_path or arguments.path,
                entry_type=entry_type,
            ),
        )


def _matches_prepared(
    prepared_call: PreparedToolCall,
    tool_name: str,
    argument_type: type[ToolArguments],
    operation: FileOperationKind,
) -> bool:
    facts = prepared_call.operation_facts
    return (
        prepared_call.tool_identity.name == tool_name
        and isinstance(prepared_call.validated_arguments, argument_type)
        and isinstance(facts, FileOperationFacts)
        and facts.operation is operation
    )


def _identity(result: os.stat_result) -> tuple[int, int, int]:
    return (result.st_dev, result.st_ino, stat.S_IFMT(result.st_mode))


def _entry_type(mode: int) -> str | None:
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "directory"
    return None


def _is_final_reparse(path: Path) -> bool:
    try:
        result = path.lstat()
    except OSError:
        return False
    attributes = getattr(result, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(result.st_mode) or bool(attributes & reparse_flag)


def _directory_nonempty(path: Path) -> bool:
    try:
        with os.scandir(path) as entries:
            return next(entries, None) is not None
    except OSError:
        return True


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _error(code: str, message: str, **details: str) -> ToolError:
    return ToolError(code=code, message=message, details=details)


def _failure(
    code: str,
    message: str,
    *,
    reason: str | None = None,
    **details: str,
) -> ToolExecutionResult:
    if reason is not None:
        details["reason"] = reason
    return ToolExecutionResult(
        ToolOutcome.OPERATION_FAILURE,
        error=ToolError(code=code, message=message, details=details),
    )


__all__ = [
    "CreateDirectoryArguments",
    "CreateDirectoryContent",
    "CreateDirectoryTool",
    "DeletePathArguments",
    "DeletePathContent",
    "DeletePathTool",
    "MovePathArguments",
    "MovePathContent",
    "MovePathTool",
]
