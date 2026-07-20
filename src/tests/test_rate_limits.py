# pyright: reportMissingParameterType=false

from datetime import datetime
from datetime import timezone
import json
from pathlib import Path

import pytest

from team_harness.agents import rate_limits
from team_harness.agents.manager import AgentManager
from team_harness.agents.rate_limits import detect_rate_limit
from team_harness.agents.rate_limits import RateLimitCircuitBreaker
from team_harness.agents.rate_limits import RateLimitSignal
from team_harness.agents.template import AgentTemplate
from team_harness.config import Config
from team_harness.tools.agent_tools import build_agent_tool_bindings
from team_harness.tracking.run_log import RunLogWriter

FIXTURE = Path(__file__).parent / "fixtures" / "claude_rate_limit.jsonl"


def _binding(bindings, name):
    return next(fn for schema, fn in bindings if schema["function"]["name"] == name)


def _fake_worker(tmp_path: Path) -> Path:
    worker = tmp_path / "fake-worker"
    worker.write_text(
        """#!/bin/sh
if [ "$EMIT_RATE_LIMIT" = "1" ]; then
    /bin/cat "$RATE_LIMIT_FIXTURE"
    exit 1
fi
printf '%s\n' '{"type":"result","subtype":"success","is_error":false}'
""",
        encoding="utf-8",
    )
    worker.chmod(0o755)
    return worker


def _config(tmp_path: Path, worker: Path, *, enabled: bool = True) -> Config:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    return Config(
        provider="openai_compat",
        model="coordinator-model",
        api_base="https://example.invalid/v1",
        api_key="test-key",
        cwd=str(tmp_path),
        run_dir=run_dir,
        rate_limit_circuit_breaker=enabled,
        agent_templates={
            "claude": AgentTemplate(command=(str(worker),), model_flag="--model"),
            "codex": AgentTemplate(command=(str(worker),), model_flag="--model"),
        },
        allowed_agents=["claude", "codex"],
    )


def test_detects_terminal_rate_limit_from_partial_jsonl_fixture():
    signal = detect_rate_limit(FIXTURE.read_bytes())

    assert signal is not None
    assert signal.reason == "worker result reported api_error_status=429"
    assert signal.resets_at == datetime.fromtimestamp(1784811600, tz=timezone.utc)


def test_detects_rejected_event_without_result_and_ignores_non_429_result():
    rejected = detect_rate_limit(b'{"type":"rate_limit_event","status":"rejected"}\n')
    ordinary_failure = detect_rate_limit(
        b'{"type":"result","is_error":true,"api_error_status":500}\n'
    )

    assert rejected == RateLimitSignal(
        resets_at=None, reason="rate_limit_event reported status=rejected"
    )
    assert ordinary_failure is None


def test_default_cooldown_expires_automatically():
    current = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
    breaker = RateLimitCircuitBreaker(
        enabled=True, default_cooldown_s=900, now=lambda: current
    )
    trip = breaker.trip(
        family="gemini",
        model="gemini-pro",
        signal=RateLimitSignal(resets_at=None, reason="api_error_status=429"),
    )

    assert trip is not None
    assert (trip.resets_at - trip.tripped_at).total_seconds() == 900
    assert breaker.active_trip("gemini") == trip

    current = trip.resets_at
    assert breaker.active_trip("gemini") is None


@pytest.mark.asyncio
async def test_bound_spawn_short_circuits_family_and_reprobes_after_reset(
    tmp_path, ui, monkeypatch
):
    current = [datetime.fromtimestamp(1784811500, tz=timezone.utc)]
    monkeypatch.setattr(rate_limits, "_utc_now", lambda: current[0])
    worker = _fake_worker(tmp_path)
    config = _config(tmp_path, worker)
    run_dir = config.run_dir
    assert run_dir is not None
    manager = AgentManager()
    run_log = RunLogWriter(
        "run_1", run_dir, config.provider, config.model, config.api_base
    )
    bindings = build_agent_tool_bindings(
        manager=manager,
        run_log=run_log,
        config=config,
        ui=ui,
        allowed_types=["claude", "codex"],
    )
    spawn = _binding(bindings, "spawn_agent")
    wait_for_agents = _binding(bindings, "wait_for_agents")
    availability = _binding(bindings, "agent_availability")
    rate_limit_env = {"EMIT_RATE_LIMIT": "1", "RATE_LIMIT_FIXTURE": str(FIXTURE)}

    first_id = await spawn(
        type="claude",
        prompt="first attempt",
        cwd=str(tmp_path),
        model="claude-fable-5",
        env=rate_limit_env,
    )
    await wait_for_agents(agent_ids=[first_id], timeout=2)

    blocked = json.loads(
        await spawn(
            type="claude",
            prompt="wasteful retry",
            cwd=str(tmp_path),
            model="claude-fable-5",
        )
    )
    family_status = json.loads(await availability())
    persisted = json.loads((run_dir / "run.json").read_text())

    assert blocked["spawned"] is False
    assert blocked["status"] == "rate_limited"
    assert blocked["family"] == "claude"
    assert blocked["model"] == "claude-fable-5"
    assert blocked["available_families"] == ["codex"]
    assert len(manager.list_all()) == 1
    assert family_status["available_families"] == ["codex"]
    assert family_status["rate_limited_families"][0]["family"] == "claude"
    assert persisted["rate_limited_families"] == [
        {
            "family": "claude",
            "model": "claude-fable-5",
            "tripped_at": "2026-07-23T12:58:20Z",
            "resets_at": "2026-07-23T13:00:00Z",
            "reason": "worker result reported api_error_status=429",
        }
    ]

    current[0] = datetime.fromtimestamp(1784811601, tz=timezone.utc)
    retry_id = await spawn(
        type="claude",
        prompt="probe after reset",
        cwd=str(tmp_path),
        model="claude-fable-5",
        env={"EMIT_RATE_LIMIT": "0"},
    )
    await wait_for_agents(agent_ids=[retry_id], timeout=2)
    failures = await manager.await_finalization_tasks(timeout_s=2)

    assert retry_id.startswith("agent_")
    assert len(manager.list_all()) == 2
    assert failures == ()


@pytest.mark.asyncio
async def test_config_knob_disables_detection_recording_and_short_circuit(tmp_path, ui):
    worker = _fake_worker(tmp_path)
    config = _config(tmp_path, worker, enabled=False)
    run_dir = config.run_dir
    assert run_dir is not None
    manager = AgentManager()
    run_log = RunLogWriter(
        "run_1", run_dir, config.provider, config.model, config.api_base
    )
    bindings = build_agent_tool_bindings(
        manager=manager,
        run_log=run_log,
        config=config,
        ui=ui,
        allowed_types=["claude", "codex"],
    )
    spawn = _binding(bindings, "spawn_agent")
    wait_for_agents = _binding(bindings, "wait_for_agents")
    availability = _binding(bindings, "agent_availability")

    first_id = await spawn(
        type="claude",
        prompt="first attempt",
        cwd=str(tmp_path),
        env={"EMIT_RATE_LIMIT": "1", "RATE_LIMIT_FIXTURE": str(FIXTURE)},
    )
    await wait_for_agents(agent_ids=[first_id], timeout=2)
    second_id = await spawn(
        type="claude",
        prompt="retry remains enabled",
        cwd=str(tmp_path),
        env={"EMIT_RATE_LIMIT": "0"},
    )
    await wait_for_agents(agent_ids=[second_id], timeout=2)
    failures = await manager.await_finalization_tasks(timeout_s=2)
    status = json.loads(await availability())
    persisted = json.loads((run_dir / "run.json").read_text())

    assert first_id.startswith("agent_")
    assert second_id.startswith("agent_")
    assert len(manager.list_all()) == 2
    assert status["circuit_breaker_enabled"] is False
    assert status["available_families"] == ["claude", "codex"]
    assert persisted["rate_limited_families"] == []
    assert failures == ()
