"""Post-crash recovery for orphaned workers: drain, reap, or ignore.

If the parent process dies hard (OOM, SIGKILL, machine reboot), the workers it
spawned keep running — reparented to init, still spending money, still writing to
the checkout — and the in-memory handles that could kill them died with the parent.
``run.json`` is the crash-durable record of what a run launched: every worker's
pid/pgid/starttime is flushed there at spawn time, so a later process can find the
leftovers and apply a policy (TH-D5, ``design/designs/process-lifecycle-and-reaping.md``):

- **drain** (recommended default): wait — under ONE shared deadline for all draining
  workers — for each group to finish on its own, then finalize its record from what
  is knowable (including a best-effort vendor session-id capture from its stdout
  log). A drained orphan's exit code is unobtainable (only the dead parent could
  have waited on it); drain yields a complete worker record and the repo edits the
  worker already made, never a fabricated run result. On timeout the group is reaped.
- **reap**: SIGTERM the group, wait a grace period, SIGKILL survivors, verify gone.
- **ignore**: leave it running, but record that decision.

Safety properties:

- Every destructive action verifies ``(pgid, starttime)`` identity immediately
  before every signal; a recycled or unverifiable pid is never killed.
- A probe failure (broken ``ps``) is reported as ``probe_failed`` — never treated
  as "the worker is gone".
- ``reap_run`` refuses to act while the run's original parent process is still
  alive (identity-verified), unless ``force=True``.
- Concurrent reapers serialize on an advisory file lock; all JSON writes are
  atomic via unique temp files.
- Outcomes are honest: a worker is only marked terminal when its group was
  *observed* gone (or verified as someone else's recycled pid).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
import fcntl
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Literal

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from team_harness.agents.process_identity import capture_starttime
from team_harness.agents.process_identity import kill_group
from team_harness.agents.process_identity import probe_group
from team_harness.agents.process_identity import ProcessProbeError
from team_harness.agents.process_identity import wait_groups
from team_harness.agents.registry import resolve_template
from team_harness.agents.session_capture import extract_session_id
from team_harness.config import load_config
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
    "kill_failed_still_running",
    "probe_failed",
    "left_running",
    "no_process_identity",
]

_VALID_POLICIES: frozenset[str] = frozenset({"drain", "reap", "ignore"})

DEFAULT_DRAIN_TIMEOUT_S = 600.0
DEFAULT_GRACE_S = 10.0

_SESSION_CAPTURE_MAX_BYTES = 65536


class ReapRefusedError(RuntimeError):
    """The run's original parent process still appears to be alive."""


class WorkerReapOutcome(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=False)

    agent_id: str
    agent_type: str
    pid: int | None
    pgid: int | None
    policy: ReapPolicy
    outcome: WorkerOutcome
    observed_at: datetime


class ReapReport(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=False)

    schema_version: int = 1
    run_id: str
    policy: ReapPolicy
    dry_run: bool = False
    executed_at: datetime
    workers: list[WorkerReapOutcome] = Field(default_factory=list)


def resolve_run_json(run_ref: Path) -> Path:
    """Accept a run directory or a direct path to its ``run.json``."""
    if run_ref.is_dir():
        return run_ref / "run.json"
    return run_ref


def _validate_inputs(
    *,
    policy: str,
    policies: Mapping[str, str] | None,
    drain_timeout_s: float,
    grace_s: float,
) -> None:
    """Reject invalid arguments BEFORE anything is read, probed, or signalled."""
    invalid = {policy} | set((policies or {}).values())
    invalid -= _VALID_POLICIES
    if invalid:
        raise ValueError(
            f"invalid reap policy value(s): {sorted(invalid)!r}; "
            f"expected one of {sorted(_VALID_POLICIES)}"
        )
    for name, value in (("drain_timeout_s", drain_timeout_s), ("grace_s", grace_s)):
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a finite, non-negative number") from None
        if not math.isfinite(numeric) or numeric < 0:
            raise ValueError(f"{name} must be a finite, non-negative number")


def _parent_still_alive(record: RunRecord) -> bool:
    """Identity-verified liveness of the process that owned this run."""
    if record.parent_pid is None or record.parent_starttime is None:
        return False
    current = capture_starttime(record.parent_pid)
    return current is not None and current == record.parent_starttime


def reap_run(
    run_ref: Path,
    *,
    policy: ReapPolicy = "drain",
    policies: Mapping[str, ReapPolicy] | None = None,
    drain_timeout_s: float = DEFAULT_DRAIN_TIMEOUT_S,
    grace_s: float = DEFAULT_GRACE_S,
    force: bool = False,
    dry_run: bool = False,
) -> ReapReport:
    """Apply a policy to every worker the run left marked running.

    ``policy`` is the default; ``policies`` overrides it per ``agent_id`` (the
    documented per-orphan choice). ``dry_run=True`` probes and reports what
    would happen without signalling anything or writing any file. Draining
    workers share ONE ``drain_timeout_s`` deadline, not one per worker.

    Refuses to act (``ReapRefusedError``) while the run's original parent is
    still alive — identity-verified — unless ``force=True``.
    """
    _validate_inputs(
        policy=policy,
        policies=policies,
        drain_timeout_s=drain_timeout_s,
        grace_s=grace_s,
    )
    run_json = resolve_run_json(run_ref)
    if dry_run:
        record = RunRecord.model_validate_json(run_json.read_text())
        return _execute(
            record=record,
            run_json=run_json,
            policy=policy,
            policies=policies or {},
            drain_timeout_s=drain_timeout_s,
            grace_s=grace_s,
            force=force,
            dry_run=True,
        )
    lock_path = run_json.with_name(run_json.name + ".lock")
    with lock_path.open("a") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            record = RunRecord.model_validate_json(run_json.read_text())
            return _execute(
                record=record,
                run_json=run_json,
                policy=policy,
                policies=policies or {},
                drain_timeout_s=drain_timeout_s,
                grace_s=grace_s,
                force=force,
                dry_run=False,
            )
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _execute(
    *,
    record: RunRecord,
    run_json: Path,
    policy: ReapPolicy,
    policies: Mapping[str, ReapPolicy],
    drain_timeout_s: float,
    grace_s: float,
    force: bool,
    dry_run: bool,
) -> ReapReport:
    if not force and _parent_still_alive(record):
        raise ReapRefusedError(
            f"run {record.run_id}: parent process {record.parent_pid} is still "
            "alive (identity verified) — reaping a live run would kill its "
            "active workers. Pass force=True / --force to override."
        )

    running = [agent for agent in record.agents if agent.status == "running"]
    outcomes: dict[str, WorkerOutcome] = {}
    verdicts: dict[str, str] = {}
    chosen: dict[str, ReapPolicy] = {
        agent.id: policies.get(agent.id, policy) for agent in running
    }

    # Phase 1 — probe everything first.
    for agent in running:
        if agent.pgid is None:
            outcomes[agent.id] = "no_process_identity"
            continue
        try:
            liveness = probe_group(agent.pgid, agent.starttime)
        except ProcessProbeError:
            outcomes[agent.id] = "probe_failed"
            continue
        if not liveness.alive:
            outcomes[agent.id] = "already_exited"
            continue
        if liveness.verdict == "identity_mismatch":
            outcomes[agent.id] = "identity_mismatch_skipped"
            continue
        verdicts[agent.id] = liveness.verdict  # "ours" | "unverifiable"
        if chosen[agent.id] == "ignore":
            outcomes[agent.id] = "left_running"

    if dry_run:
        # Report what was observed; nothing was signalled, nothing is written.
        for agent_id in verdicts:
            outcomes.setdefault(agent_id, "left_running")
        return _build_report(
            record=record,
            running=running,
            chosen=chosen,
            outcomes=outcomes,
            policy=policy,
            dry_run=True,
        )

    # Phase 2 — all draining workers wait under ONE shared deadline.
    draining: dict[str, tuple[int, str | None]] = {
        agent.id: (agent.pgid, agent.starttime)
        for agent in running
        if agent.id in verdicts
        and chosen[agent.id] == "drain"
        and agent.pgid is not None
    }
    if draining:
        exited = wait_groups(
            {pgid: starttime for pgid, starttime in draining.values()},
            timeout_s=drain_timeout_s,
        )
        for agent_id, (pgid, _) in draining.items():
            if exited.get(pgid, False):
                outcomes[agent_id] = "drained"

    # Phase 3 — kill what must die: reap-policy workers, and drain timeouts.
    for agent in running:
        if agent.id in outcomes:
            continue
        active_policy = chosen[agent.id]
        if verdicts.get(agent.id) == "unverifiable":
            # Waiting was safe; killing an unverifiable group is not.
            outcomes[agent.id] = "identity_unverifiable_skipped"
            continue
        verdict = kill_group(agent.pgid, agent.starttime, grace_s=grace_s)  # type: ignore[arg-type]
        outcomes[agent.id] = _outcome_from_kill_verdict(
            verdict, timed_out_drain=(active_policy == "drain")
        )

    # Phase 4 — persist: statuses into run.json, manifest, report.
    now = datetime.now(timezone.utc)
    changed = False
    for agent in running:
        new_status = _status_for_outcome(outcomes[agent.id])
        if new_status is None:
            continue
        agent.status = new_status
        if agent.finished_at is None:
            agent.finished_at = now
        if new_status == "drained" and agent.session_id is None:
            agent.session_id = _post_hoc_session_id(agent)
        changed = True
    report = _build_report(
        record=record,
        running=running,
        chosen=chosen,
        outcomes=outcomes,
        policy=policy,
        dry_run=False,
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
    _persist_report(run_json=run_json, report=report)
    return report


def _outcome_from_kill_verdict(verdict: str, *, timed_out_drain: bool) -> WorkerOutcome:
    if verdict == "killed":
        return "drain_timed_out_then_reaped" if timed_out_drain else "reaped"
    if verdict == "already-exited":
        return "already_exited"
    if verdict == "identity-mismatch-skipped":
        return "identity_mismatch_skipped"
    if verdict == "identity-unverifiable-skipped":
        return "identity_unverifiable_skipped"
    if verdict == "probe-failed":
        return "probe_failed"
    return "kill_failed_still_running"


def _status_for_outcome(outcome: WorkerOutcome) -> str | None:
    """Only verified-terminal observations may change the durable status."""
    if outcome in {"drained", "reaped", "drain_timed_out_then_reaped"}:
        return outcome
    if outcome == "already_exited":
        # The group finished before anyone looked. Its exit code is unknowable
        # (the dead parent was the only process that could have waited on it),
        # which is exactly the drained condition.
        return "drained"
    return None


def _post_hoc_session_id(agent: AgentRecord) -> str | None:
    """Best-effort vendor session-id capture from a drained worker's stdout.

    The parent may have crashed before its background capture task parsed the
    session id; the drained log is complete now. Uses the *current* config's
    template for the agent type — the capture spec could in principle have
    changed since spawn, which is an accepted limitation.
    """
    try:
        template = resolve_template(agent_type=agent.agent_type, config=load_config())
        stdout_bytes = Path(agent.stdout_log).read_bytes()[-_SESSION_CAPTURE_MAX_BYTES:]
        return extract_session_id(template, stdout_bytes, None)
    except Exception:
        return None


def _build_report(
    *,
    record: RunRecord,
    running: list[AgentRecord],
    chosen: Mapping[str, ReapPolicy],
    outcomes: Mapping[str, WorkerOutcome],
    policy: ReapPolicy,
    dry_run: bool,
) -> ReapReport:
    now = datetime.now(timezone.utc)
    return ReapReport(
        run_id=record.run_id,
        policy=policy,
        dry_run=dry_run,
        executed_at=now,
        workers=[
            WorkerReapOutcome(
                agent_id=agent.id,
                agent_type=agent.agent_type,
                pid=agent.pid,
                pgid=agent.pgid,
                policy=chosen[agent.id],
                outcome=outcomes[agent.id],
                observed_at=agent.finished_at or now,
            )
            for agent in running
        ],
    )


def _persist_report(*, run_json: Path, report: ReapReport) -> None:
    """Keep an audit trail: timestamped report + a convenience latest pointer.

    A no-op invocation (nothing was running) never overwrites an earlier
    report that carried real outcomes.
    """
    payload = report.model_dump(mode="json")
    stamp = report.executed_at.strftime("%Y%m%dT%H%M%S%fZ")
    _write_json_atomic(run_json.with_name(f"reap_report_{stamp}.json"), payload)
    latest = run_json.with_name("reap_report.json")
    if report.workers or not latest.exists():
        _write_json_atomic(latest, payload)


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=path.parent, prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, indent=2)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
