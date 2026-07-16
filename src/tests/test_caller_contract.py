# pyright: reportMissingParameterType=false, reportArgumentType=false

import asyncio
from datetime import datetime
from datetime import timezone
import json
import os
from pathlib import Path
import signal
import sys

from pydantic import ValidationError
import pytest

from team_harness import CallerContext
from team_harness import config as config_module
from team_harness import get_capabilities
from team_harness import TEAM_HARNESS_CAPABILITIES
from team_harness import TeamHarness
from team_harness import TeamHarnessError
from team_harness.agents.manager import AgentManager
from team_harness.agents.manager import AgentState
from team_harness.agents.process_identity import ProcessProbeError
from team_harness.agents.session_capture import extract_session_id
from team_harness.agents.spawner import spawn
from team_harness.agents.template import AgentTemplate
from team_harness.agents.template import SessionCapture
from team_harness.caller_contract import build_coordinator_context_footer
from team_harness.caller_contract import INHERITED_CALLER_CONTEXT_ENV
from team_harness.config import Config
from team_harness.harness import _finalize_run
from team_harness.harness import _force_kill_unreaped_workers
from team_harness.tools.agent_tools import build_agent_tool_bindings
from team_harness.tools.agent_tools import spawn_agent_schema
from team_harness.tracking.models import AgentRecord
from team_harness.tracking.run_log import RunLogWriter
from team_harness.tracking.worker_sessions import build_worker_failure_detail
from team_harness.tracking.worker_sessions import write_worker_sessions_manifest
from team_harness.ui.console import SilentConsole
from tests.helpers import fake_agent_template


def _caller_context(tmp_path: Path) -> CallerContext:
    assignment = tmp_path / "attempt" / "assignment.json"
    assignment.parent.mkdir(parents=True, exist_ok=True)
    assignment.write_text('{"schema_version": 1}', encoding="utf-8")
    state_path = tmp_path / "session" / "project_state"
    state_path.mkdir(parents=True)
    return CallerContext(
        trace_root=tmp_path / "traces" / "attempt-abc",
        parent_assignment_path=assignment,
        parent_attempt_id="attempt-abc",
        root_session_id="session-root",
        session_id="session-leaf",
        session_depth=2,
        workflow_role="inner",
        relevant_state_paths=(state_path,),
    )


def test_capabilities_are_public_and_name_based():
    advertised = get_capabilities()

    assert advertised.caller_contract_version == 1
    assert advertised.capabilities == TEAM_HARNESS_CAPABILITIES
    assert advertised.supports(
        "caller_run_record_v1",
        "coordinator_input_v1",
        "spawn_assignment_v1",
        "nested_caller_context_v1",
    )
    assert not advertised.supports("future_contract_v99")


def test_caller_context_rejects_relative_contract_paths(tmp_path):
    with pytest.raises(ValidationError, match="absolute"):
        CallerContext(
            trace_root=Path("relative-traces"),
            parent_assignment_path=tmp_path / "assignment.json",
            parent_attempt_id="attempt-abc",
            root_session_id="session-root",
            session_id="session-leaf",
            session_depth=0,
            workflow_role="inner",
        )


def test_spawn_delegation_metadata_is_dynamic_not_enumerated():
    properties = spawn_agent_schema(["codex"])["function"]["parameters"]["properties"]

    assert "enum" not in properties["delegated_role"]
    assert "enum" not in properties["delegated_task_id"]
    assert properties["expected_outputs"]["items"]["type"] == "string"
    assert "enum" not in properties["state_responsibility"]


@pytest.mark.asyncio
async def test_caller_run_is_self_contained_and_input_precedes_provider_call(
    monkeypatch, tmp_path
):
    context = _caller_context(tmp_path)
    request_marker = "caller-request-marker-123"
    captured_messages: list[dict] = []

    class FakeClient:
        api_base = "http://localhost:11434/v1"

        async def aclose(self):
            return None

    async def fake_resolve_model_limit(*, model_id, client, config):
        input_paths = list(context.trace_root.glob("*/coordinator_input.json"))
        assert len(input_paths) == 1
        assert input_paths[0].is_file()
        return 128_000

    async def fake_run(messages, **kwargs):
        captured_messages.extend(messages)
        messages.append({"role": "assistant", "content": "done"})

    monkeypatch.setattr(
        "team_harness.harness._make_client", lambda config: FakeClient()
    )
    monkeypatch.setattr(config_module, "CONFIG_PATH", tmp_path / "home" / "config.toml")
    monkeypatch.setattr(
        "team_harness.harness.validate_templates", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        "team_harness.harness.resolve_model_limit", fake_resolve_model_limit
    )
    monkeypatch.setattr("team_harness.harness.load_skill_metadata", lambda cwd=None: [])
    monkeypatch.setattr("team_harness.harness.run", fake_run)

    result = await TeamHarness(
        api_base="http://localhost:11434/v1", cwd=str(tmp_path), caller_context=context
    ).run(f"Implement the task with marker={request_marker}")

    run_dir = Path(result.run_json_path).parent
    assert result.text == "done"
    assert run_dir.parent == context.trace_root
    assert result.session_output_dir == str(run_dir)
    assert result.coordinator_input_path == str(run_dir / "coordinator_input.json")
    assert Path(result.run_json_path).is_file()
    assert Path(result.coordinator_input_path).is_file()
    assert request_marker in captured_messages[1]["content"]

    persisted_input = Path(result.coordinator_input_path).read_text(encoding="utf-8")
    assert request_marker in persisted_input
    input_payload = json.loads(persisted_input)
    system_input = input_payload["messages"][0]["content"]
    assert str(context.parent_assignment_path) in system_input
    assert "Workflow role: inner" in system_input
    assert f"Harness run id: {run_dir.name}" in system_input

    run_payload = json.loads(Path(result.run_json_path).read_text(encoding="utf-8"))
    assert run_payload["caller_context"]["parent_attempt_id"] == "attempt-abc"
    assert set(run_payload["capabilities"]) == TEAM_HARNESS_CAPABILITIES
    assert run_payload["coordinator_input_path"] == result.coordinator_input_path


@pytest.mark.asyncio
async def test_failure_detail_returns_canonical_caller_paths(monkeypatch, tmp_path):
    context = _caller_context(tmp_path)

    class FakeClient:
        api_base = "http://localhost:11434/v1"

        async def aclose(self):
            return None

    async def fake_resolve_model_limit(*, model_id, client, config):
        return 128_000

    async def fake_run(**kwargs):
        raise RuntimeError("synthetic coordinator failure")

    monkeypatch.setattr(
        "team_harness.harness._make_client", lambda config: FakeClient()
    )
    monkeypatch.setattr(config_module, "CONFIG_PATH", tmp_path / "home" / "config.toml")
    monkeypatch.setattr(
        "team_harness.harness.validate_templates", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        "team_harness.harness.resolve_model_limit", fake_resolve_model_limit
    )
    monkeypatch.setattr("team_harness.harness.load_skill_metadata", lambda cwd=None: [])
    monkeypatch.setattr("team_harness.harness.run", fake_run)

    with pytest.raises(TeamHarnessError, match="synthetic") as caught:
        await TeamHarness(
            api_base="http://localhost:11434/v1",
            cwd=str(tmp_path),
            caller_context=context,
        ).run("task")

    detail = caught.value.detail or {}
    assert Path(str(detail["run_json_path"])).is_file()
    assert Path(str(detail["coordinator_input_path"])).is_file()
    assert Path(str(detail["run_json_path"])).parent == Path(
        str(detail["session_output_dir"])
    )
    assert set(detail["capabilities"]) == TEAM_HARNESS_CAPABILITIES


@pytest.mark.asyncio
async def test_watcher_failure_is_finalized_before_structured_caller_error(
    monkeypatch, tmp_path
):
    """Preserve watcher diagnostics through the structured SDK failure path."""

    context = _caller_context(tmp_path)

    class FakeClient:
        api_base = "http://localhost:11434/v1"

        async def aclose(self):
            """Close the synthetic coordinator client."""

            return None

    class ManagerWithFailingWatcher(AgentManager):
        def __init__(self) -> None:
            """Register one lifecycle task that fails after startup."""

            super().__init__()

            async def fail_after_coordinator_starts() -> None:
                """Emit the exact diagnostic expected in caller-owned traces."""

                await asyncio.sleep(0)
                raise OSError("raw watcher diagnostic is retained")

            self.track_finalization_task(
                task=asyncio.create_task(fail_after_coordinator_starts())
            )

    async def fake_resolve_model_limit(*, model_id, client, config):
        """Return a stable context size without provider discovery."""

        return 128_000

    async def fake_run(messages, **kwargs):
        """Complete a coordinator turn without starting real workers."""

        messages.append({"role": "assistant", "content": "done"})

    monkeypatch.setattr("team_harness.harness.AgentManager", ManagerWithFailingWatcher)
    monkeypatch.setattr(
        "team_harness.harness._make_client", lambda config: FakeClient()
    )
    monkeypatch.setattr(config_module, "CONFIG_PATH", tmp_path / "home" / "config.toml")
    monkeypatch.setattr(
        "team_harness.harness.validate_templates", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        "team_harness.harness.resolve_model_limit", fake_resolve_model_limit
    )
    monkeypatch.setattr("team_harness.harness.load_skill_metadata", lambda cwd=None: [])
    monkeypatch.setattr("team_harness.harness.run", fake_run)

    with pytest.raises(TeamHarnessError, match="Worker finalization failed") as caught:
        await TeamHarness(
            api_base="http://localhost:11434/v1",
            cwd=str(tmp_path),
            caller_context=context,
        ).run("task")

    detail = caught.value.detail or {}
    run_json_path = Path(str(detail["run_json_path"]))
    session_output_dir = Path(str(detail["session_output_dir"]))
    worker_sessions_path = session_output_dir / "worker_sessions.json"
    assert run_json_path.is_file()
    assert worker_sessions_path.is_file()
    assert run_json_path.parent == session_output_dir
    run_payload = json.loads(run_json_path.read_text(encoding="utf-8"))
    assert run_payload["end"] is not None
    assert (
        "Worker finalization failed (OSError: raw watcher diagnostic is retained)"
        in run_payload["error"]
    )
    assert "raw watcher diagnostic is retained" in str(detail["summary"])
    assert json.loads(worker_sessions_path.read_text(encoding="utf-8"))["workers"] == []


@pytest.mark.asyncio
async def test_finalize_cancels_and_settles_overdue_capture_task(tmp_path):
    """Persist final artifacts without leaving an overdue harness task pending."""

    run_dir = tmp_path / "overdue-capture-run"
    run_dir.mkdir()
    manager = AgentManager()
    run_log = RunLogWriter(
        run_id="run_overdue_capture",
        run_dir=run_dir,
        provider="test",
        model="test",
        api_base="http://localhost",
        session_output_dir=str(run_dir),
    )

    async def never_finishing_capture() -> None:
        """Model provider-session capture that exceeds its finalization bound."""

        await asyncio.Event().wait()

    capture_task = asyncio.create_task(never_finishing_capture())
    manager.track_finalization_task(task=capture_task)

    await asyncio.wait_for(
        _finalize_run(
            manager=manager,
            run_log=run_log,
            session_output_dir=run_dir,
            shutdown_timeout_s=0.01,
            ui=SilentConsole(),
        ),
        timeout=0.5,
    )

    run_payload = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert "worker watcher/session-capture phase" in str(run_payload["error"])
    assert "FinalizationTimeoutError" in str(run_payload["error"])
    assert (run_dir / "worker_sessions.json").is_file()
    assert capture_task.cancelled()


@pytest.mark.asyncio
async def test_finalize_records_bounded_shutdown_phase(monkeypatch, tmp_path):
    """Record and settle a shutdown task that exceeds the configured bound."""

    run_dir = tmp_path / "hung-shutdown-run"
    run_dir.mkdir()
    manager = AgentManager()
    run_log = RunLogWriter(
        run_id="run_hung_shutdown",
        run_dir=run_dir,
        provider="test",
        model="test",
        api_base="http://localhost",
        session_output_dir=str(run_dir),
    )

    async def never_finishing_shutdown(**kwargs) -> None:
        """Model a shutdown phase that remains pending until cancellation."""

        del kwargs
        await asyncio.Event().wait()

    monkeypatch.setattr(
        "team_harness.harness._graceful_shutdown", never_finishing_shutdown
    )

    await asyncio.wait_for(
        _finalize_run(
            manager=manager,
            run_log=run_log,
            session_output_dir=run_dir,
            shutdown_timeout_s=0.01,
            ui=SilentConsole(),
        ),
        timeout=0.5,
    )

    run_payload = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert "FinalizationTimeoutError" in str(run_payload["error"])
    assert "worker shutdown phase" in str(run_payload["error"])
    assert (run_dir / "worker_sessions.json").is_file()


def test_force_kill_signals_helper_group_after_leader_completed(monkeypatch, tmp_path):
    """Kill surviving helpers without changing their completed leader's status."""

    class CompletedProcess:
        returncode = 0

        def kill(self) -> None:
            """Fail if finalization tries to kill an already-completed leader."""

            raise AssertionError("completed leader must not be killed")

    signalled: list[tuple[int, signal.Signals]] = []

    def record_group_signal(*, pgid: int, sig: signal.Signals) -> bool:
        """Record the trusted helper-group signal without touching the OS."""

        signalled.append((pgid, sig))
        return True

    completed_at = datetime.now(timezone.utc)
    manager = AgentManager()
    manager.register(
        state=AgentState(
            id="agent_completed_leader",
            agent_type="codex",
            prompt="completed leader",
            cwd=str(tmp_path),
            proc=CompletedProcess(),
            spawn_time=completed_at,
            stdout_log=tmp_path / "stdout.log",
            stderr_log=tmp_path / "stderr.log",
            status="done",
            exit_code=0,
            finished_at=completed_at,
            pgid=4321,
        )
    )
    monkeypatch.setattr("team_harness.harness.signal_group", record_group_signal)

    failures = _force_kill_unreaped_workers(manager=manager)

    state = manager.get(agent_id="agent_completed_leader")
    assert failures == ()
    assert signalled == [(4321, signal.SIGKILL)]
    assert state.status == "done"
    assert state.exit_code == 0
    assert state.finished_at == completed_at


def test_asyncio_run_returns_after_probe_failure_for_sigterm_ignoring_worker(
    monkeypatch, tmp_path
):
    """Force-kill an unreaped worker so asyncio.run has no pending proc waiter."""

    def fail_process_probe(*, pgid: int):
        """Model process-table failure while checking the trusted worker group."""

        raise ProcessProbeError(f"process table unavailable for pgid {pgid}")

    async def scenario() -> None:
        """Run finalization around a real worker that deliberately ignores SIGTERM."""

        run_dir = tmp_path / "real-process-probe-failure-run"
        run_dir.mkdir()
        stdout_log = run_dir / "worker.stdout.log"
        stderr_log = run_dir / "worker.stderr.log"
        stdout_log.touch()
        stderr_log.touch()
        worker_code = (
            "import signal\n"
            "import time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "print('ready', flush=True)\n"
            "while True:\n"
            "    time.sleep(1)\n"
        )
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            worker_code,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            assert proc.stdout is not None
            assert (
                await asyncio.wait_for(proc.stdout.readline(), timeout=2) == b"ready\n"
            )
            manager = AgentManager()
            spawned_at = datetime.now(timezone.utc)
            state = AgentState(
                id="agent_probe_failure",
                agent_type="codex",
                prompt="ignore SIGTERM",
                cwd=str(tmp_path),
                proc=proc,
                spawn_time=spawned_at,
                stdout_log=stdout_log,
                stderr_log=stderr_log,
                status="killed",
                pgid=proc.pid,
            )
            manager.register(state=state)
            run_log = RunLogWriter(
                run_id="run_real_probe_failure",
                run_dir=run_dir,
                provider="test",
                model="test",
                api_base="http://localhost",
                session_output_dir=str(run_dir),
            )
            run_log.record_agent_spawn(
                record=AgentRecord(
                    id=state.id,
                    agent_type=state.agent_type,
                    status=state.status,
                    cwd=state.cwd,
                    prompt=state.prompt,
                    full_prompt=state.prompt,
                    command=[sys.executable, "-c", worker_code],
                    spawned_at=spawned_at,
                    stdout_log=str(stdout_log),
                    stderr_log=str(stderr_log),
                    pid=proc.pid,
                    pgid=proc.pid,
                )
            )
            worker_done = asyncio.Event()

            async def watch_worker() -> None:
                """Mirror the production watcher and always release capture."""

                try:
                    await manager.wait_one(agent_id=state.id)
                finally:
                    worker_done.set()

            async def capture_after_worker() -> None:
                """Mirror capture waiting for the watcher final-tail signal."""

                await worker_done.wait()

            watch_task = asyncio.create_task(watch_worker(), name="test-worker-watch")
            capture_task = asyncio.create_task(
                capture_after_worker(), name="test-worker-capture"
            )
            manager.track_finalization_task(task=watch_task)
            manager.track_finalization_task(task=capture_task)
            os.killpg(proc.pid, signal.SIGTERM)
            await asyncio.sleep(0.02)
            assert proc.returncode is None

            await asyncio.wait_for(
                _finalize_run(
                    manager=manager,
                    run_log=run_log,
                    session_output_dir=run_dir,
                    shutdown_timeout_s=0.2,
                    ui=SilentConsole(),
                ),
                timeout=2,
            )

            assert proc.returncode is not None
            assert watch_task.done()
            assert capture_task.done()
            run_payload = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            assert "could not verify termination" in str(run_payload["error"])
            assert (run_dir / "worker_sessions.json").is_file()
        finally:
            if proc.returncode is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await asyncio.wait_for(proc.wait(), timeout=2)

    monkeypatch.setattr("team_harness.agents.manager.group_members", fail_process_probe)
    asyncio.run(scenario())


@pytest.mark.asyncio
async def test_caller_contract_turns_config_exit_into_structured_failure(
    monkeypatch, tmp_path
):
    context = _caller_context(tmp_path)
    monkeypatch.setattr(
        "team_harness.harness.load_config",
        lambda **kwargs: (_ for _ in ()).throw(SystemExit("bad config")),
    )

    with pytest.raises(TeamHarnessError, match="preflight failed") as caught:
        await TeamHarness(caller_context=context).run("task")

    detail = caught.value.detail or {}
    run_json_path = Path(str(detail["run_json_path"]))
    coordinator_input_path = Path(str(detail["coordinator_input_path"]))
    assert run_json_path.is_file()
    assert coordinator_input_path.is_file()
    assert run_json_path.parent.parent == context.trace_root
    input_payload = json.loads(coordinator_input_path.read_text(encoding="utf-8"))
    assert input_payload["status"] == "incomplete"
    assert input_payload["messages"] == [{"role": "user", "content": "task"}]


@pytest.mark.asyncio
async def test_direct_spawn_writes_dynamic_assignment_and_effective_prompt(tmp_path):
    context = _caller_context(tmp_path)
    run_dir = tmp_path / "harness-run"
    run_dir.mkdir()
    config = Config(
        cwd=str(tmp_path),
        run_dir=run_dir,
        worker_suffix="Verify your result.",
        agent_templates={"codex": fake_agent_template()},
    )
    manager = AgentManager()
    run_log = RunLogWriter(
        run_id="run_1",
        run_dir=run_dir,
        provider=config.provider,
        model=config.model,
        api_base=config.api_base,
    )
    bindings = build_agent_tool_bindings(
        manager=manager,
        run_log=run_log,
        config=config,
        ui=SilentConsole(),
        allowed_types=["codex"],
        session_output_dir=str(run_dir),
        caller_context=context,
    )
    spawn_fn = next(
        fn for schema, fn in bindings if schema["function"]["name"] == "spawn_agent"
    )

    agent_id = await spawn_fn(
        type="codex",
        prompt="Implement authentication",
        cwd=str(tmp_path),
        delegated_role="implementation-specialist",
        delegated_task_id="impl-auth-flow",
        expected_outputs=["code changes", "test evidence"],
        state_responsibility="Report state changes; coordinator owns acceptance.",
    )
    await manager.wait_one(agent_id)

    record = run_log.snapshot_agents()[0]
    assignment_path = Path(record.assignment_path or "")
    assignment = json.loads(assignment_path.read_text(encoding="utf-8"))
    assert assignment["delegated_role"] == "implementation-specialist"
    assert assignment["delegated_task_id"] == "impl-auth-flow"
    assert assignment["expected_outputs"] == ["code changes", "test evidence"]
    assert assignment["state_responsibility"].startswith("Report state changes")
    assert assignment["assignment_path"] == str(assignment_path)
    assert assignment["parent_assignment_path"] == str(context.parent_assignment_path)
    assert "agent_assignment_path" not in assignment
    assert assignment["authored_prompt"] == "Implement authentication"
    assert str(assignment_path) in assignment["effective_prompt"]
    assert str(context.parent_assignment_path) in assignment["effective_prompt"]
    assert "Parent harness run id: run_1" in assignment["effective_prompt"]
    assert record.prompt == assignment["authored_prompt"]
    assert record.full_prompt == assignment["effective_prompt"]


@pytest.mark.asyncio
async def test_caller_spawn_captures_streams_session_and_failure_artifacts(tmp_path):
    context = _caller_context(tmp_path)
    thread_id = "thread-session-id-123"
    output_marker = "worker-output-marker-123"
    stdout_event = {
        "type": "thread.started",
        "thread_id": thread_id,
        "content": output_marker,
    }
    stderr_text = "worker stderr marker"
    worker = tmp_path / "capture_worker.py"
    worker.write_text(
        "import json\n"
        "import sys\n"
        f"print(json.dumps({stdout_event!r}), flush=True)\n"
        f"print({stderr_text!r}, file=sys.stderr, flush=True)\n"
        "raise SystemExit(7)\n",
        encoding="utf-8",
    )
    template = AgentTemplate(
        command=(sys.executable, str(worker)),
        model_flag=None,
        session_capture=SessionCapture(
            strategy="stream_json_event",
            match={"type": "thread.started"},
            field_path=("thread_id",),
        ),
    )
    run_dir = tmp_path / "caller-run"
    run_dir.mkdir()
    config = Config(
        cwd=str(tmp_path), run_dir=run_dir, agent_templates={"codex": template}
    )
    manager = AgentManager()
    run_log = RunLogWriter(
        run_id="run_capture",
        run_dir=run_dir,
        provider=config.provider,
        model=config.model,
        api_base=config.api_base,
        session_output_dir=str(run_dir),
    )
    bindings = build_agent_tool_bindings(
        manager=manager,
        run_log=run_log,
        config=config,
        ui=SilentConsole(),
        allowed_types=["codex"],
        session_output_dir=str(run_dir),
        caller_context=context,
    )
    spawn_fn = next(
        fn for schema, fn in bindings if schema["function"]["name"] == "spawn_agent"
    )

    agent_id = await spawn_fn(
        type="codex",
        prompt="Emit the synthetic failure",
        cwd=str(tmp_path),
        delegated_role="failure-test",
    )
    exit_code = await asyncio.wait_for(manager.wait_one(agent_id), 10)
    assert exit_code == 7
    await manager.await_finalization_tasks(timeout_s=config.shutdown_timeout_s)

    state = manager.get(agent_id)
    stdout_text = state.stdout_log.read_text(encoding="utf-8")
    stderr_captured = state.stderr_log.read_text(encoding="utf-8")
    stdout_payload = json.loads(stdout_text)
    assert stdout_payload["thread_id"] == thread_id
    assert stdout_payload["content"] == output_marker
    assert stderr_captured.strip() == stderr_text
    assert extract_session_id(template, stdout_text.encode(), None) == thread_id

    records = run_log.snapshot_agents()
    assert records[0].session_id == thread_id
    persisted_record = json.loads((run_dir / "run.json").read_text())["agents"][0]
    assert persisted_record["session_id"] == thread_id
    manifest_path = write_worker_sessions_manifest(
        run_id=run_log.run_id, session_output_dir=run_dir, agents=records
    )
    failure_detail = build_worker_failure_detail(
        summary="synthetic worker failure", agents=records, session_output_dir=run_dir
    )
    persisted_failure = manifest_path.read_text(encoding="utf-8") + json.dumps(
        failure_detail
    )
    assert output_marker in persisted_failure
    assert stderr_text in persisted_failure


@pytest.mark.asyncio
async def test_finalize_awaits_provider_session_capture_before_snapshots(
    monkeypatch, tmp_path
):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    config = Config(
        cwd=str(tmp_path),
        run_dir=run_dir,
        shutdown_timeout_s=5,
        agent_templates={"codex": fake_agent_template()},
    )
    manager = AgentManager()
    run_log = RunLogWriter(
        run_id="run_finalize",
        run_dir=run_dir,
        provider=config.provider,
        model=config.model,
        api_base=config.api_base,
        session_output_dir=str(run_dir),
    )

    async def capture_after_worker_stop(**kwargs):
        await kwargs["stop_event"].wait()
        await asyncio.sleep(0.02)
        return "provider-session-final-tail"

    monkeypatch.setattr(
        "team_harness.tools.agent_tools.capture_session_id_from_path",
        capture_after_worker_stop,
    )
    bindings = build_agent_tool_bindings(
        manager=manager,
        run_log=run_log,
        config=config,
        ui=SilentConsole(),
        allowed_types=["codex"],
        session_output_dir=str(run_dir),
    )
    spawn_fn = next(
        fn for schema, fn in bindings if schema["function"]["name"] == "spawn_agent"
    )
    await spawn_fn(type="codex", prompt="finish", cwd=str(tmp_path))

    await _finalize_run(
        manager=manager,
        run_log=run_log,
        session_output_dir=run_dir,
        shutdown_timeout_s=5,
        ui=SilentConsole(),
    )

    run_payload = json.loads((run_dir / "run.json").read_text())
    sessions_payload = json.loads((run_dir / "worker_sessions.json").read_text())
    assert run_payload["agents"][0]["session_id"] == "provider-session-final-tail"
    assert (
        sessions_payload["workers"][0]["session"]["provider_session_id"]
        == "provider-session-final-tail"
    )


@pytest.mark.asyncio
async def test_nested_harness_inherits_outer_identity_and_parent_run(
    monkeypatch, tmp_path
):
    context = _caller_context(tmp_path)
    worker = tmp_path / "nested_context_worker.py"
    worker.write_text(
        "import os\nprint(os.environ['TEAM_HARNESS_CALLER_CONTEXT'], flush=True)\n",
        encoding="utf-8",
    )
    run_dir = tmp_path / "nested-parent-run"
    run_dir.mkdir()
    config = Config(
        cwd=str(tmp_path),
        run_dir=run_dir,
        agent_templates={
            "harness": AgentTemplate(
                command=(sys.executable, str(worker)), model_flag=None
            )
        },
    )
    manager = AgentManager()
    run_log = RunLogWriter(
        run_id="run_parent",
        run_dir=run_dir,
        provider=config.provider,
        model=config.model,
        api_base=config.api_base,
        session_output_dir=str(run_dir),
    )
    bindings = build_agent_tool_bindings(
        manager=manager,
        run_log=run_log,
        config=config,
        ui=SilentConsole(),
        allowed_types=["harness"],
        session_output_dir=str(run_dir),
        caller_context=context,
    )
    spawn_fn = next(
        fn for schema, fn in bindings if schema["function"]["name"] == "spawn_agent"
    )

    agent_id = await spawn_fn(
        type="harness",
        prompt="Coordinate the delegated review",
        cwd=str(tmp_path),
        env={INHERITED_CALLER_CONTEXT_ENV: '{"spoofed": true}'},
    )
    await manager.await_finalization_tasks(timeout_s=config.shutdown_timeout_s)

    record = run_log.snapshot_agents()[0]
    inherited = CallerContext.model_validate_json(
        manager.get(agent_id).stdout_log.read_text()
    )
    assert inherited.parent_harness_run_id == "run_parent"
    assert inherited.parent_assignment_path == Path(record.assignment_path or "")
    assert (
        inherited.trace_root
        == Path(record.assignment_path or "").parent / "harness_runs"
    )
    assert inherited.parent_attempt_id == context.parent_attempt_id
    assert inherited.session_id == context.session_id
    assert "Parent harness run id: run_parent" in record.full_prompt
    nested_footer = build_coordinator_context_footer(
        context=inherited,
        harness_run_id="run_nested",
        harness_run_dir=inherited.trace_root / "run_nested",
    )
    assert "Parent harness run id: run_parent" in nested_footer
    assert "Harness run id: run_nested" in nested_footer
    assert "delegated nested harness coordinator" in nested_footer
    monkeypatch.setenv(INHERITED_CALLER_CONTEXT_ENV, inherited.model_dump_json())
    assert TeamHarness()._caller_context == inherited


@pytest.mark.asyncio
async def test_stale_nested_context_is_not_inherited_by_generic_worker(
    monkeypatch, tmp_path
):
    worker = tmp_path / "generic_worker.py"
    worker.write_text(
        "import os\nprint(os.environ.get('TEAM_HARNESS_CALLER_CONTEXT', 'absent'))\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(INHERITED_CALLER_CONTEXT_ENV, '{"stale": true}')
    config = Config(
        agent_templates={
            "codex": AgentTemplate(
                command=(sys.executable, str(worker)), model_flag=None
            )
        }
    )

    result = await spawn(
        agent_id="agent_generic",
        agent_type="codex",
        prompt="ignored",
        cwd=tmp_path,
        config=config,
        log_dir=tmp_path / "logs",
    )
    assert await asyncio.wait_for(result.proc.wait(), 5) == 0
    assert (tmp_path / "logs" / "agent_generic_stdout.log").read_text().strip() == (
        "absent"
    )
