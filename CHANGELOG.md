# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.6] - 2026-05-06

### Added

- Coordinator-visible `spawn_agent` resume controls: `mode = "resume"` and
  `resume_from_session_id`, allowing workers to be spawned against captured
  provider sessions.

### Fixed

- Claude Code worker session IDs are now captured from final stream-json result
  events as well as startup events, so `worker_sessions.json` can record
  resumable Claude sessions.
- Session capture now scans both the start and end of worker logs and performs a
  final scan when the worker exits, avoiding startup-only capture misses.

## [0.2.5] - 2026-05-05

### Added

- Worker failure diagnostics in `worker_sessions.json` schema v2, including
  worker outcome, exit code, elapsed time, stdout/stderr tails, and per-worker
  diagnostic artifact paths.
- `TeamHarnessError.detail` with structured diagnostics for the latest failed
  worker, plus rendered stderr/stdout tails in the exception string.

## [0.2.4] - 2026-05-05

### Added

- Python SDK `TeamHarness(output_dir=...)` option for overriding the
  coordinator output directory per run.

## [0.2.3] - 2026-05-05

### Added

- Python SDK `TeamHarness` constructor options `agent_models` and
  `agent_reasoning_efforts` for overriding resolved worker template defaults by
  agent type.

## [0.2.2] - 2026-04-27

### Added

- `CLAUDE.md` with development commands, architecture overview, release procedure, and code conventions.
- `.claude/skills/team-harness/SKILL.md` Agent Skill so Claude Code (and other Agent-Skills-compatible tools) can answer install, setup, and usage questions about team-harness.

## [0.2.1] - 2026-04-27

### Added

- [Agent Skills](https://agentskills.io) standard support — `SKILL.md` files in `.agents/skills/` directories are discovered at startup and presented to the coordinator. Skills written for Codex CLI work in team-harness without changes.
- YAML frontmatter parsing (`pyyaml>=6.0` added as dependency) for skill name and description.
- BFS skill discovery from `.agents/skills/` (project-local, with parent directory walking) and `~/.agents/skills/` (user-global). Max depth 6.
- `_render_skills_section()` in the coordinator system prompt listing available skills.

### Removed

- **BREAKING**: Python-based skills system (`~/.team-harness/skills/`, `<cwd>/skills/`). Skills are no longer Python modules with `execute()` — they are markdown instruction files following the Agent Skills standard.
- `SkillContext`, `Skill` dataclass, `_make_skill_wrapper()`, and skills-as-tools registration in `ToolRegistry`.

## [0.2.0] - 2026-04-27

### Changed

- **BREAKING**: Renamed public SDK classes to match the package name — `Harness` → `TeamHarness`, `HarnessResult` → `TeamHarnessResult`, `HarnessError` → `TeamHarnessError`. The internal `HarnessConsole` UI class is now `TeamHarnessConsole`.
- **BREAKING**: Renamed environment variables from `HARNESS_*` to `TEAM_HARNESS_*`: `HARNESS_PROVIDER`, `HARNESS_MODEL`, `HARNESS_API_BASE`, `HARNESS_CODEX_AUTH_PATH`, `HARNESS_DEPTH`. No backwards-compatible aliases — existing deployments and scripts must be updated.
- Streamlined coordinator system prompt: removed redundant examples, added "Autonomy" section favoring autonomous operation, condensed API Error Failover Protocol, reordered safety rules.

### Added

- [Agent Skills](https://agentskills.io) standard support — `SKILL.md` files in `.agents/skills/` directories are discovered at startup and presented to the coordinator. Skills written for Codex CLI work in team-harness without changes.
- YAML frontmatter parsing (`pyyaml>=6.0` added as dependency) for skill name and description.
- BFS skill discovery from `.agents/skills/` (project-local, with parent directory walking) and `~/.agents/skills/` (user-global). Max depth 6.
- `_render_skills_section()` in the coordinator system prompt listing available skills.

### Removed

- **BREAKING**: Python-based skills system (`~/.team-harness/skills/`, `<cwd>/skills/`). Skills are no longer Python modules with `execute()` — they are markdown instruction files following the Agent Skills standard.
- `SkillContext`, `Skill` dataclass, `_make_skill_wrapper()`, and skills-as-tools registration in `ToolRegistry`.

### Fixed

- Shift+Enter now inserts a newline in the REPL instead of submitting.

## [0.1.6] - 2026-04-22

### Added

- Auto-failover to alternative harness on API errors: when a worker agent (e.g. Codex) fails due to an API error (rate limit, overloaded, auth failure, quota, server error, model unavailable), the coordinator automatically re-delegates the task to a different agent type using a different API provider.
- New `api_error_classifier` module that detects API error patterns in agent stderr/stdout and returns structured classifications.
- `wait_for_any` responses now include a `failure_classification` field for failed agents, surfacing the error category and suggested action to the coordinator.
- "API Error Failover Protocol" section in the coordinator system prompt with step-by-step failover instructions and an escalation rule (stop retrying after 2+ different agent types fail).

## [0.1.5] - 2026-04-22

### Added

- Inline markdown rendering in streamed output: `**bold**` text, `## headings`, and `> blockquotes` are now rendered with appropriate styling during streaming.
- `**bold**` markers are consumed and content rendered bold, with cross-token boundary handling.
- `## heading` prefixes consumed at line start, rest of line rendered bold.
- `> blockquote` prefixes consumed at line start, rendered dim italic with `│` left border.
- Bold and backtick highlighting in tool call output via `_style_paths()`.

### Changed

- Backtick code span style changed from bold cyan on dark background to plain blue (no background).
- Multiline tool call args (e.g. `prompt`) now render as readable indented blocks with actual newlines instead of escaped `\n` literals.

## [0.1.4] - 2026-04-21

### Fixed

- Spinner now visible during tool execution ("Working") in addition to the brief pre-streaming thinking phase.
- Live display refresh rate increased from 2Hz to 4Hz for smoother spinner animation.

### Added

- URL highlighting (cyan underline) in both tool call output and streamed coordinator text.
- 2-space left padding on streamed coordinator output for visual separation from terminal edge.
- Inline backtick code span rendering in streamed output — backtick-wrapped text rendered as bold cyan on dark background using a state machine that tracks open/close across token boundaries.

## [0.1.3] - 2026-04-20

### Added

- Animated braille spinner in the status bar during coordinator thinking phase.
- iTerm2 tab progress indicator via OSC 9;4 sequences (gated on iTerm2 detection, disabled inside tmux).
- User prompt styling with dark background and white text to distinguish user input from assistant output.
- Per-agent-type emojis in the agent panel and event log (codex, gemini, claude, openhands, opencode, harness, pi).
- File path coloring (cyan) in tool call arguments and results, with URL exclusion.
- Backtick code span highlighting (bold cyan on dark background) in tool call output.
- Bold consistency for agent types, turn numbers, and running status indicators.
- New `src/team_harness/ui/terminal.py` module for iTerm2 OSC escape sequence helpers.
- "Terminal features" section in README.

## [0.1.1] - 2026-04-17

### Fixed

- Added upgrade instructions to README Installation section.

## [0.1.0] - 2026-04-17

### Added

- Paste preview in the REPL: long pastes (4+ newlines) collapse to `[Pasted text #N +M lines]` in the prompt buffer, and the full text is restored on submit. History and run logs store the expanded text. Requires a terminal with bracketed-paste support; non-TTY and non-bracketed-paste terminals degrade gracefully.

## [0.0.1] - 2026-04-16

### Added

- Initial public release of `team-harness`.
- Coordinator-driven multi-agent orchestration that spawns external worker CLIs as tool-use actions.
- Coordinator backends: any OpenAI-compatible API (including OpenRouter) and an experimental Codex subscription backend.
- Built-in worker integrations: Codex, Gemini, Claude Code, opencode, pi, and OpenHands.
- REPL with slash commands: `/compact [focus]`, `/clear`, `/reset`, `/agents`, `/log`, `/quit`.
- Auto-compaction with model-specific thresholds and a local token-count fallback when provider usage is unavailable.
- Session tracking, per-run logs, and worker session capture.
- Project-local configuration via `th init` (config.toml, coordinator system message, worker suffix and footer templates).
- `th` CLI entry point (`team-harness` retained as compatibility alias).

[Unreleased]: https://github.com/writeitai/team-harness/compare/v0.2.6...HEAD
[0.2.6]: https://github.com/writeitai/team-harness/compare/v0.2.5...v0.2.6
[0.2.5]: https://github.com/writeitai/team-harness/compare/v0.2.4...v0.2.5
[0.2.4]: https://github.com/writeitai/team-harness/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/writeitai/team-harness/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/writeitai/team-harness/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/writeitai/team-harness/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/writeitai/team-harness/compare/v0.1.6...v0.2.0
[0.1.6]: https://github.com/writeitai/team-harness/compare/v0.1.5...v0.1.6
[0.1.5]: https://github.com/writeitai/team-harness/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/writeitai/team-harness/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/writeitai/team-harness/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/writeitai/team-harness/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/writeitai/team-harness/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/writeitai/team-harness/compare/v0.0.1...v0.1.0
[0.0.1]: https://github.com/writeitai/team-harness/releases/tag/v0.0.1
