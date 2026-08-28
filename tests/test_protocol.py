"""Unit tests for provider-neutral protocol objects."""

from dataclasses import FrozenInstanceError

import pytest

from coding_agent.protocol import (
    AssistantMessage,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    SystemMessage,
    ToolCall,
    ToolCapability,
    ToolError,
    ToolKind,
    ToolOutcome,
    ToolResult,
    ToolResultMessage,
    ToolSpec,
    UserMessage,
)


def test_protocol_enum_values_are_the_architecture_v1_sets() -> None:
    assert {item.value for item in ToolOutcome} == {
        "SUCCESS",
        "VALIDATION_ERROR",
        "POLICY_REJECTED",
        "OPERATION_FAILURE",
        "UNSUCCESSFUL_COMMAND",
        "NOT_EXECUTED",
    }
    assert {item.value for item in ToolKind} == {"LOCAL", "INTERACTION"}
    assert {item.value for item in ToolCapability} == {
        "FILE_READ",
        "FILE_MUTATION",
        "COMMAND_EXECUTION",
    }


def test_tool_protocol_objects_are_constructible_and_frozen() -> None:
    raw_arguments = {"path": "src/main.py", "options": ["number_lines"]}
    input_schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
    }
    call = ToolCall("call-1", "read_file", raw_arguments)
    spec = ToolSpec(
        name="read_file",
        description="Read a text file",
        input_schema=input_schema,
        kind=ToolKind.LOCAL,
        capabilities=frozenset({ToolCapability.FILE_READ}),
    )
    error = ToolError(
        code="FILE_NOT_FOUND",
        message="The requested file does not exist",
        details={"path": "missing.py"},
    )
    result = ToolResult(
        call_id=call.call_id,
        tool_name=call.name,
        outcome=ToolOutcome.OPERATION_FAILURE,
        content={"partial": []},
        error=error,
    )

    raw_arguments["path"] = "changed.py"
    input_schema["type"] = "array"

    assert call.raw_arguments["path"] == "src/main.py"  # type: ignore[index]
    assert spec.input_schema["type"] == "object"
    assert result.content == {"partial": ()}
    assert result.error is error

    with pytest.raises(FrozenInstanceError):
        call.name = "other"  # type: ignore[misc]
    with pytest.raises(TypeError):
        spec.input_schema["type"] = "array"  # type: ignore[index]


def test_model_protocol_objects_preserve_typed_message_order() -> None:
    call = ToolCall("call-1", "read_file", {"path": "main.py"})
    result = ToolResult(
        call_id="call-1",
        tool_name="read_file",
        outcome=ToolOutcome.SUCCESS,
        content={"text": "print('hello')"},
    )
    messages = [
        SystemMessage("Follow the current task."),
        UserMessage("Read main.py"),
        AssistantMessage(text=None, tool_calls=(call,)),
        ToolResultMessage(results=(result,)),
    ]
    request = ModelRequest(messages=messages)
    usage = ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15)
    response = ModelResponse(text="Done.", usage=usage)

    messages.clear()

    assert [type(message) for message in request.messages] == [
        SystemMessage,
        UserMessage,
        AssistantMessage,
        ToolResultMessage,
    ]
    assert request.tools == ()
    assert response.tool_calls == ()
    assert response.usage == usage


@pytest.mark.parametrize(
    ("factory", "expected_message"),
    [
        (lambda: ToolCall("", "read_file", {}), "call_id"),
        (lambda: ToolCall("call-1", " ", {}), "name"),
        (lambda: ToolError("", "message"), "code"),
        (lambda: ModelUsage(input_tokens=-1), "input_tokens"),
    ],
)
def test_basic_protocol_invariants(factory, expected_message: str) -> None:
    with pytest.raises(ValueError, match=expected_message):
        factory()
