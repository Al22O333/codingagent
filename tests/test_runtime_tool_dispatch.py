"""Integration tests for the Step 8 single-ToolCall runtime loop."""

from __future__ import annotations

from pathlib import Path

from coding_agent.context import ContextManager
from coding_agent.model_client import FakeModelClient
from coding_agent.protocol import (
    AssistantMessage,
    ModelResponse,
    ToolCall,
    ToolOutcome,
    ToolResultMessage,
    UserMessage,
)
from coding_agent.read_file import ReadFileContent, ReadFileTool
from coding_agent.runtime import AgentRuntime, RunState, RuntimeLimits
from coding_agent.tooling import PreparedToolCall, ToolExecutionResult, ToolRegistry
from coding_agent.workspace import WorkspacePathResolver


TEST_LIMITS = RuntimeLimits(
    max_model_turns=20,
    max_tool_call_attempts=20,
    max_active_run_duration_seconds=60,
    max_transport_retries=1,
    max_consecutive_protocol_errors=2,
)


class ObservingReadFileTool(ReadFileTool):
    """Assert that Runtime records the assistant call before local execution."""

    __slots__ = ("_context",)

    def __init__(
        self,
        resolver: WorkspacePathResolver,
        context: ContextManager,
    ) -> None:
        super().__init__(resolver, max_lines=20, max_bytes=4096)
        object.__setattr__(self, "_context", context)

    def execute(
        self,
        prepared_call: PreparedToolCall,
    ) -> ToolExecutionResult:
        latest_message = self._context.build_messages()[-1]
        assert isinstance(latest_message, AssistantMessage)
        assert latest_message.tool_calls[0].call_id == "call-read"
        return super().execute(prepared_call)


def _runtime(
    workspace: Path,
    responses: list[ModelResponse],
    *,
    observing: bool = False,
) -> tuple[AgentRuntime, FakeModelClient, ContextManager]:
    context = ContextManager()
    resolver = WorkspacePathResolver(workspace)
    tool = (
        ObservingReadFileTool(resolver, context)
        if observing
        else ReadFileTool(resolver, max_lines=20, max_bytes=4096)
    )
    registry = ToolRegistry()
    registry.register(tool)
    client = FakeModelClient(responses)
    return AgentRuntime(client, context, registry, TEST_LIMITS), client, context


def test_read_file_tool_loop_reaches_final_response(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("print('hello')\n", encoding="utf-8")
    tool_call = ToolCall(
        call_id="call-read",
        name="read_file",
        raw_arguments={"path": "main.py"},
    )
    runtime, client, context = _runtime(
        workspace,
        [
            ModelResponse(text="I will inspect it.", tool_calls=(tool_call,)),
            ModelResponse(text="The file prints hello."),
        ],
        observing=True,
    )

    run = runtime.run("Inspect main.py")

    assert run.state is RunState.COMPLETED
    assert run.final_response == "The file prints hello."
    assert run.model_turns == 2
    assert run.tool_call_attempts == 1
    messages = context.build_messages()
    assert messages[0] == UserMessage("Inspect main.py")
    assert messages[1] == AssistantMessage(
        text="I will inspect it.", tool_calls=(tool_call,)
    )
    assert isinstance(messages[2], ToolResultMessage)
    tool_result = messages[2].results[0]
    assert tool_result.call_id == "call-read"
    assert tool_result.tool_name == "read_file"
    assert tool_result.outcome is ToolOutcome.SUCCESS
    assert tool_result.content == ReadFileContent(
        path="main.py",
        start_line=1,
        end_line=1,
        total_lines=1,
        content="1 | print('hello')",
        truncated=False,
        next_start_line=None,
    )
    assert messages[3] == AssistantMessage(text="The file prints hello.")
    assert client.requests[0].tools[0].name == "read_file"
    assert client.requests[1].messages == messages[:3]


def test_unknown_tool_returns_validation_observation_then_continues(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    call = ToolCall(call_id="unknown-1", name="read_files", raw_arguments={})
    runtime, _, context = _runtime(
        workspace,
        [
            ModelResponse(text=None, tool_calls=(call,)),
            ModelResponse(text="I corrected the tool name."),
        ],
    )

    run = runtime.run("Inspect files")

    result = context.build_messages()[2].results[0]  # type: ignore[union-attr]
    assert result.call_id == "unknown-1"
    assert result.outcome is ToolOutcome.VALIDATION_ERROR
    assert result.error is not None and result.error.code == "UNKNOWN_TOOL"
    assert run.tool_call_attempts == 1
    assert run.state is RunState.COMPLETED


def test_invalid_arguments_return_validation_observation(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    call = ToolCall(
        call_id="invalid-1",
        name="read_file",
        raw_arguments={"path": "main.py", "start_line": 0},
    )
    runtime, _, context = _runtime(
        workspace,
        [
            ModelResponse(text=None, tool_calls=(call,)),
            ModelResponse(text="The arguments were invalid."),
        ],
    )

    run = runtime.run("Inspect main.py")

    result = context.build_messages()[2].results[0]  # type: ignore[union-attr]
    assert result.call_id == "invalid-1"
    assert result.outcome is ToolOutcome.VALIDATION_ERROR
    assert result.error is not None and result.error.code == "INVALID_ARGUMENTS"
    assert run.tool_call_attempts == 1


def test_preparation_failure_becomes_runtime_tool_result(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    call = ToolCall(
        call_id="missing-1",
        name="read_file",
        raw_arguments={"path": "missing.py"},
    )
    runtime, _, context = _runtime(
        workspace,
        [
            ModelResponse(text=None, tool_calls=(call,)),
            ModelResponse(text="The file does not exist."),
        ],
    )

    run = runtime.run("Inspect missing.py")

    result = context.build_messages()[2].results[0]  # type: ignore[union-attr]
    assert result.call_id == "missing-1"
    assert result.outcome is ToolOutcome.OPERATION_FAILURE
    assert result.error is not None and result.error.code == "FILE_NOT_FOUND"
    assert run.state is RunState.COMPLETED


def test_workspace_boundary_is_rejected_without_execution(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    call = ToolCall(
        call_id="outside-1",
        name="read_file",
        raw_arguments={"path": str(outside)},
    )
    runtime, _, context = _runtime(
        workspace,
        [
            ModelResponse(text=None, tool_calls=(call,)),
            ModelResponse(text="Outside access was rejected."),
        ],
    )

    runtime.run("Inspect outside file")

    result = context.build_messages()[2].results[0]  # type: ignore[union-attr]
    assert result.outcome is ToolOutcome.POLICY_REJECTED
    assert result.error is not None and result.error.code == "WORKSPACE_BOUNDARY"
    assert "secret" not in repr(result.content)


def test_sensitive_path_is_not_auto_executed_before_confirmation_exists(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".env").write_text("TOKEN=secret", encoding="utf-8")
    call = ToolCall(
        call_id="sensitive-1",
        name="read_file",
        raw_arguments={"path": ".env"},
    )
    runtime, _, context = _runtime(
        workspace,
        [
            ModelResponse(text=None, tool_calls=(call,)),
            ModelResponse(text="Confirmation is required."),
        ],
    )

    runtime.run("Inspect .env")

    result = context.build_messages()[2].results[0]  # type: ignore[union-attr]
    assert result.outcome is ToolOutcome.POLICY_REJECTED
    assert result.error is not None
    assert result.error.code == "SENSITIVE_PATH_CONFIRMATION_REQUIRED"
    assert "TOKEN=secret" not in repr(result)
