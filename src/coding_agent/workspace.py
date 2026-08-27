"""Shared canonical workspace path-resolution primitive."""

from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class PathResolutionMode(StrEnum):
    """Resolution semantics for existing and candidate new targets."""

    EXISTING = "EXISTING"
    NEW = "NEW"


@dataclass(frozen=True, slots=True)
class ResolvedPath:
    """Immutable filesystem and classification facts for one requested path."""

    raw_path: str
    resolved_path: Path
    exists: bool
    is_within_workspace: bool
    workspace_relative_path: str | None
    is_sensitive: bool
    is_protected: bool

    def __post_init__(self) -> None:
        has_relative_path = self.workspace_relative_path is not None
        if self.is_within_workspace != has_relative_path:
            raise ValueError(
                "workspace_relative_path must be present exactly for inside paths"
            )


class WorkspacePathResolver:
    """Bind one canonical workspace root and resolve all File Tool paths."""

    def __init__(self, workspace_root: str | Path) -> None:
        requested_root = Path(workspace_root)
        if not requested_root.exists():
            raise FileNotFoundError(f"workspace root does not exist: {requested_root}")
        if not requested_root.is_dir():
            raise NotADirectoryError(
                f"workspace root is not a directory: {requested_root}"
            )
        self._workspace_root = requested_root.resolve(strict=True)

    @property
    def workspace_root(self) -> Path:
        """The stable canonical workspace root."""
        return self._workspace_root

    def resolve_workspace_path(
        self,
        raw_path: str,
        mode: PathResolutionMode,
    ) -> ResolvedPath:
        """Resolve a requested path into facts without making a policy decision."""
        requested_path = Path(raw_path)
        bound_path = (
            requested_path
            if requested_path.is_absolute()
            else self._workspace_root / requested_path
        )

        if mode is PathResolutionMode.EXISTING:
            resolved_path = bound_path.resolve(strict=True)
            exists = True
        elif mode is PathResolutionMode.NEW:
            resolved_path = self._resolve_new_path(bound_path)
            exists = os.path.lexists(bound_path)
        else:
            raise ValueError(f"unsupported path resolution mode: {mode!r}")

        workspace_relative_path = self._workspace_relative_path(resolved_path)
        is_within_workspace = workspace_relative_path is not None
        classification_paths = self._classification_paths(
            bound_path,
            resolved_path,
            workspace_relative_path,
        )

        return ResolvedPath(
            raw_path=raw_path,
            resolved_path=resolved_path,
            exists=exists,
            is_within_workspace=is_within_workspace,
            workspace_relative_path=workspace_relative_path,
            is_sensitive=any(
                self._path_is_sensitive(path) for path in classification_paths
            ),
            is_protected=any(
                self._path_is_protected(path) for path in classification_paths
            ),
        )

    def _resolve_new_path(self, bound_path: Path) -> Path:
        probe = bound_path
        suffix: deque[str] = deque()

        while not os.path.lexists(probe):
            parent = probe.parent
            if parent == probe:
                raise FileNotFoundError(
                    f"new target has no existing parent: {bound_path}"
                )
            suffix.appendleft(probe.name)
            probe = parent

        resolved_parent = probe.resolve(strict=True)
        if suffix and not resolved_parent.is_dir():
            raise NotADirectoryError(
                f"new target parent is not a directory: {probe}"
            )

        candidate = resolved_parent
        for component in suffix:
            if component in ("", "."):
                continue
            if component == "..":
                candidate = candidate.parent
            else:
                candidate = candidate / component
        return candidate

    def _workspace_relative_path(self, path: Path) -> str | None:
        try:
            relative_path = path.relative_to(self._workspace_root)
        except ValueError:
            return None
        return relative_path.as_posix() or "."

    def _classification_paths(
        self,
        bound_path: Path,
        resolved_path: Path,
        workspace_relative_path: str | None,
    ) -> tuple[Path, ...]:
        paths: list[Path] = []
        if workspace_relative_path is not None:
            paths.append(Path(workspace_relative_path))
        else:
            paths.append(Path(resolved_path.name))

        lexical_path = Path(os.path.normpath(bound_path))
        lexical_relative = self._workspace_relative_path(lexical_path)
        if lexical_relative is not None:
            paths.append(Path(lexical_relative))
        else:
            paths.append(Path(lexical_path.name))
        return tuple(paths)

    @staticmethod
    def _path_is_protected(path: Path) -> bool:
        return any(component.casefold() == ".git" for component in path.parts)

    @staticmethod
    def _path_is_sensitive(path: Path) -> bool:
        return any(
            WorkspacePathResolver._component_is_sensitive(component)
            for component in path.parts
        )

    @staticmethod
    def _component_is_sensitive(component: str) -> bool:
        name = component.casefold()
        return (
            name == ".env"
            or name.startswith(".env.")
            or name.endswith((".pem", ".key", ".p12", ".pfx"))
            or name in {"id_rsa", "id_ed25519"}
            or name.startswith("credentials")
        )


__all__ = ["PathResolutionMode", "ResolvedPath", "WorkspacePathResolver"]
