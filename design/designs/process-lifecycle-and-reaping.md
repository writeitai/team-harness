# Design: Worker Process Lifecycle and Orphan Reaping

**Status:** Implemented (design accepted as TH-D5; shipped in the process-lifecycle
change — see `CHANGELOG.md` Unreleased. Implementation notes: the crash-durable
record is `run.json`, which is flushed at spawn time — `worker_sessions.json` is
finalize-only, so `reap_run()` reads `run.json`; and liveness excludes zombies,
which hold no resources and cannot be killed.)
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

### 4.4 On restart, choose a policy per worker — bounded drain is the sensible default

Persisted identity + liveness turns the restart decision from a hardcoded kill into a **policy**.
For each worker the manifest still marks `running`, and that `is_group_alive` confirms, the
caller can choose:

- **drain (bounded)** *(the recommended default)* — do **not** kill it; wait (poll
  `is_group_alive`, up to a **timeout**) for the group to exit, then **finalize the worker's
  manifest record from its now-complete output files** — status, exit code, captured vendor
  session id (`session_capture`) — exactly the finalization a graceful run performs. If the
  timeout elapses (a stuck/hung orphan), fall through to reap. This preserves near-complete work
  and leaves the checkout in a clean, fully-applied state.

  Be precise about what drain delivers: **a complete, auditable worker record plus whatever the
  worker already did to the checkout — not a run result.** The coordinator that would have
  consumed the worker's output is gone (it died with the parent), so drain must never fabricate
  the run-level outcome the coordinator would have produced. The consumer decides how to record
  the salvage in its own bookkeeping (see §5).
- **reap** — send `SIGTERM` to the group, wait a short grace period, then `SIGKILL`. Stops the
  leftover immediately. The escape hatch: for an explicit force-stop, a hung orphan past the
  drain timeout, or a crash cause that makes finishing unsafe (an OOM that would just re-trigger,
  disk full).
- **ignore** — leave it and record that it was left running (e.g. a human will decide).

Why drain is the better default (for a cost-conscious, git-is-truth consumer):

- **Killing mid-edit can corrupt the working tree.** An agent killed while writing files or
  staging changes leaves a half-applied mess; letting it finish yields a clean, complete change.
- **Completed work survives even if the iteration re-runs.** Under a git-is-truth consumer, a
  drained worker's commits/edits sit in the working tree, and the *next* run's fresh coordinator
  sees and builds on them. So drain salvages the substantive output (the repo change), not just a
  worker's stdout. (What it does *not* salvage is the dead coordinator's orchestration — draining
  is a salvage tool, not a run-resume tool; run continuation is TH-D4, session resume. The
  iteration may still be re-run, but from a better, completed starting point.)
- **The usual objection is weaker than it looks.** Draining happens *during recovery, before any
  new work is dispatched*, so there is no concurrent second writer on the checkout — the
  serialization is automatic, and the only real cost is latency, which the **timeout** bounds.

team-harness stays **mechanism-neutral**: it provides the liveness check, the three operations,
and the drain timeout, but does **not** hardcode which policy is the default — the crash/restart
semantics that decide belong to the consumer. The *recommendation* above (bounded drain) is what
suits a cost-conscious, git-is-truth consumer like loopy-loop; a different consumer may prefer
reap for the fastest clean slate.

The reap path is the natural extension of `AgentManager.kill()` from "terminate an in-memory
handle" to "terminate a persisted, possibly-orphaned group, safely." A convenient wrapper —
`reap_run(manifest_path, policy=..., drain_timeout_s=...) -> ReapReport` (and/or a `th reap` CLI
subcommand) — applies a chosen policy across a run's manifest and records the outcome (`drained` /
`reaped` / `drain-timed-out-then-reaped` / `already-exited` / `identity-mismatch-skipped` /
`left-running`) back into it.

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
  - on crash recovery, for the interrupted run's manifest, **pick a policy per orphan**
    (§4.4): bounded drain (the recommended default — let an in-flight worker finish within a
    timeout, then finalize its record), or reap (the escape — kill leftovers), or ignore;
  - after a drain, **write its own salvage record** linking the drained workers to its own unit
    of work — for loopy-loop: a `salvage.json` in the interrupted iteration's directory (drained
    agent ids, exit codes, pointers to their harness output dirs, a diffstat of the working
    tree) and a distinct history code (`abandoned_after_drain` rather than plain `abandoned`).
    The interrupted unit of work is still re-run — drain preserves the workers' output and repo
    edits, it does not produce the run result the dead coordinator never wrote;
  - surface it operationally (a `doctor` check that warns about a leftover group; a
    `stop --force` that reaps).

The relationship to loopy-loop's own design: its crash-recovery decision (recover session
*state* from files) is about the coordinator; this design is about the worker *processes* the
harness spawned. They are complementary — state recovery says "what task were we on," reaping
says "kill the leftover agents from the task we abandoned." Neither is process adoption.

## 6. Implementation checklist

- [x] `agents/spawner.py`: `start_new_session=True`; capture `pid`/`pgid`/`starttime` into the
      `SpawnResult`.
- [x] `tracking/models.py`: add `pid`/`pgid`/`starttime` to `WorkerSessionRecord`
      (and the `AgentRecord` carrier persisted at spawn time in `run.json`; `AgentState`
      carries `pgid` so graceful shutdown can group-kill stragglers).
- [x] `tracking/worker_sessions.py`: persist the new fields at spawn and on status updates.
- [x] Liveness helper (§4.3) — implemented as `probe_group(pgid, starttime)` in
      `agents/process_identity.py`, returning a verdict (`dead` / `ours` /
      `identity_mismatch` / `unverifiable`) rather than a bare bool; zombies are excluded.
- [x] Policy operations: `drain` (wait for exit up to `drain_timeout_s`, then finalize the
      record; timeout → reap), `reap` (SIGTERM→grace→SIGKILL), and
      `reap_run(run_ref, policy=..., drain_timeout_s=...)` + `th reap` CLI subcommand, all with
      `(pgid, starttime)` verification (§4.4). Note: `reap_run` reads **`run.json`** (flushed
      at spawn time — the crash-durable record), not `worker_sessions.json` (finalize-only);
      it refreshes the manifest afterward via the run's recorded `session_output_dir`.
- [x] Tests (`src/tests/test_process_lifecycle.py`): liveness true/false + recycled-id guard;
      drain (short-lived worker → wait → record finalized); drain timeout → falls through to
      reap; orphan reap; ignore policy; pre-identity records (`no_process_identity`); manifest
      refresh; spawner group leadership; graceful leader-kill fallback preserved; group kill
      reaches a nested child.
- [x] `CHANGELOG.md`: manifest schema 2→3, `run.json` additions + atomic writes, `th reap`,
      and the group-kill shutdown change (consumer-facing — AGENTS.md Rule 3).
