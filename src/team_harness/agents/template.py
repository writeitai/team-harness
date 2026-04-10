from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class SessionCapture:
    strategy: Literal["stream_json_event", "pre_generated_uuid"] | None = None
    match: dict[str, str] | None = None
    field_path: tuple[str, ...] | None = None


@dataclass(frozen=True)
class AgentTemplate:
    command: tuple[str, ...]
    shared_flags: tuple[str, ...] = ()
    resume_prefix: tuple[str, ...] = ()
    resume_flags: tuple[str, ...] = ()
    prompt_flag: str | None = None
    prompt_position: Literal["tail", "after_command"] = "tail"
    model_flag: str | None = "--model"
    # Env vars to set when a model is effective. Some CLIs (notably
    # Claude Code) do not take a `--model` flag as their only source of
    # truth — they also read env vars. For each name listed here, the
    # spawner sets `env[name] = effective_model` on the child process.
    # For Claude we deliberately list ONLY the 3 "main model" vars and
    # leave ANTHROPIC_DEFAULT_HAIKU_MODEL / ANTHROPIC_SMALL_FAST_MODEL
    # alone so cheap auxiliary tasks stay on the haiku path.
    model_env_vars: tuple[str, ...] = ()
    # Model used when the caller does not pass an explicit `model`. The
    # effective model is: explicit spawn argument ∨ this default ∨ None
    # (in which case nothing is injected and the worker CLI uses its own
    # internal default).
    default_model: str | None = None
    session_capture: SessionCapture | None = None


DEFAULT_AGENT_TEMPLATES: dict[str, AgentTemplate] = {
    "codex": AgentTemplate(
        command=("codex", "exec"),
        shared_flags=(
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "--json",
        ),
        resume_prefix=("resume",),
        resume_flags=("{session_id}",),
        model_flag="--model",
        default_model="gpt-5.4",
        session_capture=SessionCapture(
            strategy="stream_json_event",
            match={"type": "thread.started"},
            field_path=("thread_id",),
        ),
    ),
    "gemini": AgentTemplate(
        command=("gemini",),
        shared_flags=("--approval-mode", "yolo", "--output-format", "stream-json"),
        resume_flags=("--resume", "{session_id}"),
        prompt_flag="-p",
        model_flag="--model",
        session_capture=SessionCapture(
            strategy="stream_json_event",
            match={"type": "init"},
            field_path=("session_id",),
        ),
    ),
    "claude": AgentTemplate(
        command=("claude",),
        shared_flags=(
            "-p",
            "--dangerously-skip-permissions",
            "--output-format",
            "stream-json",
            "--verbose",
        ),
        resume_flags=("--resume", "{session_id}"),
        model_flag="--model",
        # Claude Code reads its model from several env vars. Setting just
        # ANTHROPIC_MODEL is not sufficient: `getBestModel()` and the
        # Max-subscriber branch in `getDefaultMainLoopModel()` bypass it
        # and read `ANTHROPIC_DEFAULT_OPUS_MODEL` / `ANTHROPIC_DEFAULT_SONNET_MODEL`
        # directly. We set all 3 "main model" vars together so the
        # override is deterministic.
        # We deliberately do NOT set `ANTHROPIC_DEFAULT_HAIKU_MODEL` or
        # `ANTHROPIC_SMALL_FAST_MODEL` — those control cheap auxiliary
        # helpers and must stay cheap.
        model_env_vars=(
            "ANTHROPIC_MODEL",
            "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_DEFAULT_OPUS_MODEL",
        ),
        session_capture=SessionCapture(
            strategy="stream_json_event",
            match={"type": "system", "subtype": "init"},
            field_path=("session_id",),
        ),
    ),
    "opencode": AgentTemplate(command=("opencode",), model_flag=None),
    "pi": AgentTemplate(command=("pi", "--print", "--no-session"), model_flag=None),
    "harness": AgentTemplate(command=("th", "run"), model_flag="--model"),
}


def build_template_env(
    template: AgentTemplate, *, effective_model: str | None
) -> dict[str, str]:
    """Compute the env-var overrides a template wants applied for the
    given effective model. Returns an empty dict when no injection is
    needed (effective_model is None, or the template has no env-var
    injections configured)."""

    if effective_model is None or not template.model_env_vars:
        return {}
    return {name: effective_model for name in template.model_env_vars}


def template_uses_generated_uuid(template: AgentTemplate) -> bool:
    all_tokens = (
        *template.command,
        *template.shared_flags,
        *template.resume_prefix,
        *template.resume_flags,
    )
    return any("{generated_uuid}" in token for token in all_tokens)
