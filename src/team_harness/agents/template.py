from __future__ import annotations

from dataclasses import dataclass
import os
import re
from typing import Literal
import warnings


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
    # Reasoning-effort value (e.g. "low", "medium", "high") to pass to
    # the worker CLI. Only rendered into the argv when non-None AND the
    # template has a non-empty `reasoning_effort_flag`. The harness does
    # not validate the value against any CLI-specific enum — the worker
    # CLI is responsible for rejecting bad levels.
    reasoning_effort: str | None = None
    # argv tokens that carry the reasoning-effort value. Each token has
    # any literal {effort} substring replaced once with the effective
    # reasoning_effort. Empty tuple = this CLI has no reasoning-effort
    # surface. Example token shapes: codex uses a -c key=value override;
    # claude uses a --effort <level> flag. See DEFAULT_AGENT_TEMPLATES
    # below for the concrete values shipped.
    reasoning_effort_flag: tuple[str, ...] = ()
    # Provider-wide env vars merged into the child process env at spawn
    # time. Stored as a frozen sequence of name/value pairs because
    # AgentTemplate is frozen. Each value may contain {env:VARNAME}
    # placeholders; they are resolved from the parent shell's os.environ
    # at spawn time. Missing env refs expand to an empty string and emit
    # a single UserWarning per name per run. Intended for OpenRouter-style
    # provider wiring — see the README recipe for the exact incantation.
    provider_env: tuple[tuple[str, str], ...] = ()
    # Standalone argv flags that are safe to treat as idempotent when callers
    # also pass them through spawn_agent(flags=[...]). This is intentionally
    # explicit rather than inferred from argv shape because many CLIs support
    # meaningful repeated flags.
    deduplicate_flags: tuple[str, ...] = ()
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
        default_model="gpt-5.6-sol",
        deduplicate_flags=(
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "--json",
        ),
        # Codex expresses reasoning effort via its generic -c override.
        reasoning_effort_flag=("-c", "model_reasoning_effort={effort}"),
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
        deduplicate_flags=("-p", "--dangerously-skip-permissions", "--verbose"),
        # Claude Code exposes reasoning effort via --effort.
        reasoning_effort_flag=("--effort", "{effort}"),
        session_capture=SessionCapture(
            strategy="stream_json_event",
            match={"type": "system", "subtype": "init"},
            field_path=("session_id",),
        ),
    ),
    "antigravity": AgentTemplate(
        command=("agy",),
        shared_flags=(
            "--dangerously-skip-permissions",
            "--print",
            "--print-timeout",
            "60m",
        ),
        resume_flags=("--conversation", "{session_id}"),
        model_flag=None,
        deduplicate_flags=("--dangerously-skip-permissions", "--print"),
        session_capture=None,
    ),
    "openhands": AgentTemplate(
        command=("openhands",),
        shared_flags=("--headless", "--json", "--override-with-envs"),
        resume_prefix=(),
        resume_flags=(),
        prompt_flag="-t",
        prompt_position="tail",
        model_flag=None,
        model_env_vars=("LLM_MODEL",),
        default_model=None,
        session_capture=None,
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


def render_reasoning_effort_flags(
    template: AgentTemplate, *, effort: str | None = None
) -> list[str]:
    """Return the argv tokens to append to the command for the effective
    reasoning effort: an explicit `effort` argument wins over the template's
    `reasoning_effort`. Empty list when the effective level is None or the
    template has no `reasoning_effort_flag`. Each token's literal
    `{effort}` substring is replaced once with the effective level."""

    value = effort if effort is not None else template.reasoning_effort
    if value is None or not template.reasoning_effort_flag:
        return []
    return [
        token.replace("{effort}", value, 1) for token in template.reasoning_effort_flag
    ]


_ENV_REF_RE = re.compile(r"\{env:([A-Za-z_][A-Za-z0-9_]*)\}")

# Tracks {env:VAR} references that have already produced a warning, so
# spawning the same template many times does not produce warning spam.
# Cleared by tests via `build_provider_env.clear_warnings()`.
_provider_env_warned: set[str] = set()


def build_provider_env(template: AgentTemplate) -> dict[str, str]:
    """Resolve the template's `provider_env` into a `dict[str, str]`
    ready to merge into a subprocess env. Values may contain
    `{env:NAME}` placeholders; each placeholder is replaced with the
    current value of `os.environ[NAME]`. Missing env references expand
    to `""` and emit a single `UserWarning` per NAME per process run."""

    resolved: dict[str, str] = {}

    def _resolve(match: re.Match[str]) -> str:
        name = match.group(1)
        value = os.environ.get(name)
        if value is None:
            if name not in _provider_env_warned:
                _provider_env_warned.add(name)
                warnings.warn(
                    f"provider_env references {{env:{name}}} but {name} is not "
                    "set in the environment; substituting an empty string.",
                    stacklevel=2,
                )
            return ""
        return value

    for name, raw_value in template.provider_env:
        resolved[name] = _ENV_REF_RE.sub(_resolve, raw_value)
    return resolved


def _clear_provider_env_warnings() -> None:
    """Test helper — reset the once-per-run warning tracker."""

    _provider_env_warned.clear()


def template_uses_generated_uuid(template: AgentTemplate) -> bool:
    all_tokens = (
        *template.command,
        *template.shared_flags,
        *template.resume_prefix,
        *template.resume_flags,
    )
    return any("{generated_uuid}" in token for token in all_tokens)
