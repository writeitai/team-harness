from __future__ import annotations

from datetime import datetime
from datetime import timezone
import json
from pathlib import Path
import re
from typing import Any

from team_harness.tracking.models import AgentRecord
from team_harness.tracking.models import WorkerResumeInfo
from team_harness.tracking.models import WorkerSessionInfo
from team_harness.tracking.models import WorkerSessionRecord
from team_harness.tracking.models import WorkerSessionsManifest

_TAIL_CHARS = 4096
_SECRET_RE = re.compile(
    r"(?i)(sk-[a-z0-9_-]{8,}|[a-z0-9_-]{20,}\.[a-z0-9_-]{20,}\.[a-z0-9_-]{20,})"
)


def resume_info_for_agent_type(agent_type: str) -> WorkerResumeInfo:
    if agent_type == "codex":
        return WorkerResumeInfo(supported=True, preferred_mode="resume")
    if agent_type == "gemini":
        return WorkerResumeInfo(supported=True, preferred_mode="resume")
    if agent_type == "claude":
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
    if record.status == "killed":
        return "killed"
    if record.exit_code == 0:
        return "succeeded"
    if record.exit_code is None:
        return "running"
    if record.session_id:
        return "failed_after_session"
    return "failed_before_session"


def _summary_for(record: AgentRecord) -> str | None:
    outcome = _outcome(record)
    if outcome == "failed_before_session":
        return "Worker exited before a provider session was captured."
    if outcome == "failed_after_session":
        return "Worker exited after a provider session was captured."
    if outcome == "killed":
        return "Worker was killed by team-harness."
    return None


def _redact(value: str) -> str:
    return _SECRET_RE.sub("[REDACTED]", value)


def _redacted_command(command: list[str]) -> list[str]:
    redacted: list[str] = []
    redact_next = False
    secret_flags = {"--api-key", "--apikey", "--token", "--auth-token", "--password"}
    for arg in command:
        if redact_next:
            redacted.append("[REDACTED]")
            redact_next = False
            continue
        if arg in secret_flags:
            redacted.append(arg)
            redact_next = True
            continue
        if any(arg.startswith(f"{flag}=") for flag in secret_flags):
            name, _, _ = arg.partition("=")
            redacted.append(f"{name}=[REDACTED]")
            continue
        redacted.append(_redact(arg))
    return redacted


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
    paths = _artifact_paths(session_dir, record)
    invocation_path = Path(paths["invocation_path"] or "")
    exit_code_path = Path(paths["exit_code_path"] or "")
    stdout_tail_path = Path(paths["stdout_tail_path"] or "")
    stderr_tail_path = Path(paths["stderr_tail_path"] or "")
    invocation_path.parent.mkdir(parents=True, exist_ok=True)
    invocation_path.write_text(
        json.dumps(
            {
                "agent_id": record.id,
                "agent_type": record.agent_type,
                "cwd": record.cwd,
                "command": _redacted_command(record.command),
                "spawned_at": record.spawned_at.isoformat(),
                "finished_at": record.finished_at.isoformat()
                if record.finished_at
                else None,
                "status": record.status,
                "exit_code": record.exit_code,
                "session_id": record.session_id,
            },
            indent=2,
        )
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
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(manifest.model_dump(mode="json"), indent=2))
    return output_path


def build_worker_failure_detail(
    *, summary: str, agents: list[AgentRecord], session_output_dir: str | Path
) -> dict[str, Any] | None:
    session_dir = Path(session_output_dir).resolve()
    records = [
        record
        for record in agents
        if _outcome(record)
        in {"failed_before_session", "failed_after_session", "killed"}
    ]
    if not records:
        return None
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
        "invocation": _redacted_command(record.command),
        "invocation_path": _artifact_paths(session_dir, record)["invocation_path"],
        "worker_sessions_path": str((session_dir / "worker_sessions.json").resolve()),
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }
