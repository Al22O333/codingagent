"""Pure, bounded projection of runtime ToolResults for model context."""

from __future__ import annotations

from collections.abc import Mapping

from .create_file import CreateFileContent
from .discovery import ListDirectoryContent, SearchFilesContent
from .edit_file import EditFileContent
from .protocol import ToolError, ToolResult, ToolResultMessage
from .read_file import ReadFileContent
from .search_text import (
    DEFAULT_MODEL_PROJECTION_CHARS,
    SearchTextContent,
)
from .shell import ShellContent


SHELL_STREAM_VISIBLE_CHARS = 8_000
SHELL_OMISSION_MARKER = "\n\n[... output truncated; omitted content not shown ...]\n\n"


def project_tool_result_message(message: ToolResultMessage) -> ToolResultMessage:
    """Project every result without changing correspondence or outcome."""

    return ToolResultMessage(tuple(project_tool_result(result) for result in message.results))


def project_tool_result(result: ToolResult) -> ToolResult:
    """Return one model-safe concise ToolResult."""

    return ToolResult(
        call_id=result.call_id,
        tool_name=result.tool_name,
        outcome=result.outcome,
        content=_project_content(result.tool_name, result.content),
        error=_project_error(result.error),
    )


def _project_content(tool_name: str, content: object | None) -> object | None:
    if tool_name == "ask_user" and isinstance(content, Mapping):
        return dict(content)
    if isinstance(content, ListDirectoryContent):
        return {
            "path": content.path,
            "entries": tuple(
                {"path": entry.relative_path, "type": entry.type}
                for entry in content.entries
            ),
            "truncated": content.truncated,
        }
    if isinstance(content, SearchFilesContent):
        return {
            "path": content.path,
            "pattern": content.pattern,
            "matches": content.matches,
            "truncated": content.truncated,
        }
    if isinstance(content, SearchTextContent):
        rendered: list[str] = []
        used = 0
        truncated = content.truncated
        for match in content.matches:
            line = f"{match.relative_path}:{match.line_number} | {match.line_text}"
            separator = 1 if rendered else 0
            remaining = DEFAULT_MODEL_PROJECTION_CHARS - used - separator
            if remaining <= 0:
                truncated = True
                break
            if len(line) > remaining:
                rendered.append(line[:remaining])
                used += separator + remaining
                truncated = True
                break
            rendered.append(line)
            used += separator + len(line)
        return {
            "path": content.path,
            "query": content.query,
            "matches": "\n".join(rendered),
            "truncated": truncated,
        }
    if isinstance(content, ReadFileContent):
        return {
            "path": content.path,
            "start_line": content.start_line,
            "end_line": content.end_line,
            "total_lines": content.total_lines,
            "content": content.content,
            "truncated": content.truncated,
            "next_start_line": content.next_start_line,
        }
    if isinstance(content, EditFileContent):
        return {
            "path": content.path,
            "replacement_count": content.replacement_count,
        }
    if isinstance(content, CreateFileContent):
        return {
            "path": content.path,
            "bytes_written": content.bytes_written,
        }
    if isinstance(content, ShellContent):
        stdout, stdout_truncated = _project_shell_stream(
            content.stdout,
            content.stdout_truncated,
        )
        stderr, stderr_truncated = _project_shell_stream(
            content.stderr,
            content.stderr_truncated,
        )
        return {
            "exit_code": content.exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
        }
    if isinstance(content, Mapping):
        return _bounded_mapping(content)
    return content


def _project_shell_stream(value: str, already_truncated: bool) -> tuple[str, bool]:
    truncated = already_truncated or len(value) > SHELL_STREAM_VISIBLE_CHARS
    if not truncated:
        return value, False

    retained_chars = SHELL_STREAM_VISIBLE_CHARS - len(SHELL_OMISSION_MARKER)
    head_chars = retained_chars // 2
    tail_chars = retained_chars - head_chars
    projected = (
        value[:head_chars]
        + SHELL_OMISSION_MARKER
        + (value[-tail_chars:] if tail_chars else "")
    )
    return projected, True


def _project_error(error: ToolError | None) -> ToolError | None:
    if error is None:
        return None
    return ToolError(
        code=error.code,
        message=error.message[:1_000],
        details=_bounded_value(error.details, 2_000),
    )


def _bounded_mapping(value: Mapping[object, object]) -> dict[str, object]:
    projected: dict[str, object] = {}
    remaining = 4_000
    for raw_key, raw_value in value.items():
        key = str(raw_key)[:200]
        safe_value = _bounded_value(raw_value, min(remaining, 1_000))
        projected[key] = safe_value
        remaining -= len(key) + len(repr(safe_value))
        if remaining <= 0:
            break
    return projected


def _bounded_value(value: object | None, limit: int) -> object | None:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, Mapping):
        return _bounded_mapping(value)
    if isinstance(value, (tuple, list)):
        return tuple(_bounded_value(item, max(1, limit // 4)) for item in value[:20])
    return type(value).__name__


__all__ = [
    "SHELL_OMISSION_MARKER",
    "SHELL_STREAM_VISIBLE_CHARS",
    "project_tool_result",
    "project_tool_result_message",
]
