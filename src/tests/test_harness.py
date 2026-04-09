# pyright: reportMissingParameterType=false, reportArgumentType=false

import asyncio
import inspect
import json

import click
import pytest

from team_harness import config as config_module
from team_harness.agents.manager import AgentManager
from team_harness.cli import main
from team_harness.config import Config
from team_harness.harness import _extract_final_text
from team_harness.harness import _normalize_agents
from team_harness.harness import _show_no_config_hint
from team_harness.harness import _warn_provider_startup
from team_harness.harness import AgentSummary
from team_harness.harness import Harness
from team_harness.harness import HarnessError
from team_harness.harness import HarnessResult
from team_harness.tools.agent_tools import build_agent_tool_bindings
from team_harness.tools.fs_tools import build_fs_tool_bindings
from team_harness.tools.todo_tools import build_todo_tool_bindings
from team_harness.tracking.context import ContextTracker
from team_harness.tracking.run_log import RunLogWriter
from team_harness.ui.console import make_console
from team_harness.ui.console import PlainConsole
from team_harness.ui.console import SilentConsole

# ---------------------------------------------------------------------------
# CLI / SDK parity test
# ---------------------------------------------------------------------------


def test_cli_sdk_parity():
    """Every CLI option and env var must have an SDK equivalent on Harness.__init__."""
    # Extract Click options from run_cli command
    run_cmd = main.commands["run"]
    cli_param_names = set()
    for param in run_cmd.params:
        if isinstance(param, click.Option):
            cli_param_names.add(param.name)

    # Remove task and task_file which are run()-level, not constructor-level
    cli_param_names.discard("task")
    cli_param_names.discard("task_file")

    # Map CLI param names to SDK equivalents
    cli_to_sdk = {
        "provider": "provider",
        "model": "model",
        "api_base": "api_base",
        "api_key": "api_key",
        "codex_auth_path": "codex_auth_path",
        "allowed_agents": "agents",
        "max_turns": "max_turns",
        "max_retries": "max_retries",
        "max_depth": "max_depth",
        "system_prompt": "system_prompt",
        "system_prompt_file": "system_prompt_file",
        "cwd": "cwd",
    }

    # Get Harness.__init__ signature params
    sig = inspect.signature(Harness.__init__)
    sdk_params = {name for name, p in sig.parameters.items() if name != "self"}

    # Verify every CLI param has an SDK mapping
    for cli_name in cli_param_names:
        sdk_name = cli_to_sdk.get(cli_name)
        assert sdk_name is not None, (
            f"CLI option --{cli_name.replace('_', '-')} has no SDK mapping. "
            f"Add it to Harness.__init__."
        )
        assert sdk_name in sdk_params, (
            f"CLI option --{cli_name.replace('_', '-')} maps to '{sdk_name}' "
            f"but Harness.__init__ does not have that parameter."
        )

    # Verify env vars have SDK coverage
    env_vars_to_sdk = {
        "HARNESS_MODEL": "model",
        "HARNESS_API_BASE": "api_base",
        "HARNESS_PROVIDER": "provider",
        "HARNESS_CODEX_AUTH_PATH": "codex_auth_path",
        "OPENROUTER_API_KEY": "api_key",
        "OPENAI_API_KEY": "api_key",
    }
    for env_var, sdk_name in env_vars_to_sdk.items():
        assert sdk_name in sdk_params, (
            f"Environment variable {env_var} maps to '{sdk_name}' "
            f"but Harness.__init__ does not have that parameter."
        )


# ---------------------------------------------------------------------------
# _extract_final_text edge cases
# ---------------------------------------------------------------------------


def test_extract_final_text_returns_last_assistant():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "first"},
        {"role": "user", "content": "again"},
        {"role": "assistant", "content": "second"},
    ]
    assert _extract_final_text(messages) == "second"


def test_extract_final_text_empty_when_no_assistant():
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    assert _extract_final_text(messages) == ""


def test_extract_final_text_finds_assistant_before_tool():
    messages = [
        {"role": "assistant", "content": "answer"},
        {"role": "tool", "content": "result"},
    ]
    assert _extract_final_text(messages) == "answer"


def test_extract_final_text_none_content():
    messages = [{"role": "assistant", "content": None}]
    assert _extract_final_text(messages) == ""


def test_extract_final_text_empty_list():
    assert _extract_final_text([]) == ""


# ---------------------------------------------------------------------------
# _normalize_agents
# ---------------------------------------------------------------------------


def test_normalize_agents_none():
    assert _normalize_agents(None) is None


def test_normalize_agents_string():
    assert _normalize_agents("codex,gemini") == "codex,gemini"


def test_normalize_agents_list():
    assert _normalize_agents(["codex", "gemini"]) == "codex,gemini"


# ---------------------------------------------------------------------------
# Console mode selection
# ---------------------------------------------------------------------------


def test_make_console_silent_mode():
    console = make_console(mode="silent")
    assert isinstance(console, SilentConsole)


def test_make_console_plain_mode(tmp_path):
    ctx = ContextTracker(model_id="m", model_limit=100)
    manager = AgentManager()
    console = make_console(ctx=ctx, manager=manager, run_dir=tmp_path, mode="plain")
    assert isinstance(console, PlainConsole)


def test_make_console_defaults_to_silent_when_args_missing():
    console = make_console(mode="auto")
    assert isinstance(console, SilentConsole)


def test_make_console_silent_no_ops():
    console = SilentConsole()
    console.start()
    console.stop()
    console.begin_turn(0)
    console.begin_streaming()
    console.stream_token("x")
    console.end_streaming()
    console.end_turn()
    tc = console.tool_call_start(name="test", args={})
    tc.result("ok", is_error=False)
    console.agent_event(event="spawned", state=None)
    console.context_warning()
    console.reset_separator()
    console.print("hello")
    console.print_agent_panel_inline()


# ---------------------------------------------------------------------------
# Harness.run() returns HarnessResult with correct shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_harness_run_returns_result(monkeypatch, tmp_path):
    class FakeClient:
        api_base = "http://localhost:11434/v1"
        model = "test/model"
        provider = "openai_compat"

        async def chat(self, messages, tools=None, stream=False, token_callback=None):
            from team_harness.coordinator.client import ChatResponse
            from team_harness.coordinator.client import ChoiceRecord
            from team_harness.coordinator.client import MessageRecord
            from team_harness.coordinator.client import UsageRecord

            return ChatResponse(
                choices=[ChoiceRecord(message=MessageRecord(content="final answer"))],
                usage=UsageRecord(prompt_tokens=1, completion_tokens=1),
            )

        async def get_models(self):
            return {"data": []}

        async def aclose(self):
            return None

    monkeypatch.setattr(
        "team_harness.harness._make_client", lambda config: FakeClient()
    )
    monkeypatch.setattr("team_harness.harness.RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config_module, "CONFIG_PATH", tmp_path / "home" / "config.toml")
    monkeypatch.setattr(
        "team_harness.harness.validate_templates", lambda *args, **kwargs: None
    )

    harness = Harness(api_base="http://localhost:11434/v1", cwd=str(tmp_path))
    result = await harness.run("hello")

    assert isinstance(result, HarnessResult)
    assert result.text == "final answer"
    assert isinstance(result.agents, list)
    assert isinstance(result.run_id, str)
    assert len(result.run_id) > 0


@pytest.mark.asyncio
async def test_harness_run_creates_session_output_dir_and_passes_it_to_prompt(
    monkeypatch, tmp_path
):
    class FakeClient:
        api_base = "http://localhost:11434/v1"
        model = "test/model"
        provider = "openai_compat"

        async def get_models(self):
            return {"data": []}

        async def aclose(self):
            return None

    async def fake_resolve_model_limit(model_id, client, config):
        return 128_000

    async def fake_run(messages, config, run_log, ui, tool_registry, client, ctx):
        messages.append({"role": "assistant", "content": "final answer"})

    captured: dict[str, str] = {}
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    output_root = project_dir / "artifacts"

    def fake_build_system_prompt(*, config, allowed_types, skills, session_output_dir):
        captured["session_output_dir"] = session_output_dir
        return "system"

    monkeypatch.setattr(
        "team_harness.harness._make_client", lambda config: FakeClient()
    )
    monkeypatch.setattr("team_harness.harness.RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config_module, "CONFIG_PATH", tmp_path / "home" / "config.toml")
    monkeypatch.setattr(
        "team_harness.harness.validate_templates", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        "team_harness.harness.resolve_model_limit", fake_resolve_model_limit
    )
    monkeypatch.setattr("team_harness.harness.load_skills", lambda cwd=None: [])
    monkeypatch.setattr(
        "team_harness.harness.build_system_prompt", fake_build_system_prompt
    )
    monkeypatch.setattr("team_harness.harness.run", fake_run)

    local_config = project_dir / ".team-harness" / "config.toml"
    local_config.parent.mkdir()
    local_config.write_text('[coordinator]\noutput_dir = "artifacts"\n')

    harness = Harness(api_base="http://localhost:11434/v1", cwd=str(project_dir))
    result = await harness.run("hello")

    session_output_dir = config_module.Path(captured["session_output_dir"])
    assert result.text == "final answer"
    assert session_output_dir.parent == output_root
    assert session_output_dir.is_dir()
    manifest = session_output_dir / "worker_sessions.json"
    assert manifest.exists()
    assert json.loads(manifest.read_text())["workers"] == []


# ---------------------------------------------------------------------------
# AgentSummary has no proc field
# ---------------------------------------------------------------------------


def test_agent_summary_has_no_proc_field():
    summary = AgentSummary(
        id="agent_1", agent_type="codex", status="done", exit_code=0, cwd="/tmp"
    )
    assert not hasattr(summary, "proc")
    assert summary.id == "agent_1"
    assert summary.agent_type == "codex"
    assert summary.status == "done"
    assert summary.exit_code == 0
    assert summary.cwd == "/tmp"


# ---------------------------------------------------------------------------
# Error path: HarnessError raised on loop failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_harness_error_on_loop_failure(monkeypatch, tmp_path):
    class FakeClient:
        api_base = "http://localhost:11434/v1"
        model = "test/model"
        provider = "openai_compat"

        async def chat(self, messages, tools=None, stream=False, token_callback=None):
            raise RuntimeError("boom")

        async def get_models(self):
            return {"data": []}

        async def aclose(self):
            return None

    monkeypatch.setattr(
        "team_harness.harness._make_client", lambda config: FakeClient()
    )
    monkeypatch.setattr("team_harness.harness.RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config_module, "CONFIG_PATH", tmp_path / "home" / "config.toml")
    monkeypatch.setattr(
        "team_harness.harness.validate_templates", lambda *args, **kwargs: None
    )

    harness = Harness(api_base="http://localhost:11434/v1", cwd=str(tmp_path))
    with pytest.raises(HarnessError, match="boom"):
        await harness.run("hello")

    # Verify run log was finalized despite the error
    runs_dir = tmp_path / "runs"
    run_dirs = list(runs_dir.iterdir())
    assert len(run_dirs) == 1
    run_json = run_dirs[0] / "run.json"
    assert run_json.exists()
    data = json.loads(run_json.read_text())
    assert data["end"] is not None
    manifest = tmp_path / "_outputs" / run_dirs[0].name / "worker_sessions.json"
    assert manifest.exists()
    assert json.loads(manifest.read_text())["workers"] == []


# ---------------------------------------------------------------------------
# _show_no_config_hint and _warn_provider_startup
# ---------------------------------------------------------------------------


def test_show_no_config_hint_with_ui():
    messages: list[str] = []

    class _CaptureUI:
        def print(self, msg: str) -> None:
            messages.append(msg)

    config = Config(global_config_path=None, local_config_path=None)
    _show_no_config_hint(config, ui=_CaptureUI())
    assert len(messages) == 1
    assert "No config file found" in messages[0]


def test_show_no_config_hint_silent_when_no_ui():
    config = Config(global_config_path=None, local_config_path=None)
    # Should not raise
    _show_no_config_hint(config, ui=None)


def test_show_no_config_hint_silent_when_config_exists(tmp_path):
    messages: list[str] = []

    class _CaptureUI:
        def print(self, msg: str) -> None:
            messages.append(msg)

    config = Config(global_config_path=tmp_path / "config.toml")
    _show_no_config_hint(config, ui=_CaptureUI())
    assert len(messages) == 0


def test_warn_provider_startup_no_warning_with_api_key():
    messages: list[str] = []

    class _CaptureUI:
        def print(self, msg: str) -> None:
            messages.append(msg)

    config = Config(provider="openai_compat", api_key="sk-test")
    _warn_provider_startup(config, ui=_CaptureUI())
    assert len(messages) == 0


def test_warn_provider_startup_no_warning_for_localhost():
    messages: list[str] = []

    class _CaptureUI:
        def print(self, msg: str) -> None:
            messages.append(msg)

    config = Config(
        provider="openai_compat", api_key="", api_base="http://localhost:11434/v1"
    )
    _warn_provider_startup(config, ui=_CaptureUI())
    assert len(messages) == 0


def test_warn_provider_startup_warns_missing_key():
    messages: list[str] = []

    class _CaptureUI:
        def print(self, msg: str) -> None:
            messages.append(msg)

    config = Config(
        provider="openai_compat", api_key="", api_base="https://api.example.com"
    )
    _warn_provider_startup(config, ui=_CaptureUI())
    assert any("No API key" in m for m in messages)


# ---------------------------------------------------------------------------
# build_agent_tool_bindings produces working closures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_agent_tool_bindings_produces_closures(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    config = Config(
        provider="openai_compat",
        model="test/model",
        api_base="http://localhost:9999",
        api_key="test-key",
        cwd=str(tmp_path),
        run_dir=run_dir,
        agent_templates={"codex": "echo {prompt}"},
    )
    manager = AgentManager()
    run_log = RunLogWriter(
        run_id="run_1",
        run_dir=run_dir,
        provider=config.provider,
        model=config.model,
        api_base=config.api_base,
    )
    ui = SilentConsole()
    allowed_types = ["codex"]

    bindings = build_agent_tool_bindings(
        manager=manager,
        run_log=run_log,
        config=config,
        ui=ui,
        allowed_types=allowed_types,
    )

    assert len(bindings) == 8
    schema_names = {b[0]["function"]["name"] for b in bindings}
    assert "spawn_agent" in schema_names
    assert "agent_status" in schema_names
    assert "read_agent_output" in schema_names
    assert "read_new_agent_output" in schema_names
    assert "list_agents" in schema_names
    assert "wait_for_agents" in schema_names
    assert "wait_for_any" in schema_names
    assert "kill_agent" in schema_names

    # Test list_agents closure works
    list_fn = next(
        fn for schema, fn in bindings if schema["function"]["name"] == "list_agents"
    )
    result = await list_fn()
    assert json.loads(result) == []


@pytest.mark.asyncio
async def test_build_agent_tool_bindings_spawn_records_resume_metadata(
    monkeypatch, tmp_path
):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    config = Config(
        provider="openai_compat",
        model="test/model",
        api_base="http://localhost:9999",
        api_key="test-key",
        cwd=str(tmp_path),
        run_dir=run_dir,
        agent_templates={"codex": "echo {prompt}"},
    )
    manager = AgentManager()
    run_log = RunLogWriter(
        run_id="run_1",
        run_dir=run_dir,
        provider=config.provider,
        model=config.model,
        api_base=config.api_base,
    )
    ui = SilentConsole()

    async def fake_spawn(**kwargs):
        return await asyncio.create_subprocess_exec("sh", "-lc", "exit 0")

    monkeypatch.setattr("team_harness.tools.agent_tools.spawner.spawn", fake_spawn)
    monkeypatch.setattr(
        "team_harness.tools.agent_tools.spawner.build_command",
        lambda **kwargs: ["codex", "exec"],
    )

    bindings = build_agent_tool_bindings(
        manager=manager,
        run_log=run_log,
        config=config,
        ui=ui,
        allowed_types=["codex"],
        session_output_dir=str(tmp_path / "outputs" / "run_1"),
    )
    spawn_fn = next(
        fn for schema, fn in bindings if schema["function"]["name"] == "spawn_agent"
    )

    await spawn_fn(type="codex", prompt="hello", cwd=str(tmp_path))
    await asyncio.sleep(0.1)

    data = json.loads((run_dir / "run.json").read_text())
    assert data["agents"][0]["resume"] == {
        "supported": True,
        "preferred_mode": "resume",
        "provider_session_id": None,
        "provider_session_path": None,
    }


# ---------------------------------------------------------------------------
# build_agent_tool_bindings per-run isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_tool_bindings_isolate_cursor_state(tmp_path):
    """Two independent binding sets must not share output_cursors."""
    run_dir_a = tmp_path / "run_a"
    run_dir_a.mkdir()
    run_dir_b = tmp_path / "run_b"
    run_dir_b.mkdir()

    config = Config(
        provider="openai_compat",
        model="test/model",
        api_base="http://localhost:9999",
        api_key="test-key",
        cwd=str(tmp_path),
        run_dir=run_dir_a,
        agent_templates={"codex": "echo {prompt}"},
    )
    manager_a = AgentManager()
    log_a = RunLogWriter(
        run_id="a",
        run_dir=run_dir_a,
        provider=config.provider,
        model=config.model,
        api_base=config.api_base,
    )
    ui = SilentConsole()
    bindings_a = build_agent_tool_bindings(
        manager=manager_a, run_log=log_a, config=config, ui=ui, allowed_types=["codex"]
    )

    config_b = Config(
        provider="openai_compat",
        model="test/model",
        api_base="http://localhost:9999",
        api_key="test-key",
        cwd=str(tmp_path),
        run_dir=run_dir_b,
        agent_templates={"codex": "echo {prompt}"},
    )
    manager_b = AgentManager()
    log_b = RunLogWriter(
        run_id="b",
        run_dir=run_dir_b,
        provider=config_b.provider,
        model=config_b.model,
        api_base=config_b.api_base,
    )
    bindings_b = build_agent_tool_bindings(
        manager=manager_b,
        run_log=log_b,
        config=config_b,
        ui=ui,
        allowed_types=["codex"],
    )

    # Both produce independent binding lists
    assert len(bindings_a) == 8
    assert len(bindings_b) == 8

    # list_agents calls use different managers
    list_a = next(fn for s, fn in bindings_a if s["function"]["name"] == "list_agents")
    list_b = next(fn for s, fn in bindings_b if s["function"]["name"] == "list_agents")
    assert json.loads(await list_a()) == []
    assert json.loads(await list_b()) == []


# ---------------------------------------------------------------------------
# build_fs_tool_bindings isolates cursor state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fs_tool_bindings_isolate_cursor_state(tmp_path):
    bindings_a = build_fs_tool_bindings()
    bindings_b = build_fs_tool_bindings()

    assert len(bindings_a) == 9
    assert len(bindings_b) == 9

    # Write a file to read incrementally
    test_file = tmp_path / "progress.md"
    test_file.write_text("line1\n")

    read_new_a = next(
        fn for s, fn in bindings_a if s["function"]["name"] == "read_new_file_content"
    )
    read_new_b = next(
        fn for s, fn in bindings_b if s["function"]["name"] == "read_new_file_content"
    )

    # First read from binding A should get content
    result_a = await read_new_a(path=str(test_file))
    assert result_a == "line1\n"

    # Binding B should also see all content (independent cursor)
    result_b = await read_new_b(path=str(test_file))
    assert result_b == "line1\n"

    # Second read from A should get nothing (cursor advanced)
    result_a2 = await read_new_a(path=str(test_file))
    assert result_a2 == ""

    # Append more content
    with test_file.open("a") as f:
        f.write("line2\n")

    # Both should see the new content independently
    result_a3 = await read_new_a(path=str(test_file))
    assert result_a3 == "line2\n"

    result_b2 = await read_new_b(path=str(test_file))
    assert result_b2 == "line2\n"


# ---------------------------------------------------------------------------
# build_todo_tool_bindings isolates todo path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_todo_tool_bindings_isolate_path(tmp_path):
    dir_a = tmp_path / "run_a"
    dir_a.mkdir()
    dir_b = tmp_path / "run_b"
    dir_b.mkdir()

    bindings_a = build_todo_tool_bindings(run_dir=dir_a)
    bindings_b = build_todo_tool_bindings(run_dir=dir_b)

    assert len(bindings_a) == 2
    assert len(bindings_b) == 2

    write_a = next(fn for s, fn in bindings_a if s["function"]["name"] == "todo_write")
    read_a = next(fn for s, fn in bindings_a if s["function"]["name"] == "todo_read")
    read_b = next(fn for s, fn in bindings_b if s["function"]["name"] == "todo_read")

    # Write via A
    result = await write_a(tasks=[{"id": "1", "description": "test", "status": "done"}])
    assert "1 tasks" in result

    # Read from A should have the task
    data_a = json.loads(await read_a())
    assert len(data_a) == 1
    assert data_a[0]["description"] == "test"

    # Read from B should be empty (different path)
    data_b = json.loads(await read_b())
    assert data_b == []

    # Verify files are in expected locations
    assert (dir_a / "todo.json").exists()
    assert not (dir_b / "todo.json").exists()


# ---------------------------------------------------------------------------
# Public API exports
# ---------------------------------------------------------------------------


def test_public_api_exports():
    from team_harness import AgentSummary as AS
    from team_harness import Harness as H
    from team_harness import HarnessError as HE
    from team_harness import HarnessResult as HR

    assert H is Harness
    assert HE is HarnessError
    assert HR is HarnessResult
    assert AS is AgentSummary


# ---------------------------------------------------------------------------
# HarnessResult dataclass shape
# ---------------------------------------------------------------------------


def test_harness_result_shape():
    result = HarnessResult(
        text="answer",
        agents=[
            AgentSummary(
                id="agent_1", agent_type="codex", status="done", exit_code=0, cwd="/tmp"
            )
        ],
        run_id="20260408_120000_abcd1234",
    )
    assert result.text == "answer"
    assert len(result.agents) == 1
    assert result.agents[0].id == "agent_1"
    assert result.run_id == "20260408_120000_abcd1234"


# ---------------------------------------------------------------------------
# Harness constructor stores params correctly
# ---------------------------------------------------------------------------


def test_harness_constructor():
    h = Harness(
        provider="codex",
        model="codex-mini-latest",
        api_base="https://example.com",
        api_key="sk-test",
        codex_auth_path="/tmp/auth.json",
        agents=["codex", "gemini"],
        max_turns=10,
        max_retries=3,
        max_depth=2,
        system_prompt="extra",
        system_prompt_file="/tmp/prompt.txt",
        cwd="/tmp/project",
        console_mode="plain",
    )
    assert h._provider == "codex"
    assert h._model == "codex-mini-latest"
    assert h._api_base == "https://example.com"
    assert h._api_key == "sk-test"
    assert h._codex_auth_path == "/tmp/auth.json"
    assert h._agents == ["codex", "gemini"]
    assert h._max_turns == 10
    assert h._max_retries == 3
    assert h._max_depth == 2
    assert h._system_prompt == "extra"
    assert h._system_prompt_file == "/tmp/prompt.txt"
    assert h._cwd == "/tmp/project"
    assert h._console_mode == "plain"


# ---------------------------------------------------------------------------
# HarnessError is a proper exception
# ---------------------------------------------------------------------------


def test_harness_error_is_exception():
    err = HarnessError("something went wrong")
    assert isinstance(err, Exception)
    assert str(err) == "something went wrong"
