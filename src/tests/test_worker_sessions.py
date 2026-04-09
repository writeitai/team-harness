# pyright: reportMissingParameterType=false

from datetime import datetime
from datetime import timezone
import json

from team_harness.tracking.models import AgentRecord
from team_harness.tracking.models import WorkerResumeInfo
from team_harness.tracking.worker_sessions import build_worker_sessions_manifest
from team_harness.tracking.worker_sessions import write_worker_sessions_manifest


def test_build_worker_sessions_manifest_shape_and_absolute_paths(tmp_path):
    generated_at = datetime(2026, 4, 9, 12, 5, tzinfo=timezone.utc)
    stdout_log = tmp_path / "logs" / "agent_stdout.log"
    stderr_log = tmp_path / "logs" / "agent_stderr.log"

    manifest = build_worker_sessions_manifest(
        run_id="run_1",
        session_output_dir=tmp_path / "outputs" / "run_1",
        agents=[
            AgentRecord(
                id="agent_1",
                agent_type="codex",
                coordinator_turn_index=2,
                cwd=str(tmp_path),
                prompt="Fix the bug",
                full_prompt="Fix the bug\n\nfooter",
                command=["codex"],
                spawned_at=datetime(2026, 4, 9, 12, 0, tzinfo=timezone.utc),
                finished_at=datetime(2026, 4, 9, 12, 3, tzinfo=timezone.utc),
                exit_code=0,
                status="done",
                stdout_log=str(stdout_log),
                stderr_log=str(stderr_log),
                resume=WorkerResumeInfo(supported=True, preferred_mode="resume"),
            )
        ],
        generated_at=generated_at,
    )

    payload = manifest.model_dump(mode="json")
    assert payload["schema_version"] == 1
    assert payload["run_id"] == "run_1"
    assert payload["generated_at"] == "2026-04-09T12:05:00Z"
    assert payload["session_output_dir"] == str(
        (tmp_path / "outputs" / "run_1").resolve()
    )
    assert payload["workers"] == [
        {
            "agent_id": "agent_1",
            "agent_type": "codex",
            "coordinator_turn_index": 2,
            "prompt": "Fix the bug",
            "status": "done",
            "exit_code": 0,
            "cwd": str(tmp_path),
            "spawned_at": "2026-04-09T12:00:00Z",
            "finished_at": "2026-04-09T12:03:00Z",
            "stdout_path": str(stdout_log.resolve()),
            "stderr_path": str(stderr_log.resolve()),
            "resume": {
                "supported": True,
                "preferred_mode": "resume",
                "provider_session_id": None,
                "provider_session_path": None,
            },
        }
    ]


def test_write_worker_sessions_manifest_handles_zero_workers_and_is_repeatable(
    tmp_path,
):
    generated_at = datetime(2026, 4, 9, 12, 5, tzinfo=timezone.utc)

    first = write_worker_sessions_manifest(
        run_id="run_1",
        session_output_dir=tmp_path,
        agents=[],
        generated_at=generated_at,
    )
    second = write_worker_sessions_manifest(
        run_id="run_1",
        session_output_dir=tmp_path,
        agents=[],
        generated_at=generated_at,
    )

    assert first == second
    payload = json.loads(first.read_text())
    assert payload["schema_version"] == 1
    assert payload["workers"] == []


def test_custom_agent_types_default_to_no_resume_support(tmp_path):
    manifest = build_worker_sessions_manifest(
        run_id="run_1",
        session_output_dir=tmp_path,
        agents=[
            AgentRecord(
                id="agent_custom",
                agent_type="myagent",
                cwd=str(tmp_path),
                prompt="Do a thing",
                full_prompt="Do a thing\n\nfooter",
                command=["myagent"],
                spawned_at=datetime.now(timezone.utc),
                stdout_log=str(tmp_path / "custom_stdout.log"),
                stderr_log=str(tmp_path / "custom_stderr.log"),
            )
        ],
    )

    worker = manifest.workers[0]
    assert worker.agent_type == "myagent"
    assert worker.resume.supported is False
    assert worker.resume.preferred_mode is None
