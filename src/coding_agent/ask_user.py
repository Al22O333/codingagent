"""Structured clarification Interaction Tool metadata and validation."""

from __future__ import annotations

from pydantic import field_validator

from .protocol import ToolKind
from .tooling import Tool, ToolArguments


class AskUserArguments(ToolArguments):
    question: str

    @field_validator("question")
    @classmethod
    def question_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("question must not be blank")
        return value


class AskUserTool(Tool[AskUserArguments]):
    """The sole v1 INTERACTION Tool; Runtime performs the interaction."""

    def __init__(self) -> None:
        super().__init__(
            name="ask_user",
            description=(
                "Ask the user one question when a material product choice or "
                "important ambiguity cannot be resolved from the workspace."
            ),
            argument_model=AskUserArguments,
            kind=ToolKind.INTERACTION,
        )


__all__ = ["AskUserArguments", "AskUserTool"]
