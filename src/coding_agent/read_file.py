"""Bounded UTF-8 read_file LOCAL Tool."""

from __future__ import annotations

import stat
from dataclasses import dataclass
from typing import Self

from pydantic import Field, model_validator

from .protocol import ToolCapability, ToolError, ToolKind, ToolOutcome
from .tooling import PreparedToolCall, Tool, ToolArguments, ToolExecutionResult
from .workspace import (
    FileOperationFacts,
    PathResolutionMode,
    ResolvedPath,
    WorkspacePathResolver,
)


class ReadFileArguments(ToolArguments):
    """Validated model arguments for read_file."""

    path: str = Field(min_length=1)
    start_line: int = Field(default=1, ge=1)
    end_line: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_line_range(self) -> Self:
        if self.end_line is not None and self.end_line < self.start_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        return self


@dataclass(frozen=True, slots=True)
class ReadFileContent:
    """Structured, model-facing read_file observation."""

    path: str
    start_line: int
    end_line: int | None
    total_lines: int
    content: str
    truncated: bool
    next_start_line: int | None


class ReadFileTool(Tool[ReadFileArguments]):
    """Resolve and read one UTF-8 text file with bounded output."""

    __slots__ = ("_resolver", "_max_lines", "_max_bytes")

    def __init__(
        self,
        resolver: WorkspacePathResolver,
        *,
        max_lines: int,
        max_bytes: int,
    ) -> None:
        if max_lines < 1:
            raise ValueError("max_lines must be at least 1")
        if max_bytes < 1:
            raise ValueError("max_bytes must be at least 1")
        super().__init__(
            name="read_file",
            description="Read a bounded, 1-indexed line range from a UTF-8 text file",
            argument_model=ReadFileArguments,
            kind=ToolKind.LOCAL,
            capabilities=frozenset({ToolCapability.FILE_READ}),
        )
        object.__setattr__(self, "_resolver", resolver)
        object.__setattr__(self, "_max_lines", max_lines)
        object.__setattr__(self, "_max_bytes", max_bytes)

    def prepare(
        self,
        call_id: str,
        arguments: ReadFileArguments,
    ) -> PreparedToolCall | ToolError:
        """Resolve the existing target and return facts or an expected error."""
        try:
            resolved = self._resolver.resolve_workspace_path(
                arguments.path,
                PathResolutionMode.EXISTING,
            )
        except FileNotFoundError:
            return self._error("FILE_NOT_FOUND", "file does not exist", arguments.path)
        except (OSError, ValueError):
            return self._error(
                "FILE_READ_FAILED",
                "file path could not be resolved",
                arguments.path,
            )

        if not resolved.is_within_workspace:
            return self.prepared_call(
                call_id,
                arguments,
                FileOperationFacts(target=resolved),
            )

        try:
            target_mode = resolved.resolved_path.stat().st_mode
        except FileNotFoundError:
            return self._error("FILE_NOT_FOUND", "file does not exist", arguments.path)
        except OSError:
            return self._error(
                "FILE_READ_FAILED",
                "file metadata could not be read",
                arguments.path,
            )

        if not stat.S_ISREG(target_mode):
            return self._error("NOT_A_FILE", "path is not a file", arguments.path)
        return self.prepared_call(
            call_id,
            arguments,
            FileOperationFacts(target=resolved),
        )

    def execute(
        self,
        prepared_call: PreparedToolCall,
    ) -> ToolExecutionResult:
        """Read an already resolved and policy-approved workspace target."""
        if (
            prepared_call.tool_identity.name != self.name
            or not isinstance(prepared_call.validated_arguments, ReadFileArguments)
            or not isinstance(prepared_call.operation_facts, FileOperationFacts)
        ):
            return self._failure(
                "INTERNAL_TOOL_ERROR",
                "prepared call does not match read_file",
                "<unknown>",
            )
        arguments = prepared_call.validated_arguments
        resolved = prepared_call.operation_facts.target
        if resolved.raw_path != arguments.path:
            return self._failure(
                "INTERNAL_TOOL_ERROR",
                "resolved path does not match read_file arguments",
                arguments.path,
            )
        if not resolved.is_within_workspace:
            raise ValueError(
                "outside-workspace path requires policy evaluation before execution"
            )

        try:
            content = self._read_bounded(arguments, resolved)
        except FileNotFoundError:
            return self._failure(
                "FILE_NOT_FOUND", "file disappeared before it could be read", arguments.path
            )
        except IsADirectoryError:
            return self._failure("NOT_A_FILE", "path is not a file", arguments.path)
        except UnicodeDecodeError:
            return self._failure(
                "TEXT_DECODING_FAILED",
                "file is not valid UTF-8 text",
                arguments.path,
            )
        except _BinaryFileError:
            return self._failure(
                "BINARY_FILE_UNSUPPORTED",
                "binary files are not supported by read_file",
                arguments.path,
            )
        except OSError:
            return self._failure(
                "FILE_READ_FAILED", "file could not be read", arguments.path
            )

        return ToolExecutionResult(outcome=ToolOutcome.SUCCESS, content=content)

    def _read_bounded(
        self,
        arguments: ReadFileArguments,
        resolved: ResolvedPath,
    ) -> ReadFileContent:
        rendered_lines: list[str] = []
        returned_bytes = 0
        actual_end_line: int | None = None
        byte_truncated = False
        total_lines = 0
        line_number = 1
        line_buffer = bytearray()
        line_overflow = False
        line_open = False

        def capture_segment(segment: str) -> None:
            nonlocal line_open, line_overflow
            if not segment:
                return
            line_open = True
            if not self._line_is_requested(arguments, line_number):
                return
            encoded = segment.encode("utf-8")
            remaining = self._max_bytes - len(line_buffer)
            if remaining > 0:
                line_buffer.extend(encoded[:remaining])
            if len(encoded) > remaining:
                line_overflow = True

        def finish_line() -> None:
            nonlocal actual_end_line, byte_truncated, line_buffer
            nonlocal line_number, line_open, line_overflow, returned_bytes
            if (
                self._line_is_requested(arguments, line_number)
                and len(rendered_lines) < self._max_lines
            ):
                prefix = f"{line_number} | ".encode("utf-8")
                encoded = prefix + bytes(line_buffer)
                separator_bytes = 1 if rendered_lines else 0
                available = self._max_bytes - returned_bytes - separator_bytes
                if available > 0:
                    if len(encoded) > available or line_overflow:
                        if not rendered_lines:
                            rendered = self._utf8_prefix(encoded, available)
                            rendered_lines.append(rendered)
                            returned_bytes += len(rendered.encode("utf-8"))
                            actual_end_line = line_number
                            byte_truncated = True
                    else:
                        rendered_lines.append(encoded.decode("utf-8"))
                        returned_bytes += separator_bytes + len(encoded)
                        actual_end_line = line_number
            line_number += 1
            line_buffer = bytearray()
            line_overflow = False
            line_open = False

        with resolved.resolved_path.open(
            "r",
            encoding="utf-8",
            errors="strict",
            newline=None,
        ) as stream:
            while chunk := stream.read(64 * 1024):
                if "\x00" in chunk:
                    raise _BinaryFileError
                segments = chunk.split("\n")
                for index, segment in enumerate(segments):
                    capture_segment(segment)
                    if index < len(segments) - 1:
                        finish_line()
                        total_lines += 1
            if line_open:
                finish_line()
                total_lines += 1

        truncated = byte_truncated or (
            actual_end_line is not None and actual_end_line < total_lines
        )
        next_start_line = actual_end_line + 1 if truncated else None

        return ReadFileContent(
            path=resolved.workspace_relative_path or arguments.path,
            start_line=arguments.start_line,
            end_line=actual_end_line,
            total_lines=total_lines,
            content="\n".join(rendered_lines),
            truncated=truncated,
            next_start_line=next_start_line,
        )

    @staticmethod
    def _line_is_requested(
        arguments: ReadFileArguments,
        line_number: int,
    ) -> bool:
        return line_number >= arguments.start_line and (
            arguments.end_line is None or line_number <= arguments.end_line
        )

    @staticmethod
    def _utf8_prefix(encoded: bytes, limit: int) -> str:
        return encoded[:limit].decode("utf-8", errors="ignore")

    @staticmethod
    def _error(code: str, message: str, path: str) -> ToolError:
        return ToolError(code=code, message=message, details={"path": path})

    @classmethod
    def _failure(
        cls,
        code: str,
        message: str,
        path: str,
    ) -> ToolExecutionResult:
        return ToolExecutionResult(
            outcome=ToolOutcome.OPERATION_FAILURE,
            error=cls._error(code, message, path),
        )


class _BinaryFileError(Exception):
    """Internal signal that a streamed text read encountered a NUL byte."""


__all__ = ["ReadFileArguments", "ReadFileContent", "ReadFileTool"]
