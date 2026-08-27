"""AgentRuntime with a minimal single-ToolCall local dispatch loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import uuid4

from pydantic import ValidationError

from .context import ContextManager
from .model_client import ModelClient
from .protocol import (
    AssistantMessage,
    ModelRequest,
    ModelResponse,
    ToolCall,
    ToolError,
    ToolKind,
    ToolOutcome,
    ToolResult,
    ToolResultMessage,
    UserMessage,
)
from .tooling import (
    LocalTool,
    ToolExecutionResult,
    ToolRegistry,
    UnknownToolError,
)
from .workspace import ResolvedPath


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
    tool_call_attempts: int = 0
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
        tool_registry: ToolRegistry,
    ) -> None:
        self._model_client = model_client
        self._context_manager = context_manager
        self._tool_registry = tool_registry
        self.session = Session()

    def run(self, task: str) -> AgentRun:
        """Run one user task until a final response or current-slice failure."""
        agent_run = AgentRun(run_id=str(uuid4()), current_task=task)
        self.session._add_run(agent_run)
        self._context_manager.record_user_message(UserMessage(text=task))

        while agent_run.state is RunState.RUNNING:
            request = ModelRequest(
                messages=self._context_manager.build_messages(),
                tools=self._tool_registry.specs(),
            )

            try:
                response = self._model_client.complete(request)
            except KeyboardInterrupt:
                agent_run.state = RunState.CANCELLED
                agent_run.termination_reason = TerminationReason.USER_CANCELLATION
                return agent_run

            agent_run.model_turns += 1

            if response.tool_calls:
                assistant_message = AssistantMessage(
                    text=response.text,
                    tool_calls=response.tool_calls,
                )
                self._context_manager.record_assistant_message(assistant_message)
                tool_results = self._execute_tool_batch(
                    response.tool_calls,
                    agent_run,
                )
                self._context_manager.record_tool_result_message(
                    ToolResultMessage(results=tool_results)
                )
                continue

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

        return agent_run

    def _execute_tool_batch(
        self,
        tool_calls: tuple[ToolCall, ...],
        agent_run: AgentRun,
    ) -> tuple[ToolResult, ...]:
        results: list[ToolResult] = []
        batch_stopped = False

        for tool_call in tool_calls:
            if batch_stopped:
                results.append(
                    ToolResult(
                        call_id=tool_call.call_id,
                        tool_name=tool_call.name,
                        outcome=ToolOutcome.NOT_EXECUTED,
                        error=ToolError(
                            code="BATCH_ABORTED",
                            message=(
                                "tool call was not executed because an earlier "
                                "call ended the batch"
                            ),
                        ),
                    )
                )
                continue

            result = self._dispatch_local_tool_call(tool_call, agent_run)
            results.append(result)
            if result.outcome is not ToolOutcome.SUCCESS:
                batch_stopped = True

        return tuple(results)

    def _dispatch_local_tool_call(
        self,
        tool_call: ToolCall,
        agent_run: AgentRun,
    ) -> ToolResult:
        agent_run.tool_call_attempts += 1

        try:
            tool = self._tool_registry.get(tool_call.name)
        except UnknownToolError:
            return ToolResult(
                call_id=tool_call.call_id,
                tool_name=tool_call.name,
                outcome=ToolOutcome.VALIDATION_ERROR,
                error=ToolError(
                    code="UNKNOWN_TOOL",
                    message=f"unknown tool: {tool_call.name}",
                ),
            )

        try:
            arguments = tool.validate(tool_call.raw_arguments)
        except ValidationError as error:
            return ToolResult(
                call_id=tool_call.call_id,
                tool_name=tool_call.name,
                outcome=ToolOutcome.VALIDATION_ERROR,
                error=ToolError(
                    code="INVALID_ARGUMENTS",
                    message="tool arguments failed validation",
                    details={
                        "issues": error.errors(
                            include_context=False,
                            include_input=False,
                            include_url=False,
                        )
                    },
                ),
            )

        if tool.spec.kind is not ToolKind.LOCAL or not isinstance(tool, LocalTool):
            return ToolResult(
                call_id=tool_call.call_id,
                tool_name=tool_call.name,
                outcome=ToolOutcome.VALIDATION_ERROR,
                error=ToolError(
                    code="UNSUPPORTED_TOOL_KIND",
                    message="Step 8 runtime only dispatches executable LOCAL tools",
                ),
            )

        try:
            prepared = tool.prepare(arguments)
        except Exception:
            return self._operation_failure(
                tool_call.call_id,
                tool_call.name,
                ToolError(
                    code="INTERNAL_TOOL_ERROR",
                    message="local tool preparation failed unexpectedly",
                ),
            )

        if isinstance(prepared, ToolError):
            return self._operation_failure(
                tool_call.call_id,
                tool_call.name,
                prepared,
            )

        policy_rejection = self._minimal_file_policy(tool_call, prepared)
        if policy_rejection is not None:
            return policy_rejection

        try:
            execution = tool.execute(arguments, prepared)
        except Exception:
            return self._operation_failure(
                tool_call.call_id,
                tool_call.name,
                ToolError(
                    code="INTERNAL_TOOL_ERROR",
                    message="local tool execution failed unexpectedly",
                ),
            )

        if not isinstance(execution, ToolExecutionResult):
            return self._operation_failure(
                tool_call.call_id,
                tool_call.name,
                ToolError(
                    code="INTERNAL_TOOL_ERROR",
                    message="local tool returned an invalid execution result",
                ),
            )

        return ToolResult(
            call_id=tool_call.call_id,
            tool_name=tool_call.name,
            outcome=execution.outcome,
            content=execution.content,
            error=execution.error,
        )

    @staticmethod
    def _minimal_file_policy(
        tool_call: ToolCall,
        prepared: object,
    ) -> ToolResult | None:
        if not isinstance(prepared, ResolvedPath):
            return None
        if not prepared.is_within_workspace:
            code = "WORKSPACE_BOUNDARY"
            message = "File Tool access outside the workspace is prohibited"
        elif prepared.is_sensitive:
            code = "SENSITIVE_PATH_CONFIRMATION_REQUIRED"
            message = "Sensitive Path access requires explicit user confirmation"
        elif prepared.is_protected:
            code = "PROTECTED_PATH_CONFIRMATION_REQUIRED"
            message = "Protected Path access requires explicit user confirmation"
        else:
            return None
        return ToolResult(
            call_id=tool_call.call_id,
            tool_name=tool_call.name,
            outcome=ToolOutcome.POLICY_REJECTED,
            error=ToolError(code=code, message=message),
        )

    @staticmethod
    def _operation_failure(
        call_id: str,
        tool_name: str,
        error: ToolError,
    ) -> ToolResult:
        return ToolResult(
            call_id=call_id,
            tool_name=tool_name,
            outcome=ToolOutcome.OPERATION_FAILURE,
            error=error,
        )

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
