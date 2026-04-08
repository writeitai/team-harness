import asyncio

BASH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": "Run a shell command and return combined stdout and stderr.",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}, "cwd": {"type": "string"}},
            "required": ["command"],
        },
    },
}


async def bash(command: str, cwd: str = ".") -> str:
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    communicate_coro = proc.communicate()
    try:
        stdout, _ = await asyncio.wait_for(communicate_coro, timeout=120)
    except asyncio.TimeoutError:
        communicate_coro.close()
        proc.kill()
        return "ERROR: command timed out after 120 seconds."
    output = stdout.decode(errors="replace")
    if len(output) > 32768:
        return output[:32768] + "\n[output truncated at 32 KB]"
    return output
