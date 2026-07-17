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
from team_harness.agents.template import template_supports_effort
from team_harness.agents.template import template_uses_generated_uuid
from team_harness.caller_contract import INHERITED_CALLER_CONTEXT_ENV
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
    """Launch one worker in its own process group with durable output files.

    The returned record contains the exact executed command, provider session
    hints, process identity for recovery, and effective model/effort audit data.
    """

    template = resolve_template(agent_type=agent_type, config=config)
    generated_uuid: str | None = None
    if template_uses_generated_uuid(template=template):
        generated_uuid = str(uuid.uuid4())

    # Effective model: explicit spawn argument wins over the template's
    # declared default. Used for BOTH `--model` flag injection and for
    # any env-var injection declared by the template (e.g. claude's
    # ANTHROPIC_* vars).
    effective_model = model if model is not None else template.default_model
    # Effective effort mirrors the model rule, but is only real when the
    # template can express it in argv; otherwise nothing is injected.
    effective_effort = effort if effort is not None else template.reasoning_effort
    if not template_supports_effort(template=template):
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
    provider_env = build_provider_env(template=template)
    template_env = build_template_env(
        template=template, effective_model=effective_model
    )
    merged_env = {**os.environ, **provider_env, **template_env, **(extra_env or {})}
    if extra_env is None or INHERITED_CALLER_CONTEXT_ENV not in extra_env:
        # The environment belongs to the harness coordinator process. A stale
        # inherited envelope would describe that coordinator's assignment, not
        # an arbitrary child worker's. Agent tools explicitly inject a newly
        # derived value only for ``type=harness`` descendants.
        merged_env.pop(INHERITED_CALLER_CONTEXT_ENV, None)

    stdout_file = stdout_path.open("wb")
    stderr_file = stderr_path.open("wb")
    try:
        # The worker is the process-group leader and writes its streams
        # directly to the caller-owned log files. Descendants inherit both the
        # group and file descriptors, preserving crash-recovery visibility.
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
    starttime = capture_starttime(pid=proc.pid)
    if starttime is None and proc.returncode is None:
        starttime = capture_starttime(pid=proc.pid)
    return SpawnResult(
        proc=proc,
        command=command,
        template=template,
        generated_uuid=generated_uuid,
        pid=proc.pid,
        pgid=proc.pid,
        starttime=starttime,
        effective_model=_recorded_model(
            template=template, effective_model=effective_model, extra_env=extra_env
        ),
        effective_effort=effective_effort,
    )


def _recorded_model(
    *,
    template: AgentTemplate,
    effective_model: str | None,
    extra_env: dict[str, str] | None,
) -> str | None:
    """The model value the audit trail may honestly claim reached the worker.

    The resolved model only reaches the worker through the template's
    injection surfaces (`model_flag` argv or `model_env_vars`). With no
    surface, nothing was injected — record None, not the requested value.
    For env-only templates the caller's per-spawn env wins the merge
    (spawner env precedence), so record the caller's value when it cleanly
    replaces the whole surface, and None (ambiguous) when it overrides only
    part of it or with conflicting values."""

    if template.model_flag is not None:
        return effective_model
    if not template.model_env_vars:
        return None
    overridden = {
        name: extra_env[name]
        for name in template.model_env_vars
        if extra_env is not None and name in extra_env
    }
    if not overridden:
        return effective_model
    values = set(overridden.values())
    if len(overridden) == len(template.model_env_vars) and len(values) == 1:
        return values.pop()
    return None
