"""Conflict-safe exact text replacement Tool."""

from __future__ import annotations

import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pydantic import Field, field_validator

from .protocol import ToolCapability, ToolError, ToolKind, ToolOutcome
from .tooling import PreparedToolCall, Tool, ToolArguments, ToolExecutionResult
from .workspace import (
    FileOperationFacts,
    PathResolutionMode,
    ResolvedPath,
    WorkspacePathResolver,
)


class EditFileArguments(ToolArguments):
    """Validated model arguments for exact replacement editing."""

    path: str = Field(min_length=1)
    old_text: str = Field(min_length=1)
    new_text: str
    expected_count: int = Field(default=1, ge=1)


class AtomicEditArguments(ToolArguments):
    """One exact replacement in an atomic one-file multi-edit."""

    old_text: str = Field(min_length=1)
    new_text: str
    expected_count: int = Field(default=1, ge=1)


class ApplyEditsArguments(ToolArguments):
    """Validated arguments for bounded atomic edits to one file."""

    path: str = Field(min_length=1)
    edits: tuple[AtomicEditArguments, ...] = Field(min_length=1, max_length=32)

    @field_validator("edits", mode="before")
    @classmethod
    def _freeze_json_array(cls, value: object) -> object:
        """Accept the JSON array wire shape while retaining immutable storage."""

        return tuple(value) if isinstance(value, list) else value


@dataclass(frozen=True, slots=True)
class EditFileContent:
    """Structured summary of a successful exact replacement."""

    path: str
    replacement_count: int
    bytes_before: int
    bytes_after: int


@dataclass(frozen=True, slots=True)
class ApplyEditsContent:
    """Structured summary of one successful atomic multi-edit."""

    path: str
    edit_count: int
    replacement_count: int
    bytes_before: int
    bytes_after: int


@dataclass(frozen=True, slots=True)
class _PlannedReplacement:
    start: int
    end: int
    new_text: str
    edit_index: int


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

    def prepare(
        self,
        call_id: str,
        arguments: EditFileArguments,
    ) -> PreparedToolCall | ToolError:
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
            return self.prepared_call(
                call_id,
                arguments,
                FileOperationFacts(target=resolved, affected_paths=(resolved,)),
            )
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
        return self.prepared_call(
            call_id,
            arguments,
            FileOperationFacts(target=resolved, affected_paths=(resolved,)),
        )

    def execute(
        self,
        prepared_call: PreparedToolCall,
    ) -> ToolExecutionResult:
        """Re-read, verify, and atomically replace an exact text fragment."""
        if (
            prepared_call.tool_identity.name != self.name
            or not isinstance(prepared_call.validated_arguments, EditFileArguments)
            or not isinstance(prepared_call.operation_facts, FileOperationFacts)
        ):
            return self._failure(
                "INTERNAL_TOOL_ERROR",
                "prepared call does not match edit_file",
                "<unknown>",
            )
        arguments = prepared_call.validated_arguments
        facts = prepared_call.operation_facts
        resolved = facts.target
        if facts.affected_paths != (resolved,):
            return self._failure(
                "INTERNAL_TOOL_ERROR",
                "prepared edit_file affected paths are invalid",
                arguments.path,
            )
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

        line_ending = _consistent_line_ending(original_text)
        old_text = _adapt_line_endings(arguments.old_text, line_ending)
        new_text = _adapt_line_endings(arguments.new_text, line_ending)

        actual_count = original_text.count(old_text)
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
            old_text,
            new_text,
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


class ApplyEditsTool(Tool[ApplyEditsArguments]):
    """Atomically apply bounded non-overlapping exact edits to one file."""

    __slots__ = ("_resolver",)

    def __init__(self, resolver: WorkspacePathResolver) -> None:
        super().__init__(
            name="apply_edits",
            description=(
                "Atomically apply multiple non-overlapping exact replacements "
                "to one existing UTF-8 file; every match count is validated "
                "against the same original snapshot before any write"
            ),
            argument_model=ApplyEditsArguments,
            kind=ToolKind.LOCAL,
            capabilities=frozenset({ToolCapability.FILE_MUTATION}),
        )
        object.__setattr__(self, "_resolver", resolver)

    def prepare(
        self,
        call_id: str,
        arguments: ApplyEditsArguments,
    ) -> PreparedToolCall | ToolError:
        try:
            resolved = self._resolver.resolve_workspace_path(
                arguments.path,
                PathResolutionMode.EXISTING,
            )
        except FileNotFoundError:
            return self._error(
                "FILE_NOT_FOUND",
                "apply_edits target does not exist",
                arguments.path,
            )
        except (OSError, ValueError):
            return self._error(
                "FILE_READ_FAILED",
                "apply_edits target path could not be resolved",
                arguments.path,
            )

        if not resolved.is_within_workspace:
            return self.prepared_call(
                call_id,
                arguments,
                FileOperationFacts(target=resolved, affected_paths=(resolved,)),
            )
        try:
            target_mode = resolved.resolved_path.stat().st_mode
        except FileNotFoundError:
            return self._error(
                "FILE_NOT_FOUND",
                "apply_edits target does not exist",
                arguments.path,
            )
        except OSError:
            return self._error(
                "FILE_READ_FAILED",
                "apply_edits target metadata could not be read",
                arguments.path,
            )
        if not stat.S_ISREG(target_mode):
            return self._error(
                "NOT_A_FILE",
                "apply_edits target is not a file",
                arguments.path,
            )
        return self.prepared_call(
            call_id,
            arguments,
            FileOperationFacts(target=resolved, affected_paths=(resolved,)),
        )

    def execute(self, prepared_call: PreparedToolCall) -> ToolExecutionResult:
        if (
            prepared_call.tool_identity.name != self.name
            or not isinstance(
                prepared_call.validated_arguments, ApplyEditsArguments
            )
            or not isinstance(prepared_call.operation_facts, FileOperationFacts)
        ):
            return self._failure(
                "INTERNAL_TOOL_ERROR",
                "prepared call does not match apply_edits",
                "<unknown>",
            )
        arguments = prepared_call.validated_arguments
        facts = prepared_call.operation_facts
        resolved = facts.target
        if facts.affected_paths != (resolved,) or resolved.raw_path != arguments.path:
            return self._failure(
                "INTERNAL_TOOL_ERROR",
                "prepared apply_edits facts are invalid",
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
                "apply_edits target disappeared before it could be changed",
                arguments.path,
            )
        except IsADirectoryError:
            return self._failure(
                "NOT_A_FILE", "apply_edits target is not a file", arguments.path
            )
        except OSError:
            return self._failure(
                "FILE_READ_FAILED",
                "apply_edits target could not be read",
                arguments.path,
            )
        if b"\x00" in original_bytes:
            return self._failure(
                "BINARY_FILE_UNSUPPORTED",
                "binary files are not supported by apply_edits",
                arguments.path,
            )
        try:
            original_text = original_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return self._failure(
                "TEXT_DECODING_FAILED",
                "apply_edits target is not valid UTF-8 text",
                arguments.path,
            )

        line_ending = _consistent_line_ending(original_text)
        planned: list[_PlannedReplacement] = []
        for edit_index, edit in enumerate(arguments.edits):
            old_text = _adapt_line_endings(edit.old_text, line_ending)
            new_text = _adapt_line_endings(edit.new_text, line_ending)
            spans = _exact_match_spans(original_text, old_text)
            actual_count = len(spans)
            if actual_count == 0:
                return self._failure(
                    "EDIT_TARGET_NOT_FOUND",
                    "an apply_edits exact target was not found in the current file",
                    arguments.path,
                    details={
                        "edit_index": edit_index,
                        "expected_count": edit.expected_count,
                        "actual_count": actual_count,
                    },
                )
            if actual_count != edit.expected_count:
                return self._failure(
                    "EDIT_MATCH_COUNT_MISMATCH",
                    "an apply_edits target count does not match expected_count",
                    arguments.path,
                    details={
                        "edit_index": edit_index,
                        "expected_count": edit.expected_count,
                        "actual_count": actual_count,
                    },
                )
            planned.extend(
                _PlannedReplacement(start, end, new_text, edit_index)
                for start, end in spans
            )

        planned.sort(key=lambda replacement: (replacement.start, replacement.end))
        for previous, current in zip(planned, planned[1:]):
            if current.start < previous.end:
                return self._failure(
                    "EDIT_OVERLAP",
                    "apply_edits targets overlap in the original file snapshot",
                    arguments.path,
                    details={
                        "first_edit_index": previous.edit_index,
                        "second_edit_index": current.edit_index,
                    },
                )

        pieces: list[str] = []
        cursor = 0
        for replacement in planned:
            pieces.append(original_text[cursor : replacement.start])
            pieces.append(replacement.new_text)
            cursor = replacement.end
        pieces.append(original_text[cursor:])
        updated_bytes = "".join(pieces).encode("utf-8")
        write_error = EditFileTool._replace_with_temporary_sibling(
            resolved.resolved_path,
            updated_bytes,
            original_mode,
        )
        if write_error is not None:
            return self._failure(
                "EDIT_WRITE_FAILED",
                "atomically edited file could not be installed",
                arguments.path,
                details={"reason": type(write_error).__name__},
            )
        return ToolExecutionResult(
            outcome=ToolOutcome.SUCCESS,
            content=ApplyEditsContent(
                path=resolved.workspace_relative_path or arguments.path,
                edit_count=len(arguments.edits),
                replacement_count=len(planned),
                bytes_before=len(original_bytes),
                bytes_after=len(updated_bytes),
            ),
        )

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


__all__ = [
    "ApplyEditsArguments",
    "ApplyEditsContent",
    "ApplyEditsTool",
    "AtomicEditArguments",
    "EditFileArguments",
    "EditFileContent",
    "EditFileTool",
]


def _consistent_line_ending(text: str) -> str | None:
    """Return one file-wide newline style, or None for mixed/no newlines."""

    without_crlf = text.replace("\r\n", "")
    styles = {
        style
        for style, present in (
            ("\r\n", "\r\n" in text),
            ("\n", "\n" in without_crlf),
            ("\r", "\r" in without_crlf),
        )
        if present
    }
    return next(iter(styles)) if len(styles) == 1 else None


def _adapt_line_endings(value: str, line_ending: str | None) -> str:
    """Adapt model-observed newlines to a consistent target without touching it."""

    if line_ending is None or not any(marker in value for marker in ("\r", "\n")):
        return value
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.replace("\n", line_ending)


def _exact_match_spans(text: str, needle: str) -> tuple[tuple[int, int], ...]:
    """Return non-overlapping spans matching ``str.count`` semantics."""

    spans: list[tuple[int, int]] = []
    cursor = 0
    while True:
        start = text.find(needle, cursor)
        if start < 0:
            return tuple(spans)
        end = start + len(needle)
        spans.append((start, end))
        cursor = end
