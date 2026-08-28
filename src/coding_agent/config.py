"""Lean, validated startup configuration for Coding Agent v1."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_MAX_MODEL_TURNS = 24
DEFAULT_MAX_TOOL_CALL_ATTEMPTS = 64
DEFAULT_MAX_ACTIVE_RUN_DURATION_SECONDS = 900
DEFAULT_MAX_CONTEXT_CHARS = 80_000

MODEL_TURNS_RANGE = (1, 64)
TOOL_CALL_ATTEMPTS_RANGE = (1, 256)
ACTIVE_RUN_DURATION_RANGE = (1, 3_600)
CONTEXT_CHARS_RANGE = (8_000, 256_000)


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """Effective v1 startup and operational configuration."""

    workspace: Path
    model: str
    base_url: str
    api_key: str = field(repr=False)
    max_model_turns: int = DEFAULT_MAX_MODEL_TURNS
    max_tool_call_attempts: int = DEFAULT_MAX_TOOL_CALL_ATTEMPTS
    max_active_run_duration_seconds: int = DEFAULT_MAX_ACTIVE_RUN_DURATION_SECONDS
    max_context_chars: int = DEFAULT_MAX_CONTEXT_CHARS
    debug: bool = False
    api_key_environment_name: str = "CODING_AGENT_API_KEY"

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("model is required")
        if not self.base_url.strip():
            raise ValueError("base_url is required")
        if not self.api_key.strip():
            raise ValueError("api_key is required")
        _validate_range("max_model_turns", self.max_model_turns, MODEL_TURNS_RANGE)
        _validate_range(
            "max_tool_call_attempts",
            self.max_tool_call_attempts,
            TOOL_CALL_ATTEMPTS_RANGE,
        )
        _validate_range(
            "max_active_run_duration_seconds",
            self.max_active_run_duration_seconds,
            ACTIVE_RUN_DURATION_RANGE,
        )
        _validate_range(
            "max_context_chars",
            self.max_context_chars,
            CONTEXT_CHARS_RANGE,
        )


def load_agent_config(
    workspace: str,
    environ: Mapping[str, str] | None = None,
    *,
    model: str | None = None,
    base_url: str | None = None,
    max_model_turns: int | None = None,
    max_tool_call_attempts: int | None = None,
    max_active_run_duration_seconds: int | None = None,
    max_context_chars: int | None = None,
    debug: bool | None = None,
) -> AgentConfig:
    """Resolve CLI-over-environment-over-default v1 configuration."""

    values = os.environ if environ is None else environ
    effective_model = _text_override(model, values.get("CODING_AGENT_MODEL"))
    effective_base_url = _text_override(
        base_url, values.get("CODING_AGENT_BASE_URL")
    )
    api_key = values.get("CODING_AGENT_API_KEY", "").strip()
    if not effective_model:
        raise ValueError("CODING_AGENT_MODEL or --model is required")
    if not effective_base_url:
        raise ValueError("CODING_AGENT_BASE_URL or --base-url is required")
    if not api_key:
        raise ValueError("CODING_AGENT_API_KEY is required")

    return AgentConfig(
        workspace=Path(workspace),
        model=effective_model,
        base_url=effective_base_url,
        api_key=api_key,
        max_model_turns=_integer_setting(
            max_model_turns,
            values,
            "CODING_AGENT_MAX_MODEL_TURNS",
            DEFAULT_MAX_MODEL_TURNS,
        ),
        max_tool_call_attempts=_integer_setting(
            max_tool_call_attempts,
            values,
            "CODING_AGENT_MAX_TOOL_CALL_ATTEMPTS",
            DEFAULT_MAX_TOOL_CALL_ATTEMPTS,
        ),
        max_active_run_duration_seconds=_integer_setting(
            max_active_run_duration_seconds,
            values,
            "CODING_AGENT_MAX_ACTIVE_RUN_SECONDS",
            DEFAULT_MAX_ACTIVE_RUN_DURATION_SECONDS,
        ),
        max_context_chars=_integer_setting(
            max_context_chars,
            values,
            "CODING_AGENT_MAX_CONTEXT_CHARS",
            DEFAULT_MAX_CONTEXT_CHARS,
        ),
        debug=debug if debug is not None else _environment_bool(values, "CODING_AGENT_DEBUG"),
    )


def _text_override(cli_value: str | None, environment_value: str | None) -> str:
    selected = cli_value if cli_value is not None else environment_value
    return selected.strip() if selected else ""


def _integer_setting(
    cli_value: int | None,
    values: Mapping[str, str],
    environment_name: str,
    default: int,
) -> int:
    if cli_value is not None:
        return cli_value
    raw = values.get(environment_name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as error:
        raise ValueError(f"{environment_name} must be an integer") from error


def _environment_bool(values: Mapping[str, str], name: str) -> bool:
    raw = values.get(name)
    if raw is None or not raw.strip():
        return False
    normalized = raw.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _validate_range(name: str, value: int, limits: tuple[int, int]) -> None:
    lower, upper = limits
    if not lower <= value <= upper:
        raise ValueError(f"{name} must be between {lower} and {upper}")


__all__ = ["AgentConfig", "load_agent_config"]
