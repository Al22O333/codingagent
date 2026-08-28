"""Agent runtime lifecycle, model orchestration, and Tool dispatch."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from math import isfinite
from time import monotonic, sleep
from uuid import uuid4

from pydantic import ValidationError

from .ask_user import AskUserArguments
from .constraints import (
    ConstraintDecision,
    ExplicitConstraintSnapshot,
    apply_constraint_update,
    normalize_explicit_constraint_update,
)
from .context import ContextManager
from .interaction import (
    ConfirmationDecision,
    ConfirmationRequest,
    ClarificationRequest,
    ClarificationStatus,
    UserInteraction,
    UserInteractionError,
)
from .model_client import (
    FatalProviderError,
    ModelClient,
    ModelProtocolError,
    TransientProviderError,
)
from .policy import PermissionCheckResult, PermissionDecision, PolicyEngine
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
    WAITING_FOR_USER = "WAITING_FOR_USER"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class TerminationReason(StrEnum):
    """Normalized terminal reasons for an Agent Run."""

    PROTOCOL_FAILURE = "PROTOCOL_FAILURE"
    PROVIDER_FAILURE = "PROVIDER_FAILURE"
    LIMIT_REACHED = "LIMIT_REACHED"
    USER_CANCELLATION = "USER_CANCELLATION"
    USER_INTERACTION_FAILURE = "USER_INTERACTION_FAILURE"
    RUNTIME_FAILURE = "RUNTIME_FAILURE"


class WaitReason(StrEnum):
    """Reasons represented beneath the single WAITING_FOR_USER state."""

    PERMISSION_CONFIRMATION = "PERMISSION_CONFIRMATION"
    CLARIFICATION = "CLARIFICATION"


@dataclass(frozen=True, slots=True)
class PendingAction:
    """One immutable exact action awaiting one-time user authorization."""

    prepared_call: PreparedToolCall
    permission_reason: PermissionCheckResult


@dataclass(frozen=True, slots=True)
class _ToolDispatchResult:
    result: ToolResult
    ends_batch: bool


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
    """Mutable state owned and advanced by AgentRuntime."""

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
    wait_reason: WaitReason | None = None
    pending_user_request: ConfirmationRequest | ClarificationRequest | None = None
    pending_action: PendingAction | None = None
    paused_duration_seconds: float = 0.0


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
    """Sole orchestrator for Agent Runs and Tool-driven model turns."""

    def __init__(
        self,
        model_client: ModelClient,
        context_manager: ContextManager,
        tool_registry: ToolRegistry,
        limits: RuntimeLimits,
        workspace_resolver: WorkspacePathResolver | None = None,
        *,
        policy_engine: PolicyEngine,
        user_interaction: UserInteraction,
        clock: Callable[[], float] = monotonic,
        sleep_fn: Callable[[float], None] = sleep,
        transport_retry_base_delay_seconds: float = 0.25,
        transport_retry_max_delay_seconds: float = 2.0,
        tool_activity: Callable[[ToolCall], None] | None = None,
    ) -> None:
        if (
            not isfinite(transport_retry_base_delay_seconds)
            or transport_retry_base_delay_seconds < 0
        ):
            raise ValueError("transport retry base delay must be finite and non-negative")
        if (
            not isfinite(transport_retry_max_delay_seconds)
            or transport_retry_max_delay_seconds < transport_retry_base_delay_seconds
        ):
            raise ValueError(
                "transport retry maximum delay must be finite and cover the base delay"
            )
        self._model_client = model_client
        self._context_manager = context_manager
        self._tool_registry = tool_registry
        self._limits = limits
        self._workspace_resolver = workspace_resolver
        self._policy_engine = policy_engine
        self._user_interaction = user_interaction
        self._clock = clock
        self._sleep = sleep_fn
        self._transport_retry_base_delay_seconds = (
            transport_retry_base_delay_seconds
        )
        self._transport_retry_max_delay_seconds = transport_retry_max_delay_seconds
        self._tool_activity = tool_activity
        self.session = Session()

    def run(self, task: str) -> AgentRun:
        """Run one user task until a final response or terminal failure."""
        agent_run = AgentRun(run_id=str(uuid4()), current_task=task)
        run_started_at = self._clock()
        try:
            self.session._add_run(agent_run)
            return self._run_until_terminal(agent_run, task, run_started_at)
        except KeyboardInterrupt:
            self._terminate_run(
                agent_run,
                RunState.CANCELLED,
                TerminationReason.USER_CANCELLATION,
                run_started_at,
            )
        except UserInteractionError as error:
            self._terminate_run(
                agent_run,
                RunState.FAILED,
                TerminationReason.USER_INTERACTION_FAILURE,
                run_started_at,
                error,
            )
        except Exception as error:
            self._terminate_run(
                agent_run,
                RunState.FAILED,
                TerminationReason.RUNTIME_FAILURE,
                run_started_at,
                error,
            )
        finally:
            if agent_run.state in {
                RunState.COMPLETED,
                RunState.FAILED,
                RunState.CANCELLED,
            }:
                self._clear_pending_state(agent_run)
                self._context_manager.end_run(
                    completed=agent_run.state is RunState.COMPLETED
                )
        return agent_run

    def _run_until_terminal(
        self,
        agent_run: AgentRun,
        task: str,
        run_started_at: float,
    ) -> AgentRun:
        """Execute the normal lifecycle beneath the public terminal boundary."""
        self._context_manager.start_run(UserMessage(text=task))
        self._apply_trusted_user_input(agent_run, task)
        corrective_feedback: SystemMessage | None = None

        while agent_run.state is RunState.RUNNING:
            if self._model_budget_exhausted(agent_run, run_started_at):
                return agent_run

            additional_messages = (
                (corrective_feedback,) if corrective_feedback is not None else ()
            )
            messages = self._context_manager.build_messages(additional_messages)
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

    def _terminate_run(
        self,
        agent_run: AgentRun,
        state: RunState,
        reason: TerminationReason,
        run_started_at: float,
        error: Exception | None = None,
    ) -> None:
        agent_run.state = state
        agent_run.termination_reason = reason
        agent_run.last_error = error
        self._clear_pending_state(agent_run)
        self._refresh_active_duration(agent_run, run_started_at)

    @staticmethod
    def _clear_pending_state(agent_run: AgentRun) -> None:
        agent_run.wait_reason = None
        agent_run.pending_user_request = None
        agent_run.pending_action = None

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

            if self._tool_activity is not None:
                self._tool_activity(tool_call)
            dispatch = self._dispatch_tool_call(tool_call, agent_run)
            results.append(dispatch.result)
            if dispatch.ends_batch:
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
                self._sleep(self._transport_retry_delay(retries))
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

    def _transport_retry_delay(self, retry_index: int) -> float:
        delay = self._transport_retry_base_delay_seconds
        for _ in range(retry_index):
            delay = min(delay * 2, self._transport_retry_max_delay_seconds)
            if delay >= self._transport_retry_max_delay_seconds:
                break
        return delay

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
        agent_run.active_duration_seconds = max(
            0.0,
            self._clock() - run_started_at - agent_run.paused_duration_seconds,
        )

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

    def _dispatch_tool_call(
        self,
        tool_call: ToolCall,
        agent_run: AgentRun,
    ) -> _ToolDispatchResult:
        agent_run.tool_call_attempts += 1

        try:
            tool = self._tool_registry.get(tool_call.name)
        except UnknownToolError:
            return self._dispatch_result(ToolResult(
                call_id=tool_call.call_id,
                tool_name=tool_call.name,
                outcome=ToolOutcome.VALIDATION_ERROR,
                error=ToolError(
                    code="UNKNOWN_TOOL",
                    message=f"unknown tool: {tool_call.name}",
                ),
            ))

        try:
            arguments = tool.validate(tool_call.raw_arguments)
        except ValidationError as error:
            return self._dispatch_result(ToolResult(
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
            ))

        if tool.spec.kind is ToolKind.INTERACTION:
            if tool.spec.name != "ask_user" or not isinstance(
                arguments, AskUserArguments
            ):
                return self._dispatch_result(ToolResult(
                    call_id=tool_call.call_id,
                    tool_name=tool_call.name,
                    outcome=ToolOutcome.VALIDATION_ERROR,
                    error=ToolError(
                        code="UNSUPPORTED_INTERACTION_TOOL",
                        message="v1 supports only the ask_user Interaction Tool",
                    ),
                ))
            return self._ask_user(tool_call, arguments, agent_run)

        if tool.spec.kind is not ToolKind.LOCAL or not isinstance(tool, LocalTool):
            return self._dispatch_result(ToolResult(
                call_id=tool_call.call_id,
                tool_name=tool_call.name,
                outcome=ToolOutcome.VALIDATION_ERROR,
                error=ToolError(
                    code="UNSUPPORTED_TOOL_KIND",
                    message="runtime only dispatches executable LOCAL tools",
                ),
            ))

        try:
            prepared = tool.prepare(tool_call.call_id, arguments)
        except Exception:
            return self._dispatch_result(self._operation_failure(
                tool_call.call_id,
                tool_call.name,
                ToolError(
                    code="INTERNAL_TOOL_ERROR",
                    message="local tool preparation failed unexpectedly",
                ),
            ))

        if isinstance(prepared, ToolError):
            return self._dispatch_result(self._operation_failure(
                tool_call.call_id,
                tool_call.name,
                prepared,
            ))

        if (
            not isinstance(prepared, PreparedToolCall)
            or prepared.call_id != tool_call.call_id
            or prepared.tool_identity != tool.spec
            or prepared.validated_arguments is not arguments
        ):
            return self._dispatch_result(self._operation_failure(
                tool_call.call_id,
                tool_call.name,
                ToolError(
                    code="INTERNAL_TOOL_ERROR",
                    message="local tool returned an invalid prepared action",
                ),
            ))

        constraint_result = self._policy_engine.check_explicit_constraints(
            prepared,
            agent_run.explicit_task_constraints,
        )
        if constraint_result.decision is ConstraintDecision.REJECT:
            return self._dispatch_result(ToolResult(
                call_id=tool_call.call_id,
                tool_name=tool_call.name,
                outcome=ToolOutcome.POLICY_REJECTED,
                error=ToolError(
                    code=constraint_result.reason_code or "EXPLICIT_TASK_CONSTRAINT",
                    message=constraint_result.message
                    or "action violates an explicit task constraint",
                ),
            ))

        permission_result = self._policy_engine.check_risk_permission(prepared)
        if permission_result.decision is PermissionDecision.DENY:
            return self._dispatch_result(ToolResult(
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
            ))

        if permission_result.decision is PermissionDecision.CONFIRM:
            return self._confirm_and_execute(
                tool,
                prepared,
                permission_result,
                agent_run,
            )

        return self._execute_prepared(tool, prepared)

    def _ask_user(
        self,
        tool_call: ToolCall,
        arguments: AskUserArguments,
        agent_run: AgentRun,
    ) -> _ToolDispatchResult:
        request = ClarificationRequest(
            call_id=tool_call.call_id,
            question=arguments.question,
        )
        agent_run.pending_user_request = request
        agent_run.wait_reason = WaitReason.CLARIFICATION
        agent_run.state = RunState.WAITING_FOR_USER
        wait_started_at = self._clock()

        try:
            response = self._user_interaction.ask(request)
        except KeyboardInterrupt:
            response = None
        finally:
            agent_run.paused_duration_seconds += max(
                0.0, self._clock() - wait_started_at
            )

        try:
            if response is not None and response.status is ClarificationStatus.ANSWERED:
                assert response.answer is not None
                agent_run.state = RunState.RUNNING
                self.apply_user_clarification(agent_run, response.answer)
                return self._dispatch_result(
                    ToolResult(
                        call_id=tool_call.call_id,
                        tool_name=tool_call.name,
                        outcome=ToolOutcome.SUCCESS,
                        content={"answer": response.answer},
                    ),
                    ends_batch=True,
                )
            agent_run.state = RunState.CANCELLED
            agent_run.termination_reason = TerminationReason.USER_CANCELLATION
            return self._dispatch_result(
                self._operation_failure(
                    tool_call.call_id,
                    tool_call.name,
                    ToolError(
                        code="USER_CANCELLED_CLARIFICATION",
                        message="user cancelled during clarification",
                    ),
                )
            )
        finally:
            agent_run.wait_reason = None
            agent_run.pending_user_request = None

    def _confirm_and_execute(
        self,
        tool: LocalTool,
        prepared: PreparedToolCall,
        permission_result: PermissionCheckResult,
        agent_run: AgentRun,
    ) -> _ToolDispatchResult:
        pending = PendingAction(prepared, permission_result)
        request = ConfirmationRequest(
            call_id=prepared.call_id,
            tool_name=prepared.tool_identity.name,
            action_summary=self._action_summary(prepared),
            reason_code=permission_result.reason_code or "RISK_PERMISSION",
            risk_summary=permission_result.risk_summary or "Confirmation required",
        )
        agent_run.pending_action = pending
        agent_run.pending_user_request = request
        agent_run.wait_reason = WaitReason.PERMISSION_CONFIRMATION
        agent_run.state = RunState.WAITING_FOR_USER
        wait_started_at = self._clock()

        try:
            decision = self._user_interaction.confirm(request)
        except KeyboardInterrupt:
            decision = ConfirmationDecision.CANCEL
        finally:
            agent_run.paused_duration_seconds += max(
                0.0, self._clock() - wait_started_at
            )

        try:
            if decision is ConfirmationDecision.APPROVE:
                agent_run.state = RunState.RUNNING
                return self._execute_prepared(tool, pending.prepared_call, ends_batch=True)
            if decision is ConfirmationDecision.REJECT:
                agent_run.state = RunState.RUNNING
                return self._dispatch_result(
                    ToolResult(
                        call_id=prepared.call_id,
                        tool_name=prepared.tool_identity.name,
                        outcome=ToolOutcome.POLICY_REJECTED,
                        error=ToolError(
                            code="USER_REJECTED_CONFIRMATION",
                            message="user rejected the exact prepared action",
                        ),
                    )
                )
            agent_run.state = RunState.CANCELLED
            agent_run.termination_reason = TerminationReason.USER_CANCELLATION
            return self._dispatch_result(
                ToolResult(
                    call_id=prepared.call_id,
                    tool_name=prepared.tool_identity.name,
                    outcome=ToolOutcome.POLICY_REJECTED,
                    error=ToolError(
                        code="USER_CANCELLED_CONFIRMATION",
                        message="user cancelled during permission confirmation",
                    ),
                )
            )
        finally:
            self._clear_pending_state(agent_run)

    def _execute_prepared(
        self,
        tool: LocalTool,
        prepared: PreparedToolCall,
        *,
        ends_batch: bool = False,
    ) -> _ToolDispatchResult:

        try:
            execution = tool.execute(prepared)
        except Exception:
            return self._dispatch_result(self._operation_failure(
                prepared.call_id,
                prepared.tool_identity.name,
                ToolError(
                    code="INTERNAL_TOOL_ERROR",
                    message="local tool execution failed unexpectedly",
                ),
            ))

        if not isinstance(execution, ToolExecutionResult):
            return self._dispatch_result(self._operation_failure(
                prepared.call_id,
                prepared.tool_identity.name,
                ToolError(
                    code="INTERNAL_TOOL_ERROR",
                    message="local tool returned an invalid execution result",
                ),
            ))

        return self._dispatch_result(ToolResult(
            call_id=prepared.call_id,
            tool_name=prepared.tool_identity.name,
            outcome=execution.outcome,
            content=execution.content,
            error=execution.error,
        ), ends_batch=ends_batch)

    @staticmethod
    def _dispatch_result(
        result: ToolResult,
        *,
        ends_batch: bool | None = None,
    ) -> _ToolDispatchResult:
        if ends_batch is None:
            ends_batch = result.outcome is not ToolOutcome.SUCCESS
        return _ToolDispatchResult(result=result, ends_batch=ends_batch)

    @staticmethod
    def _action_summary(prepared: PreparedToolCall) -> str:
        arguments = prepared.validated_arguments.model_dump(mode="json")
        rendered = ", ".join(
            f"{key}={value!r}" for key, value in sorted(arguments.items())
        )
        return f"{prepared.tool_identity.name}({rendered})"

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
    "PendingAction",
    "Session",
    "TerminationReason",
    "WaitReason",
]
