"""Minimal provider-neutral conversation context storage."""

from __future__ import annotations

from .protocol import (
    AssistantMessage,
    InternalMessage,
    ToolResultMessage,
    UserMessage,
)


class ContextOrderError(ValueError):
    """Raised when a conversation event would break tool-call correspondence."""


class ContextManager:
    """Record conversation messages and build an immutable ordered snapshot."""

    def __init__(self) -> None:
        self._messages: list[InternalMessage] = []
        self._pending_tool_calls: dict[str, str] = {}

    def record_user_message(self, message: UserMessage) -> None:
        """Append an ordinary user message to the conversation."""
        self._messages.append(message)

    def record_assistant_message(self, message: AssistantMessage) -> None:
        """Append an assistant message before any of its tool results."""
        call_ids = [call.call_id for call in message.tool_calls]
        if len(call_ids) != len(set(call_ids)):
            raise ContextOrderError("assistant message contains duplicate tool call ids")

        duplicate_ids = set(call_ids).intersection(self._pending_tool_calls)
        if duplicate_ids:
            duplicate = sorted(duplicate_ids)[0]
            raise ContextOrderError(f"tool call id is already pending: {duplicate}")

        self._messages.append(message)
        self._pending_tool_calls.update(
            (call.call_id, call.name) for call in message.tool_calls
        )

    def record_tool_result_message(self, message: ToolResultMessage) -> None:
        """Append results only after their corresponding assistant tool calls."""
        result_ids = [result.call_id for result in message.results]
        if len(result_ids) != len(set(result_ids)):
            raise ContextOrderError("tool result message contains duplicate call ids")

        for result in message.results:
            expected_tool_name = self._pending_tool_calls.get(result.call_id)
            if expected_tool_name is None:
                raise ContextOrderError(
                    f"tool result has no preceding assistant tool call: {result.call_id}"
                )
            if result.tool_name != expected_tool_name:
                raise ContextOrderError(
                    "tool result name does not match its assistant tool call: "
                    f"{result.call_id}"
                )

        self._messages.append(message)
        for result in message.results:
            del self._pending_tool_calls[result.call_id]

    def build_messages(self) -> tuple[InternalMessage, ...]:
        """Return the current provider-neutral messages in recorded order."""
        return tuple(self._messages)


__all__ = ["ContextManager", "ContextOrderError"]
