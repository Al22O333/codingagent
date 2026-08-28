"""Lean Runtime observer contract tests."""

from __future__ import annotations

from pathlib import Path

from coding_agent.context import ContextManager
from coding_agent.interaction import ConfirmationDecision, FakeUserInteraction
from coding_agent.model_client import FakeModelClient, TransientProviderError
from coding_agent.policy import PolicyEngine
from coding_agent.protocol import AssistantMessage, ModelResponse, ToolCall, UserMessage
from coding_agent.read_file import ReadFileTool
from coding_agent.runtime import AgentRuntime, RuntimeEvent, RuntimeLimits, RunState
from coding_agent.tooling import ToolRegistry
from coding_agent.workspace import WorkspacePathResolver


LIMITS = RuntimeLimits(10, 10, 60, 2, 3)


def _runtime(
    client: FakeModelClient,
    observer,  # type: ignore[no-untyped-def]
    *,
    registry: ToolRegistry | None = None,
    interaction: FakeUserInteraction | None = None,
    resolver: WorkspacePathResolver | None = None,
    limits: RuntimeLimits = LIMITS,
    context: ContextManager | None = None,
) -> AgentRuntime:
    return AgentRuntime(
        client,
        context or ContextManager(),
        registry or ToolRegistry(),
        limits,
        workspace_resolver=resolver,
        policy_engine=PolicyEngine(),
        user_interaction=interaction or FakeUserInteraction(),
        sleep_fn=lambda _: None,
        observer=observer,
    )


def test_observer_receives_bounded_normalized_lifecycle_and_tool_events() -> None:
    events: list[RuntimeEvent] = []
    secret = "secret-argument-" + "x" * 2_000
    call = ToolCall("call-" + "c" * 500, "unknown_tool", {"payload": secret})
    runtime = _runtime(
        FakeModelClient(
            [ModelResponse(None, (call,)), ModelResponse("Finished.")]
        ),
        events.append,
    )

    run = runtime.run("Complete the task")

    assert run.state is RunState.COMPLETED
    assert [event.kind for event in events] == [
        "run_started",
        "model_response",
        "tool_proposed",
        "tool_result",
        "model_response",
        "run_terminal",
    ]
    assert secret not in repr(events)
    assert all(len(repr(event)) < 2_000 for event in events)
    tool_result = next(event for event in events if event.kind == "tool_result")
    assert tool_result.facts["outcome"] == "VALIDATION_ERROR"


def test_observer_exception_is_isolated_from_runtime_semantics() -> None:
    def broken_observer(event: RuntimeEvent) -> None:
        raise RuntimeError(f"observer failed for {event.kind}")

    runtime = _runtime(
        FakeModelClient([ModelResponse("Finished.")]),
        broken_observer,
    )

    run = runtime.run("Complete the task")

    assert run.state is RunState.COMPLETED
    assert run.final_response == "Finished."


def test_known_runtime_secret_is_redacted_before_observer_callback() -> None:
    events: list[RuntimeEvent] = []
    secret = "runtime-provider-secret"
    call = ToolCall("shell", "shell", {"command": f"echo {secret}"})
    runtime = AgentRuntime(
        FakeModelClient([ModelResponse(None, (call,)), ModelResponse("Done.")]),
        ContextManager(),
        ToolRegistry(),
        LIMITS,
        policy_engine=PolicyEngine(),
        user_interaction=FakeUserInteraction(),
        observer=events.append,
        runtime_secret_values=(secret,),
    )

    runtime.run("Complete the task")

    assert secret not in repr(events)
    proposed = next(event for event in events if event.kind == "tool_proposed")
    assert "<redacted>" in proposed.facts["action"]


def test_observer_reports_policy_and_exact_permission_lifecycle(
    tmp_path: Path,
) -> None:
    (tmp_path / ".env").write_text("TOKEN=value", encoding="utf-8")
    resolver = WorkspacePathResolver(tmp_path)
    registry = ToolRegistry()
    registry.register(ReadFileTool(resolver, max_lines=20, max_bytes=4_096))
    events: list[RuntimeEvent] = []
    call = ToolCall("read-sensitive", "read_file", {"path": ".env"})
    runtime = _runtime(
        FakeModelClient([ModelResponse(None, (call,)), ModelResponse("Not read.")]),
        events.append,
        registry=registry,
        resolver=resolver,
        interaction=FakeUserInteraction((ConfirmationDecision.REJECT,)),
    )

    run = runtime.run("Inspect the environment file")

    assert run.state is RunState.COMPLETED
    kinds = [event.kind for event in events]
    assert kinds.count("policy_outcome") == 2
    assert "permission_requested" in kinds
    assert "permission_resolved" in kinds
    resolved = next(event for event in events if event.kind == "permission_resolved")
    assert resolved.facts["decision"] == "REJECT"


def test_observer_reports_retry_corrective_and_budget_events() -> None:
    retry_events: list[RuntimeEvent] = []
    retry_runtime = _runtime(
        FakeModelClient(
            [TransientProviderError("temporary"), ModelResponse("Recovered.")]
        ),
        retry_events.append,
    )
    retry_runtime.run("Retry")
    assert any(event.kind == "provider_retry" for event in retry_events)

    corrective_events: list[RuntimeEvent] = []
    corrective_runtime = _runtime(
        FakeModelClient([ModelResponse(" "), ModelResponse("Corrected.")]),
        corrective_events.append,
    )
    corrective_runtime.run("Correct")
    assert any(event.kind == "protocol_corrective" for event in corrective_events)

    budget_events: list[RuntimeEvent] = []
    budget_runtime = _runtime(
        FakeModelClient([ModelResponse(None, (ToolCall("x", "missing", {}),))]),
        budget_events.append,
        limits=RuntimeLimits(1, 10, 60, 0, 3),
    )
    budget_runtime.run("Stop at the model budget")
    assert any(event.kind == "budget_exhausted" for event in budget_events)


def test_observer_reports_context_history_transition() -> None:
    context = ContextManager(max_context_chars=8_000)
    context.start_run(UserMessage("Old task"))
    context.record_assistant_message(AssistantMessage("x" * 10_000))
    context.end_run(completed=True)
    events: list[RuntimeEvent] = []
    runtime = _runtime(
        FakeModelClient([ModelResponse("Current final.")]),
        events.append,
        context=context,
    )

    run = runtime.run("Current task")

    assert run.state is RunState.COMPLETED
    event = next(event for event in events if event.kind == "context_truncated")
    assert event.facts == {"history_incomplete": True}
