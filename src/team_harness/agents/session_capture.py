from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from team_harness.agents.template import AgentTemplate
from team_harness.agents.template import template_uses_generated_uuid


def _event_matches(event: dict[str, object], match: dict[str, str]) -> bool:
    """Return whether a JSONL event satisfies the configured equality match."""

    return all(event.get(key) == value for key, value in match.items())


def _value_at_path(event: dict[str, object], field_path: tuple[str, ...]) -> str | None:
    """Extract a nested string value from a parsed JSON event."""

    value: object = event
    for key in field_path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value if isinstance(value, str) else None


def _template_command_name(template: AgentTemplate) -> str:
    """Return the executable basename for provider-specific capture rules."""

    if not template.command:
        return ""
    return os.path.basename(template.command[0])


def _claude_session_id_fallback(
    template: AgentTemplate, event: dict[str, object], field_path: tuple[str, ...]
) -> str | None:
    """Claude Code may emit the reusable session id in final result records.

    The startup `system/init` record is the preferred capture source, but real
    stream-json logs can place an equally authoritative top-level `session_id`
    on later `assistant` or `result` events. This keeps Claude resumable when
    the init record is absent from the scanned window or not emitted by the CLI
    version in use.
    """

    if _template_command_name(template) != "claude":
        return None
    if field_path != ("session_id",):
        return None
    if event.get("type") not in {"system", "assistant", "result"}:
        return None
    return _value_at_path(event, field_path)


def extract_session_id(
    template: AgentTemplate, stdout_bytes: bytes, pre_generated_uuid: str | None
) -> str | None:
    if pre_generated_uuid is not None and template_uses_generated_uuid(template):
        return pre_generated_uuid
    capture = template.session_capture
    if capture is None or capture.strategy != "stream_json_event":
        return None
    if capture.match is None or capture.field_path is None:
        return None
    fallback_session_id: str | None = None
    for raw in stdout_bytes.splitlines():
        try:
            event = json.loads(raw)
        except Exception:
            continue
        if not isinstance(event, dict):
            continue
        if _event_matches(event, capture.match):
            value = _value_at_path(event, capture.field_path)
            if value is not None:
                return value
        if fallback_session_id is None:
            fallback_session_id = _claude_session_id_fallback(
                template, event, capture.field_path
            )
    return fallback_session_id


def _read_prefix_and_tail(path: Path, max_bytes: int) -> bytes:
    """Read log windows likely to contain startup and final session events."""

    if not path.exists():
        return b""
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size <= max_bytes * 2:
            return handle.read()
        prefix = handle.read(max_bytes)
        handle.seek(max(0, size - max_bytes))
        tail = handle.read(max_bytes)
    return prefix + b"\n" + tail


async def capture_session_id_from_path(
    *,
    stdout_path: Path,
    template: AgentTemplate,
    pre_generated_uuid: str | None,
    stop_event: asyncio.Event,
    max_bytes: int = 65536,
    max_wait_s: float = 30.0,
    poll_interval_s: float = 0.1,
) -> str | None:
    """Poll a worker stdout log until a provider session id is available.

    Some providers emit session metadata immediately, while Claude Code can
    emit the reusable session id in the final result event. The caller should
    set `stop_event` when the worker exits so this function performs a final
    tail scan instead of giving up after a startup-only window.
    """

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_wait_s

    async def _scan_once() -> str | None:
        """Scan the current prefix/tail snapshot of the worker log."""

        stdout_bytes = await asyncio.to_thread(
            _read_prefix_and_tail, stdout_path, max_bytes
        )
        if not stdout_bytes:
            return None
        return extract_session_id(template, stdout_bytes, pre_generated_uuid)

    while loop.time() < deadline:
        session_id = await _scan_once()
        if session_id is not None:
            return session_id
        if stop_event.is_set():
            break
        await asyncio.sleep(poll_interval_s)
    return await _scan_once()
