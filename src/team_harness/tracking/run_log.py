from datetime import datetime
from datetime import timezone
import os
from pathlib import Path
import re
from typing import Any

from team_harness.agents.process_identity import capture_starttime
from team_harness.tracking.models import AgentRecord
from team_harness.tracking.models import CoordinatorRetryRecord
from team_harness.tracking.models import RunFailureRecord
from team_harness.tracking.models import RunRecord
from team_harness.tracking.models import ToolCallRecord
from team_harness.tracking.models import TurnRecord
from team_harness.tracking.persistence import write_json_atomic

_AGENT_ID_RE = re.compile(r"^agent_[a-zA-Z0-9]+$")
_MAX_COORDINATOR_RETRY_RECORDS = 100


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
    ) -> None:
        """Create and immediately persist the durable record for one run."""

        self.path = run_dir / "run.json"
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
        self._log.turns.append(
            TurnRecord(
                index=index,
                messages_appended_delta=messages_appended_delta,
                response_text=response_text,
                usage=usage,
                tool_calls=tool_calls or [],
            )
        )
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
