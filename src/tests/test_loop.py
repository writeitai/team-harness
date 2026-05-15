# pyright: reportMissingParameterType=false, reportArgumentType=false

import copy
import json
from typing import cast
from unittest.mock import Mock

import pytest

from team_harness.coordinator.client import ChatResponse
from team_harness.coordinator.client import ChoiceRecord
from team_harness.coordinator.client import MessageRecord
from team_harness.coordinator.client import UsageRecord
from team_harness.coordinator.loop import _approximate_tokens
from team_harness.coordinator.loop import _chat_with_retry
from team_harness.coordinator.loop import _COMPACTION_BREAKER_WARNING
from team_harness.coordinator.loop import _perform_compaction
from team_harness.coordinator.loop import _perform_manual_compaction
from team_harness.coordinator.loop import _retry_sleep_seconds
from team_harness.coordinator.loop import _should_compact
from team_harness.coordinator.loop import run_one_turn
from team_harness.tools.registry import ToolRegistry
from team_harness.tracking.context import get_auto_compact_threshold
from team_harness.tracking.run_log import RunLogWriter
from tests.helpers import make_api_error
from tests.helpers import make_coordinator_api_error
from tests.helpers import make_response
from tests.helpers import SequenceClient


class RecordingClient:
    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []
        self.model = "openai/gpt-4o"
        self.api_base = "http://localhost:9999"
        self.provider = "openai_compat"

    async def chat(self, messages, tools=None, stream=False, token_callback=None):
        self.calls.append(
            {
                "messages": copy.deepcopy(messages),
                "tools": copy.deepcopy(tools),
                "stream": stream,
                "token_callback": token_callback,
            }
        )
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if stream and token_callback is not None:
            response = cast(ChatResponse, item)
            text = response.choices[0].message.content or ""
            for chunk in text:
                token_callback(chunk)
        return item

    async def get_models(self) -> dict:
        return {"data": []}

    async def aclose(self) -> None:
        return None


def make_run_log(tmp_path, config, run_id="run_1"):
    return RunLogWriter(
        run_id=run_id,
        run_dir=tmp_path,
        provider=config.provider,
        model=config.model,
        api_base=config.api_base,
    )


def compactable_messages():
    return [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "first request"},
        {
            "role": "assistant",
            "content": "tool time",
            "tool_calls": [
                {
                    "id": "call_123",
                    "type": "function",
                    "function": {"name": "sample", "arguments": '{"value":"a"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_123", "content": "tool result"},
        {"role": "assistant", "content": "after tool"},
        {"role": "user", "content": "pending user"},
    ]


@pytest.mark.asyncio
async def test_run_one_turn_executes_tool_calls_first(tmp_path, config, ctx, ui):
    run_log = make_run_log(tmp_path, config)
    registry = ToolRegistry()
    called = []

    async def sample_tool(value: str) -> str:
        called.append(value)
        return f"ok:{value}"

    registry.register(
        schema={
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
        fn=sample_tool,
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
        messages=messages,
        config=config,
        run_log=run_log,
        ui=ui,
        tool_registry=registry,
        client=client,
        ctx=ctx,
        turn_index=0,
        last_logged_index=0,
    )
    assert should_continue is True
    assert last_logged == 6
    assert called == ["a", "b", "c"]
    assert len(messages) == 6
    assert ui.turns == [0]


@pytest.mark.asyncio
async def test_run_one_turn_handles_empty_and_tool_errors(tmp_path, config, ctx, ui):
    run_log = make_run_log(tmp_path, config)
    registry = ToolRegistry()

    async def bad_tool() -> str:
        raise RuntimeError("broken")

    registry.register(
        schema={
            "type": "function",
            "function": {
                "name": "bad",
                "description": "bad",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        fn=bad_tool,
    )
    client = SequenceClient(
        [
            make_response(tool_calls=[("bad", {}, "1")], finish_reason="stop"),
            make_response(content="", tool_calls=None, finish_reason="stop"),
        ]
    )
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    assert (
        await run_one_turn(
            messages=messages,
            config=config,
            run_log=run_log,
            ui=ui,
            tool_registry=registry,
            client=client,
            ctx=ctx,
            turn_index=0,
            last_logged_index=0,
        )
    )[0] is True
    result = ui.tool_calls[0][2].results[0]
    assert result[1] is True
    should_continue, _ = await run_one_turn(
        messages=messages,
        config=config,
        run_log=run_log,
        ui=ui,
        tool_registry=registry,
        client=client,
        ctx=ctx,
        turn_index=1,
        last_logged_index=len(messages),
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
    run_log = make_run_log(tmp_path, config, run_id="run_retry")
    response = await _chat_with_retry(
        client=client,
        messages=[],
        tools=[],
        config=config,
        run_log=run_log,
        token_callback=None,
    )
    assert response.choices[0].message.content == "done"
    assert sleeps == [1, 2]
    retry_data = json.loads((tmp_path / "run.json").read_text())["coordinator_retries"]
    assert [record["attempt"] for record in retry_data] == [1, 2]
    assert retry_data[0]["host"] == "localhost"
    assert retry_data[0]["will_retry"] is True

    error_client = SequenceClient([make_api_error(400)])
    run_log = make_run_log(tmp_path, config, run_id="run_2")
    registry = ToolRegistry()
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    should_continue, _ = await run_one_turn(
        messages=messages,
        config=config,
        run_log=run_log,
        ui=ui,
        tool_registry=registry,
        client=error_client,
        ctx=ctx,
        turn_index=0,
        last_logged_index=0,
    )
    assert should_continue is False
    assert any("API error 400" in message for message in ui.messages)


def test_retry_sleep_seconds_uses_capped_exponential_backoff(config):
    config.retry_base_delay_s = 2.0
    config.retry_max_delay_s = 5.0

    assert _retry_sleep_seconds(config=config, attempt=0) == 2.0
    assert _retry_sleep_seconds(config=config, attempt=1) == 4.0
    assert _retry_sleep_seconds(config=config, attempt=2) == 5.0


@pytest.mark.asyncio
async def test_run_one_turn_stops_cleanly_when_retries_are_exhausted(
    tmp_path, config, ctx, ui
):
    config.max_retries = 0
    run_log = make_run_log(tmp_path, config, run_id="run_3")
    registry = ToolRegistry()
    client = SequenceClient([make_api_error(429)])
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]

    should_continue, last_logged = await run_one_turn(
        messages=messages,
        config=config,
        run_log=run_log,
        ui=ui,
        tool_registry=registry,
        client=client,
        ctx=ctx,
        turn_index=0,
        last_logged_index=0,
    )

    assert should_continue is False
    data = json.loads((tmp_path / "run.json").read_text())
    assert data["failure"]["kind"] == "coordinator_api"
    assert data["failure"]["retry_attempts"] == 1
    assert data["failure"]["host"] == "localhost"
    assert data["coordinator_retries"][0]["will_retry"] is False
    assert last_logged == 0
    assert any("API error (retries exhausted)" in message for message in ui.messages)


@pytest.mark.asyncio
async def test_run_one_turn_emits_context_warning_only_once(
    tmp_path, config, ctx, ui, monkeypatch
):
    run_log = make_run_log(tmp_path, config, run_id="run_4")
    registry = ToolRegistry()
    client = SequenceClient(
        [
            make_response(
                content="first",
                usage=UsageRecord(prompt_tokens=80, completion_tokens=5),
            ),
            make_response(
                content="second",
                usage=UsageRecord(prompt_tokens=80, completion_tokens=5),
            ),
        ]
    )
    warning_spy = Mock(wraps=ui.context_warning)
    monkeypatch.setattr(ui, "context_warning", warning_spy)
    ctx.model_limit = 100
    ctx.breaker_tripped = True
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]

    should_continue, last_logged = await run_one_turn(
        messages=messages,
        config=config,
        run_log=run_log,
        ui=ui,
        tool_registry=registry,
        client=client,
        ctx=ctx,
        turn_index=0,
        last_logged_index=0,
    )
    assert should_continue is False

    messages.append({"role": "user", "content": "again"})
    should_continue, _ = await run_one_turn(
        messages=messages,
        config=config,
        run_log=run_log,
        ui=ui,
        tool_registry=registry,
        client=client,
        ctx=ctx,
        turn_index=1,
        last_logged_index=last_logged,
    )

    assert should_continue is False
    assert warning_spy.call_count == 1


@pytest.mark.asyncio
async def test_missing_usage_warning_emitted_once(tmp_path, config, ctx, ui):
    run_log = make_run_log(tmp_path, config, run_id="run_missing_usage")
    registry = ToolRegistry()
    client = SequenceClient(
        [
            ChatResponse(
                choices=[
                    ChoiceRecord(
                        message=MessageRecord(content="first", tool_calls=None),
                        finish_reason="stop",
                    )
                ],
                usage=None,
            ),
            ChatResponse(
                choices=[
                    ChoiceRecord(
                        message=MessageRecord(content="second", tool_calls=None),
                        finish_reason="stop",
                    )
                ],
                usage=None,
            ),
        ]
    )
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]

    should_continue, last_logged = await run_one_turn(
        messages=messages,
        config=config,
        run_log=run_log,
        ui=ui,
        tool_registry=registry,
        client=client,
        ctx=ctx,
        turn_index=0,
        last_logged_index=0,
    )
    assert should_continue is False

    messages.append({"role": "user", "content": "again"})
    should_continue, _ = await run_one_turn(
        messages=messages,
        config=config,
        run_log=run_log,
        ui=ui,
        tool_registry=registry,
        client=client,
        ctx=ctx,
        turn_index=1,
        last_logged_index=last_logged,
    )

    assert should_continue is False
    assert ctx.usage_warning_emitted is True
    assert (
        ui.messages.count(
            "WARNING: coordinator response omitted usage; exact context tracking is unavailable for this response."
        )
        == 1
    )


@pytest.mark.asyncio
async def test_chat_with_retry_retries_coordinator_api_error(config, monkeypatch):
    sleeps = []

    async def fake_sleep(delay: int) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("asyncio.sleep", fake_sleep)
    client = SequenceClient(
        [
            make_coordinator_api_error("retry-1", status_code=429, retryable=True),
            make_coordinator_api_error("retry-2", retryable=True),
            make_response(content="done"),
        ]
    )

    response = await _chat_with_retry(
        client=client, messages=[], tools=[], config=config
    )

    assert response.choices[0].message.content == "done"
    assert sleeps == [1, 2]


@pytest.mark.asyncio
async def test_run_one_turn_handles_non_retryable_coordinator_api_error(
    tmp_path, config, ctx, ui
):
    run_log = make_run_log(tmp_path, config, run_id="run_5")
    registry = ToolRegistry()
    client = SequenceClient(
        [make_coordinator_api_error("bad auth", status_code=401, retryable=False)]
    )
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]

    should_continue, last_logged = await run_one_turn(
        messages=messages,
        config=config,
        run_log=run_log,
        ui=ui,
        tool_registry=registry,
        client=client,
        ctx=ctx,
        turn_index=0,
        last_logged_index=0,
    )

    assert should_continue is False
    assert last_logged == 0
    assert any("API error 401" in message for message in ui.messages)


@pytest.mark.asyncio
async def test_run_one_turn_triggers_compaction_before_chat(
    tmp_path, config, ctx, ui, monkeypatch
):
    run_log = make_run_log(tmp_path, config, run_id="run_6")
    registry = ToolRegistry()
    events: list[str] = []

    async def fake_perform_compaction(messages, client, ctx, ui):
        events.append("compact")
        return True

    class Client:
        model = "openai/gpt-4o"
        api_base = "http://localhost:9999"
        provider = "openai_compat"

        async def chat(self, messages, tools=None, stream=False, token_callback=None):
            events.append("chat")
            return make_response(content="done")

        async def get_models(self) -> dict:
            return {"data": []}

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(
        "team_harness.coordinator.loop._perform_compaction", fake_perform_compaction
    )
    ctx.model_id = "openai/gpt-4o"
    ctx.model_limit = 128_000
    ctx.prompt_tokens = get_auto_compact_threshold(ctx.model_id, ctx.model_limit)
    messages = compactable_messages()

    should_continue, _ = await run_one_turn(
        messages=messages,
        config=config,
        run_log=run_log,
        ui=ui,
        tool_registry=registry,
        client=Client(),
        ctx=ctx,
        turn_index=0,
        last_logged_index=0,
    )

    assert should_continue is False
    assert events == ["compact", "chat"]


@pytest.mark.asyncio
async def test_run_one_turn_skips_compaction_when_last_message_is_tool_result(
    tmp_path, config, ctx, ui, monkeypatch
):
    run_log = make_run_log(tmp_path, config, run_id="run_7")
    registry = ToolRegistry()
    compact_calls: list[int] = []

    async def fake_perform_compaction(messages, client, ctx, ui):
        compact_calls.append(1)
        return True

    monkeypatch.setattr(
        "team_harness.coordinator.loop._perform_compaction", fake_perform_compaction
    )
    ctx.model_id = "openai/gpt-4o"
    ctx.model_limit = 128_000
    ctx.prompt_tokens = get_auto_compact_threshold(ctx.model_id, ctx.model_limit)
    messages = compactable_messages()[:4]
    client = RecordingClient([make_response(content="done")])

    await run_one_turn(
        messages=messages,
        config=config,
        run_log=run_log,
        ui=ui,
        tool_registry=registry,
        client=client,
        ctx=ctx,
        turn_index=0,
        last_logged_index=0,
    )

    assert client.calls[0]["messages"][-1]["role"] == "tool"
    assert compact_calls == []


@pytest.mark.asyncio
async def test_run_one_turn_skips_compaction_when_last_message_is_assistant(
    tmp_path, config, ctx, ui, monkeypatch
):
    run_log = make_run_log(tmp_path, config, run_id="run_8")
    registry = ToolRegistry()
    compact_calls: list[int] = []

    async def fake_perform_compaction(messages, client, ctx, ui):
        compact_calls.append(1)
        return True

    monkeypatch.setattr(
        "team_harness.coordinator.loop._perform_compaction", fake_perform_compaction
    )
    ctx.model_id = "openai/gpt-4o"
    ctx.model_limit = 128_000
    ctx.prompt_tokens = get_auto_compact_threshold(ctx.model_id, ctx.model_limit)
    messages = compactable_messages()[:-1]
    messages[-1] = {"role": "assistant", "content": "assistant tail"}

    await run_one_turn(
        messages=messages,
        config=config,
        run_log=run_log,
        ui=ui,
        tool_registry=registry,
        client=SequenceClient([make_response(content="done")]),
        ctx=ctx,
        turn_index=0,
        last_logged_index=0,
    )

    assert compact_calls == []


@pytest.mark.asyncio
async def test_compaction_helper_asserts_user_tail(ctx, ui):
    messages = compactable_messages()[:-1]
    client = SequenceClient([make_response(content="summary")])

    with pytest.raises(AssertionError):
        await _perform_compaction(messages=messages, client=client, ctx=ctx, ui=ui)


@pytest.mark.asyncio
async def test_run_one_turn_uses_same_client_for_summary_and_main_reply(
    tmp_path, config, ctx, ui
):
    run_log = make_run_log(tmp_path, config, run_id="run_9")
    registry = ToolRegistry()
    client = RecordingClient(
        [
            make_response(content="summary"),
            make_response(
                content="final",
                usage=UsageRecord(prompt_tokens=11, completion_tokens=7),
            ),
        ]
    )
    ctx.model_id = "openai/gpt-4o"
    ctx.model_limit = 128_000
    ctx.prompt_tokens = get_auto_compact_threshold(ctx.model_id, ctx.model_limit)
    messages = compactable_messages()

    should_continue, _ = await run_one_turn(
        messages=messages,
        config=config,
        run_log=run_log,
        ui=ui,
        tool_registry=registry,
        client=client,
        ctx=ctx,
        turn_index=0,
        last_logged_index=0,
    )

    assert should_continue is False
    assert len(client.calls) == 2
    assert client.calls[0]["tools"] == []
    assert client.calls[0]["stream"] is False
    assert client.calls[0]["token_callback"] is None
    assert client.calls[1]["tools"] == registry.get_all_schemas()
    assert client.calls[1]["stream"] is True
    assert client.calls[1]["token_callback"].__self__ is ui


@pytest.mark.asyncio
async def test_compaction_rebuilds_messages_with_boundary_summary_and_pending_user(
    ctx, ui
):
    messages = compactable_messages()
    original_system = copy.deepcopy(messages[0])
    pending_user = copy.deepcopy(messages[-1])
    expected_messages = [
        original_system,
        {
            "role": "system",
            "content": (
                "The earlier conversation history was compacted for context management.\n"
                "Treat the next user message as an authoritative summary of the removed history.\n"
                "Continue from it without mentioning the compaction unless the user asks."
            ),
        },
        {
            "role": "user",
            "content": "Compact summary of earlier conversation:\n\nsummary body",
        },
        pending_user,
    ]
    client = SequenceClient([make_response(content="summary body")])

    compacted = await _perform_compaction(
        messages=messages, client=client, ctx=ctx, ui=ui
    )

    assert compacted is True
    assert messages == expected_messages
    assert len(messages) == 4


@pytest.mark.asyncio
async def test_compaction_preserves_original_system_prompt_exactly(ctx, ui):
    original_system = {
        "role": "system",
        "content": "  original system prompt\nwith spacing  ",
        "extra": {"mode": "strict"},
    }
    messages = [
        original_system,
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
        {"role": "user", "content": "pending"},
    ]
    client = SequenceClient([make_response(content="summary")])

    compacted = await _perform_compaction(
        messages=messages, client=client, ctx=ctx, ui=ui
    )

    assert compacted is True
    assert messages[0] == original_system


@pytest.mark.asyncio
async def test_compaction_clears_tool_call_ids(ctx, ui):
    messages = compactable_messages()
    client = SequenceClient([make_response(content="summary")])

    compacted = await _perform_compaction(
        messages=messages, client=client, ctx=ctx, ui=ui
    )

    assert compacted is True
    assert all("tool_call_id" not in message for message in messages)


@pytest.mark.asyncio
async def test_compaction_rolls_back_on_none_content(ctx, ui):
    messages = compactable_messages()
    original = copy.deepcopy(messages)
    client = SequenceClient([make_response(content=None)])

    compacted = await _perform_compaction(
        messages=messages, client=client, ctx=ctx, ui=ui
    )

    assert compacted is False
    assert messages == original


@pytest.mark.asyncio
async def test_compaction_rolls_back_on_empty_string(ctx, ui):
    messages = compactable_messages()
    original = copy.deepcopy(messages)
    client = SequenceClient([make_response(content="")])

    compacted = await _perform_compaction(
        messages=messages, client=client, ctx=ctx, ui=ui
    )

    assert compacted is False
    assert messages == original


@pytest.mark.asyncio
async def test_compaction_rolls_back_on_whitespace_only(ctx, ui):
    messages = compactable_messages()
    original = copy.deepcopy(messages)
    client = SequenceClient([make_response(content="   ")])

    compacted = await _perform_compaction(
        messages=messages, client=client, ctx=ctx, ui=ui
    )

    assert compacted is False
    assert messages == original


@pytest.mark.asyncio
async def test_end_compaction_called_on_successful_compaction(ctx, ui):
    ctx.prompt_tokens = 120
    ctx.completion_tokens = 30
    messages = compactable_messages()
    client = SequenceClient([make_response(content="summary body")])

    compacted = await _perform_compaction(
        messages=messages, client=client, ctx=ctx, ui=ui
    )

    assert compacted is True
    assert ui.compaction_end_calls == [(150, _approximate_tokens(messages))]


@pytest.mark.asyncio
async def test_end_compaction_called_on_failed_compaction(ctx, ui):
    ctx.prompt_tokens = 120
    ctx.completion_tokens = 30
    messages = compactable_messages()
    original = copy.deepcopy(messages)
    client = SequenceClient([RuntimeError("summary failed")])

    compacted = await _perform_compaction(
        messages=messages, client=client, ctx=ctx, ui=ui
    )

    assert compacted is False
    assert messages == original
    assert ui.compaction_end_calls == [(150, 150)]


@pytest.mark.asyncio
async def test_compaction_survives_begin_compaction_raise(ctx, ui):
    class RaisingBeginUI(type(ui)):
        def begin_compaction(self) -> None:
            self.compaction_begin_calls += 1
            raise RuntimeError("begin failed")

    raising_ui = RaisingBeginUI()
    messages = compactable_messages()
    client = SequenceClient([make_response(content="summary body")])

    compacted = await _perform_compaction(
        messages=messages, client=client, ctx=ctx, ui=raising_ui
    )

    assert compacted is True
    assert messages[1]["role"] == "system"
    assert messages[2]["content"] == (
        "Compact summary of earlier conversation:\n\nsummary body"
    )
    assert raising_ui.compaction_begin_calls == 1
    assert len(raising_ui.compaction_end_calls) == 1


@pytest.mark.asyncio
async def test_compaction_survives_end_compaction_raise(ctx, ui):
    class RaisingEndUI(type(ui)):
        def end_compaction(self, before_tokens: int, after_tokens: int) -> None:
            super().end_compaction(before_tokens, after_tokens)
            raise RuntimeError("end failed")

    raising_ui = RaisingEndUI()
    messages = compactable_messages()
    client = SequenceClient([make_response(content="summary body")])

    compacted = await _perform_compaction(
        messages=messages, client=client, ctx=ctx, ui=raising_ui
    )

    assert compacted is True
    assert messages[1]["role"] == "system"
    assert messages[2]["content"] == (
        "Compact summary of earlier conversation:\n\nsummary body"
    )
    assert len(raising_ui.compaction_end_calls) == 1


@pytest.mark.asyncio
async def test_manual_compaction_rebuilds_messages_with_boundary_and_summary_only(
    ctx, ui
):
    original_system = {
        "role": "system",
        "content": "  original system prompt\nwith spacing  ",
        "extra": {"mode": "strict"},
    }
    messages = [
        original_system,
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
    ]
    client = SequenceClient([make_response(content="summary body")])

    compacted = await _perform_manual_compaction(
        messages=messages, client=client, ctx=ctx, ui=ui
    )

    assert compacted is True
    assert messages == [
        original_system,
        {
            "role": "system",
            "content": (
                "The earlier conversation history was compacted for context management.\n"
                "Treat the next user message as an authoritative summary of the removed history.\n"
                "Continue from it without mentioning the compaction unless the user asks."
            ),
        },
        {
            "role": "user",
            "content": "Compact summary of earlier conversation:\n\nsummary body",
        },
    ]
    assert messages[0] is original_system


@pytest.mark.asyncio
async def test_manual_compaction_threads_focus_text_into_summary_request(ctx, ui):
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
    ]
    client = RecordingClient([make_response(content="summary body")])

    compacted = await _perform_manual_compaction(
        messages=messages, client=client, ctx=ctx, ui=ui, focus_text="focus on tests"
    )

    assert compacted is True
    request_messages = client.calls[0]["messages"]
    assert "focus on tests" not in request_messages[0]["content"]
    assert request_messages[1]["content"].endswith(
        "\n\nAdditional focus from the user:\nfocus on tests"
    )


@pytest.mark.asyncio
async def test_manual_compaction_truncates_focus_text_at_2000_characters(ctx, ui):
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
    ]
    client = RecordingClient([make_response(content="summary body")])
    focus_text = "a" * 2000 + "tail that should be removed"

    compacted = await _perform_manual_compaction(
        messages=messages, client=client, ctx=ctx, ui=ui, focus_text=focus_text
    )

    assert compacted is True
    request_content = client.calls[0]["messages"][1]["content"]
    assert (
        "Additional focus from the user:\n"
        + "a" * 2000
        + "\n[... truncated to 2000 chars ...]"
    ) in request_content
    assert "tail that should be removed" not in request_content


@pytest.mark.asyncio
async def test_manual_compaction_refuses_when_not_enough_messages(ctx, ui):
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "assistant", "content": "only one non-system message"},
    ]
    original = copy.deepcopy(messages)
    client = RecordingClient([make_response(content="summary body")])

    compacted = await _perform_manual_compaction(
        messages=messages, client=client, ctx=ctx, ui=ui
    )

    assert compacted is False
    assert messages == original
    assert client.calls == []
    assert ui.messages == [
        "Nothing to compact yet. Need at least 2 non-system messages."
    ]
    assert ui.compaction_begin_calls == 0
    assert ui.compaction_end_calls == []


@pytest.mark.asyncio
async def test_manual_compaction_accepts_between_turn_transcript_without_pending_user(
    ctx, ui
):
    messages = compactable_messages()[:4]
    client = SequenceClient([make_response(content="summary body")])

    compacted = await _perform_manual_compaction(
        messages=messages, client=client, ctx=ctx, ui=ui
    )

    assert compacted is True
    assert messages == [
        {"role": "system", "content": "system prompt"},
        {
            "role": "system",
            "content": (
                "The earlier conversation history was compacted for context management.\n"
                "Treat the next user message as an authoritative summary of the removed history.\n"
                "Continue from it without mentioning the compaction unless the user asks."
            ),
        },
        {
            "role": "user",
            "content": "Compact summary of earlier conversation:\n\nsummary body",
        },
    ]


@pytest.mark.asyncio
async def test_manual_compaction_rolls_back_on_none_content(ctx, ui):
    ctx.estimated_total_tokens = 321
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
    ]
    pre_messages = copy.deepcopy(messages)
    client = SequenceClient([make_response(content=None)])

    compacted = await _perform_manual_compaction(
        messages=messages, client=client, ctx=ctx, ui=ui
    )

    assert compacted is False
    assert messages == pre_messages
    assert ui.messages == [
        "Manual compaction failed: summarizer returned an empty response."
    ]
    assert ctx.consecutive_compact_failures == 1


@pytest.mark.asyncio
async def test_manual_compaction_rolls_back_on_empty_string(ctx, ui):
    ctx.estimated_total_tokens = 321
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
    ]
    pre_messages = copy.deepcopy(messages)
    client = SequenceClient([make_response(content="")])

    compacted = await _perform_manual_compaction(
        messages=messages, client=client, ctx=ctx, ui=ui
    )

    assert compacted is False
    assert messages == pre_messages
    assert ui.messages == [
        "Manual compaction failed: summarizer returned an empty response."
    ]
    assert ctx.consecutive_compact_failures == 1


@pytest.mark.asyncio
async def test_manual_compaction_rolls_back_on_whitespace_only(ctx, ui):
    ctx.estimated_total_tokens = 321
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
    ]
    pre_messages = copy.deepcopy(messages)
    client = SequenceClient([make_response(content="   \n\n  ")])

    compacted = await _perform_manual_compaction(
        messages=messages, client=client, ctx=ctx, ui=ui
    )

    assert compacted is False
    assert messages == pre_messages
    assert ui.messages == [
        "Manual compaction failed: summarizer returned an empty response."
    ]
    assert ctx.consecutive_compact_failures == 1


@pytest.mark.asyncio
async def test_manual_compaction_formats_coordinator_api_error(ctx, ui):
    messages_without_status = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
    ]
    pre_messages_without_status = list(messages_without_status)
    pre_estimated_without_status = ctx.estimated_total_tokens

    compacted = await _perform_manual_compaction(
        messages=messages_without_status,
        client=SequenceClient([make_coordinator_api_error("boom")]),
        ctx=ctx,
        ui=ui,
    )
    assert compacted is False
    assert messages_without_status == pre_messages_without_status
    assert ctx.estimated_total_tokens == pre_estimated_without_status
    assert ctx.consecutive_compact_failures == 1
    assert ctx.breaker_tripped is False
    assert ui.messages == ["Manual compaction failed: boom"]

    ctx.consecutive_compact_failures = 0
    ctx.breaker_tripped = False
    ui.messages.clear()
    messages_with_status = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
    ]
    pre_messages_with_status = list(messages_with_status)
    pre_estimated_with_status = ctx.estimated_total_tokens
    compacted = await _perform_manual_compaction(
        messages=messages_with_status,
        client=SequenceClient([make_coordinator_api_error("boom", status_code=503)]),
        ctx=ctx,
        ui=ui,
    )
    assert compacted is False
    assert messages_with_status == pre_messages_with_status
    assert ctx.estimated_total_tokens == pre_estimated_with_status
    assert ctx.consecutive_compact_failures == 1
    assert ctx.breaker_tripped is False
    assert ui.messages == ["Manual compaction failed: API error 503: boom"]


@pytest.mark.asyncio
async def test_manual_compaction_formats_api_status_error(ctx, ui):
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
    ]
    pre_messages = list(messages)
    pre_estimated = ctx.estimated_total_tokens

    compacted = await _perform_manual_compaction(
        messages=messages, client=SequenceClient([make_api_error(429)]), ctx=ctx, ui=ui
    )

    assert compacted is False
    assert messages == pre_messages
    assert ctx.estimated_total_tokens == pre_estimated
    assert ctx.consecutive_compact_failures == 1
    assert ctx.breaker_tripped is False
    assert ui.messages == ["Manual compaction failed: API error 429: boom"]


@pytest.mark.asyncio
async def test_manual_compaction_formats_generic_exception(ctx, ui):
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
    ]
    pre_messages = list(messages)
    pre_estimated = ctx.estimated_total_tokens

    compacted = await _perform_manual_compaction(
        messages=messages,
        client=SequenceClient([RuntimeError("summary failed")]),
        ctx=ctx,
        ui=ui,
    )

    assert compacted is False
    assert messages == pre_messages
    assert ctx.estimated_total_tokens == pre_estimated
    assert ctx.consecutive_compact_failures == 1
    assert ctx.breaker_tripped is False
    assert ui.messages == ["Manual compaction failed: summary failed"]


@pytest.mark.asyncio
async def test_manual_compaction_third_failure_trips_breaker_and_prints_warning(
    ctx, ui
):
    ctx.consecutive_compact_failures = 2
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
    ]

    compacted = await _perform_manual_compaction(
        messages=messages,
        client=SequenceClient([RuntimeError("summary failed")]),
        ctx=ctx,
        ui=ui,
    )

    assert compacted is False
    assert ctx.consecutive_compact_failures == 3
    assert ctx.breaker_tripped is True
    assert ui.messages == [
        "Manual compaction failed: summary failed",
        _COMPACTION_BREAKER_WARNING,
    ]


@pytest.mark.asyncio
async def test_manual_compaction_does_not_reprint_breaker_warning_when_already_tripped(
    ctx, ui
):
    ctx.consecutive_compact_failures = 3
    ctx.breaker_tripped = True
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
    ]

    compacted = await _perform_manual_compaction(
        messages=messages,
        client=SequenceClient([make_response(content="")]),
        ctx=ctx,
        ui=ui,
    )

    assert compacted is False
    assert (
        "Manual compaction failed: summarizer returned an empty response."
        in ui.messages
    )
    assert _COMPACTION_BREAKER_WARNING not in ui.messages
    assert ctx.consecutive_compact_failures == 4
    assert ctx.breaker_tripped is True


@pytest.mark.asyncio
async def test_manual_compaction_resets_breaker_and_sets_estimated_total_on_success(
    ctx, ui
):
    ctx.consecutive_compact_failures = 2
    ctx.breaker_tripped = True
    ctx.estimated_total_tokens = 999
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
    ]

    compacted = await _perform_manual_compaction(
        messages=messages,
        client=SequenceClient([make_response(content="summary body")]),
        ctx=ctx,
        ui=ui,
    )

    assert compacted is True
    assert ctx.consecutive_compact_failures == 0
    assert ctx.breaker_tripped is False
    assert ctx.estimated_total_tokens == _approximate_tokens(messages)


@pytest.mark.asyncio
async def test_manual_compaction_attempts_even_when_breaker_already_tripped(ctx, ui):
    ctx.consecutive_compact_failures = 3
    ctx.breaker_tripped = True
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
    ]
    client = RecordingClient([make_response(content="summary body")])

    compacted = await _perform_manual_compaction(
        messages=messages, client=client, ctx=ctx, ui=ui
    )

    assert compacted is True
    assert len(client.calls) == 1
    assert ctx.consecutive_compact_failures == 0
    assert ctx.breaker_tripped is False


@pytest.mark.asyncio
async def test_manual_compaction_calls_begin_and_end_hooks_with_expected_counts_and_success_flag(
    ctx, ui
):
    ctx.prompt_tokens = 120
    ctx.completion_tokens = 30
    success_messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
    ]

    compacted = await _perform_manual_compaction(
        messages=success_messages,
        client=SequenceClient([make_response(content="summary body")]),
        ctx=ctx,
        ui=ui,
    )

    assert compacted is True
    assert ui.compaction_begin_calls == 1
    assert ui.compaction_end_results == [
        (150, _approximate_tokens(success_messages), True)
    ]

    failure_ui = type(ui)()
    failure_ctx = copy.deepcopy(ctx)
    failure_ctx.prompt_tokens = 120
    failure_ctx.completion_tokens = 30
    failure_messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
    ]

    compacted = await _perform_manual_compaction(
        messages=failure_messages,
        client=SequenceClient(
            [make_coordinator_api_error("summary failed", status_code=503)]
        ),
        ctx=failure_ctx,
        ui=failure_ui,
    )

    assert compacted is False
    assert failure_ui.compaction_begin_calls == 1
    assert failure_ui.compaction_end_results == [(150, 150, False)]


@pytest.mark.asyncio
async def test_compaction_failure_allows_turn_to_continue(tmp_path, config, ctx, ui):
    run_log = make_run_log(tmp_path, config, run_id="run_10")
    registry = ToolRegistry()
    ctx.model_id = "openai/gpt-4o"
    ctx.model_limit = 128_000
    ctx.prompt_tokens = get_auto_compact_threshold(ctx.model_id, ctx.model_limit)
    messages = compactable_messages()
    original_prefix = copy.deepcopy(messages)
    client = RecordingClient(
        [RuntimeError("summary failed"), make_response(content="assistant reply")]
    )

    should_continue, _ = await run_one_turn(
        messages=messages,
        config=config,
        run_log=run_log,
        ui=ui,
        tool_registry=registry,
        client=client,
        ctx=ctx,
        turn_index=0,
        last_logged_index=0,
    )

    assert should_continue is False
    assert client.calls[1]["messages"] == original_prefix
    assert messages[:-1] == original_prefix
    assert messages[-1] == {"role": "assistant", "content": "assistant reply"}


@pytest.mark.asyncio
async def test_circuit_breaker_trips_after_three_failures(tmp_path, config, ctx, ui):
    run_log = make_run_log(tmp_path, config, run_id="run_11")
    registry = ToolRegistry()
    ctx.model_id = "openai/gpt-4o"
    ctx.model_limit = 128_000
    threshold = get_auto_compact_threshold(ctx.model_id, ctx.model_limit)
    responses: list[object] = []
    for _ in range(3):
        responses.append(RuntimeError("summary failed"))
        responses.append(make_response(content="assistant reply"))
    responses.append(make_response(content="assistant after breaker"))
    client = RecordingClient(responses)
    messages = compactable_messages()

    for turn in range(3):
        ctx.prompt_tokens = threshold
        should_continue, _ = await run_one_turn(
            messages=messages,
            config=config,
            run_log=run_log,
            ui=ui,
            tool_registry=registry,
            client=client,
            ctx=ctx,
            turn_index=turn,
            last_logged_index=0,
        )
        assert should_continue is False
        messages.append({"role": "user", "content": f"follow up {turn}"})

    assert ctx.consecutive_compact_failures == 3
    assert ctx.breaker_tripped is True
    assert any(
        "Auto-compaction disabled for this session after 3 failures. Use /clear to reset context."
        in message
        for message in ui.messages
    )

    prior_call_count = len(client.calls)
    ctx.prompt_tokens = threshold
    await run_one_turn(
        messages=messages,
        config=config,
        run_log=run_log,
        ui=ui,
        tool_registry=registry,
        client=client,
        ctx=ctx,
        turn_index=3,
        last_logged_index=0,
    )

    assert len(client.calls) == prior_call_count + 1
    assert ui.compaction_started == 3


@pytest.mark.asyncio
async def test_successful_compaction_resets_failure_counter(ctx, ui):
    ctx.consecutive_compact_failures = 2
    ctx.breaker_tripped = True
    messages = compactable_messages()
    client = SequenceClient([make_response(content="summary")])

    compacted = await _perform_compaction(
        messages=messages, client=client, ctx=ctx, ui=ui
    )

    assert compacted is True
    assert ctx.consecutive_compact_failures == 0
    assert ctx.breaker_tripped is False


@pytest.mark.asyncio
async def test_run_one_turn_calls_end_compaction_with_before_and_after_counts(
    tmp_path, config, ctx, ui
):
    run_log = make_run_log(tmp_path, config, run_id="run_12_compaction_notice")
    registry = ToolRegistry()
    ctx.model_id = "openai/gpt-4o"
    ctx.model_limit = 128_000
    threshold = get_auto_compact_threshold(ctx.model_id, ctx.model_limit)
    ctx.prompt_tokens = threshold
    ctx.completion_tokens = 25
    before_tokens = ctx.prompt_tokens + ctx.completion_tokens
    messages = compactable_messages()
    client = RecordingClient(
        [
            make_response(content="summary body"),
            make_response(
                content="final assistant",
                usage=UsageRecord(prompt_tokens=12, completion_tokens=8),
            ),
        ]
    )

    should_continue, _ = await run_one_turn(
        messages=messages,
        config=config,
        run_log=run_log,
        ui=ui,
        tool_registry=registry,
        client=client,
        ctx=ctx,
        turn_index=0,
        last_logged_index=0,
    )

    assert should_continue is False
    assert ui.compaction_end_calls == [
        (before_tokens, _approximate_tokens(client.calls[1]["messages"]))
    ]


@pytest.mark.asyncio
async def test_run_one_turn_resets_messages_before_to_zero_after_compaction(
    tmp_path, config, ctx, ui
):
    run_log = make_run_log(tmp_path, config, run_id="run_12")
    registry = ToolRegistry()
    ctx.model_id = "openai/gpt-4o"
    ctx.model_limit = 128_000
    ctx.prompt_tokens = get_auto_compact_threshold(ctx.model_id, ctx.model_limit)
    messages = compactable_messages()
    client = RecordingClient(
        [
            make_response(content="summary body"),
            make_response(
                content="final assistant",
                usage=UsageRecord(prompt_tokens=12, completion_tokens=8),
            ),
        ]
    )

    should_continue, last_logged = await run_one_turn(
        messages=messages,
        config=config,
        run_log=run_log,
        ui=ui,
        tool_registry=registry,
        client=client,
        ctx=ctx,
        turn_index=0,
        last_logged_index=5,
    )

    assert should_continue is False
    assert last_logged == len(messages)
    run_data = json.loads(run_log.path.read_text())
    delta = run_data["turns"][0]["messages_appended_delta"]
    assert delta == messages
    assert delta[0]["role"] == "system"
    assert delta[1]["role"] == "system"
    assert delta[2]["content"].startswith("Compact summary of earlier conversation:")
    assert delta[-1] == {"role": "assistant", "content": "final assistant"}


@pytest.mark.asyncio
async def test_run_one_turn_integration_compacts_then_replies(
    tmp_path, config, ctx, ui
):
    run_log = make_run_log(tmp_path, config, run_id="run_13")
    registry = ToolRegistry()
    ctx.model_id = "openai/gpt-4o"
    ctx.model_limit = 128_000
    ctx.prompt_tokens = get_auto_compact_threshold(ctx.model_id, ctx.model_limit)
    messages = compactable_messages()
    client = RecordingClient(
        [
            make_response(content="summary state"),
            make_response(
                content="assistant reply",
                usage=UsageRecord(prompt_tokens=99_000, completion_tokens=500),
            ),
        ]
    )

    should_continue, _ = await run_one_turn(
        messages=messages,
        config=config,
        run_log=run_log,
        ui=ui,
        tool_registry=registry,
        client=client,
        ctx=ctx,
        turn_index=0,
        last_logged_index=0,
    )

    assert should_continue is False
    assert client.calls[1]["messages"] == messages[:-1]
    assert messages[3] == {"role": "user", "content": "pending user"}
    assert messages[-1] == {"role": "assistant", "content": "assistant reply"}
    run_data = json.loads(run_log.path.read_text())
    assert (
        run_data["turns"][0]["messages_appended_delta"][-1]["content"]
        == "assistant reply"
    )


def test_should_compact_uses_exact_user_tail_guard(ctx):
    ctx.breaker_tripped = False
    ctx.prompt_tokens = 99_000
    ctx.completion_tokens = 0
    ctx.estimated_total_tokens = 500_000

    assert (
        _should_compact(
            messages=[
                {"role": "system", "content": "sys"},
                {"role": "tool", "content": "result"},
            ],
            ctx=ctx,
            threshold=10,
        )
        is False
    )


def test_should_compact_on_empty_messages_returns_false(ctx):
    ctx.prompt_tokens = 99_000
    ctx.completion_tokens = 0

    assert _should_compact(messages=[], ctx=ctx, threshold=10) is False


def test_should_compact_on_system_only_messages_returns_false(ctx):
    ctx.prompt_tokens = 99_000
    ctx.completion_tokens = 0

    assert (
        _should_compact(
            messages=[{"role": "system", "content": "sys"}], ctx=ctx, threshold=10
        )
        is False
    )
