from datetime import datetime
from datetime import timezone
import json
from pathlib import Path
import re

from team_harness.tracking.models import AgentRecord
from team_harness.tracking.models import RunRecord
from team_harness.tracking.models import ToolCallRecord
from team_harness.tracking.models import TurnRecord

_AGENT_ID_RE = re.compile(r"^agent_[a-zA-Z0-9]+$")


class RunLogWriter:
    path: Path

    def __init__(
        self, run_id: str, run_dir: Path, provider: str, model: str, api_base: str
    ) -> None:
        self.path = run_dir / "run.json"
        self._log = RunRecord(
            run_id=run_id,
            start=datetime.now(timezone.utc),
            provider=provider,
            coordinator_model=model,
            api_base=api_base,
        )
        self._flush()

    @property
    def run_id(self) -> str:
        return self._log.run_id

    @property
    def error(self) -> str | None:
        return self._log.error

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
        exit_code: int,
        finished_at: datetime,
        status: str | None = None,
    ) -> None:
        for record in self._log.agents:
            if record.id == agent_id:
                record.exit_code = exit_code
                record.finished_at = finished_at
                if status is not None:
                    record.status = status
                break
        self._flush()

    def finalize(self, error: str | None = None) -> None:
        if self._log.end is None:
            self._log.end = datetime.now(timezone.utc)
        if error and not self._log.error:
            self._log.error = error
        self._flush()

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._log.model_dump(mode="json"), indent=2))
