from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import uuid

from team_harness.agents.process_identity import capture_starttime
from team_harness.agents.registry import build_command
from team_harness.agents.registry import resolve_template
from team_harness.agents.template import AgentTemplate
from team_harness.agents.template import build_provider_env
from team_harness.agents.template import build_template_env
from team_harness.agents.template import template_uses_generated_uuid
from team_harness.config import Config


@dataclass(frozen=True)
class SpawnResult:
    proc: asyncio.subprocess.Process
    command: list[str]
    template: AgentTemplate
    generated_uuid: str | None
    # Durable process identity (TH-D5). The worker is spawned as the leader of
    # its own process group, so pgid == pid; starttime guards against pid reuse.
    pid: int | None = None
    pgid: int | None = None
    starttime: str | None = None
    # Post-resolution model/effort audit: what was actually injected into the
    # worker after "explicit spawn argument ∨ template default". None means
    # nothing was injected and the worker CLI used its own internal default.
    effective_model: str | None = None
    effective_effort: str | None = None


async def spawn(
    agent_id: str,
    agent_type: str,
    prompt: str,
    cwd: Path,
    config: Config,
    log_dir: Path,
    extra_env: dict[str, str] | None = None,
    model: str | None = None,
    effort: str | None = None,
    extra_flags: list[str] | None = None,
    allowed_agents: list[str] | None = None,
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
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
    # Effective effort mirrors the model rule, but is only real when the
    # template can express it in argv; otherwise nothing is injected.
    effective_effort = effort if effort is not None else template.reasoning_effort
    if not template.reasoning_effort_flag:
        effective_effort = None

    command = build_command(
        agent_type=agent_type,
        prompt=prompt,
        config=config,
        mode=mode,
        resume_session_id=resume_session_id,
        generated_uuid=generated_uuid,
        model=effective_model,
        effort=effort,
        extra_flags=extra_flags,
        allowed_agents=allowed_agents,
    )

    stdout_path = (stdout_path or log_dir / f"{agent_id}_stdout.log").resolve()
    stderr_path = (stderr_path or log_dir / f"{agent_id}_stderr.log").resolve()
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)

    # Env precedence (first loses, last wins on conflict):
    #   1. os.environ             — parent shell; baseline
    #   2. template.provider_env   — coarse provider-wide constants,
    #                                e.g. ANTHROPIC_BASE_URL for OpenRouter.
    #                                Values may contain {env:NAME}
    #                                placeholders that resolve against
    #                                os.environ at call time.
    #   3. template.model_env_vars — per-model, dynamic; for Claude Code's
    #                                three "main model" env vars etc.
    #   4. caller extra_env         — explicit override for tests/SDK users.
    provider_env = build_provider_env(template)
    template_env = build_template_env(template, effective_model=effective_model)
    merged_env = {**os.environ, **provider_env, **template_env, **(extra_env or {})}

    stdout_file = stdout_path.open("wb")
    stderr_file = stderr_path.open("wb")
    try:
        # start_new_session makes the worker the leader of its own process group
        # (pgid == pid), so the whole worker subtree — including any helpers the
        # CLI spawns — is one killable/watchable unit that survives the parent.
        # Identity is persisted at spawn time via the run log (TH-D5).
        proc = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=stdout_file,
            stderr=stderr_file,
            env=merged_env,
            start_new_session=True,
        )
    finally:
        stdout_file.close()
        stderr_file.close()
    # Identity capture: retry once while the child is definitely still ours
    # (unreaped) — without a starttime the worker can later only be waited on,
    # never verifiably killed (probe verdict "unverifiable").
    starttime = capture_starttime(proc.pid)
    if starttime is None and proc.returncode is None:
        starttime = capture_starttime(proc.pid)
    return SpawnResult(
        proc=proc,
        command=command,
        template=template,
        generated_uuid=generated_uuid,
        pid=proc.pid,
        pgid=proc.pid,
        starttime=starttime,
        effective_model=effective_model,
        effective_effort=effective_effort,
    )
