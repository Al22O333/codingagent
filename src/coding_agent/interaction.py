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


class ClarificationStatus(StrEnum):
    ANSWERED = "ANSWERED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class ConfirmationRequest:
    """Display-only facts describing the exact action awaiting a decision."""

    call_id: str
    tool_name: str
    action_summary: str
    reason_code: str
    risk_summary: str


@dataclass(frozen=True, slots=True)
class ClarificationRequest:
    call_id: str
    question: str


@dataclass(frozen=True, slots=True)
class ClarificationResponse:
    status: ClarificationStatus
    answer: str | None = None

    def __post_init__(self) -> None:
        if self.status is ClarificationStatus.ANSWERED and self.answer is None:
            raise ValueError("ANSWERED clarification requires an answer")
        if self.status is ClarificationStatus.CANCELLED and self.answer is not None:
            raise ValueError("CANCELLED clarification must not contain an answer")


class UserInteractionError(RuntimeError):
    """A terminal failure of the user interaction channel."""


class UserInteraction(Protocol):
    """Runtime-facing boundary for permission confirmation."""

    def confirm(self, request: ConfirmationRequest) -> ConfirmationDecision:
        """Return the user's decision for one exact prepared action."""
        ...

    def ask(self, request: ClarificationRequest) -> ClarificationResponse:
        """Return an answer or cancellation for one same-Run clarification."""
        ...


class FakeUserInteraction:
    """Return a predetermined confirmation sequence for runtime tests."""

    def __init__(
        self,
        decisions: Iterable[ConfirmationDecision] = (),
        answers: Iterable[ClarificationResponse] = (),
    ) -> None:
        self._decisions = list(decisions)
        self._answers = list(answers)
        self.requests: list[ConfirmationRequest] = []
        self.clarification_requests: list[ClarificationRequest] = []

    def confirm(self, request: ConfirmationRequest) -> ConfirmationDecision:
        self.requests.append(request)
        if not self._decisions:
            raise UserInteractionError("no fake confirmation decision remains")
        return self._decisions.pop(0)

    def ask(self, request: ClarificationRequest) -> ClarificationResponse:
        self.clarification_requests.append(request)
        if not self._answers:
            raise UserInteractionError("no fake clarification response remains")
        return self._answers.pop(0)


__all__ = [
    "ConfirmationDecision",
    "ConfirmationRequest",
    "ClarificationRequest",
    "ClarificationResponse",
    "ClarificationStatus",
    "FakeUserInteraction",
    "UserInteraction",
    "UserInteractionError",
]
