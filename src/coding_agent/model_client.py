"""Runtime-facing model client seam and deterministic test fake."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from typing import Protocol, TypeAlias, runtime_checkable

from coding_agent.protocol import ModelRequest, ModelResponse


@runtime_checkable
class ModelClient(Protocol):
    """Provider-neutral, non-streaming model completion interface."""

    def complete(self, request: ModelRequest) -> ModelResponse:
        """Return one complete normalized assistant response."""
        ...


class TransientProviderError(RuntimeError):
    """A provider failure eligible for bounded Transport Retry."""


class FatalProviderError(RuntimeError):
    """A non-retryable provider or configuration failure."""


class ModelProtocolError(ValueError):
    """An obtained assistant response cannot be reliably normalized."""


class FakeModelExhaustedError(AssertionError):
    """Raised when a test asks for more responses than it configured."""


FakeModelEvent: TypeAlias = ModelResponse | BaseException


class FakeModelClient:
    """Return a predetermined response/exception sequence for deterministic tests."""

    def __init__(self, events: Iterable[FakeModelEvent]) -> None:
        self._events = deque(events)
        self._requests: list[ModelRequest] = []

    @property
    def requests(self) -> tuple[ModelRequest, ...]:
        """Requests observed so far, in call order."""
        return tuple(self._requests)

    @property
    def remaining_events(self) -> int:
        """Number of configured events that have not been consumed."""
        return len(self._events)

    def complete(self, request: ModelRequest) -> ModelResponse:
        """Record the request and consume exactly one configured event."""
        self._requests.append(request)
        if not self._events:
            raise FakeModelExhaustedError(
                "FakeModelClient has no configured event for this request"
            )

        event = self._events.popleft()
        if isinstance(event, BaseException):
            raise event
        return event


__all__ = [
    "FakeModelClient",
    "FakeModelEvent",
    "FakeModelExhaustedError",
    "FatalProviderError",
    "ModelProtocolError",
    "ModelClient",
    "TransientProviderError",
]
