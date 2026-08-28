"""Bounded provider-neutral conversation context storage."""

from __future__ import annotations

from .protocol import (
    AssistantMessage,
    InternalMessage,
    SystemMessage,
    ToolResultMessage,
    UserMessage,
)
from .prompt import build_system_prefix


class ContextOrderError(ValueError):
    """Raised when a conversation event would break tool-call correspondence."""


class ContextLimitError(RuntimeError):
    """Raised when mandatory model-visible context cannot fit its size bound."""


class ContextManager:
    """Retain bounded conversation continuity and current-Run messages."""

    def __init__(
        self,
        *,
        max_context_chars: int = 256_000,
        max_retained_completed_runs: int = 1,
    ) -> None:
        if max_context_chars <= 0:
            raise ValueError("max_context_chars must be positive")
        if max_retained_completed_runs < 0:
            raise ValueError("max_retained_completed_runs must not be negative")
        self._max_context_chars = max_context_chars
        self._max_retained_completed_runs = max_retained_completed_runs
        self._completed_run_continuity: list[tuple[InternalMessage, ...]] = []
        self._messages: list[InternalMessage] = []
        self._pending_tool_calls: dict[str, str] = {}
        self._run_active = False
        self._history_incomplete = False

    @property
    def history_incomplete(self) -> bool:
        """Whether this Run has permanently evicted model-visible history."""
        return self._history_incomplete

    def start_run(self, message: UserMessage) -> None:
        """Start one Run with fresh transient history."""
        if self._run_active:
            raise ContextOrderError("cannot start a run while another run is active")
        self._pending_tool_calls.clear()
        self._messages = [message]
        self._run_active = True
        self._history_incomplete = False

    def end_run(self, *, completed: bool) -> None:
        """Finalize continuity and clear all current-Run transient state."""
        if self._run_active and completed:
            continuity = self._completed_run_summary()
            if continuity is not None and self._max_retained_completed_runs:
                self._completed_run_continuity.append(continuity)
                del self._completed_run_continuity[
                    : -self._max_retained_completed_runs
                ]
        if self._max_retained_completed_runs == 0:
            self._completed_run_continuity.clear()
        self._messages.clear()
        self._pending_tool_calls.clear()
        self._run_active = False

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

    def build_messages(
        self,
        additional_messages: tuple[InternalMessage, ...] = (),
    ) -> tuple[InternalMessage, ...]:
        """Build a bounded snapshot without splitting ToolCall/ToolResult groups."""
        continuity = list(self._completed_run_continuity)
        current_units = self._current_message_units()
        additional_units = [(message,) for message in additional_messages]

        while continuity and self._units_size(
            [*continuity, *current_units, *additional_units]
        ) > self._max_context_chars:
            continuity.pop(0)
            self._history_incomplete = True

        protected = self._protected_current_unit_indexes(current_units)
        index = 0
        while self._units_size(
            [*continuity, *current_units, *additional_units]
        ) > self._max_context_chars:
            while index < len(current_units) and index in protected:
                index += 1
            if index >= len(current_units):
                raise ContextLimitError(
                    "mandatory model-visible context exceeds max_context_chars"
                )
            current_units.pop(index)
            self._history_incomplete = True
            protected = {
                protected_index - 1
                if protected_index > index
                else protected_index
                for protected_index in protected
                if protected_index != index
            }

        self._completed_run_continuity = continuity
        self._messages = [message for unit in current_units for message in unit]
        return tuple(
            message
            for unit in [*continuity, *current_units, *additional_units]
            for message in unit
        )

    def build_model_messages(
        self,
        *,
        repeated_action_warning: str | None = None,
        corrective_instruction: str | None = None,
    ) -> tuple[InternalMessage, ...]:
        """Build a bounded snapshot with one request-local system prefix."""

        prefix = build_system_prefix(
            history_incomplete=self._history_incomplete,
            repeated_action_warning=repeated_action_warning,
            corrective_instruction=corrective_instruction,
        )
        previously_incomplete = self._history_incomplete
        messages_with_tail_prefix = self.build_messages((prefix,))
        if not previously_incomplete and self._history_incomplete:
            prefix = build_system_prefix(
                history_incomplete=True,
                repeated_action_warning=repeated_action_warning,
                corrective_instruction=corrective_instruction,
            )
            messages_with_tail_prefix = self.build_messages((prefix,))
        return (prefix, *messages_with_tail_prefix[:-1])

    def _completed_run_summary(self) -> tuple[InternalMessage, ...] | None:
        if not self._messages or not isinstance(self._messages[0], UserMessage):
            return None
        final = self._messages[-1]
        if not isinstance(final, AssistantMessage) or final.tool_calls:
            return None
        return (self._messages[0], final)

    def _current_message_units(self) -> list[tuple[InternalMessage, ...]]:
        units: list[tuple[InternalMessage, ...]] = []
        index = 0
        while index < len(self._messages):
            message = self._messages[index]
            if isinstance(message, AssistantMessage) and message.tool_calls:
                if (
                    index + 1 < len(self._messages)
                    and isinstance(self._messages[index + 1], ToolResultMessage)
                ):
                    units.append((message, self._messages[index + 1]))
                    index += 2
                    continue
            units.append((message,))
            index += 1
        return units

    @staticmethod
    def _protected_current_unit_indexes(
        units: list[tuple[InternalMessage, ...]],
    ) -> set[int]:
        protected = {
            index
            for index, unit in enumerate(units)
            if any(isinstance(message, (SystemMessage, UserMessage)) for message in unit)
            or ContextManager._is_clarification_unit(unit)
        }
        tool_units = [
            index
            for index, unit in enumerate(units)
            if len(unit) == 2
            and isinstance(unit[0], AssistantMessage)
            and isinstance(unit[1], ToolResultMessage)
        ]
        if tool_units:
            protected.add(tool_units[-1])
        return protected

    @staticmethod
    def _is_clarification_unit(unit: tuple[InternalMessage, ...]) -> bool:
        if len(unit) != 2 or not isinstance(unit[0], AssistantMessage):
            return False
        return any(call.name == "ask_user" for call in unit[0].tool_calls)

    @staticmethod
    def _units_size(units: list[tuple[InternalMessage, ...]]) -> int:
        """Return one centralized provider-neutral character approximation."""
        return sum(len(repr(message)) for unit in units for message in unit)


__all__ = ["ContextLimitError", "ContextManager", "ContextOrderError"]
