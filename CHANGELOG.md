# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- Retained worker watcher or provider-session final-scan task failures no
  longer escape before run finalization. Worker shutdown now uses the configured
  natural-exit timeout plus a named one-second SIGTERM grace; retained
  watcher/capture work separately uses the configured timeout. The outer bound
  includes the grace, so responsive workers do not lose it to an outer timeout.
  Overdue work triggers SIGKILL for any unreaped trusted worker group before
  harness-owned lifecycle tasks are cancelled and settled. A failed
  process-table probe can therefore no longer leave `proc.wait()` pending into
  `asyncio.run()` teardown. The harness records phase-specific timeouts and
  exact lifecycle exception messages, writes `run.json` and
  `worker_sessions.json`, and then exposes the failure through the normal
  structured `TeamHarnessError` with canonical caller-owned artifact paths
  (TH-D7).

## [0.5.0] - 2026-07-16

### Added

- **Explicit embedded-caller contract (TH-D7).** The public `CallerContext`,
  `get_capabilities()`, and `TEAM_HARNESS_CAPABILITIES` APIs let consumers
  capability-check `caller_run_record_v1`, `coordinator_input_v1`,
  `spawn_assignment_v1`, and `nested_caller_context_v1` without guessing from
  the package version. Context-aware SDK runs keep canonical `run.json`,
  generated coordinator input, worker artifacts, and direct-agent assignments
  beneath a caller-owned absolute trace root.
- `TeamHarnessResult` now returns `run_json_path`, `session_output_dir`, and
  `coordinator_input_path`; structured `TeamHarnessError.detail` exposes the
  same paths. Existing three-argument result construction remains valid.
- Generated coordinator system/user input is written atomically before client
  construction or model discovery. The automatic system footer supplies the
  outer attempt/session identity, workflow role, absolute assignment, run, and
  relevant-state paths. Context-aware config/prompt preflight failures still
  create a structured run and mark the input artifact `incomplete`; legacy
  callers keep their existing exception behavior.
- Every direct spawn gets `agents/<agent-id>/agent_assignment.json` and an
  effective prompt footer. `spawn_agent` accepts dynamic `delegated_role`,
  `delegated_task_id`, `expected_outputs`, and `state_responsibility` metadata;
  none are enums or execution gates.
- Worker stdout/stderr is captured directly under the canonical run directory.
  Provider session-id capture tasks are retained and awaited through their
  final stdout prefix/tail scan before final `run.json` and
  `worker_sessions.json` snapshots.
- Coordinator and direct-worker footers name the harness run id explicitly.
  Built-in `type=harness` descendants also receive a validated
  `TEAM_HARNESS_CALLER_CONTEXT` envelope: the same outer attempt/session/layer
  identity, their own assignment and nested trace root, and the parent harness
  run id. This records lineage without restricting dynamic delegation.

  Consumer impact: additive for callers that do not pass `caller_context`.
  Context-aware consumers should negotiate all required capability names and
  consume returned paths instead of reconstructing private locations.

## [0.4.0] - 2026-07-14

### Added

- **Per-spawn reasoning-effort override (TH-D6).** `spawn_agent` accepts an
  optional `effort` argument, mirroring the per-spawn `model` override: an
  explicit effort wins over the template's `reasoning_effort` default and
  renders through the template's `reasoning_effort_flag`. The override fails
  loudly instead of lying — a template that cannot carry the value (no
  `{effort}` placeholder), a blank level, or a raw `flags` entry carrying the
  same reasoning-effort option each return a coordinator-visible ERROR.
- **Model/effort audit trail in `run.json`.** Each agent record now carries
  `requested_model` / `requested_effort` (what the coordinator passed; `null`
  = left to template default) and `effective_model` / `effective_effort`
  (what was actually injected after resolution), so an outer reviewer — e.g.
  loopy-loop's model-tier policy — can verify which tier a task ran on
  without parsing argv. `effective_model` claims only what reached the
  worker: `null` when the template has no model-injection surface, and for
  env-only templates it reflects caller `env` overrides (`null` when
  partial/conflicting).

  Consumer impact: additive only. The `spawn_agent` schema gains one optional
  property; the four new `run.json` agent fields default to `null`, and older
  `run.json` files load unchanged. One behavior change: a template whose
  `reasoning_effort_flag` lacks the `{effort}` placeholder previously
  rendered a valueless option when `reasoning_effort` was set; it now renders
  nothing (such a config was broken either way — the level never reached the
  worker).

## [0.3.1] - 2026-07-14

### Added

- The `antigravity` agent template now injects a model via `--model` (the agy
  CLI accepts its models list's display names verbatim, e.g.
  `--model "Gemini 3.5 Flash (High)"`; run `agy models` for the list).
  Previously `model_flag` was unset, so SDK `agent_models` pins and
  `spawn_agent(model=...)` overrides for antigravity were silently ignored.
  No default pin is set: without an explicit model the account default
  applies.
- Public documentation site (team-harness.writeit.ai).

## [0.3.0] - 2026-07-13

### Added

- Built-in `antigravity` worker support for Google Antigravity CLI (`agy`)
  print-mode subprocesses.
- **Durable worker process identity and orphan reaping (TH-D5).** Workers are
  now spawned as leaders of their own process group (`start_new_session=True`),
  and their `pid`/`pgid`/`starttime` are persisted at spawn time in `run.json`
  (and surfaced in `worker_sessions.json`). New `th reap RUN_REF` command and
  `team_harness.tracking.reaper.reap_run()` API apply a policy to workers a
  crashed run left behind: `drain` (default — wait, under ONE shared deadline,
  for them to finish, then finalize their records incl. best-effort vendor
  session-id capture), `reap` (SIGTERM→grace→SIGKILL the group, verified), or
  `ignore` — with per-agent policy overrides (`policies={agent_id: ...}`) and a
  `--dry-run` probe mode. Design: `design/designs/process-lifecycle-and-reaping.md`.

  Safety hardening (driven by two independent adversarial reviews):
  - Start-time identity uses the exact kernel token on Linux (boot id +
    `/proc/<pid>/stat` start ticks — immune to wall-clock/NTP shifts and
    same-second pid reuse); the second-resolution `ps lstart` string remains
    the macOS fallback with a documented residual window.
  - A group whose leader is gone is **unverifiable** — waiting on it is
    allowed, killing it is refused (a recycled session could look identical).
  - Identity is re-verified immediately before every TERM/KILL escalation, and
    outcomes are honest: `killed`/terminal statuses only after the group is
    *observed* gone; failures surface as `kill_failed_still_running`.
  - A broken/missing `ps` raises and is reported as `probe_failed` — never
    silently treated as "the worker exited".
  - `reap_run` refuses to act while the run's original parent process is still
    alive (identity recorded at run start; `--force` overrides), validates
    policy/timeout arguments **before** touching anything, and serializes
    concurrent reapers on an advisory lock with unique atomic temp files.
  - Reap reports are kept as history (`reap_report_<ts>.json`); a no-op run
    never clobbers an earlier report with real outcomes.
- Recognition of the GPT-5.6 model family (`gpt-5.6-sol`, `gpt-5.6-terra`,
  `gpt-5.6-luna`, and their `openai/`-prefixed forms) in the context-tracking
  registries: 1.5M-token context window, 128K max output, and a Codex
  subscription cap mirroring gpt-5.5's 400K until OpenAI publishes 5.6's.

### Changed

- Default coordinator and codex-worker model bumped from `gpt-5.5` to
  `gpt-5.6-sol` (OpenAI's new frontier tier). `gpt-5.5` remains fully
  supported — override `model` / `default_model` in config to pin it, or
  select `gpt-5.6-terra` / `gpt-5.6-luna` for the cheaper tiers.
- `worker_sessions.json` `schema_version` bumped 2 → 3: worker records gain
  optional `pid`, `pgid`, and `starttime` fields (null for runs recorded by
  older versions).
- `run.json` gains a top-level `session_output_dir` field (recorded at run
  start) and worker entries gain `pid`/`pgid`/`starttime`; `run.json` is now
  written atomically (temp file + rename) so a crash can never truncate it.
- Graceful shutdown and `kill_agent` are now group-aware end to end: stragglers
  get a group SIGTERM with a verified SIGKILL escalation, and a final sweep
  kills surviving group members even when the leader already exited (e.g. a
  worker that exited successfully but left a background child running — both
  cases were reproduced by review before the fix). Note: because workers now
  run in their own process group, they no longer receive terminal Ctrl+C
  directly; team-harness's own shutdown/cleanup paths handle their termination,
  and a hard-killed parent's leftovers are covered by `th reap`.

## [0.2.10] - 2026-05-26

### Changed

- `spawn_agent(worker_label=...)` now replaces `spawn_agent(output_path=...)`.
  Worker labels are filesystem-safe names, not paths, and worker stdout/stderr
  are always written under the run session output directory.

## [0.2.8] - 2026-05-14

### Fixed

- `spawn_agent(output_path=...)` is now treated as a semantic log stem instead
  of an exact stdout file path. Worker stdout/stderr are written to
  `<stem>.stdout.jsonl` and `<stem>.stderr.log`, leaving `<stem>/` available for
  worker-authored artifact directories such as review outputs.

## [0.2.7] - 2026-05-12

### Added

- Coordinator-visible `spawn_agent` schema now describes built-in template
  default CLI flags so coordinators know `flags` is only for additional
  non-default arguments.
- Coordinator failure diagnostics now include `salvaged_workers` metadata for
  completed workers with usable stdout artifacts.

### Changed

- Default GPT model references were updated from `gpt-5.4` to `gpt-5.5`.

### Fixed

- `read_new_agent_output` now reads a bounded stdout window and shares cursor
  state with `wait_for_any`, preventing large backlog replay into coordinator
  context.
- Duplicate standalone worker spawn flags are deduplicated against configured
  template defaults for Codex and Claude agents.
- Workers with `exit_code=0` but cleanup status `killed` are summarized as
  completed successfully instead of worker crashes.

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

[Unreleased]: https://github.com/writeitai/team-harness/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/writeitai/team-harness/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/writeitai/team-harness/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/writeitai/team-harness/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/writeitai/team-harness/compare/v0.2.8...v0.3.0
[0.2.8]: https://github.com/writeitai/team-harness/compare/v0.2.7...v0.2.8
[0.2.7]: https://github.com/writeitai/team-harness/compare/v0.2.6...v0.2.7
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
