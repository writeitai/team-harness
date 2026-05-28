# pyright: reportMissingParameterType=false

from datetime import datetime
from datetime import timezone
import json

from team_harness.tracking.models import AgentRecord
from team_harness.tracking.models import WorkerResumeInfo
from team_harness.tracking.worker_sessions import build_worker_failure_detail
from team_harness.tracking.worker_sessions import build_worker_sessions_manifest
from team_harness.tracking.worker_sessions import resume_info_for_agent_type
from team_harness.tracking.worker_sessions import write_worker_sessions_manifest


def test_build_worker_sessions_manifest_shape_and_absolute_paths(tmp_path):
    generated_at = datetime(2026, 4, 9, 12, 5, tzinfo=timezone.utc)
    stdout_log = tmp_path / "logs" / "agent_stdout.log"
    stderr_log = tmp_path / "logs" / "agent_stderr.log"
    stdout_log.parent.mkdir()
    stdout_log.write_text("worker output")
    stderr_log.write_text("worker warning")

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
    assert payload["schema_version"] == 2
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
            "outcome": "succeeded",
            "elapsed_seconds": 180.0,
            "summary": None,
            "stdout_tail": "worker output",
            "stderr_tail": "worker warning",
            "invocation_path": None,
            "exit_code_path": None,
            "stdout_tail_path": None,
            "stderr_tail_path": None,
            "session": {
                "log_path": str(stdout_log.resolve()),
                "provider_session_id": None,
                "provider_session_path": None,
            },
            "resume": {"supported": True, "preferred_mode": "resume"},
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
    assert payload["schema_version"] == 2
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
    assert worker.session.log_path == str((tmp_path / "custom_stdout.log").resolve())
    assert worker.session.provider_session_id is None
    assert worker.resume.supported is False
    assert worker.resume.preferred_mode is None


def test_antigravity_resume_info_prefers_resume_mode():
    resume = resume_info_for_agent_type("antigravity")

    assert resume.supported is True
    assert resume.preferred_mode == "resume"


def test_write_worker_sessions_manifest_records_failed_before_session_diagnostics(
    tmp_path,
):
    stdout_log = tmp_path / "logs" / "agent_stdout.log"
    stderr_log = tmp_path / "logs" / "agent_stderr.log"
    stdout_log.parent.mkdir()
    stdout_log.write_text("stream-json fragment")
    stderr_log.write_text("TEST: synthetic auth failure")
    record = AgentRecord(
        id="agent_failed",
        agent_type="codex",
        cwd=str(tmp_path),
        prompt="Do a thing",
        full_prompt="Do a thing\n\nfooter",
        command=["codex", "exec", "--api-key", "sk-secret123456"],
        spawned_at=datetime(2026, 4, 9, 12, 0, tzinfo=timezone.utc),
        finished_at=datetime(2026, 4, 9, 12, 0, 7, tzinfo=timezone.utc),
        exit_code=7,
        status="failed",
        stdout_log=str(stdout_log),
        stderr_log=str(stderr_log),
    )

    manifest_path = write_worker_sessions_manifest(
        run_id="run_1",
        session_output_dir=tmp_path / "outputs" / "run_1",
        agents=[record],
        generated_at=datetime(2026, 4, 9, 12, 5, tzinfo=timezone.utc),
    )

    payload = json.loads(manifest_path.read_text())
    worker = payload["workers"][0]
    assert worker["outcome"] == "failed_before_session"
    assert worker["exit_code"] == 7
    assert worker["elapsed_seconds"] == 7.0
    assert worker["stderr_tail"] == "TEST: synthetic auth failure"
    assert worker["stdout_tail"] == "stream-json fragment"
    assert "workers/agent_failed_invocation.json" in worker["invocation_path"]
    assert "workers/agent_failed_exit_code.txt" in worker["exit_code_path"]
    assert "workers/agent_failed_stderr_tail.log" in worker["stderr_tail_path"]
    assert (
        "7\n"
        == (
            tmp_path / "outputs" / "run_1" / "workers" / "agent_failed_exit_code.txt"
        ).read_text()
    )
    invocation = json.loads(
        (
            tmp_path / "outputs" / "run_1" / "workers" / "agent_failed_invocation.json"
        ).read_text()
    )
    assert invocation["command"] == ["codex", "exec", "--api-key", "[REDACTED]"]


def test_worker_marked_killed_with_zero_exit_is_reported_as_succeeded(tmp_path):
    stdout_log = tmp_path / "logs" / "agent_stdout.log"
    stderr_log = tmp_path / "logs" / "agent_stderr.log"
    stdout_log.parent.mkdir()
    stdout_log.write_text("completed worker output")
    stderr_log.write_text("")
    record = AgentRecord(
        id="agent_salvaged",
        agent_type="codex",
        cwd=str(tmp_path),
        prompt="Do a thing",
        full_prompt="Do a thing\n\nfooter",
        command=["codex", "exec"],
        spawned_at=datetime(2026, 4, 9, 12, 0, tzinfo=timezone.utc),
        finished_at=datetime(2026, 4, 9, 12, 58, 10, tzinfo=timezone.utc),
        exit_code=0,
        status="killed",
        stdout_log=str(stdout_log),
        stderr_log=str(stderr_log),
    )

    manifest = build_worker_sessions_manifest(
        run_id="run_1",
        session_output_dir=tmp_path / "outputs" / "run_1",
        agents=[record],
    )

    worker = manifest.workers[0]
    assert worker.status == "killed"
    assert worker.exit_code == 0
    assert worker.outcome == "succeeded"
    assert (
        worker.summary
        == "Worker completed successfully, but was marked killed during coordinator cleanup."
    )


def test_build_worker_failure_detail_uses_latest_failed_agent(tmp_path):
    stdout_log = tmp_path / "stdout.log"
    stderr_log = tmp_path / "stderr.log"
    stdout_log.write_text("partial stdout")
    stderr_log.write_text("rate limit hit")
    record = AgentRecord(
        id="agent_failed",
        agent_type="codex",
        cwd=str(tmp_path),
        prompt="Do a thing",
        full_prompt="Do a thing\n\nfooter",
        command=["codex", "exec"],
        spawned_at=datetime(2026, 4, 9, 12, 0, tzinfo=timezone.utc),
        finished_at=datetime(2026, 4, 9, 12, 1, tzinfo=timezone.utc),
        exit_code=1,
        status="failed",
        stdout_log=str(stdout_log),
        stderr_log=str(stderr_log),
    )

    detail = build_worker_failure_detail(
        summary="Codex request failed.",
        agents=[record],
        session_output_dir=tmp_path / "outputs" / "run_1",
    )

    assert detail is not None
    assert detail["summary"] == "Codex request failed."
    assert detail["outcome"] == "failed_before_session"
    assert detail["exit_code"] == 1
    assert detail["stderr_tail"] == "rate limit hit"
    assert detail["stdout_tail"] == "partial stdout"


def test_build_worker_failure_detail_includes_salvaged_worker(tmp_path):
    stdout_log = tmp_path / "stdout.log"
    stderr_log = tmp_path / "stderr.log"
    stdout_log.write_text("useful completed stdout")
    stderr_log.write_text("")
    record = AgentRecord(
        id="agent_salvaged",
        agent_type="codex",
        cwd=str(tmp_path),
        prompt="Do a thing",
        full_prompt="Do a thing\n\nfooter",
        command=["codex", "exec"],
        spawned_at=datetime(2026, 4, 9, 12, 0, tzinfo=timezone.utc),
        finished_at=datetime(2026, 4, 9, 12, 58, 10, tzinfo=timezone.utc),
        exit_code=0,
        status="killed",
        stdout_log=str(stdout_log),
        stderr_log=str(stderr_log),
    )

    detail = build_worker_failure_detail(
        summary="Codex request failed.",
        agents=[record],
        session_output_dir=tmp_path / "outputs" / "run_1",
    )

    assert detail is not None
    assert detail["summary"] == "Codex request failed."
    assert "outcome" not in detail
    assert "worker_sessions.json" in str(detail["worker_sessions_path"])
    assert detail["salvaged_workers"] == [
        {
            "agent_id": "agent_salvaged",
            "agent_type": "codex",
            "status": "killed",
            "outcome": "succeeded",
            "exit_code": 0,
            "elapsed_seconds": 3490.0,
            "cwd": str(tmp_path),
            "stdout_path": str(stdout_log.resolve()),
            "stderr_path": str(stderr_log.resolve()),
            "stdout_tail": "useful completed stdout",
            "stderr_tail": "",
            "summary": (
                "Worker completed successfully, but was marked killed during "
                "coordinator cleanup."
            ),
        }
    ]


def test_build_worker_failure_detail_keeps_failed_agent_primary_with_salvage(tmp_path):
    failed_stdout = tmp_path / "failed_stdout.log"
    failed_stderr = tmp_path / "failed_stderr.log"
    salvaged_stdout = tmp_path / "salvaged_stdout.log"
    salvaged_stderr = tmp_path / "salvaged_stderr.log"
    failed_stdout.write_text("partial stdout")
    failed_stderr.write_text("rate limit hit")
    salvaged_stdout.write_text("completed stdout")
    salvaged_stderr.write_text("")
    failed = AgentRecord(
        id="agent_failed",
        agent_type="codex",
        cwd=str(tmp_path),
        prompt="Fail",
        full_prompt="Fail\n\nfooter",
        command=["codex", "exec"],
        spawned_at=datetime(2026, 4, 9, 12, 0, tzinfo=timezone.utc),
        finished_at=datetime(2026, 4, 9, 12, 1, tzinfo=timezone.utc),
        exit_code=1,
        status="failed",
        stdout_log=str(failed_stdout),
        stderr_log=str(failed_stderr),
    )
    salvaged = AgentRecord(
        id="agent_salvaged",
        agent_type="codex",
        cwd=str(tmp_path),
        prompt="Complete",
        full_prompt="Complete\n\nfooter",
        command=["codex", "exec"],
        spawned_at=datetime(2026, 4, 9, 12, 2, tzinfo=timezone.utc),
        finished_at=datetime(2026, 4, 9, 12, 3, tzinfo=timezone.utc),
        exit_code=0,
        status="killed",
        stdout_log=str(salvaged_stdout),
        stderr_log=str(salvaged_stderr),
    )

    detail = build_worker_failure_detail(
        summary="Codex request failed.",
        agents=[failed, salvaged],
        session_output_dir=tmp_path / "outputs" / "run_1",
    )

    assert detail is not None
    assert detail["agent_id"] == "agent_failed"
    assert detail["outcome"] == "failed_before_session"
    assert detail["salvaged_workers"][0]["agent_id"] == "agent_salvaged"
    assert detail["salvaged_workers"][0]["outcome"] == "succeeded"
