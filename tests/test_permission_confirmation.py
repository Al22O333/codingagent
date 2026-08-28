"""Exact-action permission confirmation lifecycle tests."""

from __future__ import annotations

from pathlib import Path

from coding_agent.context import ContextManager
from coding_agent.interaction import (
    ConfirmationDecision,
    ConfirmationRequest,
    FakeUserInteraction,
)
from coding_agent.model_client import FakeModelClient
from coding_agent.policy import PolicyEngine
from coding_agent.protocol import ModelResponse, ToolCall, ToolOutcome, ToolResultMessage
from coding_agent.read_file import ReadFileTool
from coding_agent.runtime import (
    AgentRuntime,
    RunState,
    RuntimeLimits,
    TerminationReason,
    WaitReason,
)
from coding_agent.tooling import ToolRegistry
from coding_agent.workspace import WorkspacePathResolver


LIMITS = RuntimeLimits(
    max_model_turns=10,
    max_tool_call_attempts=10,
    max_active_run_duration_seconds=5,
    max_transport_retries=0,
    max_consecutive_protocol_errors=1,
)


class ObservingInteraction(FakeUserInteraction):
    def __init__(
        self,
        decisions: tuple[ConfirmationDecision, ...],
    ) -> None:
        super().__init__(decisions)
        self.runtime: AgentRuntime | None = None
        self.observed_waiting = False
        self.pending_ids: list[int] = []

    def confirm(self, request: ConfirmationRequest) -> ConfirmationDecision:
        assert self.runtime is not None
        run = self.runtime.session.runs[-1]
        self.observed_waiting = (
            run.state is RunState.WAITING_FOR_USER
            and run.wait_reason is WaitReason.PERMISSION_CONFIRMATION
            and run.pending_user_request == request
            and run.pending_action is not None
        )
        assert run.pending_action is not None
        self.pending_ids.append(id(run.pending_action.prepared_call))
        return super().confirm(request)


class AdvancingInteraction(ObservingInteraction):
    def __init__(self, clock: "FakeClock") -> None:
        super().__init__((ConfirmationDecision.APPROVE,))
        self._clock = clock

    def confirm(self, request: ConfirmationRequest) -> ConfirmationDecision:
        self._clock.value += 30
        return super().confirm(request)


class FakeClock:
    value = 0.0

    def __call__(self) -> float:
        return self.value


def _sensitive_call(call_id: str, path: str = ".env") -> ToolCall:
    return ToolCall(
        call_id=call_id,
        name="read_file",
        raw_arguments={"path": path},
    )


def _runtime(
    workspace: Path,
    responses: list[ModelResponse],
    interaction: ObservingInteraction,
    *,
    clock: FakeClock | None = None,
) -> tuple[AgentRuntime, ContextManager, FakeModelClient]:
    registry = ToolRegistry()
    registry.register(
        ReadFileTool(
            WorkspacePathResolver(workspace),
            max_lines=20,
            max_bytes=4096,
        )
    )
    context = ContextManager()
    client = FakeModelClient(responses)
    runtime = AgentRuntime(
        client,
        context,
        registry,
        LIMITS,
        policy_engine=PolicyEngine(),
        user_interaction=interaction,
        **({"clock": clock} if clock is not None else {}),
    )
    interaction.runtime = runtime
    return runtime, context, client


def _first_results(client: FakeModelClient) -> tuple:
    message = client.requests[1].messages[2]
    assert isinstance(message, ToolResultMessage)
    return message.results


def test_approve_executes_exact_pending_action_and_cleans_state(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    interaction = ObservingInteraction((ConfirmationDecision.APPROVE,))
    runtime, _, client = _runtime(
        workspace,
        [
            ModelResponse(text=None, tool_calls=(_sensitive_call("read-secret"),)),
            ModelResponse(text="Inspected with permission."),
        ],
        interaction,
    )

    run = runtime.run("Inspect .env")

    result = _first_results(client)[0]
    assert interaction.observed_waiting
    assert result.outcome is ToolOutcome.SUCCESS
    assert "TOKEN=secret" in repr(result.content)
    assert run.state is RunState.COMPLETED
    assert run.pending_action is None
    assert run.pending_user_request is None
    assert run.wait_reason is None
    assert interaction.requests[0].action_summary == (
        "read_file(end_line=None, path='.env', start_line=1)"
    )


def test_approved_confirmation_ends_old_batch(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".env").write_text("A=1\n", encoding="utf-8")
    (workspace / "later.py").write_text("pass\n", encoding="utf-8")
    interaction = ObservingInteraction((ConfirmationDecision.APPROVE,))
    runtime, _, client = _runtime(
        workspace,
        [
            ModelResponse(
                text=None,
                tool_calls=(
                    _sensitive_call("first"),
                    _sensitive_call("later", "later.py"),
                )
            ),
            ModelResponse(text="Done."),
        ],
        interaction,
    )

    run = runtime.run("Inspect files")

    first, later = _first_results(client)
    assert first.outcome is ToolOutcome.SUCCESS
    assert later.outcome is ToolOutcome.NOT_EXECUTED
    assert run.tool_call_attempts == 1


def test_reject_and_cancel_do_not_execute_pending_action(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".env").write_text("TOKEN=secret\n", encoding="utf-8")

    rejecting = ObservingInteraction((ConfirmationDecision.REJECT,))
    reject_runtime, _, reject_client = _runtime(
        workspace,
        [
            ModelResponse(text=None, tool_calls=(_sensitive_call("reject"),)),
            ModelResponse(text="Continued without reading it."),
        ],
        rejecting,
    )
    rejected = reject_runtime.run("Inspect .env")
    reject_result = _first_results(reject_client)[0]
    assert reject_result.outcome is ToolOutcome.POLICY_REJECTED
    assert reject_result.error is not None
    assert reject_result.error.code == "USER_REJECTED_CONFIRMATION"
    assert "TOKEN=secret" not in repr(reject_result)
    assert rejected.state is RunState.COMPLETED
    assert rejected.pending_action is None

    cancelling = ObservingInteraction((ConfirmationDecision.CANCEL,))
    cancel_runtime, cancel_context, cancel_client = _runtime(
        workspace,
        [
            ModelResponse(text=None, tool_calls=(_sensitive_call("cancel"),)),
            ModelResponse(text="must not be requested"),
        ],
        cancelling,
    )
    cancelled = cancel_runtime.run("Inspect .env")
    assert cancelled.state is RunState.CANCELLED
    assert cancelled.termination_reason is TerminationReason.USER_CANCELLATION
    assert cancelled.model_turns == 1
    assert cancelled.pending_action is None
    assert len(cancel_client.requests) == 1
    assert cancel_context.build_messages() == ()


def test_each_new_action_requires_a_fresh_confirmation(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".env").write_text("A=1\n", encoding="utf-8")
    (workspace / "credentials.json").write_text("{}\n", encoding="utf-8")
    interaction = ObservingInteraction(
        (ConfirmationDecision.APPROVE, ConfirmationDecision.APPROVE)
    )
    runtime, _, _ = _runtime(
        workspace,
        [
            ModelResponse(text=None, tool_calls=(_sensitive_call("one", ".env"),)),
            ModelResponse(
                text=None,
                tool_calls=(_sensitive_call("two", "credentials.json"),)
            ),
            ModelResponse(text="Done."),
        ],
        interaction,
    )

    run = runtime.run("Inspect both sensitive files")

    assert run.state is RunState.COMPLETED
    assert [request.call_id for request in interaction.requests] == ["one", "two"]
    assert len(set(interaction.pending_ids)) == 2
    assert run.pending_action is None


def test_waiting_for_confirmation_does_not_consume_active_duration(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".env").write_text("A=1\n", encoding="utf-8")
    clock = FakeClock()
    interaction = AdvancingInteraction(clock)
    runtime, _, _ = _runtime(
        workspace,
        [
            ModelResponse(text=None, tool_calls=(_sensitive_call("wait"),)),
            ModelResponse(text="Done."),
        ],
        interaction,
        clock=clock,
    )

    run = runtime.run("Inspect .env")

    assert run.state is RunState.COMPLETED
    assert run.paused_duration_seconds == 30
    assert run.active_duration_seconds == 0
