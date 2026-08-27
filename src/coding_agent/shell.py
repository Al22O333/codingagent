"""Bounded, non-interactive local shell execution Tool."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from ctypes import Structure, byref, c_size_t, c_ulonglong, sizeof
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from pydantic import Field

from .protocol import ToolCapability, ToolError, ToolKind, ToolOutcome
from .tooling import Tool, ToolArguments, ToolExecutionResult
from .workspace import PathResolutionMode, ResolvedPath, WorkspacePathResolver


class ShellArguments(ToolArguments):
    """Validated model arguments for one local shell command."""

    command: str = Field(min_length=1)
    cwd: str = Field(default=".", min_length=1)
    timeout_seconds: int | None = Field(default=None, gt=0)


@dataclass(frozen=True, slots=True)
class ShellBackend:
    """Explicit executable for the Session's selected shell backend."""

    executable: str

    def __post_init__(self) -> None:
        if not self.executable.strip():
            raise ValueError("shell backend executable must not be empty")


@dataclass(frozen=True, slots=True)
class ShellContent:
    """Structured observation retained from one shell process."""

    command: str
    cwd: str
    exit_code: int | None
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool


class _BoundedStreamCapture:
    """Drain a pipe completely while retaining at most max_bytes."""

    def __init__(self, stream: BinaryIO, max_bytes: int) -> None:
        self._stream = stream
        self._max_bytes = max_bytes
        self._retained = bytearray()
        self.truncated = False
        self.error: OSError | None = None

    def drain(self) -> None:
        try:
            while chunk := self._stream.read(8192):
                remaining = self._max_bytes - len(self._retained)
                if remaining > 0:
                    self._retained.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    self.truncated = True
        except OSError as error:
            self.error = error
        finally:
            try:
                self._stream.close()
            except OSError as error:
                self.error = self.error or error

    def text(self) -> str:
        return bytes(self._retained).decode("utf-8", errors="replace")


if os.name == "nt":
    import ctypes

    class _JobBasicLimitInformation(Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", c_ulonglong),
            ("PerJobUserTimeLimit", c_ulonglong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", c_size_t),
            ("MaximumWorkingSetSize", c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IoCounters(Structure):
        _fields_ = [
            ("ReadOperationCount", c_ulonglong),
            ("WriteOperationCount", c_ulonglong),
            ("OtherOperationCount", c_ulonglong),
            ("ReadTransferCount", c_ulonglong),
            ("WriteTransferCount", c_ulonglong),
            ("OtherTransferCount", c_ulonglong),
        ]

    class _JobExtendedLimitInformation(Structure):
        _fields_ = [
            ("BasicLimitInformation", _JobBasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", c_size_t),
            ("JobMemoryLimit", c_size_t),
            ("PeakProcessMemoryUsed", c_size_t),
            ("PeakJobMemoryUsed", c_size_t),
        ]


class _WindowsJob:
    """Best-effort Windows process-tree owner backed by a Job Object."""

    _KILL_ON_JOB_CLOSE = 0x00002000
    _EXTENDED_LIMIT_INFORMATION = 9

    def __init__(self, handle: int) -> None:
        self._handle = handle

    @classmethod
    def attach(cls, process: subprocess.Popen[bytes]) -> _WindowsJob | None:
        if os.name != "nt":
            return None
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None

        information = _JobExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = cls._KILL_ON_JOB_CLOSE
        configured = kernel32.SetInformationJobObject(
            job,
            cls._EXTENDED_LIMIT_INFORMATION,
            byref(information),
            sizeof(information),
        )
        assigned = configured and kernel32.AssignProcessToJobObject(
            job,
            wintypes.HANDLE(int(process._handle)),  # type: ignore[attr-defined]
        )
        if not assigned:
            kernel32.CloseHandle(job)
            return None
        return cls(int(job))

    def terminate(self) -> None:
        if self._handle and os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.TerminateJobObject(wintypes.HANDLE(self._handle), 1)

    def close(self) -> None:
        if self._handle and os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle(wintypes.HANDLE(self._handle))
            self._handle = 0


class ShellTool(Tool[ShellArguments]):
    """Execute one full command string with bounded local process resources."""

    __slots__ = (
        "_backend",
        "_default_timeout_seconds",
        "_excluded_environment_names",
        "_max_stderr_bytes",
        "_max_stdout_bytes",
        "_resolver",
    )

    def __init__(
        self,
        resolver: WorkspacePathResolver,
        backend: ShellBackend,
        *,
        default_timeout_seconds: int,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
        excluded_environment_names: frozenset[str] = frozenset(),
    ) -> None:
        if default_timeout_seconds < 1:
            raise ValueError("default_timeout_seconds must be at least 1")
        if max_stdout_bytes < 1:
            raise ValueError("max_stdout_bytes must be at least 1")
        if max_stderr_bytes < 1:
            raise ValueError("max_stderr_bytes must be at least 1")
        super().__init__(
            name="shell",
            description="Execute a bounded, non-interactive local shell command",
            argument_model=ShellArguments,
            kind=ToolKind.LOCAL,
            capabilities=frozenset({ToolCapability.COMMAND_EXECUTION}),
        )
        object.__setattr__(self, "_resolver", resolver)
        object.__setattr__(self, "_backend", backend)
        object.__setattr__(self, "_default_timeout_seconds", default_timeout_seconds)
        object.__setattr__(self, "_max_stdout_bytes", max_stdout_bytes)
        object.__setattr__(self, "_max_stderr_bytes", max_stderr_bytes)
        object.__setattr__(
            self,
            "_excluded_environment_names",
            frozenset(name.casefold() for name in excluded_environment_names),
        )

    def prepare(self, arguments: ShellArguments) -> ResolvedPath | ToolError:
        """Resolve the requested existing cwd and validate directory shape."""
        try:
            resolved = self._resolver.resolve_workspace_path(
                arguments.cwd,
                PathResolutionMode.EXISTING,
            )
        except FileNotFoundError:
            return self._error("CWD_NOT_FOUND", "shell cwd does not exist")
        except (OSError, ValueError):
            return self._error("CWD_RESOLUTION_FAILED", "shell cwd could not be resolved")

        if not resolved.is_within_workspace:
            return resolved
        try:
            is_directory = resolved.resolved_path.is_dir()
        except OSError:
            return self._error("CWD_READ_FAILED", "shell cwd metadata could not be read")
        if not is_directory:
            return self._error("CWD_NOT_DIRECTORY", "shell cwd is not a directory")
        return resolved

    def execute(
        self,
        arguments: ShellArguments,
        resolved: ResolvedPath,
    ) -> ToolExecutionResult:
        """Execute an already resolved and permitted command action."""
        if resolved.raw_path != arguments.cwd:
            return self._failure(
                "INTERNAL_TOOL_ERROR",
                "resolved cwd does not match shell arguments",
            )
        if not resolved.is_within_workspace:
            raise ValueError(
                "outside-workspace cwd requires policy evaluation before execution"
            )

        timeout_seconds = arguments.timeout_seconds or self._default_timeout_seconds
        environment = self._filtered_environment()
        process: subprocess.Popen[bytes] | None = None
        windows_job: _WindowsJob | None = None
        try:
            process = subprocess.Popen(
                arguments.command,
                cwd=resolved.resolved_path,
                env=environment,
                executable=self._backend.executable,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=True,
                **self._process_group_options(),
            )
            windows_job = _WindowsJob.attach(process)
        except (OSError, ValueError) as error:
            return self._failure(
                "PROCESS_START_FAILED",
                "shell process could not be started",
                details={"reason": type(error).__name__},
            )

        if process.stdout is None or process.stderr is None:
            self._terminate_process_tree(process, windows_job)
            return self._failure(
                "PROCESS_IO_ERROR",
                "shell output pipes were not created",
            )

        stdout_capture = _BoundedStreamCapture(process.stdout, self._max_stdout_bytes)
        stderr_capture = _BoundedStreamCapture(process.stderr, self._max_stderr_bytes)
        stdout_thread = threading.Thread(target=stdout_capture.drain, daemon=True)
        stderr_thread = threading.Thread(target=stderr_capture.drain, daemon=True)
        stdout_thread.start()
        stderr_thread.start()

        timed_out = False
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._terminate_process_tree(process, windows_job)
        except KeyboardInterrupt:
            self._terminate_process_tree(process, windows_job)
            raise
        except OSError:
            self._terminate_process_tree(process, windows_job)
            self._join_capture_threads(stdout_thread, stderr_thread)
            return self._failure(
                "PROCESS_IO_ERROR",
                "shell process status could not be observed",
            )

        if windows_job is not None:
            windows_job.close()
        self._join_capture_threads(stdout_thread, stderr_thread)
        content = ShellContent(
            command=arguments.command,
            cwd=resolved.workspace_relative_path or arguments.cwd,
            exit_code=process.returncode,
            stdout=stdout_capture.text(),
            stderr=stderr_capture.text(),
            stdout_truncated=stdout_capture.truncated,
            stderr_truncated=stderr_capture.truncated,
        )

        if stdout_capture.error is not None or stderr_capture.error is not None:
            return self._failure(
                "PROCESS_IO_ERROR",
                "shell output could not be captured completely",
                content=content,
            )
        if timed_out:
            return self._failure(
                "COMMAND_TIMEOUT",
                "shell command exceeded its timeout",
                content=content,
                details={"timeout_seconds": timeout_seconds},
            )
        if process.returncode != 0:
            return ToolExecutionResult(
                outcome=ToolOutcome.UNSUCCESSFUL_COMMAND,
                content=content,
            )
        return ToolExecutionResult(outcome=ToolOutcome.SUCCESS, content=content)

    def _filtered_environment(self) -> dict[str, str]:
        return {
            name: value
            for name, value in os.environ.items()
            if name.casefold() not in self._excluded_environment_names
        }

    @staticmethod
    def _process_group_options() -> dict[str, object]:
        if os.name == "nt":
            return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        return {"start_new_session": True}

    @staticmethod
    def _join_capture_threads(*threads: threading.Thread) -> None:
        for thread in threads:
            thread.join(timeout=5)

    @staticmethod
    def _terminate_process_tree(
        process: subprocess.Popen[bytes],
        windows_job: _WindowsJob | None,
    ) -> None:
        if process.poll() is not None:
            if windows_job is not None:
                windows_job.close()
            return
        if os.name == "nt":
            if windows_job is not None:
                windows_job.terminate()
                windows_job.close()
            else:
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=5,
                        check=False,
                    )
                except (OSError, subprocess.SubprocessError):
                    pass
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=1)
            except (OSError, subprocess.SubprocessError):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    pass
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass

    @staticmethod
    def _error(
        code: str,
        message: str,
        *,
        details: object | None = None,
    ) -> ToolError:
        return ToolError(code=code, message=message, details=details)

    @classmethod
    def _failure(
        cls,
        code: str,
        message: str,
        *,
        content: ShellContent | None = None,
        details: object | None = None,
    ) -> ToolExecutionResult:
        return ToolExecutionResult(
            outcome=ToolOutcome.OPERATION_FAILURE,
            content=content,
            error=cls._error(code, message, details=details),
        )


__all__ = [
    "ShellArguments",
    "ShellBackend",
    "ShellContent",
    "ShellTool",
]
