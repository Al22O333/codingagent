"""Deterministic contract tests for bounded completion self-audit."""

from __future__ import annotations

from pathlib import Path

from coding_agent.ask_user import AskUserTool
from coding_agent.context import ContextManager
from coding_agent.edit_file import EditFileTool
from coding_agent.interaction import (
    ClarificationResponse,
    ClarificationStatus,
    ConfirmationDecision,
    FakeUserInteraction,
)
from coding_agent.model_client import FakeModelClient
from coding_agent.policy import PolicyEngine
from coding_agent.prompt import COMPLETION_AUDIT_INSTRUCTION
from coding_agent.protocol import (
    AssistantMessage,
    ModelResponse,
    RuntimeInstructionMessage,
    SystemMessage,
    ToolCall,
    ToolCapability,
    ToolKind,
    ToolOutcome,
    UserMessage,
)
from coding_agent.runtime import (
    AgentRuntime,
    RunState,
    RuntimeEvent,
    RuntimeLimits,
    TerminationReason,
)
from coding_agent.tooling import (
    PreparedToolCall,
    Tool,
    ToolArguments,
    ToolExecutionResult,
    ToolRegistry,
)
from coding_agent.workspace import WorkspacePathResolver


class AuditArguments(ToolArguments):
    value: str = "ok"


class CapabilityTool(Tool[AuditArguments]):
    """Small executable Tool with one configurable capability."""

    def __init__(self, name: str, capability: ToolCapability) -> None:
        super().__init__(
            name=name,
            description="Exercise completion-audit eligibility.",
            argument_model=AuditArguments,
            kind=ToolKind.LOCAL,
            capabilities=frozenset({capability}),
        )

    def prepare(
        self,
        call_id: str,
        arguments: AuditArguments,
    ) -> PreparedToolCall:
        return self.prepared_call(call_id, arguments, {"value": arguments.value})

    def execute(self, prepared_call: PreparedToolCall) -> ToolExecutionResult:
        return ToolExecutionResult(ToolOutcome.SUCCESS, {"executed": True})


LIMITS = RuntimeLimits(
    max_model_turns=10,
    max_tool_call_attempts=10,
    max_active_run_duration_seconds=60,
    max_transport_retries=0,
    max_consecutive_protocol_errors=2,
)


def _runtime(
    capability: ToolCapability,
    responses: list[ModelResponse],
    *,
    limits: RuntimeLimits = LIMITS,
    events: list[RuntimeEvent] | None = None,
) -> tuple[AgentRuntime, FakeModelClient, ContextManager]:
    registry = ToolRegistry()
    registry.register(CapabilityTool("action", capability))
    client = FakeModelClient(responses)
    context = ContextManager()
    runtime = AgentRuntime(
        client,
        context,
        registry,
        limits,
        policy_engine=PolicyEngine(),
        user_interaction=FakeUserInteraction(),
        observer=events.append if events is not None else None,
    )
    return runtime, client, context


def _call(*, raw_arguments: object | None = None) -> ToolCall:
    return ToolCall("action-1", "action", raw_arguments or {})


def test_read_only_tool_run_can_complete_without_self_audit() -> None:
    runtime, client, _ = _runtime(
        ToolCapability.FILE_READ,
        [
            ModelResponse(None, (_call(),)),
            ModelResponse("Direct final."),
        ],
    )

    run = runtime.run("Inspect the project")

    assert run.state is RunState.COMPLETED
    assert run.model_turns == 2
    assert run.final_response == "Direct final."
    assert len(client.requests) == 2


def test_file_mutation_candidate_is_hidden_until_audit_final() -> None:
    events: list[RuntimeEvent] = []
    runtime, client, context = _runtime(
        ToolCapability.FILE_MUTATION,
        [
            ModelResponse(None, (_call(),)),
            ModelResponse(
                "Premature candidate.",
                provider_reasoning_content="candidate-reasoning-must-not-replay",
            ),
            ModelResponse("Audited final."),
        ],
        events=events,
    )

    run = runtime.run("Change the project")

    assert run.state is RunState.COMPLETED
    assert run.model_turns == 3
    assert run.final_response == "Audited final."
    assert run.pending_final_candidate is None
    assert run.completion_audit_required is False
    assert run.completion_audit_active is False
    assert len(client.requests) == 3
    audit_request = client.requests[2]
    assert isinstance(audit_request.messages[-1], RuntimeInstructionMessage)
    assert audit_request.messages[-1].text == COMPLETION_AUDIT_INSTRUCTION
    assert AssistantMessage("Premature candidate.") in audit_request.messages
    assert "candidate-reasoning-must-not-replay" not in repr(audit_request.messages)
    candidate_message = next(
        message
        for message in audit_request.messages
        if isinstance(message, AssistantMessage)
        and message.text == "Premature candidate."
    )
    assert (
        candidate_message.provider_reasoning_content
        == "candidate-reasoning-must-not-replay"
    )
    assert context.build_messages() == (
        # Completed continuity contains only the real user task and real Final.
        UserMessage("Change the project"),
        AssistantMessage("Audited final."),
    )
    assert "Premature candidate." not in repr(context.build_messages())
    assert [event.kind for event in events].count("completion_audit_started") == 1
    assert [event.kind for event in events].count("completion_audit_finished") == 1


def test_audit_tool_turns_remain_in_one_active_audit_until_next_final() -> None:
    events: list[RuntimeEvent] = []
    runtime, client, _ = _runtime(
        ToolCapability.FILE_MUTATION,
        [
            ModelResponse(None, (_call(),)),
            ModelResponse("Candidate."),
            ModelResponse(None, (_call(raw_arguments={"value": "repair"}),)),
            ModelResponse("Final after repair."),
        ],
        events=events,
    )

    run = runtime.run("Change and review")

    assert run.state is RunState.COMPLETED
    assert run.model_turns == 4
    assert run.tool_call_attempts == 2
    assert run.final_response == "Final after repair."
    assert client.requests[2].messages[-1] == RuntimeInstructionMessage(COMPLETION_AUDIT_INSTRUCTION)
    assert client.requests[3].messages[-1] == RuntimeInstructionMessage(COMPLETION_AUDIT_INSTRUCTION)
    assert [event.kind for event in events].count("completion_audit_started") == 1
    assert [event.kind for event in events].count("completion_audit_continued") == 1
    assert [event.kind for event in events].count("completion_audit_finished") == 1


def test_command_execution_validation_failure_still_requires_audit() -> None:
    runtime, client, _ = _runtime(
        ToolCapability.COMMAND_EXECUTION,
        [
            ModelResponse(None, (_call(raw_arguments={"unknown": True}),)),
            ModelResponse("Candidate after validation failure."),
            ModelResponse("Honest audited final."),
        ],
    )

    run = runtime.run("Run a command")

    assert run.state is RunState.COMPLETED
    assert run.model_turns == 3
    assert run.tool_call_attempts == 1
    assert run.final_response == "Honest audited final."
    assert client.requests[2].messages[-1] == RuntimeInstructionMessage(COMPLETION_AUDIT_INSTRUCTION)


def test_unknown_tool_does_not_make_run_eligible() -> None:
    runtime, client, _ = _runtime(
        ToolCapability.FILE_READ,
        [
            ModelResponse(None, (ToolCall("unknown", "missing", {}),)),
            ModelResponse("Unknown tool reported."),
        ],
    )

    run = runtime.run("Try an unknown tool")

    assert run.state is RunState.COMPLETED
    assert run.model_turns == 2
    assert len(client.requests) == 2


def test_budget_exhaustion_never_accepts_unaudited_candidate() -> None:
    limits = RuntimeLimits(
        max_model_turns=2,
        max_tool_call_attempts=10,
        max_active_run_duration_seconds=60,
        max_transport_retries=0,
        max_consecutive_protocol_errors=2,
    )
    runtime, client, _ = _runtime(
        ToolCapability.FILE_MUTATION,
        [
            ModelResponse(None, (_call(),)),
            ModelResponse("Candidate without audit budget."),
            ModelResponse("must remain unused"),
        ],
        limits=limits,
    )

    run = runtime.run("Change the project")

    assert run.state is RunState.FAILED
    assert run.termination_reason is TerminationReason.LIMIT_REACHED
    assert run.limit_reached == "max_model_turns"
    assert run.final_response is None
    assert len(client.requests) == 2
    assert run.pending_final_candidate is None
    assert run.completion_audit_required is False
    assert run.completion_audit_active is False


def test_audit_can_use_same_run_clarification_then_finish() -> None:
    registry = ToolRegistry()
    registry.register(CapabilityTool("action", ToolCapability.FILE_MUTATION))
    registry.register(AskUserTool())
    ask_call = ToolCall(
        "ask-1",
        "ask_user",
        {"question": "Which compatibility behavior should be preserved?"},
    )
    client = FakeModelClient(
        [
            ModelResponse(None, (_call(),)),
            ModelResponse("Candidate."),
            ModelResponse(None, (ask_call,)),
            ModelResponse("Final with clarification."),
        ]
    )
    interaction = FakeUserInteraction(
        answers=(
            ClarificationResponse(ClarificationStatus.ANSWERED, "Keep v1 behavior"),
        )
    )
    runtime = AgentRuntime(
        client,
        ContextManager(),
        registry,
        LIMITS,
        policy_engine=PolicyEngine(),
        user_interaction=interaction,
    )

    run = runtime.run("Change the project")

    assert run.state is RunState.COMPLETED
    assert run.explicit_user_clarifications == ["Keep v1 behavior"]
    assert len(interaction.clarification_requests) == 1
    assert client.requests[3].messages[-1] == RuntimeInstructionMessage(COMPLETION_AUDIT_INSTRUCTION)


def test_audit_tool_call_uses_normal_permission_confirmation(tmp_path: Path) -> None:
    sensitive = tmp_path / ".env"
    sensitive.write_text("MODE=old\n", encoding="utf-8")
    registry = ToolRegistry()
    registry.register(CapabilityTool("action", ToolCapability.FILE_MUTATION))
    registry.register(EditFileTool(WorkspacePathResolver(tmp_path)))
    edit_call = ToolCall(
        "edit-sensitive",
        "edit_file",
        {
            "path": ".env",
            "old_text": "MODE=old",
            "new_text": "MODE=new",
            "expected_count": 1,
        },
    )
    client = FakeModelClient(
        [
            ModelResponse(None, (_call(),)),
            ModelResponse("Candidate."),
            ModelResponse(None, (edit_call,)),
            ModelResponse("Final after approved audit action."),
        ]
    )
    interaction = FakeUserInteraction((ConfirmationDecision.APPROVE,))
    runtime = AgentRuntime(
        client,
        ContextManager(),
        registry,
        LIMITS,
        workspace_resolver=WorkspacePathResolver(tmp_path),
        policy_engine=PolicyEngine(),
        user_interaction=interaction,
    )

    run = runtime.run("Change the project")

    assert run.state is RunState.COMPLETED
    assert sensitive.read_text(encoding="utf-8") == "MODE=new\n"
    assert len(interaction.requests) == 1
    assert client.requests[3].messages[-1] == RuntimeInstructionMessage(COMPLETION_AUDIT_INSTRUCTION)


def test_keyboard_interrupt_during_audit_cancels_and_cleans_state() -> None:
    runtime, client, _ = _runtime(
        ToolCapability.FILE_MUTATION,
        [
            ModelResponse(None, (_call(),)),
            ModelResponse("Candidate."),
            KeyboardInterrupt(),
        ],
    )

    run = runtime.run("Change the project")

    assert run.state is RunState.CANCELLED
    assert run.termination_reason is TerminationReason.USER_CANCELLATION
    assert len(client.requests) == 3
    assert run.pending_final_candidate is None
    assert run.completion_audit_required is False
    assert run.completion_audit_active is False


def test_protocol_recovery_during_audit_keeps_audit_instruction() -> None:
    runtime, client, _ = _runtime(
        ToolCapability.FILE_MUTATION,
        [
            ModelResponse(None, (_call(),)),
            ModelResponse("Candidate."),
            ModelResponse("   "),
            ModelResponse("Audited final after correction."),
        ],
    )

    run = runtime.run("Change the project")

    assert run.state is RunState.COMPLETED
    assert run.model_turns == 4
    final_request_messages = client.requests[3].messages
    assert final_request_messages[-1] == RuntimeInstructionMessage(COMPLETION_AUDIT_INSTRUCTION)
    assert isinstance(final_request_messages[0], SystemMessage)
    assert "previous response was invalid" in final_request_messages[0].text
