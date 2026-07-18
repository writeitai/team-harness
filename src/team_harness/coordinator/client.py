from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from typing import Callable
from typing import cast

from openai import APIConnectionError
from openai import APIStatusError
from openai import APITimeoutError
from openai import AsyncOpenAI


@dataclass
class UsageRecord:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # Cache-read prompt tokens reported by the provider (OpenRouter/Anthropic
    # cached_tokens). Additive-only: it appears in model_dump() when non-zero so
    # existing consumers that read prompt_tokens/completion_tokens are unaffected.
    cached_prompt_tokens: int = 0

    def model_dump(self) -> dict[str, int]:
        payload = {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }
        if self.cached_prompt_tokens:
            payload["cached_prompt_tokens"] = self.cached_prompt_tokens
        return payload


@dataclass
class FunctionRecord:
    name: str
    arguments: str

    def model_dump(self) -> dict[str, str]:
        return {"name": self.name, "arguments": self.arguments}


@dataclass
class ToolCallRecord:
    id: str
    function: FunctionRecord
    type: str = "function"

    def model_dump(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "function": self.function.model_dump(),
        }


@dataclass
class MessageRecord:
    content: str | None
    tool_calls: list[ToolCallRecord] | None = None


@dataclass
class ChoiceRecord:
    message: MessageRecord
    finish_reason: str | None = None


@dataclass
class ChatResponse:
    choices: list[ChoiceRecord]
    usage: UsageRecord | None = None


class CoordinatorAPIError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
        error_type: str | None = None,
        cause_type: str | None = None,
        host: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.error_type = error_type or type(self).__name__
        self.cause_type = cause_type
        self.host = host


class _AsyncNullContext:
    def __init__(self, value: Any) -> None:
        self._value = value

    async def __aenter__(self) -> Any:
        return self._value

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


def _is_anthropic_model(model: str) -> bool:
    """True for Anthropic-family model names (case-insensitive)."""
    lowered = model.lower()
    return "claude" in lowered or "anthropic" in lowered


_CACHE_CONTROL: dict[str, str] = {"type": "ephemeral"}


def _with_cache_control(message: dict) -> dict:
    """Return a copy of `message` with an ephemeral cache breakpoint on its
    content, promoting a string body to the content-part form OpenRouter
    expects for Anthropic caching. Non-string / empty bodies are returned
    unchanged so no invalid part is emitted."""

    content = message.get("content")
    if isinstance(content, str) and content:
        parts = [
            {"type": "text", "text": content, "cache_control": dict(_CACHE_CONTROL)}
        ]
        return {**message, "content": parts}
    if isinstance(content, list) and content:
        new_parts = [dict(part) if isinstance(part, dict) else part for part in content]
        last = new_parts[-1]
        if isinstance(last, dict):
            last["cache_control"] = dict(_CACHE_CONTROL)
            return {**message, "content": new_parts}
    return message


def _apply_prompt_cache(messages: list[dict]) -> list[dict]:
    """Inject ephemeral cache breakpoints for OpenRouter/Anthropic.

    Places one breakpoint on the leading system message (the long-lived
    prefix) and one on the final message of the request. Because the
    conversation is append-only, the breakpoint written on this turn's last
    message becomes the most recent boundary before the next turn's new
    content — a cache read then covers the whole prefix up to it. Only the
    modified messages are copied; the caller's list is left untouched."""

    if not messages:
        return messages
    result = list(messages)
    if result[0].get("role") == "system":
        result[0] = _with_cache_control(result[0])
    last_index = len(result) - 1
    if last_index != 0 or result[0].get("role") != "system":
        result[last_index] = _with_cache_control(result[last_index])
    return result


def _extract_cached_tokens(usage: Any) -> int:
    """Pull cache-read prompt tokens from a provider usage object.

    Handles the OpenAI-compatible `prompt_tokens_details.cached_tokens` shape
    (object or mapping) plus the Anthropic-style `cache_read_input_tokens`
    fallback that some OpenRouter responses expose at the usage top level."""

    if usage is None:
        return 0
    details = getattr(usage, "prompt_tokens_details", None)
    if details is None and isinstance(usage, Mapping):
        details = usage.get("prompt_tokens_details")
    if details is not None:
        cached = getattr(details, "cached_tokens", None)
        if cached is None and isinstance(details, Mapping):
            cached = details.get("cached_tokens")
        if cached:
            return int(cached)
    fallback = getattr(usage, "cache_read_input_tokens", None)
    if fallback is None and isinstance(usage, Mapping):
        fallback = usage.get("cache_read_input_tokens")
    return int(fallback) if fallback else 0


class CoordinatorClient:
    def __init__(
        self, api_base: str, api_key: str, model: str, prompt_cache: str = "auto"
    ) -> None:
        self.model = model
        self.api_base = api_base
        self.provider = "openai_compat"
        # Anthropic-family models need explicit cache breakpoints; OpenAI-family
        # caching is automatic server-side, so we inject nothing for them.
        self._cache_prefix = prompt_cache == "auto" and _is_anthropic_model(model)
        self._client = AsyncOpenAI(base_url=api_base, api_key=api_key)

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        stream: bool = False,
        token_callback: Callable[[str], None] | None = None,
    ) -> ChatResponse:
        request_messages = (
            _apply_prompt_cache(messages) if self._cache_prefix else messages
        )
        try:
            if not stream or token_callback is None:
                completion = await self._client.chat.completions.create(
                    model=self.model,
                    messages=cast(Any, request_messages),
                    tools=cast(Any, tools),
                )
                return self._normalize_completion(completion)

            accumulated_tool_calls: dict[int, dict[str, Any]] = {}
            full_content = ""
            finish_reason: str | None = None
            usage: UsageRecord | None = None

            stream_resp = await self._client.chat.completions.create(
                model=self.model,
                messages=cast(Any, request_messages),
                tools=cast(Any, tools),
                stream=True,
                stream_options={"include_usage": True},
            )
            context = (
                stream_resp
                if hasattr(stream_resp, "__aenter__")
                else _AsyncNullContext(stream_resp)
            )
            async with context as iterator:
                async for chunk in iterator:
                    choice = (
                        chunk.choices[0] if getattr(chunk, "choices", None) else None
                    )
                    if choice is not None:
                        delta = choice.delta
                        if getattr(delta, "content", None):
                            content_piece = str(delta.content)
                            full_content += content_piece
                            token_callback(content_piece)
                        finish_reason = choice.finish_reason or finish_reason
                        for tc_delta in getattr(delta, "tool_calls", []) or []:
                            idx = tc_delta.index
                            if idx not in accumulated_tool_calls:
                                accumulated_tool_calls[idx] = {
                                    "id": tc_delta.id or "",
                                    "type": "function",
                                    "function": {
                                        "name": (
                                            tc_delta.function.name
                                            if tc_delta.function
                                            and tc_delta.function.name
                                            else ""
                                        ),
                                        "arguments": "",
                                    },
                                }
                            if tc_delta.id:
                                accumulated_tool_calls[idx]["id"] = tc_delta.id
                            if tc_delta.function:
                                if tc_delta.function.name:
                                    accumulated_tool_calls[idx]["function"]["name"] = (
                                        tc_delta.function.name
                                    )
                                if tc_delta.function.arguments:
                                    accumulated_tool_calls[idx]["function"][
                                        "arguments"
                                    ] += tc_delta.function.arguments
                    if getattr(chunk, "usage", None):
                        usage = UsageRecord(
                            prompt_tokens=int(getattr(chunk.usage, "prompt_tokens", 0)),
                            completion_tokens=int(
                                getattr(chunk.usage, "completion_tokens", 0)
                            ),
                            cached_prompt_tokens=_extract_cached_tokens(chunk.usage),
                        )

            tool_calls = [
                ToolCallRecord(
                    id=item["id"],
                    type=str(item.get("type", "function")),
                    function=FunctionRecord(
                        name=str(item["function"]["name"]),
                        arguments=str(item["function"]["arguments"]),
                    ),
                )
                for _, item in sorted(accumulated_tool_calls.items())
            ]
            return ChatResponse(
                choices=[
                    ChoiceRecord(
                        message=MessageRecord(
                            content=full_content or None, tool_calls=tool_calls or None
                        ),
                        finish_reason=finish_reason,
                    )
                ],
                usage=usage,
            )
        except (APIConnectionError, APIStatusError, APITimeoutError) as exc:
            raise _translate_api_error(exc) from exc

    async def get_models(self) -> dict[str, Any]:
        try:
            models = await self._client.models.list()
            return {"data": [model.model_dump() for model in models.data]}
        except (APIConnectionError, APIStatusError, APITimeoutError) as exc:
            raise _translate_api_error(exc) from exc

    async def aclose(self) -> None:
        return None

    def _normalize_completion(self, completion: Any) -> ChatResponse:
        message = completion.choices[0].message
        usage = getattr(completion, "usage", None)
        tool_calls = None
        if getattr(message, "tool_calls", None):
            tool_calls = [
                ToolCallRecord(
                    id=tool_call.id,
                    type=getattr(tool_call, "type", "function"),
                    function=FunctionRecord(
                        name=tool_call.function.name,
                        arguments=tool_call.function.arguments,
                    ),
                )
                for tool_call in message.tool_calls
            ]
        usage_record = None
        if usage is not None:
            usage_record = UsageRecord(
                prompt_tokens=int(getattr(usage, "prompt_tokens", 0)),
                completion_tokens=int(getattr(usage, "completion_tokens", 0)),
                cached_prompt_tokens=_extract_cached_tokens(usage),
            )
        return ChatResponse(
            choices=[
                ChoiceRecord(
                    message=MessageRecord(
                        content=getattr(message, "content", None), tool_calls=tool_calls
                    ),
                    finish_reason=getattr(completion.choices[0], "finish_reason", None),
                )
            ],
            usage=usage_record,
        )


def _translate_api_error(
    exc: APIConnectionError | APIStatusError | APITimeoutError,
) -> CoordinatorAPIError:
    if isinstance(exc, APIStatusError):
        retryable = exc.status_code == 429 or exc.status_code >= 500
        return CoordinatorAPIError(
            exc.message,
            status_code=exc.status_code,
            retryable=retryable,
            cause_type=type(exc).__name__,
        )
    return CoordinatorAPIError(str(exc), retryable=True, cause_type=type(exc).__name__)
