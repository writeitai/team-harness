# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

```bash
uv sync --extra dev          # Install all deps including dev
uv run ruff check src/       # Lint
uv run ruff format src/      # Format
uv run pyright src/           # Type check
uv run pytest src/tests/ -v  # Run all tests
uv run pytest src/tests/test_console.py -v                    # Run one test file
uv run pytest src/tests/test_skills.py::test_frontmatter_parsing -v  # Run one test
```

CI runs ruff lint, ruff format check, pyright, and pytest on Python 3.12/3.13/3.14. All four must pass before merge.

## Architecture

team-harness is a multi-agent orchestration harness. A **coordinator LLM** (talking to an OpenAI-compatible API or Codex subscription) receives user tasks, breaks them into work units, and delegates execution to **worker CLIs** (Codex, Gemini, Claude Code, opencode, pi, OpenHands) spawned as subprocesses.

### Request flow

```
User input → cli.py (_repl / _run)
  → harness.py (TeamHarness.run)
    → coordinator/loop.py (run → run_one_turn loop)
      → coordinator/client.py (chat with LLM)
      → LLM returns tool calls → tools/registry.py (execute)
        → tools/agent_tools.py (spawn_agent, wait_for_agents, ...)
          → agents/spawner.py (subprocess.exec worker CLI)
          → agents/manager.py (track AgentState lifecycle)
        → tools/fs_tools.py (read_file, write_file, grep, ...)
        → tools/shell_tools.py (bash)
        → tools/todo_tools.py (todo_write, todo_read)
      → Loop continues until LLM returns content without tool calls
```

### Key architectural concepts

**Coordinator vs Workers**: The coordinator is an LLM that plans and delegates. Workers are external CLI processes (codex, gemini, claude, etc.) that do the actual work. The coordinator never implements — it orchestrates.

**Tool Registry**: `tools/registry.py` maps tool name → (schema, async fn). The coordinator loop sends all schemas to the LLM, then dispatches tool calls by name. Tool bindings are created per-run via `build_*_tool_bindings()` factory functions to capture run-specific closures (manager, run_log, config).

**Agent Templates**: `agents/template.py` defines how each worker CLI is invoked — command, flags, model injection, session capture strategy. Templates are structured (command list + flag lists), NOT string templates. Config merges built-in defaults with user overrides from `config.toml`.

**Console Hierarchy**: `ui/console.py` has `ConsoleBase` (ABC) → `SilentConsole` (SDK), `PlainConsole` (non-TTY), `TeamHarnessConsole` (Rich TUI with Live panels, spinners, markdown rendering). The console drives the entire visual lifecycle: `begin_turn → begin_streaming → stream_token → end_streaming → tool_call_start → end_turn`.

**Context Tracking**: `tracking/context.py` tracks token usage from API responses and triggers auto-compaction when the model-specific threshold is reached. Compaction rewrites conversation history into a summary via a separate LLM call.

**Agent Skills**: `skills/loader.py` discovers `SKILL.md` files from `.agents/skills/` directories (project-local with parent-dir walking, plus `~/.agents/skills/` global). Parses YAML frontmatter for name+description. Skills are instructions the coordinator reads via `read_file`, NOT executable code.

### Two entry points

- **CLI** (`cli.py`): `th run` (single-shot) and `th repl` (interactive loop with slash commands). The REPL handles `/clear`, `/compact`, `/agents`, `/log`, `/quit`.
- **SDK** (`harness.py`): `TeamHarness(...)`.run(task)` returns `TeamHarnessResult`. Uses `SilentConsole` by default.

### Configuration resolution

CLI flags → env vars (`TEAM_HARNESS_*`) → local `.team-harness/config.toml` → global `~/.team-harness/config.toml` → built-in defaults.

## Releasing

**CRITICAL: Before any release work, always check the current state first:**

```bash
git fetch --tags
gh release list --limit 5          # What's the latest published release?
git tag --sort=-v:refname | head -5 # What tags exist?
grep '^version' pyproject.toml      # What version is in the code?
```

The version in `pyproject.toml` MUST be higher than the latest git tag. If a tag already exists for a version, you cannot reuse it — you must bump to a new version.

**Release pipeline** (`.github/workflows/release.yml`):

Pushing a tag matching `v*.*.*` triggers: build → publish to PyPI → create GitHub Release. The pipeline is fully automated — do NOT manually create GitHub releases with `gh release create`, because the CI workflow does this itself after publishing to PyPI. Creating the release manually causes the CI's `github-release` step to fail with "already exists".

**Correct release procedure:**

1. Check the latest tag and release (commands above)
2. Bump version in `pyproject.toml` to a version higher than the latest tag
3. Update `CHANGELOG.md` with a new section for the version
4. Commit: `git commit -m "chore: bump version to X.Y.Z"`
5. Push to main: `git push`
6. Create and push the tag: `git tag vX.Y.Z && git push origin vX.Y.Z`
7. CI handles the rest (build, PyPI publish, GitHub Release creation)

**Do NOT** run `gh release create` — let CI do it. The tag push is the only trigger needed.

## Conventions

- **Imports**: ruff enforces single-line, force-sorted imports (`force-single-line = true`). One import per line.
- **Async**: pytest uses `asyncio_mode = "auto"` — all async test functions run automatically without `@pytest.mark.asyncio`.
- **Console methods**: New optional methods on `ConsoleBase` must use default no-op implementations (not `@abstractmethod`) to avoid breaking external subclasses. Follow the `begin_compaction`/`end_compaction` pattern.
- **Test doubles**: `conftest.py` provides `DummyUI` (duck-typed console stand-in). `test_cli.py` has local `FakeConsole` classes per test. Both must be updated when adding new console methods.
- **Agent tool bindings**: Use `build_*_tool_bindings()` factory pattern that returns `list[tuple[schema, fn]]`. Never use module-level state for new tool modules.
