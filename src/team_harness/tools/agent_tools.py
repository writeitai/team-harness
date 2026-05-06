import asyncio
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING
import uuid

from team_harness.agents import spawner
from team_harness.agents.api_error_classifier import classify_agent_failure
from team_harness.agents.manager import AgentState
from team_harness.agents.registry import check_harness_depth
from team_harness.agents.session_capture import capture_session_id_from_path
from team_harness.agents.template import AgentTemplate
from team_harness.coordinator.system_prompt import DEFAULT_WORKER_FOOTER
from team_harness.tracking.models import AgentRecord
from team_harness.tracking.worker_sessions import resume_info_for_agent_type

if TYPE_CHECKING:
    from team_harness.agents.manager import AgentManager
    from team_harness.config import Config
    from team_harness.tracking.run_log import RunLogWriter
    from team_harness.ui.console import ConsoleBase

_manager: "AgentManager | None" = None
_run_log: "RunLogWriter | None" = None
_config: "Config | None" = None
_ui: "ConsoleBase | None" = None
_session_output_dir: str = ""

_output_cursors: dict[str, int] = {}
_output_locks: dict[str, asyncio.Lock] = {}
_wait_stdout_cursors: dict[str, int] = {}
_wait_stderr_cursors: dict[str, int] = {}


def _build_worker_output_footer(
    output_dir: str = "", config: "Config | None" = None
) -> str:
    template = (
        getattr(config, "worker_footer", DEFAULT_WORKER_FOOTER)
        if config
        else DEFAULT_WORKER_FOOTER
    )
    return template.format(session_output_dir=output_dir)


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def _tail_text(path: Path, n_chars: int) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as handle:
        size = path.stat().st_size
        handle.seek(max(0, size - n_chars * 2))
        return handle.read().decode(errors="replace")[-n_chars:]


def _classify_if_failed(state: AgentState) -> dict | None:
    """Compute and cache failure classification for a failed agent.

    Reads the tail of stderr and stdout and runs the API error classifier.
    Returns the cached dict if already classified, or None if the failure
    does not look like an API error.
    """
    if state.exit_code is None or state.exit_code == 0:
        return None
    if state.failure_classification is not None:
        return state.failure_classification
    stderr_text = _tail_text(state.stderr_log, 4000)
    stdout_text = _tail_text(state.stdout_log, 4000)
    result = classify_agent_failure(stderr_text, stdout_text)
    if result is not None:
        state.failure_classification = {
            "is_api_error": result.is_api_error,
            "category": result.category,
            "detail": result.detail,
            "suggested_action": (
                "Respawn this task with a different agent type. "
                "See the API Error Failover Protocol."
            ),
        }
    return state.failure_classification


def _seconds_since_last_output(stdout_log: Path, stderr_log: Path) -> float | None:
    mtimes: list[float] = []
    for path in (stdout_log, stderr_log):
        try:
            mtimes.append(path.stat().st_mtime)
        except FileNotFoundError:
            continue
    if not mtimes:
        return None
    return max(0.0, datetime.now(timezone.utc).timestamp() - max(mtimes))


def _build_running_snapshot(
    state: AgentState, *, advance_cursors: bool
) -> dict[str, object]:
    now = datetime.now(timezone.utc)
    elapsed = int((now - state.spawn_time).total_seconds())
    stdout_total = _file_size(state.stdout_log)
    stderr_total = _file_size(state.stderr_log)
    stdout_prev = _wait_stdout_cursors.get(state.id, 0)
    stderr_prev = _wait_stderr_cursors.get(state.id, 0)
    stdout_delta = max(0, stdout_total - stdout_prev)
    stderr_delta = max(0, stderr_total - stderr_prev)
    if advance_cursors:
        _wait_stdout_cursors[state.id] = stdout_total
        _wait_stderr_cursors[state.id] = stderr_total
    last_output_age = _seconds_since_last_output(state.stdout_log, state.stderr_log)
    is_alive = state.proc.returncode is None
    stderr_tail = _tail_text(state.stderr_log, 400)

    if not is_alive:
        advisory = "Process has exited. Read the full output with read_agent_output."
    elif stderr_delta > 0 or stdout_delta > 0:
        advisory = (
            f"HEALTHY — producing output. {stdout_delta} new stdout bytes and "
            f"{stderr_delta} new stderr bytes since last check. "
            "DO NOT kill. Re-enter wait_for_any."
        )
    elif last_output_age is not None and last_output_age < 120:
        advisory = (
            f"HEALTHY — output file was touched {int(last_output_age)}s ago. "
            "DO NOT kill. Re-enter wait_for_any."
        )
    else:
        advisory = (
            f"QUIET — no new output for {int(last_output_age) if last_output_age else -1}s. "
            "Investigate with read_agent_output before considering kill_agent."
        )

    snapshot: dict[str, object] = {
        "agent_id": state.id,
        "agent_type": state.agent_type,
        "status": _status_from_state(state),
        "elapsed_seconds": elapsed,
        "stdout_bytes_total": stdout_total,
        "stderr_bytes_total": stderr_total,
        "stdout_bytes_delta_since_last_check": stdout_delta,
        "stderr_bytes_delta_since_last_check": stderr_delta,
        "seconds_since_last_output": int(last_output_age)
        if last_output_age is not None
        else None,
        "is_alive": is_alive,
        "recent_stderr_tail": stderr_tail,
        "advisory": advisory,
    }
    if not is_alive:
        classification = _classify_if_failed(state)
        if classification is not None:
            snapshot["failure_classification"] = classification
    return snapshot


def _patience_policy(config: "Config | None") -> dict[str, object]:
    floor = float(
        getattr(config, "min_agent_lifetime_before_kill_s", 600.0) if config else 600.0
    )
    return {
        "min_wait_before_kill_seconds": floor,
        "stderr_growth_free_kill_window_seconds": 120,
        "typical_durations": {
            "codex_planning": "20-45 min",
            "gemini_research": "15-30 min",
            "claude_review": "5-15 min",
        },
        "rationale": (
            "timed_out=true means the agents are STILL RUNNING. This is the expected "
            "and normal outcome for any non-trivial task. Re-enter wait_for_any with "
            "a longer timeout. Only consider kill_agent if the agent has been quiet "
            "for >= 20 minutes AND you have read the full output and confirmed the "
            "trajectory is wrong."
        ),
    }


def _should_refuse_kill(
    state: AgentState, *, min_lifetime_s: float
) -> tuple[bool, dict[str, object]]:
    # A zero (or negative) floor disables the patience backstop entirely —
    # this is the escape hatch for tests and users who need legacy behavior.
    if min_lifetime_s <= 0:
        snapshot = _build_running_snapshot(state, advance_cursors=False)
        return False, snapshot
    snapshot = _build_running_snapshot(state, advance_cursors=False)
    elapsed = int(snapshot["elapsed_seconds"])  # type: ignore[arg-type]
    age = snapshot["seconds_since_last_output"]
    stderr_delta = int(snapshot["stderr_bytes_delta_since_last_check"])  # type: ignore[arg-type]
    too_young = elapsed < min_lifetime_s
    actively_producing = (isinstance(age, int) and age < 120) or stderr_delta > 0
    if too_young:
        reason = (
            f"Agent is only {elapsed}s old (floor is {int(min_lifetime_s)}s). "
            "Re-enter wait_for_any and inspect stderr/stdout before escalating. "
            "If stderr is growing, the agent is healthy — continue waiting."
        )
        return True, {
            "killed": False,
            "refused": True,
            "reason": reason,
            "snapshot": snapshot,
        }
    if actively_producing:
        reason = (
            f"Agent produced output very recently (age={age}s, "
            f"stderr_delta={stderr_delta}). It is actively working. "
            "Re-enter wait_for_any."
        )
        return True, {
            "killed": False,
            "refused": True,
            "reason": reason,
            "snapshot": snapshot,
        }
    return False, snapshot


AGENT_STATUS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "agent_status",
        "description": "Get the current status of a spawned agent.",
        "parameters": {
            "type": "object",
            "properties": {"agent_id": {"type": "string"}},
            "required": ["agent_id"],
        },
    },
}
READ_AGENT_OUTPUT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_agent_output",
        "description": "Read stdout and stderr log tails for a spawned agent.",
        "parameters": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "tail_bytes": {"type": "integer"},
            },
            "required": ["agent_id"],
        },
    },
}
READ_NEW_AGENT_OUTPUT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_new_agent_output",
        "description": "Read only newly appended stdout content for an agent.",
        "parameters": {
            "type": "object",
            "properties": {"agent_id": {"type": "string"}},
            "required": ["agent_id"],
        },
    },
}
LIST_AGENTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_agents",
        "description": "List all agents in the current run.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}
WAIT_FOR_AGENTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "wait_for_agents",
        "description": "Wait for all specified agents or until timeout.",
        "parameters": {
            "type": "object",
            "properties": {
                "agent_ids": {"type": "array", "items": {"type": "string"}},
                "timeout": {"type": "number"},
            },
            "required": [],
        },
    },
}
WAIT_FOR_ANY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "wait_for_any",
        "description": "Wait for the first agent to complete or until timeout.",
        "parameters": {
            "type": "object",
            "properties": {
                "agent_ids": {"type": "array", "items": {"type": "string"}},
                "timeout": {"type": "number"},
            },
            "required": ["agent_ids"],
        },
    },
}
KILL_AGENT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "kill_agent",
        "description": "Terminate a running agent.",
        "parameters": {
            "type": "object",
            "properties": {"agent_id": {"type": "string"}},
            "required": ["agent_id"],
        },
    },
}


def setup(
    manager: "AgentManager",
    run_log: "RunLogWriter",
    config: "Config",
    ui: "ConsoleBase",
    session_output_dir: str = "",
) -> None:
    global _manager
    global _run_log
    global _config
    global _ui
    global _session_output_dir
    _manager = manager
    _run_log = run_log
    _config = config
    _ui = ui
    _session_output_dir = session_output_dir
    _output_cursors.clear()
    _output_locks.clear()


def _require_setup() -> tuple["AgentManager", "RunLogWriter", "Config", "ConsoleBase"]:
    if _manager is None or _run_log is None or _config is None or _ui is None:
        raise RuntimeError(
            "agent_tools.setup() must be called before using agent tools"
        )
    return _manager, _run_log, _config, _ui


def spawn_agent_schema(allowed_types: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": "spawn_agent",
            "description": "Spawn a worker agent subprocess.",
            "parameters": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": allowed_types},
                    "prompt": {"type": "string"},
                    "cwd": {"type": "string"},
                    "model": {"type": "string"},
                    "mode": {
                        "type": "string",
                        "enum": ["fresh", "resume"],
                        "description": (
                            "Use 'resume' to continue an existing provider "
                            "session instead of starting a fresh one."
                        ),
                    },
                    "resume_from_session_id": {
                        "type": "string",
                        "description": (
                            "Provider session ID to resume when mode == 'resume'. "
                            "Use IDs captured in worker_sessions.json."
                        ),
                    },
                    "flags": {"type": "array", "items": {"type": "string"}},
                    "env": {"type": "object"},
                    "agents": {
                        "type": "array",
                        "description": "Only used when type == 'harness'.",
                        "items": {"type": "string"},
                    },
                    "output_path": {"type": "string"},
                },
                "required": ["type", "prompt", "cwd"],
            },
        },
    }


async def spawn_agent(**kwargs: object) -> str:
    manager, run_log, config, ui = _require_setup()
    agent_type = str(kwargs["type"])
    prompt = str(kwargs["prompt"])
    cwd = str(kwargs["cwd"])
    model = str(kwargs["model"]) if kwargs.get("model") is not None else None
    flags = (
        [str(item) for item in kwargs.get("flags", [])]
        if kwargs.get("flags") is not None
        else None
    )
    raw_env = kwargs.get("env")
    env: dict[str, str] | None = None
    if isinstance(raw_env, Mapping):
        env = {str(key): str(value) for key, value in raw_env.items()}
    agents = (
        [str(item) for item in kwargs.get("agents", [])]
        if kwargs.get("agents") is not None
        else None
    )
    output_path = (
        str(kwargs["output_path"]) if kwargs.get("output_path") is not None else None
    )
    if agent_type == "harness":
        try:
            check_harness_depth(config)
        except ValueError:
            return f"ERROR: max harness depth ({config.max_depth}) reached"

    parts = [prompt.rstrip()]
    if config.worker_suffix:
        parts.append(config.worker_suffix)
    parts.append(_build_worker_output_footer(_session_output_dir, config))
    full_prompt = "\n\n".join(part for part in parts if part)
    current_depth = int(os.environ.get("TEAM_HARNESS_DEPTH", "0"))
    extra_env = {**(env or {}), "TEAM_HARNESS_DEPTH": str(current_depth + 1)}
    agent_id = "agent_" + uuid.uuid4().hex[:12]
    run_dir = config.run_dir
    if run_dir is None:
        raise RuntimeError("config.run_dir must be set before spawning agents")
    stdout_log = (
        Path(output_path) if output_path else run_dir / f"{agent_id}_stdout.log"
    )
    stderr_log = run_dir / f"{agent_id}_stderr.log"
    spawn_result = await spawner.spawn(
        agent_id=agent_id,
        agent_type=agent_type,
        prompt=full_prompt,
        cwd=Path(cwd),
        config=config,
        log_dir=run_dir,
        extra_env=extra_env,
        model=model,
        extra_flags=flags,
        allowed_agents=agents if agent_type == "harness" else None,
        output_path=output_path,
        mode=str(kwargs.get("mode", "fresh")),
        resume_session_id=(
            str(kwargs["resume_from_session_id"])
            if kwargs.get("resume_from_session_id") is not None
            else None
        ),
    )
    state = AgentState(
        id=agent_id,
        agent_type=agent_type,
        prompt=prompt,
        cwd=cwd,
        proc=spawn_result.proc,
        spawn_time=datetime.now(timezone.utc),
        stdout_log=stdout_log,
        stderr_log=stderr_log,
        session_id=spawn_result.generated_uuid,
    )
    manager.register(state)
    record = AgentRecord(
        id=agent_id,
        agent_type=agent_type,
        status=state.status,
        cwd=cwd,
        prompt=prompt,
        full_prompt=full_prompt,
        command=spawn_result.command,
        spawned_at=state.spawn_time,
        stdout_log=str(stdout_log),
        stderr_log=str(stderr_log),
        session_id=state.session_id,
    )
    run_log.record_agent_spawn(record)
    ui.agent_event(event="spawned", state=state)
    done_event = asyncio.Event()
    asyncio.ensure_future(_watch_agent(agent_id, done_event))
    asyncio.ensure_future(
        _capture_session_id_task(
            agent_id=agent_id,
            template=spawn_result.template,
            pre_generated_uuid=spawn_result.generated_uuid,
            stop_event=done_event,
        )
    )
    return agent_id


async def _watch_agent(agent_id: str, done_event: asyncio.Event) -> None:
    manager, run_log, _, ui = _require_setup()
    try:
        exit_code = await manager.wait_one(agent_id)
        state = manager.get(agent_id)
        if state.status != "killed":
            state.status = "done" if exit_code == 0 else "failed"
            if state.status == "failed":
                _classify_if_failed(state)
            run_log.update_agent(
                agent_id,
                exit_code=exit_code,
                finished_at=state.finished_at or datetime.now(timezone.utc),
                status=state.status,
            )
            ui.agent_event(event="done" if exit_code == 0 else "failed", state=state)
    finally:
        done_event.set()


async def _capture_session_id_task(
    *,
    agent_id: str,
    template: AgentTemplate,
    pre_generated_uuid: str | None,
    stop_event: asyncio.Event,
) -> None:
    manager, run_log, _, _ = _require_setup()
    state = manager.get(agent_id)
    session_id = await capture_session_id_from_path(
        stdout_path=state.stdout_log,
        template=template,
        pre_generated_uuid=pre_generated_uuid,
        stop_event=stop_event,
        max_wait_s=24 * 60 * 60,
    )
    if session_id is not None:
        state.session_id = session_id
        run_log.update_agent(agent_id, session_id=session_id)


def _status_from_state(state: AgentState) -> str:
    if state.status == "killed":
        return "killed"
    if state.exit_code is None:
        return "running"
    if state.exit_code == 0:
        return "done (exit 0)"
    return f"failed (exit {state.exit_code})"


async def agent_status(agent_id: str) -> str:
    manager, _, _, _ = _require_setup()
    manager.poll_exit_codes()
    return _status_from_state(manager.get(agent_id))


async def read_agent_output(agent_id: str, tail_bytes: int = 8192) -> str:
    manager, _, _, _ = _require_setup()
    state = manager.get(agent_id)

    def _tail(path: Path) -> str:
        if not path.exists():
            return ""
        with path.open("rb") as handle:
            size = path.stat().st_size
            handle.seek(max(0, size - tail_bytes))
            return handle.read().decode(errors="replace")

    stdout_text = await asyncio.to_thread(_tail, state.stdout_log)
    stderr_text = await asyncio.to_thread(_tail, state.stderr_log)
    return f"=== stdout ===\n{stdout_text}\n=== stderr ===\n{stderr_text}"


async def read_new_agent_output(agent_id: str) -> str:
    manager, _, _, _ = _require_setup()
    state = manager.get(agent_id)
    lock = _output_locks.setdefault(agent_id, asyncio.Lock())
    async with lock:
        cursor = _output_cursors.get(agent_id, 0)

        def _read() -> tuple[int, bytes]:
            if not state.stdout_log.exists():
                return cursor, b""
            size = state.stdout_log.stat().st_size
            if size <= cursor:
                return cursor, b""
            with state.stdout_log.open("rb") as handle:
                handle.seek(cursor)
                data = handle.read()
            return cursor + len(data), data

        new_cursor, data = await asyncio.to_thread(_read)
        if not data:
            return ""
        _output_cursors[agent_id] = new_cursor
        return data.decode("utf-8", errors="replace")


async def list_agents() -> str:
    manager, _, _, _ = _require_setup()
    manager.poll_exit_codes()
    payload = []
    for state in manager.list_all():
        payload.append(
            {
                "id": state.id,
                "type": state.agent_type,
                "status": _status_from_state(state),
                "cwd": state.cwd,
                "elapsed": _elapsed(state),
            }
        )
    return json.dumps(payload)


async def wait_for_agents(
    agent_ids: list[str] | None = None, timeout: float | None = None
) -> str:
    manager, _, _, _ = _require_setup()
    ids = list(manager._agents) if agent_ids is None else agent_ids
    if not ids:
        return json.dumps({"agents": {}, "timed_out": False})
    try:
        await asyncio.wait_for(manager.wait_for(ids), timeout=timeout)
        manager.poll_exit_codes()
        return json.dumps(
            {
                "agents": {
                    agent_id: _status_from_state(manager.get(agent_id))
                    for agent_id in ids
                },
                "timed_out": False,
            }
        )
    except asyncio.TimeoutError:
        manager.poll_exit_codes()
        return json.dumps(
            {
                "agents": {
                    agent_id: _status_from_state(manager.get(agent_id))
                    for agent_id in ids
                },
                "timed_out": True,
            }
        )


async def wait_for_any(agent_ids: list[str], timeout: float | None = None) -> str:
    manager, _, config, _ = _require_setup()
    if not agent_ids:
        return json.dumps({"agent_id": None, "timed_out": False, "running": []})
    tasks = {
        asyncio.ensure_future(manager.wait_one(agent_id)): agent_id
        for agent_id in agent_ids
    }
    try:
        done, pending = await asyncio.wait_for(
            asyncio.wait(tasks.keys(), return_when=asyncio.FIRST_COMPLETED),
            timeout=timeout,
        )
        for pending_task in pending:
            pending_task.cancel()
        finished_id = tasks[next(iter(done))]
        running_ids = [aid for aid in agent_ids if aid != finished_id]
        finished_state = manager.get(finished_id)
        elapsed = int(
            (datetime.now(timezone.utc) - finished_state.spawn_time).total_seconds()
        )
        result: dict[str, object] = {
            "agent_id": finished_id,
            "finished_agent_id": finished_id,
            "timed_out": False,
            "status": await agent_status(finished_id),
            "elapsed_seconds": elapsed,
            "running": [
                _build_running_snapshot(manager.get(aid), advance_cursors=True)
                for aid in running_ids
            ],
            "patience_policy": _patience_policy(config),
        }
        classification = _classify_if_failed(finished_state)
        if classification is not None:
            result["failure_classification"] = classification
        return json.dumps(result)
    except asyncio.TimeoutError:
        for task in tasks:
            task.cancel()
        return json.dumps(
            {
                "agent_id": None,
                "finished_agent_id": None,
                "timed_out": True,
                "running": [
                    _build_running_snapshot(manager.get(aid), advance_cursors=True)
                    for aid in agent_ids
                ],
                "patience_policy": _patience_policy(config),
            }
        )


async def kill_agent(agent_id: str, *, force: bool = False) -> str:
    manager, run_log, config, ui = _require_setup()
    state = manager.get(agent_id)
    if state.proc.returncode is not None:
        manager.poll_exit_codes()
        return json.dumps(
            {
                "killed": False,
                "refused": False,
                "message": f"Agent {agent_id} already finished.",
            }
        )
    if not force:
        floor = float(getattr(config, "min_agent_lifetime_before_kill_s", 600.0))
        refused, payload = _should_refuse_kill(state, min_lifetime_s=floor)
        if refused:
            return json.dumps(payload)
    manager.kill(agent_id)
    try:
        await asyncio.wait_for(state.proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        state.proc.kill()
        await state.proc.wait()
    state.exit_code = state.proc.returncode
    state.finished_at = datetime.now(timezone.utc)
    state.status = "killed"
    run_log.update_agent(
        agent_id,
        exit_code=state.exit_code if state.exit_code is not None else -1,
        finished_at=state.finished_at,
        status="killed",
    )
    ui.agent_event(event="killed", state=state)
    return json.dumps(
        {
            "killed": True,
            "refused": False,
            "agent_id": agent_id,
            "message": f"Killed {agent_id}.",
        }
    )


def _elapsed(state: AgentState) -> str:
    end = state.finished_at or datetime.now(timezone.utc)
    total_seconds = int((end - state.spawn_time).total_seconds())
    return f"{total_seconds // 60}:{total_seconds % 60:02d}"


AGENT_TOOL_SCHEMAS = [
    (AGENT_STATUS_SCHEMA, agent_status),
    (READ_AGENT_OUTPUT_SCHEMA, read_agent_output),
    (READ_NEW_AGENT_OUTPUT_SCHEMA, read_new_agent_output),
    (LIST_AGENTS_SCHEMA, list_agents),
    (WAIT_FOR_AGENTS_SCHEMA, wait_for_agents),
    (WAIT_FOR_ANY_SCHEMA, wait_for_any),
    (KILL_AGENT_SCHEMA, kill_agent),
]


def build_agent_tool_bindings(
    *,
    manager: "AgentManager",
    run_log: "RunLogWriter",
    config: "Config",
    ui: "ConsoleBase",
    allowed_types: list[str],
    session_output_dir: str = "",
) -> list[tuple[dict, Callable[..., Awaitable[str]]]]:
    """Build per-run agent tool closures for concurrent safety.

    Returns a list of (schema, async_callable) pairs. Each closure captures
    its own output_cursors and output_locks dicts so that concurrent runs
    do not share state.
    """
    output_cursors: dict[str, int] = {}
    output_locks: dict[str, asyncio.Lock] = {}

    async def _spawn_agent(**kwargs: object) -> str:
        agent_type = str(kwargs["type"])
        prompt = str(kwargs["prompt"])
        cwd = str(kwargs["cwd"])
        model_val = str(kwargs["model"]) if kwargs.get("model") is not None else None
        flags = (
            [str(item) for item in kwargs.get("flags", [])]
            if kwargs.get("flags") is not None
            else None
        )
        raw_env = kwargs.get("env")
        env: dict[str, str] | None = None
        if isinstance(raw_env, Mapping):
            env = {str(key): str(value) for key, value in raw_env.items()}
        agents_arg = (
            [str(item) for item in kwargs.get("agents", [])]
            if kwargs.get("agents") is not None
            else None
        )
        output_path = (
            str(kwargs["output_path"])
            if kwargs.get("output_path") is not None
            else None
        )
        if agent_type == "harness":
            try:
                check_harness_depth(config)
            except ValueError:
                return f"ERROR: max harness depth ({config.max_depth}) reached"

        _parts = [prompt.rstrip()]
        if config.worker_suffix:
            _parts.append(config.worker_suffix)
        _parts.append(_build_worker_output_footer(session_output_dir, config))
        full_prompt = "\n\n".join(p for p in _parts if p)
        current_depth = int(os.environ.get("TEAM_HARNESS_DEPTH", "0"))
        extra_env = {**(env or {}), "TEAM_HARNESS_DEPTH": str(current_depth + 1)}
        agent_id = "agent_" + uuid.uuid4().hex[:12]
        run_dir = config.run_dir
        if run_dir is None:
            raise RuntimeError("config.run_dir must be set before spawning agents")
        stdout_log = (
            Path(output_path) if output_path else run_dir / f"{agent_id}_stdout.log"
        )
        stderr_log = run_dir / f"{agent_id}_stderr.log"
        spawn_result = await spawner.spawn(
            agent_id=agent_id,
            agent_type=agent_type,
            prompt=full_prompt,
            cwd=Path(cwd),
            config=config,
            log_dir=run_dir,
            extra_env=extra_env,
            model=model_val,
            extra_flags=flags,
            allowed_agents=agents_arg if agent_type == "harness" else None,
            output_path=output_path,
            mode=str(kwargs.get("mode", "fresh")),
            resume_session_id=(
                str(kwargs["resume_from_session_id"])
                if kwargs.get("resume_from_session_id") is not None
                else None
            ),
        )
        state = AgentState(
            id=agent_id,
            agent_type=agent_type,
            prompt=prompt,
            cwd=cwd,
            proc=spawn_result.proc,
            spawn_time=datetime.now(timezone.utc),
            stdout_log=stdout_log,
            stderr_log=stderr_log,
            session_id=spawn_result.generated_uuid,
        )
        manager.register(state)
        record = AgentRecord(
            id=agent_id,
            agent_type=agent_type,
            coordinator_turn_index=None,
            status=state.status,
            cwd=cwd,
            prompt=prompt,
            full_prompt=full_prompt,
            command=spawn_result.command,
            spawned_at=state.spawn_time,
            stdout_log=str(stdout_log),
            stderr_log=str(stderr_log),
            session_id=state.session_id,
            resume=resume_info_for_agent_type(agent_type),
        )
        run_log.record_agent_spawn(record)
        ui.agent_event(event="spawned", state=state)

        done_event = asyncio.Event()

        async def _watch() -> None:
            try:
                exit_code = await manager.wait_one(agent_id)
                s = manager.get(agent_id)
                if s.status != "killed":
                    s.status = "done" if exit_code == 0 else "failed"
                    if s.status == "failed":
                        _classify_if_failed(s)
                    run_log.update_agent(
                        agent_id,
                        exit_code=exit_code,
                        finished_at=s.finished_at or datetime.now(timezone.utc),
                        status=s.status,
                    )
                    ui.agent_event(
                        event="done" if exit_code == 0 else "failed", state=s
                    )
            finally:
                done_event.set()

        async def _capture_session() -> None:
            session_id = await capture_session_id_from_path(
                stdout_path=stdout_log,
                template=spawn_result.template,
                pre_generated_uuid=spawn_result.generated_uuid,
                stop_event=done_event,
                max_wait_s=24 * 60 * 60,
            )
            if session_id is not None:
                s = manager.get(agent_id)
                s.session_id = session_id
                run_log.update_agent(agent_id, session_id=session_id)

        asyncio.ensure_future(_watch())
        asyncio.ensure_future(_capture_session())
        return agent_id

    async def _agent_status(agent_id: str) -> str:
        manager.poll_exit_codes()
        return _status_from_state(manager.get(agent_id))

    async def _read_agent_output(agent_id: str, tail_bytes: int = 8192) -> str:
        state = manager.get(agent_id)

        def _tail(path: Path) -> str:
            if not path.exists():
                return ""
            with path.open("rb") as handle:
                size = path.stat().st_size
                handle.seek(max(0, size - tail_bytes))
                return handle.read().decode(errors="replace")

        stdout_text = await asyncio.to_thread(_tail, state.stdout_log)
        stderr_text = await asyncio.to_thread(_tail, state.stderr_log)
        return f"=== stdout ===\n{stdout_text}\n=== stderr ===\n{stderr_text}"

    async def _read_new_agent_output(agent_id: str) -> str:
        state = manager.get(agent_id)
        lock = output_locks.setdefault(agent_id, asyncio.Lock())
        async with lock:
            cursor = output_cursors.get(agent_id, 0)

            def _read() -> tuple[int, bytes]:
                if not state.stdout_log.exists():
                    return cursor, b""
                size = state.stdout_log.stat().st_size
                if size <= cursor:
                    return cursor, b""
                with state.stdout_log.open("rb") as handle:
                    handle.seek(cursor)
                    data = handle.read()
                return cursor + len(data), data

            new_cursor, data = await asyncio.to_thread(_read)
            if not data:
                return ""
            output_cursors[agent_id] = new_cursor
            return data.decode("utf-8", errors="replace")

    async def _list_agents() -> str:
        manager.poll_exit_codes()
        payload = []
        for state in manager.list_all():
            payload.append(
                {
                    "id": state.id,
                    "type": state.agent_type,
                    "status": _status_from_state(state),
                    "cwd": state.cwd,
                    "elapsed": _elapsed(state),
                }
            )
        return json.dumps(payload)

    async def _wait_for_agents(
        agent_ids: list[str] | None = None, timeout: float | None = None
    ) -> str:
        ids = list(manager._agents) if agent_ids is None else agent_ids
        if not ids:
            return json.dumps({"agents": {}, "timed_out": False})
        try:
            await asyncio.wait_for(manager.wait_for(ids), timeout=timeout)
            manager.poll_exit_codes()
            return json.dumps(
                {
                    "agents": {
                        aid: _status_from_state(manager.get(aid)) for aid in ids
                    },
                    "timed_out": False,
                }
            )
        except asyncio.TimeoutError:
            manager.poll_exit_codes()
            return json.dumps(
                {
                    "agents": {
                        aid: _status_from_state(manager.get(aid)) for aid in ids
                    },
                    "timed_out": True,
                }
            )

    async def _wait_for_any(agent_ids: list[str], timeout: float | None = None) -> str:
        if not agent_ids:
            return json.dumps({"agent_id": None, "timed_out": False, "running": []})
        tasks = {asyncio.ensure_future(manager.wait_one(aid)): aid for aid in agent_ids}
        try:
            done, pending = await asyncio.wait_for(
                asyncio.wait(tasks.keys(), return_when=asyncio.FIRST_COMPLETED),
                timeout=timeout,
            )
            for pending_task in pending:
                pending_task.cancel()
            finished_id = tasks[next(iter(done))]
            finished_state = manager.get(finished_id)
            elapsed = int(
                (datetime.now(timezone.utc) - finished_state.spawn_time).total_seconds()
            )
            wait_result: dict[str, object] = {
                "agent_id": finished_id,
                "finished_agent_id": finished_id,
                "timed_out": False,
                "status": _status_from_state(finished_state),
                "elapsed_seconds": elapsed,
                "running": [
                    _build_running_snapshot(manager.get(aid), advance_cursors=True)
                    for aid in agent_ids
                    if aid != finished_id
                ],
                "patience_policy": _patience_policy(config),
            }
            classification = _classify_if_failed(finished_state)
            if classification is not None:
                wait_result["failure_classification"] = classification
            return json.dumps(wait_result)
        except asyncio.TimeoutError:
            for task in tasks:
                task.cancel()
            return json.dumps(
                {
                    "agent_id": None,
                    "finished_agent_id": None,
                    "timed_out": True,
                    "running": [
                        _build_running_snapshot(manager.get(aid), advance_cursors=True)
                        for aid in agent_ids
                    ],
                    "patience_policy": _patience_policy(config),
                }
            )

    async def _kill_agent(agent_id: str, *, force: bool = False) -> str:
        state = manager.get(agent_id)
        if state.proc.returncode is not None:
            manager.poll_exit_codes()
            return json.dumps(
                {
                    "killed": False,
                    "refused": False,
                    "message": f"Agent {agent_id} already finished.",
                }
            )
        if not force:
            floor = float(getattr(config, "min_agent_lifetime_before_kill_s", 600.0))
            refused, payload = _should_refuse_kill(state, min_lifetime_s=floor)
            if refused:
                return json.dumps(payload)
        manager.kill(agent_id)
        try:
            await asyncio.wait_for(state.proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            state.proc.kill()
            await state.proc.wait()
        state.exit_code = state.proc.returncode
        state.finished_at = datetime.now(timezone.utc)
        state.status = "killed"
        run_log.update_agent(
            agent_id,
            exit_code=state.exit_code if state.exit_code is not None else -1,
            finished_at=state.finished_at,
            status="killed",
        )
        ui.agent_event(event="killed", state=state)
        return json.dumps(
            {
                "killed": True,
                "refused": False,
                "agent_id": agent_id,
                "message": f"Killed {agent_id}.",
            }
        )

    return [
        (spawn_agent_schema(allowed_types), _spawn_agent),
        (AGENT_STATUS_SCHEMA, _agent_status),
        (READ_AGENT_OUTPUT_SCHEMA, _read_agent_output),
        (READ_NEW_AGENT_OUTPUT_SCHEMA, _read_new_agent_output),
        (LIST_AGENTS_SCHEMA, _list_agents),
        (WAIT_FOR_AGENTS_SCHEMA, _wait_for_agents),
        (WAIT_FOR_ANY_SCHEMA, _wait_for_any),
        (KILL_AGENT_SCHEMA, _kill_agent),
    ]
