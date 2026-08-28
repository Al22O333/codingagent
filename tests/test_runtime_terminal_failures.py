"""Regression tests for the Runtime terminal exception boundary."""

from __future__ import annotations

from io import StringIO
from pathlib import Path

from coding_agent.cli import ConsoleUserInteraction
from coding_agent.context import ContextManager
from coding_agent.interaction import FakeUserInteraction, UserInteractionError
from coding_agent.model_client import FakeModelClient
from coding_agent.policy import PolicyEngine
from coding_agent.protocol import ModelResponse, ToolCall
from coding_agent.read_file import ReadFileTool
from coding_agent.runtime import (
    AgentRuntime,
    RunState,
    RuntimeLimits,
    TerminationReason,
)
from coding_agent.tooling import PreparedToolCall, ToolRegistry
from coding_agent.workspace import WorkspacePathResolver


LIMITS = RuntimeLimits(10, 10, 30, 0, 1)


class InterruptingReadFileTool(ReadFileTool):
    def execute(self, prepared_call: PreparedToolCall):  # type: ignore[no-untyped-def]
        raise KeyboardInterrupt


class ExplodingPolicyEngine(PolicyEngine):
    def check_explicit_constraints(self, prepared_call, snapshot):  # type: ignore[no-untyped-def]
        raise RuntimeError("boom")


class ExplodingContextManager(ContextManager):
    def build_model_messages(self, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("context boom")


class BrokenWriter(StringIO):
    def write(self, value: str) -> int:
        raise OSError("terminal unavailable")


def _runtime(
    workspace: Path,
    tool: ReadFileTool,
    client: FakeModelClient,
    *,
    context: ContextManager | None = None,
    policy: PolicyEngine | None = None,
    interaction=None,  # type: ignore[no-untyped-def]
) -> AgentRuntime:
    registry = ToolRegistry()
    registry.register(tool)
    return AgentRuntime(
        client,
        context or ContextManager(),
        registry,
        LIMITS,
        workspace_resolver=WorkspacePathResolver(workspace),
        policy_engine=policy or PolicyEngine(),
        user_interaction=interaction or FakeUserInteraction(),
    )


def test_local_tool_keyboard_interrupt_cancels_without_next_model_turn(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("content", encoding="utf-8")
    resolver = WorkspacePathResolver(workspace)
    client = FakeModelClient(
        [
            ModelResponse(
                text=None,
                tool_calls=(ToolCall("read", "read_file", {"path": "main.py"}),)
            ),
            ModelResponse(text="Recovered after cancellation."),
        ]
    )
    context = ContextManager()
    runtime = _runtime(
        workspace,
        InterruptingReadFileTool(resolver, max_lines=10, max_bytes=100),
        client,
        context=context,
    )

    run = runtime.run("Read main.py")

    assert run.state is RunState.CANCELLED
    assert run.termination_reason is TerminationReason.USER_CANCELLATION
    assert run.pending_action is None
    assert run.pending_user_request is None
    assert run.wait_reason is None
    assert len(client.requests) == 1
    assert context.build_messages() == ()

    second = runtime.run("Try again")

    assert second.state is RunState.COMPLETED
    assert len(client.requests) == 2


def test_policy_exception_fails_without_escaping_or_next_model_turn(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("content", encoding="utf-8")
    resolver = WorkspacePathResolver(workspace)
    client = FakeModelClient(
        [
            ModelResponse(
                text=None,
                tool_calls=(ToolCall("read", "read_file", {"path": "main.py"}),)
            ),
            ModelResponse(text="Recovered after runtime failure."),
        ]
    )
    runtime = _runtime(
        workspace,
        ReadFileTool(resolver, max_lines=10, max_bytes=100),
        client,
        policy=ExplodingPolicyEngine(),
    )

    run = runtime.run("Read main.py")

    assert run.state is RunState.FAILED
    assert run.termination_reason is TerminationReason.RUNTIME_FAILURE
    assert isinstance(run.last_error, RuntimeError)
    assert len(client.requests) == 1

    second = runtime.run("Report recovery")

    assert second.state is RunState.COMPLETED
    assert second.final_response == "Recovered after runtime failure."
    assert len(client.requests) == 2
    assert runtime.session.runs == (run, second)


def test_context_exception_fails_before_model_request(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resolver = WorkspacePathResolver(workspace)
    client = FakeModelClient([ModelResponse(text="must remain unused")])
    runtime = _runtime(
        workspace,
        ReadFileTool(resolver, max_lines=10, max_bytes=100),
        client,
        context=ExplodingContextManager(),
    )

    run = runtime.run("Inspect the project")

    assert run.state is RunState.FAILED
    assert run.termination_reason is TerminationReason.RUNTIME_FAILURE
    assert isinstance(run.last_error, RuntimeError)
    assert client.requests == ()


def test_console_io_error_becomes_terminal_user_interaction_failure(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".env").write_text("TOKEN=secret", encoding="utf-8")
    resolver = WorkspacePathResolver(workspace)
    client = FakeModelClient(
        [
            ModelResponse(
                text=None,
                tool_calls=(ToolCall("read", "read_file", {"path": ".env"}),)
            ),
            ModelResponse(text="must remain unused"),
        ]
    )
    interaction = ConsoleUserInteraction(StringIO("y\n"), BrokenWriter())
    runtime = _runtime(
        workspace,
        ReadFileTool(resolver, max_lines=10, max_bytes=100),
        client,
        interaction=interaction,
    )

    run = runtime.run("Inspect .env")

    assert run.state is RunState.FAILED
    assert run.termination_reason is TerminationReason.USER_INTERACTION_FAILURE
    assert isinstance(run.last_error, UserInteractionError)
    assert isinstance(run.last_error.__cause__, OSError)
    assert run.pending_action is None
    assert run.pending_user_request is None
    assert run.wait_reason is None
    assert len(client.requests) == 1
