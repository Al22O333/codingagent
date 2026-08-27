"""Typed tool metadata, argument validation, and registry lookup."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Generic, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, ConfigDict

from coding_agent.protocol import (
    ToolCapability,
    ToolError,
    ToolKind,
    ToolOutcome,
    ToolSpec,
)


class ToolArguments(BaseModel):
    """Strict, immutable base model for model-provided tool arguments."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


ArgumentsT = TypeVar("ArgumentsT", bound=ToolArguments)


@dataclass(frozen=True, slots=True)
class Tool(Generic[ArgumentsT]):
    """A tool's typed argument contract and provider-neutral metadata."""

    name: str
    description: str
    argument_model: type[ArgumentsT]
    kind: ToolKind
    capabilities: frozenset[ToolCapability] = frozenset()

    def __post_init__(self) -> None:
        if not isinstance(self.argument_model, type) or not issubclass(
            self.argument_model, ToolArguments
        ):
            raise TypeError("argument_model must inherit from ToolArguments")
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))

    @property
    def spec(self) -> ToolSpec:
        """Generate the model schema and runtime metadata from one typed model."""
        return ToolSpec(
            name=self.name,
            description=self.description,
            input_schema=self.argument_model.model_json_schema(),
            kind=self.kind,
            capabilities=self.capabilities,
        )

    def validate(self, raw_arguments: object) -> ArgumentsT:
        """Validate untrusted model arguments into a typed immutable value."""
        return self.argument_model.model_validate(_validation_input(raw_arguments))


def _validation_input(value: object) -> object:
    """Copy immutable protocol containers into ordinary JSON-like containers."""
    if isinstance(value, Mapping):
        return {key: _validation_input(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_validation_input(item) for item in value]
    if isinstance(value, frozenset):
        return [_validation_input(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    """Execution-level result returned by a LOCAL Tool."""

    outcome: ToolOutcome
    content: object | None = None
    error: ToolError | None = None

    def __post_init__(self) -> None:
        allowed_outcomes = {
            ToolOutcome.SUCCESS,
            ToolOutcome.OPERATION_FAILURE,
            ToolOutcome.UNSUCCESSFUL_COMMAND,
        }
        if self.outcome not in allowed_outcomes:
            raise ValueError("invalid LOCAL Tool execution outcome")
        if self.outcome is ToolOutcome.SUCCESS and self.error is not None:
            raise ValueError("successful execution must not contain ToolError")
        if self.outcome is ToolOutcome.OPERATION_FAILURE and self.error is None:
            raise ValueError("operation failure must contain ToolError")


@runtime_checkable
class LocalTool(Protocol):
    """Minimal executable LOCAL Tool seam used by AgentRuntime."""

    @property
    def spec(self) -> ToolSpec:
        """Return provider-neutral metadata."""
        ...

    def validate(self, raw_arguments: object) -> Any:
        """Validate untrusted arguments."""
        ...

    def prepare(self, arguments: Any) -> object | ToolError:
        """Return dynamic operation facts or an expected preparation error."""
        ...

    def execute(
        self,
        arguments: Any,
        prepared: object,
    ) -> ToolExecutionResult:
        """Execute an already prepared and permitted local action."""
        ...


class ToolRegistryError(ValueError):
    """Base class for tool registry configuration and lookup failures."""


class InvalidToolSpecError(ToolRegistryError):
    """Raised when a tool cannot expose a valid v1 ToolSpec."""


class DuplicateToolNameError(ToolRegistryError):
    """Raised when two tools register the same name."""


class UnknownToolError(ToolRegistryError):
    """Raised when lookup cannot find a registered tool name."""


class ToolRegistry:
    """Thin startup registry for tool lookup and ToolSpec enumeration."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool[Any]] = {}

    def register(self, tool: Tool[Any]) -> None:
        """Validate and register one tool, failing fast on configuration errors."""
        if not isinstance(tool, Tool):
            raise InvalidToolSpecError("registered object must be a Tool")

        try:
            spec = tool.spec
        except (TypeError, ValueError) as error:
            raise InvalidToolSpecError(f"invalid ToolSpec: {error}") from error

        self._validate_spec(spec)
        if spec.name in self._tools:
            raise DuplicateToolNameError(
                f"tool name is already registered: {spec.name}"
            )
        self._tools[spec.name] = tool

    def get(self, name: str) -> Tool[Any]:
        """Return a registered tool or report a call-level unknown tool."""
        try:
            return self._tools[name]
        except KeyError as error:
            raise UnknownToolError(f"unknown tool: {name}") from error

    def specs(self) -> tuple[ToolSpec, ...]:
        """Return specs in deterministic registration order."""
        return tuple(tool.spec for tool in self._tools.values())

    @staticmethod
    def _validate_spec(spec: ToolSpec) -> None:
        if not isinstance(spec, ToolSpec):
            raise InvalidToolSpecError("tool.spec must be a ToolSpec")
        if not isinstance(spec.kind, ToolKind):
            raise InvalidToolSpecError("ToolSpec.kind must be a ToolKind")
        if not isinstance(spec.input_schema, Mapping):
            raise InvalidToolSpecError("ToolSpec.input_schema must be a mapping")
        if spec.input_schema.get("type") != "object":
            raise InvalidToolSpecError(
                "ToolSpec.input_schema must describe an object"
            )
        if any(
            not isinstance(capability, ToolCapability)
            for capability in spec.capabilities
        ):
            raise InvalidToolSpecError(
                "ToolSpec capabilities must be ToolCapability values"
            )


__all__ = [
    "DuplicateToolNameError",
    "InvalidToolSpecError",
    "LocalTool",
    "Tool",
    "ToolArguments",
    "ToolExecutionResult",
    "ToolRegistry",
    "ToolRegistryError",
    "UnknownToolError",
]
