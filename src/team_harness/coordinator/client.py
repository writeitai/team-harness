from __future__ import annotations

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

    def model_dump(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }


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
        self, message: str, *, status_code: int | None = None, retryable: bool = False
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class _AsyncNullContext:
    def __init__(self, value: Any) -> None:
        self._value = value

    async def __aenter__(self) -> Any:
        return self._value

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


class CoordinatorClient:
    def __init__(self, api_base: str, api_key: str, model: str) -> None:
        self.model = model
        self.api_base = api_base
        self.provider = "openai_compat"
        self._client = AsyncOpenAI(base_url=api_base, api_key=api_key)

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        stream: bool = False,
        token_callback: Callable[[str], None] | None = None,
    ) -> ChatResponse:
        try:
            if not stream or token_callback is None:
                completion = await self._client.chat.completions.create(
                    model=self.model,
                    messages=cast(Any, messages),
                    tools=cast(Any, tools),
                )
                return self._normalize_completion(completion)

            accumulated_tool_calls: dict[int, dict[str, Any]] = {}
            full_content = ""
            finish_reason: str | None = None
            usage: UsageRecord | None = None

            stream_resp = await self._client.chat.completions.create(
                model=self.model,
                messages=cast(Any, messages),
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
            exc.message, status_code=exc.status_code, retryable=retryable
        )
    return CoordinatorAPIError(str(exc), retryable=True)
