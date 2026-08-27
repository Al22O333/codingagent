"""Tests for the Step 5 model-to-final AgentRuntime slice."""

from __future__ import annotations

import pytest

from coding_agent.context import ContextManager
from coding_agent.model_client import FakeModelClient
from coding_agent.protocol import (
    AssistantMessage,
    ModelRequest,
    ModelResponse,
    ToolCall,
    UserMessage,
)
from coding_agent.runtime import (
    AgentRuntime,
    ModelProtocolError,
    RunState,
    TerminationReason,
)
from coding_agent.tooling import ToolRegistry


def test_valid_final_response_completes_run() -> None:
    response = ModelResponse(text="Task completed.")
    client = FakeModelClient([response])
    context = ContextManager()
    runtime = AgentRuntime(client, context, ToolRegistry())

    run = runtime.run("Complete the task")

    assert run.state is RunState.COMPLETED
    assert run.model_turns == 1
    assert run.final_response == "Task completed."
    assert run.termination_reason is None
    assert run.last_error is None
    assert runtime.session.runs == (run,)
    assert client.requests[0].messages == (UserMessage("Complete the task"),)
    assert context.build_messages() == (
        UserMessage("Complete the task"),
        AssistantMessage(text="Task completed."),
    )


@pytest.mark.parametrize("text", [None, "", "   \n\t"])
def test_empty_no_tool_response_follows_protocol_error_path(
    text: str | None,
) -> None:
    client = FakeModelClient([ModelResponse(text=text)])
    context = ContextManager()
    runtime = AgentRuntime(client, context, ToolRegistry())

    run = runtime.run("Complete the task")

    assert run.state is RunState.FAILED
    assert run.state is not RunState.COMPLETED
    assert run.model_turns == 1
    assert run.consecutive_protocol_errors == 1
    assert run.final_response is None
    assert run.termination_reason is TerminationReason.PROTOCOL_FAILURE
    assert isinstance(run.last_error, ModelProtocolError)
    assert context.build_messages() == (UserMessage("Complete the task"),)


def test_session_keeps_sequential_runs_and_conversation_continuity() -> None:
    client = FakeModelClient(
        [ModelResponse(text="First final."), ModelResponse(text="Second final.")]
    )
    context = ContextManager()
    runtime = AgentRuntime(client, context, ToolRegistry())

    first = runtime.run("First task")
    second = runtime.run("Second task")

    assert runtime.session.runs == (first, second)
    assert first.run_id != second.run_id
    assert client.requests[1].messages == (
        UserMessage("First task"),
        AssistantMessage(text="First final."),
        UserMessage("Second task"),
    )
    assert second.state is RunState.COMPLETED


def test_keyboard_interrupt_cancels_run_without_consuming_model_turn() -> None:
    class InterruptingModelClient:
        def complete(self, request: ModelRequest) -> ModelResponse:
            raise KeyboardInterrupt

    client = InterruptingModelClient()
    context = ContextManager()
    runtime = AgentRuntime(client, context, ToolRegistry())

    run = runtime.run("Complete the task")

    assert run.state is RunState.CANCELLED
    assert run.model_turns == 0
    assert run.final_response is None
    assert run.termination_reason is TerminationReason.USER_CANCELLATION
    assert context.build_messages() == (UserMessage("Complete the task"),)


def test_multi_tool_turn_is_not_implemented_before_step_9() -> None:
    first_call = ToolCall(
        call_id="call-1", name="read_file", raw_arguments={"path": "main.py"}
    )
    second_call = ToolCall(
        call_id="call-2", name="read_file", raw_arguments={"path": "other.py"}
    )
    client = FakeModelClient(
        [
            ModelResponse(
                text="I will inspect them.",
                tool_calls=(first_call, second_call),
            )
        ]
    )
    context = ContextManager()
    runtime = AgentRuntime(client, context, ToolRegistry())

    with pytest.raises(NotImplementedError, match="outside Step 8 scope"):
        runtime.run("Inspect both files")

    assert context.build_messages() == (UserMessage("Inspect both files"),)
