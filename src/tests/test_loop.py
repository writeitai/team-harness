# pyright: reportMissingParameterType=false, reportArgumentType=false


from unittest.mock import Mock

import pytest

from team_harness.coordinator.loop import _chat_with_retry
from team_harness.coordinator.loop import run
from team_harness.coordinator.loop import run_one_turn
from team_harness.tools.registry import ToolRegistry
from team_harness.tracking.run_log import RunLogWriter
from tests.helpers import make_api_error
from tests.helpers import make_response
from tests.helpers import SequenceClient


@pytest.mark.asyncio
async def test_run_one_turn_executes_tool_calls_first(tmp_path, config, ctx, ui):
    run_log = RunLogWriter("run_1", tmp_path, config.model, config.api_base)
    registry = ToolRegistry()
    called = []

    async def sample_tool(value: str) -> str:
        called.append(value)
        return f"ok:{value}"

    registry.register(
        {
            "type": "function",
            "function": {
                "name": "sample",
                "description": "sample",
                "parameters": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                },
            },
        },
        sample_tool,
    )
    client = SequenceClient(
        [
            make_response(
                content="tool time",
                tool_calls=[
                    ("sample", {"value": "a"}, "1"),
                    ("sample", {"value": "b"}, "2"),
                    ("sample", {"value": "c"}, "3"),
                ],
                finish_reason="stop",
            )
        ]
    )
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    should_continue, last_logged = await run_one_turn(
        messages, config, run_log, ui, registry, client, ctx, 0, 0
    )
    assert should_continue is True
    assert last_logged == 6
    assert called == ["a", "b", "c"]
    assert len(messages) == 6
    assert ui.turns == [0]


@pytest.mark.asyncio
async def test_run_one_turn_handles_empty_and_tool_errors(tmp_path, config, ctx, ui):
    run_log = RunLogWriter("run_1", tmp_path, config.model, config.api_base)
    registry = ToolRegistry()

    async def bad_tool() -> str:
        raise RuntimeError("broken")

    registry.register(
        {
            "type": "function",
            "function": {
                "name": "bad",
                "description": "bad",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        bad_tool,
    )
    client = SequenceClient(
        [
            make_response(tool_calls=[("bad", {}, "1")], finish_reason="stop"),
            make_response(content="", tool_calls=None, finish_reason="stop"),
        ]
    )
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    assert (
        await run_one_turn(messages, config, run_log, ui, registry, client, ctx, 0, 0)
    )[0] is True
    result = ui.tool_calls[0][2].results[0]
    assert result[1] is True
    should_continue, _ = await run_one_turn(
        messages, config, run_log, ui, registry, client, ctx, 1, len(messages)
    )
    assert should_continue is False
    assert "WARNING: coordinator returned empty response" in ui.messages


@pytest.mark.asyncio
async def test_chat_with_retry_and_run_api_errors(
    tmp_path, config, ctx, ui, monkeypatch
):
    sleeps = []

    async def fake_sleep(delay: int) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("asyncio.sleep", fake_sleep)
    ok_response = make_response(content="done")
    client = SequenceClient([make_api_error(429), make_api_error(429), ok_response])
    response = await _chat_with_retry(client, [], [], config, token_callback=None)
    assert response.choices[0].message.content == "done"
    assert sleeps == [1, 2]

    error_client = SequenceClient([make_api_error(400)])
    run_log = RunLogWriter("run_2", tmp_path, config.model, config.api_base)
    registry = ToolRegistry()
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    should_continue, _ = await run_one_turn(
        messages, config, run_log, ui, registry, error_client, ctx, 0, 0
    )
    assert should_continue is False
    assert any("API error 400" in message for message in ui.messages)


@pytest.mark.asyncio
async def test_run_one_turn_stops_cleanly_when_retries_are_exhausted(
    tmp_path, config, ctx, ui
):
    config.max_retries = 0
    run_log = RunLogWriter("run_3", tmp_path, config.model, config.api_base)
    registry = ToolRegistry()
    client = SequenceClient([make_api_error(429)])
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]

    should_continue, last_logged = await run_one_turn(
        messages, config, run_log, ui, registry, client, ctx, 0, 0
    )

    assert should_continue is False
    assert last_logged == 0
    assert any("API error (retries exhausted)" in message for message in ui.messages)


@pytest.mark.asyncio
async def test_run_one_turn_emits_context_warning_only_once(
    tmp_path, config, ctx, ui, monkeypatch
):
    run_log = RunLogWriter("run_4", tmp_path, config.model, config.api_base)
    registry = ToolRegistry()
    client = SequenceClient(
        [make_response(content="first"), make_response(content="second")]
    )
    warning_spy = Mock(wraps=ui.context_warning)
    monkeypatch.setattr(ui, "context_warning", warning_spy)
    ctx.prompt_tokens = 80
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]

    should_continue, last_logged = await run_one_turn(
        messages, config, run_log, ui, registry, client, ctx, 0, 0
    )
    assert should_continue is False

    messages.append({"role": "user", "content": "again"})
    should_continue, _ = await run_one_turn(
        messages, config, run_log, ui, registry, client, ctx, 1, last_logged
    )

    assert should_continue is False
    assert warning_spy.call_count == 1


@pytest.mark.asyncio
async def test_run_respects_max_turns(tmp_path, config, ctx, ui):
    config.max_turns = 2
    run_log = RunLogWriter("run_1", tmp_path, config.model, config.api_base)
    registry = ToolRegistry()

    async def noop() -> str:
        return "ok"

    registry.register(
        {
            "type": "function",
            "function": {
                "name": "noop",
                "description": "noop",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        noop,
    )
    client = SequenceClient(
        [
            make_response(tool_calls=[("noop", {}, "1")]),
            make_response(tool_calls=[("noop", {}, "2")]),
            make_response(tool_calls=[("noop", {}, "3")]),
        ]
    )
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    await run(messages, config, run_log, ui, registry, client, ctx)
    assert any("Max turns (2) reached" in message for message in ui.messages)
