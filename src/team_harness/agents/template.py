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
        session_capture=SessionCapture(
            strategy="stream_json_event",
            match={"type": "system", "subtype": "init"},
            field_path=("session_id",),
        ),
    ),
    "opencode": AgentTemplate(
        command=("opencode",),
        model_flag=None,
    ),
    "pi": AgentTemplate(
        command=("pi", "--print", "--no-session"),
        model_flag=None,
    ),
    "harness": AgentTemplate(
        command=("th", "run"),
        model_flag="--model",
    ),
}


def template_uses_generated_uuid(template: AgentTemplate) -> bool:
    all_tokens = (
        *template.command,
        *template.shared_flags,
        *template.resume_prefix,
        *template.resume_flags,
    )
    return any("{generated_uuid}" in token for token in all_tokens)
