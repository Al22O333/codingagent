"""OpenAI-compatible Chat Completions implementation of ModelClient."""

from __future__ import annotations

import asyncio
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
    AsyncOpenAI,
    InternalServerError,
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
    ProjectInstructionMessage,
    RuntimeInstructionMessage,
    SystemMessage,
    ToolCall,
    ToolResultMessage,
    ToolSpec,
    UserMessage,
)


_INTERRUPT_POLL_SECONDS = 0.1


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
    """Synchronous, non-streaming contract with cancellable internal HTTP I/O."""

    def __init__(
        self,
        config: OpenAICompatibleConfig,
        *,
        sdk_client: Any | None = None,
    ) -> None:
        self._config = config
        # An injected async SDK is caller-owned (primarily a test seam).
        self._client = sdk_client

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
            response = asyncio.run(self._request(payload))
        except (APITimeoutError, APIConnectionError, RateLimitError, InternalServerError) as error:
            raise TransientProviderError(str(error)) from error
        except APIStatusError as error:
            if error.status_code == 429 or error.status_code >= 500:
                raise TransientProviderError(str(error)) from error
            raise FatalProviderError(str(error)) from error
        except OpenAIError as error:
            raise FatalProviderError(str(error)) from error

        return self._normalize_response(response)

    async def _request(self, payload: dict[str, object]) -> object:
        if self._client is not None:
            return await self._wait_for_response(self._client, payload)
        # Each complete() owns its event loop and HTTP client. Close connections
        # before returning, including on cancellation; never reuse a closed loop.
        async with AsyncOpenAI(
            api_key=self._config.api_key,
            base_url=self._config.base_url,
            timeout=self._config.timeout_seconds,
            max_retries=0,
        ) as client:
            return await self._wait_for_response(client, payload)

    @staticmethod
    async def _wait_for_response(client: Any, payload: dict[str, object]) -> object:
        response_task = asyncio.create_task(client.chat.completions.create(**payload))
        try:
            # Windows socket waits can defer KeyboardInterrupt. Bounded event-loop
            # wakeups let Python process Ctrl+C even when the server sends nothing.
            while not response_task.done():
                await asyncio.wait((response_task,), timeout=_INTERRUPT_POLL_SECONDS)
            return response_task.result()
        finally:
            if not response_task.done():
                response_task.cancel()
            # Drain cancellation before closing the client or starting another Run.
            await asyncio.gather(response_task, return_exceptions=True)

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
            elif isinstance(message, ProjectInstructionMessage):
                wire.append({"role": "user", "content": message.text})
            elif isinstance(message, RuntimeInstructionMessage):
                wire.append({"role": "user", "content": message.text})
            elif isinstance(message, AssistantMessage):
                item: dict[str, object] = {
                    "role": "assistant",
                    "content": message.text,
                }
                if message.provider_reasoning_content is not None:
                    item["reasoning_content"] = message.provider_reasoning_content
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
        reasoning_content = getattr(message, "reasoning_content", None)
        if reasoning_content is not None and not isinstance(reasoning_content, str):
            raise ModelProtocolError(
                "provider assistant reasoning continuation is not text"
            )

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
            provider_reasoning_content=reasoning_content,
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
