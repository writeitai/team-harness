import asyncio
import os
import signal

DEFAULT_BASH_TIMEOUT_SECONDS = 120
BASH_TERMINATION_GRACE_SECONDS = 1.0
MAX_BASH_OUTPUT_CHARS = 32_768

BASH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": "Run a shell command and return combined stdout and stderr.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "cwd": {"type": "string"},
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "default": DEFAULT_BASH_TIMEOUT_SECONDS,
                    "description": (
                        "Maximum runtime before the command and its descendants are "
                        "terminated. Defaults to 120 seconds; explicitly raise this "
                        "for a known long-running foreground command."
                    ),
                },
            },
            "required": ["command"],
        },
    },
}


async def bash(
    command: str, cwd: str = ".", timeout_seconds: int = DEFAULT_BASH_TIMEOUT_SECONDS
) -> str:
    """Run one shell command with a bounded, caller-selectable timeout."""
    timeout_error = _validate_timeout_seconds(timeout_seconds=timeout_seconds)
    if timeout_error is not None:
        return timeout_error

    proc = await asyncio.create_subprocess_shell(
        cmd=command,
        cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    communicate_coro = proc.communicate()
    try:
        stdout, _ = await asyncio.wait_for(
            fut=communicate_coro, timeout=timeout_seconds
        )
    except asyncio.TimeoutError:
        await _terminate_process_group(proc=proc)
        return f"ERROR: command timed out after {timeout_seconds} seconds."
    except BaseException:
        await _terminate_process_group(proc=proc)
        raise
    output = stdout.decode(errors="replace")
    if len(output) > MAX_BASH_OUTPUT_CHARS:
        return output[:MAX_BASH_OUTPUT_CHARS] + "\n[output truncated at 32 KB]"
    return output


def _validate_timeout_seconds(*, timeout_seconds: object) -> str | None:
    """Return a coordinator-visible error for an invalid command timeout."""
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int):
        return "ERROR: timeout_seconds must be an integer."
    if timeout_seconds < 1:
        return "ERROR: timeout_seconds must be at least 1."
    return None


async def _terminate_process_group(*, proc: asyncio.subprocess.Process) -> None:
    """Terminate a shell group, allow a short grace, then kill and reap it."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        await proc.wait()
        return
    try:
        await asyncio.wait_for(fut=proc.wait(), timeout=BASH_TERMINATION_GRACE_SECONDS)
        return
    except asyncio.TimeoutError:
        pass
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    await proc.wait()
