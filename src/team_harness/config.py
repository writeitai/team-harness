from dataclasses import dataclass
from dataclasses import field
import os
from pathlib import Path
import tomllib
import warnings

LOCAL_CONFIG_DIR_NAME = ".team-harness"
CONFIG_PATH = Path.home() / ".team-harness" / "config.toml"
RUNS_DIR = Path.home() / ".team-harness" / "runs"
SKILLS_USER_DIR = Path.home() / ".team-harness" / "skills"

DEFAULT_TEMPLATES: dict[str, str] = {
    "codex": 'codex exec --yolo --model gpt-5.4 PROMPT="{prompt}"',
    "gemini": 'gemini --approval-mode=yolo -p "{prompt}"',
    "claude": "claude -p --dangerously-skip-permissions {prompt}",
    "opencode": "opencode {prompt}",
    "pi": "pi --print --no-session {prompt}",
    "harness": "th run {prompt}",
}


@dataclass
class Config:
    provider: str = "openai_compat"
    model: str = "gpt-5.4"
    api_base: str = "https://openrouter.ai/api/v1"
    api_key: str = ""
    codex_auth_path: str = ""
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
    global_config_path: Path | None = None
    local_config_path: Path | None = None


def _default_config_text() -> str:
    return """# th — global configuration
# Applies to all projects. Project-level .team-harness/config.toml overrides these.

[coordinator]
# Coordinator backend: "openai_compat" (OpenRouter / any OpenAI-compatible API)
# or "codex" (experimental ChatGPT Codex subscription).
provider = "openai_compat"

# Model name passed to the coordinator API.
model = "gpt-5.4"

# Base URL for the coordinator API.
api_base = "https://openrouter.ai/api/v1"

# API key. Prefer the OPENROUTER_API_KEY or OPENAI_API_KEY env var instead.
api_key = ""

# Extra text appended to the system prompt for every run.
system_prompt = ""

# Maximum coordinator turns before the run stops.
max_turns = 50

# Retry budget for transient API errors (429 / 5xx).
max_retries = 5

# Maximum nesting depth for recursive th-run agents.
max_depth = 3

# Override the model's context window size (tokens). Leave commented to auto-detect.
# context_limit = 128000

# Seconds to wait for running agents on /quit or Ctrl+C before force-killing.
shutdown_timeout_s = 10.0

# Restrict which agent types the coordinator can spawn. Leave commented to allow all.
# allowed_agents = ["codex", "gemini", "claude", "opencode", "pi", "harness"]

# --- Experimental Codex subscription coordinator ---
# provider = "codex"
# model = "codex-mini-latest"
# codex_auth_path = "~/.codex/auth.json"

# Agent templates. {prompt} is replaced with the task text at spawn time.
[agents.codex]
template = "codex exec --yolo --model gpt-5.4 PROMPT=\\"{prompt}\\""

[agents.gemini]
template = "gemini --approval-mode=yolo -p \\"{prompt}\\""

[agents.claude]
template = "claude -p --dangerously-skip-permissions {prompt}"

[agents.opencode]
template = "opencode {prompt}"

[agents.pi]
template = "pi --print --no-session {prompt}"

[agents.harness]
template = "th run {prompt}"
"""


def _local_config_text() -> str:
    return """# Project-level team-harness config.
# Values here override ~/.team-harness/config.toml.
# Lists replace, they do not extend, the global value.
# Do not store API keys here; prefer environment variables.

[coordinator]
# Coordinator backend: "openai_compat" or "codex" (experimental).
provider = "openai_compat"

# Model name passed to the coordinator API.
model = "gpt-5.4"

# Base URL for the coordinator API.
api_base = "https://openrouter.ai/api/v1"

# API key — prefer OPENROUTER_API_KEY or OPENAI_API_KEY env var instead.
# api_key = ""

# Extra text appended to the system prompt for every run.
system_prompt = ""

# Maximum coordinator turns before the run stops.
max_turns = 50

# Retry budget for transient API errors (429 / 5xx).
max_retries = 5

# Maximum nesting depth for recursive th-run agents.
max_depth = 3

# Override the model's context window size (tokens). Leave commented to auto-detect.
# context_limit = 128000

# Seconds to wait for running agents on /quit or Ctrl+C before force-killing.
shutdown_timeout_s = 10.0

# Restrict which agent types the coordinator can spawn. Leave commented to allow all.
# allowed_agents = ["codex", "gemini", "claude", "opencode", "pi", "harness"]

# --- Experimental Codex subscription coordinator ---
# provider = "codex"
# model = "codex-mini-latest"
# codex_auth_path = ".team-harness/codex-auth.json"

# Agent templates. {prompt} is replaced with the task text at spawn time.
[agents.codex]
template = "codex exec --yolo --model gpt-5.4 PROMPT=\\"{prompt}\\""

[agents.gemini]
template = "gemini --approval-mode=yolo -p \\"{prompt}\\""

[agents.claude]
template = "claude -p --dangerously-skip-permissions {prompt}"

[agents.opencode]
template = "opencode {prompt}"

[agents.pi]
template = "pi --print --no-session {prompt}"

[agents.harness]
template = "th run {prompt}"
"""


def _parse_allowed_agents(raw: str | list[str] | None) -> list[str] | None:
    if raw is None:
        return None
    if isinstance(raw, list):
        return [item.strip() for item in raw if item.strip()]
    return [item.strip() for item in raw.split(",") if item.strip()]


def _parse_provider(raw: object) -> str:
    provider = str(raw).strip().lower()
    if not provider:
        return "openai_compat"
    if provider == "openai_compat":
        return provider
    if provider == "codex":
        return provider
    if provider == "openrouter":
        warnings.warn(
            "Provider 'openrouter' is deprecated; use 'openai_compat' instead.",
            stacklevel=2,
        )
        return "openai_compat"
    raise SystemExit(
        f"Invalid provider {provider!r}. Expected one of: 'openai_compat', 'codex'."
    )


def find_local_config(start: Path | None = None) -> Path | None:
    current = (start or Path.cwd()).resolve()
    while True:
        candidate = current / LOCAL_CONFIG_DIR_NAME / "config.toml"
        if candidate.is_file():
            return candidate
        parent = current.parent
        if parent == current:
            return None
        current = parent


def _deep_merge(
    base: dict[str, object], override: dict[str, object]
) -> dict[str, object]:
    merged = dict(base)
    for key, override_value in override.items():
        base_value = merged.get(key)
        if isinstance(base_value, dict) and isinstance(override_value, dict):
            merged[key] = _deep_merge(base=base_value, override=override_value)
            continue
        merged[key] = override_value
    return merged


def _load_toml_file(path: Path) -> dict[str, object]:
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"Invalid TOML in {path}: {exc}") from exc
    return data


def load_config(
    *,
    provider: str | None = None,
    model: str | None = None,
    api_base: str | None = None,
    api_key: str | None = None,
    codex_auth_path: str | None = None,
    max_turns: int | None = None,
    max_retries: int | None = None,
    max_depth: int | None = None,
    system_prompt: str | None = None,
    system_prompt_file: str | None = None,
    allowed_agents: str | None = None,
    cwd: str | None = None,
) -> Config:
    start_dir = Path(cwd).resolve() if cwd else Path.cwd().resolve()
    global_path = CONFIG_PATH.resolve() if CONFIG_PATH.exists() else None
    local_path = find_local_config(start_dir)
    if (
        global_path is not None
        and local_path is not None
        and local_path.resolve() == global_path.resolve()
    ):
        local_path = None

    global_data = _load_toml_file(global_path) if global_path else {}
    local_data = _load_toml_file(local_path) if local_path else {}
    config_data = _deep_merge(base=global_data, override=local_data)

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
    env_provider = os.environ.get("HARNESS_PROVIDER")
    env_codex_auth_path = os.environ.get("HARNESS_CODEX_AUTH_PATH")
    env_api_key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get(
        "OPENAI_API_KEY"
    )
    cli_allowed_agents = _parse_allowed_agents(allowed_agents)
    provider_value = _parse_provider(
        provider
        if provider is not None
        else env_provider or coordinator.get("provider", Config.provider)
    )
    raw_model = model if model is not None else env_model or coordinator.get("model")
    raw_api_base = (
        api_base
        if api_base is not None
        else env_api_base or coordinator.get("api_base")
    )

    return Config(
        provider=provider_value,
        model=str(raw_model)
        if raw_model
        else "gpt-5.4"
        if provider_value == "openai_compat"
        else "codex-mini-latest",
        api_base=str(raw_api_base)
        if raw_api_base
        else "https://openrouter.ai/api/v1"
        if provider_value == "openai_compat"
        else "",
        api_key=api_key
        if api_key is not None
        else env_api_key or str(coordinator.get("api_key", "")),
        codex_auth_path=codex_auth_path
        if codex_auth_path is not None
        else env_codex_auth_path or str(coordinator.get("codex_auth_path", "")),
        max_turns=max_turns
        if max_turns is not None
        else int(coordinator.get("max_turns", Config.max_turns)),
        max_retries=max_retries
        if max_retries is not None
        else int(coordinator.get("max_retries", Config.max_retries)),
        max_depth=max_depth
        if max_depth is not None
        else int(coordinator.get("max_depth", Config.max_depth)),
        system_prompt_extension="\n\n".join(part for part in prompt_parts if part),
        context_limit=(
            int(coordinator["context_limit"])
            if coordinator.get("context_limit") is not None
            else None
        ),
        shutdown_timeout_s=float(
            coordinator.get("shutdown_timeout_s", Config.shutdown_timeout_s)
        ),
        allowed_agents=cli_allowed_agents
        if cli_allowed_agents is not None
        else _parse_allowed_agents(coordinator.get("allowed_agents")),
        agent_templates=agent_templates,
        cwd=str(start_dir),
        global_config_path=global_path,
        local_config_path=local_path,
    )
