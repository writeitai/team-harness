from datetime import datetime
from datetime import timezone
import json
from pathlib import Path

from team_harness.tracking.models import AgentRecord
from team_harness.tracking.models import RunRecord
from team_harness.tracking.models import ToolCallRecord
from team_harness.tracking.models import TurnRecord


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
        self._flush()

    def record_agent_spawn(self, record: AgentRecord) -> None:
        self._log.agents.append(record)
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
