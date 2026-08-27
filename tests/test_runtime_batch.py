"""Tests for Step 9 sequential multi-tool batch semantics."""

from __future__ import annotations

from pathlib import Path

from coding_agent.context import ContextManager
from coding_agent.model_client import FakeModelClient
from coding_agent.protocol import ModelResponse, ToolCall, ToolOutcome, ToolResultMessage
from coding_agent.read_file import ReadFileTool
from coding_agent.runtime import AgentRuntime, RunState
from coding_agent.tooling import ToolRegistry
from coding_agent.workspace import WorkspacePathResolver


class TrackingReadFileTool(ReadFileTool):
    __slots__ = ("prepared_paths",)

    def __init__(self, resolver: WorkspacePathResolver) -> None:
        super().__init__(resolver, max_lines=20, max_bytes=4096)
        object.__setattr__(self, "prepared_paths", [])

    def prepare(self, arguments):
        self.prepared_paths.append(arguments.path)
        return super().prepare(arguments)


def test_batch_fail_stop_preserves_correspondence_and_attempt_count(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "first.py").write_text("first", encoding="utf-8")
    (workspace / "third.py").write_text("third", encoding="utf-8")
    calls = (
        ToolCall(
            call_id="call-1",
            name="read_file",
            raw_arguments={"path": "first.py"},
        ),
        ToolCall(
            call_id="call-2",
            name="read_file",
            raw_arguments={"path": "missing.py"},
        ),
        ToolCall(
            call_id="call-3",
            name="read_file",
            raw_arguments={"path": "third.py"},
        ),
    )
    client = FakeModelClient(
        [
            ModelResponse(text="Reading files.", tool_calls=calls),
            ModelResponse(text="The second file was missing."),
        ]
    )
    context = ContextManager()
    tool = TrackingReadFileTool(WorkspacePathResolver(workspace))
    registry = ToolRegistry()
    registry.register(tool)
    runtime = AgentRuntime(client, context, registry)

    run = runtime.run("Inspect three files")

    result_message = context.build_messages()[2]
    assert isinstance(result_message, ToolResultMessage)
    results = result_message.results
    assert [result.call_id for result in results] == [
        "call-1",
        "call-2",
        "call-3",
    ]
    assert [result.outcome for result in results] == [
        ToolOutcome.SUCCESS,
        ToolOutcome.OPERATION_FAILURE,
        ToolOutcome.NOT_EXECUTED,
    ]
    assert results[1].error is not None
    assert results[1].error.code == "FILE_NOT_FOUND"
    assert results[2].error is not None
    assert results[2].error.code == "BATCH_ABORTED"
    assert tool.prepared_paths == ["first.py", "missing.py"]
    assert run.tool_call_attempts == 2
    assert run.model_turns == 2
    assert run.state is RunState.COMPLETED
    assert client.requests[1].messages[-1] is result_message


def test_validation_failure_stops_before_later_call_validation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "later.py").write_text("later", encoding="utf-8")
    calls = (
        ToolCall(
            call_id="invalid",
            name="read_file",
            raw_arguments={"path": "later.py", "start_line": 0},
        ),
        ToolCall(
            call_id="later",
            name="read_file",
            raw_arguments={"path": "later.py"},
        ),
    )
    client = FakeModelClient(
        [
            ModelResponse(text=None, tool_calls=calls),
            ModelResponse(text="The first call was invalid."),
        ]
    )
    context = ContextManager()
    tool = TrackingReadFileTool(WorkspacePathResolver(workspace))
    registry = ToolRegistry()
    registry.register(tool)
    runtime = AgentRuntime(client, context, registry)

    run = runtime.run("Read later.py")

    results = context.build_messages()[2].results  # type: ignore[union-attr]
    assert [result.outcome for result in results] == [
        ToolOutcome.VALIDATION_ERROR,
        ToolOutcome.NOT_EXECUTED,
    ]
    assert tool.prepared_paths == []
    assert run.tool_call_attempts == 1
