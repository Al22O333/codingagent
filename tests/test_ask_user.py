"""Same-Run structured clarification Interaction Tool tests."""

from __future__ import annotations

from coding_agent.ask_user import AskUserTool
from coding_agent.context import ContextManager
from coding_agent.interaction import (
    ClarificationResponse,
    ClarificationStatus,
    FakeUserInteraction,
)
from coding_agent.model_client import FakeModelClient
from coding_agent.policy import PolicyEngine
from coding_agent.protocol import (
    ModelResponse,
    ToolCall,
    ToolKind,
    ToolOutcome,
    ToolResultMessage,
    UserMessage,
)
from coding_agent.runtime import (
    AgentRuntime,
    RunState,
    RuntimeLimits,
    TerminationReason,
)
from coding_agent.tooling import ToolRegistry


LIMITS = RuntimeLimits(
    max_model_turns=10,
    max_tool_call_attempts=10,
    max_active_run_duration_seconds=30,
    max_transport_retries=0,
    max_consecutive_protocol_errors=1,
)


def test_tool_description_names_unresolved_product_choices() -> None:
    description = AskUserTool().spec.description

    assert "material product choice" in description
    assert "cannot be resolved from the workspace" in description


class ExplodingPolicyEngine(PolicyEngine):
    def check_explicit_constraints(self, prepared_call, snapshot):  # type: ignore[no-untyped-def]
        raise AssertionError("Interaction Tool entered Explicit Constraint policy")

    def check_risk_permission(self, prepared_call):  # type: ignore[no-untyped-def]
        raise AssertionError("Interaction Tool entered Risk Permission policy")


def _call(call_id: str = "ask", question: str = "Which file?") -> ToolCall:
    return ToolCall(
        call_id=call_id,
        name="ask_user",
        raw_arguments={"question": question},
    )


def _runtime(
    responses: list[ModelResponse],
    interaction: FakeUserInteraction,
    *,
    exploding_policy: bool = False,
) -> tuple[AgentRuntime, ContextManager, FakeModelClient]:
    registry = ToolRegistry()
    tool = AskUserTool()
    assert tool.spec.kind is ToolKind.INTERACTION
    registry.register(tool)
    context = ContextManager()
    client = FakeModelClient(responses)
    runtime = AgentRuntime(
        client,
        context,
        registry,
        LIMITS,
        policy_engine=ExplodingPolicyEngine() if exploding_policy else PolicyEngine(),
        user_interaction=interaction,
    )
    return runtime, context, client


def test_answer_becomes_single_tool_observation_and_next_model_turn() -> None:
    interaction = FakeUserInteraction(
        answers=(
            ClarificationResponse(ClarificationStatus.ANSWERED, "src/main.py"),
        )
    )
    runtime, context, client = _runtime(
        [
            ModelResponse(text=None, tool_calls=(_call(),)),
            ModelResponse(text="I will inspect src/main.py."),
        ],
        interaction,
        exploding_policy=True,
    )

    run = runtime.run("Find the issue")

    messages = client.requests[1].messages
    result_message = messages[3]
    assert isinstance(result_message, ToolResultMessage)
    assert result_message.results[0].outcome is ToolOutcome.SUCCESS
    assert dict(result_message.results[0].content) == {"answer": "src/main.py"}
    assert [message for message in messages if isinstance(message, UserMessage)] == [
        UserMessage("Find the issue")
    ]
    assert len(client.requests) == 2
    assert run.explicit_user_clarifications == ["src/main.py"]
    assert run.state is RunState.COMPLETED


def test_answered_clarification_ends_old_batch_and_updates_trusted_constraints() -> None:
    interaction = FakeUserInteraction(
        answers=(
            ClarificationResponse(ClarificationStatus.ANSWERED, "不要运行命令"),
        )
    )
    runtime, _, client = _runtime(
        [
            ModelResponse(
                text=None,
                tool_calls=(
                    _call("first", "May I run commands?"),
                    _call("second", "This must not execute"),
                ),
            ),
            ModelResponse(text="Understood."),
        ],
        interaction,
    )

    run = runtime.run("Continue carefully")

    result_message = client.requests[1].messages[3]
    assert isinstance(result_message, ToolResultMessage)
    first, second = result_message.results
    assert first.outcome is ToolOutcome.SUCCESS
    assert second.outcome is ToolOutcome.NOT_EXECUTED
    assert run.tool_call_attempts == 1
    assert run.explicit_task_constraints.forbid_command_execution
    assert len(interaction.clarification_requests) == 1


def test_cancelled_clarification_cancels_same_run_without_next_turn() -> None:
    interaction = FakeUserInteraction(
        answers=(ClarificationResponse(ClarificationStatus.CANCELLED),)
    )
    runtime, context, client = _runtime(
        [
            ModelResponse(text=None, tool_calls=(_call(),)),
            ModelResponse(text="must remain unused"),
        ],
        interaction,
    )

    run = runtime.run("Find the issue")

    assert run.state is RunState.CANCELLED
    assert run.termination_reason is TerminationReason.USER_CANCELLATION
    assert run.pending_user_request is None
    assert len(client.requests) == 1
    assert context.build_messages() == ()


def test_blank_question_fails_validation_without_user_interaction() -> None:
    interaction = FakeUserInteraction()
    runtime, _, client = _runtime(
        [
            ModelResponse(text=None, tool_calls=(_call(question="   "),)),
            ModelResponse(text="The question was invalid."),
        ],
        interaction,
    )

    run = runtime.run("Find the issue")

    result_message = client.requests[1].messages[3]
    assert isinstance(result_message, ToolResultMessage)
    result = result_message.results[0]
    assert result.outcome is ToolOutcome.VALIDATION_ERROR
    assert not interaction.clarification_requests
    assert run.state is RunState.COMPLETED
