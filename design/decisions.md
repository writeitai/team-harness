# Architecture Decision Log

Decisions made while building and reviewing team-harness, recorded with the context and
rationale a future reader — a human, or an agent with **no memory of the conversation that
produced them** — needs to understand each one cold.

Companion docs:
- `CLAUDE.md` — developer reference (commands, architecture map, release process).
- `design/designs/` — self-contained, binding design docs (the detailed form of decisions).
- `design/analysis/` — working notes (may be messy or superseded).
- `README.md` — user-facing framing.

Each entry states the **Decision** (the conclusion, plainly), the **Context** (what problem
it solves or why the question arose), and the **Consequences**. A `**Refined by**` line
records later decisions that modify an earlier one.

> **Some of these are deliberate choices that read like defects to someone skimming the
> code.** They are recorded here precisely so a future agent does not "fix" them by accident.
> team-harness is a library other projects depend on — before changing a behavior below, read
> the entry and consider the consumer impact.

---

## TH-D1. The coordinator orchestrates; workers implement

**Decision.** The **coordinator** is an LLM that plans and delegates by emitting tool calls;
it never implements the work itself. The actual work is done by **workers** — external coding
CLIs (Codex, Gemini, Claude Code, opencode, pi, OpenHands) spawned as subprocesses. The
coordinator's tools spawn workers, read/write files, run shell, and manage a todo list, but
"do the change" always means "delegate to a worker."

**Context.** Mixing planning and implementation in one agent makes long tasks unfocused and
hard to supervise. Separating a thin orchestrating brain from swappable execution engines lets
team-harness be model- and CLI-agnostic, and keeps responsibility legible.

**Consequences.** The coordinator loop (`coordinator/loop.py`) runs until the LLM returns
content with no tool calls. Workers are the unit of real work and the unit of failure. Adding
a new backend means adding an agent template (`agents/template.py`), not touching the loop.

## TH-D2. Workers are one-shot batch subprocesses, not reattachable sessions

**Decision.** Each worker is launched with `asyncio.create_subprocess_exec` as a **one-shot
batch process**: `stdin=asyncio.subprocess.DEVNULL`, `stdout`/`stderr` redirected to **files**,
no controlling TTY (`agents/spawner.py`). It runs to completion and exits. There is no
interactive channel and nothing to "reattach" to.

**Context.** The worker CLIs are invoked in their non-interactive/exec mode: give them a
prompt, let them run, collect the result. This is simpler and more reproducible than driving an
interactive REPL, and it is what the CLIs are designed for in automation.

**Consequences.** Process control lives entirely in the parent's in-memory
`asyncio.subprocess.Process` handle (held by `agents/manager.py`'s `AgentManager`). **If the
parent dies, you cannot re-adopt a running worker** — there is no stdio to reconnect and no way
to rebuild the asyncio transport; the worker's output is in its log files, but there is no
control channel. Continuing a worker's *work* after an interruption is therefore a *new*
process that resumes the logical session (TH-D4), never a reattachment. This is the premise
behind TH-D5.

## TH-D3. A normal `run()` return means "the loop ended cleanly," not "every worker succeeded"

**Decision.** `TeamHarness.run()` returns a `TeamHarnessResult` when the coordinator loop ends
without a *terminal* error; it raises `TeamHarnessError` only on terminal failures (API/retry
exhaustion, or a recorded `run_log.error`) — see `harness.py`. A **failed worker does not by
itself fail the run**: failed workers survive as entries in `TeamHarnessResult.agents`, and the
coordinator may legitimately finish after a worker failed (synthesize an answer, route around
it, decide it has enough).

**Context.** The coordinator is an orchestrator, not a build system. Whether the *task* was
truly accomplished is a judgment the coordinator (and the caller) make from the workers'
outputs — it is not mechanically decidable from "the loop returned."

**Consequences.** **Consumers must not equate a normal return with task success.** They should
inspect `TeamHarnessResult.agents` (statuses, exit codes) and apply their own acceptance
criteria. `loopy-loop` depends on exactly this contract (its own decision log records that an
iteration's mechanical completion is distinct from the work being good). Changing this — e.g.
making a failed worker raise — is a breaking change to the consumer contract (AGENTS.md Rule 3).

## TH-D4. Worker continuity is via captured session id + resume, not process adoption

**Decision.** team-harness captures each worker's vendor **session id** from its output
(`agents/session_capture.py`) and records resume capability per agent type
(`tracking/worker_sessions.py`, `WorkerResumeInfo`). To continue a worker's work, you spawn a
**new** process that resumes that logical session (the CLIs' `--resume`/`continue` modes) —
never by reattaching to a still-running process (which TH-D2 makes impossible).

**Context.** Long tasks and crashes need a way to pick up where a worker left off. The vendor
CLIs already persist their own session state; capturing the id lets team-harness re-enter that
state cleanly.

**Consequences.** Process *identity* (is it still running?) and session *continuity* (resume
the work) are two different concerns handled by two different mechanisms — resume by session id
(this decision), and process reaping by pid/pgid (TH-D5). Not every backend supports resume
(`WorkerResumeInfo.supported`); callers must handle the unsupported case.

## TH-D5. Persist worker process identity and spawn workers in their own process group, to enable orphan reaping

**Decision.** team-harness will **persist each worker's process identity** — `pid`, process
group id (`pgid`), and process `starttime` — into the per-run worker-session manifest
(extending `WorkerSessionRecord` / `WorkerSessionsManifest`, which already persists rich
per-agent metadata but no process identity today), and will **spawn each worker with its own
process group** (`start_new_session=True`). It will expose a **reap** operation that, given a
prior run's manifest, kills any still-alive worker process group — verifying identity by
`(pgid, starttime)` so a recycled id is never killed.

**Context.** The `AgentManager` handle that lets team-harness kill a worker is **in memory
only**; nothing durable records which OS processes a run launched. Because workers are one-shot
subprocesses in the parent's process group (TH-D2), a **hard crash of the parent** (OOM,
SIGKILL, panic) reparents the workers to init and leaves them running — still spending money,
still writing to the target checkout — with nothing tracking them and no startup cleanup
anywhere. Re-adopting them is impossible (TH-D2), so the only durable fix is *prevent and
reap*: know what was launched, and be able to kill leftovers on restart.

**Consequences.**
- The existing worker-session manifest gains `pid`/`pgid`/`starttime`; spawning gains
  `start_new_session=True` so a whole worker (and any nested sub-workers, up to the configured
  `max_depth`) is one killable group.
- Identity + a durable `is_group_alive(pgid, starttime)` liveness check turns the restart
  decision into a **policy per orphan**, not a hardcoded kill: **drain (bounded)** — wait up to a
  timeout for the worker to finish, then finalize its manifest record from the completed output
  files (a worker record and its repo edits — never a fabricated run result; the dead
  coordinator's outcome is not reconstructed); **reap** (SIGTERM→grace→SIGKILL) —
  the escape for force-stop / hung-past-timeout / unsafe-to-finish; or **ignore**. `starttime`
  verification guards every path against pid reuse. team-harness stays mechanism-neutral and does
  not hardcode the default; the recommended default for a cost-conscious, git-is-truth consumer
  is **bounded drain** (it avoids wasting near-complete work and half-applied edits, and the
  serialization objection is moot because draining happens during recovery before new work is
  dispatched). This extends `AgentManager.kill()` from in-memory-only to persisted-and-reapable.
- **Consumer contract (loopy-loop):** the consumer owns *its own* process liveness (e.g. a
  worker pid + heartbeat) and, on crash recovery, chooses a policy per orphan for the interrupted
  run before starting fresh. team-harness provides the manifest, the liveness check, and the
  policy operations; the consumer decides which and when. See
  `design/designs/process-lifecycle-and-reaping.md` for the full contract.
- **Cross-platform:** `start_new_session` + `os.killpg` work on macOS (dev) and Linux (prod);
  cgroup-based supervision would be more bulletproof but is Linux-only and is a documented
  non-goal for now.
- This does **not** add process *adoption* (TH-D2 still holds) — it adds cleanup of the
  processes a dead parent left behind.

Detailed design: `design/designs/process-lifecycle-and-reaping.md`.

## TH-D6. Per-spawn model/effort overrides fail loudly and are audited; the harness never second-guesses the choice

**Decision.** The coordinator may override a worker's model and reasoning effort per spawn
(`spawn_agent(model=…, effort=…)`); the explicit argument always wins over the agent
template's default. Two invariants govern the feature:

1. **Fail loudly, never silently.** An override that cannot take effect as requested
   returns a coordinator-visible ERROR from `spawn_agent` instead of being dropped or
   double-applied: an agent type whose template cannot carry an effort value (no
   `reasoning_effort_flag` with an `{effort}` placeholder — see
   `template_supports_effort()` in `agents/template.py`), a blank level, or a raw
   `flags` entry that carries the same reasoning-effort option the override would render.
2. **The audit trail claims only what actually happened.** Each spawn records
   `requested_model`/`requested_effort` (the coordinator's explicit arguments; null =
   left to the template default) and `effective_model`/`effective_effort` (what was
   actually injected after resolution) on its `run.json` agent record. `effective_model`
   is null when the template has no model-injection surface (`model_flag` /
   `model_env_vars`), and for env-only templates it accounts for caller `env` overrides
   winning the spawn-env merge (null when the override is partial or conflicting) — see
   `_recorded_model()` in `agents/spawner.py`.

**Context.** Built for loopy-loop's model-tier policy (loopy D9): strong harness
coordinators choose cheaper or stronger workers per task from named tiers, and an *outer
reviewer* — not the engine — verifies that e.g. a review actually ran on the strong tier.
That consumer makes honest audit fields the load-bearing part of the feature: a recorded
effort the worker never received, or a model claim for a template that injects nothing,
is worse than no record at all. The same reasoning rejects silent drops: a coordinator
that believes it escalated when it didn't will happily mark the work reviewed.

**Consequences.** The harness validates *renderability*, never *policy* — there is no
model allowlist, no cost fence, no per-depth restriction; whether a choice was wise is
the consumer's judgment call over the audit fields. A template whose
`reasoning_effort_flag` lacks the `{effort}` placeholder now renders nothing rather than
a valueless option (the level never reached the worker either way; the old behavior
could make the CLI eat the next token as the option's value). Anyone adding a new
injection surface to templates must extend `_recorded_model()`/`template_supports_effort()`
so the audit fields keep telling the truth.
