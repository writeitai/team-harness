from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import uuid

from team_harness.agents.registry import build_command
from team_harness.agents.registry import resolve_template
from team_harness.agents.template import AgentTemplate
from team_harness.agents.template import build_template_env
from team_harness.agents.template import template_uses_generated_uuid
from team_harness.config import Config


@dataclass(frozen=True)
class SpawnResult:
    proc: asyncio.subprocess.Process
    command: list[str]
    template: AgentTemplate
    generated_uuid: str | None


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
    mode: str = "fresh",
    resume_session_id: str | None = None,
) -> SpawnResult:
    template = resolve_template(agent_type=agent_type, config=config)
    generated_uuid: str | None = None
    if template_uses_generated_uuid(template):
        generated_uuid = str(uuid.uuid4())

    # Effective model: explicit spawn argument wins over the template's
    # declared default. Used for BOTH `--model` flag injection and for
    # any env-var injection declared by the template (e.g. claude's
    # ANTHROPIC_* vars).
    effective_model = model if model is not None else template.default_model

    command = build_command(
        agent_type=agent_type,
        prompt=prompt,
        config=config,
        mode=mode,
        resume_session_id=resume_session_id,
        generated_uuid=generated_uuid,
        model=effective_model,
        extra_flags=extra_flags,
        allowed_agents=allowed_agents,
    )

    stdout_path = (
        Path(output_path) if output_path else log_dir / f"{agent_id}_stdout.log"
    )
    stderr_path = log_dir / f"{agent_id}_stderr.log"
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)

    # Env merge order: parent environment is the base, the template's
    # model env vars layer on top, and the caller's explicit extra_env
    # wins over everything so tests and SDK users can override without
    # having to fork the template.
    template_env = build_template_env(template, effective_model=effective_model)
    merged_env = {**os.environ, **template_env, **(extra_env or {})}

    stdout_file = stdout_path.open("wb")
    stderr_file = stderr_path.open("wb")
    try:
        proc = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=stdout_file,
            stderr=stderr_file,
            env=merged_env,
        )
    finally:
        stdout_file.close()
        stderr_file.close()
    return SpawnResult(
        proc=proc, command=command, template=template, generated_uuid=generated_uuid
    )
