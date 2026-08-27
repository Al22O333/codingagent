"""Tests for the Step 5 model-to-final AgentRuntime slice."""

from __future__ import annotations

from pathlib import Path

import pytest

from coding_agent.context import ContextLimitError, ContextManager
from coding_agent.interaction import FakeUserInteraction
from coding_agent.model_client import FakeModelClient
from coding_agent.policy import PolicyEngine
from coding_agent.protocol import (
    AssistantMessage,
    ModelRequest,
    ModelResponse,
    ToolCall,
    ToolOutcome,
    UserMessage,
)
from coding_agent.read_file import ReadFileTool
from coding_agent.runtime import (
    AgentRuntime,
    ModelProtocolError,
    RunState,
    RuntimeLimits,
    TerminationReason,
)
from coding_agent.tooling import ToolRegistry
from coding_agent.workspace import WorkspacePathResolver


TEST_LIMITS = RuntimeLimits(
    max_model_turns=20,
    max_tool_call_attempts=20,
    max_active_run_duration_seconds=60,
    max_transport_retries=1,
    max_consecutive_protocol_errors=2,
)
FAIL_FAST_PROTOCOL_LIMITS = RuntimeLimits(
    max_model_turns=20,
    max_tool_call_attempts=20,
    max_active_run_duration_seconds=60,
    max_transport_retries=1,
    max_consecutive_protocol_errors=1,
)


def test_valid_final_response_completes_run() -> None:
    response = ModelResponse(text="Task completed.")
    client = FakeModelClient([response])
    context = ContextManager()
    runtime = AgentRuntime(
        client,
        context,
        ToolRegistry(),
        TEST_LIMITS,
        policy_engine=PolicyEngine(),
        user_interaction=FakeUserInteraction(),
    )

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
    runtime = AgentRuntime(
        client,
        context,
        ToolRegistry(),
        FAIL_FAST_PROTOCOL_LIMITS,
        policy_engine=PolicyEngine(),
        user_interaction=FakeUserInteraction(),
    )

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
    runtime = AgentRuntime(
        client,
        context,
        ToolRegistry(),
        TEST_LIMITS,
        policy_engine=PolicyEngine(),
        user_interaction=FakeUserInteraction(),
    )

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


def test_second_run_does_not_receive_stale_tool_observation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "a.py"
    target.write_text("old content", encoding="utf-8")
    call = ToolCall(
        call_id="old-read",
        name="read_file",
        raw_arguments={"path": "a.py"},
    )
    client = FakeModelClient(
        [
            ModelResponse(text=None, tool_calls=(call,)),
            ModelResponse(text="First final."),
            ModelResponse(text="Second final."),
        ]
    )
    context = ContextManager()
    registry = ToolRegistry()
    registry.register(
        ReadFileTool(
            WorkspacePathResolver(workspace),
            max_lines=10,
            max_bytes=1024,
        )
    )
    runtime = AgentRuntime(
        client,
        context,
        registry,
        TEST_LIMITS,
        policy_engine=PolicyEngine(),
        user_interaction=FakeUserInteraction(),
    )

    first = runtime.run("Read a.py")
    target.write_text("new content", encoding="utf-8")
    second = runtime.run("Do a new task")

    assert first.state is RunState.COMPLETED
    assert second.state is RunState.COMPLETED
    second_request = client.requests[2]
    assert second_request.messages == (
        UserMessage("Read a.py"),
        AssistantMessage("First final."),
        UserMessage("Do a new task"),
    )
    assert all(
        "old content" not in repr(message)
        for message in second_request.messages
    )


def test_context_limit_failure_is_a_controlled_runtime_failure() -> None:
    client = FakeModelClient([ModelResponse(text="must remain unused")])
    runtime = AgentRuntime(
        client,
        ContextManager(max_context_chars=20),
        ToolRegistry(),
        TEST_LIMITS,
        policy_engine=PolicyEngine(),
        user_interaction=FakeUserInteraction(),
    )

    run = runtime.run("A task that cannot fit the configured context bound")

    assert run.state is RunState.FAILED
    assert run.termination_reason is TerminationReason.RUNTIME_FAILURE
    assert isinstance(run.last_error, ContextLimitError)
    assert client.requests == ()


def test_keyboard_interrupt_cancels_run_without_consuming_model_turn() -> None:
    class InterruptingModelClient:
        def complete(self, request: ModelRequest) -> ModelResponse:
            raise KeyboardInterrupt

    client = InterruptingModelClient()
    context = ContextManager()
    runtime = AgentRuntime(
        client,
        context,
        ToolRegistry(),
        TEST_LIMITS,
        policy_engine=PolicyEngine(),
        user_interaction=FakeUserInteraction(),
    )

    run = runtime.run("Complete the task")

    assert run.state is RunState.CANCELLED
    assert run.model_turns == 0
    assert run.final_response is None
    assert run.termination_reason is TerminationReason.USER_CANCELLATION
    assert context.build_messages() == (UserMessage("Complete the task"),)


def test_multiple_successful_tool_calls_are_processed_before_next_turn(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("main", encoding="utf-8")
    (workspace / "other.py").write_text("other", encoding="utf-8")
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
            ),
            ModelResponse(text="Both files were read."),
        ]
    )
    context = ContextManager()
    registry = ToolRegistry()
    registry.register(
        ReadFileTool(
            WorkspacePathResolver(workspace),
            max_lines=10,
            max_bytes=1024,
        )
    )
    runtime = AgentRuntime(
        client,
        context,
        registry,
        TEST_LIMITS,
        policy_engine=PolicyEngine(),
        user_interaction=FakeUserInteraction(),
    )

    run = runtime.run("Inspect both files")

    results = context.build_messages()[2].results  # type: ignore[union-attr]
    assert [result.outcome for result in results] == [
        ToolOutcome.SUCCESS,
        ToolOutcome.SUCCESS,
    ]
    assert run.tool_call_attempts == 2
    assert run.state is RunState.COMPLETED
