"""Race-safe create-only UTF-8 file Tool."""

from __future__ import annotations

import os
from dataclasses import dataclass

from pydantic import Field

from .protocol import ToolCapability, ToolError, ToolKind, ToolOutcome
from .tooling import PreparedToolCall, Tool, ToolArguments, ToolExecutionResult
from .workspace import (
    FileOperationFacts,
    PathResolutionMode,
    ResolvedPath,
    WorkspacePathResolver,
)


class CreateFileArguments(ToolArguments):
    """Validated model arguments for create-only file creation."""

    path: str = Field(min_length=1)
    content: str


@dataclass(frozen=True, slots=True)
class CreateFileContent:
    """Structured summary of a successfully created file."""

    path: str
    bytes_written: int


class CreateFileTool(Tool[CreateFileArguments]):
    """Create one UTF-8 text file only when the target is absent."""

    __slots__ = ("_resolver",)

    def __init__(self, resolver: WorkspacePathResolver) -> None:
        super().__init__(
            name="create_file",
            description=(
                "Create a new UTF-8 text file only when the target is known not "
                "to exist; never use it to replace an existing file"
            ),
            argument_model=CreateFileArguments,
            kind=ToolKind.LOCAL,
            capabilities=frozenset({ToolCapability.FILE_MUTATION}),
        )
        object.__setattr__(self, "_resolver", resolver)

    def prepare(
        self,
        call_id: str,
        arguments: CreateFileArguments,
    ) -> PreparedToolCall | ToolError:
        """Resolve a candidate new target and require its direct parent."""
        try:
            resolved = self._resolver.resolve_workspace_path(
                arguments.path,
                PathResolutionMode.NEW,
            )
        except FileNotFoundError:
            return self._error(
                "PARENT_DIRECTORY_NOT_FOUND",
                "create_file parent directory does not exist",
                arguments.path,
            )
        except NotADirectoryError:
            return self._error(
                "NOT_A_DIRECTORY",
                "create_file parent path is not a directory",
                arguments.path,
            )
        except (OSError, ValueError):
            return self._error(
                "FILE_WRITE_FAILED",
                "create_file target path could not be resolved",
                arguments.path,
            )

        if not resolved.is_within_workspace:
            return self.prepared_call(
                call_id,
                arguments,
                FileOperationFacts(target=resolved, affected_paths=(resolved,)),
            )
        if resolved.exists:
            return self._error(
                "FILE_ALREADY_EXISTS",
                "create_file target already exists",
                arguments.path,
            )
        try:
            parent_exists = resolved.resolved_path.parent.exists()
            parent_is_directory = resolved.resolved_path.parent.is_dir()
        except OSError:
            return self._error(
                "FILE_WRITE_FAILED",
                "create_file parent metadata could not be read",
                arguments.path,
            )
        if not parent_exists:
            return self._error(
                "PARENT_DIRECTORY_NOT_FOUND",
                "create_file parent directory does not exist",
                arguments.path,
            )
        if not parent_is_directory:
            return self._error(
                "NOT_A_DIRECTORY",
                "create_file parent path is not a directory",
                arguments.path,
            )
        return self.prepared_call(
            call_id,
            arguments,
            FileOperationFacts(target=resolved, affected_paths=(resolved,)),
        )

    def execute(
        self,
        prepared_call: PreparedToolCall,
    ) -> ToolExecutionResult:
        """Create the prepared target with OS exclusive-create semantics."""
        if (
            prepared_call.tool_identity.name != self.name
            or not isinstance(prepared_call.validated_arguments, CreateFileArguments)
            or not isinstance(prepared_call.operation_facts, FileOperationFacts)
        ):
            return self._failure(
                "INTERNAL_TOOL_ERROR",
                "prepared call does not match create_file",
                "<unknown>",
            )
        arguments = prepared_call.validated_arguments
        facts = prepared_call.operation_facts
        resolved = facts.target
        if facts.affected_paths != (resolved,):
            return self._failure(
                "INTERNAL_TOOL_ERROR",
                "prepared create_file affected paths are invalid",
                arguments.path,
            )
        if resolved.raw_path != arguments.path:
            return self._failure(
                "INTERNAL_TOOL_ERROR",
                "resolved path does not match create_file arguments",
                arguments.path,
            )
        if not resolved.is_within_workspace:
            raise ValueError(
                "outside-workspace path requires policy evaluation before execution"
            )

        try:
            content = arguments.content.encode("utf-8")
        except UnicodeEncodeError:
            return self._failure(
                "FILE_WRITE_FAILED",
                "create_file content could not be encoded as UTF-8",
                arguments.path,
            )

        created = False
        try:
            with resolved.resolved_path.open("xb") as stream:
                created = True
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            return self._failure(
                "FILE_ALREADY_EXISTS",
                "create_file target already exists",
                arguments.path,
            )
        except FileNotFoundError:
            return self._failure(
                "PARENT_DIRECTORY_NOT_FOUND",
                "create_file parent directory no longer exists",
                arguments.path,
            )
        except NotADirectoryError:
            return self._failure(
                "NOT_A_DIRECTORY",
                "create_file parent path is not a directory",
                arguments.path,
            )
        except OSError as error:
            if created:
                self._remove_partial_file(resolved)
            return self._failure(
                "FILE_WRITE_FAILED",
                "create_file target could not be written",
                arguments.path,
                details={"reason": type(error).__name__},
            )

        return ToolExecutionResult(
            outcome=ToolOutcome.SUCCESS,
            content=CreateFileContent(
                path=resolved.workspace_relative_path or arguments.path,
                bytes_written=len(content),
            ),
        )

    @staticmethod
    def _remove_partial_file(resolved: ResolvedPath) -> None:
        try:
            resolved.resolved_path.unlink()
        except OSError:
            pass

    @staticmethod
    def _error(code: str, message: str, path: str) -> ToolError:
        return ToolError(code=code, message=message, details={"path": path})

    @classmethod
    def _failure(
        cls,
        code: str,
        message: str,
        path: str,
        *,
        details: dict[str, object] | None = None,
    ) -> ToolExecutionResult:
        error_details: dict[str, object] = {"path": path}
        if details is not None:
            error_details.update(details)
        return ToolExecutionResult(
            outcome=ToolOutcome.OPERATION_FAILURE,
            error=ToolError(code=code, message=message, details=error_details),
        )


__all__ = ["CreateFileArguments", "CreateFileContent", "CreateFileTool"]
