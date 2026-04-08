# team-harness

A lightweight, model-agnostic multi-agent orchestration harness. It runs a coordinator LLM through either an OpenAI-compatible API or an experimental Codex subscription backend and lets that coordinator spawn external worker CLIs (Codex, Gemini, Claude Code, opencode, pi, or nested th runs) as tool-use actions.

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

| Worker    | Install docs                                                |
|-----------|-------------------------------------------------------------|
| `codex`   | [Codex CLI](https://github.com/openai/codex)               |
| `gemini`  | [Gemini CLI](https://github.com/google-gemini/gemini-cli)  |
| `claude`  | [Claude Code](https://docs.anthropic.com/en/docs/claude-code) |
| `opencode`| [opencode](https://github.com/opencode-ai/opencode)        |
| `pi`      | [pi](https://github.com/badlogic/pi-mono)                  |

## Quick start

```bash
# Set your API key
export OPENROUTER_API_KEY="sk-or-..."

# Create a project-local config in ./.team-harness/config.toml
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

[agents.codex]
template = "codex exec --yolo --model gpt-5.4 PROMPT=\"{prompt}\""

[agents.gemini]
template = "gemini --approval-mode=yolo -p \"{prompt}\""

[agents.claude]
template = "claude -p --dangerously-skip-permissions {prompt}"

[agents.opencode]
template = "opencode {prompt}"

[agents.pi]
template = "pi --print --no-session {prompt}"

[agents.harness]
template = "th run {prompt}"
```

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

`th init` writes `./.team-harness/config.toml`. Local config discovery walks upward from the effective `--cwd` and the nearest ancestor config overrides the global file.

Lists replace rather than extend. For example, setting `[coordinator].allowed_agents` in a local config replaces the global list instead of appending to it.

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

Add a new `[agents.<name>]` section with a `template` containing `{prompt}`:

```toml
[agents.myagent]
template = "my-custom-cli --mode auto {prompt}"
```

The new type appears automatically in the coordinator's `spawn_agent` tool.

`{prompt}` is substituted after tokenization, not by shell evaluation. Quoted placeholder forms such as `PROMPT="{prompt}"` are supported.

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
| `/reset`   | Clear conversation history and context tracking; start fresh    |
| `/quit`    | Graceful shutdown: wait for running agents, then exit           |
| `/agents`  | Print current agent status table inline                         |
| `/log`     | Print the path to the current run log                           |

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
