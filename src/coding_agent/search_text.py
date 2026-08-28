"""Python-baseline bounded text search Tool."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from pydantic import Field

from .discovery import (
    DiscoveryIgnoreRules,
    _glob_matches,
    _prepare_directory,
    _prepared_file_action,
    _validate_prepared_directory,
    _walk_visible_files,
)
from .protocol import ToolCapability, ToolError, ToolKind, ToolOutcome
from .tooling import PreparedToolCall, Tool, ToolArguments, ToolExecutionResult
from .workspace import FileOperationFacts, WorkspacePathResolver


DEFAULT_MODEL_PROJECTION_CHARS = 16_000


class SearchTextArguments(ToolArguments):
    """Validated model arguments for workspace text search."""

    query: str = Field(min_length=1)
    path: str = Field(default=".", min_length=1)
    file_glob: str | None = Field(default=None, min_length=1)
    regex: bool = False
    case_sensitive: bool = False


@dataclass(frozen=True, slots=True)
class TextMatch:
    """One model-facing line match."""

    relative_path: str
    line_number: int
    line_text: str
    line_truncated: bool


@dataclass(frozen=True, slots=True)
class SearchTextContent:
    """Bounded deterministic text search result."""

    query: str
    path: str
    matches: tuple[TextMatch, ...]
    truncated: bool


class SearchTextTool(Tool[SearchTextArguments]):
    """Search UTF-8 workspace files without relying on an external index."""

    __slots__ = ("_max_line_bytes", "_max_matches", "_resolver")

    def __init__(
        self,
        resolver: WorkspacePathResolver,
        *,
        max_matches: int,
        max_line_bytes: int,
    ) -> None:
        if max_matches < 1:
            raise ValueError("max_matches must be at least 1")
        if max_line_bytes < 1:
            raise ValueError("max_line_bytes must be at least 1")
        super().__init__(
            name="search_text",
            description="Search UTF-8 workspace files for literal text or a regex",
            argument_model=SearchTextArguments,
            kind=ToolKind.LOCAL,
            capabilities=frozenset({ToolCapability.FILE_READ}),
        )
        object.__setattr__(self, "_resolver", resolver)
        object.__setattr__(self, "_max_matches", max_matches)
        object.__setattr__(self, "_max_line_bytes", max_line_bytes)

    def prepare(
        self,
        call_id: str,
        arguments: SearchTextArguments,
    ) -> PreparedToolCall | ToolError:
        resolved = _prepare_directory(self._resolver, arguments.path)
        if isinstance(resolved, ToolError):
            return resolved
        return self.prepared_call(
            call_id,
            arguments,
            FileOperationFacts(target=resolved),
        )

    def execute(
        self,
        prepared_call: PreparedToolCall,
    ) -> ToolExecutionResult:
        prepared = _prepared_file_action(
            prepared_call,
            self.name,
            SearchTextArguments,
        )
        if isinstance(prepared, ToolError):
            return self._failure(prepared)
        arguments, facts = prepared
        resolved = facts.target
        mismatch = _validate_prepared_directory(arguments.path, resolved)
        if mismatch is not None:
            return self._failure(mismatch)

        try:
            matcher = self._matcher(arguments)
        except re.error as error:
            return self._failure(
                ToolError(
                    code="INVALID_SEARCH_PATTERN",
                    message="regular expression is invalid",
                    details={"reason": str(error)},
                )
            )

        try:
            ignore_rules = DiscoveryIgnoreRules(self._resolver.workspace_root)
        except OSError:
            return self._failure(
                ToolError(
                    code="SEARCH_FAILED",
                    message="workspace text search could not load ignore rules",
                    details={"path": arguments.path},
                )
            )
        files = iter(_walk_visible_files(self._resolver, resolved, ignore_rules))

        matches: list[TextMatch] = []
        truncated = False
        while not truncated:
            try:
                path, relative_path, relative_to_start = next(files)
            except StopIteration:
                break
            except OSError:
                return self._failure(
                    ToolError(
                        code="SEARCH_FAILED",
                        message="workspace text search could not enumerate files",
                        details={"path": arguments.path},
                    )
                )
            if arguments.file_glob is not None and not _glob_matches(
                relative_to_start,
                arguments.file_glob,
            ):
                continue
            file_matches = self._search_file(
                path,
                relative_path,
                matcher,
                self._max_matches - len(matches) + 1,
            )
            for match in file_matches:
                if len(matches) >= self._max_matches:
                    truncated = True
                    break
                matches.append(match)
            if truncated:
                break

        return ToolExecutionResult(
            outcome=ToolOutcome.SUCCESS,
            content=SearchTextContent(
                query=arguments.query,
                path=resolved.workspace_relative_path or arguments.path,
                matches=tuple(matches),
                truncated=truncated,
            ),
        )

    def _matcher(self, arguments: SearchTextArguments):
        if arguments.regex:
            flags = 0 if arguments.case_sensitive else re.IGNORECASE
            pattern = re.compile(arguments.query, flags)
            return pattern.search
        if arguments.case_sensitive:
            return lambda line: arguments.query in line
        folded_query = arguments.query.casefold()
        return lambda line: folded_query in line.casefold()

    def _search_file(
        self,
        path: Path,
        relative_path: str,
        matcher,
        match_limit: int,
    ) -> list[TextMatch]:
        max_scanned_line_bytes = max(self._max_line_bytes, 1024 * 1024)
        matches: list[TextMatch] = []
        line_number = 1
        line_buffer = bytearray()
        line_overflow = False
        line_open = False

        def capture_segment(segment: str) -> None:
            nonlocal line_open, line_overflow
            if not segment:
                return
            line_open = True
            if line_overflow:
                return
            encoded = segment.encode("utf-8")
            remaining = max_scanned_line_bytes - len(line_buffer)
            if len(encoded) > remaining:
                line_overflow = True
                line_buffer.clear()
                return
            line_buffer.extend(encoded)

        def finish_line() -> None:
            nonlocal line_buffer, line_number, line_open, line_overflow
            if not line_overflow:
                line = line_buffer.decode("utf-8")
                if matcher(line) and len(matches) < match_limit:
                    rendered, rendered_truncated = self._bounded_line(line)
                    matches.append(
                        TextMatch(
                            relative_path=relative_path,
                            line_number=line_number,
                            line_text=rendered,
                            line_truncated=rendered_truncated,
                        )
                    )
            line_number += 1
            line_buffer = bytearray()
            line_overflow = False
            line_open = False

        try:
            with path.open(
                "r",
                encoding="utf-8",
                errors="strict",
                newline=None,
            ) as stream:
                while chunk := stream.read(64 * 1024):
                    if "\x00" in chunk:
                        return []
                    segments = chunk.split("\n")
                    for index, segment in enumerate(segments):
                        capture_segment(segment)
                        if index < len(segments) - 1:
                            finish_line()
                if line_open:
                    finish_line()
        except (OSError, UnicodeDecodeError):
            return []
        return matches

    def _bounded_line(self, line: str) -> tuple[str, bool]:
        encoded = line.encode("utf-8")
        if len(encoded) <= self._max_line_bytes:
            return line, False
        return (
            encoded[: self._max_line_bytes].decode("utf-8", errors="ignore"),
            True,
        )

    @staticmethod
    def _failure(error: ToolError) -> ToolExecutionResult:
        return ToolExecutionResult(
            outcome=ToolOutcome.OPERATION_FAILURE,
            error=error,
        )


__all__ = [
    "DEFAULT_MODEL_PROJECTION_CHARS",
    "SearchTextArguments",
    "SearchTextContent",
    "SearchTextTool",
    "TextMatch",
]
