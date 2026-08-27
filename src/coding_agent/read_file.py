"""Bounded UTF-8 read_file LOCAL Tool."""

from __future__ import annotations

import stat
from dataclasses import dataclass
from typing import Self

from pydantic import Field, model_validator

from .protocol import ToolCapability, ToolError, ToolKind, ToolOutcome
from .tooling import Tool, ToolArguments, ToolExecutionResult
from .workspace import PathResolutionMode, ResolvedPath, WorkspacePathResolver


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

    def prepare(self, arguments: ReadFileArguments) -> ResolvedPath | ToolError:
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
            return resolved

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
        return resolved

    def execute(
        self,
        arguments: ReadFileArguments,
        resolved: ResolvedPath,
    ) -> ToolExecutionResult:
        """Read an already resolved and policy-approved workspace target."""
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
            raw_content = resolved.resolved_path.read_bytes()
        except FileNotFoundError:
            return self._failure(
                "FILE_NOT_FOUND", "file disappeared before it could be read", arguments.path
            )
        except IsADirectoryError:
            return self._failure("NOT_A_FILE", "path is not a file", arguments.path)
        except OSError:
            return self._failure(
                "FILE_READ_FAILED", "file could not be read", arguments.path
            )

        if b"\x00" in raw_content:
            return self._failure(
                "BINARY_FILE_UNSUPPORTED",
                "binary files are not supported by read_file",
                arguments.path,
            )

        try:
            text = raw_content.decode("utf-8")
        except UnicodeDecodeError:
            return self._failure(
                "TEXT_DECODING_FAILED",
                "file is not valid UTF-8 text",
                arguments.path,
            )

        content = self._build_content(arguments, resolved, text)
        return ToolExecutionResult(outcome=ToolOutcome.SUCCESS, content=content)

    def _build_content(
        self,
        arguments: ReadFileArguments,
        resolved: ResolvedPath,
        text: str,
    ) -> ReadFileContent:
        lines = text.splitlines()
        total_lines = len(lines)
        requested_last_line = min(arguments.end_line or total_lines, total_lines)
        first_index = arguments.start_line - 1
        rendered_lines: list[str] = []
        returned_bytes = 0
        actual_end_line: int | None = None
        byte_truncated = False

        if first_index < total_lines and arguments.start_line <= requested_last_line:
            for line_number in range(arguments.start_line, requested_last_line + 1):
                if len(rendered_lines) >= self._max_lines:
                    break
                rendered = f"{line_number} | {lines[line_number - 1]}"
                separator_bytes = 1 if rendered_lines else 0
                encoded = rendered.encode("utf-8")
                available_bytes = self._max_bytes - returned_bytes - separator_bytes
                if available_bytes <= 0:
                    break
                if len(encoded) > available_bytes:
                    if rendered_lines:
                        break
                    rendered = self._utf8_prefix(encoded, available_bytes)
                    encoded = rendered.encode("utf-8")
                    byte_truncated = True
                rendered_lines.append(rendered)
                returned_bytes += separator_bytes + len(encoded)
                actual_end_line = line_number

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


__all__ = ["ReadFileArguments", "ReadFileContent", "ReadFileTool"]
