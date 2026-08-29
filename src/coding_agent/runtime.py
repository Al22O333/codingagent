"""Agent runtime lifecycle, model orchestration, and Tool dispatch."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from math import isfinite
from time import monotonic, sleep
from types import MappingProxyType
from uuid import uuid4

from pydantic import ValidationError

from .ask_user import AskUserArguments
from .create_file import CreateFileContent
from .discovery import ListDirectoryContent, SearchFilesContent
from .edit_file import EditFileContent
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
    ToolCapability,
    ToolError,
    ToolKind,
    ToolOutcome,
    ToolResult,
    ToolResultMessage,
    SystemMessage,
    UserMessage,
)
from .read_file import ReadFileContent
from .search_text import SearchTextContent
from .shell import ShellContent
from .tooling import (
    LocalTool,
    PreparedToolCall,
    ToolExecutionResult,
    ToolRegistry,
    UnknownToolError,
)
from .workspace import WorkspacePathResolver


def _bounded_observation_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    marker = " ...[middle omitted]... "
    retained = max(0, limit - len(marker))
    head = retained * 2 // 3
    tail = retained - head
    return value[:head] + marker + (value[-tail:] if tail else "")


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
class RuntimeEvent:
    """One small normalized observability fact with no control authority."""

    kind: str
    facts: Mapping[str, str | int | float | bool | None] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        bounded: dict[str, str | int | float | bool | None] = {}
        for raw_key, raw_value in list(self.facts.items())[:20]:
            key = str(raw_key)[:80]
            value = (
                _bounded_observation_text(raw_value, 2_000)
                if isinstance(raw_value, str)
                else raw_value
            )
            bounded[key] = value
        object.__setattr__(self, "kind", self.kind[:80])
        object.__setattr__(self, "facts", MappingProxyType(bounded))


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
    completion_audit_required: bool = False
    completion_audit_active: bool = False
    pending_final_candidate: str | None = None


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
        transport_retry_base_delay_seconds: float = 0.5,
        transport_retry_max_delay_seconds: float = 2.0,
        observer: Callable[[RuntimeEvent], object] | None = None,
        runtime_secret_values: tuple[str, ...] = (),
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
        self._observer = observer
        self._runtime_secret_values = tuple(
            value for value in runtime_secret_values if value
        )
        self.session = Session()

    def run(self, task: str) -> AgentRun:
        """Run one user task until a final response or terminal failure."""
        agent_run = AgentRun(run_id=str(uuid4()), current_task=task)
        run_started_at = self._clock()
        try:
            self.session._add_run(agent_run)
            self._emit("run_started", run_id=agent_run.run_id)
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
                self._clear_completion_audit_state(agent_run)
                self._emit(
                    "run_terminal",
                    run_id=agent_run.run_id,
                    state=agent_run.state.value,
                    reason=(
                        agent_run.termination_reason.value
                        if agent_run.termination_reason is not None
                        else None
                    ),
                    model_turns=agent_run.model_turns,
                    tool_call_attempts=agent_run.tool_call_attempts,
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

            was_incomplete = self._context_manager.history_incomplete
            messages = self._context_manager.build_model_messages(
                completion_audit_active=agent_run.completion_audit_active,
                corrective_instruction=(
                    corrective_feedback.text
                    if corrective_feedback is not None
                    else None
                )
            )
            if not was_incomplete and self._context_manager.history_incomplete:
                self._emit("context_truncated", history_incomplete=True)
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
                self._emit(
                    "protocol_corrective",
                    consecutive_errors=agent_run.consecutive_protocol_errors,
                )
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
                self._emit(
                    "protocol_corrective",
                    consecutive_errors=agent_run.consecutive_protocol_errors,
                )
                continue

            agent_run.consecutive_protocol_errors = 0
            agent_run.last_error = None
            corrective_feedback = None

            if response.tool_calls:
                if agent_run.completion_audit_active:
                    self._emit(
                        "completion_audit_continued",
                        model_turn=agent_run.model_turns,
                        tool_call_count=len(response.tool_calls),
                    )
                assistant_message = AssistantMessage(
                    text=response.text,
                    tool_calls=response.tool_calls,
                    provider_reasoning_content=response.provider_reasoning_content,
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

            assistant_message = AssistantMessage(
                text=response.text,
                provider_reasoning_content=response.provider_reasoning_content,
            )
            if (
                agent_run.completion_audit_required
                and not agent_run.completion_audit_active
            ):
                agent_run.pending_final_candidate = response.text
                agent_run.completion_audit_active = True
                self._context_manager.record_candidate_message(assistant_message)
                self._emit(
                    "completion_audit_started",
                    model_turn=agent_run.model_turns,
                )
                continue

            self._context_manager.record_assistant_message(assistant_message)
            agent_run.final_response = response.text
            agent_run.state = RunState.COMPLETED
            if agent_run.completion_audit_active:
                self._emit(
                    "completion_audit_finished",
                    model_turn=agent_run.model_turns,
                )
                agent_run.pending_final_candidate = None
                agent_run.completion_audit_active = False
                self._context_manager.clear_pending_candidate()
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

    @staticmethod
    def _clear_completion_audit_state(agent_run: AgentRun) -> None:
        agent_run.completion_audit_required = False
        agent_run.completion_audit_active = False
        agent_run.pending_final_candidate = None

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
                result = self._not_executed(tool_call)
                results.append(result)
                self._emit_tool_result(result)
                continue

            if self._tool_budget_exhausted(agent_run, run_started_at):
                result = self._not_executed(tool_call)
                results.append(result)
                self._emit_tool_result(result)
                batch_stopped = True
                continue

            self._emit(
                "tool_proposed",
                call_id=tool_call.call_id,
                tool_name=tool_call.name,
                action=self._tool_observation_summary(tool_call),
            )
            dispatch = self._dispatch_tool_call(tool_call, agent_run)
            results.append(dispatch.result)
            self._emit_tool_result(dispatch.result)
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
                delay = self._transport_retry_delay(retries)
                self._emit(
                    "provider_retry",
                    retry_number=retries + 1,
                    delay_seconds=delay,
                )
                self._sleep(delay)
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
            self._emit_model_response(response, agent_run.model_turns)
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
        self._emit("budget_exhausted", limit=limit_name)
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

        if tool.spec.capabilities.intersection(
            {ToolCapability.FILE_MUTATION, ToolCapability.COMMAND_EXECUTION}
        ):
            agent_run.completion_audit_required = True

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
        self._emit(
            "policy_outcome",
            policy="explicit_constraint",
            decision=constraint_result.decision.value,
            reason_code=constraint_result.reason_code,
            tool_name=tool_call.name,
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
        self._emit(
            "policy_outcome",
            policy="risk_permission",
            decision=permission_result.decision.value,
            reason_code=permission_result.reason_code,
            tool_name=tool_call.name,
        )
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
        self._emit(
            "permission_requested",
            call_id=prepared.call_id,
            tool_name=prepared.tool_identity.name,
            reason_code=request.reason_code,
        )
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
            self._emit(
                "permission_resolved",
                call_id=prepared.call_id,
                decision=decision.value,
            )
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

    def _emit_tool_result(self, result: ToolResult) -> None:
        facts: dict[str, str | int | float | bool | None] = {
            "call_id": result.call_id,
            "tool_name": result.tool_name,
            "outcome": result.outcome.value,
            "error_code": result.error.code if result.error is not None else None,
        }
        content = result.content
        if isinstance(content, ReadFileContent):
            facts["line_count"] = max(0, content.end_line - content.start_line + 1)
        elif isinstance(content, ListDirectoryContent):
            facts["result_count"] = len(content.entries)
        elif isinstance(content, SearchFilesContent):
            facts["result_count"] = len(content.matches)
        elif isinstance(content, SearchTextContent):
            facts["result_count"] = len(content.matches)
        elif isinstance(content, EditFileContent):
            facts["replacement_count"] = content.replacement_count
        elif isinstance(content, CreateFileContent):
            facts["created"] = True
        elif isinstance(content, ShellContent):
            facts["exit_code"] = content.exit_code
            diagnostic = content.stderr.strip() or content.stdout.strip()
            if diagnostic:
                facts["diagnostic"] = _bounded_observation_text(diagnostic, 2_000)
        self._emit("tool_result", **facts)

    def _emit_model_response(self, response: ModelResponse, turn: int) -> None:
        facts: dict[str, str | int | float | bool | None] = {"model_turn": turn}
        if response.usage is not None:
            facts.update(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                total_tokens=response.usage.total_tokens,
            )
        self._emit("model_response", **facts)

    def _emit(
        self,
        kind: str,
        **facts: str | int | float | bool | None,
    ) -> None:
        """Notify a read-only observer without granting control authority."""
        if self._observer is None:
            return
        redacted_facts = {
            key: self._redact_runtime_secrets(value)
            if isinstance(value, str)
            else value
            for key, value in facts.items()
        }
        try:
            self._observer(RuntimeEvent(kind, redacted_facts))
        except Exception:
            pass

    def _redact_runtime_secrets(self, value: str) -> str:
        redacted = value
        for secret in self._runtime_secret_values:
            redacted = redacted.replace(secret, "<redacted>")
        return redacted

    @staticmethod
    def _tool_observation_summary(tool_call: ToolCall) -> str:
        arguments = tool_call.raw_arguments
        if not isinstance(arguments, Mapping):
            return "requested"
        if tool_call.name == "shell":
            command = arguments.get("command")
            return command if isinstance(command, str) else "command"
        path = arguments.get("path")
        if isinstance(path, str):
            return path.replace("\r", " ").replace("\n", " ")[:200]
        return "requested"

    @staticmethod
    def _dispatch_result(
        result: ToolResult,
        *,
        ends_batch: bool | None = None,
    ) -> _ToolDispatchResult:
        if ends_batch is None:
            ends_batch = result.outcome is not ToolOutcome.SUCCESS
        return _ToolDispatchResult(result=result, ends_batch=ends_batch)

    def _action_summary(self, prepared: PreparedToolCall) -> str:
        arguments = prepared.validated_arguments.model_dump(mode="json")
        if prepared.tool_identity.name == "shell":
            command = arguments.get("command")
            return (
                _bounded_observation_text(
                    self._redact_runtime_secrets(command),
                    1_000,
                )
                if isinstance(command, str)
                else "local shell command"
            )
        path = arguments.get("path")
        if isinstance(path, str):
            return path.replace("\r", " ").replace("\n", " ")[:1_000]
        parts: list[str] = []
        for key, value in sorted(arguments.items()):
            if key in {"content", "old_text", "new_text"} and isinstance(value, str):
                rendered_value = f"<omitted, {len(value)} chars>"
            else:
                rendered_value = self._redact_runtime_secrets(repr(value))[:400]
            parts.append(f"{key}={rendered_value}")
        rendered = ", ".join(parts)
        return f"{prepared.tool_identity.name}({rendered})"[:1_000]

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
    "RuntimeEvent",
    "RunState",
    "PendingAction",
    "Session",
    "TerminationReason",
    "WaitReason",
]
