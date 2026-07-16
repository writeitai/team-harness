from __future__ import annotations

from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from team_harness.tracking.models import AgentRecord
from team_harness.tracking.models import WorkerResumeInfo
from team_harness.tracking.models import WorkerSessionInfo
from team_harness.tracking.models import WorkerSessionRecord
from team_harness.tracking.models import WorkerSessionsManifest
from team_harness.tracking.persistence import write_json_atomic

_TAIL_CHARS = 4096


def resume_info_for_agent_type(agent_type: str) -> WorkerResumeInfo:
    if agent_type == "codex":
        return WorkerResumeInfo(supported=True, preferred_mode="resume")
    if agent_type == "gemini":
        return WorkerResumeInfo(supported=True, preferred_mode="resume")
    if agent_type == "claude":
        return WorkerResumeInfo(supported=True, preferred_mode="resume")
    if agent_type == "antigravity":
        return WorkerResumeInfo(supported=True, preferred_mode="resume")
    if agent_type == "opencode":
        return WorkerResumeInfo(supported=True, preferred_mode="continue")
    # openhands intentionally falls through: no session_capture, no resume wiring yet
    return WorkerResumeInfo(supported=False, preferred_mode=None)


def _tail_text(path: Path, n_chars: int = _TAIL_CHARS) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as handle:
        size = path.stat().st_size
        handle.seek(max(0, size - n_chars * 2))
        return handle.read().decode(errors="replace")[-n_chars:]


def _elapsed_seconds(record: AgentRecord) -> float | None:
    if record.finished_at is None:
        return None
    return max(0.0, (record.finished_at - record.spawned_at).total_seconds())


def _outcome(record: AgentRecord) -> str:
    if record.exit_code == 0:
        return "succeeded"
    if record.status == "killed":
        return "killed"
    # Post-crash reap verdicts (TH-D5): the parent that spawned the worker died,
    # so an orphan's exit code is unobtainable — status carries the outcome.
    if record.status in {"drained", "reaped", "drain_timed_out_then_reaped"}:
        return record.status
    if record.exit_code is None:
        return "running"
    if record.session_id:
        return "failed_after_session"
    return "failed_before_session"


def _summary_for(record: AgentRecord) -> str | None:
    if record.status == "killed" and record.exit_code == 0:
        return "Worker completed successfully, but was marked killed during coordinator cleanup."
    outcome = _outcome(record)
    if outcome == "failed_before_session":
        return "Worker exited before a provider session was captured."
    if outcome == "failed_after_session":
        return "Worker exited after a provider session was captured."
    if outcome == "killed":
        return "Worker was killed by team-harness."
    if outcome == "drained":
        return (
            "Worker was orphaned by a parent crash, allowed to finish (drained); "
            "its exit code is unknown because the original parent is gone."
        )
    if outcome == "reaped":
        return "Worker was orphaned by a parent crash and killed during reaping."
    if outcome == "drain_timed_out_then_reaped":
        return (
            "Worker was orphaned by a parent crash, did not finish within the "
            "drain timeout, and was killed."
        )
    return None


def _build_salvaged_worker(record: AgentRecord) -> dict[str, Any] | None:
    if record.exit_code != 0:
        return None
    stdout_path = Path(record.stdout_log).resolve()
    stdout_tail = _tail_text(stdout_path)
    if not stdout_tail:
        return None
    stderr_path = Path(record.stderr_log).resolve()
    return {
        "agent_id": record.id,
        "agent_type": record.agent_type,
        "status": record.status,
        "outcome": _outcome(record),
        "exit_code": record.exit_code,
        "elapsed_seconds": _elapsed_seconds(record),
        "cwd": record.cwd,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "stdout_tail": stdout_tail,
        "stderr_tail": _tail_text(stderr_path),
        "summary": _summary_for(record),
    }


def _recorded_command(command: list[str]) -> list[str]:
    """Copy the executed command into a JSON-compatible audit list."""

    return [str(item) for item in command]


def _artifact_paths(session_dir: Path, record: AgentRecord) -> dict[str, str | None]:
    workers_dir = session_dir / "workers"
    prefix = workers_dir / record.id
    return {
        "invocation_path": str(
            (prefix.with_name(prefix.name + "_invocation.json")).resolve()
        ),
        "exit_code_path": str(
            (prefix.with_name(prefix.name + "_exit_code.txt")).resolve()
        ),
        "stdout_tail_path": str(
            (prefix.with_name(prefix.name + "_stdout_tail.log")).resolve()
        ),
        "stderr_tail_path": str(
            (prefix.with_name(prefix.name + "_stderr_tail.log")).resolve()
        ),
    }


def _write_worker_artifacts(
    *, session_dir: Path, record: AgentRecord, stdout_tail: str, stderr_tail: str
) -> dict[str, str | None]:
    """Persist one worker's invocation, exit code, and output-tail artifacts."""

    paths = _artifact_paths(session_dir, record)
    invocation_path = Path(paths["invocation_path"] or "")
    exit_code_path = Path(paths["exit_code_path"] or "")
    stdout_tail_path = Path(paths["stdout_tail_path"] or "")
    stderr_tail_path = Path(paths["stderr_tail_path"] or "")
    invocation_path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(
        path=invocation_path,
        payload={
            "agent_id": record.id,
            "agent_type": record.agent_type,
            "cwd": record.cwd,
            "command": _recorded_command(command=record.command),
            "spawned_at": record.spawned_at.isoformat(),
            "finished_at": (
                record.finished_at.isoformat() if record.finished_at else None
            ),
            "status": record.status,
            "exit_code": record.exit_code,
            "session_id": record.session_id,
        },
    )
    exit_code_path.write_text(
        "" if record.exit_code is None else f"{record.exit_code}\n"
    )
    stdout_tail_path.write_text(stdout_tail)
    stderr_tail_path.write_text(stderr_tail)
    return paths


def _build_worker_session_record(
    *, record: AgentRecord, artifact_paths: dict[str, str | None] | None = None
) -> WorkerSessionRecord:
    stdout_path = Path(record.stdout_log).resolve()
    stderr_path = Path(record.stderr_log).resolve()
    stdout_tail = _tail_text(stdout_path)
    stderr_tail = _tail_text(stderr_path)
    artifacts = artifact_paths or {}
    return WorkerSessionRecord(
        agent_id=record.id,
        agent_type=record.agent_type,
        coordinator_turn_index=record.coordinator_turn_index,
        prompt=record.prompt,
        status=record.status,
        exit_code=record.exit_code,
        cwd=record.cwd,
        spawned_at=record.spawned_at,
        finished_at=record.finished_at,
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
        outcome=_outcome(record),
        elapsed_seconds=_elapsed_seconds(record),
        summary=_summary_for(record),
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
        invocation_path=artifacts.get("invocation_path"),
        exit_code_path=artifacts.get("exit_code_path"),
        stdout_tail_path=artifacts.get("stdout_tail_path"),
        stderr_tail_path=artifacts.get("stderr_tail_path"),
        session=WorkerSessionInfo(
            log_path=str(stdout_path), provider_session_id=record.session_id
        ),
        resume=record.resume or resume_info_for_agent_type(record.agent_type),
        pid=record.pid,
        pgid=record.pgid,
        starttime=record.starttime,
    )


def build_worker_sessions_manifest(
    *,
    run_id: str,
    session_output_dir: str | Path,
    agents: list[AgentRecord],
    generated_at: datetime | None = None,
    artifact_paths_by_agent: dict[str, dict[str, str | None]] | None = None,
) -> WorkerSessionsManifest:
    session_dir = Path(session_output_dir).resolve()
    created_at = generated_at or datetime.now(timezone.utc)
    workers = [
        _build_worker_session_record(
            record=record, artifact_paths=(artifact_paths_by_agent or {}).get(record.id)
        )
        for record in agents
    ]
    return WorkerSessionsManifest(
        run_id=run_id,
        generated_at=created_at,
        session_output_dir=str(session_dir),
        workers=workers,
    )


def write_worker_sessions_manifest(
    *,
    run_id: str,
    session_output_dir: str | Path,
    agents: list[AgentRecord],
    generated_at: datetime | None = None,
) -> Path:
    """Write the compact worker/session index and its referenced artifacts."""

    session_dir = Path(session_output_dir).resolve()
    artifact_paths_by_agent: dict[str, dict[str, str | None]] = {}
    for record in agents:
        stdout_tail = _tail_text(Path(record.stdout_log).resolve())
        stderr_tail = _tail_text(Path(record.stderr_log).resolve())
        artifact_paths_by_agent[record.id] = _write_worker_artifacts(
            session_dir=session_dir,
            record=record,
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
        )
    manifest = build_worker_sessions_manifest(
        run_id=run_id,
        session_output_dir=session_dir,
        agents=agents,
        generated_at=generated_at,
        artifact_paths_by_agent=artifact_paths_by_agent,
    )
    output_path = session_dir / "worker_sessions.json"
    write_json_atomic(path=output_path, payload=manifest.model_dump(mode="json"))
    return output_path


def build_worker_failure_detail(
    *, summary: str, agents: list[AgentRecord], session_output_dir: str | Path
) -> dict[str, Any] | None:
    """Build structured caller evidence for the most relevant worker failure."""

    session_dir = Path(session_output_dir).resolve()
    worker_sessions_path = str((session_dir / "worker_sessions.json").resolve())
    salvaged_workers = [
        salvaged
        for record in agents
        if (salvaged := _build_salvaged_worker(record)) is not None
    ]
    records = [
        record
        for record in agents
        if _outcome(record)
        in {"failed_before_session", "failed_after_session", "killed"}
    ]
    if not records:
        if not agents:
            return None
        return {
            "summary": summary,
            "worker_sessions_path": worker_sessions_path,
            "salvaged_workers": salvaged_workers,
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }
    record = max(records, key=lambda item: item.finished_at or item.spawned_at)
    stdout_path = Path(record.stdout_log).resolve()
    stderr_path = Path(record.stderr_log).resolve()
    return {
        "summary": summary,
        "agent_summary": _summary_for(record),
        "agent_id": record.id,
        "agent_type": record.agent_type,
        "status": record.status,
        "outcome": _outcome(record),
        "exit_code": record.exit_code,
        "elapsed_seconds": _elapsed_seconds(record),
        "cwd": record.cwd,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "stdout_tail": _tail_text(stdout_path),
        "stderr_tail": _tail_text(stderr_path),
        "salvaged_workers": salvaged_workers,
        "invocation": _recorded_command(command=record.command),
        "invocation_path": _artifact_paths(session_dir, record)["invocation_path"],
        "worker_sessions_path": worker_sessions_path,
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }
