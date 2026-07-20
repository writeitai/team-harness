from datetime import datetime
from datetime import timezone
import os
from pathlib import Path
import re
from typing import Any

from team_harness.agents.process_identity import capture_starttime
from team_harness.tracking.models import AgentRecord
from team_harness.tracking.models import CoordinatorRetryRecord
from team_harness.tracking.models import RateLimitedFamilyRecord
from team_harness.tracking.models import RunFailureRecord
from team_harness.tracking.models import RunRecord
from team_harness.tracking.models import ToolCallRecord
from team_harness.tracking.models import TurnRecord
from team_harness.tracking.persistence import write_json_atomic

_AGENT_ID_RE = re.compile(r"^agent_[a-zA-Z0-9]+$")
_MAX_COORDINATOR_RETRY_RECORDS = 100
_DEFAULT_TOOL_RESULT_MAX_BYTES = 8192


def _truncate_persisted_text(text: str, max_bytes: int) -> str:
    """Truncate a string to `max_bytes` UTF-8 bytes for run.json persistence,
    appending a note. The full stream stays in the worker logs on disk."""

    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    kept = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return (
        f"{kept}\n[... truncated {len(encoded) - max_bytes} of {len(encoded)} "
        "bytes for run.json persistence; full stream in the worker log ...]"
    )


def _truncate_delta_for_persistence(
    messages_appended_delta: list[dict], max_bytes: int
) -> list[dict]:
    """Copy the turn delta, truncating tool-role message contents. Untouched
    messages keep their original reference so the live message list is never
    mutated."""

    persisted: list[dict] = []
    for message in messages_appended_delta:
        content = message.get("content")
        if message.get("role") == "tool" and isinstance(content, str):
            truncated = _truncate_persisted_text(content, max_bytes)
            if truncated != content:
                persisted.append({**message, "content": truncated})
                continue
        persisted.append(message)
    return persisted


def _truncate_tool_calls_for_persistence(
    tool_calls: list[ToolCallRecord], max_bytes: int
) -> list[ToolCallRecord]:
    """Copy tool-call records, truncating each result string for persistence."""

    persisted: list[ToolCallRecord] = []
    for tool_call in tool_calls:
        truncated = _truncate_persisted_text(tool_call.result, max_bytes)
        if truncated == tool_call.result:
            persisted.append(tool_call)
        else:
            persisted.append(tool_call.model_copy(update={"result": truncated}))
    return persisted


class RunLogWriter:
    path: Path

    def __init__(
        self,
        run_id: str,
        run_dir: Path,
        provider: str,
        model: str,
        api_base: str,
        session_output_dir: str | None = None,
        caller_context: dict[str, Any] | None = None,
        capabilities: list[str] | None = None,
        coordinator_input_path: str | None = None,
        tool_result_max_bytes: int = _DEFAULT_TOOL_RESULT_MAX_BYTES,
    ) -> None:
        """Create and immediately persist the durable record for one run."""

        self.path = run_dir / "run.json"
        self._tool_result_max_bytes = tool_result_max_bytes
        parent_pid = os.getpid()
        self._log = RunRecord(
            run_id=run_id,
            start=datetime.now(timezone.utc),
            provider=provider,
            coordinator_model=model,
            api_base=api_base,
            session_output_dir=session_output_dir,
            # Recorded so a recovery process can verify whether this run's
            # owner is still alive before reaping its workers (TH-D5).
            parent_pid=parent_pid,
            parent_starttime=capture_starttime(pid=parent_pid),
            caller_context=caller_context,
            capabilities=capabilities or [],
            coordinator_input_path=coordinator_input_path,
        )
        self._flush()

    @property
    def run_id(self) -> str:
        return self._log.run_id

    @property
    def error(self) -> str | None:
        return self._log.error

    def snapshot_failure(self) -> RunFailureRecord | None:
        return self._log.failure.model_copy(deep=True) if self._log.failure else None

    def record_turn_delta(
        self,
        *,
        index: int,
        messages_appended_delta: list[dict],
        response_text: str | None,
        usage: dict,
        tool_calls: list[ToolCallRecord] | None = None,
    ) -> None:
        # Persistence-only truncation: the live coordinator `messages` list is
        # never touched, only the copies written to run.json. The full stream
        # already exists once on disk in the worker logs.
        persisted_delta = _truncate_delta_for_persistence(
            messages_appended_delta, self._tool_result_max_bytes
        )
        persisted_tool_calls = _truncate_tool_calls_for_persistence(
            tool_calls or [], self._tool_result_max_bytes
        )
        self._log.turns.append(
            TurnRecord(
                index=index,
                messages_appended_delta=persisted_delta,
                response_text=response_text,
                usage=usage,
                tool_calls=persisted_tool_calls,
            )
        )
        # Agent-id detection runs on the original (untruncated) tool calls;
        # spawn_agent results are short ids that never hit the byte ceiling.
        for tool_call in tool_calls or []:
            if tool_call.name != "spawn_agent" or tool_call.is_error:
                continue
            agent_id = tool_call.result.strip()
            if not _AGENT_ID_RE.fullmatch(agent_id):
                continue
            self.set_agent_turn(agent_id=agent_id, turn_index=index, flush=False)
        self._flush()

    def record_agent_spawn(self, record: AgentRecord) -> None:
        self._log.agents.append(record)
        self._flush()

    def snapshot_agents(self) -> list[AgentRecord]:
        return [record.model_copy(deep=True) for record in self._log.agents]

    def set_agent_turn(
        self, agent_id: str, turn_index: int, *, flush: bool = True
    ) -> None:
        for record in self._log.agents:
            if record.id != agent_id or record.coordinator_turn_index is not None:
                continue
            record.coordinator_turn_index = turn_index
            break
        if flush:
            self._flush()

    def update_agent(
        self,
        agent_id: str,
        *,
        exit_code: int | None = None,
        finished_at: datetime | None = None,
        status: str | None = None,
        session_id: str | None = None,
    ) -> None:
        for record in self._log.agents:
            if record.id == agent_id:
                if exit_code is not None:
                    record.exit_code = exit_code
                if finished_at is not None:
                    record.finished_at = finished_at
                if status is not None:
                    record.status = status
                if session_id is not None:
                    record.session_id = session_id
                break
        self._flush()

    def record_coordinator_retry(self, record: CoordinatorRetryRecord) -> None:
        self._log.coordinator_retries.append(record)
        if len(self._log.coordinator_retries) > _MAX_COORDINATOR_RETRY_RECORDS:
            self._log.coordinator_retries = self._log.coordinator_retries[
                -_MAX_COORDINATOR_RETRY_RECORDS:
            ]
        self._flush()

    def record_rate_limited_family(self, record: RateLimitedFamilyRecord) -> None:
        """Append one observed worker-family circuit interval to run.json."""

        self._log.rate_limited_families.append(record)
        self._flush()

    def update_api_base(self, api_base: str) -> None:
        """Record the effective client endpoint after client construction."""

        self._log.api_base = api_base
        self._flush()

    def finalize(
        self, error: str | None = None, failure: RunFailureRecord | None = None
    ) -> None:
        if self._log.end is None:
            self._log.end = datetime.now(timezone.utc)
        if error and not self._log.error:
            self._log.error = error
        if failure is not None and self._log.failure is None:
            self._log.failure = failure
        self._flush()

    def _flush(self) -> None:
        """Atomically persist the current in-memory run snapshot."""

        # Atomic write: run.json is the crash-durable record of what this run
        # launched (TH-D5 reads it to reap orphans), so a crash mid-write must
        # never leave it truncated. The temp name is unique per write so a
        # concurrent writer (e.g. a forced reap) cannot race on one path.
        write_json_atomic(path=self.path, payload=self._log.model_dump(mode="json"))
