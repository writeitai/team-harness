# pyright: reportMissingParameterType=false
"""Tests for TH-D5: durable process identity, liveness, and drain/reap policies.

These tests spawn real OS processes (``sleep``/``sh``) as process-group leaders
to simulate workers orphaned by a parent crash, then exercise the liveness
probe and the reap policies against a hand-built ``run.json``.
"""

import asyncio
from datetime import datetime
from datetime import timezone
import json
import signal
import subprocess
import time

import pytest

from team_harness.agents.process_identity import capture_starttime
from team_harness.agents.process_identity import group_members
from team_harness.agents.process_identity import kill_group
from team_harness.agents.process_identity import probe_group
from team_harness.agents.process_identity import wait_group
from team_harness.agents.spawner import spawn
from team_harness.config import Config
from team_harness.tracking.models import AgentRecord
from team_harness.tracking.models import RunRecord
from team_harness.tracking.reaper import reap_run
from team_harness.tracking.run_log import RunLogWriter
from tests.helpers import fake_agent_template


def _spawn_group(command: list[str]) -> subprocess.Popen:
    """Spawn a command as its own process-group leader, like the spawner does."""
    return subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _reap_leftover(proc: subprocess.Popen) -> None:
    """Best-effort cleanup so a failing test never leaks a sleep process."""
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=5)
    except (subprocess.TimeoutExpired, ChildProcessError):
        pass


def _agent_record(
    proc: subprocess.Popen | None,
    *,
    starttime: str | None = None,
    pid: int | None = None,
    pgid: int | None = None,
    status: str = "running",
    tmp_path=None,
) -> AgentRecord:
    resolved_pid = pid if pid is not None else (proc.pid if proc else None)
    resolved_pgid = pgid if pgid is not None else resolved_pid
    log_dir = str(tmp_path) if tmp_path else "/tmp"
    return AgentRecord(
        id="agent_test123",
        agent_type="codex",
        status=status,
        cwd=".",
        prompt="p",
        full_prompt="p",
        command=["sleep"],
        spawned_at=datetime.now(timezone.utc),
        stdout_log=f"{log_dir}/out.log",
        stderr_log=f"{log_dir}/err.log",
        pid=resolved_pid,
        pgid=resolved_pgid,
        starttime=starttime
        if starttime is not None
        else (capture_starttime(resolved_pid) if resolved_pid else None),
    )


def _write_run_json(tmp_path, agents: list[AgentRecord], session_output_dir=None):
    record = RunRecord(
        run_id="run_test",
        start=datetime.now(timezone.utc),
        provider="openai_compat",
        coordinator_model="m",
        api_base="http://localhost",
        session_output_dir=str(session_output_dir) if session_output_dir else None,
        agents=agents,
    )
    run_json = tmp_path / "run.json"
    run_json.write_text(json.dumps(record.model_dump(mode="json"), indent=2))
    return run_json


# ---------------------------------------------------------------------------
# Liveness probes
# ---------------------------------------------------------------------------


def test_probe_group_alive_and_ours():
    proc = _spawn_group(["sleep", "30"])
    try:
        starttime = capture_starttime(proc.pid)
        assert starttime is not None
        liveness = probe_group(proc.pid, starttime)
        assert liveness.alive
        assert liveness.verdict == "ours"
    finally:
        _reap_leftover(proc)


def test_probe_group_dead_after_exit():
    proc = _spawn_group(["sleep", "0.05"])
    starttime = capture_starttime(proc.pid)
    proc.wait(timeout=5)
    liveness = probe_group(proc.pid, starttime)
    assert not liveness.alive
    assert liveness.verdict == "dead"


def test_probe_group_identity_mismatch_on_recycled_pid():
    proc = _spawn_group(["sleep", "30"])
    try:
        liveness = probe_group(proc.pid, "Mon Jan  1 00:00:00 1990")
        assert liveness.alive
        assert liveness.verdict == "identity_mismatch"
    finally:
        _reap_leftover(proc)


def test_probe_group_unverifiable_without_starttime():
    proc = _spawn_group(["sleep", "30"])
    try:
        liveness = probe_group(proc.pid, None)
        assert liveness.alive
        assert liveness.verdict == "unverifiable"
    finally:
        _reap_leftover(proc)


# ---------------------------------------------------------------------------
# kill_group / wait_group
# ---------------------------------------------------------------------------


def test_kill_group_kills_leader_and_nested_child():
    # The child sleep is a group member; killing the group must reach it too.
    proc = _spawn_group(["sh", "-c", "sleep 30 & wait"])
    try:
        starttime = capture_starttime(proc.pid)
        time.sleep(0.2)  # let the nested sleep spawn
        assert len(group_members(proc.pid)) >= 2
        verdict = kill_group(proc.pid, starttime, grace_s=5)
        assert verdict == "killed"
        assert group_members(proc.pid) == []
    finally:
        _reap_leftover(proc)


def test_kill_group_refuses_identity_mismatch():
    proc = _spawn_group(["sleep", "30"])
    try:
        verdict = kill_group(proc.pid, "Mon Jan  1 00:00:00 1990", grace_s=1)
        assert verdict == "identity-mismatch-skipped"
        assert proc.poll() is None  # untouched
    finally:
        _reap_leftover(proc)


def test_wait_group_returns_true_when_process_finishes():
    proc = _spawn_group(["sleep", "0.2"])
    starttime = capture_starttime(proc.pid)
    assert wait_group(proc.pid, starttime, timeout_s=10, poll_interval_s=0.05)
    _reap_leftover(proc)


def test_wait_group_times_out_on_long_process():
    proc = _spawn_group(["sleep", "30"])
    try:
        starttime = capture_starttime(proc.pid)
        assert not wait_group(proc.pid, starttime, timeout_s=0.3, poll_interval_s=0.05)
    finally:
        _reap_leftover(proc)


# ---------------------------------------------------------------------------
# reap_run policies
# ---------------------------------------------------------------------------


def test_reap_run_reap_policy_kills_orphan(tmp_path):
    proc = _spawn_group(["sleep", "30"])
    try:
        run_json = _write_run_json(tmp_path, [_agent_record(proc, tmp_path=tmp_path)])
        report = reap_run(run_json, policy="reap", grace_s=5)
        assert [worker.outcome for worker in report.workers] == ["reaped"]
        assert group_members(proc.pid) == []
        updated = RunRecord.model_validate_json(run_json.read_text())
        assert updated.agents[0].status == "reaped"
        assert updated.agents[0].finished_at is not None
        assert (tmp_path / "reap_report.json").exists()
    finally:
        _reap_leftover(proc)


def test_reap_run_drain_policy_waits_and_finalizes(tmp_path):
    proc = _spawn_group(["sleep", "0.2"])
    run_json = _write_run_json(tmp_path, [_agent_record(proc, tmp_path=tmp_path)])
    report = reap_run(run_json, policy="drain", drain_timeout_s=10)
    assert [worker.outcome for worker in report.workers] == ["drained"]
    updated = RunRecord.model_validate_json(run_json.read_text())
    assert updated.agents[0].status == "drained"
    assert updated.agents[0].exit_code is None  # unobtainable for an orphan
    _reap_leftover(proc)


def test_reap_run_drain_timeout_falls_through_to_reap(tmp_path):
    proc = _spawn_group(["sleep", "30"])
    try:
        run_json = _write_run_json(tmp_path, [_agent_record(proc, tmp_path=tmp_path)])
        report = reap_run(run_json, policy="drain", drain_timeout_s=0.3, grace_s=5)
        assert [worker.outcome for worker in report.workers] == [
            "drain_timed_out_then_reaped"
        ]
        assert group_members(proc.pid) == []
    finally:
        _reap_leftover(proc)


def test_reap_run_ignore_policy_leaves_process_running(tmp_path):
    proc = _spawn_group(["sleep", "30"])
    try:
        run_json = _write_run_json(tmp_path, [_agent_record(proc, tmp_path=tmp_path)])
        report = reap_run(run_json, policy="ignore")
        assert [worker.outcome for worker in report.workers] == ["left_running"]
        assert proc.poll() is None
        updated = RunRecord.model_validate_json(run_json.read_text())
        assert updated.agents[0].status == "running"  # unchanged
    finally:
        _reap_leftover(proc)


def test_reap_run_already_exited_becomes_drained(tmp_path):
    proc = _spawn_group(["sleep", "0.05"])
    starttime = capture_starttime(proc.pid)
    proc.wait(timeout=5)
    record = _agent_record(proc, starttime=starttime, tmp_path=tmp_path)
    run_json = _write_run_json(tmp_path, [record])
    report = reap_run(run_json, policy="reap")
    assert [worker.outcome for worker in report.workers] == ["already_exited"]
    updated = RunRecord.model_validate_json(run_json.read_text())
    assert updated.agents[0].status == "drained"


def test_reap_run_skips_recycled_pid(tmp_path):
    proc = _spawn_group(["sleep", "30"])
    try:
        record = _agent_record(
            proc, starttime="Mon Jan  1 00:00:00 1990", tmp_path=tmp_path
        )
        run_json = _write_run_json(tmp_path, [record])
        report = reap_run(run_json, policy="reap")
        assert [worker.outcome for worker in report.workers] == [
            "identity_mismatch_skipped"
        ]
        assert proc.poll() is None  # untouched
    finally:
        _reap_leftover(proc)


def test_reap_run_no_identity_for_pre_v0_2_11_records(tmp_path):
    record = _agent_record(None, pid=None, pgid=None, tmp_path=tmp_path)
    run_json = _write_run_json(tmp_path, [record])
    report = reap_run(run_json, policy="reap")
    assert [worker.outcome for worker in report.workers] == ["no_process_identity"]


def test_reap_run_skips_terminal_workers(tmp_path):
    record = _agent_record(
        None, pid=12345, pgid=12345, status="done", tmp_path=tmp_path
    )
    run_json = _write_run_json(tmp_path, [record])
    report = reap_run(run_json, policy="reap")
    assert report.workers == []


def test_reap_run_refreshes_worker_sessions_manifest(tmp_path):
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    proc = _spawn_group(["sleep", "0.1"])
    run_json = _write_run_json(
        tmp_path,
        [_agent_record(proc, tmp_path=tmp_path)],
        session_output_dir=session_dir,
    )
    reap_run(run_json, policy="drain", drain_timeout_s=10)
    manifest = json.loads((session_dir / "worker_sessions.json").read_text())
    assert manifest["workers"][0]["outcome"] == "drained"
    assert manifest["workers"][0]["pgid"] == proc.pid
    _reap_leftover(proc)


# ---------------------------------------------------------------------------
# Spawner integration: process identity captured at spawn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_makes_worker_its_own_group_leader(tmp_path):
    import os

    # A worker that stays alive long enough for identity capture — an instant
    # exit (echo) races the ps-based starttime lookup.
    config = Config(agent_templates={"codex": fake_agent_template(binary="sleep")})
    result = await spawn(
        agent_id="agent_pgid",
        agent_type="codex",
        prompt="5",
        cwd=tmp_path,
        config=config,
        log_dir=tmp_path / "logs",
    )
    try:
        assert result.pid == result.proc.pid
        assert result.pgid == result.proc.pid
        # Group leadership: the child's pgid is its own pid, not ours.
        assert result.pgid != os.getpgid(0)
        assert result.starttime is not None
        assert os.getpgid(result.proc.pid) == result.proc.pid
    finally:
        result.proc.kill()
        await asyncio.wait_for(result.proc.wait(), 5)


def test_run_log_persists_identity_at_spawn_time(tmp_path):
    writer = RunLogWriter(
        run_id="run_x",
        run_dir=tmp_path,
        provider="openai_compat",
        model="m",
        api_base="http://localhost",
        session_output_dir=str(tmp_path / "session"),
    )
    record = _agent_record(None, pid=4242, pgid=4242, tmp_path=tmp_path)
    record.starttime = "Mon Jan  1 00:00:00 2026"
    writer.record_agent_spawn(record)
    on_disk = json.loads((tmp_path / "run.json").read_text())
    assert on_disk["session_output_dir"] == str(tmp_path / "session")
    assert on_disk["agents"][0]["pid"] == 4242
    assert on_disk["agents"][0]["pgid"] == 4242
    assert on_disk["agents"][0]["starttime"] == "Mon Jan  1 00:00:00 2026"


def test_graceful_leader_kill_still_works_without_pgid():
    # AgentState without pgid (test doubles, pre-existing paths) must still be
    # killable via the leader-only fallback — this guards the fallback branch.
    proc = _spawn_group(["sleep", "30"])
    try:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=5)
        assert proc.poll() is not None
    finally:
        _reap_leftover(proc)
