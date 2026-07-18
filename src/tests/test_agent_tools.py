# pyright: reportMissingParameterType=false

import asyncio
from datetime import datetime
from datetime import timezone
import json

import pytest

from team_harness.agents.manager import AgentState
from team_harness.agents.template import AgentTemplate
from team_harness.cli import _graceful_shutdown
from team_harness.tools import agent_tools
from team_harness.tracking.models import AgentRecord
from team_harness.tracking.run_log import RunLogWriter
from team_harness.tracking.worker_sessions import write_worker_sessions_manifest
from tests.helpers import fake_agent_template


def test_spawn_agent_schema_exposes_resume_fields():
    """The tool schema must distinguish live agent refs from raw session ids."""

    schema = agent_tools.spawn_agent_schema(allowed_types=["codex", "claude"])
    properties = schema["function"]["parameters"]["properties"]

    assert properties["mode"] == {
        "type": "string",
        "enum": ["fresh", "resume"],
        "description": (
            "Use 'resume' to continue an existing provider session instead of "
            "starting a fresh one."
        ),
    }
    assert properties["resume_from_session_id"]["type"] == "string"
    assert "worker_sessions.json" in properties["resume_from_session_id"]["description"]
    assert properties["resume_from_agent_id"]["type"] == "string"
    assert properties["resume_from_agent_id"]["minLength"] == 1
    assert "live harness run" in properties["resume_from_agent_id"]["description"]
    assert "mutually exclusive" in properties["resume_from_agent_id"]["description"]
    assert "output_path" not in properties
    assert "filesystem-safe worker label" in properties["worker_label"]["description"]
    assert schema["function"]["parameters"]["additionalProperties"] is False


def test_spawn_agent_schema_exposes_effort_field():
    schema = agent_tools.spawn_agent_schema(["codex", "claude"])
    properties = schema["function"]["parameters"]["properties"]

    assert properties["effort"]["type"] == "string"
    assert "reasoning-effort" in properties["effort"]["description"]
    assert "effort" not in schema["function"]["parameters"]["required"]


def test_spawn_agent_schema_describes_template_default_flags(config):
    schema = agent_tools.spawn_agent_schema(["codex", "claude"], config=config)
    description = schema["function"]["description"]
    flags_description = schema["function"]["parameters"]["properties"]["flags"][
        "description"
    ]
    agent_lines = {
        line.split(":", 1)[0].removeprefix("- "): line
        for line in description.splitlines()
        if line.startswith("- ")
    }

    assert "Default CLI behavior by agent type" in description
    assert set(agent_lines) == {"codex", "claude"}

    codex_line = agent_lines["codex"]
    assert "--dangerously-bypass-approvals-and-sandbox" in codex_line
    assert "--skip-git-repo-check" in codex_line
    assert "--json" in codex_line
    assert "gpt-5.6-sol" in codex_line
    assert "--dangerously-skip-permissions" not in codex_line
    assert "--output-format" not in codex_line
    assert "--verbose" not in codex_line

    claude_line = agent_lines["claude"]
    assert "--dangerously-skip-permissions" in claude_line
    assert "--output-format" in claude_line
    assert "stream-json" in claude_line
    assert "--verbose" in claude_line
    assert "--dangerously-bypass-approvals-and-sandbox" not in claude_line
    assert "--skip-git-repo-check" not in claude_line
    assert "Additional non-default CLI flags" in flags_description


@pytest.mark.asyncio
async def test_spawn_agent_can_resume_provider_session(tmp_path, config, manager, ui):
    capture_file = tmp_path / "args.txt"
    fake_codex = tmp_path / "fake-codex"
    fake_codex.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$@" > "$CAPTURE_FILE"\n', encoding="utf-8"
    )
    fake_codex.chmod(0o755)
    config.run_dir = tmp_path
    config.worker_suffix = ""
    config.agent_templates = {
        "codex": AgentTemplate(
            command=(str(fake_codex), "exec"),
            shared_flags=("--json",),
            resume_prefix=("resume",),
            resume_flags=("{session_id}",),
            model_flag=None,
        )
    }
    run_log = RunLogWriter(
        run_id="run_1",
        run_dir=tmp_path,
        provider=config.provider,
        model=config.model,
        api_base=config.api_base,
    )
    agent_tools.setup(manager=manager, run_log=run_log, config=config, ui=ui)

    agent_id = await agent_tools.spawn_agent(
        type="codex",
        prompt="continue",
        cwd=str(tmp_path),
        mode="resume",
        resume_from_session_id="sid-123",
        env={"CAPTURE_FILE": str(capture_file)},
    )
    await asyncio.wait_for(manager.wait_one(agent_id), 2)

    args = capture_file.read_text(encoding="utf-8").splitlines()
    assert args[:4] == ["exec", "resume", "--json", "sid-123"]


@pytest.mark.asyncio
async def test_bound_spawn_agent_resolves_same_run_agent_session(tmp_path, ui):
    """The live-run agent id must resolve to the captured provider session id."""

    from team_harness.agents.manager import AgentManager
    from team_harness.config import Config

    capture_file = tmp_path / "args.txt"
    fake_codex = tmp_path / "fake-codex"
    fake_codex.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$@" > "$CAPTURE_FILE"\n', encoding="utf-8"
    )
    fake_codex.chmod(0o755)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    config = Config(
        provider="openai_compat",
        model="test/model",
        api_base="http://localhost:9999",
        api_key="test-key",
        cwd=str(tmp_path),
        run_dir=run_dir,
        worker_suffix="",
        agent_templates={
            "codex": AgentTemplate(
                command=(str(fake_codex), "exec"),
                shared_flags=("--json",),
                resume_prefix=("resume",),
                resume_flags=("{session_id}",),
                model_flag=None,
            )
        },
    )
    manager = AgentManager()
    source_proc = await asyncio.create_subprocess_exec("sh", "-lc", "exit 0")
    await asyncio.wait_for(source_proc.wait(), timeout=2)
    source_stdout = tmp_path / "source-stdout.jsonl"
    source_stderr = tmp_path / "source-stderr.log"
    source_stdout.write_text("", encoding="utf-8")
    source_stderr.write_text("", encoding="utf-8")
    manager.register(
        state=AgentState(
            id="agent_source",
            agent_type="codex",
            prompt="original",
            cwd=str(tmp_path),
            proc=source_proc,
            spawn_time=datetime.now(timezone.utc),
            stdout_log=source_stdout,
            stderr_log=source_stderr,
            session_id="captured-thread-123",
        )
    )
    run_log = RunLogWriter(
        run_id="run_1",
        run_dir=run_dir,
        provider=config.provider,
        model=config.model,
        api_base=config.api_base,
    )
    bindings = agent_tools.build_agent_tool_bindings(
        manager=manager, run_log=run_log, config=config, ui=ui, allowed_types=["codex"]
    )
    spawn_fn = next(
        fn for schema, fn in bindings if schema["function"]["name"] == "spawn_agent"
    )

    resumed_id = await spawn_fn(
        type="codex",
        prompt="continue",
        cwd=str(tmp_path),
        mode="resume",
        resume_from_agent_id="agent_source",
        env={"CAPTURE_FILE": str(capture_file)},
    )
    await asyncio.wait_for(manager.wait_one(agent_id=resumed_id), timeout=2)

    assert capture_file.read_text(encoding="utf-8").splitlines()[:4] == [
        "exec",
        "resume",
        "--json",
        "captured-thread-123",
    ]
    assert len(manager.list_all()) == 2
    assert len(run_log.snapshot_agents()) == 1


@pytest.mark.asyncio
async def test_same_run_resume_rejects_invalid_sources(tmp_path, manager):
    """Ambiguous, unknown, live, uncaptured, and cross-type sources must fail."""

    completed_proc = await asyncio.create_subprocess_exec("sh", "-lc", "exit 0")
    await asyncio.wait_for(completed_proc.wait(), timeout=2)
    running_proc = await asyncio.create_subprocess_exec("sleep", "5")
    stdout = tmp_path / "source-stdout.jsonl"
    stderr = tmp_path / "source-stderr.log"
    stdout.write_text("", encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    manager.register(
        state=AgentState(
            id="agent_uncaptured",
            agent_type="codex",
            prompt="original",
            cwd=str(tmp_path),
            proc=completed_proc,
            spawn_time=datetime.now(timezone.utc),
            stdout_log=stdout,
            stderr_log=stderr,
        )
    )
    manager.register(
        state=AgentState(
            id="agent_wrong_type",
            agent_type="claude",
            prompt="original",
            cwd=str(tmp_path),
            proc=completed_proc,
            spawn_time=datetime.now(timezone.utc),
            stdout_log=stdout,
            stderr_log=stderr,
            session_id="claude-session",
        )
    )
    manager.register(
        state=AgentState(
            id="agent_running",
            agent_type="codex",
            prompt="original",
            cwd=str(tmp_path),
            proc=running_proc,
            spawn_time=datetime.now(timezone.utc),
            stdout_log=stdout,
            stderr_log=stderr,
            session_id="live-session",
        )
    )

    cases = [
        (
            {
                "mode": "resume",
                "resume_from_agent_id": "agent_source",
                "resume_from_session_id": "raw-session",
            },
            "mutually exclusive",
        ),
        (
            {"mode": "fresh", "resume_from_agent_id": "agent_uncaptured"},
            "requires mode='resume'",
        ),
        (
            {"mode": "fresh", "resume_from_session_id": "raw-session"},
            "resume_from_session_id requires mode='resume'",
        ),
        ({"mode": "resume", "resume_from_agent_id": "agent_missing"}, "does not exist"),
        (
            {"mode": "resume", "resume_from_agent_id": "agent_wrong_type"},
            "not requested type",
        ),
        ({"mode": "resume", "resume_from_agent_id": "agent_running"}, "still running"),
        (
            {"mode": "resume", "resume_from_agent_id": "agent_uncaptured"},
            "no captured provider session id",
        ),
    ]
    try:
        for kwargs, expected_error in cases:
            session_id, error = agent_tools._resolve_resume_session_id(
                manager=manager, agent_type="codex", kwargs=kwargs
            )
            assert session_id is None
            assert error is not None
            assert expected_error in error
    finally:
        running_proc.terminate()
        await asyncio.wait_for(running_proc.wait(), timeout=2)


@pytest.mark.asyncio
async def test_invalid_same_run_resume_creates_no_worker_or_record(
    tmp_path, config, manager, ui
):
    """A rejected source reference must fail before spawn-side effects."""

    config.run_dir = tmp_path / "run"
    config.run_dir.mkdir(exist_ok=True)
    config.worker_suffix = ""
    config.agent_templates = {"codex": fake_agent_template()}
    run_log = RunLogWriter(
        run_id="run_1",
        run_dir=config.run_dir,
        provider=config.provider,
        model=config.model,
        api_base=config.api_base,
    )
    agent_tools.setup(manager=manager, run_log=run_log, config=config, ui=ui)

    result = await agent_tools.spawn_agent(
        type="codex",
        prompt="continue",
        cwd=str(tmp_path),
        mode="resume",
        resume_from_agent_id="agent_missing",
    )

    assert result.startswith("ERROR:")
    assert "does not exist" in result
    assert manager.list_all() == []
    assert run_log.snapshot_agents() == []
    assert not (config.run_dir / "agents").exists()


@pytest.mark.asyncio
async def test_spawn_agent_rejects_effort_for_unsupported_template(
    tmp_path, config, manager, ui
):
    """An effort override on a template with no reasoning_effort_flag must
    fail loudly, not silently drop the override."""
    config.run_dir = tmp_path
    config.agent_templates = {"codex": fake_agent_template()}
    run_log = RunLogWriter(
        run_id="run_1",
        run_dir=tmp_path,
        provider=config.provider,
        model=config.model,
        api_base=config.api_base,
    )
    agent_tools.setup(manager=manager, run_log=run_log, config=config, ui=ui)

    result = await agent_tools.spawn_agent(
        type="codex", prompt="hello", cwd=str(tmp_path), effort="high"
    )

    assert result.startswith("ERROR:")
    assert "reasoning-effort" in result
    assert manager.list_all() == []
    assert run_log.snapshot_agents() == []


@pytest.mark.asyncio
async def test_spawn_agent_effort_override_injected_and_recorded(
    tmp_path, config, manager, ui
):
    capture_file = tmp_path / "args.txt"
    fake_codex = tmp_path / "fake-codex"
    fake_codex.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$@" > "$CAPTURE_FILE"\n', encoding="utf-8"
    )
    fake_codex.chmod(0o755)
    config.run_dir = tmp_path
    config.worker_suffix = ""
    config.agent_templates = {
        "codex": AgentTemplate(
            command=(str(fake_codex), "exec"),
            model_flag=None,
            reasoning_effort="low",
            reasoning_effort_flag=("-c", "model_reasoning_effort={effort}"),
        )
    }
    run_log = RunLogWriter(
        run_id="run_1",
        run_dir=tmp_path,
        provider=config.provider,
        model=config.model,
        api_base=config.api_base,
    )
    agent_tools.setup(manager=manager, run_log=run_log, config=config, ui=ui)

    agent_id = await agent_tools.spawn_agent(
        type="codex",
        prompt="hello",
        cwd=str(tmp_path),
        effort="xhigh",
        env={"CAPTURE_FILE": str(capture_file)},
    )
    await asyncio.wait_for(manager.wait_one(agent_id), 2)

    args = capture_file.read_text(encoding="utf-8").splitlines()
    assert "model_reasoning_effort=xhigh" in args
    assert "model_reasoning_effort=low" not in args
    record = run_log.snapshot_agents()[0]
    assert record.requested_effort == "xhigh"
    assert record.effective_effort == "xhigh"
    assert record.requested_model is None
    assert record.effective_model is None


@pytest.mark.asyncio
async def test_spawn_agent_rejects_blank_effort(tmp_path, config, manager, ui):
    config.run_dir = tmp_path
    config.agent_templates = {
        "codex": AgentTemplate(
            command=("echo",),
            model_flag=None,
            reasoning_effort_flag=("-c", "model_reasoning_effort={effort}"),
        )
    }
    run_log = RunLogWriter(
        run_id="run_1",
        run_dir=tmp_path,
        provider=config.provider,
        model=config.model,
        api_base=config.api_base,
    )
    agent_tools.setup(manager=manager, run_log=run_log, config=config, ui=ui)

    result = await agent_tools.spawn_agent(
        type="codex", prompt="hello", cwd=str(tmp_path), effort="  "
    )

    assert result.startswith("ERROR:")
    assert "non-empty" in result
    assert manager.list_all() == []


@pytest.mark.asyncio
async def test_spawn_agent_rejects_effort_when_template_lacks_placeholder(
    tmp_path, config, manager, ui
):
    """A hand-written reasoning_effort_flag without {effort} cannot carry the
    value; treating it as supported would record an effort the worker never
    received."""
    config.run_dir = tmp_path
    config.agent_templates = {
        "codex": AgentTemplate(
            command=("echo",), model_flag=None, reasoning_effort_flag=("--effort",)
        )
    }
    run_log = RunLogWriter(
        run_id="run_1",
        run_dir=tmp_path,
        provider=config.provider,
        model=config.model,
        api_base=config.api_base,
    )
    agent_tools.setup(manager=manager, run_log=run_log, config=config, ui=ui)

    result = await agent_tools.spawn_agent(
        type="codex", prompt="hello", cwd=str(tmp_path), effort="high"
    )

    assert result.startswith("ERROR:")
    assert "{effort}" in result
    assert manager.list_all() == []


@pytest.mark.asyncio
async def test_spawn_agent_rejects_effort_colliding_with_raw_flags(
    tmp_path, config, manager, ui
):
    """effort= plus a raw flag carrying the same option would render the
    option twice — whichever the CLI honors, the audit record lies for the
    other. Covers both token shapes: codex's key=value prefix and claude's
    standalone option."""
    codex_shape = AgentTemplate(
        command=("echo",),
        model_flag=None,
        reasoning_effort_flag=("-c", "model_reasoning_effort={effort}"),
    )
    claude_shape = AgentTemplate(
        command=("echo",),
        model_flag=None,
        reasoning_effort_flag=("--effort", "{effort}"),
    )
    config.run_dir = tmp_path
    config.agent_templates = {"codex": codex_shape, "claude": claude_shape}
    run_log = RunLogWriter(
        run_id="run_1",
        run_dir=tmp_path,
        provider=config.provider,
        model=config.model,
        api_base=config.api_base,
    )
    agent_tools.setup(manager=manager, run_log=run_log, config=config, ui=ui)

    codex_result = await agent_tools.spawn_agent(
        type="codex",
        prompt="hello",
        cwd=str(tmp_path),
        effort="xhigh",
        flags=["-c", "model_reasoning_effort=low"],
    )
    claude_result = await agent_tools.spawn_agent(
        type="claude",
        prompt="hello",
        cwd=str(tmp_path),
        effort="high",
        flags=["--effort", "low"],
    )

    assert codex_result.startswith("ERROR:")
    assert "model_reasoning_effort=low" in codex_result
    assert claude_result.startswith("ERROR:")
    assert "--effort" in claude_result
    assert manager.list_all() == []


@pytest.mark.asyncio
async def test_bound_spawn_agent_closure_applies_and_records_effort(tmp_path, ui):
    """The production path spawns through build_agent_tool_bindings'
    closure, not the module-level function — it must behave identically."""
    from team_harness.agents.manager import AgentManager
    from team_harness.config import Config

    capture_file = tmp_path / "args.txt"
    fake_codex = tmp_path / "fake-codex"
    fake_codex.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$@" > "$CAPTURE_FILE"\n', encoding="utf-8"
    )
    fake_codex.chmod(0o755)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    config = Config(
        provider="openai_compat",
        model="test/model",
        api_base="http://localhost:9999",
        api_key="test-key",
        cwd=str(tmp_path),
        run_dir=run_dir,
        worker_suffix="",
        agent_templates={
            "codex": AgentTemplate(
                command=(str(fake_codex),),
                model_flag=None,
                reasoning_effort="low",
                reasoning_effort_flag=("-c", "model_reasoning_effort={effort}"),
            )
        },
    )
    manager = AgentManager()
    run_log = RunLogWriter(
        run_id="run_1",
        run_dir=run_dir,
        provider=config.provider,
        model=config.model,
        api_base=config.api_base,
    )
    bindings = agent_tools.build_agent_tool_bindings(
        manager=manager, run_log=run_log, config=config, ui=ui, allowed_types=["codex"]
    )
    spawn_fn = next(
        fn for schema, fn in bindings if schema["function"]["name"] == "spawn_agent"
    )

    unsupported = await spawn_fn(
        type="codex",
        prompt="hello",
        cwd=str(tmp_path),
        effort="xhigh",
        flags=["-c", "model_reasoning_effort=low"],
    )
    assert unsupported.startswith("ERROR:")

    agent_id = await spawn_fn(
        type="codex",
        prompt="hello",
        cwd=str(tmp_path),
        effort="xhigh",
        env={"CAPTURE_FILE": str(capture_file)},
    )
    await asyncio.wait_for(manager.wait_one(agent_id), 2)

    args = capture_file.read_text(encoding="utf-8").splitlines()
    assert "model_reasoning_effort=xhigh" in args
    record = run_log.snapshot_agents()[0]
    assert record.requested_effort == "xhigh"
    assert record.effective_effort == "xhigh"


@pytest.mark.asyncio
async def test_spawn_agent_records_template_default_effort_as_effective(
    tmp_path, config, manager, ui
):
    """Without a per-spawn override, effective_effort reflects the template
    default while requested_effort stays None — the audit trail shows the
    coordinator did not choose."""
    config.run_dir = tmp_path
    config.worker_suffix = ""
    config.agent_templates = {
        "codex": AgentTemplate(
            command=("echo",),
            model_flag=None,
            reasoning_effort="medium",
            reasoning_effort_flag=("-c", "model_reasoning_effort={effort}"),
        )
    }
    run_log = RunLogWriter(
        run_id="run_1",
        run_dir=tmp_path,
        provider=config.provider,
        model=config.model,
        api_base=config.api_base,
    )
    agent_tools.setup(manager=manager, run_log=run_log, config=config, ui=ui)

    agent_id = await agent_tools.spawn_agent(
        type="codex", prompt="hello", cwd=str(tmp_path)
    )
    await asyncio.wait_for(manager.wait_one(agent_id), 2)

    record = run_log.snapshot_agents()[0]
    assert record.requested_effort is None
    assert record.effective_effort == "medium"


@pytest.mark.asyncio
async def test_spawn_agent_appends_suffix_before_output_instruction(
    tmp_path, config, manager, ui
):
    config.run_dir = tmp_path
    config.agent_templates = {"codex": fake_agent_template()}
    config.worker_suffix = "Always include a brief verification note."
    run_log = RunLogWriter(
        run_id="run_1",
        run_dir=tmp_path,
        provider=config.provider,
        model=config.model,
        api_base=config.api_base,
    )
    agent_tools.setup(manager=manager, run_log=run_log, config=config, ui=ui)
    agent_id = await agent_tools.spawn_agent(
        type="codex", prompt="hello", cwd=str(tmp_path)
    )
    await asyncio.sleep(0.1)
    state = manager.get(agent_id)
    data = json.loads((tmp_path / "run.json").read_text())
    full_prompt = data["agents"][0]["full_prompt"]
    assert "hello" in full_prompt
    prompt_index = full_prompt.index("hello")
    suffix_index = full_prompt.index(config.worker_suffix)
    footer = agent_tools._build_worker_output_footer("", config)
    output_index = full_prompt.index(footer)
    assert prompt_index < suffix_index < output_index
    assert full_prompt.endswith(footer)
    assert state.agent_type == "codex"


@pytest.mark.asyncio
async def test_spawn_agent_treats_worker_label_as_session_log_directory(
    tmp_path, config, manager, ui
):
    config.run_dir = tmp_path / "run"
    config.run_dir.mkdir(exist_ok=True)
    config.agent_templates = {"codex": fake_agent_template()}
    session_output_dir = tmp_path / "session"
    run_log = RunLogWriter(
        run_id="run_1",
        run_dir=config.run_dir,
        provider=config.provider,
        model=config.model,
        api_base=config.api_base,
    )
    agent_tools.setup(
        manager=manager,
        run_log=run_log,
        config=config,
        ui=ui,
        session_output_dir=str(session_output_dir),
    )

    agent_id = await agent_tools.spawn_agent(
        type="codex",
        prompt="review the plan",
        cwd=str(tmp_path),
        worker_label="leaf2_plan_review_codex",
    )
    await asyncio.wait_for(manager.wait_one(agent_id), 2)
    await asyncio.sleep(0.1)
    state = manager.get(agent_id)

    assert (
        state.stdout_log
        == (
            session_output_dir
            / "workers"
            / f"leaf2_plan_review_codex__{agent_id}"
            / "stdout.jsonl"
        ).resolve()
    )
    assert (
        state.stderr_log
        == (
            session_output_dir
            / "workers"
            / f"leaf2_plan_review_codex__{agent_id}"
            / "stderr.log"
        ).resolve()
    )
    assert state.stdout_log.exists()
    assert state.stderr_log.exists()

    worker_dir = state.stdout_log.parent
    (worker_dir / "review.md").write_text("review", encoding="utf-8")
    manifest_path = write_worker_sessions_manifest(
        run_id="run_1",
        session_output_dir=session_output_dir,
        agents=run_log.snapshot_agents(),
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["workers"][0]["stdout_path"] == str(state.stdout_log.resolve())
    assert worker_dir.is_dir()
    assert (worker_dir / "review.md").read_text(encoding="utf-8") == "review"


async def test_spawn_agent_rejects_path_like_worker_label(
    tmp_path, config, manager, ui
):
    config.run_dir = tmp_path / "run"
    config.run_dir.mkdir(exist_ok=True)
    config.agent_templates = {"codex": fake_agent_template()}
    run_log = RunLogWriter(
        run_id="run_1",
        run_dir=config.run_dir,
        provider=config.provider,
        model=config.model,
        api_base=config.api_base,
    )
    agent_tools.setup(
        manager=manager,
        run_log=run_log,
        config=config,
        ui=ui,
        session_output_dir=str(tmp_path / "session"),
    )

    with pytest.raises(ValueError, match="worker_label"):
        await agent_tools.spawn_agent(
            type="codex",
            prompt="review the plan",
            cwd=str(tmp_path),
            worker_label="../escaped",
        )


async def test_spawn_agent_rejects_removed_output_path(tmp_path, config, manager, ui):
    config.run_dir = tmp_path / "run"
    config.run_dir.mkdir(exist_ok=True)
    config.agent_templates = {"codex": fake_agent_template()}
    run_log = RunLogWriter(
        run_id="run_1",
        run_dir=config.run_dir,
        provider=config.provider,
        model=config.model,
        api_base=config.api_base,
    )
    agent_tools.setup(
        manager=manager,
        run_log=run_log,
        config=config,
        ui=ui,
        session_output_dir=str(tmp_path / "session"),
    )

    with pytest.raises(ValueError, match="unknown spawn_agent fields: output_path"):
        await agent_tools.spawn_agent(
            type="codex",
            prompt="review the plan",
            cwd=str(tmp_path),
            output_path="old-stem",
        )


@pytest.mark.asyncio
async def test_read_new_output_waits_and_kills(tmp_path, config, manager, ui):
    stdout = tmp_path / "agent_stdout.log"
    stderr = tmp_path / "agent_stderr.log"
    stdout.write_text("one\n")
    stderr.write_text("")
    proc = await asyncio.create_subprocess_exec("sleep", "5")
    state = AgentState(
        id="agent_1",
        agent_type="codex",
        prompt="p",
        cwd=str(tmp_path),
        proc=proc,
        spawn_time=datetime.now(timezone.utc),
        stdout_log=stdout,
        stderr_log=stderr,
    )
    manager.register(state)
    run_log = RunLogWriter(
        run_id="run_1",
        run_dir=tmp_path,
        provider=config.provider,
        model=config.model,
        api_base=config.api_base,
    )
    agent_tools.setup(manager=manager, run_log=run_log, config=config, ui=ui)
    assert await agent_tools.read_new_agent_output("agent_1") == "one\n"
    stdout.write_text("one\ntwo\n")
    pieces = await asyncio.gather(
        agent_tools.read_new_agent_output("agent_1"),
        agent_tools.read_new_agent_output("agent_1"),
    )
    assert "".join(pieces) == "two\n"
    timeout_result = json.loads(
        await agent_tools.wait_for_any(["agent_1"], timeout=0.1)
    )
    assert timeout_result["timed_out"] is True
    assert json.loads(await agent_tools.wait_for_agents([], timeout=0.1)) == {
        "agents": {},
        "timed_out": False,
    }
    assert json.loads(await agent_tools.wait_for_any([], timeout=0.1)) == {
        "agent_id": None,
        "timed_out": False,
        "running": [],
    }
    result = json.loads(await agent_tools.kill_agent("agent_1"))
    assert result["killed"] is True
    assert result["agent_id"] == "agent_1"
    await asyncio.sleep(0.1)
    assert not any(
        event == "done" and agent_id == "agent_1" for event, agent_id in ui.agent_events
    )


@pytest.mark.asyncio
async def test_read_new_output_truncates_large_initial_backlog(
    tmp_path, config, manager, ui
):
    stdout = tmp_path / "agent_stdout.log"
    stderr = tmp_path / "agent_stderr.log"
    large_text = "a" * (agent_tools.READ_NEW_AGENT_OUTPUT_MAX_BYTES + 10)
    stdout.write_text(large_text)
    stderr.write_text("")
    proc = await asyncio.create_subprocess_exec("sh", "-lc", "exit 0")
    state = AgentState(
        id="agent_1",
        agent_type="codex",
        prompt="p",
        cwd=str(tmp_path),
        proc=proc,
        spawn_time=datetime.now(timezone.utc),
        stdout_log=stdout,
        stderr_log=stderr,
    )
    manager.register(state)
    run_log = RunLogWriter(
        run_id="run_1",
        run_dir=tmp_path,
        provider=config.provider,
        model=config.model,
        api_base=config.api_base,
    )
    agent_tools.setup(manager=manager, run_log=run_log, config=config, ui=ui)

    first = await agent_tools.read_new_agent_output("agent_1")
    stdout.write_text(large_text + "tail\n")
    second = await agent_tools.read_new_agent_output("agent_1")
    await proc.wait()

    assert first.startswith("[read_new_agent_output truncated:")
    assert (
        f"showing latest {agent_tools.READ_NEW_AGENT_OUTPUT_MAX_BYTES} bytes" in first
    )
    assert str(stdout) in first
    assert first.endswith("a" * agent_tools.READ_NEW_AGENT_OUTPUT_MAX_BYTES)
    assert second == "tail\n"


@pytest.mark.asyncio
async def test_wait_for_any_advances_read_new_output_cursor(
    tmp_path, config, manager, ui
):
    stdout = tmp_path / "agent_stdout.log"
    stderr = tmp_path / "agent_stderr.log"
    stdout.write_text("before\n")
    stderr.write_text("")
    proc = await asyncio.create_subprocess_exec("sleep", "5")
    state = AgentState(
        id="agent_1",
        agent_type="codex",
        prompt="p",
        cwd=str(tmp_path),
        proc=proc,
        spawn_time=datetime.now(timezone.utc),
        stdout_log=stdout,
        stderr_log=stderr,
    )
    manager.register(state)
    run_log = RunLogWriter(
        run_id="run_1",
        run_dir=tmp_path,
        provider=config.provider,
        model=config.model,
        api_base=config.api_base,
    )
    agent_tools.setup(manager=manager, run_log=run_log, config=config, ui=ui)

    timeout_result = json.loads(
        await agent_tools.wait_for_any(["agent_1"], timeout=0.1)
    )
    stdout.write_text("before\nafter\n")
    new_output = await agent_tools.read_new_agent_output("agent_1")
    result = json.loads(await agent_tools.kill_agent("agent_1"))

    assert timeout_result["timed_out"] is True
    assert new_output == "after\n"
    assert result["killed"] is True


@pytest.mark.asyncio
async def test_list_agents_and_graceful_shutdown(tmp_path, config, manager, ui):
    stdout = tmp_path / "agent_stdout.log"
    stderr = tmp_path / "agent_stderr.log"
    proc = await asyncio.create_subprocess_exec("sh", "-lc", "echo hello")
    state = AgentState(
        id="agent_1",
        agent_type="codex",
        prompt="p",
        cwd=str(tmp_path),
        proc=proc,
        spawn_time=datetime.now(timezone.utc),
        stdout_log=stdout,
        stderr_log=stderr,
    )
    manager.register(state)
    run_log = RunLogWriter(
        run_id="run_1",
        run_dir=tmp_path,
        provider=config.provider,
        model=config.model,
        api_base=config.api_base,
    )
    agent_tools.setup(manager=manager, run_log=run_log, config=config, ui=ui)
    await asyncio.sleep(0.1)
    payload = json.loads(await agent_tools.list_agents())
    assert payload[0]["status"] == "done (exit 0)"

    proc2 = await asyncio.create_subprocess_exec("sleep", "5")
    state2 = AgentState(
        id="agent_2",
        agent_type="codex",
        prompt="p",
        cwd=str(tmp_path),
        proc=proc2,
        spawn_time=datetime.now(timezone.utc),
        stdout_log=tmp_path / "agent2_stdout.log",
        stderr_log=tmp_path / "agent2_stderr.log",
    )
    manager.register(state2)
    run_log.record_agent_spawn(
        AgentRecord(
            id="agent_2",
            agent_type="codex",
            cwd=str(tmp_path),
            prompt="p",
            full_prompt="p",
            command=["sleep", "5"],
            spawned_at=state2.spawn_time,
            stdout_log=str(state2.stdout_log),
            stderr_log=str(state2.stderr_log),
        )
    )
    await _graceful_shutdown(manager=manager, run_log=run_log, ui=ui, timeout=0.01)
    data = json.loads((tmp_path / "run.json").read_text())
    statuses = {agent["id"]: agent["status"] for agent in data["agents"]}
    assert statuses["agent_2"] == "killed"


@pytest.mark.asyncio
async def test_list_agents_shows_killed_status(tmp_path, config, manager, ui):
    config.run_dir = tmp_path
    config.agent_templates = {
        "codex": AgentTemplate(command=("sh", "-lc", "sleep 5"), model_flag=None)
    }
    run_log = RunLogWriter(
        run_id="run_1",
        run_dir=tmp_path,
        provider=config.provider,
        model=config.model,
        api_base=config.api_base,
    )
    agent_tools.setup(manager=manager, run_log=run_log, config=config, ui=ui)

    agent_id = await agent_tools.spawn_agent(
        type="codex", prompt="hello", cwd=str(tmp_path)
    )
    result = json.loads(await agent_tools.kill_agent(agent_id))
    payload = json.loads(await agent_tools.list_agents())

    assert result["killed"] is True
    assert result["agent_id"] == agent_id
    assert len(payload) == 1
    assert payload[0]["id"] == agent_id
    assert payload[0]["type"] == "codex"
    assert payload[0]["status"] == "killed"
    assert payload[0]["cwd"] == str(tmp_path)


@pytest.mark.asyncio
async def test_spawn_agent_harness_depth_guard_blocks_spawn(
    tmp_path, config, manager, ui, monkeypatch
):
    config.run_dir = tmp_path
    config.max_depth = 2
    run_log = RunLogWriter(
        run_id="run_1",
        run_dir=tmp_path,
        provider=config.provider,
        model=config.model,
        api_base=config.api_base,
    )
    agent_tools.setup(manager=manager, run_log=run_log, config=config, ui=ui)
    monkeypatch.setenv("TEAM_HARNESS_DEPTH", "2")

    result = await agent_tools.spawn_agent(
        type="harness", prompt="hello", cwd=str(tmp_path)
    )

    assert result == "ERROR: max harness depth (2) reached"
    assert manager.list_all() == []
    data = json.loads((tmp_path / "run.json").read_text())
    assert data["agents"] == []


@pytest.mark.asyncio
async def test_kill_agent_updates_manager_and_run_log(tmp_path, config, manager, ui):
    stdout = tmp_path / "agent_stdout.log"
    stderr = tmp_path / "agent_stderr.log"
    proc = await asyncio.create_subprocess_exec("sleep", "5")
    state = AgentState(
        id="agent_1",
        agent_type="codex",
        prompt="p",
        cwd=str(tmp_path),
        proc=proc,
        spawn_time=datetime.now(timezone.utc),
        stdout_log=stdout,
        stderr_log=stderr,
    )
    manager.register(state)
    run_log = RunLogWriter(
        "run_1", tmp_path, config.provider, config.model, config.api_base
    )
    run_log.record_agent_spawn(
        AgentRecord(
            id="agent_1",
            agent_type="codex",
            cwd=str(tmp_path),
            prompt="p",
            full_prompt="p",
            command=["sleep", "5"],
            spawned_at=state.spawn_time,
            stdout_log=str(stdout),
            stderr_log=str(stderr),
        )
    )
    agent_tools.setup(manager, run_log, config, ui)

    result = json.loads(await agent_tools.kill_agent("agent_1"))

    assert result["killed"] is True
    assert result["agent_id"] == "agent_1"
    assert state.status == "killed"
    assert state.finished_at is not None
    assert state.exit_code is not None

    data = json.loads((tmp_path / "run.json").read_text())
    assert data["agents"][0]["status"] == "killed"
    assert data["agents"][0]["exit_code"] == state.exit_code
    assert ("killed", "agent_1") in ui.agent_events


@pytest.mark.asyncio
async def test_wait_for_any_includes_failure_classification_on_api_error(
    tmp_path, config, manager, ui
):
    """When an agent fails with API error patterns in stderr, wait_for_any
    should include a failure_classification in the response."""
    stdout = tmp_path / "agent_stdout.log"
    stderr = tmp_path / "agent_stderr.log"
    stdout.write_text("")
    stderr.write_text("Error: API request failed with status: 429 rate limit exceeded")
    proc = await asyncio.create_subprocess_exec("sh", "-lc", "exit 1")
    state = AgentState(
        id="agent_1",
        agent_type="codex",
        prompt="do the thing",
        cwd=str(tmp_path),
        proc=proc,
        spawn_time=datetime.now(timezone.utc),
        stdout_log=stdout,
        stderr_log=stderr,
    )
    manager.register(state)
    run_log = RunLogWriter(
        "run_1", tmp_path, config.provider, config.model, config.api_base
    )
    agent_tools.setup(manager, run_log, config, ui)

    result = json.loads(await agent_tools.wait_for_any(["agent_1"], timeout=5))

    assert result["timed_out"] is False
    assert result["finished_agent_id"] == "agent_1"
    assert "failure_classification" in result
    fc = result["failure_classification"]
    assert fc["is_api_error"] is True
    assert fc["category"] == "rate_limit"
    assert "suggested_action" in fc


@pytest.mark.asyncio
async def test_wait_for_any_no_classification_on_normal_failure(
    tmp_path, config, manager, ui
):
    """When an agent fails without API error patterns, no failure_classification
    should be present."""
    stdout = tmp_path / "agent_stdout.log"
    stderr = tmp_path / "agent_stderr.log"
    stdout.write_text("")
    stderr.write_text("Traceback: IndexError: list index out of range")
    proc = await asyncio.create_subprocess_exec("sh", "-lc", "exit 1")
    state = AgentState(
        id="agent_1",
        agent_type="codex",
        prompt="do the thing",
        cwd=str(tmp_path),
        proc=proc,
        spawn_time=datetime.now(timezone.utc),
        stdout_log=stdout,
        stderr_log=stderr,
    )
    manager.register(state)
    run_log = RunLogWriter(
        "run_1", tmp_path, config.provider, config.model, config.api_base
    )
    agent_tools.setup(manager, run_log, config, ui)

    result = json.loads(await agent_tools.wait_for_any(["agent_1"], timeout=5))

    assert result["timed_out"] is False
    assert "failure_classification" not in result


@pytest.mark.asyncio
async def test_wait_for_any_no_classification_on_success(tmp_path, config, manager, ui):
    """Successful agents should never have a failure_classification."""
    stdout = tmp_path / "agent_stdout.log"
    stderr = tmp_path / "agent_stderr.log"
    stdout.write_text("done")
    stderr.write_text("")
    proc = await asyncio.create_subprocess_exec("sh", "-lc", "exit 0")
    state = AgentState(
        id="agent_1",
        agent_type="codex",
        prompt="do the thing",
        cwd=str(tmp_path),
        proc=proc,
        spawn_time=datetime.now(timezone.utc),
        stdout_log=stdout,
        stderr_log=stderr,
    )
    manager.register(state)
    run_log = RunLogWriter(
        "run_1", tmp_path, config.provider, config.model, config.api_base
    )
    agent_tools.setup(manager, run_log, config, ui)

    result = json.loads(await agent_tools.wait_for_any(["agent_1"], timeout=5))

    assert result["timed_out"] is False
    assert "failure_classification" not in result


@pytest.mark.asyncio
async def test_read_agent_output_clamps_tail_bytes_and_banners(
    tmp_path, config, manager, ui
):
    stdout = tmp_path / "agent_stdout.log"
    stderr = tmp_path / "agent_stderr.log"
    stdout.write_text("S" * 5000)
    stderr.write_text("E" * 5000)
    proc = await asyncio.create_subprocess_exec("sh", "-lc", "exit 0")
    state = AgentState(
        id="agent_1",
        agent_type="codex",
        prompt="p",
        cwd=str(tmp_path),
        proc=proc,
        spawn_time=datetime.now(timezone.utc),
        stdout_log=stdout,
        stderr_log=stderr,
    )
    manager.register(state)
    await proc.wait()
    config.read_output_max_tail_bytes = 1024
    run_log = RunLogWriter(
        run_id="run_1",
        run_dir=tmp_path,
        provider=config.provider,
        model=config.model,
        api_base=config.api_base,
    )
    agent_tools.setup(manager=manager, run_log=run_log, config=config, ui=ui)

    result = await agent_tools.read_agent_output("agent_1", tail_bytes=100_000)

    assert result.startswith("[read_agent_output truncated:")
    # Banner names both full log paths.
    assert str(stdout) in result
    assert str(stderr) in result
    # Only the clamped tail (1024 bytes) is returned per stream.
    assert result.count("S") == 1024
    assert result.count("E") == 1024


@pytest.mark.asyncio
async def test_read_agent_output_no_banner_within_ceiling(
    tmp_path, config, manager, ui
):
    stdout = tmp_path / "agent_stdout.log"
    stderr = tmp_path / "agent_stderr.log"
    stdout.write_text("short out")
    stderr.write_text("short err")
    proc = await asyncio.create_subprocess_exec("sh", "-lc", "exit 0")
    state = AgentState(
        id="agent_1",
        agent_type="codex",
        prompt="p",
        cwd=str(tmp_path),
        proc=proc,
        spawn_time=datetime.now(timezone.utc),
        stdout_log=stdout,
        stderr_log=stderr,
    )
    manager.register(state)
    await proc.wait()
    run_log = RunLogWriter(
        run_id="run_1",
        run_dir=tmp_path,
        provider=config.provider,
        model=config.model,
        api_base=config.api_base,
    )
    agent_tools.setup(manager=manager, run_log=run_log, config=config, ui=ui)

    result = await agent_tools.read_agent_output("agent_1", tail_bytes=8192)

    assert "truncated" not in result
    assert result == "=== stdout ===\nshort out\n=== stderr ===\nshort err"


def test_build_direct_spawn_footer_contains_result_card_instruction(tmp_path):
    footer = agent_tools._build_direct_spawn_footer(
        assignment_path=tmp_path / "agent_assignment.json",
        output_dir=tmp_path / "out",
        caller_context=None,
        delegated_role=None,
        delegated_task_id=None,
        expected_outputs=[],
        state_responsibility=None,
        parent_harness_run_id="run_1",
    )

    assert "result card" in footer
    assert "at most 15 lines" in footer
    assert "Write long reports to files, not stdout." in footer
