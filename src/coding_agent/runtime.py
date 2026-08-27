"""Minimal AgentRuntime for a user-to-model-to-final run."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import uuid4

from .context import ContextManager
from .model_client import ModelClient
from .protocol import AssistantMessage, ModelRequest, ModelResponse, UserMessage


class RunState(StrEnum):
    """Top-level lifecycle states needed by the v1 runtime."""

    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class TerminationReason(StrEnum):
    """Termination reasons exercised by the minimal runtime slice."""

    PROTOCOL_FAILURE = "PROTOCOL_FAILURE"
    USER_CANCELLATION = "USER_CANCELLATION"


class ModelProtocolError(ValueError):
    """A model response cannot be treated as a valid final response."""


@dataclass(slots=True)
class AgentRun:
    """Minimal mutable state owned and advanced by AgentRuntime."""

    run_id: str
    current_task: str
    state: RunState = RunState.RUNNING
    model_turns: int = 0
    consecutive_protocol_errors: int = 0
    final_response: str | None = None
    termination_reason: TerminationReason | None = None
    last_error: ModelProtocolError | None = None


@dataclass(slots=True)
class Session:
    """In-process collection of sequential Agent Runs."""

    session_id: str = field(default_factory=lambda: str(uuid4()))
    _runs: list[AgentRun] = field(default_factory=list, repr=False)

    @property
    def runs(self) -> tuple[AgentRun, ...]:
        """Return runs in creation order without exposing the mutable list."""
        return tuple(self._runs)

    def _add_run(self, run: AgentRun) -> None:
        self._runs.append(run)


class AgentRuntime:
    """Sole orchestrator for the current model-to-final vertical slice."""

    def __init__(
        self,
        model_client: ModelClient,
        context_manager: ContextManager,
    ) -> None:
        self._model_client = model_client
        self._context_manager = context_manager
        self.session = Session()

    def run(self, task: str) -> AgentRun:
        """Run one user task until a final response or current-slice failure."""
        agent_run = AgentRun(run_id=str(uuid4()), current_task=task)
        self.session._add_run(agent_run)
        self._context_manager.record_user_message(UserMessage(text=task))

        request = ModelRequest(messages=self._context_manager.build_messages())

        try:
            response = self._model_client.complete(request)
        except KeyboardInterrupt:
            agent_run.state = RunState.CANCELLED
            agent_run.termination_reason = TerminationReason.USER_CANCELLATION
            return agent_run

        agent_run.model_turns += 1

        if response.tool_calls:
            raise NotImplementedError("tool turns are outside Step 5 scope")

        if not self._is_final_response(response):
            error = ModelProtocolError(
                "model returned no tool calls and no non-blank final text"
            )
            agent_run.consecutive_protocol_errors += 1
            agent_run.last_error = error
            agent_run.state = RunState.FAILED
            agent_run.termination_reason = TerminationReason.PROTOCOL_FAILURE
            return agent_run

        assistant_message = AssistantMessage(text=response.text)
        self._context_manager.record_assistant_message(assistant_message)
        agent_run.final_response = response.text
        agent_run.consecutive_protocol_errors = 0
        agent_run.state = RunState.COMPLETED
        return agent_run

    @staticmethod
    def _is_final_response(response: ModelResponse) -> bool:
        return response.text is not None and bool(response.text.strip())


__all__ = [
    "AgentRun",
    "AgentRuntime",
    "ModelProtocolError",
    "RunState",
    "Session",
    "TerminationReason",
]
