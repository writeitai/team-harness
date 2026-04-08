import asyncio
from collections.abc import Callable
import json
from typing import TYPE_CHECKING

from openai import APIStatusError

from team_harness.tracking.models import ToolCallRecord

if TYPE_CHECKING:
    from team_harness.config import Config
    from team_harness.coordinator.client import ChatResponse
    from team_harness.coordinator.client import CoordinatorClient
    from team_harness.tools.registry import ToolRegistry
    from team_harness.tracking.context import ContextTracker
    from team_harness.tracking.run_log import RunLogWriter
    from team_harness.ui.console import ConsoleBase


class MaxRetriesExceeded(RuntimeError):
    """Sentinel raised when retriable API failures exhaust the retry budget."""


async def run(
    messages: list[dict],
    config: "Config",
    run_log: "RunLogWriter",
    ui: "ConsoleBase",
    tool_registry: "ToolRegistry",
    client: "CoordinatorClient",
    ctx: "ContextTracker",
) -> None:
    turn_index = 0
    last_logged_index = 0
    while turn_index < config.max_turns:
        should_continue, last_logged_index = await run_one_turn(
            messages,
            config,
            run_log,
            ui,
            tool_registry,
            client,
            ctx,
            turn_index,
            last_logged_index,
        )
        turn_index += 1
        if not should_continue:
            return
    ui.print(f"Max turns ({config.max_turns}) reached — stopping.")


async def run_one_turn(
    messages: list[dict],
    config: "Config",
    run_log: "RunLogWriter",
    ui: "ConsoleBase",
    tool_registry: "ToolRegistry",
    client: "CoordinatorClient",
    ctx: "ContextTracker",
    turn_index: int,
    last_logged_index: int,
) -> tuple[bool, int]:
    ui.begin_turn(turn_index)
    messages_before = last_logged_index
    tools = tool_registry.get_all_schemas()
    ui.begin_streaming()
    try:
        response = await _chat_with_retry(
            client, messages, tools, config, token_callback=ui.stream_token
        )
    except MaxRetriesExceeded as exc:
        ui.end_streaming()
        ui.print(f"API error (retries exhausted): {exc}")
        run_log.finalize(error=str(exc))
        return False, last_logged_index
    except APIStatusError as exc:
        ui.end_streaming()
        ui.print(f"API error {exc.status_code}: {exc.message}")
        run_log.finalize(error=str(exc))
        return False, last_logged_index
    ui.end_streaming()

    if response.usage:
        ctx.update(response.usage)
        if ctx.at_warning and not ctx.at_warning_emitted:
            ui.context_warning()
            ctx.at_warning_emitted = True

    choice = response.choices[0]
    tool_calls = choice.message.tool_calls or []
    if tool_calls:
        assistant_msg = {
            "role": "assistant",
            "content": choice.message.content,
            "tool_calls": [tool_call.model_dump() for tool_call in tool_calls],
        }
        messages.append(assistant_msg)
        tool_call_records: list[ToolCallRecord] = []
        for tool_call in tool_calls:
            arguments = {}
            tool_ctx = None
            try:
                arguments = json.loads(tool_call.function.arguments)
                tool_ctx = ui.tool_call_start(tool_call.function.name, arguments)
                result = await tool_registry.execute(tool_call.function.name, arguments)
                is_error = result.startswith("ERROR:")
            except json.JSONDecodeError as exc:
                result = f"ERROR: invalid tool arguments JSON: {exc}"
                is_error = True
            except Exception as exc:
                result = f"ERROR: {exc}"
                is_error = True
            if tool_ctx is None:
                tool_ctx = ui.tool_call_start(tool_call.function.name, arguments)
            tool_ctx.result(result, is_error=is_error)
            tool_call_records.append(
                ToolCallRecord(
                    name=tool_call.function.name,
                    arguments=arguments,
                    result=result,
                    is_error=is_error,
                )
            )
            messages.append(
                {"role": "tool", "tool_call_id": tool_call.id, "content": result}
            )
        new_last_logged = len(messages)
        run_log.record_turn_delta(
            index=turn_index,
            messages_appended_delta=messages[messages_before:],
            response_text=choice.message.content,
            usage=response.usage.model_dump() if response.usage else {},
            tool_calls=tool_call_records,
        )
        ui.end_turn()
        return True, new_last_logged

    content = choice.message.content or ""
    if not content:
        ui.print("WARNING: coordinator returned empty response")
    messages.append({"role": "assistant", "content": content})
    new_last_logged = len(messages)
    run_log.record_turn_delta(
        index=turn_index,
        messages_appended_delta=messages[messages_before:],
        response_text=content,
        usage=response.usage.model_dump() if response.usage else {},
        tool_calls=[],
    )
    ui.end_turn()
    return False, new_last_logged


async def _chat_with_retry(
    client: "CoordinatorClient",
    messages: list[dict],
    tools: list[dict],
    config: "Config",
    token_callback: Callable[[str], None] | None = None,
    attempt: int = 0,
) -> "ChatResponse":
    retryable = {429, 500, 502, 503, 504}
    try:
        return await client.chat(
            messages,
            tools=tools,
            stream=token_callback is not None,
            token_callback=token_callback,
        )
    except APIStatusError as exc:
        if exc.status_code in retryable:
            if attempt < config.max_retries:
                await asyncio.sleep(2**attempt)
                return await _chat_with_retry(
                    client,
                    messages,
                    tools,
                    config,
                    token_callback=token_callback,
                    attempt=attempt + 1,
                )
            raise MaxRetriesExceeded(str(exc)) from exc
        raise
