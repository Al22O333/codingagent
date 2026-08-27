"""Closed deterministic v1 Explicit Task Constraint handling."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .protocol import ToolCapability
from .tooling import PreparedToolCall
from .workspace import (
    FileOperationFacts,
    PathResolutionMode,
    ResolvedPath,
    WorkspacePathResolver,
)


class ConstraintDecision(StrEnum):
    """Explicit Task Constraint decisions never include confirmation."""

    PASS = "PASS"
    REJECT = "REJECT"


@dataclass(frozen=True, slots=True)
class ExplicitConstraintSnapshot:
    """Immutable normalized constraints for one current Agent Run."""

    forbid_file_mutation: bool = False
    forbid_command_execution: bool = False
    write_scopes: tuple[ResolvedPath, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "write_scopes", tuple(self.write_scopes))


@dataclass(frozen=True, slots=True)
class ExplicitConstraintUpdate:
    """Fields set by one trusted user input; None means no update."""

    forbid_file_mutation: bool | None = None
    forbid_command_execution: bool | None = None
    write_scopes: tuple[ResolvedPath, ...] | None = None

    def __post_init__(self) -> None:
        if self.write_scopes is not None:
            object.__setattr__(self, "write_scopes", tuple(self.write_scopes))


@dataclass(frozen=True, slots=True)
class ConstraintCheckResult:
    """Structured deterministic result consumed by AgentRuntime."""

    decision: ConstraintDecision
    reason_code: str | None = None
    message: str | None = None

    def __post_init__(self) -> None:
        has_reason = self.reason_code is not None or self.message is not None
        if self.decision is ConstraintDecision.PASS and has_reason:
            raise ValueError("PASS constraint result must not contain a reason")
        if self.decision is ConstraintDecision.REJECT and (
            self.reason_code is None or self.message is None
        ):
            raise ValueError("REJECT constraint result requires a reason")


_WRITE_SCOPE_PATTERNS = (
    re.compile(r"只(?:能)?修改\s*[`'\"]?([^\s，。；;`'\"]+)[`'\"]?", re.IGNORECASE),
    re.compile(
        r"only\s+modify\s+(?:files\s+(?:under|in)\s+)?[`'\"]?([^\s,.;`'\"]+)[`'\"]?",
        re.IGNORECASE,
    ),
)
_FORBID_MUTATION = (
    "不要修改文件",
    "请勿修改文件",
    "不可以修改文件",
    "不允许修改文件",
    "只读，不要修改",
    "do not modify files",
    "don't modify files",
    "no file changes",
)
_ALLOW_MUTATION = (
    "可以修改文件",
    "允许修改文件",
    "file changes are allowed",
    "you may modify files",
)
_FORBID_COMMAND = (
    "不要运行命令",
    "请勿运行命令",
    "不可以运行命令",
    "不允许运行命令",
    "do not run commands",
    "don't run commands",
    "no command execution",
)
_ALLOW_COMMAND = (
    "可以运行命令",
    "允许运行命令",
    "command execution is allowed",
    "you may run commands",
)
_CLEAR_WRITE_SCOPE = (
    "不再限制修改范围",
    "取消修改范围限制",
    "remove the write scope restriction",
    "write scope is unrestricted",
)


def normalize_explicit_constraint_update(
    user_input: str,
    resolver: WorkspacePathResolver | None,
) -> ExplicitConstraintUpdate | None:
    """Normalize only the small, explicit v1 vocabulary from trusted input."""
    folded = user_input.casefold()
    mutation_toggle = _closed_toggle(folded, _FORBID_MUTATION, _ALLOW_MUTATION)
    command_toggle = _closed_toggle(folded, _FORBID_COMMAND, _ALLOW_COMMAND)
    if mutation_toggle is _AMBIGUOUS or command_toggle is _AMBIGUOUS:
        return None

    write_scope: tuple[ResolvedPath, ...] | None = None
    clear_scope = any(phrase in folded for phrase in _CLEAR_WRITE_SCOPE)
    scope_matches = [
        match.group(1)
        for pattern in _WRITE_SCOPE_PATTERNS
        if (match := pattern.search(user_input)) is not None
    ]
    if clear_scope and scope_matches:
        return None
    if len(set(scope_matches)) > 1:
        return None
    if clear_scope:
        write_scope = ()
    elif scope_matches:
        if resolver is None:
            return None
        resolved_scope = _resolve_write_scope(scope_matches[0], resolver)
        if resolved_scope is None:
            return None
        write_scope = (resolved_scope,)

    file_update = mutation_toggle if isinstance(mutation_toggle, bool) else None
    command_update = command_toggle if isinstance(command_toggle, bool) else None
    if file_update is None and command_update is None and write_scope is None:
        return None
    return ExplicitConstraintUpdate(
        forbid_file_mutation=file_update,
        forbid_command_execution=command_update,
        write_scopes=write_scope,
    )


def apply_constraint_update(
    snapshot: ExplicitConstraintSnapshot,
    update: ExplicitConstraintUpdate,
) -> ExplicitConstraintSnapshot:
    """Return the next immutable constraint snapshot."""
    return ExplicitConstraintSnapshot(
        forbid_file_mutation=(
            snapshot.forbid_file_mutation
            if update.forbid_file_mutation is None
            else update.forbid_file_mutation
        ),
        forbid_command_execution=(
            snapshot.forbid_command_execution
            if update.forbid_command_execution is None
            else update.forbid_command_execution
        ),
        write_scopes=(
            snapshot.write_scopes
            if update.write_scopes is None
            else update.write_scopes
        ),
    )


def check_explicit_constraints(
    prepared_call: PreparedToolCall,
    snapshot: ExplicitConstraintSnapshot,
) -> ConstraintCheckResult:
    """Check one prepared local action against normalized hard constraints."""
    capabilities = prepared_call.tool_identity.capabilities
    if (
        ToolCapability.FILE_MUTATION in capabilities
        and snapshot.forbid_file_mutation
    ):
        return _reject(
            "FORBID_FILE_MUTATION",
            "user explicitly prohibited file mutation",
        )
    if (
        ToolCapability.COMMAND_EXECUTION in capabilities
        and snapshot.forbid_command_execution
    ):
        return _reject(
            "FORBID_COMMAND_EXECUTION",
            "user explicitly prohibited command execution",
        )
    if ToolCapability.FILE_MUTATION in capabilities and snapshot.write_scopes:
        facts = prepared_call.operation_facts
        if not isinstance(facts, FileOperationFacts) or not facts.affected_paths:
            return _reject(
                "WRITE_SCOPE_UNENFORCEABLE",
                "file mutation does not expose enforceable affected paths",
            )
        for affected_path in facts.affected_paths:
            if not affected_path.is_within_workspace or not any(
                _path_is_within(affected_path.resolved_path, scope.resolved_path)
                for scope in snapshot.write_scopes
            ):
                return _reject(
                    "WRITE_SCOPE",
                    "file mutation is outside the user-authorized write scope",
                )
    return ConstraintCheckResult(decision=ConstraintDecision.PASS)


class _Ambiguous:
    pass


_AMBIGUOUS = _Ambiguous()


def _closed_toggle(
    text: str,
    set_phrases: tuple[str, ...],
    clear_phrases: tuple[str, ...],
) -> bool | _Ambiguous | None:
    set_constraint = any(phrase in text for phrase in set_phrases)
    clear_constraint = any(
        re.search(rf"(?<!不){re.escape(phrase)}", text) is not None
        for phrase in clear_phrases
    )
    if set_constraint and clear_constraint:
        return _AMBIGUOUS
    if set_constraint:
        return True
    if clear_constraint:
        return False
    return None


def _resolve_write_scope(
    raw_scope: str,
    resolver: WorkspacePathResolver,
) -> ResolvedPath | None:
    if any(character in raw_scope for character in "*?[]{}"):
        return None
    path = Path(raw_scope)
    if path.is_absolute():
        return None
    try:
        resolved = resolver.resolve_workspace_path(raw_scope, PathResolutionMode.NEW)
    except (FileNotFoundError, NotADirectoryError, OSError, ValueError):
        return None
    if not resolved.is_within_workspace:
        return None
    return resolved


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _reject(reason_code: str, message: str) -> ConstraintCheckResult:
    return ConstraintCheckResult(
        decision=ConstraintDecision.REJECT,
        reason_code=reason_code,
        message=message,
    )


__all__ = [
    "ConstraintCheckResult",
    "ConstraintDecision",
    "ExplicitConstraintSnapshot",
    "ExplicitConstraintUpdate",
    "apply_constraint_update",
    "check_explicit_constraints",
    "normalize_explicit_constraint_update",
]
