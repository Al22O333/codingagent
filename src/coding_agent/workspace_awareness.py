"""Bounded, read-only Git workspace change awareness."""

from __future__ import annotations

import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Protocol


MAX_AWARENESS_PATHS = 200
MAX_GIT_STATUS_BYTES = 64 * 1024
MAX_AWARENESS_PATH_CHARS = 500
DEFAULT_GIT_TIMEOUT_SECONDS = 3.0


class WorkspaceAwarenessState(StrEnum):
    """Availability of an exact-root Git workspace snapshot."""

    AVAILABLE = "AVAILABLE"
    NOT_GIT = "NOT_GIT"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    """One bounded read-only observation of dirty Git paths."""

    state: WorkspaceAwarenessState
    dirty_paths: tuple[str, ...] = ()
    truncated: bool = False

    def __post_init__(self) -> None:
        normalized = tuple(
            sorted(
                {
                    path
                    for raw_path in self.dirty_paths
                    if (path := _normalize_path(raw_path)) is not None
                }
            )
        )
        if len(normalized) != len(set(self.dirty_paths)):
            raise ValueError("workspace snapshot contains an invalid path")
        if len(normalized) > MAX_AWARENESS_PATHS:
            raise ValueError("workspace snapshot path count exceeds bound")
        object.__setattr__(self, "dirty_paths", normalized)


@dataclass(frozen=True, slots=True)
class WorkspaceChangeFacts:
    """Conservative terminal attribution facts for one Run."""

    awareness_state: WorkspaceAwarenessState
    pre_existing_dirty_paths: tuple[str, ...] = ()
    known_agent_touched_paths: tuple[str, ...] = ()
    new_or_other_dirty_paths: tuple[str, ...] = ()
    attribution_uncertain: bool = False
    truncated: bool = False

    def __post_init__(self) -> None:
        for field_name in (
            "pre_existing_dirty_paths",
            "known_agent_touched_paths",
            "new_or_other_dirty_paths",
        ):
            raw_paths = tuple(getattr(self, field_name))
            paths = tuple(
                sorted(
                    {
                        path
                        for raw_path in raw_paths
                        if (path := _normalize_path(raw_path)) is not None
                    }
                )
            )
            if len(paths) != len(set(raw_paths)):
                raise ValueError(f"{field_name} contains an invalid path")
            if len(paths) > MAX_AWARENESS_PATHS:
                raise ValueError(f"{field_name} exceeds awareness path bound")
            object.__setattr__(self, field_name, paths)


class WorkspaceChangeObserver(Protocol):
    """Read-only snapshot seam used by AgentRuntime."""

    def snapshot(self) -> WorkspaceSnapshot:
        """Return one bounded snapshot without mutating the workspace."""


class GitWorkspaceChangeObserver:
    """Observe dirty paths only for a Git root equal to the bound workspace."""

    __slots__ = ("_workspace", "_timeout_seconds")

    def __init__(
        self,
        workspace: str | Path,
        *,
        timeout_seconds: float = DEFAULT_GIT_TIMEOUT_SECONDS,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Git awareness timeout must be positive")
        self._workspace = Path(workspace).resolve(strict=True)
        self._timeout_seconds = timeout_seconds

    def snapshot(self) -> WorkspaceSnapshot:
        try:
            top_level = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=self._workspace,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=self._timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired):
            return WorkspaceSnapshot(WorkspaceAwarenessState.UNAVAILABLE)
        if top_level.returncode != 0:
            return WorkspaceSnapshot(WorkspaceAwarenessState.NOT_GIT)
        try:
            reported_root = Path(
                top_level.stdout.decode("utf-8", errors="strict").strip()
            ).resolve(strict=True)
        except (OSError, UnicodeDecodeError, ValueError):
            return WorkspaceSnapshot(WorkspaceAwarenessState.UNAVAILABLE)
        if reported_root != self._workspace:
            return WorkspaceSnapshot(WorkspaceAwarenessState.UNAVAILABLE)

        try:
            status = subprocess.run(
                [
                    "git",
                    "status",
                    "--porcelain=v1",
                    "-z",
                    "--untracked-files=all",
                ],
                cwd=self._workspace,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=self._timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired):
            return WorkspaceSnapshot(WorkspaceAwarenessState.UNAVAILABLE)
        if status.returncode != 0:
            return WorkspaceSnapshot(WorkspaceAwarenessState.UNAVAILABLE)
        paths, truncated, valid = _parse_porcelain_status(status.stdout)
        if not valid:
            return WorkspaceSnapshot(WorkspaceAwarenessState.UNAVAILABLE)
        return WorkspaceSnapshot(
            WorkspaceAwarenessState.AVAILABLE,
            dirty_paths=paths,
            truncated=truncated,
        )


def build_workspace_change_facts(
    start: WorkspaceSnapshot,
    end: WorkspaceSnapshot,
    known_touched_paths: Iterable[str],
    *,
    execution_uncertain: bool,
) -> WorkspaceChangeFacts:
    """Compare two snapshots without claiming content-level provenance."""

    touched_all: set[str] = set()
    invalid_touched = False
    for raw_path in known_touched_paths:
        path = _normalize_path(raw_path)
        if path is None:
            invalid_touched = True
        else:
            touched_all.add(path)
    touched_truncated = len(touched_all) > MAX_AWARENESS_PATHS
    touched = tuple(sorted(touched_all)[:MAX_AWARENESS_PATHS])
    pre_existing = start.dirty_paths
    new_or_other: tuple[str, ...] = ()
    if (
        start.state is WorkspaceAwarenessState.AVAILABLE
        and end.state is WorkspaceAwarenessState.AVAILABLE
    ):
        new_or_other = tuple(
            sorted(set(end.dirty_paths) - set(pre_existing) - set(touched))
        )[:MAX_AWARENESS_PATHS]

    if (
        start.state is WorkspaceAwarenessState.AVAILABLE
        and end.state is WorkspaceAwarenessState.AVAILABLE
    ):
        state = WorkspaceAwarenessState.AVAILABLE
    elif (
        start.state is WorkspaceAwarenessState.NOT_GIT
        and end.state is WorkspaceAwarenessState.NOT_GIT
    ):
        state = WorkspaceAwarenessState.NOT_GIT
    else:
        state = WorkspaceAwarenessState.UNAVAILABLE

    truncated = (
        start.truncated or end.truncated or touched_truncated or invalid_touched
    )
    attribution_uncertain = (
        execution_uncertain
        or state is not WorkspaceAwarenessState.AVAILABLE
        or truncated
        or bool(set(pre_existing) & set(touched))
        or bool(new_or_other)
    )
    return WorkspaceChangeFacts(
        awareness_state=state,
        pre_existing_dirty_paths=pre_existing,
        known_agent_touched_paths=touched,
        new_or_other_dirty_paths=new_or_other,
        attribution_uncertain=attribution_uncertain,
        truncated=truncated,
    )


def _parse_porcelain_status(data: bytes) -> tuple[tuple[str, ...], bool, bool]:
    truncated = len(data) > MAX_GIT_STATUS_BYTES
    bounded = data[:MAX_GIT_STATUS_BYTES]
    if truncated:
        bounded = bounded[: bounded.rfind(b"\0") + 1]
    tokens = bounded.split(b"\0")
    paths: set[str] = set()
    index = 0
    while index < len(tokens):
        record = tokens[index]
        index += 1
        if not record:
            continue
        if len(record) < 4 or record[2:3] != b" ":
            return (), truncated, False
        status = record[:2]
        normalized = _decode_path(record[3:])
        if normalized is None:
            truncated = True
        else:
            paths.add(normalized)
        if b"R" in status or b"C" in status:
            if index >= len(tokens) or not tokens[index]:
                return (), truncated, False
            original = _decode_path(tokens[index])
            index += 1
            if original is None:
                truncated = True
            else:
                paths.add(original)
        if len(paths) > MAX_AWARENESS_PATHS:
            truncated = True
    return tuple(sorted(paths)[:MAX_AWARENESS_PATHS]), truncated, True


def _decode_path(value: bytes) -> str | None:
    try:
        return _normalize_path(value.decode("utf-8", errors="strict"))
    except UnicodeDecodeError:
        return None


def _normalize_path(value: str) -> str | None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_AWARENESS_PATH_CHARS
    ):
        return None
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path.as_posix()


__all__ = [
    "GitWorkspaceChangeObserver",
    "WorkspaceAwarenessState",
    "WorkspaceChangeFacts",
    "WorkspaceChangeObserver",
    "WorkspaceSnapshot",
    "build_workspace_change_facts",
]
