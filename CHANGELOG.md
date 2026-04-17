# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/writeitai/team-harness/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/writeitai/team-harness/compare/v0.0.1...v0.1.0
[0.0.1]: https://github.com/writeitai/team-harness/releases/tag/v0.0.1
