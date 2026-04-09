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
from team_harness.agents.manager import AgentState
from team_harness.agents.registry import check_harness_depth
from team_harness.tracking.models import AgentRecord

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


def _build_worker_output_footer(output_dir: str = "") -> str:
    parts = [
        "---",
        "IMPORTANT — output requirements:",
        "- Write substantial artifacts to files when useful instead of relying only on",
        "  stdout.",
    ]
    if output_dir:
        parts.append(f"- Session output directory: {output_dir}")
        parts.append("  Place outputs, notes, and scratchpads there.")
    parts.append(
        "- Report blockers, errors, and any final result clearly in your response."
    )
    parts.append("---")
    return "\n".join(parts)


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

    full_prompt = (
        prompt.rstrip() + "\n\n" + _build_worker_output_footer(_session_output_dir)
    )
    current_depth = int(os.environ.get("HARNESS_DEPTH", "0"))
    extra_env = {**(env or {}), "HARNESS_DEPTH": str(current_depth + 1)}
    agent_id = "agent_" + uuid.uuid4().hex[:12]
    run_dir = config.run_dir
    if run_dir is None:
        raise RuntimeError("config.run_dir must be set before spawning agents")
    stdout_log = (
        Path(output_path) if output_path else run_dir / f"{agent_id}_stdout.log"
    )
    stderr_log = run_dir / f"{agent_id}_stderr.log"
    proc = await spawner.spawn(
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
    )
    state = AgentState(
        id=agent_id,
        agent_type=agent_type,
        prompt=prompt,
        cwd=cwd,
        proc=proc,
        spawn_time=datetime.now(timezone.utc),
        stdout_log=stdout_log,
        stderr_log=stderr_log,
    )
    manager.register(state)
    record = AgentRecord(
        id=agent_id,
        agent_type=agent_type,
        status=state.status,
        cwd=cwd,
        prompt=prompt,
        full_prompt=full_prompt,
        command=spawner.build_command(
            agent_type=agent_type,
            prompt=full_prompt,
            config=config,
            model=model,
            extra_flags=flags,
            allowed_agents=agents if agent_type == "harness" else None,
        )
        if hasattr(spawner, "build_command")
        else [],
        spawned_at=state.spawn_time,
        stdout_log=str(stdout_log),
        stderr_log=str(stderr_log),
    )
    run_log.record_agent_spawn(record)
    ui.agent_event(event="spawned", state=state)
    asyncio.ensure_future(_watch_agent(agent_id))
    return agent_id


async def _watch_agent(agent_id: str) -> None:
    manager, run_log, _, ui = _require_setup()
    exit_code = await manager.wait_one(agent_id)
    state = manager.get(agent_id)
    if state.status != "killed":
        state.status = "done" if exit_code == 0 else "failed"
        run_log.update_agent(
            agent_id,
            exit_code=exit_code,
            finished_at=state.finished_at or datetime.now(timezone.utc),
            status=state.status,
        )
        ui.agent_event(event="done" if exit_code == 0 else "failed", state=state)


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
    manager, _, _, _ = _require_setup()
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
        return json.dumps(
            {
                "agent_id": finished_id,
                "timed_out": False,
                "status": await agent_status(finished_id),
            }
        )
    except asyncio.TimeoutError:
        for task in tasks:
            task.cancel()
        return json.dumps({"agent_id": None, "timed_out": True, "running": agent_ids})


async def kill_agent(agent_id: str) -> str:
    manager, run_log, _, ui = _require_setup()
    state = manager.get(agent_id)
    if state.proc.returncode is not None:
        manager.poll_exit_codes()
        return f"Agent {agent_id} already finished."
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
    return f"Killed {agent_id}."


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

        full_prompt = (
            prompt.rstrip() + "\n\n" + _build_worker_output_footer(session_output_dir)
        )
        current_depth = int(os.environ.get("HARNESS_DEPTH", "0"))
        extra_env = {**(env or {}), "HARNESS_DEPTH": str(current_depth + 1)}
        agent_id = "agent_" + uuid.uuid4().hex[:12]
        run_dir = config.run_dir
        if run_dir is None:
            raise RuntimeError("config.run_dir must be set before spawning agents")
        stdout_log = (
            Path(output_path) if output_path else run_dir / f"{agent_id}_stdout.log"
        )
        stderr_log = run_dir / f"{agent_id}_stderr.log"
        proc = await spawner.spawn(
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
        )
        state = AgentState(
            id=agent_id,
            agent_type=agent_type,
            prompt=prompt,
            cwd=cwd,
            proc=proc,
            spawn_time=datetime.now(timezone.utc),
            stdout_log=stdout_log,
            stderr_log=stderr_log,
        )
        manager.register(state)
        record = AgentRecord(
            id=agent_id,
            agent_type=agent_type,
            status=state.status,
            cwd=cwd,
            prompt=prompt,
            full_prompt=full_prompt,
            command=spawner.build_command(
                agent_type=agent_type,
                prompt=full_prompt,
                config=config,
                model=model_val,
                extra_flags=flags,
                allowed_agents=agents_arg if agent_type == "harness" else None,
            )
            if hasattr(spawner, "build_command")
            else [],
            spawned_at=state.spawn_time,
            stdout_log=str(stdout_log),
            stderr_log=str(stderr_log),
        )
        run_log.record_agent_spawn(record)
        ui.agent_event(event="spawned", state=state)

        async def _watch() -> None:
            exit_code = await manager.wait_one(agent_id)
            s = manager.get(agent_id)
            if s.status != "killed":
                s.status = "done" if exit_code == 0 else "failed"
                run_log.update_agent(
                    agent_id,
                    exit_code=exit_code,
                    finished_at=s.finished_at or datetime.now(timezone.utc),
                    status=s.status,
                )
                ui.agent_event(event="done" if exit_code == 0 else "failed", state=s)

        asyncio.ensure_future(_watch())
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
            return json.dumps(
                {
                    "agent_id": finished_id,
                    "timed_out": False,
                    "status": _status_from_state(manager.get(finished_id)),
                }
            )
        except asyncio.TimeoutError:
            for task in tasks:
                task.cancel()
            return json.dumps(
                {"agent_id": None, "timed_out": True, "running": agent_ids}
            )

    async def _kill_agent(agent_id: str) -> str:
        state = manager.get(agent_id)
        if state.proc.returncode is not None:
            manager.poll_exit_codes()
            return f"Agent {agent_id} already finished."
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
        return f"Killed {agent_id}."

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
