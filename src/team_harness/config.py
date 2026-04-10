from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
import os
from pathlib import Path
import tomllib
from typing import cast
from typing import Literal
import warnings

from team_harness.agents.template import AgentTemplate
from team_harness.agents.template import DEFAULT_AGENT_TEMPLATES
from team_harness.agents.template import SessionCapture
from team_harness.coordinator.system_prompt import COORDINATOR_PROMPT
from team_harness.coordinator.system_prompt import DEFAULT_WORKER_FOOTER

LOCAL_CONFIG_DIR_NAME = ".team-harness"
CONFIG_PATH = Path.home() / ".team-harness" / "config.toml"
RUNS_DIR = Path.home() / ".team-harness" / "runs"
SKILLS_USER_DIR = Path.home() / ".team-harness" / "skills"
PROMPT_FILE_MAX_BYTES = 100 * 1024


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
    coordinator_system_message: str = COORDINATOR_PROMPT
    coordinator_prompt: str = COORDINATOR_PROMPT
    worker_suffix: str = ""
    worker_footer: str = DEFAULT_WORKER_FOOTER
    system_prompt_extension: str = ""
    output_dir: str = "_outputs"
    context_limit: int | None = None
    shutdown_timeout_s: float = 10.0
    min_agent_lifetime_before_kill_s: float = 600.0
    allowed_agents: list[str] | None = None
    agent_templates: dict[str, AgentTemplate] = field(default_factory=dict)
    cwd: str = "."
    run_dir: Path | None = None
    global_config_path: Path | None = None
    local_config_path: Path | None = None

    def __post_init__(self) -> None:
        default_message = COORDINATOR_PROMPT
        if (
            self.coordinator_system_message == default_message
            and self.coordinator_prompt != default_message
        ):
            self.coordinator_system_message = self.coordinator_prompt
        else:
            self.coordinator_prompt = self.coordinator_system_message


# Structured agent-template block used verbatim in both the global and local
# sample config files. Kept as a module-level string so the two callers and
# the round-trip test (`test_default_config_text_roundtrip_matches_builtin_defaults`)
# all see exactly the same content.
_STRUCTURED_AGENTS_BLOCK = """# Worker agent invocations. Each agent is described as a structured
# command: a base `command` list, `shared_flags` that are always applied,
# `resume_flags` that are applied only when resuming a previous session,
# and a `session_capture` sub-table describing how the harness extracts
# the provider's session id from the worker's stream-json output.
#
# These blocks override the built-in defaults. Any field you omit falls
# back to the corresponding built-in default for that agent type (so, for
# example, a custom `[agents.codex]` that only sets `model_flag` keeps all
# other fields from the default codex template).
#
# Custom (non-built-in) agent types must set at least `command`.

# Codex worker. `--json` is required so the harness can parse the initial
# `thread.started` event and capture the session id for future resume.
# `default_model` is the model passed to codex when the coordinator does
# not override it via `spawn_agent(model="...")`.
[agents.codex]
command = ["codex", "exec"]
shared_flags = [
    "--dangerously-bypass-approvals-and-sandbox",
    "--skip-git-repo-check",
    "--json",
]
resume_prefix = ["resume"]
resume_flags = ["{session_id}"]
model_flag = "--model"
default_model = "gpt-5.4"

[agents.codex.session_capture]
strategy = "stream_json_event"
match = { type = "thread.started" }
field_path = ["thread_id"]

# Gemini worker. `stream-json` gives us a parseable session id in the
# initial `init` event.
[agents.gemini]
command = ["gemini"]
shared_flags = ["--approval-mode", "yolo", "--output-format", "stream-json"]
resume_flags = ["--resume", "{session_id}"]
prompt_flag = "-p"
model_flag = "--model"

[agents.gemini.session_capture]
strategy = "stream_json_event"
match = { type = "init" }
field_path = ["session_id"]

# Claude Code worker. `--verbose` is mandatory when `-p` and
# `--output-format stream-json` are combined (Claude CLI requirement).
# Claude reads its model from several env vars, not just --model. We
# set the three "main model" vars together (ANTHROPIC_MODEL plus the
# opus/sonnet alias resolvers) so overriding the model is deterministic
# across all of Claude Code's internal code paths. We deliberately do
# NOT set ANTHROPIC_DEFAULT_HAIKU_MODEL / ANTHROPIC_SMALL_FAST_MODEL /
# CLAUDE_CODE_SUBAGENT_MODEL so cheap auxiliary helpers stay cheap.
# `default_model` is left unset by default — configure per-project.
[agents.claude]
command = ["claude"]
shared_flags = [
    "-p",
    "--dangerously-skip-permissions",
    "--output-format", "stream-json",
    "--verbose",
]
resume_flags = ["--resume", "{session_id}"]
model_flag = "--model"
model_env_vars = [
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
]
# default_model = "claude-sonnet-4-6"   # uncomment to pin a default

[agents.claude.session_capture]
strategy = "stream_json_event"
match = { type = "system", subtype = "init" }
field_path = ["session_id"]

[agents.opencode]
command = ["opencode"]

[agents.pi]
command = ["pi", "--print", "--no-session"]

[agents.harness]
command = ["th", "run"]
model_flag = "--model"
"""


def _default_config_text() -> str:
    return (
        """# th — global configuration
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

# Coordinator system-message, worker suffix, and worker footer files.
coordinator_system_message_file = "coordinator_system_message.md"
worker_suffix_file = "worker_suffix.md"
worker_footer_file = "worker_footer.md"

# Extra text appended to the system prompt for every run.
system_prompt = ""

# Base directory for per-session coordinator/worker artifacts.
output_dir = "_outputs"

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

# Minimum lifetime before kill_agent will refuse early termination.
min_agent_lifetime_before_kill_s = 600.0

# Restrict which agent types the coordinator can spawn. Leave commented to allow all.
# allowed_agents = ["codex", "gemini", "claude", "opencode", "pi", "harness"]

# --- Experimental Codex subscription coordinator ---
# provider = "codex"
# model = "codex-mini-latest"
# codex_auth_path = "~/.codex/auth.json"

"""
        + _STRUCTURED_AGENTS_BLOCK
    )


def _local_config_text() -> str:
    return (
        """# Project-level team-harness config.
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

# Coordinator system-message, worker suffix, and worker footer files.
coordinator_system_message_file = "coordinator_system_message.md"
worker_suffix_file = "worker_suffix.md"
worker_footer_file = "worker_footer.md"

# Extra text appended to the system prompt for every run.
system_prompt = ""

# Base directory for per-session coordinator/worker artifacts.
output_dir = "_outputs"

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

# Minimum lifetime before kill_agent will refuse early termination.
min_agent_lifetime_before_kill_s = 600.0

# Restrict which agent types the coordinator can spawn. Leave commented to allow all.
# allowed_agents = ["codex", "gemini", "claude", "opencode", "pi", "harness"]

# --- Experimental Codex subscription coordinator ---
# provider = "codex"
# model = "codex-mini-latest"
# codex_auth_path = ".team-harness/codex-auth.json"

"""
        + _STRUCTURED_AGENTS_BLOCK
    )


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


def _coordinator_file_key(
    *,
    local_section: dict[str, object],
    local_config_path: Path | None,
    global_section: dict[str, object],
    global_config_path: Path | None,
    new_key: str,
    old_key: str,
) -> tuple[str, bool, str | None, Path | None]:
    for section, config_path, scope in (
        (local_section, local_config_path, "local"),
        (global_section, global_config_path, "global"),
    ):
        if config_path is None:
            continue
        has_new = new_key in section
        has_old = old_key in section if old_key != new_key else False
        if not has_new and not has_old:
            continue
        if has_new and has_old:
            warnings.warn(
                f"Both coordinator.{old_key} and coordinator.{new_key} are set in the "
                f"{scope} config; coordinator.{new_key} wins.",
                stacklevel=2,
            )
        if has_new:
            value = section.get(new_key)
            return True, False, value if isinstance(value, str) else None, config_path
        value = section.get(old_key)
        return True, True, value if isinstance(value, str) else None, config_path
    return False, False, None, None


def _parse_string_tuple(raw: object, *, key: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise SystemExit(f"agents.*.{key} must be an array of strings.")
    values: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise SystemExit(f"agents.*.{key} must be an array of strings.")
        values.append(item)
    return tuple(values)


def _parse_field_path(raw: object) -> tuple[str, ...] | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        parts = tuple(part for part in raw.split(".") if part)
        return parts or None
    if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        return tuple(raw)
    raise SystemExit("agents.*.session_capture.field_path must be a string or array.")


def _parse_session_capture(raw: object) -> SessionCapture | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SystemExit("agents.*.session_capture must be a table.")
    strategy = raw.get("strategy")
    if strategy is None:
        strategy_value: Literal["stream_json_event", "pre_generated_uuid"] | None = None
    elif isinstance(strategy, str) and strategy in {
        "stream_json_event",
        "pre_generated_uuid",
    }:
        strategy_value = cast(
            Literal["stream_json_event", "pre_generated_uuid"], strategy
        )
    else:
        raise SystemExit(
            "agents.*.session_capture.strategy must be "
            "'stream_json_event' or 'pre_generated_uuid'."
        )
    match = raw.get("match")
    match_value: dict[str, str] | None = None
    if match is not None:
        if not isinstance(match, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in match.items()
        ):
            raise SystemExit(
                "agents.*.session_capture.match must be a table of string values."
            )
        match_value = dict(match)
    field_path_value = _parse_field_path(raw.get("field_path"))
    capture = SessionCapture(
        strategy=strategy_value, match=match_value, field_path=field_path_value
    )
    if capture.strategy == "stream_json_event" and (
        capture.match is None or capture.field_path is None
    ):
        raise SystemExit(
            "agents.*.session_capture.strategy='stream_json_event' requires "
            "match and field_path."
        )
    return capture


def _reject_legacy_agent_templates(
    data: dict[str, object], source_path: Path | None
) -> None:
    """Raise a clear migration error if any `[agents.<name>]` table in the
    given raw TOML data still uses the legacy single-string `template` form.

    Called once per source file (global and local) so the error can name
    the exact file the offending key came from — after `_deep_merge` that
    information is lost.
    """

    agents = data.get("agents")
    if not isinstance(agents, dict):
        return
    for agent_name, section in agents.items():
        if not isinstance(agent_name, str) or not isinstance(section, dict):
            continue
        if "template" in section:
            origin = str(source_path) if source_path is not None else "<inline>"
            raise SystemExit(
                f"agents.{agent_name}.template is no longer supported "
                f"(in {origin}).\n"
                "The single-string template form was removed in team-harness "
                "after #16. Migrate to the structured form, e.g.:\n\n"
                f"    [agents.{agent_name}]\n"
                '    command = ["codex", "exec"]\n'
                "    shared_flags = ["
                '"--dangerously-bypass-approvals-and-sandbox", "--json"]\n\n'
                "See README.md → 'Adding custom agent types' for the full schema "
                "(command / shared_flags / resume_prefix / resume_flags / "
                "session_capture / model_flag), or run `th init --force` to "
                "regenerate a structured sample config."
            )


def _structured_agent_keys_present(section: dict[str, object]) -> bool:
    return any(
        key in section
        for key in (
            "command",
            "shared_flags",
            "resume_prefix",
            "resume_flags",
            "prompt_flag",
            "prompt_position",
            "model_flag",
            "model_env_vars",
            "default_model",
            "session_capture",
        )
    )


def _parse_agent_template(agent_name: str, section: dict[str, object]) -> AgentTemplate:
    base = DEFAULT_AGENT_TEMPLATES.get(agent_name)
    command_raw = section.get("command")
    if command_raw is None:
        if base is None:
            raise SystemExit(
                f"agents.{agent_name}.command is required for structured custom agents."
            )
        command = base.command
    else:
        command = _parse_string_tuple(command_raw, key="command")
        if not command:
            raise SystemExit(f"agents.{agent_name}.command must not be empty.")

    prompt_position_raw = section.get("prompt_position")
    prompt_position: Literal["tail", "after_command"]
    if prompt_position_raw is None:
        prompt_position = base.prompt_position if base else "tail"
    elif isinstance(prompt_position_raw, str) and prompt_position_raw in {
        "tail",
        "after_command",
    }:
        prompt_position = cast(Literal["tail", "after_command"], prompt_position_raw)
    else:
        raise SystemExit(
            f"agents.{agent_name}.prompt_position must be 'tail' or 'after_command'."
        )

    def _string_or_none(raw: object, *, key: str, fallback: str | None) -> str | None:
        if raw is None:
            return fallback
        if raw is False:
            return None
        if isinstance(raw, str):
            return raw
        raise SystemExit(f"agents.{agent_name}.{key} must be a string.")

    shared_flags = (
        _parse_string_tuple(section["shared_flags"], key="shared_flags")
        if "shared_flags" in section
        else base.shared_flags
        if base
        else ()
    )
    resume_prefix = (
        _parse_string_tuple(section["resume_prefix"], key="resume_prefix")
        if "resume_prefix" in section
        else base.resume_prefix
        if base
        else ()
    )
    resume_flags = (
        _parse_string_tuple(section["resume_flags"], key="resume_flags")
        if "resume_flags" in section
        else base.resume_flags
        if base
        else ()
    )
    prompt_flag = _string_or_none(
        section.get("prompt_flag"),
        key="prompt_flag",
        fallback=base.prompt_flag if base else None,
    )
    model_flag = _string_or_none(
        section.get("model_flag"),
        key="model_flag",
        fallback=base.model_flag if base else "--model",
    )
    model_env_vars = (
        _parse_string_tuple(section["model_env_vars"], key="model_env_vars")
        if "model_env_vars" in section
        else base.model_env_vars
        if base
        else ()
    )
    # `default_model` accepts:
    #   - absent (key not present)                → inherit from base
    #   - `false`                                  → explicitly clear (None)
    #   - `""` (empty string)                      → explicitly clear (None)
    #   - any non-empty string                     → use that value
    if "default_model" not in section:
        default_model = base.default_model if base else None
    else:
        default_model_raw = section["default_model"]
        if default_model_raw is False or default_model_raw == "":
            default_model = None
        elif isinstance(default_model_raw, str):
            default_model = default_model_raw
        else:
            raise SystemExit(
                f"agents.{agent_name}.default_model must be a string, "
                "false, or omitted."
            )
    session_capture = (
        _parse_session_capture(section["session_capture"])
        if "session_capture" in section
        else base.session_capture
        if base
        else None
    )
    return AgentTemplate(
        command=command,
        shared_flags=shared_flags,
        resume_prefix=resume_prefix,
        resume_flags=resume_flags,
        prompt_flag=prompt_flag,
        prompt_position=prompt_position,
        model_flag=model_flag,
        model_env_vars=model_env_vars,
        default_model=default_model,
        session_capture=session_capture,
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
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"Invalid TOML in {path}: {exc}") from exc
    return data


def _get_section(data: dict[str, object], key: str) -> dict[str, object]:
    section = data.get(key, {})
    if isinstance(section, dict):
        return section
    return {}


def _resolve_config_prompt_path(config_path: Path, file_path: str) -> Path:
    prompt_path = Path(file_path).expanduser()
    if prompt_path.is_absolute():
        return prompt_path
    return (config_path.parent / prompt_path).resolve()


def _read_prompt_file(path: Path, *, label: str) -> str:
    try:
        size_bytes = path.stat().st_size
    except FileNotFoundError:
        raise
    except PermissionError as exc:
        raise SystemExit(f"Cannot read {label} {path}: permission denied.") from exc
    except OSError as exc:
        raise SystemExit(f"Cannot read {label} {path}: {exc}") from exc

    if size_bytes > PROMPT_FILE_MAX_BYTES:
        raise SystemExit(
            f"Cannot read {label} {path}: file exceeds {PROMPT_FILE_MAX_BYTES} bytes."
        )

    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise
    except PermissionError as exc:
        raise SystemExit(f"Cannot read {label} {path}: permission denied.") from exc
    except UnicodeDecodeError as exc:
        raise SystemExit(f"Cannot read {label} {path}: file must be UTF-8.") from exc
    except OSError as exc:
        raise SystemExit(f"Cannot read {label} {path}: {exc}") from exc


def _resolve_base_prompt_text(
    *,
    local_section: dict[str, object],
    local_config_path: Path | None,
    global_section: dict[str, object],
    global_config_path: Path | None,
    new_key: str,
    old_key: str | None = None,
    fallback: str,
) -> str:
    configured, used_old_key, file_path, source_config_path = _coordinator_file_key(
        local_section=local_section,
        local_config_path=local_config_path,
        global_section=global_section,
        global_config_path=global_config_path,
        new_key=new_key,
        old_key=old_key or new_key,
    )
    if not configured:
        return fallback
    display_key = old_key if used_old_key and old_key is not None else new_key
    if used_old_key and old_key is not None:
        warnings.warn(
            f"coordinator.{old_key} is deprecated; rename it to "
            f"coordinator.{new_key}. The old key will be removed in 1.0.",
            stacklevel=2,
        )
    if not file_path or not file_path.strip() or source_config_path is None:
        warnings.warn(
            f"Configured coordinator.{display_key} is empty; falling back to built-in "
            "base prompt.",
            stacklevel=2,
        )
        return fallback

    resolved_path = _resolve_config_prompt_path(source_config_path, file_path)
    try:
        text = _read_prompt_file(resolved_path, label=f"coordinator.{display_key}")
    except FileNotFoundError:
        warnings.warn(
            f"Configured coordinator.{display_key} {resolved_path} was not found; "
            "falling back to the built-in base prompt.",
            stacklevel=2,
        )
        return fallback

    if not text.strip():
        warnings.warn(
            f"Configured coordinator.{display_key} {resolved_path} is empty; falling "
            "back to the built-in base prompt.",
            stacklevel=2,
        )
        return fallback
    return text


def _resolve_optional_prompt_text(
    *,
    local_section: dict[str, object],
    local_config_path: Path | None,
    global_section: dict[str, object],
    global_config_path: Path | None,
    key: str,
    fallback: str,
) -> str:
    configured, _, file_path, source_config_path = _coordinator_file_key(
        local_section=local_section,
        local_config_path=local_config_path,
        global_section=global_section,
        global_config_path=global_config_path,
        new_key=key,
        old_key=key,
    )
    if (
        not configured
        or not file_path
        or not file_path.strip()
        or source_config_path is None
    ):
        return fallback

    resolved_path = _resolve_config_prompt_path(source_config_path, file_path)
    try:
        text = _read_prompt_file(resolved_path, label=f"coordinator.{key}")
    except FileNotFoundError:
        return fallback
    if not text.strip():
        return fallback
    return text


def _read_cli_prompt_extension(cli_system_prompt_file: str, cwd: Path) -> str:
    prompt_path = Path(cli_system_prompt_file).expanduser()
    if not prompt_path.is_absolute():
        prompt_path = (cwd / prompt_path).resolve()
    try:
        return _read_prompt_file(prompt_path, label="CLI --system-prompt-file")
    except FileNotFoundError:
        raise SystemExit(f"CLI --system-prompt-file not found: {prompt_path}") from None


def _coordinator_int(
    coordinator: dict[str, object], key: str, default: int | float
) -> int:
    raw = cast(int | float | str, coordinator.get(key, default))
    return int(raw)


def _coordinator_float(
    coordinator: dict[str, object], key: str, default: float
) -> float:
    raw = cast(int | float | str, coordinator.get(key, default))
    return float(raw)


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
    cli_system_prompt_file: str | None = None,
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

    # Validate each source file separately BEFORE deep-merge so the
    # migration error can cite the exact file that still contains a
    # legacy `template = "..."` key.
    _reject_legacy_agent_templates(global_data, global_path)
    _reject_legacy_agent_templates(local_data, local_path)

    global_coordinator = _get_section(global_data, "coordinator")
    local_coordinator = _get_section(local_data, "coordinator")
    config_data = _deep_merge(base=global_data, override=local_data)

    coordinator = _get_section(config_data, "coordinator")
    agents_section = config_data.get("agents", {})
    if not isinstance(agents_section, dict):
        agents_section = {}

    agent_templates: dict[str, AgentTemplate] = {}
    for agent_name, section in agents_section.items():
        if isinstance(agent_name, str) and isinstance(section, dict):
            if _structured_agent_keys_present(section):
                agent_templates[agent_name] = _parse_agent_template(agent_name, section)

    prompt_parts: list[str] = []
    config_prompt = coordinator.get("system_prompt")
    if isinstance(config_prompt, str) and config_prompt:
        prompt_parts.append(config_prompt)
    if system_prompt:
        prompt_parts.append(system_prompt)
    if cli_system_prompt_file:
        prompt_parts.append(
            _read_cli_prompt_extension(cli_system_prompt_file, start_dir)
        )

    coordinator_system_message = _resolve_base_prompt_text(
        local_section=local_coordinator,
        local_config_path=local_path,
        global_section=global_coordinator,
        global_config_path=global_path,
        new_key="coordinator_system_message_file",
        old_key="coordinator_prompt_file",
        fallback=COORDINATOR_PROMPT,
    )
    worker_suffix = _resolve_optional_prompt_text(
        local_section=local_coordinator,
        local_config_path=local_path,
        global_section=global_coordinator,
        global_config_path=global_path,
        key="worker_suffix_file",
        fallback="",
    )
    worker_footer = _resolve_optional_prompt_text(
        local_section=local_coordinator,
        local_config_path=local_path,
        global_section=global_coordinator,
        global_config_path=global_path,
        key="worker_footer_file",
        fallback=DEFAULT_WORKER_FOOTER,
    )

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
        else _coordinator_int(coordinator, "max_turns", Config.max_turns),
        max_retries=max_retries
        if max_retries is not None
        else _coordinator_int(coordinator, "max_retries", Config.max_retries),
        max_depth=max_depth
        if max_depth is not None
        else _coordinator_int(coordinator, "max_depth", Config.max_depth),
        coordinator_system_message=coordinator_system_message,
        coordinator_prompt=coordinator_system_message,
        worker_suffix=worker_suffix,
        worker_footer=worker_footer,
        system_prompt_extension="\n\n".join(part for part in prompt_parts if part),
        output_dir=str(coordinator.get("output_dir", Config.output_dir)),
        context_limit=(
            int(cast(int | str, coordinator["context_limit"]))
            if coordinator.get("context_limit") is not None
            else None
        ),
        shutdown_timeout_s=_coordinator_float(
            coordinator, "shutdown_timeout_s", Config.shutdown_timeout_s
        ),
        min_agent_lifetime_before_kill_s=_coordinator_float(
            coordinator,
            "min_agent_lifetime_before_kill_s",
            Config.min_agent_lifetime_before_kill_s,
        ),
        allowed_agents=cli_allowed_agents
        if cli_allowed_agents is not None
        else _parse_allowed_agents(
            cast(str | list[str] | None, coordinator.get("allowed_agents"))
        ),
        agent_templates=agent_templates,
        cwd=str(start_dir),
        global_config_path=global_path,
        local_config_path=local_path,
    )
