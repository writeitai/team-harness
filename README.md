# team-harness
# team-harness

`team-harness` is a lightweight, model-agnostic multi-agent orchestration harness. It runs a coordinator LLM through an OpenAI-compatible API and lets that coordinator spawn external worker CLIs such as Codex, Gemini, Claude Code, opencode, `pi`, or even nested `team-harness` runs.

## Installation

Published package:

```bash
uv tool install team-harness
```

Development setup:

```bash
git clone <repo>
cd team-harness
uv sync --extra dev
uv run team-harness --help
```

## Prerequisites

Worker CLIs are installed and authenticated separately from the harness.

- `codex`
- `gemini`
- `claude`
- `opencode`
- `pi`

You do not need every worker installed. Restrict a run with `--agents codex` or another explicit allowlist.

## First Run

On first run, `team-harness` creates:

```text
~/.team-harness/config.toml
```

Edit that file to add your coordinator API key, or set `OPENROUTER_API_KEY` in the environment. The default coordinator target is OpenRouter at `https://openrouter.ai/api/v1` with model `openai/gpt-4o`.

## Authentication

- The coordinator uses your OpenRouter or other OpenAI-compatible API credentials.
- Each worker CLI uses its own native authentication and local config.
- The harness does not forward the coordinator API key into workers unless you explicitly provide environment overrides at spawn time.

## Trust Model

- Skills loaded from `~/.team-harness/skills/*.py` and `./skills/*.py` execute arbitrary Python at startup.
- The `bash` tool runs commands with the harness process's privileges.
- Worker CLIs are separate local processes and may read or modify files in the working directories you give them.
- The harness itself only sends the coordinator task and tool outputs to the configured coordinator model endpoint.

Treat tasks, skills, and worker prompts as trusted local automation.

## Usage

Single shot:

```bash
team-harness run "Write a Python script that scrapes Hacker News top 10"
team-harness run -f task.txt
```

Interactive REPL:

```bash
team-harness repl
```

Run logs:

```bash
team-harness logs
team-harness logs <run-id>
```

## REPL Commands

- `/reset`
- `/quit`
- `/agents`
- `/log`

## Notes

- Run logs are written under `~/.team-harness/runs/<run-id>/run.json`.
- Agent stdout/stderr logs and todo state live in the same run directory.
- The bundled example skill is `skills/summarise.py`.
