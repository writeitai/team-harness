import asyncio
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from pathlib import Path
import signal

from team_harness.agents.process_identity import group_members
from team_harness.agents.process_identity import ProcessProbeError
from team_harness.agents.process_identity import signal_group


class FinalizationTimeoutError(TimeoutError):
    """Describe a bounded lifecycle phase that did not finish in time."""

    def __init__(
        self, *, phase: str, timeout_s: float, unfinished_task_count: int
    ) -> None:
        """Record the phase, effective bound, and unfinished task count."""

        self.phase = phase
        self.timeout_s = timeout_s
        self.unfinished_task_count = unfinished_task_count
        super().__init__(
            f"{phase} exceeded its {timeout_s:g}s finalization bound "
            f"with {unfinished_task_count} unfinished task(s)"
        )


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
    # Process-group id when spawned with start_new_session (pgid == pid); lets
    # shutdown kill the whole worker subtree, not just the leader (TH-D5).
    # None for test doubles / externally-constructed states → leader-only kill.
    pgid: int | None = None


class AgentManager:
    def __init__(self) -> None:
        """Initialize an empty worker registry and retained finalization set."""

        self._agents: dict[str, AgentState] = {}
        self._finalization_tasks: set[asyncio.Task[None]] = set()

    def register(self, state: AgentState) -> None:
        self._agents[state.id] = state

    def get(self, agent_id: str) -> AgentState:
        return self._agents[agent_id]

    def list_all(self) -> list[AgentState]:
        return list(self._agents.values())

    def running_count(self) -> int:
        return sum(1 for state in self._agents.values() if state.status == "running")

    def track_finalization_task(self, task: asyncio.Task[None]) -> None:
        """Keep worker watcher/session-capture tasks alive through run finalization."""

        self._finalization_tasks.add(task)

    async def await_finalization_tasks(
        self, *, timeout_s: float
    ) -> tuple[BaseException, ...]:
        """Await watcher/session-capture tasks within a shared time bound.

        A task failure is returned to the run finalizer instead of escaping from
        this method. If the shared deadline expires, unfinished tasks are
        cancelled and settled, and a phase-specific ``FinalizationTimeoutError``
        is returned. Only harness-owned watcher and capture tasks enter this
        registry; both are cancellation-cooperative once worker processes have
        been force-terminated by the run finalizer. The finalizer can therefore
        persist both final snapshots without leaving tasks for
        ``asyncio.run()`` teardown.
        """

        if timeout_s < 0:
            raise ValueError("timeout_s must be greater than or equal to zero")

        failures: list[BaseException] = []
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while self._finalization_tasks:
            tasks = tuple(self._finalization_tasks)
            remaining_s = max(0.0, deadline - loop.time())
            done, pending = await asyncio.wait(tasks, timeout=remaining_s)
            self._finalization_tasks.difference_update(done)
            for task in done:
                try:
                    failure = task.exception()
                except asyncio.CancelledError as exc:
                    failures.append(exc)
                else:
                    if failure is not None:
                        failures.append(failure)
            if not pending:
                continue

            # The run finalizer force-terminates unreaped worker groups before
            # entering this phase, so proc.wait() can complete. Cancel any
            # other overdue harness-owned capture work and fully settle it;
            # leaving tasks pending would merely move the wedge to
            # asyncio.run() teardown.
            overdue = set(pending)
            overdue.update(self._finalization_tasks)
            self._finalization_tasks.clear()
            unfinished_task_count = sum(not task.done() for task in overdue)
            for task in overdue:
                if not task.done():
                    task.cancel(msg="worker finalization deadline expired")
            results = await asyncio.gather(*overdue, return_exceptions=True)
            failures.extend(
                result
                for result in results
                if isinstance(result, BaseException)
                and not isinstance(result, asyncio.CancelledError)
            )
            failures.append(
                FinalizationTimeoutError(
                    phase="worker watcher/session-capture phase",
                    timeout_s=timeout_s,
                    unfinished_task_count=unfinished_task_count,
                )
            )
            break
        return tuple(failures)

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
        """Terminate a live worker execution group and mark it killed."""

        state = self._agents[agent_id]
        if state.proc.returncode is not None:
            return
        # Workers run in their own process group (TH-D5): TERM the whole group
        # when the pgid is trusted, so the worker CLI's own children get the
        # signal too. The leader-only terminate stays as belt-and-braces (and
        # as the sole path for states without a pgid, e.g. test doubles).
        if state.pgid is not None:
            signal_group(pgid=state.pgid, sig=signal.SIGTERM)
        try:
            state.proc.terminate()
        except ProcessLookupError:
            pass
        state.status = "killed"
        state.finished_at = datetime.now(timezone.utc)

    async def ensure_group_dead(
        self,
        agent_id: str,
        *,
        term_wait_s: float = 1.0,
        kill_wait_s: float = 2.0,
        poll_interval_s: float = 0.1,
    ) -> bool:
        """Best-effort: make sure no member of the worker's group survives.

        In-run counterpart of the post-crash reaper. The identity anchor here is
        the run's own lifetime: the group was created by this process via
        ``start_new_session`` and, while any member lives, the pgid cannot be
        recycled. (The residual window — the group fully emptying and the pgid
        being recycled by an unrelated new session *during this same run* — is
        accepted as negligible; the alternative is certainly leaking helpers.)

        Returns True when the group is verified gone, False when members might
        remain (including when the process table cannot be probed).
        """
        state = self._agents[agent_id]
        if state.pgid is None:
            return state.proc.returncode is not None

        async def _wait_members_gone(timeout_s: float) -> bool | None:
            """Poll until the group disappears, times out, or cannot be probed."""

            deadline = asyncio.get_event_loop().time() + timeout_s
            while True:
                try:
                    if not group_members(pgid=state.pgid):  # type: ignore[arg-type]
                        return True
                except ProcessProbeError:
                    return None
                if asyncio.get_event_loop().time() >= deadline:
                    return False
                await asyncio.sleep(poll_interval_s)

        try:
            members = group_members(pgid=state.pgid)
        except ProcessProbeError:
            return False
        if not members:
            return True
        # Polite TERM first — the sweep path (leader already exited, helpers
        # survive) reaches here without anyone having signalled the group yet.
        signal_group(pgid=state.pgid, sig=signal.SIGTERM)
        gone = await _wait_members_gone(term_wait_s)
        if gone is None:
            return False
        if gone:
            return True
        signal_group(pgid=state.pgid, sig=signal.SIGKILL)
        gone = await _wait_members_gone(kill_wait_s)
        return bool(gone)
