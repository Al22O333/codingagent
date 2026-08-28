"""OpenAI-compatible Chat Completions implementation of ModelClient."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from typing import Any
from uuid import uuid4

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    InternalServerError,
    OpenAI,
    OpenAIError,
    RateLimitError,
)

from .model_client import (
    FatalProviderError,
    ModelProtocolError,
    TransientProviderError,
)
from .protocol import (
    AssistantMessage,
    InternalMessage,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    SystemMessage,
    ToolCall,
    ToolResultMessage,
    ToolSpec,
    UserMessage,
)


@dataclass(frozen=True, slots=True)
class OpenAICompatibleConfig:
    model: str
    base_url: str
    api_key: str = field(repr=False)
    timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("model must not be blank")
        if not self.api_key.strip():
            raise ValueError("api_key must not be blank")
        if not self.base_url.strip():
            raise ValueError("base_url must not be blank")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")


class OpenAICompatibleModelClient:
    """Translate provider-neutral contracts to one non-streaming SDK request."""

    def __init__(
        self,
        config: OpenAICompatibleConfig,
        *,
        sdk_client: Any | None = None,
    ) -> None:
        self._config = config
        self._client = sdk_client or OpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            max_retries=0,
        )

    def complete(self, request: ModelRequest) -> ModelResponse:
        payload: dict[str, object] = {
            "model": self._config.model,
            "messages": self._serialize_messages(request.messages),
            "stream": False,
        }
        if request.tools:
            payload["tools"] = [self._serialize_tool(spec) for spec in request.tools]
            payload["tool_choice"] = "auto"

        try:
            response = self._client.chat.completions.create(**payload)
        except (APITimeoutError, APIConnectionError, RateLimitError, InternalServerError) as error:
            raise TransientProviderError(str(error)) from error
        except APIStatusError as error:
            if error.status_code == 429 or error.status_code >= 500:
                raise TransientProviderError(str(error)) from error
            raise FatalProviderError(str(error)) from error
        except OpenAIError as error:
            raise FatalProviderError(str(error)) from error

        return self._normalize_response(response)

    @staticmethod
    def _serialize_messages(
        messages: tuple[InternalMessage, ...],
    ) -> list[dict[str, object]]:
        wire: list[dict[str, object]] = []
        for message in messages:
            if isinstance(message, SystemMessage):
                wire.append({"role": "system", "content": message.text})
            elif isinstance(message, UserMessage):
                wire.append({"role": "user", "content": message.text})
            elif isinstance(message, AssistantMessage):
                item: dict[str, object] = {
                    "role": "assistant",
                    "content": message.text,
                }
                if message.tool_calls:
                    item["tool_calls"] = [
                        {
                            "id": call.call_id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": _serialize_raw_arguments(
                                    call.raw_arguments
                                ),
                            },
                        }
                        for call in message.tool_calls
                    ]
                wire.append(item)
            elif isinstance(message, ToolResultMessage):
                for result in message.results:
                    wire.append(
                        {
                            "role": "tool",
                            "tool_call_id": result.call_id,
                            "content": json.dumps(
                                _jsonable(result),
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        }
                    )
            else:  # pragma: no cover - closed InternalMessage union guard
                raise TypeError(f"unsupported internal message: {type(message)!r}")
        return wire

    @staticmethod
    def _serialize_tool(spec: ToolSpec) -> dict[str, object]:
        return {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": _jsonable(spec.input_schema),
            },
        }

    @staticmethod
    def _normalize_response(response: object) -> ModelResponse:
        choices = getattr(response, "choices", None)
        if not isinstance(choices, (list, tuple)) or len(choices) != 1:
            raise ModelProtocolError("provider response must contain exactly one choice")
        message = getattr(choices[0], "message", None)
        if message is None:
            raise ModelProtocolError("provider response choice has no message")

        text = getattr(message, "content", None)
        if text is not None and not isinstance(text, str):
            raise ModelProtocolError("provider assistant content is not text")

        normalized_calls: list[ToolCall] = []
        for provider_call in getattr(message, "tool_calls", None) or ():
            function = getattr(provider_call, "function", None)
            if function is None:
                raise ModelProtocolError("provider tool call has no function payload")
            name = getattr(function, "name", None)
            if not isinstance(name, str) or not name.strip():
                raise ModelProtocolError("provider tool call has no usable name")
            call_id = getattr(provider_call, "id", None)
            if not isinstance(call_id, str) or not call_id.strip():
                call_id = f"generated-call-{uuid4()}"
            encoded_arguments = getattr(function, "arguments", None)
            if isinstance(encoded_arguments, str):
                try:
                    raw_arguments: object = json.loads(encoded_arguments)
                except json.JSONDecodeError:
                    raw_arguments = encoded_arguments
            else:
                raw_arguments = encoded_arguments
            normalized_calls.append(
                ToolCall(
                    call_id=call_id,
                    name=name,
                    raw_arguments=raw_arguments,
                )
            )

        call_ids = [call.call_id for call in normalized_calls]
        if len(call_ids) != len(set(call_ids)):
            raise ModelProtocolError("provider response has duplicate tool call ids")

        finish_reason = getattr(choices[0], "finish_reason", None)
        expected_finish_reason = "tool_calls" if normalized_calls else "stop"
        if finish_reason != expected_finish_reason:
            raise ModelProtocolError(
                "provider response did not complete with the expected finish reason: "
                f"{finish_reason!r}"
            )

        usage = _normalize_usage(getattr(response, "usage", None))
        return ModelResponse(
            text=text,
            tool_calls=tuple(normalized_calls),
            usage=usage,
        )


def _normalize_usage(usage: object | None) -> ModelUsage | None:
    if usage is None:
        return None
    return ModelUsage(
        input_tokens=getattr(usage, "prompt_tokens", None),
        output_tokens=getattr(usage, "completion_tokens", None),
        total_tokens=getattr(usage, "total_tokens", None),
    )


def _serialize_raw_arguments(raw_arguments: object) -> str:
    if isinstance(raw_arguments, str):
        return raw_arguments
    return json.dumps(
        _jsonable(raw_arguments),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _jsonable(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _jsonable(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_jsonable(item) for item in value), key=repr)
    return value


__all__ = ["OpenAICompatibleConfig", "OpenAICompatibleModelClient"]
