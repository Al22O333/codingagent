"""Model-visible ToolResult projection tests."""

from __future__ import annotations

from coding_agent.create_file import CreateFileContent
from coding_agent.context import ContextManager
from coding_agent.discovery import (
    DirectoryEntry,
    ListDirectoryContent,
    SearchFilesContent,
)
from coding_agent.edit_file import EditFileContent
from coding_agent.projection import project_tool_result
from coding_agent.protocol import (
    AssistantMessage,
    SystemMessage,
    ToolCall,
    ToolError,
    ToolOutcome,
    ToolResult,
    ToolResultMessage,
    UserMessage,
)
from coding_agent.read_file import ReadFileContent
from coding_agent.search_text import SearchTextContent, TextMatch


def _result(tool_name: str, content: object) -> ToolResult:
    return ToolResult("call-1", tool_name, ToolOutcome.SUCCESS, content=content)


def test_discovery_and_search_projections_are_concise_and_bounded() -> None:
    listed = project_tool_result(
        _result(
            "list_directory",
            ListDirectoryContent(
                ".",
                (DirectoryEntry("src", "directory"), DirectoryEntry("a.py", "file")),
                True,
            ),
        )
    )
    files = project_tool_result(
        _result("search_files", SearchFilesContent("*.py", ".", ("a.py",), False))
    )
    text = project_tool_result(
        _result(
            "search_text",
            SearchTextContent(
                "needle",
                ".",
                (TextMatch("a.py", 7, "x" * 20_000, False),),
                False,
            ),
        )
    )

    assert listed.call_id == files.call_id == text.call_id == "call-1"
    assert listed.outcome is files.outcome is text.outcome is ToolOutcome.SUCCESS
    assert listed.content["truncated"] is True  # type: ignore[index]
    assert files.content["matches"] == ("a.py",)  # type: ignore[index]
    assert len(text.content["matches"]) <= 16_000  # type: ignore[index,arg-type]
    assert text.content["truncated"] is True  # type: ignore[index]


def test_file_mutation_and_read_projections_preserve_only_required_fields() -> None:
    read = project_tool_result(
        _result(
            "read_file",
            ReadFileContent("a.py", 1, 1, 1, "1 | value", False, None),
        )
    )
    edited = project_tool_result(
        _result("edit_file", EditFileContent("a.py", 1, 10, 20))
    )
    created = project_tool_result(
        _result("create_file", CreateFileContent("new.py", 12))
    )

    assert read.content["content"] == "1 | value"  # type: ignore[index]
    assert edited.content == {"path": "a.py", "replacement_count": 1}
    assert "bytes_before" not in edited.content  # type: ignore[operator]
    assert created.content == {"path": "new.py", "bytes_written": 12}


def test_ask_user_and_error_projection_are_faithful_safe_and_corresponding() -> None:
    answer = project_tool_result(
        _result("ask_user", {"answer": "src/main.py"})
    )
    failed = project_tool_result(
        ToolResult(
            "call-error",
            "read_file",
            ToolOutcome.OPERATION_FAILURE,
            error=ToolError(
                "FILE_NOT_FOUND",
                "missing",
                {"path": "missing.py", "traceback": object()},
            ),
        )
    )

    assert answer.content == {"answer": "src/main.py"}
    assert failed.call_id == "call-error"
    assert failed.outcome is ToolOutcome.OPERATION_FAILURE
    assert failed.error is not None
    assert failed.error.code == "FILE_NOT_FOUND"
    assert "object at" not in repr(failed.error.details)


def test_projection_precedes_global_eviction_without_mutating_internal_history() -> None:
    context = ContextManager(max_context_chars=8_000)
    call = ToolCall("call-error", "read_file", {"path": "missing.py"})
    raw_result = ToolResult(
        "call-error",
        "read_file",
        ToolOutcome.OPERATION_FAILURE,
        error=ToolError("IO_ERROR", "x" * 20_000, {"detail": "y" * 20_000}),
    )
    context.start_run(UserMessage("Inspect the file"))
    context.record_assistant_message(AssistantMessage(None, (call,)))
    context.record_tool_result_message(ToolResultMessage((raw_result,)))

    visible = context.build_model_messages()

    assert isinstance(visible[0], SystemMessage)
    assert isinstance(visible[-1], ToolResultMessage)
    assert len(visible[-1].results[0].error.message) == 1_000  # type: ignore[union-attr]
    assert context._messages[-1] == ToolResultMessage((raw_result,))
