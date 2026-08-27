"""AgentRuntime with a minimal single-ToolCall local dispatch loop."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from time import monotonic
from uuid import uuid4

from pydantic import ValidationError

from .constraints import (
    ConstraintDecision,
    ExplicitConstraintSnapshot,
    apply_constraint_update,
    normalize_explicit_constraint_update,
)
from .context import ContextManager
from .model_client import (
    FatalProviderError,
    ModelClient,
    ModelProtocolError,
    TransientProviderError,
)
from .policy import PermissionDecision, PolicyEngine
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
    SystemMessage,
    UserMessage,
)
from .tooling import (
    LocalTool,
    PreparedToolCall,
    ToolExecutionResult,
    ToolRegistry,
    UnknownToolError,
)
from .workspace import WorkspacePathResolver


class RunState(StrEnum):
    """Top-level lifecycle states needed by the v1 runtime."""

    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class TerminationReason(StrEnum):
    """Termination reasons exercised by the minimal runtime slice."""

    PROTOCOL_FAILURE = "PROTOCOL_FAILURE"
    PROVIDER_FAILURE = "PROVIDER_FAILURE"
    LIMIT_REACHED = "LIMIT_REACHED"
    USER_CANCELLATION = "USER_CANCELLATION"


@dataclass(frozen=True, slots=True)
class RuntimeLimits:
    """Explicit hard limits supplied by configuration."""

    max_model_turns: int
    max_tool_call_attempts: int
    max_active_run_duration_seconds: float
    max_transport_retries: int
    max_consecutive_protocol_errors: int

    def __post_init__(self) -> None:
        if self.max_model_turns < 1:
            raise ValueError("max_model_turns must be at least 1")
        if self.max_tool_call_attempts < 1:
            raise ValueError("max_tool_call_attempts must be at least 1")
        if self.max_active_run_duration_seconds <= 0:
            raise ValueError("max_active_run_duration_seconds must be positive")
        if self.max_transport_retries < 0:
            raise ValueError("max_transport_retries must not be negative")
        if self.max_consecutive_protocol_errors < 1:
            raise ValueError("max_consecutive_protocol_errors must be at least 1")


@dataclass(slots=True)
class AgentRun:
    """Minimal mutable state owned and advanced by AgentRuntime."""

    run_id: str
    current_task: str
    state: RunState = RunState.RUNNING
    model_turns: int = 0
    tool_call_attempts: int = 0
    active_duration_seconds: float = 0.0
    consecutive_protocol_errors: int = 0
    final_response: str | None = None
    termination_reason: TerminationReason | None = None
    limit_reached: str | None = None
    last_error: Exception | None = None
    explicit_user_clarifications: list[str] = field(default_factory=list)
    explicit_scope_updates: list[str] = field(default_factory=list)
    explicit_task_constraints: ExplicitConstraintSnapshot = field(
        default_factory=ExplicitConstraintSnapshot
    )


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
        limits: RuntimeLimits,
        workspace_resolver: WorkspacePathResolver | None = None,
        *,
        policy_engine: PolicyEngine,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._model_client = model_client
        self._context_manager = context_manager
        self._tool_registry = tool_registry
        self._limits = limits
        self._workspace_resolver = workspace_resolver
        self._policy_engine = policy_engine
        self._clock = clock
        self.session = Session()

    def run(self, task: str) -> AgentRun:
        """Run one user task until a final response or current-slice failure."""
        agent_run = AgentRun(run_id=str(uuid4()), current_task=task)
        run_started_at = self._clock()
        self.session._add_run(agent_run)
        self._context_manager.record_user_message(UserMessage(text=task))
        self._apply_trusted_user_input(agent_run, task)
        corrective_feedback: SystemMessage | None = None

        while agent_run.state is RunState.RUNNING:
            if self._model_budget_exhausted(agent_run, run_started_at):
                return agent_run

            messages = self._context_manager.build_messages()
            if corrective_feedback is not None:
                messages = messages + (corrective_feedback,)
            request = ModelRequest(
                messages=messages,
                tools=self._tool_registry.specs(),
            )

            try:
                response = self._complete_model_request(
                    request,
                    agent_run,
                    run_started_at,
                )
            except KeyboardInterrupt:
                agent_run.state = RunState.CANCELLED
                agent_run.termination_reason = TerminationReason.USER_CANCELLATION
                self._refresh_active_duration(agent_run, run_started_at)
                return agent_run
            except ModelProtocolError as error:
                if self._record_protocol_error(
                    agent_run,
                    error,
                    run_started_at,
                ):
                    return agent_run
                corrective_feedback = self._corrective_feedback()
                continue

            if response is None:
                return agent_run

            protocol_error = self._response_protocol_error(response)
            if protocol_error is not None:
                if self._record_protocol_error(
                    agent_run,
                    protocol_error,
                    run_started_at,
                ):
                    return agent_run
                corrective_feedback = self._corrective_feedback()
                continue

            agent_run.consecutive_protocol_errors = 0
            agent_run.last_error = None
            corrective_feedback = None

            if response.tool_calls:
                assistant_message = AssistantMessage(
                    text=response.text,
                    tool_calls=response.tool_calls,
                )
                self._context_manager.record_assistant_message(assistant_message)
                tool_results = self._execute_tool_batch(
                    response.tool_calls,
                    agent_run,
                    run_started_at,
                )
                self._context_manager.record_tool_result_message(
                    ToolResultMessage(results=tool_results)
                )
                if agent_run.state is not RunState.RUNNING:
                    return agent_run
                continue

            assistant_message = AssistantMessage(text=response.text)
            self._context_manager.record_assistant_message(assistant_message)
            agent_run.final_response = response.text
            agent_run.state = RunState.COMPLETED
            self._refresh_active_duration(agent_run, run_started_at)
            return agent_run

        return agent_run

    def _execute_tool_batch(
        self,
        tool_calls: tuple[ToolCall, ...],
        agent_run: AgentRun,
        run_started_at: float,
    ) -> tuple[ToolResult, ...]:
        results: list[ToolResult] = []
        batch_stopped = False

        for tool_call in tool_calls:
            if batch_stopped:
                results.append(self._not_executed(tool_call))
                continue

            if self._tool_budget_exhausted(agent_run, run_started_at):
                results.append(self._not_executed(tool_call))
                batch_stopped = True
                continue

            result = self._dispatch_local_tool_call(tool_call, agent_run)
            results.append(result)
            if result.outcome is not ToolOutcome.SUCCESS:
                batch_stopped = True

        return tuple(results)

    def _complete_model_request(
        self,
        request: ModelRequest,
        agent_run: AgentRun,
        run_started_at: float,
    ) -> ModelResponse | None:
        retries = 0
        while True:
            if self._active_duration_exhausted(agent_run, run_started_at):
                return None
            try:
                response = self._model_client.complete(request)
            except TransientProviderError as error:
                agent_run.last_error = error
                if retries >= self._limits.max_transport_retries:
                    agent_run.state = RunState.FAILED
                    agent_run.termination_reason = TerminationReason.PROVIDER_FAILURE
                    self._refresh_active_duration(agent_run, run_started_at)
                    return None
                retries += 1
                continue
            except FatalProviderError as error:
                agent_run.last_error = error
                agent_run.state = RunState.FAILED
                agent_run.termination_reason = TerminationReason.PROVIDER_FAILURE
                self._refresh_active_duration(agent_run, run_started_at)
                return None
            except ModelProtocolError:
                agent_run.model_turns += 1
                raise

            agent_run.model_turns += 1
            return response

    def _model_budget_exhausted(
        self,
        agent_run: AgentRun,
        run_started_at: float,
    ) -> bool:
        if self._active_duration_exhausted(agent_run, run_started_at):
            return True
        if agent_run.model_turns >= self._limits.max_model_turns:
            self._fail_limit(agent_run, "max_model_turns", run_started_at)
            return True
        return False

    def _tool_budget_exhausted(
        self,
        agent_run: AgentRun,
        run_started_at: float,
    ) -> bool:
        if self._active_duration_exhausted(agent_run, run_started_at):
            return True
        if agent_run.tool_call_attempts >= self._limits.max_tool_call_attempts:
            self._fail_limit(agent_run, "max_tool_call_attempts", run_started_at)
            return True
        return False

    def _active_duration_exhausted(
        self,
        agent_run: AgentRun,
        run_started_at: float,
    ) -> bool:
        self._refresh_active_duration(agent_run, run_started_at)
        if (
            agent_run.active_duration_seconds
            >= self._limits.max_active_run_duration_seconds
        ):
            self._fail_limit(agent_run, "max_active_run_duration", run_started_at)
            return True
        return False

    def _fail_limit(
        self,
        agent_run: AgentRun,
        limit_name: str,
        run_started_at: float,
    ) -> None:
        agent_run.state = RunState.FAILED
        agent_run.termination_reason = TerminationReason.LIMIT_REACHED
        agent_run.limit_reached = limit_name
        self._refresh_active_duration(agent_run, run_started_at)

    def _refresh_active_duration(
        self,
        agent_run: AgentRun,
        run_started_at: float,
    ) -> None:
        agent_run.active_duration_seconds = max(0.0, self._clock() - run_started_at)

    def _record_protocol_error(
        self,
        agent_run: AgentRun,
        error: ModelProtocolError,
        run_started_at: float,
    ) -> bool:
        agent_run.consecutive_protocol_errors += 1
        agent_run.last_error = error
        if (
            agent_run.consecutive_protocol_errors
            >= self._limits.max_consecutive_protocol_errors
        ):
            agent_run.state = RunState.FAILED
            agent_run.termination_reason = TerminationReason.PROTOCOL_FAILURE
            self._refresh_active_duration(agent_run, run_started_at)
            return True
        return False

    @staticmethod
    def _response_protocol_error(response: ModelResponse) -> ModelProtocolError | None:
        if response.tool_calls:
            call_ids = [call.call_id for call in response.tool_calls]
            if len(call_ids) != len(set(call_ids)):
                return ModelProtocolError(
                    "model response contains duplicate tool call ids"
                )
            return None
        if response.text is None or not response.text.strip():
            return ModelProtocolError(
                "model returned no tool calls and no non-blank final text"
            )
        return None

    @staticmethod
    def _corrective_feedback() -> SystemMessage:
        return SystemMessage(
            text=(
                "Your previous response was invalid. Produce a valid response "
                "using the provided tool protocol."
            )
        )

    @staticmethod
    def _not_executed(tool_call: ToolCall) -> ToolResult:
        return ToolResult(
            call_id=tool_call.call_id,
            tool_name=tool_call.name,
            outcome=ToolOutcome.NOT_EXECUTED,
            error=ToolError(
                code="BATCH_ABORTED",
                message=(
                    "tool call was not executed because an earlier call ended "
                    "the batch"
                ),
            ),
        )

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
            prepared = tool.prepare(tool_call.call_id, arguments)
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

        if (
            not isinstance(prepared, PreparedToolCall)
            or prepared.call_id != tool_call.call_id
            or prepared.tool_identity != tool.spec
            or prepared.validated_arguments is not arguments
        ):
            return self._operation_failure(
                tool_call.call_id,
                tool_call.name,
                ToolError(
                    code="INTERNAL_TOOL_ERROR",
                    message="local tool returned an invalid prepared action",
                ),
            )

        constraint_result = self._policy_engine.check_explicit_constraints(
            prepared,
            agent_run.explicit_task_constraints,
        )
        if constraint_result.decision is ConstraintDecision.REJECT:
            return ToolResult(
                call_id=tool_call.call_id,
                tool_name=tool_call.name,
                outcome=ToolOutcome.POLICY_REJECTED,
                error=ToolError(
                    code=constraint_result.reason_code or "EXPLICIT_TASK_CONSTRAINT",
                    message=constraint_result.message
                    or "action violates an explicit task constraint",
                ),
            )

        permission_result = self._policy_engine.check_risk_permission(prepared)
        if permission_result.decision is not PermissionDecision.ALLOW:
            return ToolResult(
                call_id=tool_call.call_id,
                tool_name=tool_call.name,
                outcome=ToolOutcome.POLICY_REJECTED,
                error=ToolError(
                    code=permission_result.reason_code or "RISK_PERMISSION",
                    message=permission_result.message
                    or "action did not pass Risk Permission",
                    details={
                        "permission_decision": permission_result.decision.value,
                        "risk_summary": permission_result.risk_summary,
                        "matched_rules": permission_result.matched_rules,
                    },
                ),
            )

        try:
            execution = tool.execute(prepared)
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

    def apply_user_clarification(
        self,
        agent_run: AgentRun,
        answer: str,
    ) -> bool:
        """Apply one trusted same-Run user answer through the closed normalizer."""
        if agent_run not in self.session.runs:
            raise ValueError("clarification target is not owned by this Session")
        if agent_run.state is not RunState.RUNNING:
            raise ValueError("clarification target is not an active Agent Run")
        agent_run.explicit_user_clarifications.append(answer)
        return self._apply_trusted_user_input(
            agent_run,
            answer,
            record_scope_update=True,
        )

    def _apply_trusted_user_input(
        self,
        agent_run: AgentRun,
        user_input: str,
        *,
        record_scope_update: bool = False,
    ) -> bool:
        update = normalize_explicit_constraint_update(
            user_input,
            self._workspace_resolver,
        )
        if update is None:
            return False
        agent_run.explicit_task_constraints = apply_constraint_update(
            agent_run.explicit_task_constraints,
            update,
        )
        if record_scope_update and update.write_scopes is not None:
            agent_run.explicit_scope_updates.append(user_input)
        return True

__all__ = [
    "AgentRun",
    "AgentRuntime",
    "ModelProtocolError",
    "RuntimeLimits",
    "RunState",
    "Session",
    "TerminationReason",
]
