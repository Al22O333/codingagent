"""Explicit non-interactive execution boundary tests."""

from __future__ import annotations

from io import StringIO
from pathlib import Path

from coding_agent.cli import CLIConfig, build_runtime
from coding_agent.interaction import NonInteractiveUserInteraction
from coding_agent.model_client import FakeModelClient
from coding_agent.protocol import ModelResponse, ToolCall
from coding_agent.runtime import (
    RequiredInteractionKind,
    RunState,
    TerminationReason,
)


def _config(workspace: Path) -> CLIConfig:
    return CLIConfig(
        workspace=workspace,
        model="test-model",
        base_url="https://provider.invalid/v1",
        api_key="runtime-secret",
    )


def test_ordinary_noninteractive_run_completes_without_interaction(
    tmp_path: Path,
) -> None:
    runtime = build_runtime(
        _config(tmp_path),
        model_client=FakeModelClient([ModelResponse("Done without input.")]),
        user_interaction=NonInteractiveUserInteraction(),
        stdout=StringIO(),
    )

    run = runtime.run("Inspect")

    assert run.state is RunState.COMPLETED
    assert run.final_response == "Done without input."
    assert run.required_interaction is None


def test_clarification_terminates_with_bounded_redacted_question_and_no_pending_state(
    tmp_path: Path,
) -> None:
    question = "Choose a format using runtime-secret " + "x" * 2_000
    client = FakeModelClient(
        [
            ModelResponse(
                None,
                (ToolCall("ask", "ask_user", {"question": question}),),
            )
        ]
    )
    runtime = build_runtime(
        _config(tmp_path),
        model_client=client,
        user_interaction=NonInteractiveUserInteraction(),
        stdout=StringIO(),
    )

    run = runtime.run("Implement the unspecified format")

    assert run.state is RunState.FAILED
    assert run.termination_reason is TerminationReason.CLARIFICATION_REQUIRED
    assert run.model_turns == 1
    assert run.tool_call_attempts == 1
    assert run.pending_action is None
    assert run.pending_user_request is None
    assert run.wait_reason is None
    assert run.required_interaction is not None
    assert run.required_interaction.kind is RequiredInteractionKind.CLARIFICATION
    assert run.required_interaction.question is not None
    assert len(run.required_interaction.question) <= 1_000
    assert "runtime-secret" not in run.required_interaction.question
    assert "<redacted>" in run.required_interaction.question
    assert len(client.requests) == 1


def test_permission_boundary_never_executes_or_approves_exact_delete(
    tmp_path: Path,
) -> None:
    target = tmp_path / "obsolete.txt"
    target.write_text("sentinel", encoding="utf-8")
    client = FakeModelClient(
        [
            ModelResponse(
                None,
                (ToolCall("delete", "delete_path", {"path": "obsolete.txt"}),),
            )
        ]
    )
    runtime = build_runtime(
        _config(tmp_path),
        model_client=client,
        user_interaction=NonInteractiveUserInteraction(),
        stdout=StringIO(),
    )

    run = runtime.run("Delete obsolete.txt")

    assert run.state is RunState.FAILED
    assert run.termination_reason is TerminationReason.PERMISSION_REQUIRED
    assert target.read_text(encoding="utf-8") == "sentinel"
    assert run.pending_action is None
    assert run.pending_user_request is None
    assert run.wait_reason is None
    assert run.required_interaction is not None
    assert run.required_interaction.kind is RequiredInteractionKind.PERMISSION
    assert run.required_interaction.tool_name == "delete_path"
    assert run.required_interaction.action_preview == "obsolete.txt"
    assert run.required_interaction.reason_code == "FILE_DELETE_CONFIRMATION"
    assert "not approved or executed" in (run.required_interaction.exact_scope or "")
    assert len(client.requests) == 1
