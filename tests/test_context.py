from __future__ import annotations

import pytest

from coding_agent.context import ContextManager, ContextOrderError
from coding_agent.protocol import (
    AssistantMessage,
    ToolCall,
    ToolOutcome,
    ToolResult,
    ToolResultMessage,
    UserMessage,
)


def _tool_call(call_id: str = "call-1", name: str = "read_file") -> ToolCall:
    return ToolCall(call_id=call_id, name=name, raw_arguments={"path": "main.py"})


def _tool_result(
    call_id: str = "call-1", name: str = "read_file"
) -> ToolResult:
    return ToolResult(
        call_id=call_id,
        tool_name=name,
        outcome=ToolOutcome.SUCCESS,
        content={"text": "print('hello')"},
    )


def test_messages_are_built_in_deterministic_recording_order() -> None:
    context = ContextManager()
    user = UserMessage(text="Inspect main.py")
    assistant = AssistantMessage(text="I will inspect it.")

    context.record_user_message(user)
    context.record_assistant_message(assistant)

    assert context.build_messages() == (user, assistant)


def test_assistant_tool_call_precedes_its_tool_result() -> None:
    context = ContextManager()
    assistant = AssistantMessage(text=None, tool_calls=(_tool_call(),))
    result_message = ToolResultMessage(results=(_tool_result(),))

    context.record_assistant_message(assistant)
    context.record_tool_result_message(result_message)

    assert context.build_messages() == (assistant, result_message)


def test_tool_result_without_preceding_call_is_rejected_without_mutation() -> None:
    context = ContextManager()
    user = UserMessage(text="Inspect main.py")
    context.record_user_message(user)

    with pytest.raises(ContextOrderError, match="no preceding assistant tool call"):
        context.record_tool_result_message(
            ToolResultMessage(results=(_tool_result(),))
        )

    assert context.build_messages() == (user,)


def test_mismatched_tool_name_is_rejected_without_mutation() -> None:
    context = ContextManager()
    assistant = AssistantMessage(text=None, tool_calls=(_tool_call(),))
    context.record_assistant_message(assistant)

    with pytest.raises(ContextOrderError, match="name does not match"):
        context.record_tool_result_message(
            ToolResultMessage(results=(_tool_result(name="search_files"),))
        )

    assert context.build_messages() == (assistant,)


def test_build_messages_returns_a_stable_immutable_snapshot() -> None:
    context = ContextManager()
    first = UserMessage(text="First")
    second = UserMessage(text="Second")
    context.record_user_message(first)

    snapshot = context.build_messages()
    context.record_user_message(second)

    assert snapshot == (first,)
    assert context.build_messages() == (first, second)


def test_duplicate_call_ids_are_rejected_before_recording() -> None:
    context = ContextManager()
    assistant = AssistantMessage(
        text=None,
        tool_calls=(_tool_call(), _tool_call()),
    )

    with pytest.raises(ContextOrderError, match="duplicate tool call ids"):
        context.record_assistant_message(assistant)

    assert context.build_messages() == ()
