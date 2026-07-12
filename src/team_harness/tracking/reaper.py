"""Post-crash recovery for orphaned workers: drain, reap, or ignore.

If the parent process dies hard (OOM, SIGKILL, machine reboot), the workers it
spawned keep running — reparented to init, still spending money, still writing to
the checkout — and the in-memory handles that could kill them died with the parent.
``run.json`` is the crash-durable record of what a run launched: every worker's
pid/pgid/starttime is flushed there at spawn time, so a later process can find the
leftovers and apply a policy (TH-D5, ``design/designs/process-lifecycle-and-reaping.md``):

- **drain** (recommended default): wait up to a timeout for the group to finish on
  its own, then finalize its record from what is knowable. A drained orphan's exit
  code is unobtainable (only the dead parent could have waited on it) — drain
  yields a complete worker record and the repo edits the worker already made,
  never a fabricated run result. On timeout the group is reaped instead.
- **reap**: SIGTERM the group, wait a grace period, SIGKILL survivors.
- **ignore**: leave it running, but record that decision.

Every action verifies identity via ``(pgid, starttime)`` before touching anything,
so a recycled pid is never killed. Outcomes are written back into ``run.json``
(atomically), a ``reap_report.json`` is dropped beside it, and the
``worker_sessions.json`` manifest is refreshed when the run recorded where it lives.
"""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from team_harness.agents.process_identity import kill_group
from team_harness.agents.process_identity import probe_group
from team_harness.agents.process_identity import wait_group
from team_harness.tracking.models import AgentRecord
from team_harness.tracking.models import RunRecord
from team_harness.tracking.worker_sessions import write_worker_sessions_manifest

ReapPolicy = Literal["drain", "reap", "ignore"]
WorkerOutcome = Literal[
    "drained",
    "reaped",
    "drain_timed_out_then_reaped",
    "already_exited",
    "identity_mismatch_skipped",
    "identity_unverifiable_skipped",
    "left_running",
    "no_process_identity",
]

DEFAULT_DRAIN_TIMEOUT_S = 600.0
DEFAULT_GRACE_S = 10.0

_RUNNING_STATUSES = {"running"}


class WorkerReapOutcome(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=False)

    agent_id: str
    agent_type: str
    pid: int | None
    pgid: int | None
    outcome: WorkerOutcome


class ReapReport(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=False)

    schema_version: int = 1
    run_id: str
    policy: ReapPolicy
    executed_at: datetime
    workers: list[WorkerReapOutcome] = Field(default_factory=list)

    @property
    def touched(self) -> int:
        return sum(
            1
            for worker in self.workers
            if worker.outcome
            in {"drained", "reaped", "drain_timed_out_then_reaped", "already_exited"}
        )


def resolve_run_json(run_ref: Path) -> Path:
    """Accept a run directory or a direct path to its ``run.json``."""
    if run_ref.is_dir():
        return run_ref / "run.json"
    return run_ref


def reap_run(
    run_ref: Path,
    *,
    policy: ReapPolicy = "drain",
    drain_timeout_s: float = DEFAULT_DRAIN_TIMEOUT_S,
    grace_s: float = DEFAULT_GRACE_S,
) -> ReapReport:
    """Apply ``policy`` to every worker the run left marked running.

    Reads ``run.json`` (the spawn-time-durable record), probes each running
    worker's group identity, acts, and persists the outcomes: worker statuses are
    updated in ``run.json`` (atomic replace), a ``reap_report.json`` is written
    beside it, and ``worker_sessions.json`` is refreshed when the run recorded
    its session output dir.
    """
    run_json = resolve_run_json(run_ref)
    record = RunRecord.model_validate_json(run_json.read_text())
    now = datetime.now(timezone.utc)
    outcomes: list[WorkerReapOutcome] = []
    changed = False

    for agent in record.agents:
        if agent.status not in _RUNNING_STATUSES:
            continue
        outcome = _apply_policy(
            agent, policy=policy, drain_timeout_s=drain_timeout_s, grace_s=grace_s
        )
        outcomes.append(
            WorkerReapOutcome(
                agent_id=agent.id,
                agent_type=agent.agent_type,
                pid=agent.pid,
                pgid=agent.pgid,
                outcome=outcome,
            )
        )
        new_status = _status_for_outcome(outcome)
        if new_status is not None:
            agent.status = new_status
            if agent.finished_at is None:
                agent.finished_at = now
            changed = True

    report = ReapReport(
        run_id=record.run_id, policy=policy, executed_at=now, workers=outcomes
    )
    if changed:
        _write_json_atomic(run_json, record.model_dump(mode="json"))
        if record.session_output_dir is not None:
            # Same finalization a graceful run performs — refresh the manifest
            # from the updated records (statuses now carry the reap verdicts).
            write_worker_sessions_manifest(
                run_id=record.run_id,
                session_output_dir=record.session_output_dir,
                agents=record.agents,
            )
    _write_json_atomic(
        run_json.with_name("reap_report.json"), report.model_dump(mode="json")
    )
    return report


def _apply_policy(
    agent: AgentRecord, *, policy: ReapPolicy, drain_timeout_s: float, grace_s: float
) -> WorkerOutcome:
    if agent.pgid is None:
        # Recorded before process identity existed (pre-v0.2.11): nothing safe
        # to act on. The worker may or may not still run; we cannot tell.
        return "no_process_identity"
    liveness = probe_group(agent.pgid, agent.starttime)
    if not liveness.alive:
        return "already_exited"
    if liveness.verdict == "identity_mismatch":
        return "identity_mismatch_skipped"
    if policy == "ignore":
        return "left_running"
    if liveness.verdict == "unverifiable":
        # Waiting on an unverifiable group is safe; killing it is not.
        if policy == "drain" and wait_group(
            agent.pgid, agent.starttime, timeout_s=drain_timeout_s
        ):
            return "drained"
        return "identity_unverifiable_skipped"
    if policy == "drain":
        if wait_group(agent.pgid, agent.starttime, timeout_s=drain_timeout_s):
            return "drained"
        kill_group(agent.pgid, agent.starttime, grace_s=grace_s)
        return "drain_timed_out_then_reaped"
    kill_group(agent.pgid, agent.starttime, grace_s=grace_s)
    return "reaped"


def _status_for_outcome(outcome: str) -> str | None:
    if outcome in {"drained", "reaped", "drain_timed_out_then_reaped"}:
        return outcome
    if outcome == "already_exited":
        # The group finished before anyone looked. Its exit code is unknowable
        # (the dead parent was the only process that could have waited on it),
        # which is exactly the drained condition.
        return "drained"
    return None


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    temp_path.write_text(json.dumps(payload, indent=2))
    os.replace(temp_path, path)
