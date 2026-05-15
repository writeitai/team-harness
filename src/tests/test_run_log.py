# pyright: reportMissingParameterType=false

from datetime import datetime
from datetime import timezone
import json

from team_harness.tracking.models import AgentRecord
from team_harness.tracking.models import CoordinatorRetryRecord
from team_harness.tracking.models import RunFailureRecord
from team_harness.tracking.models import ToolCallRecord
from team_harness.tracking.run_log import RunLogWriter


def test_run_log_delta_replay_and_agent_update(tmp_path):
    writer = RunLogWriter(
        run_id="run_1",
        run_dir=tmp_path,
        provider="openai_compat",
        model="model",
        api_base="base",
    )
    writer.record_turn_delta(
        index=0,
        messages_appended_delta=[
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u"},
        ],
        response_text=None,
        usage={},
    )
    writer.record_agent_spawn(
        AgentRecord(
            id="agent_1",
            agent_type="codex",
            cwd=".",
            prompt="p",
            full_prompt="p\nx",
            command=["echo"],
            spawned_at=datetime.now(timezone.utc),
            stdout_log="out",
            stderr_log="err",
        )
    )
    writer.record_turn_delta(
        index=1,
        messages_appended_delta=[
            {"role": "assistant", "content": "a"},
            {"role": "tool", "tool_call_id": "1", "content": "ok"},
        ],
        response_text="a",
        usage={"prompt_tokens": 1, "completion_tokens": 1},
        tool_calls=[
            ToolCallRecord(
                name="spawn_agent", arguments={"type": "codex"}, result="agent_1"
            ),
            ToolCallRecord(name="tool", arguments={}, result="ok"),
        ],
    )
    finished_at = datetime.now(timezone.utc)
    writer.update_agent("agent_1", exit_code=0, finished_at=finished_at, status="done")
    writer.finalize(error="boom")
    writer.finalize()
    data = json.loads((tmp_path / "run.json").read_text())
    replay = []
    for turn in data["turns"]:
        replay.extend(turn["messages_appended_delta"])
    assert replay == [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "a"},
        {"role": "tool", "tool_call_id": "1", "content": "ok"},
    ]
    assert data["agents"][0]["status"] == "done"
    assert data["agents"][0]["coordinator_turn_index"] == 1
    assert data["error"] == "boom"
    assert data["provider"] == "openai_compat"


def test_snapshot_agents_returns_copy(tmp_path):
    writer = RunLogWriter(
        run_id="run_1",
        run_dir=tmp_path,
        provider="openai_compat",
        model="model",
        api_base="base",
    )
    writer.record_agent_spawn(
        AgentRecord(
            id="agent_1",
            agent_type="codex",
            cwd=".",
            prompt="p",
            full_prompt="p\nx",
            command=["echo"],
            spawned_at=datetime.now(timezone.utc),
            stdout_log="out",
            stderr_log="err",
        )
    )

    snapshot = writer.snapshot_agents()
    snapshot[0].status = "tampered"

    data = json.loads((tmp_path / "run.json").read_text())
    assert data["agents"][0]["status"] == "running"


def test_run_log_records_coordinator_retries_and_failure(tmp_path):
    writer = RunLogWriter(
        run_id="run_1",
        run_dir=tmp_path,
        provider="codex",
        model="gpt-5.5",
        api_base="https://chatgpt.com/backend-api/codex/responses",
    )
    writer.record_coordinator_retry(
        CoordinatorRetryRecord(
            attempt=1,
            max_retries=5,
            will_retry=True,
            sleep_seconds=1.0,
            provider="codex",
            model="gpt-5.5",
            api_base="https://chatgpt.com/backend-api/codex/responses",
            host="chatgpt.com",
            error_type="CoordinatorAPIError",
            cause_type="ConnectError",
            status_code=None,
            retryable=True,
            message="dns failed",
            recorded_at=datetime.now(timezone.utc),
        )
    )
    writer.finalize(
        error="dns failed",
        failure=RunFailureRecord(
            kind="coordinator_api",
            message="dns failed",
            provider="codex",
            model="gpt-5.5",
            api_base="https://chatgpt.com/backend-api/codex/responses",
            host="chatgpt.com",
            error_type="CoordinatorAPIError",
            cause_type="ConnectError",
            retryable=True,
            retry_attempts=1,
            max_retries=5,
        ),
    )

    data = json.loads((tmp_path / "run.json").read_text())
    assert data["error"] == "dns failed"
    assert data["failure"]["kind"] == "coordinator_api"
    assert data["failure"]["host"] == "chatgpt.com"
    assert data["coordinator_retries"][0]["attempt"] == 1
    assert data["coordinator_retries"][0]["will_retry"] is True
    assert writer.snapshot_failure().kind == "coordinator_api"
