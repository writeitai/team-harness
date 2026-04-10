from __future__ import annotations

import asyncio
import json
from pathlib import Path

from team_harness.agents.template import AgentTemplate
from team_harness.agents.template import template_uses_generated_uuid


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
    for raw in stdout_bytes.splitlines():
        try:
            event = json.loads(raw)
        except Exception:
            continue
        if not isinstance(event, dict):
            continue
        if not all(event.get(key) == value for key, value in capture.match.items()):
            continue
        value: object = event
        for key in capture.field_path:
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(key)
        if isinstance(value, str):
            return value
    return None


async def capture_session_id_from_path(
    *,
    stdout_path: Path,
    template: AgentTemplate,
    pre_generated_uuid: str | None,
    stop_event: asyncio.Event,
    max_bytes: int = 4096,
    max_wait_s: float = 30.0,
    poll_interval_s: float = 0.1,
) -> str | None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_wait_s

    def _read_prefix() -> bytes:
        if not stdout_path.exists():
            return b""
        with stdout_path.open("rb") as handle:
            return handle.read(max_bytes)

    while not stop_event.is_set() and loop.time() < deadline:
        stdout_bytes = await asyncio.to_thread(_read_prefix)
        if stdout_bytes:
            session_id = extract_session_id(template, stdout_bytes, pre_generated_uuid)
            if session_id is not None:
                return session_id
        await asyncio.sleep(poll_interval_s)
    return None
