"""Tests for runtime budgets and model recovery semantics."""

from __future__ import annotations

from pathlib import Path

import pytest

from coding_agent.context import ContextManager
from coding_agent.interaction import FakeUserInteraction
from coding_agent.model_client import (
    FakeModelClient,
    FatalProviderError,
    ModelProtocolError,
    TransientProviderError,
)
from coding_agent.policy import PolicyEngine
from coding_agent.protocol import (
    AssistantMessage,
    ModelRequest,
    ModelResponse,
    SystemMessage,
    ToolCall,
    ToolOutcome,
    ToolResultMessage,
    UserMessage,
)
from coding_agent.read_file import ReadFileTool
from coding_agent.runtime import (
    AgentRuntime,
    RunState,
    RuntimeLimits,
    TerminationReason,
)
from coding_agent.tooling import ToolRegistry
from coding_agent.workspace import WorkspacePathResolver


def _limits(
    *,
    model_turns: int = 10,
    tool_attempts: int = 10,
    active_seconds: float = 60,
    transport_retries: int = 1,
    protocol_errors: int = 2,
) -> RuntimeLimits:
    return RuntimeLimits(
        max_model_turns=model_turns,
        max_tool_call_attempts=tool_attempts,
        max_active_run_duration_seconds=active_seconds,
        max_transport_retries=transport_retries,
        max_consecutive_protocol_errors=protocol_errors,
    )


def _registry(workspace: Path | None = None) -> ToolRegistry:
    registry = ToolRegistry()
    if workspace is not None:
        registry.register(
            ReadFileTool(
                WorkspacePathResolver(workspace),
                max_lines=20,
                max_bytes=4096,
            )
        )
    return registry


def test_transport_retry_reuses_same_request_without_extra_model_turn() -> None:
    delays: list[float] = []
    client = FakeModelClient(
        [TransientProviderError("temporary"), ModelResponse(text="Recovered.")]
    )
    runtime = AgentRuntime(
        client,
        ContextManager(),
        _registry(),
        _limits(transport_retries=1),
        policy_engine=PolicyEngine(),
        user_interaction=FakeUserInteraction(),
        sleep_fn=delays.append,
    )

    run = runtime.run("Complete the task")

    assert run.state is RunState.COMPLETED
    assert run.model_turns == 1
    assert len(client.requests) == 2
    assert client.requests[0] is client.requests[1]
    assert delays == [0.25]


def test_transport_retry_exhaustion_fails_without_model_turn() -> None:
    delays: list[float] = []
    client = FakeModelClient(
        [TransientProviderError("one"), TransientProviderError("two")]
    )
    runtime = AgentRuntime(
        client,
        ContextManager(),
        _registry(),
        _limits(transport_retries=1),
        policy_engine=PolicyEngine(),
        user_interaction=FakeUserInteraction(),
        sleep_fn=delays.append,
    )

    run = runtime.run("Complete the task")

    assert run.state is RunState.FAILED
    assert run.termination_reason is TerminationReason.PROVIDER_FAILURE
    assert run.model_turns == 0
    assert len(client.requests) == 2
    assert client.requests[0] is client.requests[1]
    assert delays == [0.25]


def test_fatal_provider_error_is_not_retried() -> None:
    delays: list[float] = []
    client = FakeModelClient(
        [FatalProviderError("bad credential"), ModelResponse(text="unused")]
    )
    runtime = AgentRuntime(
        client,
        ContextManager(),
        _registry(),
        _limits(transport_retries=3),
        policy_engine=PolicyEngine(),
        user_interaction=FakeUserInteraction(),
        sleep_fn=delays.append,
    )

    run = runtime.run("Complete the task")

    assert run.state is RunState.FAILED
    assert run.termination_reason is TerminationReason.PROVIDER_FAILURE
    assert run.model_turns == 0
    assert len(client.requests) == 1
    assert delays == []


def test_transport_retry_backoff_grows_and_is_bounded() -> None:
    delays: list[float] = []
    client = FakeModelClient(
        [
            TransientProviderError("one"),
            TransientProviderError("two"),
            TransientProviderError("three"),
            ModelResponse(text="Recovered."),
        ]
    )
    runtime = AgentRuntime(
        client,
        ContextManager(),
        _registry(),
        _limits(transport_retries=3),
        policy_engine=PolicyEngine(),
        user_interaction=FakeUserInteraction(),
        sleep_fn=delays.append,
        transport_retry_base_delay_seconds=0.1,
        transport_retry_max_delay_seconds=0.15,
    )

    run = runtime.run("Complete the task")

    assert run.state is RunState.COMPLETED
    assert run.model_turns == 1
    assert delays == [0.1, 0.15, 0.15]
    assert len({id(request) for request in client.requests}) == 1


def test_first_attempt_success_does_not_sleep() -> None:
    delays: list[float] = []
    runtime = AgentRuntime(
        FakeModelClient([ModelResponse(text="Done.")]),
        ContextManager(),
        _registry(),
        _limits(transport_retries=3),
        policy_engine=PolicyEngine(),
        user_interaction=FakeUserInteraction(),
        sleep_fn=delays.append,
    )

    run = runtime.run("Complete the task")

    assert run.state is RunState.COMPLETED
    assert delays == []


def test_invalid_response_consumes_turn_and_corrective_response_consumes_another() -> None:
    client = FakeModelClient(
        [ModelResponse(text="  "), ModelResponse(text="Corrected final.")]
    )
    context = ContextManager()
    runtime = AgentRuntime(
        client,
        context,
        _registry(),
        _limits(protocol_errors=2),
        policy_engine=PolicyEngine(),
        user_interaction=FakeUserInteraction(),
    )

    run = runtime.run("Complete the task")

    assert run.state is RunState.COMPLETED
    assert run.model_turns == 2
    assert run.consecutive_protocol_errors == 0
    assert context.build_messages() == (
        UserMessage("Complete the task"),
        AssistantMessage(text="Corrected final."),
    )
    assert isinstance(client.requests[1].messages[0], SystemMessage)
    assert "previous response was invalid" in client.requests[1].messages[0].text
    assert client.requests[1].messages[-1] == UserMessage("Complete the task")


def test_model_client_protocol_error_uses_corrective_reprompt() -> None:
    client = FakeModelClient(
        [ModelProtocolError("malformed"), ModelResponse(text="Corrected.")]
    )
    runtime = AgentRuntime(
        client,
        ContextManager(),
        _registry(),
        _limits(protocol_errors=2),
        policy_engine=PolicyEngine(),
        user_interaction=FakeUserInteraction(),
    )

    run = runtime.run("Complete the task")

    assert run.state is RunState.COMPLETED
    assert run.model_turns == 2
    assert len(client.requests) == 2


def test_protocol_error_limit_exhaustion_stops_without_next_turn() -> None:
    client = FakeModelClient(
        [
            ModelResponse(text=None),
            ModelResponse(text="   "),
            ModelResponse(text="must remain unused"),
        ]
    )
    context = ContextManager()
    runtime = AgentRuntime(
        client,
        context,
        _registry(),
        _limits(protocol_errors=2),
        policy_engine=PolicyEngine(),
        user_interaction=FakeUserInteraction(),
    )

    run = runtime.run("Complete the task")

    assert run.state is RunState.FAILED
    assert run.termination_reason is TerminationReason.PROTOCOL_FAILURE
    assert run.model_turns == 2
    assert run.consecutive_protocol_errors == 2
    assert len(client.requests) == 2
    assert context.build_messages() == ()


def test_model_turn_budget_stops_before_requesting_another_turn(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("main", encoding="utf-8")
    call = ToolCall(
        call_id="read-1",
        name="read_file",
        raw_arguments={"path": "main.py"},
    )
    client = FakeModelClient(
        [
            ModelResponse(text=None, tool_calls=(call,)),
            ModelResponse(text="must remain unused"),
        ]
    )
    runtime = AgentRuntime(
        client,
        ContextManager(),
        _registry(workspace),
        _limits(model_turns=1),
        policy_engine=PolicyEngine(),
        user_interaction=FakeUserInteraction(),
    )

    run = runtime.run("Inspect main.py")

    assert run.state is RunState.FAILED
    assert run.termination_reason is TerminationReason.LIMIT_REACHED
    assert run.limit_reached == "max_model_turns"
    assert run.model_turns == 1
    assert run.tool_call_attempts == 1
    assert len(client.requests) == 1


def test_tool_attempt_budget_marks_remaining_calls_not_executed(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "one.py").write_text("one", encoding="utf-8")
    (workspace / "two.py").write_text("two", encoding="utf-8")
    calls = (
        ToolCall(
            call_id="one",
            name="read_file",
            raw_arguments={"path": "one.py"},
        ),
        ToolCall(
            call_id="two",
            name="read_file",
            raw_arguments={"path": "two.py"},
        ),
    )
    client = FakeModelClient([ModelResponse(text=None, tool_calls=calls)])
    context = ContextManager()
    runtime = AgentRuntime(
        client,
        context,
        _registry(workspace),
        _limits(tool_attempts=1),
        policy_engine=PolicyEngine(),
        user_interaction=FakeUserInteraction(),
    )

    run = runtime.run("Read two files")

    assert run.state is RunState.FAILED
    assert run.termination_reason is TerminationReason.LIMIT_REACHED
    assert run.limit_reached == "max_tool_call_attempts"
    assert run.tool_call_attempts == 1
    assert context.build_messages() == ()


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class AdvancingModelClient:
    def __init__(self, clock: FakeClock, response: ModelResponse) -> None:
        self.clock = clock
        self.response = response
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        self.clock.now += 2.0
        return self.response


def test_active_duration_limit_stops_before_tool_execution(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("main", encoding="utf-8")
    call = ToolCall(
        call_id="read-1",
        name="read_file",
        raw_arguments={"path": "main.py"},
    )
    clock = FakeClock()
    client = AdvancingModelClient(
        clock,
        ModelResponse(text=None, tool_calls=(call,)),
    )
    context = ContextManager()
    runtime = AgentRuntime(
        client,
        context,
        _registry(workspace),
        _limits(active_seconds=1),
        policy_engine=PolicyEngine(),
        user_interaction=FakeUserInteraction(),
        clock=clock,
    )

    run = runtime.run("Inspect main.py")

    assert run.state is RunState.FAILED
    assert run.termination_reason is TerminationReason.LIMIT_REACHED
    assert run.limit_reached == "max_active_run_duration"
    assert run.active_duration_seconds == 2.0
    assert run.model_turns == 1
    assert run.tool_call_attempts == 0
    assert context.build_messages() == ()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"model_turns": 0},
        {"tool_attempts": 0},
        {"active_seconds": 0},
        {"transport_retries": -1},
        {"protocol_errors": 0},
    ],
)
def test_runtime_limits_reject_invalid_values(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        _limits(**kwargs)
