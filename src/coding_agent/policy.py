"""Pure deterministic Explicit Constraint and Risk Permission checks."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .constraints import (
    ConstraintCheckResult,
    ExplicitConstraintSnapshot,
    check_explicit_constraints,
)
from .protocol import ToolCapability
from .shell import ShellOperationFacts, ShellRiskAction
from .tooling import PreparedToolCall
from .workspace import FileOperationFacts, FileOperationKind


class PermissionDecision(StrEnum):
    """The three deterministic v1 Risk Permission outcomes."""

    ALLOW = "ALLOW"
    CONFIRM = "CONFIRM"
    DENY = "DENY"


@dataclass(frozen=True, slots=True)
class PermissionCheckResult:
    """Decision plus structured rule facts for Runtime and UI consumers."""

    decision: PermissionDecision
    reason_code: str | None = None
    message: str | None = None
    risk_summary: str | None = None
    matched_rules: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "matched_rules", tuple(self.matched_rules))
        has_reason = any(
            value is not None
            for value in (self.reason_code, self.message, self.risk_summary)
        )
        if self.decision is PermissionDecision.ALLOW and (
            has_reason or self.matched_rules
        ):
            raise ValueError("ALLOW permission result must not contain risk reasons")
        if self.decision is not PermissionDecision.ALLOW and (
            self.reason_code is None
            or self.message is None
            or self.risk_summary is None
        ):
            raise ValueError("CONFIRM and DENY results require structured reasons")


_DENY_SHELL_ACTIONS = frozenset(
    {
        ShellRiskAction.PRIVILEGE_ESCALATION,
        ShellRiskAction.SYSTEM_CONFIGURATION,
        ShellRiskAction.SHUTDOWN_OR_REBOOT,
        ShellRiskAction.BACKGROUND_OR_DETACHED_PROCESS,
        ShellRiskAction.INTERACTIVE_COMMAND,
    }
)
_CONFIRM_SHELL_ACTIONS = frozenset(
    {
        ShellRiskAction.DEPENDENCY_INSTALL,
        ShellRiskAction.NETWORK_ACCESS,
        ShellRiskAction.GIT_MUTATION,
        ShellRiskAction.GIT_REMOTE_WRITE,
        ShellRiskAction.FILE_DELETION,
    }
)


class PolicyEngine:
    """Stateless policy service; it never executes or interacts with users."""

    def check_explicit_constraints(
        self,
        prepared_call: PreparedToolCall,
        snapshot: ExplicitConstraintSnapshot,
    ) -> ConstraintCheckResult:
        """Return PASS/REJECT without mutating the supplied Task State."""
        return check_explicit_constraints(prepared_call, snapshot)

    def check_risk_permission(
        self,
        prepared_call: PreparedToolCall,
    ) -> PermissionCheckResult:
        """Return ALLOW/CONFIRM/DENY from prepared deterministic facts."""
        facts = prepared_call.operation_facts
        if isinstance(facts, FileOperationFacts):
            return self._check_file(prepared_call, facts)
        if isinstance(facts, ShellOperationFacts):
            return self._check_shell(prepared_call, facts)
        return self._result(
            PermissionDecision.DENY,
            "UNSUPPORTED_OPERATION_FACTS",
            "local action has no supported deterministic policy facts",
            "Runtime cannot safely evaluate this local action",
            ("UNSUPPORTED_OPERATION_FACTS",),
        )

    @staticmethod
    def _check_file(
        prepared_call: PreparedToolCall,
        facts: FileOperationFacts,
    ) -> PermissionCheckResult:
        target = facts.target
        affected_paths = facts.affected_paths or (target,)
        capabilities = prepared_call.tool_identity.capabilities
        if any(not path.is_within_workspace for path in affected_paths):
            return PolicyEngine._result(
                PermissionDecision.DENY,
                "WORKSPACE_BOUNDARY",
                "File Tool access outside the workspace is prohibited",
                "Target resolves outside the bound workspace",
                ("WORKSPACE_BOUNDARY",),
            )
        if (
            any(path.is_protected for path in affected_paths)
            and ToolCapability.FILE_MUTATION in capabilities
        ):
            return PolicyEngine._result(
                PermissionDecision.DENY,
                "PROTECTED_PATH_MUTATION",
                "File Tool mutation of protected paths is prohibited",
                "Target is inside protected .git internals",
                ("PROTECTED_PATH", "FILE_MUTATION"),
            )
        if facts.operation is FileOperationKind.DELETE and (
            target.workspace_relative_path == "." or facts.directory_nonempty
        ):
            return PolicyEngine._result(
                PermissionDecision.DENY,
                "RECURSIVE_DELETE_DENIED",
                "delete_path cannot delete the workspace root or a non-empty directory",
                "Recursive or workspace-root deletion is outside the Tool contract",
                ("FILE_MUTATION", "DELETE", "RECURSIVE_DELETE_DENIED"),
            )
        if any(path.is_sensitive for path in affected_paths):
            return PolicyEngine._result(
                PermissionDecision.CONFIRM,
                "SENSITIVE_PATH_CONFIRMATION",
                "Sensitive Path access requires explicit user confirmation",
                "Sensitive content may be read, created, or modified",
                ("SENSITIVE_PATH",),
            )
        if facts.operation is FileOperationKind.DELETE:
            return PolicyEngine._result(
                PermissionDecision.CONFIRM,
                "FILE_DELETE_CONFIRMATION",
                "Deleting this exact path requires explicit user confirmation",
                "One regular file or empty directory will be removed",
                ("FILE_MUTATION", "DELETE"),
            )
        if target.is_protected and ToolCapability.FILE_READ in capabilities:
            return PolicyEngine._result(
                PermissionDecision.CONFIRM,
                "PROTECTED_PATH_READ_CONFIRMATION",
                "Reading protected .git internals requires confirmation",
                "Target is inside protected .git internals",
                ("PROTECTED_PATH", "FILE_READ"),
            )
        return PermissionCheckResult(decision=PermissionDecision.ALLOW)

    @staticmethod
    def _check_shell(
        prepared_call: PreparedToolCall,
        facts: ShellOperationFacts,
    ) -> PermissionCheckResult:
        if ToolCapability.COMMAND_EXECUTION not in prepared_call.tool_identity.capabilities:
            return PolicyEngine._result(
                PermissionDecision.DENY,
                "INVALID_SHELL_CAPABILITY",
                "Shell facts require COMMAND_EXECUTION capability",
                "Prepared action has inconsistent static and dynamic facts",
                ("INVALID_SHELL_CAPABILITY",),
            )
        if not facts.cwd.is_within_workspace:
            return PolicyEngine._result(
                PermissionDecision.DENY,
                "WORKSPACE_BOUNDARY",
                "Shell cwd must resolve inside the workspace",
                "Requested Shell working directory is outside the workspace",
                ("WORKSPACE_BOUNDARY",),
            )

        actions = facts.surface_facts.recognized_actions
        deny_actions = actions & _DENY_SHELL_ACTIONS
        if deny_actions:
            matched = tuple(sorted(action.value for action in deny_actions))
            return PolicyEngine._result(
                PermissionDecision.DENY,
                "SHELL_ACTION_DENIED",
                "Shell command contains a v1-prohibited recognizable action",
                f"Denied Shell actions: {', '.join(matched)}",
                matched,
            )
        confirm_actions = actions & _CONFIRM_SHELL_ACTIONS
        if confirm_actions:
            matched = tuple(sorted(action.value for action in confirm_actions))
            return PolicyEngine._result(
                PermissionDecision.CONFIRM,
                "SHELL_ACTION_CONFIRMATION",
                "Shell command contains a recognizable high-risk action",
                f"Shell actions requiring confirmation: {', '.join(matched)}",
                matched,
            )
        if (
            facts.surface_facts.has_compound_syntax
            and facts.surface_facts.has_unknown_segment
        ):
            return PolicyEngine._result(
                PermissionDecision.CONFIRM,
                "AMBIGUOUS_COMPLEX_SHELL",
                "Complex Shell composition could not be reliably classified",
                "Compound command contains an unknown or ambiguous segment",
                ("COMPOUND_SYNTAX", "UNKNOWN_SEGMENT"),
            )
        return PermissionCheckResult(decision=PermissionDecision.ALLOW)

    @staticmethod
    def _result(
        decision: PermissionDecision,
        reason_code: str,
        message: str,
        risk_summary: str,
        matched_rules: tuple[str, ...],
    ) -> PermissionCheckResult:
        return PermissionCheckResult(
            decision=decision,
            reason_code=reason_code,
            message=message,
            risk_summary=risk_summary,
            matched_rules=matched_rules,
        )


__all__ = [
    "PermissionCheckResult",
    "PermissionDecision",
    "PolicyEngine",
]
