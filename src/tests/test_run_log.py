# pyright: reportMissingParameterType=false

from datetime import datetime
from datetime import timezone
import json

from team_harness.tracking.models import AgentRecord
from team_harness.tracking.models import ToolCallRecord
from team_harness.tracking.run_log import RunLogWriter


def test_run_log_delta_replay_and_agent_update(tmp_path):
    writer = RunLogWriter("run_1", tmp_path, "model", "base")
    writer.record_turn_delta(
        index=0,
        messages_appended_delta=[
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u"},
        ],
        response_text=None,
        usage={},
    )
    writer.record_turn_delta(
        index=1,
        messages_appended_delta=[
            {"role": "assistant", "content": "a"},
            {"role": "tool", "tool_call_id": "1", "content": "ok"},
        ],
        response_text="a",
        usage={"prompt_tokens": 1, "completion_tokens": 1},
        tool_calls=[ToolCallRecord(name="tool", arguments={}, result="ok")],
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
    assert data["error"] == "boom"
