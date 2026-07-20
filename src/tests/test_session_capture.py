# pyright: reportMissingParameterType=false

import asyncio
import json

import pytest

from team_harness.agents.session_capture import capture_session_id_from_path
from team_harness.agents.session_capture import extract_session_id
from team_harness.agents.template import AgentTemplate
from team_harness.agents.template import DEFAULT_AGENT_TEMPLATES
from team_harness.agents.template import SessionCapture


def test_extract_session_id_matches_codex_thread_started():
    payload = (
        json.dumps({"type": "item.completed", "session_id": "wrong"})
        + "\n"
        + json.dumps({"type": "thread.started", "thread_id": "codex-thread-1"})
        + "\n"
    ).encode()

    assert (
        extract_session_id(DEFAULT_AGENT_TEMPLATES["codex"], payload, None)
        == "codex-thread-1"
    )


def test_extract_session_id_uses_claude_result_fallback():
    payload = (
        json.dumps({"type": "assistant", "message": {"content": []}})
        + "\n"
        + json.dumps(
            {"type": "result", "subtype": "success", "session_id": "claude-session-1"}
        )
        + "\n"
    ).encode()

    assert (
        extract_session_id(DEFAULT_AGENT_TEMPLATES["claude"], payload, None)
        == "claude-session-1"
    )


def test_extract_session_id_matches_grok_end_event():
    payload = (
        json.dumps({"type": "thought", "data": "thinking"})
        + "\n"
        + json.dumps({"type": "text", "data": "hello"})
        + "\n"
        + json.dumps(
            {
                "type": "end",
                "stopReason": "EndTurn",
                "sessionId": "019f7ed2-e43d-76b1-bfad-91b125272638",
            }
        )
        + "\n"
    ).encode()

    assert (
        extract_session_id(DEFAULT_AGENT_TEMPLATES["grok"], payload, None)
        == "019f7ed2-e43d-76b1-bfad-91b125272638"
    )


def test_claude_fallback_is_limited_to_claude_templates():
    template = AgentTemplate(
        command=("not-claude",),
        session_capture=SessionCapture(
            strategy="stream_json_event",
            match={"type": "system", "subtype": "init"},
            field_path=("session_id",),
        ),
    )
    payload = (
        json.dumps({"type": "result", "session_id": "should-not-match"}) + "\n"
    ).encode()

    assert extract_session_id(template, payload, None) is None


@pytest.mark.asyncio
async def test_capture_session_id_reads_tail_after_stop_event(tmp_path):
    stdout_path = tmp_path / "worker.log"
    stdout_path.write_bytes(
        b"x" * 256
        + b"\n"
        + json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "session_id": "claude-tail-session",
            }
        ).encode()
        + b"\n"
    )
    stop_event = asyncio.Event()
    stop_event.set()

    assert (
        await capture_session_id_from_path(
            stdout_path=stdout_path,
            template=DEFAULT_AGENT_TEMPLATES["claude"],
            pre_generated_uuid=None,
            stop_event=stop_event,
            max_bytes=128,
            max_wait_s=1,
        )
        == "claude-tail-session"
    )


@pytest.mark.asyncio
async def test_capture_session_id_waits_for_final_claude_result(tmp_path):
    stdout_path = tmp_path / "worker.log"
    stdout_path.write_text(
        json.dumps({"type": "assistant", "message": {"content": []}}) + "\n"
    )
    stop_event = asyncio.Event()

    async def finish_worker_log() -> None:
        await asyncio.sleep(0.02)
        with stdout_path.open("ab") as handle:
            handle.write(b"x" * 256)
            handle.write(b"\n")
            handle.write(
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "success",
                        "is_error": False,
                        "session_id": "claude-final-session",
                    }
                ).encode()
            )
            handle.write(b"\n")
        stop_event.set()

    writer = asyncio.create_task(finish_worker_log())
    try:
        assert (
            await capture_session_id_from_path(
                stdout_path=stdout_path,
                template=DEFAULT_AGENT_TEMPLATES["claude"],
                pre_generated_uuid=None,
                stop_event=stop_event,
                max_bytes=128,
                max_wait_s=1,
                poll_interval_s=0.01,
            )
            == "claude-final-session"
        )
    finally:
        await writer


@pytest.mark.asyncio
async def test_capture_session_id_reads_grok_end_from_final_tail(tmp_path):
    stdout_path = tmp_path / "worker.log"
    stdout_path.write_bytes(
        b"x" * 256
        + b"\n"
        + json.dumps({"type": "text", "data": "partial"}).encode()
        + b"\n"
        + json.dumps(
            {"type": "end", "stopReason": "EndTurn", "sessionId": "grok-tail-session"}
        ).encode()
        + b"\n"
    )
    stop_event = asyncio.Event()
    stop_event.set()

    assert (
        await capture_session_id_from_path(
            stdout_path=stdout_path,
            template=DEFAULT_AGENT_TEMPLATES["grok"],
            pre_generated_uuid=None,
            stop_event=stop_event,
            max_bytes=128,
            max_wait_s=1,
        )
        == "grok-tail-session"
    )


@pytest.mark.asyncio
async def test_capture_session_id_waits_for_final_grok_end(tmp_path):
    stdout_path = tmp_path / "worker.log"
    stdout_path.write_text(json.dumps({"type": "thought", "data": "start"}) + "\n")
    stop_event = asyncio.Event()

    async def finish_worker_log() -> None:
        await asyncio.sleep(0.02)
        with stdout_path.open("ab") as handle:
            handle.write(b"x" * 256)
            handle.write(b"\n")
            handle.write(
                json.dumps(
                    {
                        "type": "end",
                        "stopReason": "EndTurn",
                        "sessionId": "grok-final-session",
                    }
                ).encode()
            )
            handle.write(b"\n")
        stop_event.set()

    writer = asyncio.create_task(finish_worker_log())
    try:
        assert (
            await capture_session_id_from_path(
                stdout_path=stdout_path,
                template=DEFAULT_AGENT_TEMPLATES["grok"],
                pre_generated_uuid=None,
                stop_event=stop_event,
                max_bytes=128,
                max_wait_s=1,
                poll_interval_s=0.01,
            )
            == "grok-final-session"
        )
    finally:
        await writer
