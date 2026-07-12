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

### 4.3 A reap operation

Add an operation — e.g. `reap_run(manifest_path) -> ReapReport` (and/or a `th reap` CLI
subcommand) — that:

1. reads the manifest,
2. for each worker still marked `running`, checks whether `pgid` is alive **and** its
   `starttime` matches what we recorded,
3. if and only if both match, sends `SIGTERM` to the group, waits a short grace period, then
   `SIGKILL`,
4. records the outcome (`reaped` / `already-exited` / `identity-mismatch-skipped`) back into the
   manifest and returns a small report.

This is the natural extension of `AgentManager.kill()` from "terminate an in-memory handle" to
"terminate a persisted, possibly-orphaned group, safely."

### 4.4 What we deliberately do NOT do

- No process **adoption**/reattachment (TH-D2).
- No cgroup/systemd-scope supervision. It would be more bulletproof but is Linux-only; we
  target macOS (dev) and Linux (prod) with the same code, so `start_new_session` + `os.killpg`
  is the portable choice. Documented non-goal, revisit only if the process-group approach
  proves insufficient on Linux at scale.

## 5. Consumer contract (how `loopy-loop` uses this)

Responsibilities split cleanly:

- **team-harness owns worker-process lifecycle**: it launches workers in their own groups,
  persists their identity, and provides `reap`. It does **not** decide *when* to reap — it has
  no knowledge of the consumer's crash/restart semantics.
- **The consumer owns its own liveness and the reap trigger.** `loopy-loop` runs the harness
  inside its own `loopy worker` process. It should:
  - record its **worker pid + a heartbeat** in the session directory, so a restarted
    coordinator can tell "the worker is alive and busy" from "the worker is dead" — this closes
    the duplicate-work window where a second `/register` reclaims a task that's still running;
  - on crash recovery, before starting fresh work for an interrupted run, call team-harness
    **reap** for that run's manifest to kill any orphaned workers;
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
- [ ] New `reap` function + `th reap` CLI subcommand, with `(pgid, starttime)` verification.
- [ ] Tests: orphan simulation (spawn a sleep, drop the handle, reap via manifest); recycled-id
      guard (starttime mismatch → skip); graceful path unaffected; group kill reaches a
      nested child.
- [ ] `CHANGELOG.md`: note the manifest schema addition and the new `reap` surface
      (consumer-facing — AGENTS.md Rule 3).
