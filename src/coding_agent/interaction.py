"""Runtime-facing user interaction contracts and deterministic test fake."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class ConfirmationDecision(StrEnum):
    """Possible user decisions for one exact-action confirmation."""

    APPROVE = "APPROVE"
    REJECT = "REJECT"
    CANCEL = "CANCEL"


@dataclass(frozen=True, slots=True)
class ConfirmationRequest:
    """Display-only facts describing the exact action awaiting a decision."""

    call_id: str
    tool_name: str
    action_summary: str
    reason_code: str
    risk_summary: str


class UserInteractionError(RuntimeError):
    """A terminal failure of the user interaction channel."""


class UserInteraction(Protocol):
    """Runtime-facing boundary for permission confirmation."""

    def confirm(self, request: ConfirmationRequest) -> ConfirmationDecision:
        """Return the user's decision for one exact prepared action."""
        ...


class FakeUserInteraction:
    """Return a predetermined confirmation sequence for runtime tests."""

    def __init__(self, decisions: Iterable[ConfirmationDecision] = ()) -> None:
        self._decisions = list(decisions)
        self.requests: list[ConfirmationRequest] = []

    def confirm(self, request: ConfirmationRequest) -> ConfirmationDecision:
        self.requests.append(request)
        if not self._decisions:
            raise UserInteractionError("no fake confirmation decision remains")
        return self._decisions.pop(0)


__all__ = [
    "ConfirmationDecision",
    "ConfirmationRequest",
    "FakeUserInteraction",
    "UserInteraction",
    "UserInteractionError",
]
