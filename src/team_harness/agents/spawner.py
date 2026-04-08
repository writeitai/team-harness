import asyncio
import os
from pathlib import Path

from team_harness.agents.registry import build_command
from team_harness.config import Config


async def spawn(
    agent_id: str,
    agent_type: str,
    prompt: str,
    cwd: Path,
    config: Config,
    log_dir: Path,
    extra_env: dict[str, str] | None = None,
    model: str | None = None,
    extra_flags: list[str] | None = None,
    allowed_agents: list[str] | None = None,
    output_path: str | None = None,
) -> asyncio.subprocess.Process:
    command = build_command(
        agent_type,
        prompt,
        config,
        model=model,
        extra_flags=extra_flags,
        allowed_agents=allowed_agents,
    )

    stdout_path = (
        Path(output_path) if output_path else log_dir / f"{agent_id}_stdout.log"
    )
    stderr_path = log_dir / f"{agent_id}_stderr.log"
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)

    stdout_file = stdout_path.open("wb")
    stderr_file = stderr_path.open("wb")
    try:
        proc = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=stdout_file,
            stderr=stderr_file,
            env={**os.environ, **(extra_env or {})},
        )
    finally:
        stdout_file.close()
        stderr_file.close()
    return proc
