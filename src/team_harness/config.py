from dataclasses import dataclass
from dataclasses import field
import os
from pathlib import Path
import tomllib

CONFIG_PATH = Path.home() / ".team-harness" / "config.toml"
RUNS_DIR = Path.home() / ".team-harness" / "runs"
SKILLS_USER_DIR = Path.home() / ".team-harness" / "skills"

DEFAULT_TEMPLATES: dict[str, str] = {
    "codex": "codex exec {prompt}",
    "gemini": "gemini -p {prompt}",
    "claude": "claude -p --dangerously-skip-permissions {prompt}",
    "opencode": "opencode {prompt}",
    "pi": "pi --print --no-session {prompt}",
    "harness": "team-harness run {prompt}",
}


@dataclass
class Config:
    model: str = "openai/gpt-4o"
    api_base: str = "https://openrouter.ai/api/v1"
    api_key: str = ""
    max_turns: int = 50
    max_retries: int = 5
    max_depth: int = 3
    system_prompt_extension: str = ""
    context_limit: int | None = None
    shutdown_timeout_s: float = 10.0
    allowed_agents: list[str] | None = None
    agent_templates: dict[str, str] = field(default_factory=dict)
    cwd: str = "."
    run_dir: Path | None = None


def _default_config_text() -> str:
    return """# team-harness configuration
[coordinator]
model = "openai/gpt-4o"
api_base = "https://openrouter.ai/api/v1"
api_key = ""
system_prompt = ""
# context_limit = 128000
# shutdown_timeout_s = 10.0
# allowed_agents = ["codex", "gemini"]

[agents.codex]
template = "codex exec {prompt}"

[agents.gemini]
template = "gemini -p {prompt}"

[agents.claude]
template = "claude -p --dangerously-skip-permissions {prompt}"

[agents.opencode]
template = "opencode {prompt}"

[agents.pi]
template = "pi --print --no-session {prompt}"

[agents.harness]
template = "team-harness run {prompt}"
"""


def _create_default_config() -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(_default_config_text())
    print(
        "Created default config at ~/.team-harness/config.toml — edit to configure your API key."
    )


def _parse_allowed_agents(raw: str | list[str] | None) -> list[str] | None:
    if raw is None:
        return None
    if isinstance(raw, list):
        return [item.strip() for item in raw if item.strip()]
    return [item.strip() for item in raw.split(",") if item.strip()]


def load_config(
    *,
    model: str | None = None,
    api_base: str | None = None,
    api_key: str | None = None,
    max_turns: int | None = None,
    max_retries: int | None = None,
    max_depth: int | None = None,
    system_prompt: str | None = None,
    system_prompt_file: str | None = None,
    allowed_agents: str | None = None,
    cwd: str | None = None,
) -> Config:
    config_data: dict[str, object] = {}
    if not CONFIG_PATH.exists():
        _create_default_config()
    if CONFIG_PATH.exists():
        config_data = tomllib.loads(CONFIG_PATH.read_text())

    coordinator = config_data.get("coordinator", {})
    if not isinstance(coordinator, dict):
        coordinator = {}
    agents_section = config_data.get("agents", {})
    if not isinstance(agents_section, dict):
        agents_section = {}

    agent_templates: dict[str, str] = {}
    for agent_name, section in agents_section.items():
        if isinstance(agent_name, str) and isinstance(section, dict):
            template = section.get("template")
            if isinstance(template, str):
                agent_templates[agent_name] = template

    prompt_parts: list[str] = []
    config_prompt = coordinator.get("system_prompt")
    if isinstance(config_prompt, str) and config_prompt:
        prompt_parts.append(config_prompt)
    if system_prompt:
        prompt_parts.append(system_prompt)
    if system_prompt_file:
        prompt_parts.append(Path(system_prompt_file).read_text())

    env_model = os.environ.get("HARNESS_MODEL")
    env_api_base = os.environ.get("HARNESS_API_BASE")
    env_api_key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get(
        "OPENAI_API_KEY"
    )

    return Config(
        model=model or env_model or str(coordinator.get("model", Config.model)),
        api_base=api_base
        or env_api_base
        or str(coordinator.get("api_base", Config.api_base)),
        api_key=api_key or env_api_key or str(coordinator.get("api_key", "")),
        max_turns=max_turns or int(coordinator.get("max_turns", Config.max_turns)),
        max_retries=max_retries
        or int(coordinator.get("max_retries", Config.max_retries)),
        max_depth=max_depth or int(coordinator.get("max_depth", Config.max_depth)),
        system_prompt_extension="\n\n".join(part for part in prompt_parts if part),
        context_limit=(
            int(coordinator["context_limit"])
            if coordinator.get("context_limit") is not None
            else None
        ),
        shutdown_timeout_s=float(
            coordinator.get("shutdown_timeout_s", Config.shutdown_timeout_s)
        ),
        allowed_agents=_parse_allowed_agents(allowed_agents)
        or _parse_allowed_agents(coordinator.get("allowed_agents")),
        agent_templates=agent_templates,
        cwd=cwd or Config.cwd,
    )
