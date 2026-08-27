"""Python-baseline bounded text search Tool."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from pydantic import Field

from .discovery import (
    DiscoveryIgnoreRules,
    _glob_matches,
    _prepare_directory,
    _prepared_file_action,
    _resolve_visible_entry,
    _validate_prepared_directory,
)
from .protocol import ToolCapability, ToolError, ToolKind, ToolOutcome
from .tooling import PreparedToolCall, Tool, ToolArguments, ToolExecutionResult
from .workspace import FileOperationFacts, ResolvedPath, WorkspacePathResolver


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
            files = self._visible_files(arguments, resolved, ignore_rules)
        except OSError:
            return self._failure(
                ToolError(
                    code="SEARCH_FAILED",
                    message="workspace text search could not enumerate files",
                    details={"path": arguments.path},
                )
            )

        matches: list[TextMatch] = []
        truncated = False
        for path, relative_path in files:
            file_matches = self._search_file(path, relative_path, matcher)
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

    def _visible_files(
        self,
        arguments: SearchTextArguments,
        start: ResolvedPath,
        ignore_rules: DiscoveryIgnoreRules,
    ) -> list[tuple[Path, str]]:
        files: list[tuple[Path, str]] = []
        pending = [start.resolved_path]
        while pending:
            current = pending.pop()
            with os.scandir(current) as iterator:
                entries = sorted(
                    iterator,
                    key=lambda entry: (entry.name.casefold(), entry.name),
                )
            directories: list[Path] = []
            for entry in entries:
                entry_facts = _resolve_visible_entry(self._resolver, Path(entry.path))
                if entry_facts is None:
                    continue
                resolved_entry, relative_path = entry_facts
                is_directory = resolved_entry.resolved_path.is_dir()
                if ignore_rules.ignores(relative_path, is_directory=is_directory):
                    continue
                if is_directory:
                    if not entry.is_symlink():
                        directories.append(resolved_entry.resolved_path)
                    continue
                if not resolved_entry.resolved_path.is_file():
                    continue
                relative_to_start = Path(entry.path).relative_to(
                    start.resolved_path
                ).as_posix()
                if arguments.file_glob is not None and not _glob_matches(
                    relative_to_start,
                    arguments.file_glob,
                ):
                    continue
                files.append((resolved_entry.resolved_path, relative_path))
            pending.extend(reversed(directories))
        files.sort(key=lambda item: (item[1].casefold(), item[1]))
        return files

    def _search_file(
        self,
        path: Path,
        relative_path: str,
        matcher,
    ) -> list[TextMatch]:
        try:
            raw_content = path.read_bytes()
        except OSError:
            return []
        if b"\x00" in raw_content:
            return []
        try:
            text = raw_content.decode("utf-8")
        except UnicodeDecodeError:
            return []

        matches: list[TextMatch] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if matcher(line):
                rendered, line_truncated = self._bounded_line(line)
                matches.append(
                    TextMatch(
                        relative_path=relative_path,
                        line_number=line_number,
                        line_text=rendered,
                        line_truncated=line_truncated,
                    )
                )
                if len(matches) > self._max_matches:
                    break
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
    "SearchTextArguments",
    "SearchTextContent",
    "SearchTextTool",
    "TextMatch",
]
