import asyncio
import json
from pathlib import Path
from typing import Any

import click
from pydantic import ValidationError

from team_harness.agents.manager import AgentManager
from team_harness.agents.registry import get_allowed_types
from team_harness.agents.registry import validate_templates
from team_harness.config import _default_config_text
from team_harness.config import _local_config_text
from team_harness.config import CONFIG_PATH
from team_harness.config import load_config
from team_harness.config import LOCAL_CONFIG_DIR_NAME
from team_harness.config import RUNS_DIR
from team_harness.coordinator.loop import _perform_manual_compaction
from team_harness.coordinator.loop import run_one_turn
from team_harness.coordinator.system_prompt import build_system_prompt
from team_harness.coordinator.system_prompt import COORDINATOR_PROMPT
from team_harness.coordinator.system_prompt import DEFAULT_WORKER_FOOTER
from team_harness.harness import _build_registry
from team_harness.harness import _finalize_run
from team_harness.harness import _graceful_shutdown
from team_harness.harness import _make_client
from team_harness.harness import _make_run_id
from team_harness.harness import _prepare_session_output_dir
from team_harness.harness import _show_no_config_hint
from team_harness.harness import _warn_provider_startup
from team_harness.harness import TeamHarness
from team_harness.skills.loader import load_skill_metadata
from team_harness.tools import agent_tools
from team_harness.tools import fs_tools
from team_harness.tools import todo_tools
from team_harness.tracking.context import ContextTracker
from team_harness.tracking.context import resolve_model_limit
from team_harness.tracking.reaper import DEFAULT_DRAIN_TIMEOUT_S
from team_harness.tracking.reaper import DEFAULT_GRACE_S
from team_harness.tracking.reaper import reap_run
from team_harness.tracking.reaper import ReapRefusedError
from team_harness.tracking.reaper import resolve_run_json
from team_harness.tracking.run_log import RunLogWriter
from team_harness.ui.console import ConsoleBase
from team_harness.ui.console import make_console
from team_harness.ui.prompt import make_prompt_session
from team_harness.ui.prompt import read_user_input


@click.group()
def main() -> None:
    """th \u2014 multi-agent AI orchestration harness."""


def _write_config_file(path: Path, text: str, force: bool) -> None:
    if path.exists() and not force:
        raise click.ClickException(
            f"Config file already exists at {path}. Use --force to overwrite."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    click.echo(f"Created config at {path}")


def _write_sidecar_file(path: Path, text: str) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    click.echo(f"Created prompt file at {path}")


def _write_init_files(config_path: Path, config_text: str, force: bool) -> None:
    _write_config_file(path=config_path, text=config_text, force=force)
    _write_sidecar_file(
        config_path.parent / "coordinator_system_message.md", COORDINATOR_PROMPT
    )
    _write_sidecar_file(config_path.parent / "worker_suffix.md", "")
    _write_sidecar_file(config_path.parent / "worker_footer.md", DEFAULT_WORKER_FOOTER)


@main.command()
@click.option("--global", "use_global", is_flag=True, help="Create global config.")
@click.option("--force", is_flag=True, help="Overwrite an existing config file.")
def init(use_global: bool, force: bool) -> None:
    path = (
        CONFIG_PATH
        if use_global
        else Path.cwd() / LOCAL_CONFIG_DIR_NAME / "config.toml"
    )
    text = _default_config_text() if use_global else _local_config_text()
    _write_init_files(path, text, force)


@main.command(name="run")
@click.argument("task", required=False)
@click.option("--file", "-f", "task_file", type=click.Path())
@click.option("--provider", default=None)
@click.option("--model", default=None)
@click.option("--api-base", default=None)
@click.option("--api-key", default=None)
@click.option("--codex-auth-path", default=None)
@click.option("--agents", "allowed_agents", default=None)
@click.option("--max-retries", type=int, default=None)
@click.option("--max-depth", type=int, default=None)
@click.option("--system-prompt", default=None)
@click.option("--system-prompt-file", "cli_system_prompt_file", default=None)
@click.option("--cwd", default=".")
def run_cli(
    task: str | None,
    task_file: str | None,
    provider: str | None,
    model: str | None,
    api_base: str | None,
    api_key: str | None,
    codex_auth_path: str | None,
    allowed_agents: str | None,
    max_retries: int | None,
    max_depth: int | None,
    system_prompt: str | None,
    cli_system_prompt_file: str | None,
    cwd: str,
) -> None:
    asyncio.run(
        _run(
            task=task,
            task_file=task_file,
            provider=provider,
            model=model,
            api_base=api_base,
            api_key=api_key,
            codex_auth_path=codex_auth_path,
            allowed_agents=allowed_agents,
            max_retries=max_retries,
            max_depth=max_depth,
            system_prompt=system_prompt,
            system_prompt_file=cli_system_prompt_file,
            cwd=cwd,
        )
    )


@main.command()
@click.option("--provider", default=None)
@click.option("--model", default=None)
@click.option("--api-base", default=None)
@click.option("--api-key", default=None)
@click.option("--codex-auth-path", default=None)
@click.option("--agents", "allowed_agents", default=None)
@click.option("--max-retries", type=int, default=None)
@click.option("--max-depth", type=int, default=None)
@click.option("--system-prompt", default=None)
@click.option("--system-prompt-file", "cli_system_prompt_file", default=None)
@click.option("--cwd", default=".")
def repl(
    provider: str | None,
    model: str | None,
    api_base: str | None,
    api_key: str | None,
    codex_auth_path: str | None,
    allowed_agents: str | None,
    max_retries: int | None,
    max_depth: int | None,
    system_prompt: str | None,
    cli_system_prompt_file: str | None,
    cwd: str,
) -> None:
    asyncio.run(
        _repl(
            provider=provider,
            model=model,
            api_base=api_base,
            api_key=api_key,
            codex_auth_path=codex_auth_path,
            allowed_agents=allowed_agents,
            max_retries=max_retries,
            max_depth=max_depth,
            system_prompt=system_prompt,
            cli_system_prompt_file=cli_system_prompt_file,
            cwd=cwd,
        )
    )


@main.command()
@click.argument("run_id", required=False)
def logs(run_id: str | None) -> None:
    if run_id:
        run_json = RUNS_DIR / run_id / "run.json"
        if not run_json.exists():
            click.echo(f"ERROR: Run '{run_id}' not found at {run_json}", err=True)
            raise SystemExit(1)
        click.echo(json.dumps(json.loads(run_json.read_text()), indent=2))
        return
    if not RUNS_DIR.exists():
        click.echo("No runs yet.")
        return
    runs = sorted(RUNS_DIR.iterdir(), reverse=True)[:20]
    printed = False
    for run_dir in runs:
        run_json = run_dir / "run.json"
        if not run_json.exists():
            continue
        data = json.loads(run_json.read_text())
        click.echo(
            f"{run_dir.name}  start={str(data.get('start', '?'))[:19]}  "
            f"model={data.get('coordinator_model', '?')}  "
            f"turns={len(data.get('turns', []))}  agents={len(data.get('agents', []))}"
        )
        printed = True
    if not printed:
        click.echo("No runs yet.")


@main.command()
@click.argument("run_ref")
@click.option(
    "--policy",
    type=click.Choice(["drain", "reap", "ignore"]),
    default="drain",
    show_default=True,
    help="drain: wait for orphans to finish (timeout → kill); "
    "reap: kill now; ignore: only record what is still running.",
)
@click.option(
    "--drain-timeout-s",
    type=click.FloatRange(min=0),
    default=DEFAULT_DRAIN_TIMEOUT_S,
    show_default=True,
    help="Max seconds to wait — shared across ALL draining orphans — under "
    "--policy drain.",
)
@click.option(
    "--grace-s",
    type=click.FloatRange(min=0),
    default=DEFAULT_GRACE_S,
    show_default=True,
    help="Seconds between SIGTERM and SIGKILL when killing a group.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Act even if the run's original parent process still appears alive.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Probe and report only: no signals are sent, no files are written.",
)
@click.option("--json", "as_json", is_flag=True, help="Print the report as JSON.")
def reap(
    run_ref: str,
    policy: str,
    drain_timeout_s: float,
    grace_s: float,
    force: bool,
    dry_run: bool,
    as_json: bool,
) -> None:
    """Handle workers orphaned by a crashed run.

    RUN_REF is a run id (resolved under the runs dir), a run directory, or a
    direct path to its run.json. Every action verifies process identity
    (pgid + start time), so a recycled pid is never touched. Refuses to act on
    a run whose original parent is still alive unless --force is given.
    """
    candidate = Path(run_ref)
    if not candidate.exists():
        candidate = RUNS_DIR / run_ref
    run_json = resolve_run_json(candidate)
    if not run_json.exists():
        raise click.ClickException(f"run.json not found for '{run_ref}'")
    try:
        report = reap_run(
            run_json,
            policy=policy,  # type: ignore[arg-type]  # click.Choice guarantees the literal
            drain_timeout_s=drain_timeout_s,
            grace_s=grace_s,
            force=force,
            dry_run=dry_run,
        )
    except ReapRefusedError as exc:
        raise click.ClickException(str(exc)) from exc
    except (json.JSONDecodeError, ValidationError) as exc:
        raise click.ClickException(f"cannot parse {run_json}: {exc}") from exc
    except OSError as exc:
        raise click.ClickException(f"cannot read {run_json}: {exc}") from exc
    if as_json:
        click.echo(json.dumps(report.model_dump(mode="json"), indent=2))
        return
    prefix = "[dry-run] " if dry_run else ""
    if not report.workers:
        click.echo(f"{prefix}{report.run_id}: no workers were left marked running.")
        return
    for worker in report.workers:
        click.echo(
            f"{prefix}{worker.agent_id} ({worker.agent_type}) "
            f"pgid={worker.pgid} policy={worker.policy}: {worker.outcome}"
        )


def _prepare_task(task: str | None, task_file: str | None) -> str:
    if task and task_file:
        raise click.UsageError("Provide either TASK or --file, not both.")
    if not task and not task_file:
        raise click.UsageError("Provide TASK or --file.")
    if task_file:
        return Path(task_file).read_text()
    assert task is not None
    return task


async def _run(task: str | None, task_file: str | None, **kwargs: Any) -> None:
    resolved_task = _prepare_task(task=task, task_file=task_file)
    allowed_agents = kwargs.pop("allowed_agents", None)
    harness = TeamHarness(
        provider=kwargs.get("provider"),
        model=kwargs.get("model"),
        api_base=kwargs.get("api_base"),
        api_key=kwargs.get("api_key"),
        codex_auth_path=kwargs.get("codex_auth_path"),
        agents=allowed_agents,
        max_retries=kwargs.get("max_retries"),
        max_depth=kwargs.get("max_depth"),
        system_prompt=kwargs.get("system_prompt"),
        system_prompt_file=kwargs.get("system_prompt_file"),
        cwd=kwargs.get("cwd"),
        console_mode="auto",
    )
    await harness.run(resolved_task)


async def _repl(**kwargs: Any) -> None:
    config = load_config(**kwargs)
    _show_no_config_hint(config, ui=None)
    click.echo(
        "No config file found. Run `team-harness init` to create one."
    ) if config.global_config_path is None and config.local_config_path is None else None
    run_id = _make_run_id()
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    session_output_dir = _prepare_session_output_dir(config=config, session_id=run_id)
    config.run_dir = run_dir
    manager = AgentManager()
    client = _make_client(config)
    run_log: RunLogWriter | None = None
    ui: ConsoleBase | None = None
    try:
        run_log = RunLogWriter(
            run_id=run_id,
            run_dir=run_dir,
            provider=config.provider,
            model=config.model,
            api_base=client.api_base,
            session_output_dir=str(session_output_dir),
        )
        model_limit = await resolve_model_limit(
            model_id=config.model, client=client, config=config
        )
        ctx = ContextTracker(model_id=config.model, model_limit=model_limit)
        ui = make_console(ctx=ctx, manager=manager, run_dir=run_dir, mode="auto")
        _warn_provider_startup(config, ui=ui)
        skills = load_skill_metadata(cwd=config.cwd)
        allowed_types = get_allowed_types(config)
        validate_templates(config=config, allowed_types=allowed_types)
        agent_tools.setup(
            manager=manager,
            run_log=run_log,
            config=config,
            ui=ui,
            session_output_dir=str(session_output_dir),
        )
        todo_tools.setup(run_dir=run_dir)
        fs_tools.setup_fs()
        registry = _build_registry(
            allowed_types=allowed_types,
            manager=manager,
            run_log=run_log,
            config=config,
            ui=ui,
            run_dir=run_dir,
            session_output_dir=str(session_output_dir),
        )
        system_prompt = build_system_prompt(
            config=config,
            allowed_types=allowed_types,
            skills=skills,
            session_output_dir=str(session_output_dir),
        )
        messages = [{"role": "system", "content": system_prompt}]
        turn_index = 0
        last_logged_index = 0
        session = make_prompt_session()
        ui.print_welcome(model=config.model, cwd=config.cwd, provider=config.provider)
        try:
            while True:
                ui.pause_for_input()
                try:
                    raw = await read_user_input(session)
                finally:
                    ui.resume_after_input()
                if raw is None:
                    break
                if not raw:
                    continue
                match raw:
                    case "/clear" | "/reset":
                        messages.clear()
                        messages.append({"role": "system", "content": system_prompt})
                        ctx.reset()
                        last_logged_index = 0
                        ui.reset_separator()
                        ui.print("Context reset. Agent state and run log preserved.")
                    case "/quit":
                        await _graceful_shutdown(
                            manager=manager,
                            run_log=run_log,
                            ui=ui,
                            timeout=config.shutdown_timeout_s,
                        )
                        break
                    case "/agents":
                        ui.print_agent_panel_inline()
                    case "/log":
                        ui.print(str(run_log.path))
                    case _ if raw == "/compact" or raw.startswith("/compact "):
                        focus_text = raw[len("/compact") :].strip() or None
                        compacted = await _perform_manual_compaction(
                            messages=messages,
                            client=client,
                            ctx=ctx,
                            ui=ui,
                            focus_text=focus_text,
                        )
                        if compacted:
                            last_logged_index = 0
                    case _:
                        ui.print_user_prompt(raw)
                        messages.append({"role": "user", "content": raw})
                        ctx.set_estimated_total(messages)
                        should_continue = True
                        while should_continue:
                            should_continue, last_logged_index = await run_one_turn(
                                messages=messages,
                                config=config,
                                run_log=run_log,
                                ui=ui,
                                tool_registry=registry,
                                client=client,
                                ctx=ctx,
                                turn_index=turn_index,
                                last_logged_index=last_logged_index,
                            )
                            turn_index += 1
        finally:
            await _finalize_run(
                manager=manager,
                run_log=run_log,
                session_output_dir=session_output_dir,
                shutdown_timeout_s=config.shutdown_timeout_s,
                ui=ui,
            )
            ui.stop()
    finally:
        await client.aclose()
