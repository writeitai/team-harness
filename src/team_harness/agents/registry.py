import os
import shlex
import shutil
import warnings

from team_harness.config import Config
from team_harness.config import DEFAULT_TEMPLATES

PROMPT_SENTINEL = "\x00PROMPT\x00"


def all_agent_types(config: Config) -> list[str]:
    custom = [
        name for name in sorted(config.agent_templates) if name not in DEFAULT_TEMPLATES
    ]
    return list(DEFAULT_TEMPLATES.keys()) + custom


def resolve_template(agent_type: str, config: Config) -> str:
    user_template = config.agent_templates.get(agent_type)
    if user_template:
        return user_template
    if agent_type not in DEFAULT_TEMPLATES:
        raise ValueError(f"Unknown agent type {agent_type!r}")
    return DEFAULT_TEMPLATES[agent_type]


def _tokenize_template(template: str) -> list[str]:
    if "{prompt}" not in template:
        raise ValueError("Template missing {prompt} placeholder")
    marked = template.replace("{prompt}", PROMPT_SENTINEL)
    return shlex.split(marked)


def build_command(
    agent_type: str,
    prompt: str,
    config: Config,
    *,
    model: str | None = None,
    extra_flags: list[str] | None = None,
    allowed_agents: list[str] | None = None,
) -> list[str]:
    template = resolve_template(agent_type=agent_type, config=config)
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
