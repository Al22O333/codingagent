"""Unit tests for the Step 2 tool abstraction and registry."""

from typing import cast

import pytest
from pydantic import ValidationError

from coding_agent.protocol import ToolCall, ToolCapability, ToolKind
from coding_agent.tooling import (
    DuplicateToolNameError,
    InvalidToolSpecError,
    Tool,
    ToolArguments,
    ToolRegistry,
    UnknownToolError,
)


class DummyArguments(ToolArguments):
    path: str
    count: int = 1


class DummyTool(Tool[DummyArguments]):
    def __init__(
        self,
        name: str = "dummy",
        *,
        kind: ToolKind = ToolKind.LOCAL,
        capabilities: frozenset[ToolCapability] = frozenset(
            {ToolCapability.FILE_READ}
        ),
    ) -> None:
        super().__init__(
            name=name,
            description="A test-only dummy tool",
            argument_model=DummyArguments,
            kind=kind,
            capabilities=capabilities,
        )


def test_dummy_tool_validates_arguments_and_generates_json_schema() -> None:
    tool = DummyTool()

    arguments = tool.validate({"path": "src/main.py", "count": 2})
    spec = tool.spec

    assert arguments == DummyArguments(path="src/main.py", count=2)
    assert spec.name == "dummy"
    assert spec.kind is ToolKind.LOCAL
    assert spec.capabilities == frozenset({ToolCapability.FILE_READ})
    assert spec.input_schema["type"] == "object"
    assert spec.input_schema["additionalProperties"] is False
    assert spec.input_schema["properties"]["count"]["type"] == "integer"  # type: ignore[index]


def test_tool_argument_validation_is_strict_and_forbids_extra_fields() -> None:
    tool = DummyTool()

    with pytest.raises(ValidationError):
        tool.validate({"path": "main.py", "count": "2"})
    with pytest.raises(ValidationError):
        tool.validate({"path": "main.py", "unknown": True})


def test_tool_validates_immutable_protocol_arguments() -> None:
    tool = DummyTool()
    call = ToolCall(
        call_id="call-1",
        name="dummy",
        raw_arguments={"path": "main.py", "count": 2},
    )

    assert tool.validate(call.raw_arguments) == DummyArguments(
        path="main.py",
        count=2,
    )


def test_registry_registers_and_returns_tool_and_specs() -> None:
    registry = ToolRegistry()
    tool = DummyTool()

    registry.register(tool)

    assert registry.get("dummy") is tool
    assert registry.specs() == (tool.spec,)


def test_registry_rejects_duplicate_names_at_startup() -> None:
    registry = ToolRegistry()
    registry.register(DummyTool())

    with pytest.raises(DuplicateToolNameError, match="dummy"):
        registry.register(DummyTool())


def test_registry_reports_unknown_tool() -> None:
    registry = ToolRegistry()

    with pytest.raises(UnknownToolError, match="missing"):
        registry.get("missing")


@pytest.mark.parametrize(
    "tool",
    [
        DummyTool(name=""),
        DummyTool(kind=cast(ToolKind, "LOCAL")),
        DummyTool(
            capabilities=cast(
                frozenset[ToolCapability],
                frozenset({"FILE_READ"}),
            )
        ),
    ],
)
def test_registry_rejects_invalid_tool_specs(tool: Tool[DummyArguments]) -> None:
    registry = ToolRegistry()

    with pytest.raises(InvalidToolSpecError):
        registry.register(tool)
