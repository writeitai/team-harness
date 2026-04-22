import asyncio
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from pathlib import Path


@dataclass
class AgentState:
    id: str
    agent_type: str
    prompt: str
    cwd: str
    proc: asyncio.subprocess.Process
    spawn_time: datetime
    stdout_log: Path
    stderr_log: Path
    session_id: str | None = None
    status: str = "running"
    exit_code: int | None = None
    finished_at: datetime | None = None
    failure_classification: dict | None = None


class AgentManager:
    def __init__(self) -> None:
        self._agents: dict[str, AgentState] = {}

    def register(self, state: AgentState) -> None:
        self._agents[state.id] = state

    def get(self, agent_id: str) -> AgentState:
        return self._agents[agent_id]

    def list_all(self) -> list[AgentState]:
        return list(self._agents.values())

    def running_count(self) -> int:
        return sum(1 for state in self._agents.values() if state.status == "running")

    def poll_exit_codes(self) -> None:
        for state in self._agents.values():
            if state.exit_code is not None:
                continue
            if state.proc.returncode is None:
                continue
            state.exit_code = state.proc.returncode
            state.finished_at = datetime.now(timezone.utc)
            if state.status == "running":
                state.status = "done" if state.exit_code == 0 else "failed"

    async def wait_one(self, agent_id: str) -> int:
        state = self.get(agent_id)
        exit_code = await state.proc.wait()
        state.exit_code = exit_code
        state.finished_at = datetime.now(timezone.utc)
        if state.status != "killed":
            state.status = "done" if exit_code == 0 else "failed"
        return exit_code

    async def wait_for(self, agent_ids: list[str] | None = None) -> dict[str, int]:
        ids = agent_ids if agent_ids is not None else list(self._agents)
        results = await asyncio.gather(*(self.wait_one(agent_id) for agent_id in ids))
        return dict(zip(ids, results, strict=True))

    def kill(self, agent_id: str) -> None:
        state = self._agents[agent_id]
        if state.proc.returncode is not None:
            return
        try:
            state.proc.terminate()
            state.status = "killed"
            state.finished_at = datetime.now(timezone.utc)
        except ProcessLookupError:
            pass
