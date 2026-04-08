# pyright: reportMissingParameterType=false, reportArgumentType=false, reportAttributeAccessIssue=false

import json

import httpx
from openai import APIStatusError

from team_harness.coordinator.client import ChatResponse
from team_harness.coordinator.client import ChoiceRecord
from team_harness.coordinator.client import CoordinatorAPIError
from team_harness.coordinator.client import FunctionRecord
from team_harness.coordinator.client import MessageRecord
from team_harness.coordinator.client import ToolCallRecord as ClientToolCallRecord
from team_harness.coordinator.client import UsageRecord


class SequenceClient:
    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.calls = 0
        self.model = "test/model"
        self.api_base = "http://localhost:9999"
        self.provider = "openai_compat"

    async def chat(self, messages, tools=None, stream=False, token_callback=None):
        self.calls += 1
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if stream and token_callback is not None:
            text = item.choices[0].message.content or ""
            for chunk in text:
                token_callback(chunk)
        return item

    async def get_models(self) -> dict:
        return {"data": []}

    async def aclose(self) -> None:
        return None


def make_response(
    *,
    content: str | None = None,
    tool_calls: list[tuple[str, dict, str]] | None = None,
    finish_reason: str | None = "stop",
    usage: UsageRecord | None = None,
) -> ChatResponse:
    tool_call_records = None
    if tool_calls:
        tool_call_records = [
            ClientToolCallRecord(
                id=call_id,
                function=FunctionRecord(name=name, arguments=json.dumps(arguments)),
            )
            for name, arguments, call_id in tool_calls
        ]
    return ChatResponse(
        choices=[
            ChoiceRecord(
                message=MessageRecord(content=content, tool_calls=tool_call_records),
                finish_reason=finish_reason,
            )
        ],
        usage=usage or UsageRecord(prompt_tokens=10, completion_tokens=5),
    )


def make_api_error(status_code: int, message: str = "boom") -> APIStatusError:
    request = httpx.Request("POST", "https://example.com/v1/chat/completions")
    response = httpx.Response(status_code=status_code, request=request)
    return APIStatusError(message, response=response, body={})


def make_coordinator_api_error(
    message: str = "boom", *, status_code: int | None = None, retryable: bool = False
) -> CoordinatorAPIError:
    return CoordinatorAPIError(message, status_code=status_code, retryable=retryable)
