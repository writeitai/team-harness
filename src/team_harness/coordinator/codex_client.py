from __future__ import annotations

import json
from typing import Any
from typing import AsyncIterator

import httpx

from team_harness.coordinator.auth import CodexAuth
from team_harness.coordinator.client import ChatResponse
from team_harness.coordinator.client import ChoiceRecord
from team_harness.coordinator.client import CoordinatorAPIError
from team_harness.coordinator.client import FunctionRecord
from team_harness.coordinator.client import MessageRecord
from team_harness.coordinator.client import ToolCallRecord
from team_harness.coordinator.client import UsageRecord

DEFAULT_CODEX_BASE_URL = "https://chatgpt.com/backend-api"


class CodexCoordinatorClient:
    def __init__(
        self,
        model: str,
        auth: CodexAuth,
        *,
        api_base: str = "",
        timeout_s: float = 60.0,
    ) -> None:
        self.model = model
        self.provider = "codex"
        self.api_base = _normalize_codex_url(api_base)
        self._auth = auth
        self._client = httpx.AsyncClient(
            timeout=timeout_s,
            follow_redirects=True,
            headers={
                "Authorization": f"Bearer {auth.token}",
                "chatgpt-account-id": auth.account_id,
                "OpenAI-Beta": "responses=experimental",
                "accept": "text/event-stream",
                "content-type": "application/json",
                "originator": "team-harness",
            },
        )

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        stream: bool = False,
        token_callback: Any = None,
    ) -> ChatResponse:
        instructions, input_items = _messages_to_codex(messages)
        body: dict[str, Any] = {
            "model": self.model,
            "store": False,
            "stream": True,
            "include": ["reasoning.encrypted_content"],
            "instructions": instructions,
            "input": input_items,
        }
        if tools:
            body["tools"] = _tools_to_codex(tools)
            body["tool_choice"] = "auto"
            body["parallel_tool_calls"] = True

        text_chunks: list[str] = []
        tool_calls: list[ToolCallRecord] = []
        saw_completed = False
        completed_response: dict[str, Any] | None = None

        try:
            async with self._client.stream(
                "POST", self.api_base, json=body
            ) as response:
                response.raise_for_status()
                async for event in _iter_sse_events(response):
                    event_type = event.get("type")
                    if event_type == "response.output_text.delta":
                        delta = event.get("delta")
                        if isinstance(delta, str) and delta:
                            text_chunks.append(delta)
                            if stream and token_callback is not None:
                                token_callback(delta)
                        continue
                    if event_type == "response.output_item.done":
                        item = event.get("item")
                        if not isinstance(item, dict):
                            continue
                        item_type = item.get("type")
                        if item_type == "message":
                            text = _message_text_from_item(item)
                            if text:
                                text_chunks.append(text)
                                if stream and token_callback is not None:
                                    token_callback(text)
                            continue
                        if item_type == "function_call":
                            tool_call = _tool_call_from_item(item)
                            if tool_call is not None:
                                tool_calls.append(tool_call)
                            continue
                    if event_type == "response.completed":
                        response_payload = event.get("response")
                        if isinstance(response_payload, dict):
                            completed_response = response_payload
                        saw_completed = True
                        continue
                    if event_type == "response.failed":
                        raise CoordinatorAPIError(
                            _response_error_message(event, "Codex response failed.")
                        )
                    if event_type == "error":
                        raise CoordinatorAPIError(
                            _response_error_message(event, "Codex request failed.")
                        )
        except CoordinatorAPIError:
            raise
        except httpx.HTTPStatusError as exc:
            raise _map_http_status_error(exc) from exc
        except httpx.TimeoutException as exc:
            raise CoordinatorAPIError(str(exc), retryable=True) from exc
        except httpx.NetworkError as exc:
            raise CoordinatorAPIError(str(exc), retryable=True) from exc
        except httpx.TransportError as exc:
            raise CoordinatorAPIError(str(exc), retryable=True) from exc
        except json.JSONDecodeError as exc:
            raise CoordinatorAPIError(
                "Received malformed SSE JSON from Codex."
            ) from exc

        if not saw_completed:
            raise CoordinatorAPIError(
                "Codex response stream ended before completion.", retryable=True
            )

        return ChatResponse(
            choices=[
                ChoiceRecord(
                    message=MessageRecord(
                        content="".join(text_chunks) or None,
                        tool_calls=tool_calls or None,
                    ),
                    finish_reason=_finish_reason_from_response(
                        completed_response or {}, has_tool_calls=bool(tool_calls)
                    ),
                )
            ],
            usage=_usage_from_response(completed_response or {}),
        )

    async def get_models(self) -> dict[str, Any]:
        return {"data": []}

    async def aclose(self) -> None:
        await self._client.aclose()


def _messages_to_codex(messages: list[dict]) -> tuple[str, list[dict[str, Any]]]:
    instructions_parts: list[str] = []
    items: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        content = message.get("content")
        if role == "system":
            if isinstance(content, str) and content.strip():
                instructions_parts.append(content)
            continue
        if role == "user":
            if isinstance(content, str) and content.strip():
                items.append(
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": content}],
                    }
                )
            continue
        if role == "assistant":
            if isinstance(content, str) and content:
                items.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": content, "annotations": []}
                        ],
                    }
                )
            for tool_call in message.get("tool_calls", []) or []:
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function")
                if not isinstance(function, dict):
                    continue
                call_id = str(tool_call.get("id", "") or "")
                name = str(function.get("name", "") or "")
                arguments = function.get("arguments", "")
                if not call_id or not name:
                    continue
                items.append(
                    {
                        "type": "function_call",
                        "id": f"fc_{call_id[:58]}",
                        "call_id": call_id,
                        "name": name,
                        "arguments": (
                            arguments
                            if isinstance(arguments, str)
                            else json.dumps(arguments, separators=(",", ":"))
                        ),
                    }
                )
            continue
        if role == "tool":
            call_id = str(message.get("tool_call_id", "") or "")
            if not call_id:
                continue
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": str(content or ""),
                }
            )
    return "\n\n".join(part for part in instructions_parts if part), items


def _tools_to_codex(tools: list[dict]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for tool in tools:
        function = tool.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue
        converted.append(
            {
                "type": "function",
                "name": name,
                "description": str(function.get("description", "") or ""),
                "parameters": (
                    function.get("parameters")
                    if isinstance(function.get("parameters"), dict)
                    else {}
                ),
            }
        )
    return converted


async def _iter_sse_events(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    data_lines: list[str] = []
    async for line in response.aiter_lines():
        if line == "":
            if data_lines:
                yield _parse_sse_payload(data_lines)
                data_lines = []
            continue
        if line.startswith("event:"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].strip())
    if data_lines:
        yield _parse_sse_payload(data_lines)


def _normalize_codex_url(base_url: str | None) -> str:
    raw = (base_url or DEFAULT_CODEX_BASE_URL).strip().rstrip("/")
    if not raw:
        raw = DEFAULT_CODEX_BASE_URL
    if raw.endswith("/codex/responses"):
        return raw
    if raw.endswith("/codex"):
        return f"{raw}/responses"
    return f"{raw}/codex/responses"


def _parse_sse_payload(data_lines: list[str]) -> dict[str, Any]:
    payload = "\n".join(data_lines).strip()
    if not payload or payload == "[DONE]":
        return {}
    parsed = json.loads(payload)
    if not isinstance(parsed, dict):
        raise json.JSONDecodeError("Expected JSON object", payload, 0)
    return parsed


def _message_text_from_item(item: dict[str, Any]) -> str:
    parts: list[str] = []
    for block in item.get("content", []) or []:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "output_text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif block_type == "refusal":
            refusal = block.get("refusal")
            if isinstance(refusal, str):
                parts.append(refusal)
    return "".join(parts)


def _tool_call_from_item(item: dict[str, Any]) -> ToolCallRecord | None:
    call_id = item.get("call_id")
    name = item.get("name")
    arguments = item.get("arguments", "")
    if not isinstance(call_id, str) or not call_id:
        return None
    if not isinstance(name, str) or not name:
        return None
    return ToolCallRecord(
        id=call_id,
        function=FunctionRecord(
            name=name,
            arguments=arguments
            if isinstance(arguments, str)
            else json.dumps(arguments),
        ),
    )


def _usage_from_response(response: dict[str, Any]) -> UsageRecord | None:
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return None
    return UsageRecord(
        prompt_tokens=int(usage.get("input_tokens") or 0),
        completion_tokens=int(usage.get("output_tokens") or 0),
    )


def _finish_reason_from_response(
    response: dict[str, Any], *, has_tool_calls: bool
) -> str | None:
    status = response.get("status")
    if status == "completed":
        return "tool_calls" if has_tool_calls else "stop"
    if status == "incomplete":
        details = response.get("incomplete_details")
        if isinstance(details, dict):
            reason = details.get("reason")
            if reason == "max_output_tokens":
                return "length"
            if reason == "content_filter":
                return "content_filter"
            if isinstance(reason, str) and reason:
                return reason
        return "incomplete"
    if isinstance(status, str) and status:
        return status
    return None


def _response_error_message(event: dict[str, Any], fallback: str) -> str:
    response = event.get("response")
    if isinstance(response, dict):
        error = response.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message.strip():
                return message
            code = error.get("code")
            if isinstance(code, str) and code.strip():
                return code
    message = event.get("message")
    if isinstance(message, str) and message.strip():
        return message
    code = event.get("code")
    if isinstance(code, str) and code.strip():
        return code
    return fallback


def _map_http_status_error(exc: httpx.HTTPStatusError) -> CoordinatorAPIError:
    status_code = exc.response.status_code
    if status_code in {401, 403}:
        return CoordinatorAPIError(
            "Codex authentication failed or expired. Run `codex login` and retry.",
            status_code=status_code,
        )
    message = _http_error_message(exc.response)
    return CoordinatorAPIError(
        message,
        status_code=status_code,
        retryable=status_code == 429 or status_code >= 500,
    )


def _http_error_message(response: httpx.Response) -> str:
    payload = response.text.strip()
    if payload:
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return payload
        if isinstance(parsed, dict):
            error = parsed.get("error")
            if isinstance(error, dict):
                message = error.get("message")
                if isinstance(message, str) and message.strip():
                    return message
            detail = parsed.get("detail")
            if isinstance(detail, str) and detail.strip():
                return detail
    return f"Codex request failed with status {response.status_code}."
