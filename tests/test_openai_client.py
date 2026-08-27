"""Contract tests for the OpenAI-compatible concrete ModelClient."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from openai import APITimeoutError, BadRequestError

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
) -> object:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=text, tool_calls=tool_calls or [])
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
            tool_calls=[_provider_call("read_file", "{}", call_id=None)],
        )
    )
    generated = client.complete(ModelRequest(messages=(UserMessage("Read"),)))
    assert generated.tool_calls[0].call_id.startswith("generated-call-")

    duplicate_client, _ = _client(
        _response(
            text=None,
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


def test_config_rejects_missing_required_values() -> None:
    with pytest.raises(ValueError):
        OpenAICompatibleConfig(model="", api_key="key")
    with pytest.raises(ValueError):
        OpenAICompatibleConfig(model="model", api_key="")
