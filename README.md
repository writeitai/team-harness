# team-harness

A lightweight, model-agnostic multi-agent orchestration harness. It runs a coordinator LLM through either an OpenAI-compatible API or an experimental Codex subscription backend and lets that coordinator spawn external worker CLIs (Codex, Gemini, Claude Code, opencode, pi, OpenHands, or nested th runs) as tool-use actions.

## Installation

```bash
pip install team-harness
# or
uv tool install team-harness
```

The installed command is `th`. The legacy command `team-harness` also works as a compatibility alias.

Development setup:

```bash
git clone https://github.com/writeitai/team-harness.git
cd team-harness
uv sync --extra dev
uv run th --help
```

## Prerequisites

Worker CLIs must be installed and authenticated separately. You do not need all of them; restrict a run with `--agents codex,gemini` to use only the ones you have.
Install with `pip install openhands` (the PyPI distribution name is `openhands`, provided by the OpenHands-CLI repo).

| Worker    | Install docs                                                |
|-----------|-------------------------------------------------------------|
| `codex`   | [Codex CLI](https://github.com/openai/codex)               |
| `gemini`  | [Gemini CLI](https://github.com/google-gemini/gemini-cli)  |
| `claude`  | [Claude Code](https://docs.anthropic.com/en/docs/claude-code) |
| `openhands` | [OpenHands CLI](https://github.com/OpenHands/OpenHands-CLI) |
| `opencode`| [opencode](https://github.com/opencode-ai/opencode)        |
| `pi`      | [pi](https://github.com/badlogic/pi-mono)                  |

## Quick start

```bash
# Set your API key
export OPENROUTER_API_KEY="sk-or-..."

# Create a project-local config in ./.team-harness/
# Creates config.toml, coordinator_system_message.md, worker_suffix.md, and worker_footer.md
th init

# Single-shot run
th run "Write unit tests for src/utils.py using pytest"

# From a file
th run -f task.txt

# Interactive REPL
th repl

# View run logs
th logs
th logs <run-id>
```

Experimental Codex subscription coordinator:

```bash
codex login
team-harness run --provider codex --model codex-mini-latest "Review this repo and file issues"
```

## Python SDK

Use team-harness programmatically from Python:

```python
import asyncio
from team_harness import Harness, HarnessResult

async def main():
    harness = Harness(
        api_key="sk-or-...",
        model="anthropic/claude-sonnet-4",
        agents=["codex", "gemini"],
    )
    result: HarnessResult = await harness.run(
        "Write unit tests for src/utils.py using pytest"
    )
    print(result.text)
    for agent in result.agents:
        print(f"  {agent.id} ({agent.agent_type}): {agent.status}")

asyncio.run(main())
```

All CLI options are available as constructor parameters:

```python
harness = Harness(
    provider="codex",           # or "openai_compat" (default)
    model="codex-mini-latest",
    api_base="https://openrouter.ai/api/v1",
    api_key="sk-or-...",
    codex_auth_path="~/.codex/auth.json",
    agents=["codex", "gemini"], # or "codex,gemini"
    max_turns=50,
    max_retries=5,
    max_depth=3,
    system_prompt="Extra instructions",
    system_prompt_file="prompt.txt",
    cwd="./project",
    console_mode="silent",      # "silent" | "auto" | "plain" | "rich"
)
```

The `run()` method returns a `HarnessResult` with:

- `text` -- final assistant response
- `agents` -- list of `AgentSummary` (id, agent_type, status, exit_code, cwd)
- `run_id` -- unique run identifier

Errors raise `HarnessError`. Run logs are always finalized, even on failure.

## Configuration

th works out of the box with built-in defaults. To create a config file explicitly:

```bash
# Create project-local config for the current repo
th init

# Create global config under ~/.team-harness/config.toml
th init --global

# Overwrite an existing config file
th init --force
th init --global --force
```

Global config is intended for user-wide defaults. Project config is intended for repo-specific settings and should not contain secrets; keep API keys in environment variables.

Example global config:

```toml
[coordinator]
provider = "openai_compat"
model = "gpt-5.4"
api_base = "https://openrouter.ai/api/v1"
coordinator_system_message_file = "coordinator_system_message.md"
worker_suffix_file = "worker_suffix.md"
worker_footer_file = "worker_footer.md"
system_prompt = ""
output_dir = "_outputs"

# Worker agents are described as structured commands: a base `command`
# list, `shared_flags` that are always applied, and `resume_flags` that
# are applied only when resuming a previous session. A `session_capture`
# sub-table describes how the harness extracts the provider's session id
# from the worker's stream-json output so the run can be resumed later.
#
# Any field you omit is inherited from the built-in default for that
# agent type, so it is fine to override only the piece you care about.
# Run `th init --force` to regenerate a complete, commented sample.

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
reasoning_effort_flag = ["-c", "model_reasoning_effort={effort}"]
# reasoning_effort = "high"   # uncomment to pin a level

[agents.codex.session_capture]
strategy = "stream_json_event"
match = { type = "thread.started" }
field_path = ["thread_id"]

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
reasoning_effort_flag = ["--effort", "{effort}"]
# default_model = "claude-sonnet-4-6"   # uncomment to pin a default
# reasoning_effort = "high"               # values: low|medium|high|max

# Uncomment the provider_env block to route claude through OpenRouter.
# See "Connecting workers to OpenRouter" below for the full recipe.
# [agents.claude.provider_env]
# ANTHROPIC_BASE_URL = "https://openrouter.ai/api"
# ANTHROPIC_AUTH_TOKEN = "{env:OPENROUTER_API_KEY}"
# ANTHROPIC_API_KEY = ""

[agents.claude.session_capture]
strategy = "stream_json_event"
match = { type = "system", subtype = "init" }
field_path = ["session_id"]

[agents.openhands]
command = ["openhands"]
shared_flags = ["--headless", "--json", "--override-with-envs"]
prompt_flag = "-t"
model_env_vars = ["LLM_MODEL"]

[agents.opencode]
command = ["opencode"]

[agents.pi]
command = ["pi", "--print", "--no-session"]

[agents.harness]
command = ["th", "run"]
model_flag = "--model"
```

OpenHands runs are not auto-resumable from team-harness today. The `--json` output format is not parseable as stream-json.

`--override-with-envs` is required so `LLM_MODEL` injection works. A side-effect is that any `LLM_MODEL`, `LLM_API_KEY`, or `LLM_BASE_URL` already set in your shell will also be picked up by the worker. Unset or override them if you want deterministic per-run behavior.

Custom `[agents.openhands]` sections in existing `.team-harness/config.toml` files will, after upgrade, inherit the new built-in defaults for any fields they do not explicitly set (including `shared_flags`). If your custom section was a standalone agent that coincidentally used the name `openhands`, rename it or explicitly clear inherited fields (e.g. `shared_flags = []`, `prompt_flag = false`, `model_env_vars = []`).

### Prompt configuration

`th init` creates four files in the target `.team-harness/` directory:

| File                | Purpose                                                                     |
|---------------------|-----------------------------------------------------------------------------|
| `config.toml`            | All coordinator and agent settings                                          |
| `coordinator_system_message.md`  | Editable coordinator base prompt (seeded from the built-in default)         |
| `worker_suffix.md`       | Text automatically appended to every spawned worker prompt (empty by default)|
| `worker_footer.md`       | Default worker output requirements template, editable per project           |

Prompt-related config keys:

| Key                       | Purpose                                                                    |
|---------------------------|----------------------------------------------------------------------------|
| `coordinator_system_message_file` | Path to the coordinator base prompt file                                   |
| `worker_suffix_file`      | Path to text appended to every spawned worker prompt                       |
| `worker_footer_file`      | Path to the worker footer template appended after the suffix               |
| `system_prompt`           | Inline extension text appended after the coordinator base prompt           |

**`coordinator_system_message_file`** — Points to the coordinator base prompt file. If the file is missing, a warning is emitted and the built-in default is used. If no key is configured, the built-in default is used silently.

**`worker_suffix_file`** — Points to a file whose contents are appended to every spawned worker prompt. The coordinator is told that this suffix exists so it does not duplicate those instructions. If the file is missing or empty, no suffix is appended.

**`worker_footer_file`** — Points to a file whose contents define the footer appended to every spawned worker prompt. The footer should usually keep the `{session_output_dir}` placeholder so workers are told where to write artifacts. If the file is missing or empty, the built-in footer is used.

**`system_prompt`** — Inline text appended as an extension after the base prompt. This is separate from `coordinator_system_message_file` and is additive.

**CLI `--system-prompt-file`** — Reads extra text from a file and appends it as a runtime extension. This is an extension source (like `system_prompt`), not a base prompt replacement. CLI paths resolve relative to the current working directory.

Prompt file paths in `config.toml` resolve relative to the directory containing the config file that defined them. Absolute paths are used as-is.

Prompt files are read with UTF-8 encoding and are limited to 100 KB. Files that exceed this limit, are not valid UTF-8, or are unreadable produce a clear error message.

Experimental Codex config:

```toml
[coordinator]
provider = "codex"
model = "codex-mini-latest"
# optional override for custom proxies or tests
# api_base = "https://chatgpt.com/backend-api"
# optional explicit auth location
# codex_auth_path = "~/.codex/auth.json"
```

### Project-level configuration

`th init` writes `./.team-harness/config.toml`, `coordinator_system_message.md`, `worker_suffix.md`, and `worker_footer.md`. Local config discovery walks upward from the effective `--cwd` and the nearest ancestor config overrides the global file.

Lists replace rather than extend. For example, setting `[coordinator].allowed_agents` in a local config replaces the global list instead of appending to it.

`[coordinator].output_dir` controls where per-run coordinator and worker
artifacts are written. Each run creates `<output_dir>/<run_id>/`, and the
coordinator may instruct workers to place notes, reports, logs, or other
deliverables there. The harness also writes a compact
`worker_sessions.json` manifest in that directory summarizing every spawned
worker for the run. Relative `output_dir` values resolve against the effective
`--cwd`.

`th init --force` overwrites `config.toml` but preserves existing `coordinator_system_message.md`, `worker_suffix.md`, and `worker_footer.md` files to protect user customizations. Missing sidecar files are re-created.

Project-level `.team-harness/config.toml`, `.team-harness/coordinator_system_message.md`, `.team-harness/worker_suffix.md`, and `.team-harness/worker_footer.md` should normally be committed to version control so prompt behavior is reproducible across contributors and CI.

### Configuration resolution order

1. CLI flags
2. Environment variables
3. Local `.team-harness/config.toml`
4. Global `~/.team-harness/config.toml`
5. Built-in defaults

Relevant environment variables:

- `HARNESS_PROVIDER`
- `HARNESS_MODEL`
- `HARNESS_API_BASE`
- `HARNESS_CODEX_AUTH_PATH`
- `OPENROUTER_API_KEY` or `OPENAI_API_KEY`

### Adding custom agent types

Add a new `[agents.<name>]` section with a structured command. The only
required field is `command`; everything else has sensible defaults.

```toml
[agents.myagent]
command = ["my-custom-cli"]
shared_flags = ["--mode", "auto"]
model_flag = "--model"   # set to `false` if the CLI has no model flag
```

Some CLIs use env-based model injection instead of a `--model` flag. OpenHands is the built-in example:

```toml
[agents.openhands]
command = ["openhands"]
shared_flags = ["--headless", "--json", "--override-with-envs"]
model_flag = false
model_env_vars = ["LLM_MODEL"]
```

The new type appears automatically in the coordinator's `spawn_agent` tool.
The task prompt is appended at the tail of the argv list by default; set
`prompt_position = "after_command"` if your CLI wants the prompt earlier,
or `prompt_flag = "-p"` if the prompt is introduced by a flag (like `gemini -p`).

Placeholders that can appear inside `shared_flags`, `resume_prefix`, or
`resume_flags`:

- `{session_id}` — substituted with the resume session id (resume mode only).
- `{generated_uuid}` — substituted with a harness-generated UUID at spawn
  time. Useful for CLIs like `claude` that accept `--session-id <uuid>` up
  front so the harness can record the id deterministically.

Session ids can be captured from a worker's stream-json output via a
`[agents.<name>.session_capture]` sub-table with `strategy`, `match`, and
`field_path` (see the codex/gemini/claude examples above).

### Setting a default model

Two config keys control the model a worker runs with:

- **`default_model`** — the model used when the coordinator does not pass
  an explicit `model=...` in its `spawn_agent` tool call. Absent = no
  default; worker CLI uses its own internal default.
- **`model_flag`** — the CLI flag name used to inject the model into the
  argv, e.g. `"--model"`.

Precedence:

| Source | Priority |
|---|---|
| Explicit `spawn_agent(model="…")` from the coordinator | 1 (highest) |
| `[agents.<name>].default_model` | 2 |
| Worker CLI's own internal default | 3 (fallback) |

Note: `[coordinator].model` controls the **coordinator's own** model (the
one used to talk to OpenRouter / Codex). It does NOT flow through to
workers. Per-agent defaults come from `[agents.<name>].default_model`.

#### Codex example

```toml
[agents.codex]
command = ["codex", "exec"]
default_model = "gpt-5.4"    # every codex spawn gets --model gpt-5.4
```

Clear a default on a specific agent with `default_model = false` (or an
empty string). This is useful if the built-in default is wrong for your
setup.

#### Claude example — env-var model injection

Claude Code does not rely solely on `--model`. Several internal code
paths (`getBestModel`, the Max-subscriber branch of `getDefaultMainLoopModel`)
bypass `ANTHROPIC_MODEL` and read `ANTHROPIC_DEFAULT_OPUS_MODEL` or
`ANTHROPIC_DEFAULT_SONNET_MODEL` directly. Setting just `ANTHROPIC_MODEL`
is not enough for a deterministic override.

Templates can declare `model_env_vars` — a list of env var names that the
spawner will set to the effective model on every spawn:

```toml
[agents.claude]
command = ["claude"]
shared_flags = [
    "-p",
    "--dangerously-skip-permissions",
    "--output-format", "stream-json",
    "--verbose",
]
model_flag = "--model"
model_env_vars = [
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
]
default_model = "claude-sonnet-4-6"   # optional; leave unset to let the
                                      # coordinator decide per spawn
```

The built-in `claude` default intentionally lists **only** those three
env vars and does **not** touch `ANTHROPIC_DEFAULT_HAIKU_MODEL`,
`ANTHROPIC_SMALL_FAST_MODEL`, or `CLAUDE_CODE_SUBAGENT_MODEL` — cheap
auxiliary helpers keep running on haiku. If your own shell environment
sets any of those, they pass through to the worker unchanged (the
harness only merges its own env vars on top of `os.environ`).

Merge order for child process env: `os.environ` < template `provider_env`
< template `model_env_vars` < caller's explicit `extra_env`. A test or
SDK caller can always override a template env var by passing
`extra_env={"ANTHROPIC_MODEL": "…"}`.

### Reasoning effort

Worker CLIs that expose a reasoning-effort knob are configured via two
fields:

- **`reasoning_effort`** — the value (e.g. `"high"`). Absent = no
  injection, worker CLI uses its own default.
- **`reasoning_effort_flag`** — the argv token shape, with a literal
  `{effort}` placeholder that the harness substitutes at render time.
  This ships with a sensible default per built-in agent so users
  normally only set `reasoning_effort`.

Per-CLI shapes and allowed values:

| Worker | `reasoning_effort_flag` | Allowed values |
|---|---|---|
| codex  | `["-c", "model_reasoning_effort={effort}"]` | `low`, `medium`, `high`, `xhigh` |
| claude | `["--effort", "{effort}"]` | `low`, `medium`, `high`, `max` |
| gemini | (not supported upstream) | — |

The harness does **not** validate the value against a per-CLI enum. Pass
what the worker CLI accepts; invalid levels are reported by the worker.

Example — pin codex to high effort:

```toml
[agents.codex]
reasoning_effort = "high"
```

Clear a default with `reasoning_effort = false` (same convention as
`default_model`).

### Connecting workers to OpenRouter

team-harness can route worker CLIs through OpenRouter so the same
OpenRouter account that fuels the coordinator also fuels each worker.
This relies on a third template field, `provider_env` — a list of env
vars the spawner sets on the child process. Values may contain
`{env:VARNAME}` placeholders that are resolved from the parent shell at
spawn time, so API keys stay in your shell and never touch `config.toml`.

Before either recipe: export your OpenRouter key in your shell once:

```bash
export OPENROUTER_API_KEY=sk-or-...
```

#### Codex via OpenRouter

Codex reads its provider config from a `-c` command-line override (or
the `~/.codex/config.toml` file). Add the `-c` tokens to the codex
template's `shared_flags`:

```toml
[agents.codex]
command = ["codex", "exec"]
shared_flags = [
    "--dangerously-bypass-approvals-and-sandbox",
    "--skip-git-repo-check",
    "--json",
    "-c", "model_provider=openrouter",
    "-c", 'model_providers.openrouter.name="openrouter"',
    "-c", 'model_providers.openrouter.base_url="https://openrouter.ai/api/v1"',
    "-c", 'model_providers.openrouter.env_key="OPENROUTER_API_KEY"',
]
default_model = "openai/gpt-5.3-codex"   # the OpenRouter-flavoured model name
```

No `provider_env` needed — codex reads `OPENROUTER_API_KEY` itself via
the `env_key` setting.

#### Claude Code via OpenRouter

Claude Code reads its provider config from env vars. Add a
`[agents.claude.provider_env]` sub-table:

```toml
[agents.claude]
default_model = "anthropic/claude-opus-4.6"   # OpenRouter-flavoured model name

[agents.claude.provider_env]
ANTHROPIC_BASE_URL = "https://openrouter.ai/api"
ANTHROPIC_AUTH_TOKEN = "{env:OPENROUTER_API_KEY}"
ANTHROPIC_API_KEY = ""   # must be empty — prevents Claude Code from falling back to native auth
```

The `{env:OPENROUTER_API_KEY}` placeholder is resolved from `os.environ`
at spawn time. If the variable is missing, the harness warns once and
substitutes an empty string (the child will then fail its own auth with
a clear message).

Note: the three `ANTHROPIC_DEFAULT_*_MODEL` env vars from the "Setting a
default model" section continue to work and layer on top of
`provider_env` — setting `default_model = "anthropic/claude-opus-4.6"`
above populates all three of them automatically.

#### Gemini via OpenRouter

Not supported by the upstream `gemini` CLI — it authenticates directly
against Google APIs with no OpenAI-compatible base-URL mode. The harness
does not ship a recipe.

### Migrating from legacy single-string templates

Earlier versions of team-harness accepted a `template = "codex exec ... {prompt}"`
single-string form. That form was deprecated in #16 and **removed** in the
follow-up refactor. Attempting to load a config that still contains a
`template = "..."` line now raises a clear error naming the offending file:

```
agents.codex.template is no longer supported (in /path/to/config.toml).
The single-string template form was removed in team-harness after #16.
Migrate to the structured form, e.g.:

    [agents.codex]
    command = ["codex", "exec"]
    shared_flags = ["--dangerously-bypass-approvals-and-sandbox", "--json"]

See README.md → 'Adding custom agent types' for the full schema ...
```

The fastest migration path is:

```bash
th init --force    # regenerates a complete structured sample
```

`th init --force` preserves your existing `coordinator_system_message.md`,
`worker_suffix.md`, and `worker_footer.md` sidecar files, so you can use it
to regenerate just `config.toml`.

### Authentication

- `provider = "openai_compat"` uses your OpenRouter or other OpenAI-compatible API key.
- `provider = "codex"` uses the auth file written by `codex login`.
- Codex auth resolution order is:
  1. `codex_auth_path` from CLI or config
  2. `HARNESS_CODEX_AUTH_PATH`
  3. `$CODEX_HOME/auth.json`
  4. `~/.codex/auth.json`
- Codex auth path values that are relative resolve against the effective harness `--cwd`.
- Each worker CLI uses its own native auth and local config.
- The harness does not forward the coordinator API key to workers unless you explicitly pass environment overrides at spawn time.

### Codex Subscription

`provider = "codex"` is experimental. team-harness talks to the ChatGPT Codex Responses SSE endpoint through a shared `httpx` client and still uses the same `model` field in config and CLI overrides.

Known built-in Codex model names:

- `codex-mini-latest`
- `openai/codex-mini-latest`
- `gpt-5.1-codex-mini`
- `openai/gpt-5.1-codex-mini`
- `gpt-5.1-codex-max`
- `openai/gpt-5.1-codex-max`

Unknown Codex models still work, but startup prints a warning because context tracking may be inaccurate.

## CLI flags

```
th run [OPTIONS] [TASK]

Options:
  -f, --file PATH            Read task from file instead of argument
  --provider TEXT             Coordinator provider: "openai_compat" or "codex"
  --model TEXT                Override coordinator model (e.g. "anthropic/claude-sonnet-4")
  --api-base TEXT             Override coordinator base URL
  --api-key TEXT              Override coordinator API key for openai_compat
  --codex-auth-path TEXT      Override Codex auth.json location
  --agents TEXT               Comma-separated allowlist (e.g. "codex,gemini")
  --max-turns INT             Maximum coordinator turns (default: 50)
  --max-retries INT           API retry budget for 429/5xx errors (default: 5)
  --max-depth INT             Nested harness depth limit (default: 3)
  --system-prompt TEXT        Extra text appended to the system prompt
  --system-prompt-file PATH   Read system prompt extension from file
  --cwd PATH                  Working directory for the run (default: ".")
```

`th repl` accepts the same options (except `-f`/`--file` and the `TASK` argument).

## REPL commands

| Command    | Description                                                     |
|------------|-----------------------------------------------------------------|
| `/clear`   | Clear conversation history and context tracking; start fresh    |
| `/reset`   | Alias for `/clear`                                              |
| `/quit`    | Graceful shutdown: wait for running agents, then exit           |
| `/agents`  | Print current agent status table inline                         |
| `/log`     | Print the path to the current run log                           |

## Context management

- The status bar shows current context occupancy from the latest exact API usage, not cumulative spend.
- When local changes exist after the last exact usage update, the displayed total may be estimated and prefixed with `~`.
- Auto-compaction runs proactively before a new coordinator turn once the model-specific threshold is reached.
- Auto-compaction only runs when the last message role is `user`, so it never compacts in the middle of a tool exchange.
- Auto-compaction is always on in v1 and does not have a public config knob.
- `/clear` is the manual escape hatch when you want to keep the same session, run log, and agent state but start with a fresh conversation.

## REPL editing keys

| Key              | Action                                         |
|------------------|------------------------------------------------|
| `Enter`          | Submit the current input                       |
| `Alt+Enter`      | Insert a newline (multi-line editing)          |
| `Esc Esc`        | Clear the entire input buffer                  |
| `Ctrl+C`         | Clear current input without exiting the REPL   |
| `Ctrl+D`         | Exit the REPL (when the input buffer is empty) |
| `Up` / `Down`    | Navigate input history within the session      |

Standard cursor movement keys (Left/Right, Home/End, Ctrl+A/E, Ctrl+W, Ctrl+K) work as expected.

If Alt/Esc key sequences feel delayed in tmux, set `set -sg escape-time 0` in your tmux config.

## Coordinator tools

The coordinator model has access to these tools:

**Agent management:** `spawn_agent`, `kill_agent`, `agent_status`, `list_agents`, `wait_for_agents`, `wait_for_any`, `read_new_agent_output`

**File system:** `read_file`, `write_file`, `append_file`, `edit_file`, `multi_edit_file`, `ls`, `glob`, `grep`, `read_new_file_content`

**Shell:** `bash`

**Task tracking:** `todo_write`, `todo_read`

## Skills

Skills are Python modules loaded from `~/.team-harness/skills/` and `<effective cwd>/skills/`. Each skill exports `name`, `description`, `parameters_schema`, and an async `execute(**args, ctx)` function.

Example (`skills/summarise.py`):

```python
name = "summarise_file"
description = "Summarise a file using the coordinator model."
parameters_schema = {
    "type": "object",
    "properties": {"path": {"type": "string"}},
    "required": ["path"],
}

async def execute(path: str, ctx):
    content = await ctx.read_file(path)
    # ctx.client gives access to the coordinator model
    return f"Summary of {path}: {len(content)} chars"
```

## Run logs

Each run creates a directory under `~/.team-harness/runs/<run-id>/` containing:

- `run.json` — full delta-based run log (losslessly replayable conversation)
- `<agent-id>_stdout.log` / `<agent-id>_stderr.log` — per-agent output
- `todo.json` — persistent task list

Each run also creates `<output_dir>/<run-id>/worker_sessions.json`, a compact
worker index with per-agent prompt, status, timestamps, log paths, and
resume-related metadata.

## Trust model

- **Skills** execute arbitrary Python with the harness process's full privileges. Treat skill directories as you would your `PATH`.
- **`bash` tool** runs shell commands unsandboxed with `stdin=/dev/null`.
- **Worker CLIs** are separate local processes that may read/write files in their assigned working directories.
- The harness only sends coordinator task content and tool outputs to the configured API endpoint.

This tool is designed for trusted local automation. Do not run untrusted tasks or skills.

## Migration

The preferred CLI command is now `th`. If you are upgrading from a previous version:

- `team-harness` still works as a compatibility alias.
- `pip install team-harness` does not change.
- `python -m team_harness` does not change.
- Config, runs, and skills remain under `~/.team-harness/`.
- Existing config files are not modified by upgrades.

## Development

```bash
uv sync --extra dev
uv run ruff check src/        # lint
uv run ruff format src/        # format
uv run pyright src/             # type check
uv run pytest src/tests/ -v    # test
```

## License

MIT
