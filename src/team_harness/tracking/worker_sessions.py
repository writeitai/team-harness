from __future__ import annotations

from datetime import datetime
from datetime import timezone
import json
from pathlib import Path

from team_harness.tracking.models import AgentRecord
from team_harness.tracking.models import WorkerResumeInfo
from team_harness.tracking.models import WorkerSessionInfo
from team_harness.tracking.models import WorkerSessionRecord
from team_harness.tracking.models import WorkerSessionsManifest


def resume_info_for_agent_type(agent_type: str) -> WorkerResumeInfo:
    if agent_type == "codex":
        return WorkerResumeInfo(supported=True, preferred_mode="resume")
    if agent_type == "gemini":
        return WorkerResumeInfo(supported=True, preferred_mode="resume")
    if agent_type == "claude":
        return WorkerResumeInfo(supported=True, preferred_mode="resume")
    if agent_type == "opencode":
        return WorkerResumeInfo(supported=True, preferred_mode="continue")
    return WorkerResumeInfo(supported=False, preferred_mode=None)


def build_worker_sessions_manifest(
    *,
    run_id: str,
    session_output_dir: str | Path,
    agents: list[AgentRecord],
    generated_at: datetime | None = None,
) -> WorkerSessionsManifest:
    session_dir = Path(session_output_dir).resolve()
    created_at = generated_at or datetime.now(timezone.utc)
    workers = [
        WorkerSessionRecord(
            agent_id=record.id,
            agent_type=record.agent_type,
            coordinator_turn_index=record.coordinator_turn_index,
            prompt=record.prompt,
            status=record.status,
            exit_code=record.exit_code,
            cwd=record.cwd,
            spawned_at=record.spawned_at,
            finished_at=record.finished_at,
            stdout_path=str(Path(record.stdout_log).resolve()),
            stderr_path=str(Path(record.stderr_log).resolve()),
            session=WorkerSessionInfo(
                log_path=str(Path(record.stdout_log).resolve()),
                provider_session_id=record.session_id,
            ),
            resume=record.resume or resume_info_for_agent_type(record.agent_type),
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
    manifest = build_worker_sessions_manifest(
        run_id=run_id,
        session_output_dir=session_output_dir,
        agents=agents,
        generated_at=generated_at,
    )
    output_path = Path(session_output_dir).resolve() / "worker_sessions.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(manifest.model_dump(mode="json"), indent=2))
    return output_path
