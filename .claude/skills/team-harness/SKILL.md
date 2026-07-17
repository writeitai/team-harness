---
name: team-harness
description: Install, configure, and run team-harness — a CLI (`th`) and Python SDK that coordinates multiple AI coding agent CLIs (Codex, Gemini, Claude Code, OpenHands, OpenCode, pi) so they collaborate as a team on a single task. Use this skill whenever the user mentions team-harness, the `th` command, `th init` / `th run` / `th repl` / `th logs`, asks to install or set up team-harness, wants a multi-agent run across Codex / Gemini / Claude / OpenHands together, or asks how to point team-harness at OpenRouter or another OpenAI-compatible provider. Use even when the user does not say "team-harness" explicitly but describes wanting to orchestrate several AI coding CLIs together as a team, plug different model providers into one workflow, or fan a task out to multiple agent CLIs and aggregate the results.
---

# team-harness

team-harness is a coordination layer that runs an LLM coordinator on top of one or more worker CLIs (Codex, Gemini, Claude Code, OpenHands, OpenCode, pi). The coordinator decomposes a task, spawns workers via a `spawn_agent` tool, watches their stream-json output, and aggregates results. Users interact with it through the `th` CLI (REPL or headless) or the `team_harness` Python SDK.

Use this skill any time you help a user install, configure, or run team-harness. The sections below are ordered as a typical first-time path: install → install workers → init config → authenticate → run. Skip ahead when the user is already past a step.

## Quickstart (happy path)

If the user just wants the shortest possible "get me running":

```bash
# 1. Install
uv tool install team-harness            # or: pip install team-harness

# 2. Make sure at least one worker CLI is on PATH and authenticated
#    (codex, gemini, claude, openhands, opencode, or pi).

# 3. Create project-local config
cd <their project>
th init

# 4. Run
OPENROUTER_API_KEY="sk-or-..." th repl
#   or
OPENROUTER_API_KEY="sk-or-..." th run "Write unit tests for src/utils.py"
```

If they have only some workers installed, gate the run with `--agents`, e.g. `th run --agents codex,claude "..."`.

## 1. Installation

```bash
pip install team-harness
# or
uv tool install team-harness
```

Upgrade with the `--upgrade` flag (`pip install --upgrade team-harness` / `uv tool install --upgrade team-harness`).

The installed CLI command is `th`. The legacy `team-harness` command still works as an alias. `python -m team_harness` also works.

## 2. Worker CLIs (prerequisites)

Workers are separate local CLIs that team-harness shells out to. The user does **not** need all of them — they only need the ones they intend to use, and runs can be restricted with `--agents`.

| Worker      | Install                                                              |
|-------------|----------------------------------------------------------------------|
| `codex`     | https://github.com/openai/codex                                      |
| `gemini`    | https://github.com/google-gemini/gemini-cli                          |
| `claude`    | Claude Code (https://docs.anthropic.com/en/docs/claude-code)         |
| `openhands` | `pip install openhands` (the OpenHands-CLI repo publishes this name) |
| `opencode`  | https://github.com/opencode-ai/opencode                              |
| `pi`        | https://github.com/badlogic/pi-mono                                  |

Each worker uses its own native auth (e.g. `claude` uses Claude's own login, `codex` uses `codex login`'s `~/.codex/auth.json`). team-harness does not forward the coordinator API key to workers unless you explicitly configure `provider_env` (see "Routing workers through OpenRouter" below).

If a user reports "the coordinator can't spawn X", the first thing to check is whether `X` is on `PATH` and authenticated when run by hand outside team-harness.

## 3. Project setup with `th init`

`th init` is the standard way to scaffold config. Run it from the project root:

```bash
th init                 # writes ./.team-harness/
th init --global        # writes ~/.team-harness/
th init --force         # overwrite config.toml (sidecar prompt files preserved)
```

`th init` creates four files inside the target `.team-harness/`:

| File                              | Purpose                                                                |
|-----------------------------------|------------------------------------------------------------------------|
| `config.toml`                     | Coordinator + per-agent settings (provider, model, commands, flags)    |
| `coordinator_system_message.md`   | Editable coordinator base prompt                                       |
| `worker_suffix.md`                | Text appended to every spawned worker prompt (empty by default)        |
| `worker_footer.md`                | Worker output-requirements footer (default keeps `{session_output_dir}` placeholder) |

`th init --force` regenerates `config.toml` but **preserves** the three sidecar prompt files so user customizations survive a re-init. Missing sidecars are recreated.

Project-level `.team-harness/` should normally be committed to git so prompt behaviour is reproducible across contributors and CI. **Do not put API keys in `config.toml`** — keep them in environment variables.

### Config resolution order

1. CLI flags
2. Environment variables
3. Local `.team-harness/config.toml` (discovered by walking upward from `--cwd`)
4. Global `~/.team-harness/config.toml`
5. Built-in defaults

Lists replace rather than extend — e.g. setting `[coordinator].allowed_agents` in a local config overrides the global list, it does not append.

## 4. Authenticating the coordinator

The coordinator (the LLM that decides what to do and when to spawn workers) needs an API of its own. There are three common shapes:

### a) OpenRouter / any OpenAI-compatible API (most common)

```bash
export OPENROUTER_API_KEY="sk-or-..."
th repl
```

Or pin a specific provider explicitly:

```bash
OPENAI_API_KEY="sk-..." TEAM_HARNESS_API_BASE="https://api.openai.com/v1" th repl
```

In `config.toml`:

```toml
[coordinator]
provider = "openai_compat"
model = "anthropic/claude-sonnet-4.6"
api_base = "https://openrouter.ai/api/v1"
```

### b) Codex subscription (experimental)

Uses the auth file written by `codex login`:

```bash
TEAM_HARNESS_PROVIDER=codex th repl
```

Or in `config.toml`:

```toml
[coordinator]
provider = "codex"
model = "codex-mini-latest"
# codex_auth_path = "~/.codex/auth.json"   # only if non-default
```

Codex auth resolution: `codex_auth_path` → `TEAM_HARNESS_CODEX_AUTH_PATH` → `$CODEX_HOME/auth.json` → `~/.codex/auth.json`.

### Relevant env vars

- `TEAM_HARNESS_PROVIDER` — `openai_compat` or `codex`
- `TEAM_HARNESS_MODEL` — coordinator model id
- `TEAM_HARNESS_API_BASE` — base URL for `openai_compat`
- `TEAM_HARNESS_CODEX_AUTH_PATH`
- `OPENROUTER_API_KEY` / `OPENAI_API_KEY`

## 5. Running team-harness

### REPL (interactive)

```bash
th repl
```

Useful REPL commands:

| Command           | What it does                                                         |
|-------------------|----------------------------------------------------------------------|
| `/clear` / `/reset` | Clear conversation history; keep session, run log, agents alive    |
| `/compact [focus]`  | Manually compact earlier conversation; optional focus biases summary |
| `/agents`           | Print agent status table inline                                    |
| `/log`              | Print path to the current run log                                  |
| `/quit`             | Wait for running agents, then exit                                 |

REPL editing: `Enter` submits, `Shift+Enter` / `Alt+Enter` insert newline, `Esc Esc` clears the buffer, `Ctrl+C` clears input without exiting, `Ctrl+D` exits when the buffer is empty, `Up`/`Down` navigates input history. If `Alt`/`Esc` feels laggy in tmux, `set -sg escape-time 0`.

### Headless

```bash
th run "Write unit tests for src/utils.py using pytest"
th run -f task.txt        # read prompt from a file
```

Common flags (see full list in the README):

```
--provider TEXT         openai_compat | codex
--model TEXT            coordinator model
--api-base TEXT
--api-key TEXT          openai_compat key
--agents TEXT           comma-separated allowlist, e.g. "codex,gemini"
--max-retries INT       429/5xx retry budget (default 5)
--max-depth INT         nested harness depth limit (default 3)
--system-prompt TEXT    extra text appended to coordinator system prompt
--system-prompt-file PATH
--cwd PATH              working directory for the run (default ".")
```

### Logs

```bash
th logs                 # latest run
th logs <run-id>        # specific run
```

Each run lives under `~/.team-harness/runs/<run-id>/`:

- `run.json` — full delta-based, losslessly replayable run log
- `<agent-id>_stdout.log` / `<agent-id>_stderr.log` — per-agent output
- `todo.json` — persistent task list

Each run also writes `<output_dir>/<run-id>/worker_sessions.json`, a compact per-worker manifest (prompt, status, timestamps, log paths, resume metadata).

### Python SDK

```python
import asyncio
from team_harness import TeamHarness, TeamHarnessResult

async def main():
    harness = TeamHarness(
        api_key="sk-or-...",
        model="anthropic/claude-sonnet-4.6",
        agents=["codex", "gemini"],
    )
    result: TeamHarnessResult = await harness.run(
        "Write unit tests for src/utils.py using pytest"
    )
    print(result.text)
    for agent in result.agents:
        print(f"  {agent.id} ({agent.agent_type}): {agent.status}")

asyncio.run(main())
```

`TeamHarness(...)` accepts the same options as the CLI: `provider`, `model`, `api_base`, `api_key`, `codex_auth_path`, `agents`, `max_retries`, `max_depth`, `system_prompt`, `system_prompt_file`, `cwd`, and `console_mode` (`"silent" | "auto" | "plain" | "rich"` — use `"silent"` from the SDK).

`result` is a `TeamHarnessResult` with `text` (final assistant response), `agents` (list of `AgentSummary` with `id`, `agent_type`, `status`, `exit_code`, `cwd`), and `run_id`. Errors raise `TeamHarnessError`. Run logs are finalized even on failure.

## 6. Common configuration recipes

### Pin a worker's default model

`default_model` is the model used when the coordinator spawns this worker without an explicit `model=...` override. `model_flag` is the argv flag the harness uses to inject it.

```toml
[agents.codex]
command = ["codex", "exec"]
default_model = "gpt-5.4"
```

Clear an inherited default with `default_model = false`.

### Pin reasoning effort (codex / claude)

```toml
[agents.codex]
reasoning_effort = "high"        # codex: low | medium | high | xhigh
```

```toml
[agents.claude]
reasoning_effort = "high"        # claude: low | medium | high | max
```

`reasoning_effort_flag` ships with sensible defaults per agent (`["-c", "model_reasoning_effort={effort}"]` for codex, `["--effort", "{effort}"]` for claude). Gemini doesn't support this upstream.

### Routing workers through OpenRouter

The coordinator and the worker auth are independent. To route worker CLIs through the same OpenRouter account:

```bash
export OPENROUTER_API_KEY=sk-or-...
```

**Codex** reads provider config from `-c` overrides:

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
default_model = "openai/gpt-5.3-codex"
```

**Claude Code** reads provider config from env vars — set them via `provider_env`:

```toml
[agents.claude]
default_model = "anthropic/claude-opus-4.6"

[agents.claude.provider_env]
ANTHROPIC_BASE_URL = "https://openrouter.ai/api"
ANTHROPIC_AUTH_TOKEN = "{env:OPENROUTER_API_KEY}"
ANTHROPIC_API_KEY = ""    # must be empty so Claude Code doesn't fall back to native auth
```

`{env:OPENROUTER_API_KEY}` resolves from `os.environ` at spawn time. Missing vars are substituted with an empty string and a one-time warning.

**Gemini via OpenRouter is not supported upstream** — the `gemini` CLI authenticates directly against Google APIs and has no OpenAI-compatible base-URL mode.

### Adding a custom worker CLI

Add a `[agents.<name>]` section. `command` is the only required field:

```toml
[agents.myagent]
command = ["my-custom-cli"]
shared_flags = ["--mode", "auto"]
model_flag = "--model"        # set to false if there's no model flag
prompt_flag = "-p"            # only if the prompt is introduced by a flag
prompt_position = "after_command"   # only if the prompt belongs near the front of argv
```

For env-based model injection (like OpenHands) use `model_env_vars = ["LLM_MODEL"]` and set `model_flag = false`.

Placeholders inside `shared_flags` / `resume_prefix` / `resume_flags`:

- `{session_id}` — the resume session id (resume mode only)
- `{generated_uuid}` — a harness-generated UUID at spawn time (used by the `claude` agent's `--session-id <uuid>` form)

Session ids can be captured from a worker's stream-json output via `[agents.<name>.session_capture]`:

```toml
[agents.codex.session_capture]
strategy = "stream_json_event"
match = { type = "thread.started" }
field_path = ["thread_id"]
```

The new agent shows up automatically in the coordinator's `spawn_agent` tool.

## 7. Coordinator tools (so you can predict what it can do)

The coordinator has these tools available — useful when explaining what it will and won't try:

- **Agent management:** `spawn_agent`, `kill_agent`, `agent_status`, `list_agents`, `wait_for_agents`, `wait_for_any`, `read_new_agent_output`
- **File system:** `read_file`, `write_file`, `append_file`, `edit_file`, `multi_edit_file`, `ls`, `glob`, `grep`, `read_new_file_content`
- **Shell:** `bash`
- **Task tracking:** `todo_write`, `todo_read`

`bash` defaults to a 120-second whole-command deadline. For a known
long-running foreground batch, pass a positive named `timeout_seconds` sized
for the complete batch; timeout or cancellation cleans up the command's whole
process group. This outer deadline is separate from timeouts inside the invoked
program.

It also discovers Agent Skills under `<cwd>/.agents/skills/` and `~/.agents/skills/` (project skills override global ones). Skill metadata is shown to the coordinator at startup and full instructions are fetched via `read_file` on demand.

## 8. Common gotchas

- **OpenHands runs are not auto-resumable** today — its `--json` output is not stream-json parseable. Custom `[agents.openhands]` sections inherit the new built-in `shared_flags` after upgrade; if your config used `openhands` as a coincidentally-named custom agent, rename it or explicitly clear inherited fields (`shared_flags = []`, `prompt_flag = false`, `model_env_vars = []`).
- **OpenHands `LLM_MODEL` leakage**: `--override-with-envs` is required for `LLM_MODEL` injection, but it also picks up `LLM_MODEL` / `LLM_API_KEY` / `LLM_BASE_URL` from the parent shell. Unset them for deterministic per-run behaviour.
- **Claude Code model overrides**: setting only `ANTHROPIC_MODEL` is not enough — internal code paths read `ANTHROPIC_DEFAULT_SONNET_MODEL` / `ANTHROPIC_DEFAULT_OPUS_MODEL` directly. The built-in `claude` template's `model_env_vars` already lists all three; preserve that if you customize. The harness intentionally does not touch `ANTHROPIC_DEFAULT_HAIKU_MODEL`, `ANTHROPIC_SMALL_FAST_MODEL`, or `CLAUDE_CODE_SUBAGENT_MODEL` so cheap helpers keep running on haiku.
- **Legacy `template = "..."` string form was removed.** If a config still has it, the harness raises a clear error. Fix: `th init --force` to regenerate `config.toml` (sidecar prompt files are preserved).
- **Prompt files** are read as UTF-8 and capped at 100 KB. Larger / non-UTF-8 / unreadable files produce a clear error.
- **Trust model**: skills execute arbitrary Python with the harness process's full privileges; the `bash` tool runs unsandboxed with `stdin=/dev/null`. Treat the skills directory like `PATH` — only run trusted tasks and skills.

## 9. Where to learn more

The repo's `README.md` is the source of truth and is more exhaustive than this skill (full agent template schema, Codex subscription details, model-precedence tables, terminal-feature list, full migration notes from the `template = "..."` form). When the user asks for something this skill doesn't cover, point them at the README rather than guessing.

Repo: https://github.com/writeitai/team-harness

## How to use this skill

When the user asks anything about installing, configuring, or running team-harness:

1. **Figure out where they are in the install→init→auth→run flow** — do they already have it installed? Have they run `th init`? Do they have an API key set?
2. **Give them the next concrete step**, not a wall of text. The Quickstart is a good fallback when they have nothing yet.
3. **Use exact commands and exact env var names.** team-harness has many small naming conventions (`TEAM_HARNESS_*`, `OPENROUTER_API_KEY`, `model_env_vars`, `provider_env`, `{env:VAR}` placeholders) and they all matter.
4. **When they ask about advanced config** (custom agents, OpenRouter routing, reasoning effort, session capture), use the recipes above. For anything beyond them, defer to the README rather than improvising.
5. **Sanity-check before recommending a worker** — if they say "have it spawn `gemini`", confirm the gemini CLI is installed and authenticated, and remember gemini-via-OpenRouter is not supported.
