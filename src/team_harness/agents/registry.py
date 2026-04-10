from __future__ import annotations

import os
import shlex
import shutil
import warnings

from team_harness.agents.template import AgentTemplate
from team_harness.agents.template import DEFAULT_AGENT_TEMPLATES
from team_harness.config import Config

PROMPT_SENTINEL = "\x00PROMPT\x00"


def all_agent_types(config: Config) -> list[str]:
    custom = [
        name
        for name in sorted(config.agent_templates)
        if name not in DEFAULT_AGENT_TEMPLATES
    ]
    return list(DEFAULT_AGENT_TEMPLATES.keys()) + custom


def resolve_template(agent_type: str, config: Config) -> str | AgentTemplate:
    user_template = config.agent_templates.get(agent_type)
    if user_template is not None:
        return user_template
    if agent_type not in DEFAULT_AGENT_TEMPLATES:
        raise ValueError(f"Unknown agent type {agent_type!r}")
    return DEFAULT_AGENT_TEMPLATES[agent_type]


def _tokenize_template(template: str) -> list[str]:
    if "{prompt}" not in template:
        raise ValueError("Template missing {prompt} placeholder")
    marked = template.replace("{prompt}", PROMPT_SENTINEL)
    return shlex.split(marked)


def _substitute_template_token(
    token: str,
    *,
    prompt: str,
    session_id: str | None,
    generated_uuid: str | None,
) -> str:
    result = token.replace("{prompt}", prompt)
    if "{session_id}" in result:
        if session_id is None:
            raise ValueError("Resume mode requires a session_id for this template.")
        result = result.replace("{session_id}", session_id)
    if "{generated_uuid}" in result:
        if generated_uuid is None:
            raise ValueError(
                "Template requires a generated UUID but none was provided."
            )
        result = result.replace("{generated_uuid}", generated_uuid)
    return result


def build_command_from_template(
    template: AgentTemplate,
    prompt: str,
    *,
    mode: str = "fresh",
    session_id: str | None = None,
    generated_uuid: str | None = None,
    model: str | None = None,
    extra_flags: list[str] | None = None,
    allowed_agents: list[str] | None = None,
) -> list[str]:
    if mode not in {"fresh", "resume"}:
        raise ValueError(f"Unsupported spawn mode {mode!r}")

    def _substitute(tokens: tuple[str, ...]) -> list[str]:
        return [
            _substitute_template_token(
                token,
                prompt=prompt,
                session_id=session_id,
                generated_uuid=generated_uuid,
            )
            for token in tokens
        ]

    command = list(template.command)
    if mode == "resume":
        command.extend(_substitute(template.resume_prefix))

    prompt_args: list[str] = []
    if template.prompt_flag is not None:
        prompt_args.append(template.prompt_flag)
    prompt_args.append(prompt)

    if template.prompt_position == "after_command":
        command.extend(prompt_args)

    command.extend(_substitute(template.shared_flags))
    if model is not None and template.model_flag is not None:
        command.extend([template.model_flag, model])
    if mode == "resume":
        command.extend(_substitute(template.resume_flags))
    if extra_flags:
        command.extend(extra_flags)
    if template.prompt_position == "tail":
        command.extend(prompt_args)
    if allowed_agents is not None:
        command.extend(["--agents", ",".join(allowed_agents)])
    return command


def build_command(
    agent_type: str,
    prompt: str,
    config: Config,
    *,
    mode: str = "fresh",
    resume_session_id: str | None = None,
    generated_uuid: str | None = None,
    model: str | None = None,
    extra_flags: list[str] | None = None,
    allowed_agents: list[str] | None = None,
) -> list[str]:
    template = resolve_template(agent_type=agent_type, config=config)
    if isinstance(template, AgentTemplate):
        return build_command_from_template(
            template=template,
            prompt=prompt,
            mode=mode,
            session_id=resume_session_id,
            generated_uuid=generated_uuid,
            model=model,
            extra_flags=extra_flags,
            allowed_agents=allowed_agents if agent_type == "harness" else None,
        )

    if mode != "fresh":
        raise ValueError(
            "Legacy string templates do not support resume mode. "
            "Configure a structured agent template first."
        )

    template_tokens = _tokenize_template(template=template)
    command = [token.replace(PROMPT_SENTINEL, prompt) for token in template_tokens]

    if model is not None:
        if "--model" in template_tokens:
            warnings.warn(
                f"Template for {agent_type!r} already contains '--model'; "
                f"ignoring model override {model!r}.",
                stacklevel=2,
            )
        else:
            prompt_index = next(
                (
                    index
                    for index, token in enumerate(template_tokens)
                    if PROMPT_SENTINEL in token
                ),
                len(command),
            )
            command[prompt_index:prompt_index] = ["--model", model]

    if extra_flags:
        command.extend(extra_flags)
    if agent_type == "harness" and allowed_agents is not None:
        command.extend(["--agents", ",".join(allowed_agents)])

    return command


def get_allowed_types(config: Config) -> list[str]:
    available = all_agent_types(config)
    if config.allowed_agents is None:
        return available
    unknown = sorted(set(config.allowed_agents) - set(available))
    if unknown:
        raise ValueError(f"Unknown agent types in allowlist: {unknown}")
    return config.allowed_agents


def validate_templates(config: Config, allowed_types: list[str]) -> None:
    for agent_type in allowed_types:
        template = resolve_template(agent_type=agent_type, config=config)
        if isinstance(template, AgentTemplate):
            binary = template.command[0]
        else:
            binary = _tokenize_template(template=template)[0]
        if not shutil.which(binary):
            warnings.warn(
                f"Agent type '{agent_type}': binary '{binary}' not found on PATH",
                stacklevel=2,
            )


def check_harness_depth(config: Config) -> None:
    current_depth = int(os.environ.get("HARNESS_DEPTH", "0"))
    if current_depth >= config.max_depth:
        raise ValueError(
            f"Max harness nesting depth ({config.max_depth}) reached — refusing to spawn a nested harness instance."
        )
