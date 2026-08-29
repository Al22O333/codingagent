"""Provider-neutral protocol value objects defined by architecture document 05."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import TypeAlias, cast


class ToolOutcome(StrEnum):
    """Runtime-level outcome categories for tool calls."""

    SUCCESS = "SUCCESS"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    POLICY_REJECTED = "POLICY_REJECTED"
    OPERATION_FAILURE = "OPERATION_FAILURE"
    UNSUCCESSFUL_COMMAND = "UNSUCCESSFUL_COMMAND"
    NOT_EXECUTED = "NOT_EXECUTED"


class ToolKind(StrEnum):
    """The two dispatch paths supported by the runtime."""

    LOCAL = "LOCAL"
    INTERACTION = "INTERACTION"


class ToolCapability(StrEnum):
    """Minimal static capabilities needed for hard constraints."""

    FILE_READ = "FILE_READ"
    FILE_MUTATION = "FILE_MUTATION"
    COMMAND_EXECUTION = "COMMAND_EXECUTION"


def _require_non_empty(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")


def _freeze(value: object) -> object:
    """Copy common container values into immutable protocol representations."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A normalized but not yet argument-validated model tool call."""

    call_id: str
    name: str
    raw_arguments: object

    def __post_init__(self) -> None:
        _require_non_empty(self.call_id, "call_id")
        _require_non_empty(self.name, "name")
        object.__setattr__(self, "raw_arguments", _freeze(self.raw_arguments))


@dataclass(frozen=True, slots=True)
class ToolError:
    """Tool- or policy-specific error information."""

    code: str
    message: str
    details: object | None = None

    def __post_init__(self) -> None:
        _require_non_empty(self.code, "code")
        _require_non_empty(self.message, "message")
        object.__setattr__(self, "details", _freeze(self.details))


@dataclass(frozen=True, slots=True)
class ToolResult:
    """The complete runtime-level result corresponding to one tool call."""

    call_id: str
    tool_name: str
    outcome: ToolOutcome
    content: object | None = None
    error: ToolError | None = None

    def __post_init__(self) -> None:
        _require_non_empty(self.call_id, "call_id")
        _require_non_empty(self.tool_name, "tool_name")
        object.__setattr__(self, "content", _freeze(self.content))


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Provider-neutral model-visible tool schema plus runtime metadata."""

    name: str
    description: str
    input_schema: Mapping[str, object]
    kind: ToolKind
    capabilities: frozenset[ToolCapability]

    def __post_init__(self) -> None:
        _require_non_empty(self.name, "name")
        _require_non_empty(self.description, "description")
        frozen_schema = _freeze(self.input_schema)
        object.__setattr__(
            self,
            "input_schema",
            cast(Mapping[str, object], frozen_schema),
        )
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))


@dataclass(frozen=True, slots=True)
class SystemMessage:
    """Provider-neutral system instruction message."""

    text: str


@dataclass(frozen=True, slots=True)
class UserMessage:
    """Provider-neutral user message."""

    text: str


@dataclass(frozen=True, slots=True)
class RuntimeInstructionMessage:
    """Request-local Runtime control instruction, never user-authored history."""

    text: str


@dataclass(frozen=True, slots=True)
class AssistantMessage:
    """Provider-neutral assistant message, optionally containing tool calls."""

    text: str | None
    tool_calls: tuple[ToolCall, ...] = ()
    provider_reasoning_content: str | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_calls", tuple(self.tool_calls))
        if (
            self.provider_reasoning_content is not None
            and not isinstance(self.provider_reasoning_content, str)
        ):
            raise TypeError("provider_reasoning_content must be text or None")


@dataclass(frozen=True, slots=True)
class ToolResultMessage:
    """Provider-neutral collection of results for an assistant tool turn."""

    results: tuple[ToolResult, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "results", tuple(self.results))


InternalMessage: TypeAlias = (
    SystemMessage
    | UserMessage
    | RuntimeInstructionMessage
    | AssistantMessage
    | ToolResultMessage
)


@dataclass(frozen=True, slots=True)
class ModelUsage:
    """Optional provider usage metadata."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None

    def __post_init__(self) -> None:
        for field_name in ("input_tokens", "output_tokens", "total_tokens"):
            value = getattr(self, field_name)
            if value is not None and value < 0:
                raise ValueError(f"{field_name} must not be negative")


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """Provider-neutral request passed to a future ModelClient."""

    messages: tuple[InternalMessage, ...]
    tools: tuple[ToolSpec, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "tools", tuple(self.tools))


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """A successfully normalized provider-neutral assistant response."""

    text: str | None
    tool_calls: tuple[ToolCall, ...] = ()
    usage: ModelUsage | None = None
    provider_reasoning_content: str | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_calls", tuple(self.tool_calls))
        if (
            self.provider_reasoning_content is not None
            and not isinstance(self.provider_reasoning_content, str)
        ):
            raise TypeError("provider_reasoning_content must be text or None")


__all__ = [
    "AssistantMessage",
    "InternalMessage",
    "ModelRequest",
    "ModelResponse",
    "ModelUsage",
    "RuntimeInstructionMessage",
    "SystemMessage",
    "ToolCall",
    "ToolCapability",
    "ToolError",
    "ToolKind",
    "ToolOutcome",
    "ToolResult",
    "ToolResultMessage",
    "ToolSpec",
    "UserMessage",
]
