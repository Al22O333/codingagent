"""Contract tests for the OpenAI-compatible concrete ModelClient."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from openai import APITimeoutError, BadRequestError

from coding_agent.context import ContextManager
from coding_agent.interaction import FakeUserInteraction
from coding_agent.model_client import (
    FatalProviderError,
    ModelProtocolError,
    TransientProviderError,
)
from coding_agent.openai_client import (
    OpenAICompatibleConfig,
    OpenAICompatibleModelClient,
)
from coding_agent.protocol import (
    AssistantMessage,
    ModelRequest,
    SystemMessage,
    ToolCall,
    ToolError,
    ToolKind,
    ToolOutcome,
    ToolResult,
    ToolResultMessage,
    ToolSpec,
    UserMessage,
)
from coding_agent.policy import PolicyEngine
from coding_agent.runtime import AgentRuntime, RunState, RuntimeLimits
from coding_agent.tooling import ToolRegistry


class FakeCompletions:
    def __init__(self, events: list[object]) -> None:
        self.events = list(events)
        self.calls: list[dict[str, object]] = []

    def create(self, **payload: object) -> object:
        self.calls.append(payload)
        event = self.events.pop(0)
        if isinstance(event, Exception):
            raise event
        return event


class FakeSDK:
    def __init__(self, events: list[object]) -> None:
        self.completions = FakeCompletions(events)
        self.chat = SimpleNamespace(completions=self.completions)


def _response(
    *,
    text: str | None,
    tool_calls: list[object] | None = None,
    finish_reason: str = "stop",
) -> object:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=text, tool_calls=tool_calls or []),
                finish_reason=finish_reason,
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=4,
            total_tokens=14,
        ),
    )


def _provider_call(
    name: str,
    arguments: str,
    call_id: str | None = "provider-call-1",
) -> object:
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _client(event: object) -> tuple[OpenAICompatibleModelClient, FakeSDK]:
    sdk = FakeSDK([event])
    client = OpenAICompatibleModelClient(
        OpenAICompatibleConfig(
            model="test-model",
            api_key="test-key",
            base_url="https://provider.invalid/v1",
        ),
        sdk_client=sdk,
    )
    return client, sdk


def test_no_tool_final_smoke_and_request_serialization() -> None:
    client, sdk = _client(_response(text="Finished."))
    request = ModelRequest(
        messages=(SystemMessage("Be concise."), UserMessage("Do it.")),
    )

    response = client.complete(request)

    assert response.text == "Finished."
    assert response.tool_calls == ()
    assert response.usage is not None
    assert response.usage.total_tokens == 14
    assert sdk.completions.calls == [
        {
            "model": "test-model",
            "messages": [
                {"role": "system", "content": "Be concise."},
                {"role": "user", "content": "Do it."},
            ],
            "stream": False,
        }
    ]


def test_read_file_tool_call_smoke_preserves_identity_and_raw_arguments() -> None:
    client, sdk = _client(
        _response(
            text=None,
            finish_reason="tool_calls",
            tool_calls=[
                _provider_call("read_file", '{"path":"main.py","start_line":2}')
            ],
        )
    )
    spec = ToolSpec(
        name="read_file",
        description="Read a file.",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
        kind=ToolKind.LOCAL,
        capabilities=frozenset(),
    )

    response = client.complete(
        ModelRequest(messages=(UserMessage("Read it."),), tools=(spec,))
    )

    assert response.tool_calls == (
        ToolCall(
            call_id="provider-call-1",
            name="read_file",
            raw_arguments={"path": "main.py", "start_line": 2},
        ),
    )
    assert sdk.completions.calls[0]["tool_choice"] == "auto"
    assert sdk.completions.calls[0]["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
            },
        }
    ]


def test_tool_history_and_results_serialize_with_call_correspondence() -> None:
    client, sdk = _client(_response(text="Done."))
    call = ToolCall("call-1", "read_file", {"path": "main.py"})
    result = ToolResult(
        call_id="call-1",
        tool_name="read_file",
        outcome=ToolOutcome.OPERATION_FAILURE,
        error=ToolError("FILE_NOT_FOUND", "missing"),
    )

    client.complete(
        ModelRequest(
            messages=(
                UserMessage("Read it."),
                AssistantMessage(text=None, tool_calls=(call,)),
                ToolResultMessage((result,)),
            )
        )
    )

    messages = sdk.completions.calls[0]["messages"]
    assert isinstance(messages, list)
    assert json.loads(messages[1]["tool_calls"][0]["function"]["arguments"]) == {
        "path": "main.py"
    }
    assert messages[2]["tool_call_id"] == "call-1"
    encoded_result = json.loads(messages[2]["content"])
    assert encoded_result["outcome"] == "OPERATION_FAILURE"
    assert encoded_result["error"]["code"] == "FILE_NOT_FOUND"


def test_malformed_arguments_remain_call_level_validation_input() -> None:
    client, _ = _client(
        _response(
            text=None,
            finish_reason="tool_calls",
            tool_calls=[_provider_call("read_file", "{not-json")],
        )
    )

    response = client.complete(ModelRequest(messages=(UserMessage("Read"),)))

    assert response.tool_calls[0].raw_arguments == "{not-json"

    history_client, history_sdk = _client(_response(text="Corrected."))
    history_client.complete(
        ModelRequest(
            messages=(
                AssistantMessage(
                    text=None,
                    tool_calls=(response.tool_calls[0],),
                ),
            )
        )
    )
    history = history_sdk.completions.calls[0]["messages"]
    assert history[0]["tool_calls"][0]["function"]["arguments"] == "{not-json"


def test_missing_call_id_is_generated_but_duplicate_ids_are_protocol_error() -> None:
    client, _ = _client(
        _response(
            text=None,
            finish_reason="tool_calls",
            tool_calls=[_provider_call("read_file", "{}", call_id=None)],
        )
    )
    generated = client.complete(ModelRequest(messages=(UserMessage("Read"),)))
    assert generated.tool_calls[0].call_id.startswith("generated-call-")

    duplicate_client, _ = _client(
        _response(
            text=None,
            finish_reason="tool_calls",
            tool_calls=[
                _provider_call("read_file", "{}", "same"),
                _provider_call("read_file", "{}", "same"),
            ],
        )
    )
    with pytest.raises(ModelProtocolError, match="duplicate"):
        duplicate_client.complete(ModelRequest(messages=(UserMessage("Read"),)))


def test_provider_exceptions_are_normalized() -> None:
    request = httpx.Request("POST", "https://provider.invalid/v1/chat/completions")
    transient_client, _ = _client(APITimeoutError(request=request))
    with pytest.raises(TransientProviderError):
        transient_client.complete(ModelRequest(messages=(UserMessage("Hi"),)))

    response = httpx.Response(400, request=request)
    fatal_client, _ = _client(
        BadRequestError(message="bad request", response=response, body=None)
    )
    with pytest.raises(FatalProviderError):
        fatal_client.complete(ModelRequest(messages=(UserMessage("Hi"),)))


@pytest.mark.parametrize("finish_reason", ["length", "content_filter", "aborted"])
def test_incomplete_or_unknown_finish_reason_is_protocol_error(
    finish_reason: str,
) -> None:
    client, _ = _client(_response(text="Partial answer", finish_reason=finish_reason))

    with pytest.raises(ModelProtocolError, match="finish reason"):
        client.complete(ModelRequest(messages=(UserMessage("Complete it"),)))


@pytest.mark.parametrize(
    "response",
    [
        SimpleNamespace(choices=[], usage=None),
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content={"not": "text"}, tool_calls=[]),
                    finish_reason="stop",
                )
            ],
            usage=None,
        ),
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=None,
                        tool_calls=[SimpleNamespace(id="call", function=None)],
                    ),
                    finish_reason="tool_calls",
                )
            ],
            usage=None,
        ),
    ],
)
def test_malformed_provider_shapes_raise_protocol_error(response: object) -> None:
    client, _ = _client(response)

    with pytest.raises(ModelProtocolError):
        client.complete(ModelRequest(messages=(UserMessage("Complete it"),)))


def test_truncated_provider_response_cannot_complete_runtime() -> None:
    sdk = FakeSDK(
        [
            _response(text="Partial answer", finish_reason="length"),
            _response(text="Complete answer", finish_reason="stop"),
        ]
    )
    client = OpenAICompatibleModelClient(
        OpenAICompatibleConfig(
            model="test-model",
            base_url="https://provider.invalid/v1",
            api_key="test-key",
        ),
        sdk_client=sdk,
    )
    runtime = AgentRuntime(
        client,
        ContextManager(),
        ToolRegistry(),
        RuntimeLimits(5, 5, 30, 0, 2),
        policy_engine=PolicyEngine(),
        user_interaction=FakeUserInteraction(),
        sleep_fn=lambda _: None,
    )

    run = runtime.run("Complete the task")

    assert run.state is RunState.COMPLETED
    assert run.final_response == "Complete answer"
    assert run.model_turns == 2
    assert len(sdk.completions.calls) == 2


def test_config_rejects_missing_required_values() -> None:
    with pytest.raises(ValueError):
        OpenAICompatibleConfig(model="", base_url="https://provider.invalid/v1", api_key="key")
    with pytest.raises(ValueError):
        OpenAICompatibleConfig(model="model", base_url="https://provider.invalid/v1", api_key="")
    with pytest.raises(ValueError):
        OpenAICompatibleConfig(model="model", base_url="", api_key="key")


def test_config_repr_does_not_expose_api_key() -> None:
    secret = "super-secret-test-key"
    config = OpenAICompatibleConfig(
        model="test-model",
        base_url="https://provider.invalid/v1",
        api_key=secret,
    )

    representation = repr(config)

    assert secret not in representation
    assert "test-model" in representation


def test_default_sdk_client_uses_bounded_timeout_and_disables_sdk_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_openai(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("coding_agent.openai_client.OpenAI", fake_openai)

    OpenAICompatibleModelClient(
        OpenAICompatibleConfig(
            model="test-model",
            base_url="https://provider.invalid/v1",
            api_key="test-key",
        )
    )

    assert captured["base_url"] == "https://provider.invalid/v1"
    assert captured["timeout"] == 60.0
    assert captured["max_retries"] == 0
