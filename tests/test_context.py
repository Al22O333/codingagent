from __future__ import annotations

import pytest

from coding_agent.context import ContextLimitError, ContextManager, ContextOrderError
from coding_agent.protocol import (
    AssistantMessage,
    ToolCall,
    ToolOutcome,
    ToolResult,
    ToolResultMessage,
    UserMessage,
)


def _tool_call(call_id: str = "call-1", name: str = "read_file") -> ToolCall:
    return ToolCall(call_id=call_id, name=name, raw_arguments={"path": "main.py"})


def _tool_result(
    call_id: str = "call-1", name: str = "read_file"
) -> ToolResult:
    return ToolResult(
        call_id=call_id,
        tool_name=name,
        outcome=ToolOutcome.SUCCESS,
        content={"text": "print('hello')"},
    )


def test_messages_are_built_in_deterministic_recording_order() -> None:
    context = ContextManager()
    user = UserMessage(text="Inspect main.py")
    assistant = AssistantMessage(text="I will inspect it.")

    context.record_user_message(user)
    context.record_assistant_message(assistant)

    assert context.build_messages() == (user, assistant)


def test_assistant_tool_call_precedes_its_tool_result() -> None:
    context = ContextManager()
    assistant = AssistantMessage(text=None, tool_calls=(_tool_call(),))
    result_message = ToolResultMessage(results=(_tool_result(),))

    context.record_assistant_message(assistant)
    context.record_tool_result_message(result_message)

    assert context.build_messages() == (assistant, result_message)


def test_tool_result_without_preceding_call_is_rejected_without_mutation() -> None:
    context = ContextManager()
    user = UserMessage(text="Inspect main.py")
    context.record_user_message(user)

    with pytest.raises(ContextOrderError, match="no preceding assistant tool call"):
        context.record_tool_result_message(
            ToolResultMessage(results=(_tool_result(),))
        )

    assert context.build_messages() == (user,)


def test_mismatched_tool_name_is_rejected_without_mutation() -> None:
    context = ContextManager()
    assistant = AssistantMessage(text=None, tool_calls=(_tool_call(),))
    context.record_assistant_message(assistant)

    with pytest.raises(ContextOrderError, match="name does not match"):
        context.record_tool_result_message(
            ToolResultMessage(results=(_tool_result(name="search_files"),))
        )

    assert context.build_messages() == (assistant,)


def test_build_messages_returns_a_stable_immutable_snapshot() -> None:
    context = ContextManager()
    first = UserMessage(text="First")
    second = UserMessage(text="Second")
    context.record_user_message(first)

    snapshot = context.build_messages()
    context.record_user_message(second)

    assert snapshot == (first,)
    assert context.build_messages() == (first, second)


def test_duplicate_call_ids_are_rejected_before_recording() -> None:
    context = ContextManager()
    assistant = AssistantMessage(
        text=None,
        tool_calls=(_tool_call(), _tool_call()),
    )

    with pytest.raises(ContextOrderError, match="duplicate tool call ids"):
        context.record_assistant_message(assistant)

    assert context.build_messages() == ()


def test_new_run_keeps_only_bounded_task_and_final_continuity() -> None:
    context = ContextManager(max_retained_completed_runs=1)
    old_call = AssistantMessage(text=None, tool_calls=(_tool_call("old-call"),))
    old_result = ToolResultMessage(results=(_tool_result("old-call"),))

    context.start_run(UserMessage("First task"))
    context.record_assistant_message(old_call)
    context.record_tool_result_message(old_result)
    context.record_assistant_message(AssistantMessage("First final"))
    context.end_run(completed=True)
    context.start_run(UserMessage("Second task"))

    assert context.build_messages() == (
        UserMessage("First task"),
        AssistantMessage("First final"),
        UserMessage("Second task"),
    )
    assert old_call not in context.build_messages()
    assert old_result not in context.build_messages()


def test_completed_run_continuity_is_bounded() -> None:
    context = ContextManager(max_retained_completed_runs=1)
    for number in range(3):
        context.start_run(UserMessage(f"Task {number}"))
        context.record_assistant_message(AssistantMessage(f"Final {number}"))
        context.end_run(completed=True)

    context.start_run(UserMessage("Current task"))

    assert context.build_messages() == (
        UserMessage("Task 2"),
        AssistantMessage("Final 2"),
        UserMessage("Current task"),
    )


def test_failed_and_cancelled_runs_do_not_enter_continuity() -> None:
    context = ContextManager(max_retained_completed_runs=1)

    context.start_run(UserMessage("Failed task"))
    context.record_assistant_message(AssistantMessage("Not a final result"))
    context.end_run(completed=False)
    context.start_run(UserMessage("Next task"))

    assert context.build_messages() == (UserMessage("Next task"),)


def test_end_run_clears_pending_tool_correspondence() -> None:
    context = ContextManager()
    context.start_run(UserMessage("Interrupted task"))
    context.record_assistant_message(
        AssistantMessage(None, (_tool_call("pending"),))
    )

    context.end_run(completed=False)
    context.start_run(UserMessage("Recovery task"))

    assert context.build_messages() == (UserMessage("Recovery task"),)


def test_trimming_drops_old_tool_groups_atomically_and_keeps_latest() -> None:
    context = ContextManager(max_context_chars=750)
    task = UserMessage("Current task")
    first_call = AssistantMessage(
        None, (_tool_call("call-a"), _tool_call("call-b"))
    )
    first_results = ToolResultMessage(
        (_tool_result("call-a"), _tool_result("call-b"))
    )
    second_call = AssistantMessage(None, (_tool_call("call-c"),))
    second_result = ToolResultMessage((_tool_result("call-c"),))
    context.start_run(task)
    context.record_assistant_message(first_call)
    context.record_tool_result_message(first_results)
    context.record_assistant_message(second_call)
    context.record_tool_result_message(second_result)

    messages = context.build_messages()

    assert task in messages
    assert first_call not in messages
    assert first_results not in messages
    assert messages[-2:] == (second_call, second_result)
    assert sum(len(repr(message)) for message in messages) <= 750
    assert context.history_incomplete is True

    context.build_messages()
    assert context.history_incomplete is True


def test_mandatory_context_that_cannot_fit_fails_without_dropping_task() -> None:
    context = ContextManager(max_context_chars=50)
    task = UserMessage("x" * 100)
    context.start_run(task)

    with pytest.raises(ContextLimitError, match="mandatory model-visible context"):
        context.build_messages()

    assert context._messages == [task]


def test_large_latest_tool_result_is_visible_after_older_group_is_evicted() -> None:
    task = UserMessage("Inspect the latest result")
    old_call = AssistantMessage(None, (_tool_call("old"),))
    old_result = ToolResultMessage(
        (_tool_result_with_text("old", "o" * 500),)
    )
    latest_call = AssistantMessage(None, (_tool_call("latest"),))
    latest_result = ToolResultMessage(
        (_tool_result_with_text("latest", "n" * 500),)
    )
    mandatory_size = sum(
        len(repr(message)) for message in (task, latest_call, latest_result)
    )
    context = ContextManager(max_context_chars=mandatory_size)
    context.start_run(task)
    context.record_assistant_message(old_call)
    context.record_tool_result_message(old_result)
    context.record_assistant_message(latest_call)
    context.record_tool_result_message(latest_result)

    assert context.build_messages() == (task, latest_call, latest_result)


def test_completed_run_continuity_is_evicted_before_current_run_units() -> None:
    context = ContextManager(max_context_chars=180)
    context.start_run(UserMessage("Old task"))
    context.record_assistant_message(AssistantMessage("Old final"))
    context.end_run(completed=True)
    current = UserMessage("Current task" + "x" * 80)
    context.start_run(current)

    assert context.build_messages() == (current,)
    assert context.history_incomplete is True


def test_history_incomplete_resets_only_at_next_run_start() -> None:
    context = ContextManager(max_context_chars=750)
    context.start_run(UserMessage("Task"))
    context.record_assistant_message(
        AssistantMessage(None, (_tool_call("old"),))
    )
    context.record_tool_result_message(
        ToolResultMessage((_tool_result_with_text("old", "x" * 1000),))
    )
    context.record_assistant_message(
        AssistantMessage(None, (_tool_call("latest"),))
    )
    context.record_tool_result_message(
        ToolResultMessage((_tool_result("latest"),))
    )

    context.build_messages()
    assert context.history_incomplete is True
    context.end_run(completed=False)
    assert context.history_incomplete is True

    context.start_run(UserMessage("Next task"))
    assert context.history_incomplete is False


def _tool_result_with_text(call_id: str, text: str) -> ToolResult:
    return ToolResult(
        call_id=call_id,
        tool_name="read_file",
        outcome=ToolOutcome.SUCCESS,
        content={"text": text},
    )
