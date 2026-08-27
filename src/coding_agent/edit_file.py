"""Conflict-safe exact text replacement Tool."""

from __future__ import annotations

import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pydantic import Field

from .protocol import ToolCapability, ToolError, ToolKind, ToolOutcome
from .tooling import Tool, ToolArguments, ToolExecutionResult
from .workspace import PathResolutionMode, ResolvedPath, WorkspacePathResolver


class EditFileArguments(ToolArguments):
    """Validated model arguments for exact replacement editing."""

    path: str = Field(min_length=1)
    old_text: str = Field(min_length=1)
    new_text: str
    expected_count: int = Field(default=1, ge=1)


@dataclass(frozen=True, slots=True)
class EditFileContent:
    """Structured summary of a successful exact replacement."""

    path: str
    replacement_count: int
    bytes_before: int
    bytes_after: int


class EditFileTool(Tool[EditFileArguments]):
    """Replace an exact UTF-8 text fragment only at the expected count."""

    __slots__ = ("_resolver",)

    def __init__(self, resolver: WorkspacePathResolver) -> None:
        super().__init__(
            name="edit_file",
            description=(
                "Replace exact text in an existing UTF-8 file when its occurrence "
                "count matches the expected count"
            ),
            argument_model=EditFileArguments,
            kind=ToolKind.LOCAL,
            capabilities=frozenset({ToolCapability.FILE_MUTATION}),
        )
        object.__setattr__(self, "_resolver", resolver)

    def prepare(self, arguments: EditFileArguments) -> ResolvedPath | ToolError:
        """Resolve the existing edit target and validate its file shape."""
        try:
            resolved = self._resolver.resolve_workspace_path(
                arguments.path,
                PathResolutionMode.EXISTING,
            )
        except FileNotFoundError:
            return self._error("FILE_NOT_FOUND", "edit target does not exist", arguments.path)
        except (OSError, ValueError):
            return self._error(
                "FILE_READ_FAILED",
                "edit target path could not be resolved",
                arguments.path,
            )

        if not resolved.is_within_workspace:
            return resolved
        try:
            target_mode = resolved.resolved_path.stat().st_mode
        except FileNotFoundError:
            return self._error("FILE_NOT_FOUND", "edit target does not exist", arguments.path)
        except OSError:
            return self._error(
                "FILE_READ_FAILED",
                "edit target metadata could not be read",
                arguments.path,
            )
        if not stat.S_ISREG(target_mode):
            return self._error("NOT_A_FILE", "edit target is not a file", arguments.path)
        return resolved

    def execute(
        self,
        arguments: EditFileArguments,
        resolved: ResolvedPath,
    ) -> ToolExecutionResult:
        """Re-read, verify, and atomically replace an exact text fragment."""
        if resolved.raw_path != arguments.path:
            return self._failure(
                "INTERNAL_TOOL_ERROR",
                "resolved path does not match edit_file arguments",
                arguments.path,
            )
        if not resolved.is_within_workspace:
            raise ValueError(
                "outside-workspace path requires policy evaluation before execution"
            )

        try:
            original_bytes = resolved.resolved_path.read_bytes()
            original_mode = stat.S_IMODE(resolved.resolved_path.stat().st_mode)
        except FileNotFoundError:
            return self._failure(
                "FILE_NOT_FOUND",
                "edit target disappeared before it could be changed",
                arguments.path,
            )
        except IsADirectoryError:
            return self._failure("NOT_A_FILE", "edit target is not a file", arguments.path)
        except OSError:
            return self._failure(
                "FILE_READ_FAILED",
                "edit target could not be read",
                arguments.path,
            )

        if b"\x00" in original_bytes:
            return self._failure(
                "BINARY_FILE_UNSUPPORTED",
                "binary files are not supported by edit_file",
                arguments.path,
            )
        try:
            original_text = original_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return self._failure(
                "TEXT_DECODING_FAILED",
                "edit target is not valid UTF-8 text",
                arguments.path,
            )

        actual_count = original_text.count(arguments.old_text)
        if actual_count == 0:
            return self._failure(
                "EDIT_TARGET_NOT_FOUND",
                "exact edit target was not found in the current file",
                arguments.path,
                details={
                    "expected_count": arguments.expected_count,
                    "actual_count": actual_count,
                },
            )
        if actual_count != arguments.expected_count:
            return self._failure(
                "EDIT_MATCH_COUNT_MISMATCH",
                "exact edit target count does not match expected_count",
                arguments.path,
                details={
                    "expected_count": arguments.expected_count,
                    "actual_count": actual_count,
                },
            )

        updated_bytes = original_text.replace(
            arguments.old_text,
            arguments.new_text,
        ).encode("utf-8")
        write_error = self._replace_with_temporary_sibling(
            resolved.resolved_path,
            updated_bytes,
            original_mode,
        )
        if write_error is not None:
            return self._failure(
                "EDIT_WRITE_FAILED",
                "edited file could not be installed",
                arguments.path,
                details={"reason": type(write_error).__name__},
            )

        return ToolExecutionResult(
            outcome=ToolOutcome.SUCCESS,
            content=EditFileContent(
                path=resolved.workspace_relative_path or arguments.path,
                replacement_count=actual_count,
                bytes_before=len(original_bytes),
                bytes_after=len(updated_bytes),
            ),
        )

    @staticmethod
    def _replace_with_temporary_sibling(
        target: Path,
        content: bytes,
        original_mode: int,
    ) -> OSError | None:
        file_descriptor: int | None = None
        temporary_path: str | None = None
        try:
            file_descriptor, temporary_path = tempfile.mkstemp(
                prefix=f".{target.name}.",
                suffix=".tmp",
                dir=target.parent,
            )
            with os.fdopen(file_descriptor, "wb") as stream:
                file_descriptor = None
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary_path, original_mode)
            os.replace(temporary_path, target)
            temporary_path = None
            return None
        except OSError as error:
            return error
        finally:
            if file_descriptor is not None:
                try:
                    os.close(file_descriptor)
                except OSError:
                    pass
            if temporary_path is not None:
                try:
                    os.unlink(temporary_path)
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


__all__ = ["EditFileArguments", "EditFileContent", "EditFileTool"]
