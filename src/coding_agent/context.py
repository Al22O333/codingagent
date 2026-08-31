"""Bounded provider-neutral conversation context storage."""

from __future__ import annotations

from dataclasses import dataclass

from .protocol import (
    AssistantMessage,
    InternalMessage,
    ProjectInstructionMessage,
    RuntimeInstructionMessage,
    SystemMessage,
    ToolResultMessage,
    UserMessage,
)
from .project_instructions import RootProjectInstructions
from .prompt import COMPLETION_AUDIT_INSTRUCTION, build_system_prefix
from .projection import project_tool_result_message


class ContextOrderError(ValueError):
    """Raised when a conversation event would break tool-call correspondence."""


class ContextLimitError(RuntimeError):
    """Raised when mandatory model-visible context cannot fit its size bound."""


@dataclass(frozen=True, slots=True)
class ModelContextSize:
    """Content-free size of the latest attempted model-visible snapshot."""

    chars: int
    limit: int
    reasoning_chars: int


@dataclass(frozen=True, slots=True)
class CompletedRunContinuity:
    """Terminal-safe conversational continuity for one completed Run."""

    task: str
    final_response: str


class ContextManager:
    """Retain bounded conversation continuity and current-Run messages."""

    def __init__(
        self,
        *,
        max_context_chars: int = 256_000,
        max_retained_completed_runs: int = 1,
        root_project_instructions: RootProjectInstructions | None = None,
    ) -> None:
        if max_context_chars <= 0:
            raise ValueError("max_context_chars must be positive")
        if max_retained_completed_runs < 0:
            raise ValueError("max_retained_completed_runs must not be negative")
        self._max_context_chars = max_context_chars
        self._max_retained_completed_runs = max_retained_completed_runs
        self._root_project_instructions = root_project_instructions
        self._completed_run_continuity: list[tuple[InternalMessage, ...]] = []
        self._messages: list[InternalMessage] = []
        self._pending_tool_calls: dict[str, str] = {}
        self._pending_candidate: AssistantMessage | None = None
        self._run_active = False
        self._history_incomplete = False
        self._project_instruction: ProjectInstructionMessage | None = None
        self._last_model_context_size: ModelContextSize | None = None

    @property
    def last_model_context_size(self) -> ModelContextSize | None:
        return self._last_model_context_size

    @property
    def history_incomplete(self) -> bool:
        """Whether this Run has permanently evicted model-visible history."""
        return self._history_incomplete

    @property
    def completed_run_continuity(self) -> tuple[CompletedRunContinuity, ...]:
        """Export only bounded completed task/final pairs."""

        records: list[CompletedRunContinuity] = []
        for unit in self._completed_run_continuity:
            if (
                len(unit) == 2
                and isinstance(unit[0], UserMessage)
                and isinstance(unit[1], AssistantMessage)
                and not unit[1].tool_calls
                and unit[1].text is not None
            ):
                records.append(
                    CompletedRunContinuity(unit[0].text, unit[1].text)
                )
        return tuple(records)

    def restore_completed_run_continuity(
        self,
        records: tuple[CompletedRunContinuity, ...],
    ) -> None:
        """Restore validated historical pairs before a new Run starts."""

        if self._run_active:
            raise ContextOrderError("cannot restore continuity during an active run")
        if not all(isinstance(record, CompletedRunContinuity) for record in records):
            raise TypeError("continuity records must be CompletedRunContinuity")
        retained = records[-self._max_retained_completed_runs :]
        if self._max_retained_completed_runs == 0:
            retained = ()
        self._completed_run_continuity = [
            (
                UserMessage(record.task),
                AssistantMessage(record.final_response),
            )
            for record in retained
        ]

    def start_run(self, message: UserMessage) -> None:
        """Start one Run with fresh transient history."""
        if self._run_active:
            raise ContextOrderError("cannot start a run while another run is active")
        self._last_model_context_size = None
        self._pending_tool_calls.clear()
        self._pending_candidate = None
        self._messages = [message]
        self._project_instruction = (
            self._root_project_instructions.load()
            if self._root_project_instructions is not None
            else None
        )
        self._run_active = True
        self._history_incomplete = False

    def end_run(self, *, completed: bool) -> None:
        """Finalize continuity and clear all current-Run transient state."""
        self._last_model_context_size = None
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
        self._project_instruction = None
        self._pending_tool_calls.clear()
        self._pending_candidate = None
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

    def record_candidate_message(self, message: AssistantMessage) -> None:
        """Record and protect the one hidden completion-audit candidate."""
        if message.tool_calls:
            raise ContextOrderError("completion candidate must not contain tool calls")
        if self._pending_candidate is not None:
            raise ContextOrderError("a completion candidate is already pending")
        self.record_assistant_message(message)
        self._pending_candidate = message

    def clear_pending_candidate(self) -> None:
        """Stop protecting a candidate after terminal audit resolution."""
        self._pending_candidate = None

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
        return self._build_bounded_messages(
            additional_messages,
            project_tool_results=False,
        )

    def _build_bounded_messages(
        self,
        additional_messages: tuple[InternalMessage, ...],
        *,
        project_tool_results: bool,
    ) -> tuple[InternalMessage, ...]:
        """Evict whole raw units using their eventual model-visible size."""
        continuity = list(self._completed_run_continuity)
        current_units = self._current_message_units()
        additional_units = [(message,) for message in additional_messages]

        def visible_units() -> list[tuple[InternalMessage, ...]]:
            units = [*continuity, *current_units, *additional_units]
            if not project_tool_results:
                return units
            return [self._project_unit(unit) for unit in units]

        while continuity and self._units_size(visible_units()) > self._max_context_chars:
            continuity.pop(0)
            self._history_incomplete = True

        protected = self._protected_current_unit_indexes(current_units)
        index = 0
        while self._units_size(visible_units()) > self._max_context_chars:
            while index < len(current_units) and index in protected:
                index += 1
            if index >= len(current_units):
                if project_tool_results:
                    self._record_model_context_size(visible_units())
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
        result_units = visible_units()
        if project_tool_results:
            self._record_model_context_size(result_units)
        return tuple(
            message
            for unit in result_units
            for message in unit
        )

    def _record_model_context_size(
        self, units: list[tuple[InternalMessage, ...]],
    ) -> None:
        self._last_model_context_size = ModelContextSize(
            chars=self._units_size(units),
            limit=self._max_context_chars,
            reasoning_chars=sum(
                len(message.provider_reasoning_content or "")
                for unit in units for message in unit
                if isinstance(message, AssistantMessage)
            ),
        )

    def build_model_messages(
        self,
        *,
        repeated_action_warning: str | None = None,
        completion_audit_active: bool = False,
        corrective_instruction: str | None = None,
    ) -> tuple[InternalMessage, ...]:
        """Build a bounded snapshot with prefix and optional Runtime instruction."""

        prefix = build_system_prefix(
            history_incomplete=self._history_incomplete,
            repeated_action_warning=repeated_action_warning,
            completion_audit_active=False,
            corrective_instruction=corrective_instruction,
        )
        tail_instructions = self._tail_instructions(
            completion_audit_active=completion_audit_active,
            corrective_instruction=corrective_instruction,
        )
        project_instructions = (
            (self._project_instruction,)
            if self._project_instruction is not None
            else ()
        )
        previously_incomplete = self._history_incomplete
        messages_with_request_instructions = self._build_bounded_messages(
            (prefix, *project_instructions, *tail_instructions),
            project_tool_results=True,
        )
        if not previously_incomplete and self._history_incomplete:
            prefix = build_system_prefix(
                history_incomplete=True,
                repeated_action_warning=repeated_action_warning,
                completion_audit_active=False,
                corrective_instruction=corrective_instruction,
            )
            messages_with_request_instructions = self._build_bounded_messages(
                (prefix, *project_instructions, *tail_instructions),
                project_tool_results=True,
            )
        instruction_count = 1 + len(project_instructions) + len(tail_instructions)
        retained_history = messages_with_request_instructions[:-instruction_count]
        return (
            prefix,
            *project_instructions,
            *retained_history,
            *tail_instructions,
        )

    @staticmethod
    def _tail_instructions(
        *,
        completion_audit_active: bool,
        corrective_instruction: str | None,
    ) -> tuple[RuntimeInstructionMessage, ...]:
        instructions: list[RuntimeInstructionMessage] = []
        if completion_audit_active:
            instructions.append(
                RuntimeInstructionMessage(COMPLETION_AUDIT_INSTRUCTION)
            )
        return tuple(instructions)

    @staticmethod
    def _project_unit(
        unit: tuple[InternalMessage, ...],
    ) -> tuple[InternalMessage, ...]:
        return tuple(
            project_tool_result_message(message)
            if isinstance(message, ToolResultMessage)
            else message
            for message in unit
        )

    def _completed_run_summary(self) -> tuple[InternalMessage, ...] | None:
        if not self._messages or not isinstance(self._messages[0], UserMessage):
            return None
        final = self._messages[-1]
        if not isinstance(final, AssistantMessage) or final.tool_calls:
            return None
        return (
            self._messages[0],
            AssistantMessage(text=final.text, tool_calls=()),
        )

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

    def _protected_current_unit_indexes(
        self,
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
        if self._pending_candidate is not None:
            protected.update(
                index
                for index, unit in enumerate(units)
                if any(message is self._pending_candidate for message in unit)
            )
        return protected

    @staticmethod
    def _is_clarification_unit(unit: tuple[InternalMessage, ...]) -> bool:
        if len(unit) != 2 or not isinstance(unit[0], AssistantMessage):
            return False
        return any(call.name == "ask_user" for call in unit[0].tool_calls)

    @staticmethod
    def _units_size(units: list[tuple[InternalMessage, ...]]) -> int:
        """Return one centralized provider-neutral character approximation."""
        size = 0
        for unit in units:
            for message in unit:
                size += len(repr(message))
                if isinstance(message, AssistantMessage):
                    size += len(message.provider_reasoning_content or "")
        return size


__all__ = [
    "CompletedRunContinuity",
    "ContextLimitError",
    "ContextManager",
    "ContextOrderError",
]
