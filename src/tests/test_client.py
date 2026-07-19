# pyright: reportMissingParameterType=false, reportAttributeAccessIssue=false

from types import SimpleNamespace

import pytest

from team_harness.coordinator.client import _apply_prompt_cache
from team_harness.coordinator.client import _extract_cached_tokens
from team_harness.coordinator.client import _is_anthropic_model
from team_harness.coordinator.client import CoordinatorClient


class _FakeCompletions:
    def __init__(self, completion: object) -> None:
        self._completion = completion
        self.captured: dict = {}

    async def create(self, **kwargs: object) -> object:
        self.captured = kwargs
        return self._completion


def _install_fake_client(
    client: CoordinatorClient, completion: object
) -> _FakeCompletions:
    completions = _FakeCompletions(completion)
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return completions


def _make_completion(
    *, content: str = "ok", cached_tokens: int | None = None
) -> object:
    usage = SimpleNamespace(prompt_tokens=100, completion_tokens=20)
    if cached_tokens is not None:
        usage.prompt_tokens_details = SimpleNamespace(cached_tokens=cached_tokens)
    message = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(choices=[choice], usage=usage)


def test_is_anthropic_model_detects_family():
    assert _is_anthropic_model("anthropic/claude-opus-4")
    assert _is_anthropic_model("CLAUDE-sonnet-4")
    assert _is_anthropic_model("some-anthropic-model")
    assert not _is_anthropic_model("openai/gpt-5.6-sol")
    assert not _is_anthropic_model("google/gemini-2.5-pro")


def test_apply_prompt_cache_marks_system_and_last_message():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "first"},
        {"role": "tool", "tool_call_id": "1", "content": "result"},
    ]
    result = _apply_prompt_cache(messages)

    assert result[0]["content"] == [
        {"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}
    ]
    # Middle message untouched.
    assert result[1] == {"role": "user", "content": "first"}
    assert result[-1]["content"] == [
        {"type": "text", "text": "result", "cache_control": {"type": "ephemeral"}}
    ]
    # The caller's list is not mutated in place.
    assert messages[0]["content"] == "sys"
    assert messages[-1]["content"] == "result"


def test_apply_prompt_cache_single_system_message_marked_once():
    messages = [{"role": "system", "content": "sys"}]
    result = _apply_prompt_cache(messages)
    assert result[0]["content"] == [
        {"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}
    ]


@pytest.mark.asyncio
async def test_chat_injects_cache_control_for_claude_model():
    client = CoordinatorClient(
        api_base="http://localhost:9999", api_key="k", model="anthropic/claude-opus-4"
    )
    completions = _install_fake_client(client, _make_completion())
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},
    ]

    await client.chat(messages=messages, tools=[])

    sent = completions.captured["messages"]
    assert sent[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert sent[-1]["content"][0]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_chat_omits_cache_control_for_gpt_model():
    client = CoordinatorClient(
        api_base="http://localhost:9999", api_key="k", model="openai/gpt-5.6-sol"
    )
    completions = _install_fake_client(client, _make_completion())
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},
    ]

    await client.chat(messages=messages, tools=[])

    sent = completions.captured["messages"]
    # Non-Anthropic payloads are passed through verbatim (string content).
    assert sent == messages
    assert all(isinstance(message["content"], str) for message in sent)


@pytest.mark.asyncio
async def test_chat_prompt_cache_off_disables_injection_for_claude():
    client = CoordinatorClient(
        api_base="http://localhost:9999",
        api_key="k",
        model="anthropic/claude-opus-4",
        prompt_cache="off",
    )
    completions = _install_fake_client(client, _make_completion())
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},
    ]

    await client.chat(messages=messages, tools=[])

    assert completions.captured["messages"] == messages


def test_extract_cached_tokens_from_details_object_and_mapping():
    obj_usage = SimpleNamespace(prompt_tokens_details=SimpleNamespace(cached_tokens=42))
    assert _extract_cached_tokens(obj_usage) == 42
    mapping_usage = {"prompt_tokens_details": {"cached_tokens": 7}}
    assert _extract_cached_tokens(mapping_usage) == 7
    fallback_usage = SimpleNamespace(cache_read_input_tokens=3)
    assert _extract_cached_tokens(fallback_usage) == 3
    assert _extract_cached_tokens(SimpleNamespace()) == 0
    assert _extract_cached_tokens(None) == 0


@pytest.mark.asyncio
async def test_chat_passes_cached_tokens_into_usage():
    client = CoordinatorClient(
        api_base="http://localhost:9999", api_key="k", model="anthropic/claude-opus-4"
    )
    _install_fake_client(client, _make_completion(cached_tokens=55))

    response = await client.chat(messages=[{"role": "user", "content": "hi"}], tools=[])

    assert response.usage is not None
    assert response.usage.cached_prompt_tokens == 55
    dumped = response.usage.model_dump()
    assert dumped["cached_prompt_tokens"] == 55
    # Existing keys preserved for downstream usage metering.
    assert dumped["prompt_tokens"] == 100
    assert dumped["completion_tokens"] == 20


@pytest.mark.asyncio
async def test_chat_usage_without_cache_omits_cached_key():
    client = CoordinatorClient(
        api_base="http://localhost:9999", api_key="k", model="openai/gpt-5.6-sol"
    )
    _install_fake_client(client, _make_completion())

    response = await client.chat(messages=[{"role": "user", "content": "hi"}], tools=[])

    assert response.usage is not None
    assert response.usage.model_dump() == {
        "prompt_tokens": 100,
        "completion_tokens": 20,
    }
