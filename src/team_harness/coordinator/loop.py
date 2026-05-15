import asyncio
from collections.abc import Callable
from datetime import datetime
from datetime import timezone
import json
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from openai import APIStatusError

from team_harness.coordinator.client import CoordinatorAPIError
from team_harness.tracking.context import get_auto_compact_threshold
from team_harness.tracking.models import CoordinatorRetryRecord
from team_harness.tracking.models import RunFailureRecord
from team_harness.tracking.models import ToolCallRecord as RunLogToolCallRecord

if TYPE_CHECKING:
    from team_harness.config import Config
    from team_harness.coordinator.client import ChatResponse
    from team_harness.coordinator.protocols import CoordinatorLike
    from team_harness.tools.registry import ToolRegistry
    from team_harness.tracking.context import ContextTracker
    from team_harness.tracking.run_log import RunLogWriter
    from team_harness.ui.console import ConsoleBase

_COMPACT_BOUNDARY_TEXT = (
    "The earlier conversation history was compacted for context management.\n"
    "Treat the next user message as an authoritative summary of the removed history.\n"
    "Continue from it without mentioning the compaction unless the user asks."
)
_COMPACT_SUMMARY_PREFIX = "Compact summary of earlier conversation:\n\n"
_COMPACTION_SYSTEM_PROMPT = """You are compacting a coding-session transcript for continuation in the same harness.
Write a faithful continuation summary for the next model call.

Rules:
- Do not invent work, files, edits, commands, test results, tool outputs, or decisions.
- Preserve the user's goal, constraints, decisions, current implementation state, important commands, important outputs, errors, and remaining work.
- If something is uncertain, say that it is uncertain.
- Preserve exact filenames, commands, errors, branch names, environment details, and pending tasks whenever they still matter.
- Preserve the state of any spawned workers or agents if the transcript mentions them, including type, status, cwd, and last meaningful output.
- Ensure the summary is significantly smaller than the original transcript.
- Target under 10k tokens.
- Keep the summary dense and implementation-focused.
- Do not address the user.
- Do not ask follow-up questions.
- In "Active files & recent edits", write one bullet per file: filename plus a brief description of what changed.
- Output Markdown with these sections exactly:
  1. Goal
  2. Decisions
  3. Current state
  4. Outstanding work
  5. Architectural constraints & technical context
  6. Active files & recent edits
"""
_COMPACTION_FAILURE_WARNING = "Auto-compaction failed; continuing without compaction."
_COMPACTION_BREAKER_WARNING = (
    "Auto-compaction disabled for this session after 3 failures. "
    "Use /clear to reset context."
)


class MaxRetriesExceeded(RuntimeError):
    """Sentinel raised when retriable API failures exhaust the retry budget."""

    def __init__(self, message: str, *, failure: RunFailureRecord) -> None:
        super().__init__(message)
        self.failure = failure


async def run(
    messages: list[dict],
    config: "Config",
    run_log: "RunLogWriter",
    ui: "ConsoleBase",
    tool_registry: "ToolRegistry",
    client: "CoordinatorLike",
    ctx: "ContextTracker",
) -> None:
    turn_index = 0
    last_logged_index = 0
    while True:
        should_continue, last_logged_index = await run_one_turn(
            messages=messages,
            config=config,
            run_log=run_log,
            ui=ui,
            tool_registry=tool_registry,
            client=client,
            ctx=ctx,
            turn_index=turn_index,
            last_logged_index=last_logged_index,
        )
        turn_index += 1
        if not should_continue:
            return


async def run_one_turn(
    messages: list[dict],
    config: "Config",
    run_log: "RunLogWriter",
    ui: "ConsoleBase",
    tool_registry: "ToolRegistry",
    client: "CoordinatorLike",
    ctx: "ContextTracker",
    turn_index: int,
    last_logged_index: int,
) -> tuple[bool, int]:
    ui.begin_turn(turn_index)
    messages_before = last_logged_index
    threshold = get_auto_compact_threshold(ctx.model_id, ctx.model_limit)
    if _should_compact(messages=messages, ctx=ctx, threshold=threshold):
        compacted = await _perform_compaction(
            messages=messages, client=client, ctx=ctx, ui=ui
        )
        if compacted:
            messages_before = 0

    tools = tool_registry.get_all_schemas()
    ui.begin_streaming()
    try:
        response = await _chat_with_retry(
            client=client,
            messages=messages,
            tools=tools,
            config=config,
            run_log=run_log,
            token_callback=ui.stream_token,
        )
    except MaxRetriesExceeded as exc:
        ui.end_streaming()
        ui.print(f"API error (retries exhausted): {exc}")
        run_log.finalize(error=str(exc), failure=exc.failure)
        return False, last_logged_index
    except CoordinatorAPIError as exc:
        ui.end_streaming()
        if exc.status_code is None:
            ui.print(f"API error: {exc}")
        else:
            ui.print(f"API error {exc.status_code}: {exc}")
        run_log.finalize(
            error=str(exc),
            failure=_coordinator_failure_record(
                client=client, config=config, exc=exc, retry_attempts=1
            ),
        )
        return False, last_logged_index
    except APIStatusError as exc:
        ui.end_streaming()
        ui.print(f"API error {exc.status_code}: {exc.message}")
        run_log.finalize(
            error=str(exc),
            failure=_api_status_failure_record(
                client=client, config=config, exc=exc, retry_attempts=1
            ),
        )
        return False, last_logged_index
    ui.end_streaming()

    if response.usage:
        ctx.update(response.usage)
        if ctx.at_warning and not ctx.at_warning_emitted:
            ui.context_warning()
            ctx.at_warning_emitted = True
    elif not ctx.usage_warning_emitted:
        ui.print(
            "WARNING: coordinator response omitted usage; exact context tracking is unavailable for this response."
        )
        ctx.usage_warning_emitted = True

    choice = response.choices[0]
    tool_calls = choice.message.tool_calls or []
    if tool_calls:
        assistant_msg = {
            "role": "assistant",
            "content": choice.message.content,
            "tool_calls": [tool_call.model_dump() for tool_call in tool_calls],
        }
        messages.append(assistant_msg)
        tool_call_records: list[RunLogToolCallRecord] = []
        for tool_call in tool_calls:
            arguments = {}
            tool_ctx = None
            try:
                arguments = json.loads(tool_call.function.arguments)
                tool_ctx = ui.tool_call_start(
                    name=tool_call.function.name, args=arguments
                )
                result = await tool_registry.execute(
                    name=tool_call.function.name, arguments=arguments
                )
                is_error = result.startswith("ERROR:")
            except json.JSONDecodeError as exc:
                result = f"ERROR: invalid tool arguments JSON: {exc}"
                is_error = True
            except Exception as exc:
                result = f"ERROR: {exc}"
                is_error = True
            if tool_ctx is None:
                tool_ctx = ui.tool_call_start(
                    name=tool_call.function.name, args=arguments
                )
            tool_ctx.result(result, is_error=is_error)
            tool_call_records.append(
                RunLogToolCallRecord(
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


def _should_compact(
    messages: list[dict], ctx: "ContextTracker", threshold: int
) -> bool:
    if ctx.breaker_tripped:
        return False
    if not messages or messages[-1]["role"] != "user":
        return False
    # Falls back to estimate if API usage is unavailable
    return ctx.total >= threshold


async def _perform_compaction(
    messages: list[dict],
    client: "CoordinatorLike",
    ctx: "ContextTracker",
    ui: "ConsoleBase",
) -> bool:
    assert messages and messages[-1]["role"] == "user"
    before_tokens = ctx.prompt_tokens + ctx.completion_tokens
    after_tokens = before_tokens
    try:
        ui.begin_compaction()
    except Exception:
        pass
    try:
        original_system = messages[0]
        pending_user = messages[-1]
        rendered_transcript = _render_transcript(messages[1:-1])
        try:
            response = await client.chat(
                messages=[
                    {"role": "system", "content": _COMPACTION_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            "Summarize the following conversation so the same model "
                            "can continue the work after the earlier history is removed.\n"
                            "Preserve exact filenames, commands, errors, decisions, "
                            "constraints, and pending tasks whenever they still matter.\n\n"
                            "Conversation transcript:\n\n"
                            f"{rendered_transcript}"
                        ),
                    },
                ],
                tools=[],
                stream=False,
                token_callback=None,
            )
        except Exception:
            _record_compaction_failure(ctx=ctx, ui=ui)
            return False
        content = response.choices[0].message.content
        if content is None or not content.strip():
            _record_compaction_failure(ctx=ctx, ui=ui)
            return False
        replacement_messages = _build_summary_messages(
            original_system=original_system,
            summary_text=content.strip(),
            pending_user=pending_user,
        )
        messages[:] = replacement_messages
        ctx.consecutive_compact_failures = 0
        ctx.breaker_tripped = False
        after_tokens = _approximate_tokens(messages)
        return True
    finally:
        try:
            ui.end_compaction(before_tokens, after_tokens)
        except Exception:
            pass


def _record_manual_compaction_failure(ctx: "ContextTracker", ui: "ConsoleBase") -> None:
    ctx.consecutive_compact_failures += 1
    if ctx.consecutive_compact_failures == 3:
        ctx.breaker_tripped = True
        ui.print(_COMPACTION_BREAKER_WARNING)


def _build_manual_summary_messages(
    original_system: dict, summary_text: str
) -> list[dict]:
    return [
        original_system,
        {"role": "system", "content": _COMPACT_BOUNDARY_TEXT},
        {"role": "user", "content": _COMPACT_SUMMARY_PREFIX + summary_text},
    ]


async def _perform_manual_compaction(
    messages: list[dict],
    client: "CoordinatorLike",
    ctx: "ContextTracker",
    ui: "ConsoleBase",
    focus_text: str | None = None,
) -> bool:
    non_system_messages = sum(
        1 for message in messages if message.get("role") != "system"
    )
    if non_system_messages < 2:
        ui.print("Nothing to compact yet. Need at least 2 non-system messages.")
        return False

    before_tokens = ctx.prompt_tokens + ctx.completion_tokens
    after_tokens = before_tokens
    success = False
    ui.begin_compaction()
    try:
        rendered_transcript = _render_transcript(messages[1:])
        request_content = (
            "Summarize the following conversation so the same model "
            "can continue the work after the earlier history is removed.\n"
            "Preserve exact filenames, commands, errors, decisions, "
            "constraints, and pending tasks whenever they still matter.\n\n"
            "Conversation transcript:\n\n"
            f"{rendered_transcript}"
        )
        trimmed_focus_text = focus_text.strip() if focus_text is not None else None
        if trimmed_focus_text:
            capped_focus_text = trimmed_focus_text
            if len(capped_focus_text) > 2000:
                capped_focus_text = (
                    capped_focus_text[:2000] + "\n[... truncated to 2000 chars ...]"
                )
            request_content += (
                f"\n\nAdditional focus from the user:\n{capped_focus_text}"
            )
        try:
            response = await client.chat(
                messages=[
                    {"role": "system", "content": _COMPACTION_SYSTEM_PROMPT},
                    {"role": "user", "content": request_content},
                ],
                tools=[],
                stream=False,
                token_callback=None,
            )
        except CoordinatorAPIError as exc:
            if exc.status_code is None:
                ui.print(f"Manual compaction failed: {exc}")
            else:
                ui.print(
                    f"Manual compaction failed: API error {exc.status_code}: {exc}"
                )
            _record_manual_compaction_failure(ctx=ctx, ui=ui)
            return False
        except APIStatusError as exc:
            ui.print(
                f"Manual compaction failed: API error {exc.status_code}: {exc.message}"
            )
            _record_manual_compaction_failure(ctx=ctx, ui=ui)
            return False
        except Exception as exc:
            ui.print(f"Manual compaction failed: {exc}")
            _record_manual_compaction_failure(ctx=ctx, ui=ui)
            return False

        content = response.choices[0].message.content
        if content is None or not content.strip():
            ui.print("Manual compaction failed: summarizer returned an empty response.")
            _record_manual_compaction_failure(ctx=ctx, ui=ui)
            return False
        replacement = _build_manual_summary_messages(messages[0], content.strip())
        messages[:] = replacement
        ctx.consecutive_compact_failures = 0
        ctx.breaker_tripped = False
        ctx.set_estimated_total(messages)
        after_tokens = _approximate_tokens(messages)
        success = True
        return True
    finally:
        try:
            ui.end_compaction(before_tokens, after_tokens, success=success)
        except Exception:
            pass


def _record_compaction_failure(ctx: "ContextTracker", ui: "ConsoleBase") -> None:
    ctx.consecutive_compact_failures += 1
    if ctx.consecutive_compact_failures >= 3:
        ctx.breaker_tripped = True
        ui.print(_COMPACTION_BREAKER_WARNING)
        return
    ui.print(_COMPACTION_FAILURE_WARNING)


def _build_summary_messages(
    original_system: dict, summary_text: str, pending_user: dict
) -> list[dict]:
    return [
        original_system,
        {"role": "system", "content": _COMPACT_BOUNDARY_TEXT},
        {"role": "user", "content": _COMPACT_SUMMARY_PREFIX + summary_text},
        pending_user,
    ]


def _render_transcript(messages: list[dict]) -> str:
    rendered_messages: list[str] = []
    for message in messages:
        lines = [f"role: {message.get('role', '')}"]
        if "tool_call_id" in message:
            lines.append(f"tool_call_id: {message['tool_call_id']}")
        if "tool_calls" in message:
            lines.append(
                "tool_calls: "
                + json.dumps(message["tool_calls"], sort_keys=True, ensure_ascii=True)
            )
        content = message.get("content")
        if content is not None:
            lines.append(str(content))
        rendered_messages.append("\n".join(lines))
    return "\n\n".join(rendered_messages)


def _approximate_tokens(messages: list[dict]) -> int:
    chars = 0
    for message in messages:
        role = message.get("role")
        if isinstance(role, str):
            chars += len(role)
        content = message.get("content")
        if isinstance(content, str):
            chars += len(content)
        tool_calls = message.get("tool_calls")
        if tool_calls:
            chars += len(json.dumps(tool_calls, sort_keys=True))
        tool_call_id = message.get("tool_call_id")
        if isinstance(tool_call_id, str):
            chars += len(tool_call_id)
    return max(1, chars // 4)


async def _chat_with_retry(
    client: "CoordinatorLike",
    messages: list[dict],
    tools: list[dict],
    config: "Config",
    run_log: "RunLogWriter | None" = None,
    token_callback: Callable[[str], None] | None = None,
    attempt: int = 0,
) -> "ChatResponse":
    retryable = {429, 500, 502, 503, 504}
    try:
        return await client.chat(
            messages=messages,
            tools=tools,
            stream=token_callback is not None,
            token_callback=token_callback,
        )
    except CoordinatorAPIError as exc:
        if exc.retryable:
            _record_retry(
                run_log=run_log, client=client, config=config, attempt=attempt, exc=exc
            )
            if attempt < config.max_retries:
                await asyncio.sleep(
                    _retry_sleep_seconds(config=config, attempt=attempt)
                )
                return await _chat_with_retry(
                    client=client,
                    messages=messages,
                    tools=tools,
                    config=config,
                    run_log=run_log,
                    token_callback=token_callback,
                    attempt=attempt + 1,
                )
            raise MaxRetriesExceeded(
                str(exc),
                failure=_coordinator_failure_record(
                    client=client, config=config, exc=exc, retry_attempts=attempt + 1
                ),
            ) from exc
        raise
    except APIStatusError as exc:
        if exc.status_code in retryable:
            _record_retry(
                run_log=run_log, client=client, config=config, attempt=attempt, exc=exc
            )
            if attempt < config.max_retries:
                await asyncio.sleep(
                    _retry_sleep_seconds(config=config, attempt=attempt)
                )
                return await _chat_with_retry(
                    client=client,
                    messages=messages,
                    tools=tools,
                    config=config,
                    run_log=run_log,
                    token_callback=token_callback,
                    attempt=attempt + 1,
                )
            raise MaxRetriesExceeded(
                str(exc),
                failure=_api_status_failure_record(
                    client=client, config=config, exc=exc, retry_attempts=attempt + 1
                ),
            ) from exc
        raise


def _retry_sleep_seconds(*, config: "Config", attempt: int) -> float:
    return min(config.retry_max_delay_s, config.retry_base_delay_s * (2**attempt))


def _client_provider(client: "CoordinatorLike", config: "Config") -> str:
    return str(getattr(client, "provider", config.provider))


def _client_model(client: "CoordinatorLike", config: "Config") -> str:
    return str(getattr(client, "model", config.model))


def _client_api_base(client: "CoordinatorLike", config: "Config") -> str:
    return str(getattr(client, "api_base", config.api_base))


def _host_from_api_base(api_base: str) -> str | None:
    return urlparse(api_base).hostname


def _record_retry(
    *,
    run_log: "RunLogWriter | None",
    client: "CoordinatorLike",
    config: "Config",
    attempt: int,
    exc: CoordinatorAPIError | APIStatusError,
) -> None:
    if run_log is None:
        return
    will_retry = attempt < config.max_retries
    api_base = _client_api_base(client, config)
    if isinstance(exc, CoordinatorAPIError):
        error_type = exc.error_type
        cause_type = exc.cause_type
        status_code = exc.status_code
        retryable = exc.retryable
        message = str(exc)
        host = exc.host or _host_from_api_base(api_base)
    else:
        error_type = type(exc).__name__
        cause_type = type(exc).__name__
        status_code = exc.status_code
        retryable = exc.status_code == 429 or exc.status_code >= 500
        message = exc.message
        host = _host_from_api_base(api_base)
    run_log.record_coordinator_retry(
        CoordinatorRetryRecord(
            attempt=attempt + 1,
            max_retries=config.max_retries,
            will_retry=will_retry,
            sleep_seconds=_retry_sleep_seconds(config=config, attempt=attempt)
            if will_retry
            else None,
            provider=_client_provider(client, config),
            model=_client_model(client, config),
            api_base=api_base,
            host=host,
            error_type=error_type,
            cause_type=cause_type,
            status_code=status_code,
            retryable=retryable,
            message=message,
            recorded_at=datetime.now(timezone.utc),
        )
    )


def _coordinator_failure_record(
    *,
    client: "CoordinatorLike",
    config: "Config",
    exc: CoordinatorAPIError,
    retry_attempts: int,
) -> RunFailureRecord:
    api_base = _client_api_base(client, config)
    kind = "coordinator_auth" if exc.status_code in {401, 403} else "coordinator_api"
    return RunFailureRecord(
        kind=kind,
        message=str(exc),
        provider=_client_provider(client, config),
        model=_client_model(client, config),
        api_base=api_base,
        host=exc.host or _host_from_api_base(api_base),
        error_type=exc.error_type,
        cause_type=exc.cause_type,
        status_code=exc.status_code,
        retryable=exc.retryable,
        retry_attempts=retry_attempts,
        max_retries=config.max_retries,
    )


def _api_status_failure_record(
    *,
    client: "CoordinatorLike",
    config: "Config",
    exc: APIStatusError,
    retry_attempts: int,
) -> RunFailureRecord:
    api_base = _client_api_base(client, config)
    return RunFailureRecord(
        kind="coordinator_auth" if exc.status_code in {401, 403} else "coordinator_api",
        message=exc.message,
        provider=_client_provider(client, config),
        model=_client_model(client, config),
        api_base=api_base,
        host=_host_from_api_base(api_base),
        error_type=type(exc).__name__,
        cause_type=type(exc).__name__,
        status_code=exc.status_code,
        retryable=exc.status_code == 429 or exc.status_code >= 500,
        retry_attempts=retry_attempts,
        max_retries=config.max_retries,
    )
