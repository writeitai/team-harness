# pyright: reportMissingParameterType=false, reportArgumentType=false

from typing import Any
from typing import cast

import httpx
import pytest

from team_harness.coordinator.auth import CodexAuth
from team_harness.coordinator.client import CoordinatorAPIError
from team_harness.coordinator.codex_client import _messages_to_codex
from team_harness.coordinator.codex_client import _normalize_codex_url
from team_harness.coordinator.codex_client import _tools_to_codex
from team_harness.coordinator.codex_client import CodexCoordinatorClient


class FakeResponse:
    def __init__(self, lines, *, status_code=200, text="") -> None:
        self._lines = lines
        self.status_code = status_code
        self._text = text
        self.request = httpx.Request(
            "POST", "https://chatgpt.com/backend-api/codex/responses"
        )

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    def raise_for_status(self):
        if self.status_code < 400:
            return None
        response = httpx.Response(
            self.status_code, request=self.request, text=self._text
        )
        raise httpx.HTTPStatusError("boom", request=self.request, response=response)


class FakeStreamContext:
    def __init__(self, *, response=None, exc=None) -> None:
        self._response = response
        self._exc = exc

    async def __aenter__(self):
        if self._exc is not None:
            raise self._exc
        return self._response

    async def __aexit__(self, exc_type, exc, tb):
        return None


class FakeAsyncClient:
    def __init__(self, *, response=None, exc=None) -> None:
        self._response = response
        self._exc = exc
        self.last_json = None
        self.closed = False

    def stream(self, method, url, *, json):
        self.last_json = json
        return FakeStreamContext(response=self._response, exc=self._exc)

    async def aclose(self):
        self.closed = True


def _make_client(
    *, response=None, exc=None
) -> tuple[CodexCoordinatorClient, FakeAsyncClient]:
    client = CodexCoordinatorClient(
        "codex-mini-latest", CodexAuth(token="token", account_id="acct_123")
    )
    fake_http = FakeAsyncClient(response=response, exc=exc)
    client._client = cast(Any, fake_http)
    return client, fake_http


def test_messages_to_codex_converts_chat_messages():
    instructions, items = _messages_to_codex(
        [
            {"role": "system", "content": "system one"},
            {"role": "system", "content": "system two"},
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "content": "thinking",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "todo_write", "arguments": '{"tasks":[]}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
        ]
    )

    assert instructions == "system one\n\nsystem two"
    assert items == [
        {"role": "user", "content": [{"type": "input_text", "text": "hello"}]},
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "thinking", "annotations": []}],
        },
        {
            "type": "function_call",
            "id": "fc_call_1",
            "call_id": "call_1",
            "name": "todo_write",
            "arguments": '{"tasks":[]}',
        },
        {"type": "function_call_output", "call_id": "call_1", "output": "ok"},
    ]


def test_tools_to_codex_converts_function_schema():
    assert _tools_to_codex(
        [
            {
                "type": "function",
                "function": {
                    "name": "spawn_agent",
                    "description": "Spawn a worker",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
    ) == [
        {
            "type": "function",
            "name": "spawn_agent",
            "description": "Spawn a worker",
            "parameters": {"type": "object", "properties": {}},
        }
    ]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", "https://chatgpt.com/backend-api/codex/responses"),
        (
            "https://chatgpt.com/backend-api",
            "https://chatgpt.com/backend-api/codex/responses",
        ),
        (
            "https://chatgpt.com/backend-api/codex",
            "https://chatgpt.com/backend-api/codex/responses",
        ),
        (
            "https://chatgpt.com/backend-api/codex/responses",
            "https://chatgpt.com/backend-api/codex/responses",
        ),
    ],
)
def test_normalize_codex_url(raw, expected):
    assert _normalize_codex_url(raw) == expected


@pytest.mark.asyncio
async def test_codex_client_chat_text_stream():
    response = FakeResponse(
        [
            "event: response.output_text.delta",
            'data: {"type":"response.output_text.delta","delta":"Hello"}',
            "",
            'data: {"type":"response.completed","response":{"status":"completed","usage":{"input_tokens":3,"output_tokens":4}}}',
            "",
        ]
    )
    client, fake_http = _make_client(response=response)
    tokens = []

    result = await client.chat(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
        stream=True,
        token_callback=tokens.append,
    )

    assert result.choices[0].message.content == "Hello"
    assert result.choices[0].finish_reason == "stop"
    assert result.usage is not None
    assert result.usage.prompt_tokens == 3
    assert result.usage.completion_tokens == 4
    assert tokens == ["Hello"]
    assert fake_http.last_json["instructions"] == "sys"
    assert fake_http.last_json["input"] == [
        {"role": "user", "content": [{"type": "input_text", "text": "hi"}]}
    ]


@pytest.mark.asyncio
async def test_codex_client_chat_mixed_text_and_tool_calls():
    response = FakeResponse(
        [
            'data: {"type":"response.output_text.delta","delta":"Hello "}',
            "",
            'data: {"type":"response.output_item.done","item":{"type":"function_call","call_id":"call_1","name":"todo_write","arguments":"{\\"tasks\\":[]}"}}',
            "",
            'data: {"type":"response.output_text.delta","delta":"world"}',
            "",
            'data: {"type":"response.completed","response":{"status":"completed"}}',
            "",
        ]
    )
    client, _ = _make_client(response=response)

    result = await client.chat(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    )

    assert result.choices[0].message.content == "Hello world"
    assert result.choices[0].finish_reason == "tool_calls"
    assert result.choices[0].message.tool_calls is not None
    assert result.choices[0].message.tool_calls[0].id == "call_1"
    assert result.choices[0].message.tool_calls[0].function.name == "todo_write"


@pytest.mark.asyncio
async def test_codex_client_maps_incomplete_length_finish_reason():
    response = FakeResponse(
        [
            'data: {"type":"response.output_item.done","item":{"type":"message","content":[{"type":"output_text","text":"partial"}]}}',
            "",
            'data: {"type":"response.completed","response":{"status":"incomplete","incomplete_details":{"reason":"max_output_tokens"}}}',
            "",
        ]
    )
    client, _ = _make_client(response=response)

    result = await client.chat(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    )

    assert result.choices[0].message.content == "partial"
    assert result.choices[0].finish_reason == "length"


@pytest.mark.asyncio
async def test_codex_client_maps_malformed_sse_json():
    response = FakeResponse(["data: {bad json", ""])
    client, _ = _make_client(response=response)

    with pytest.raises(CoordinatorAPIError, match="malformed SSE JSON"):
        await client.chat(
            [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
        )


@pytest.mark.parametrize("status_code", [401, 403])
@pytest.mark.asyncio
async def test_codex_client_maps_auth_http_errors(status_code):
    response = FakeResponse(
        [], status_code=status_code, text='{"error":{"message":"bad"}}'
    )
    client, _ = _make_client(response=response)

    with pytest.raises(
        CoordinatorAPIError, match="Codex authentication failed or expired"
    ):
        await client.chat(
            [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
        )


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ReadTimeout("timeout"),
        httpx.ConnectError(
            "network", request=httpx.Request("POST", "https://example.test")
        ),
    ],
)
@pytest.mark.asyncio
async def test_codex_client_maps_retryable_transport_errors(exc):
    client, _ = _make_client(exc=exc)

    with pytest.raises(CoordinatorAPIError) as caught:
        await client.chat(
            [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
        )

    assert caught.value.retryable is True


@pytest.mark.asyncio
async def test_codex_client_detects_missing_response_completed():
    response = FakeResponse(
        ['data: {"type":"response.output_text.delta","delta":"Hello"}', ""]
    )
    client, _ = _make_client(response=response)

    with pytest.raises(CoordinatorAPIError, match="ended before completion") as caught:
        await client.chat(
            [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
        )

    assert caught.value.retryable is True
