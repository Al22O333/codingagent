"""Bounded workspace discovery Tools with shared ignore behavior."""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from heapq import heappop, heappush
from pathlib import Path, PurePosixPath

from pathspec import PathSpec
from pydantic import Field

from .protocol import ToolCapability, ToolError, ToolKind, ToolOutcome
from .tooling import PreparedToolCall, Tool, ToolArguments, ToolExecutionResult
from .workspace import (
    FileOperationFacts,
    PathResolutionMode,
    ResolvedPath,
    WorkspacePathResolver,
)


DEFAULT_NOISE_DIRECTORIES = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        "dist",
        "build",
    }
)


class ListDirectoryArguments(ToolArguments):
    """Validated arguments for a one-level directory listing."""

    path: str = Field(default=".", min_length=1)


class SearchFilesArguments(ToolArguments):
    """Validated arguments for recursive path-glob search."""

    pattern: str = Field(min_length=1)
    path: str = Field(default=".", min_length=1)


@dataclass(frozen=True, slots=True)
class DirectoryEntry:
    """One model-facing direct child of a listed directory."""

    relative_path: str
    type: str

    def __post_init__(self) -> None:
        if self.type not in {"file", "directory"}:
            raise ValueError("directory entry type must be file or directory")


@dataclass(frozen=True, slots=True)
class ListDirectoryContent:
    """Bounded one-level directory listing."""

    path: str
    entries: tuple[DirectoryEntry, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class SearchFilesContent:
    """Bounded deterministic file-path search result."""

    pattern: str
    path: str
    matches: tuple[str, ...]
    truncated: bool


class DiscoveryIgnoreRules:
    """Workspace-root gitignore plus the small v1 noise-directory set."""

    def __init__(self, workspace_root: Path) -> None:
        gitignore = workspace_root / ".gitignore"
        lines: tuple[str, ...] = ()
        if (
            gitignore.is_file()
            and gitignore.resolve(strict=True).is_relative_to(workspace_root)
        ):
            lines = tuple(
                gitignore.read_text(encoding="utf-8", errors="replace").splitlines()
            )
        self._gitignore = PathSpec.from_lines("gitwildmatch", lines)

    def ignores(self, workspace_relative_path: str, *, is_directory: bool) -> bool:
        """Return whether discovery should hide a workspace-relative path."""
        path = PurePosixPath(workspace_relative_path)
        if any(part.casefold() in DEFAULT_NOISE_DIRECTORIES for part in path.parts):
            return True
        candidate = path.as_posix()
        if is_directory:
            candidate += "/"
        return self._gitignore.match_file(candidate)


class ListDirectoryTool(Tool[ListDirectoryArguments]):
    """List one directory level with deterministic bounded output."""

    __slots__ = ("_max_entries", "_resolver")

    def __init__(self, resolver: WorkspacePathResolver, *, max_entries: int) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        super().__init__(
            name="list_directory",
            description="List one level of a workspace directory",
            argument_model=ListDirectoryArguments,
            kind=ToolKind.LOCAL,
            capabilities=frozenset({ToolCapability.FILE_READ}),
        )
        object.__setattr__(self, "_resolver", resolver)
        object.__setattr__(self, "_max_entries", max_entries)

    def prepare(
        self,
        call_id: str,
        arguments: ListDirectoryArguments,
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
            ListDirectoryArguments,
        )
        if isinstance(prepared, ToolError):
            return _failure(prepared)
        arguments, facts = prepared
        resolved = facts.target
        mismatch = _validate_prepared_directory(arguments.path, resolved)
        if mismatch is not None:
            return _failure(mismatch)

        try:
            ignore_rules = DiscoveryIgnoreRules(self._resolver.workspace_root)
            entries = self._visible_entries(resolved, ignore_rules)
        except OSError:
            return _failure(
                ToolError(
                    code="DIRECTORY_LIST_FAILED",
                    message="directory contents could not be listed",
                    details={"path": arguments.path},
                )
            )

        entries.sort(
            key=lambda entry: (
                0 if entry.type == "directory" else 1,
                entry.relative_path.casefold(),
                entry.relative_path,
            )
        )
        truncated = len(entries) > self._max_entries
        content = ListDirectoryContent(
            path=resolved.workspace_relative_path or arguments.path,
            entries=tuple(entries[: self._max_entries]),
            truncated=truncated,
        )
        return ToolExecutionResult(outcome=ToolOutcome.SUCCESS, content=content)

    def _visible_entries(
        self,
        directory: ResolvedPath,
        ignore_rules: DiscoveryIgnoreRules,
    ) -> list[DirectoryEntry]:
        visible: list[DirectoryEntry] = []
        with os.scandir(directory.resolved_path) as iterator:
            for entry in iterator:
                entry_facts = _resolve_visible_entry(self._resolver, Path(entry.path))
                if entry_facts is None:
                    continue
                resolved_entry, relative_path = entry_facts
                is_directory = resolved_entry.resolved_path.is_dir()
                is_file = resolved_entry.resolved_path.is_file()
                if not is_directory and not is_file:
                    continue
                if ignore_rules.ignores(
                    relative_path,
                    is_directory=is_directory,
                ):
                    continue
                visible.append(
                    DirectoryEntry(
                        relative_path=relative_path,
                        type="directory" if is_directory else "file",
                    )
                )
        return visible


class SearchFilesTool(Tool[SearchFilesArguments]):
    """Recursively locate files by workspace-relative glob."""

    __slots__ = ("_max_results", "_resolver")

    def __init__(self, resolver: WorkspacePathResolver, *, max_results: int) -> None:
        if max_results < 1:
            raise ValueError("max_results must be at least 1")
        super().__init__(
            name="search_files",
            description="Find workspace files using a recursive glob pattern",
            argument_model=SearchFilesArguments,
            kind=ToolKind.LOCAL,
            capabilities=frozenset({ToolCapability.FILE_READ}),
        )
        object.__setattr__(self, "_resolver", resolver)
        object.__setattr__(self, "_max_results", max_results)

    def prepare(
        self,
        call_id: str,
        arguments: SearchFilesArguments,
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
            SearchFilesArguments,
        )
        if isinstance(prepared, ToolError):
            return _failure(prepared)
        arguments, facts = prepared
        resolved = facts.target
        mismatch = _validate_prepared_directory(arguments.path, resolved)
        if mismatch is not None:
            return _failure(mismatch)

        try:
            ignore_rules = DiscoveryIgnoreRules(self._resolver.workspace_root)
            matches = self._find_matches(arguments, resolved, ignore_rules)
        except OSError:
            return _failure(
                ToolError(
                    code="FILE_SEARCH_FAILED",
                    message="workspace files could not be searched",
                    details={"path": arguments.path},
                )
            )

        truncated = len(matches) > self._max_results
        content = SearchFilesContent(
            pattern=arguments.pattern,
            path=resolved.workspace_relative_path or arguments.path,
            matches=tuple(matches[: self._max_results]),
            truncated=truncated,
        )
        return ToolExecutionResult(outcome=ToolOutcome.SUCCESS, content=content)

    def _find_matches(
        self,
        arguments: SearchFilesArguments,
        start: ResolvedPath,
        ignore_rules: DiscoveryIgnoreRules,
    ) -> list[str]:
        matches: list[str] = []
        for _, relative_path, relative_to_start in _walk_visible_files(
            self._resolver,
            start,
            ignore_rules,
        ):
            if _glob_matches(relative_to_start, arguments.pattern):
                matches.append(relative_path)
                if len(matches) > self._max_results:
                    break
        return matches


def _walk_visible_files(
    resolver: WorkspacePathResolver,
    start: ResolvedPath,
    ignore_rules: DiscoveryIgnoreRules,
) -> Iterator[tuple[Path, str, str]]:
    """Yield visible files in deterministic path order with bounded results upstream."""
    pending: list[tuple[str, str, Path]] = []

    def enqueue(directory: Path) -> None:
        with os.scandir(directory) as iterator:
            for entry in iterator:
                path = Path(entry.path)
                try:
                    relative_path = path.relative_to(resolver.workspace_root).as_posix()
                except ValueError:
                    continue
                heappush(
                    pending,
                    (relative_path.casefold(), relative_path, path),
                )

    enqueue(start.resolved_path)
    while pending:
        _, _, path = heappop(pending)
        entry_facts = _resolve_visible_entry(resolver, path)
        if entry_facts is None:
            continue
        resolved_entry, relative_path = entry_facts
        is_directory = resolved_entry.resolved_path.is_dir()
        if ignore_rules.ignores(relative_path, is_directory=is_directory):
            continue
        if is_directory:
            if not path.is_symlink():
                enqueue(resolved_entry.resolved_path)
            continue
        if not resolved_entry.resolved_path.is_file():
            continue
        relative_to_start = path.relative_to(start.resolved_path).as_posix()
        yield resolved_entry.resolved_path, relative_path, relative_to_start


def _prepare_directory(
    resolver: WorkspacePathResolver,
    raw_path: str,
) -> ResolvedPath | ToolError:
    try:
        resolved = resolver.resolve_workspace_path(raw_path, PathResolutionMode.EXISTING)
    except FileNotFoundError:
        return ToolError(
            code="DIRECTORY_NOT_FOUND",
            message="directory does not exist",
            details={"path": raw_path},
        )
    except (OSError, ValueError):
        return ToolError(
            code="DIRECTORY_LIST_FAILED",
            message="directory path could not be resolved",
            details={"path": raw_path},
        )
    if not resolved.is_within_workspace:
        return resolved
    try:
        is_directory = resolved.resolved_path.is_dir()
    except OSError:
        return ToolError(
            code="DIRECTORY_LIST_FAILED",
            message="directory metadata could not be read",
            details={"path": raw_path},
        )
    if not is_directory:
        return ToolError(
            code="NOT_A_DIRECTORY",
            message="path is not a directory",
            details={"path": raw_path},
        )
    return resolved


def _validate_prepared_directory(
    raw_path: str,
    resolved: ResolvedPath,
) -> ToolError | None:
    if resolved.raw_path != raw_path:
        return ToolError(
            code="INTERNAL_TOOL_ERROR",
            message="resolved directory does not match Tool arguments",
        )
    if not resolved.is_within_workspace:
        raise ValueError(
            "outside-workspace directory requires policy evaluation before execution"
        )
    return None


def _resolve_visible_entry(
    resolver: WorkspacePathResolver,
    path: Path,
) -> tuple[ResolvedPath, str] | None:
    try:
        resolved = resolver.resolve_workspace_path(str(path), PathResolutionMode.EXISTING)
    except (FileNotFoundError, OSError, ValueError):
        return None
    if not resolved.is_within_workspace or resolved.is_sensitive:
        return None
    try:
        lexical_relative_path = path.relative_to(resolver.workspace_root).as_posix()
    except ValueError:
        return None
    return resolved, lexical_relative_path


def _glob_matches(relative_path: str, pattern: str) -> bool:
    path = PurePosixPath(relative_path)
    if path.match(pattern):
        return True
    if pattern.startswith("**/"):
        return path.match(pattern[3:])
    return False


def _prepared_file_action(
    prepared_call: PreparedToolCall,
    tool_name: str,
    argument_type,
):
    if (
        prepared_call.tool_identity.name != tool_name
        or not isinstance(prepared_call.validated_arguments, argument_type)
        or not isinstance(prepared_call.operation_facts, FileOperationFacts)
    ):
        return ToolError(
            code="INTERNAL_TOOL_ERROR",
            message=f"prepared call does not match {tool_name}",
        )
    return prepared_call.validated_arguments, prepared_call.operation_facts


def _failure(error: ToolError) -> ToolExecutionResult:
    return ToolExecutionResult(
        outcome=ToolOutcome.OPERATION_FAILURE,
        error=error,
    )


__all__ = [
    "DEFAULT_NOISE_DIRECTORIES",
    "DirectoryEntry",
    "DiscoveryIgnoreRules",
    "ListDirectoryArguments",
    "ListDirectoryContent",
    "ListDirectoryTool",
    "SearchFilesArguments",
    "SearchFilesContent",
    "SearchFilesTool",
]
