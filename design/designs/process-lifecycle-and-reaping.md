# Design: Worker Process Lifecycle and Orphan Reaping

**Status:** Proposed (design accepted as TH-D5; implementation pending)
**Date:** 2026-07-12
**Decision:** `design/decisions.md` TH-D5 (and its premises TH-D2, TH-D4).
**Primary consumer:** `loopy-loop`, which runs team-harness in a long-horizon loop and needs
crash recovery to be safe.

This document is self-contained: it explains the process model, why the naive fixes don't
work, and the design — enough for a future agent or a non-specialist human to implement it
without re-deriving the reasoning.

## 1. The process model (what actually runs)

A team-harness run is a tree of OS processes:

```
parent process (the SDK/CLI caller — e.g. a `loopy worker`)
└── team-harness coordinator loop        (in-process; async, same PID as the parent)
    ├── worker CLI subprocess  (codex/claude/gemini/…)   ← child of the parent
    │   └── (that CLI may itself spawn helpers)
    └── worker CLI subprocess  …                          ← child of the parent
```

Key facts, all verifiable in the code:

- The coordinator loop runs **in the parent process** (`harness.py` → `coordinator/loop.py`);
  it is not a separate process.
- Each **worker** is launched with `asyncio.create_subprocess_exec(...)` in
  `agents/spawner.py`, with `stdin=DEVNULL` and `stdout`/`stderr` redirected to **log files**.
  It is a **one-shot batch process**: it runs to completion and exits (TH-D2).
- The only handle to a running worker is the in-memory `asyncio.subprocess.Process` stored in
  `AgentManager` (`agents/manager.py`). `AgentManager.kill()` calls `proc.terminate()` on that
  handle.
- team-harness already **persists a rich per-worker manifest** to disk
  (`tracking/worker_sessions.py`, `WorkerSessionRecord` / `WorkerSessionsManifest`): agent id,
  command, cwd, spawn/finish times, exit code, log paths, captured vendor session id, resume
  info. **It does not persist any OS process identity** (no pid, no pgid).
- Workers are spawned in the parent's **own process group** (no `start_new_session`), and
  nesting is bounded by the configured `max_depth` (default 3).

## 2. The problem: orphaned workers after a hard parent crash

If the parent process ends **gracefully**, team-harness finalizes: it terminates tracked
workers (within `shutdown_timeout_s`) and writes the manifest. Fine.

If the parent process dies **hard** — OOM kill, `SIGKILL`, a panic, a machine reboot — none of
that runs. The workers it spawned are reparented to init and **keep running**:

- they keep **spending money** (each is an LLM-backed CLI making API calls),
- they keep **writing to the target checkout** (uncoordinated with whatever restarts),
- and **nothing tracks them**: the `AgentManager` handles died with the parent, and no pid was
  ever written to disk. A restarted parent has no idea they exist. There is no startup reaping
  anywhere in team-harness or its consumers today.

This is a real cost and correctness hazard for any long-running consumer.

## 3. Why the obvious fixes don't work

- **"Re-adopt the running worker on restart."** Not possible. Process control is tied to the
  dead parent's asyncio transport; a new process cannot reconnect a child's stdio or rebuild
  the transport. And there's nothing to drive anyway — the worker is a one-shot batch job with
  `stdin=DEVNULL` (TH-D2). Adoption is both impossible and pointless.
- **"Just kill the pid on restart."** A bare pid is unsafe to kill after any time gap: the OS
  may have **recycled** it to an unrelated process. Killing a recycled pid can kill something
  innocent.
- **"Resume the work instead."** That's a *different* concern (TH-D4, session resume) and
  doesn't address the orphan that's still running and spending money. Resume is about
  continuing the work; reaping is about stopping the leftover.

## 4. The design

Two changes to team-harness, plus a clear contract for the consumer.

### 4.1 Spawn each worker in its own process group

In `agents/spawner.py`, pass `start_new_session=True` to `create_subprocess_exec`. This makes
each worker a **process-group leader**; the worker and any helpers it spawns share one group.
Killing the group (`os.killpg`) then reliably terminates the **whole subtree** in one call —
which matters because a worker CLI may spawn its own children up to `max_depth`.

### 4.2 Persist process identity in the manifest

Extend `WorkerSessionRecord` with:

- `pid: int` — the worker's process id,
- `pgid: int` — its process-group id (== pid, since it's the group leader),
- `starttime: str | int` — the process start time (Linux: field 22 of `/proc/<pid>/stat`;
  portable fallback: the wall-clock spawn time we already record, used as a coarse guard).

`starttime` is the **identity guard**: a `(pgid, starttime)` pair is effectively unique, so we
can tell "the group we launched" from "a recycled id now owned by something else." Write these
at spawn time and keep updating `status`/`exit_code`/`finished_at` on exit, exactly as the
manifest is maintained today.

### 4.3 A durable liveness check

Once identity is persisted, "is this worker still running?" becomes a durable, cross-process
question — today it can only be answered through the in-memory `Process` handle, which dies
with the parent. Add a small helper:

```
is_group_alive(pgid, starttime) -> bool
```

that checks the group leader exists (`os.kill(pgid, 0)` / a `/proc` probe) **and** its
`starttime` matches what we recorded. The `starttime` guard is what makes this safe against pid
reuse: a live pid with a *different* start time is a recycled id, not our worker. This helper
underpins both the reclaim-safety check (a consumer verifying "is the previous run actually
dead?") and every policy below.

### 4.4 On restart, choose a policy per worker — reap is one option, not the only one

Persisted identity + liveness turns the restart decision from a hardcoded kill into a **policy**.
For each worker the manifest still marks `running`, and that `is_group_alive` confirms, the
caller can choose:

- **reap** *(the default for a fast, clean restart)* — send `SIGTERM` to the group, wait a short
  grace period, then `SIGKILL`. Stops the leftover immediately.
- **drain** *(let it finish, then harvest)* — do **not** kill it; wait (poll `is_group_alive`)
  until the group exits, then parse its now-complete output files exactly as the normal flow
  would (`session_capture` etc.) and record the salvaged result. Worthwhile when the in-flight
  work is expensive or nearly done.
- **ignore** — leave it and record that it was left running (e.g. a human will decide).

Two properties make this safe and honest, and callers must respect them:

- **Drain requires serialization.** While an orphan is draining, the caller must not start new
  work on the same checkout — two writers on one working tree is exactly the corruption a
  single-worker consumer avoids. Drain therefore *pauses* fresh work until the orphan exits, and
  can block for a long time (a worker may be a 45-minute run). When you can't afford that wait,
  reap and re-run instead.
- **Drain salvages a worker, not necessarily a run.** If the caller's *coordinator* died too
  (e.g. loopy-loop runs the coordinator loop inside its worker, so a worker crash kills the
  coordinator conversation with it), draining a worker recovers that worker's output and the repo
  changes it made — but the orchestrating run is gone and still has to be re-run or reconstructed.
  Drain is a salvage tool, not a run-resume tool (run continuation is TH-D4, session resume).

The reap path is the natural extension of `AgentManager.kill()` from "terminate an in-memory
handle" to "terminate a persisted, possibly-orphaned group, safely." A convenient wrapper —
`reap_run(manifest_path, policy=...) -> ReapReport` (and/or a `th reap` CLI subcommand) — applies
a chosen policy across a run's manifest and records the outcome (`reaped` / `drained` /
`already-exited` / `identity-mismatch-skipped` / `left-running`) back into it.

### 4.5 What we deliberately do NOT do

- No process **adoption**/reattachment (TH-D2).
- No cgroup/systemd-scope supervision. It would be more bulletproof but is Linux-only; we
  target macOS (dev) and Linux (prod) with the same code, so `start_new_session` + `os.killpg`
  is the portable choice. Documented non-goal, revisit only if the process-group approach
  proves insufficient on Linux at scale.

## 5. Consumer contract (how `loopy-loop` uses this)

Responsibilities split cleanly:

- **team-harness owns worker-process lifecycle**: it launches workers in their own groups,
  persists their identity, and provides the liveness helper and the policy operations (reap /
  drain / ignore). It does **not** decide *which policy* or *when* — it has no knowledge of the
  consumer's crash/restart semantics.
- **The consumer owns liveness of its own process and the policy decision.** `loopy-loop` runs
  the harness inside its own `loopy worker` process. It should:
  - record its **worker pid + a heartbeat** in the session directory, so a restarted
    coordinator can tell "the worker is alive and busy" from "the worker is dead" — this closes
    the duplicate-work window where a second `/register` reclaims a task that's still running;
  - on crash recovery, for the interrupted run's manifest, **pick a policy per orphan**: reap
    (the default — kill leftovers, then re-run the iteration), or drain (wait for an
    expensive/nearly-done worker to finish and harvest its output, *pausing* fresh work until it
    exits — see §4.4), or ignore;
  - surface it operationally (a `doctor` check that warns about a leftover group; a
    `stop --force` that reaps).

The relationship to loopy-loop's own design: its crash-recovery decision (recover session
*state* from files) is about the coordinator; this design is about the worker *processes* the
harness spawned. They are complementary — state recovery says "what task were we on," reaping
says "kill the leftover agents from the task we abandoned." Neither is process adoption.

## 6. Implementation checklist

- [ ] `agents/spawner.py`: `start_new_session=True`; capture `pid`/`pgid`/`starttime` into the
      `SpawnResult`.
- [ ] `tracking/models.py`: add `pid`/`pgid`/`starttime` to `WorkerSessionRecord`
      (and any live `AgentState`/`AgentRecord` carrier they derive from).
- [ ] `tracking/worker_sessions.py`: persist the new fields at spawn and on status updates.
- [ ] `is_group_alive(pgid, starttime)` liveness helper (§4.3).
- [ ] Policy operations: `reap` (SIGTERM→grace→SIGKILL), `drain` (wait for exit + harvest),
      and a `reap_run(manifest_path, policy=...)` wrapper + `th reap` CLI subcommand, all with
      `(pgid, starttime)` verification (§4.4).
- [ ] Tests: liveness true/false + recycled-id guard (starttime mismatch → not-alive/skip);
      orphan reap (spawn a sleep, drop the handle, reap via manifest); drain (short-lived
      worker → wait → harvest output); graceful path unaffected; group kill reaches a nested
      child.
- [ ] `CHANGELOG.md`: note the manifest schema addition and the new `reap` surface
      (consumer-facing — AGENTS.md Rule 3).
