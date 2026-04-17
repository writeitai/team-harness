from abc import ABC
from abc import abstractmethod
from datetime import datetime
from datetime import timezone
from importlib.metadata import version as _pkg_version
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import TYPE_CHECKING

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from team_harness.ui.terminal import clear_terminal_progress
from team_harness.ui.terminal import register_progress_cleanup
from team_harness.ui.terminal import set_terminal_progress

try:
    _VERSION = _pkg_version("team-harness")
except Exception:
    _VERSION = "dev"

if TYPE_CHECKING:
    from team_harness.agents.manager import AgentManager
    from team_harness.agents.manager import AgentState
    from team_harness.tracking.context import ContextTracker

AGENT_COLORS: dict[str, str] = {
    "codex": "blue",
    "gemini": "green",
    "claude": "magenta",
    "openhands": "bright_cyan",
    "opencode": "cyan",
    "harness": "yellow",
    "pi": "bright_red",
}

AGENT_EMOJIS: dict[str, str] = {
    "codex": "\U0001f537",
    "gemini": "\u264a",
    "claude": "\U0001f7e3",
    "openhands": "\U0001f590",
    "opencode": "\U0001f4bb",
    "harness": "\U0001f528",
    "pi": "\U0001f534",
}

_PATH_RE = re.compile(
    r"(?<![/\w])(?:~?/(?!/)|\.\.?/)[^\s,;:)\]\"']+(?<![.])"
    r"|(?:(?:^|(?<=\s))[\w][\w.-]*/(?:[\w][\w.-]*/)*\w+\.\w+)"
)


def _style_paths(text: str) -> Text:
    """Return a Text object with file paths highlighted in cyan."""
    styled = Text(text)
    styled.highlight_regex(_PATH_RE, style="cyan")
    return styled


class ConsoleBase(ABC):
    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def print_welcome(self, model: str, cwd: str, provider: str) -> None: ...

    @abstractmethod
    def pause_for_input(self) -> None: ...

    @abstractmethod
    def resume_after_input(self) -> None: ...

    @abstractmethod
    def begin_turn(self, n: int) -> None: ...

    def begin_compaction(self) -> None:
        return None

    @abstractmethod
    def begin_streaming(self) -> None: ...

    @abstractmethod
    def stream_token(self, token: str) -> None: ...

    @abstractmethod
    def end_streaming(self) -> None: ...

    def end_compaction(
        self, before_tokens: int, after_tokens: int, success: bool = True
    ) -> None:
        return None

    def print_user_prompt(self, text: str) -> None:
        return None

    @abstractmethod
    def end_turn(self) -> None: ...

    @abstractmethod
    def tool_call_start(self, name: str, args: dict) -> "ToolCallContext": ...

    @abstractmethod
    def agent_event(self, event: str, state: "AgentState") -> None: ...

    @abstractmethod
    def context_warning(self) -> None: ...

    @abstractmethod
    def reset_separator(self) -> None: ...

    @abstractmethod
    def print(self, msg: str) -> None: ...

    @abstractmethod
    def print_agent_panel_inline(self) -> None: ...


class ToolCallContext:
    def __init__(self, callback: callable) -> None:
        self._callback = callback

    def result(self, text: str, is_error: bool = False) -> None:
        self._callback(text, is_error)


class SilentConsole(ConsoleBase):
    """Console that discards all output. Used by the Python SDK."""

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def print_welcome(self, model: str, cwd: str, provider: str) -> None:
        return None

    def pause_for_input(self) -> None:
        return None

    def resume_after_input(self) -> None:
        return None

    def begin_turn(self, n: int) -> None:
        return None

    def begin_streaming(self) -> None:
        return None

    def stream_token(self, token: str) -> None:
        return None

    def end_streaming(self) -> None:
        return None

    def end_turn(self) -> None:
        return None

    def tool_call_start(self, name: str, args: dict) -> ToolCallContext:
        return ToolCallContext(lambda text, is_error: None)

    def agent_event(self, event: str, state: "AgentState") -> None:
        return None

    def context_warning(self) -> None:
        return None

    def reset_separator(self) -> None:
        return None

    def print(self, msg: str) -> None:
        return None

    def print_agent_panel_inline(self) -> None:
        return None


class PlainConsole(ConsoleBase):
    def __init__(
        self, ctx: "ContextTracker", manager: "AgentManager", run_dir: Path
    ) -> None:
        self._ctx = ctx
        self._manager = manager
        self._run_dir = run_dir
        self._start = time.monotonic()

    def _prefix(self) -> str:
        elapsed = int(time.monotonic() - self._start)
        return f"[{elapsed // 60:02d}:{elapsed % 60:02d}]"

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def print_welcome(self, model: str, cwd: str, provider: str) -> None:
        print(f"th \u2014 team-harness (v{_VERSION})")
        print(f"model:     {model}")
        print(f"directory: {cwd}")

    def pause_for_input(self) -> None:
        pass

    def resume_after_input(self) -> None:
        pass

    def begin_turn(self, n: int) -> None:
        print(f"\n{self._prefix()} [Turn {n}]")
        print(f"{self._prefix()} \u280b Thinking...")

    def begin_compaction(self) -> None:
        print(f"{self._prefix()} Compacting conversation...")

    def begin_streaming(self) -> None:
        return None

    def stream_token(self, token: str) -> None:
        print(token, end="", flush=True)

    def end_streaming(self) -> None:
        return None

    def end_compaction(
        self, before_tokens: int, after_tokens: int, success: bool = True
    ) -> None:
        if success:
            print(
                f"{self._prefix()} Context compacted: ~{before_tokens:,} -> ~{after_tokens:,} tokens"
            )

    def end_turn(self) -> None:
        print()

    def tool_call_start(self, name: str, args: dict) -> ToolCallContext:
        print(f"\n{self._prefix()}   \u25b6 {name}({json.dumps(args, sort_keys=True)})")

        def _render(text: str, is_error: bool) -> None:
            for line in text.splitlines() or [""]:
                prefix = "ERROR" if is_error else "RESULT"
                print(f"{self._prefix()}   {prefix}: {line}")

        return ToolCallContext(_render)

    def print_user_prompt(self, text: str) -> None:
        print(f"{self._prefix()} > {text}")

    def agent_event(self, event: str, state: "AgentState") -> None:
        emoji = AGENT_EMOJIS.get(state.agent_type, "")
        emoji_prefix = f"{emoji} " if emoji else ""
        print(
            f"{self._prefix()}   {emoji_prefix}{state.agent_type} {state.id[:6]}  {event}"
        )

    def context_warning(self) -> None:
        print(
            f"{self._prefix()} \u26a0 Context at {self._ctx.pct:.0f}% \u2014 consider /clear"
        )

    def reset_separator(self) -> None:
        print(f"{self._prefix()} --- Context reset ---")

    def print(self, msg: str) -> None:
        print(f"{self._prefix()} {msg}")

    def print_agent_panel_inline(self) -> None:
        self._manager.poll_exit_codes()
        for state in self._manager.list_all():
            print(
                f"{self._prefix()} {state.id} {state.agent_type} {state.status} {state.cwd} {_elapsed(state)}"
            )


class HarnessConsole(ConsoleBase):
    def __init__(
        self, ctx: "ContextTracker", manager: "AgentManager", run_dir: Path
    ) -> None:
        self._ctx = ctx
        self._manager = manager
        self._run_dir = run_dir
        self._turn = 0
        self._start = time.monotonic()
        self._compacting = False
        self._phase = "idle"
        self._console = Console(highlight=False)
        self._live = Live(
            self._render_live(),
            console=self._console,
            refresh_per_second=2,
            transient=False,
        )
        # _live_enabled: intent -- should Live be used at all?
        # _live_running: actual -- is the Live display currently active?
        self._live_enabled = False
        self._live_running = False
        self._streaming = False

    def start(self) -> None:
        self._live_enabled = True
        register_progress_cleanup()
        # Don't start Live yet -- it will start after the first input is
        # submitted so the prompt appears inline below the welcome box.

    def stop(self) -> None:
        clear_terminal_progress()
        self._phase = "idle"
        if self._live_running:
            self._live.stop()
            self._live_running = False
        self._live_enabled = False

    def print_welcome(self, model: str, cwd: str, provider: str) -> None:
        display_cwd = cwd.replace(str(Path.home()), "~")
        body = Text.assemble(
            ("  >_ ", "bold"),
            ("team-harness", "bold"),
            (f" (v{_VERSION})\n\n", "dim"),
            ("  model:     ", "dim"),
            (model, ""),
            ("    ", ""),
            ("/model", "dim cyan"),
            (" to change\n", "dim"),
            ("  provider:  ", "dim"),
            (provider, ""),
            ("\n", ""),
            ("  directory: ", "dim"),
            (display_cwd, ""),
        )
        self._console.print()
        self._console.print(Panel(body, border_style="dim", padding=(1, 1)))
        self._console.print()

    def pause_for_input(self) -> None:
        self._phase = "idle"
        clear_terminal_progress()
        if self._live_running:
            self._live.stop()
            self._live_running = False
        # Print a static status line so the user sees context/agent info
        # without Live reserving terminal space.
        self._console.print(self._render_status_bar())
        self._console.print()

    def resume_after_input(self) -> None:
        # Don't start Live here -- it causes the screen to jump.
        # Live starts in begin_turn() when actual processing begins.
        pass

    def begin_turn(self, n: int) -> None:
        self._phase = "thinking"
        set_terminal_progress()
        self._turn = n
        if self._live_enabled and not self._live_running:
            self._live.start()
            self._live.update(self._render_live())
            self._live_running = True
        self._console.rule(f"[dim]Turn [bold]{n}[/bold][/dim]", style="dim")

    def begin_streaming(self) -> None:
        self._phase = "streaming"
        if self._live_running:
            self._live.stop()
            self._live_running = False
        self._streaming = True

    def begin_compaction(self) -> None:
        self._compacting = True
        if self._live_running:
            self._live.update(self._render_live())

    def stream_token(self, token: str) -> None:
        self._console.print(
            token, end="", markup=False, highlight=False, soft_wrap=True
        )

    def end_streaming(self) -> None:
        if not self._streaming:
            return
        self._console.print()
        if self._live_enabled and not self._live_running:
            self._live.start()
            self._live.update(self._render_live())
            self._live_running = True
        self._streaming = False
        self._phase = "tools"

    def end_compaction(
        self, before_tokens: int, after_tokens: int, success: bool = True
    ) -> None:
        self._compacting = False
        if self._live_running:
            self._live.update(self._render_live())
        if success:
            self._console.print(
                f"Context compacted: ~{before_tokens:,} -> ~{after_tokens:,} tokens"
            )

    def end_turn(self) -> None:
        self._phase = "idle"
        clear_terminal_progress()
        if self._live_running:
            self._live.update(self._render_live())

    def tool_call_start(self, name: str, args: dict) -> ToolCallContext:
        args_text = _style_paths(_fmt_args(args))
        header = Text.assemble(
            ("\n  ", ""),
            ("\u25b6", "bold"),
            (" ", ""),
            (f"{name}", "bold cyan"),
            ("(", ""),
        )
        header.append_text(args_text)
        header.append(")")
        self._console.print(header)

        def _render(text: str, is_error: bool) -> None:
            style = "red" if is_error else "dim"
            lines = text.splitlines()
            for line in lines[:5]:
                styled_line = _style_paths(line)
                styled_line.stylize(style)
                prefix = Text("    \u2502 ", style="dim")
                self._console.print(Text.assemble(prefix, styled_line))
            if len(lines) > 5:
                self._console.print(
                    f"    [dim]\u2502 \u2026 ({len(lines) - 5} more lines)[/dim]"
                )
            if self._live_running:
                self._live.update(self._render_live())

        return ToolCallContext(_render)

    def print_user_prompt(self, text: str) -> None:
        body = Text(text, style="white on rgb(55,55,55)")
        self._console.print(Panel(body, border_style="dim", padding=(0, 1)))

    def agent_event(self, event: str, state: "AgentState") -> None:
        color = AGENT_COLORS.get(state.agent_type, "white")
        emoji = AGENT_EMOJIS.get(state.agent_type, "")
        emoji_prefix = f"{emoji} " if emoji else ""
        self._console.print(
            f"  [bold {color}]{emoji_prefix}{state.agent_type}[/bold {color}] {state.id[:6]}  {event}"
        )
        if self._live_running:
            self._live.update(self._render_live())

    def context_warning(self) -> None:
        self._console.print(
            f"\n  [bold red]\u26a0 Context at {self._ctx.pct:.0f}% \u2014 consider /clear[/bold red]\n"
        )

    def reset_separator(self) -> None:
        self._console.rule("[bold yellow]Context reset[/bold yellow]", style="yellow")

    def print(self, msg: str) -> None:
        self._console.print(msg)

    def print_agent_panel_inline(self) -> None:
        if self._live_running:
            self._live.stop()
            self._live_running = False
        self._console.print(self._render_agent_panel(self._manager.list_all()))
        if self._live_enabled and not self._live_running:
            self._live.start()
            self._live.update(self._render_live())
            self._live_running = True

    def _render_live(self) -> Layout:
        self._manager.poll_exit_codes()
        layout = Layout()
        blocks: list[Layout] = []
        agents = self._manager.list_all()
        todos = self._load_todos()
        if agents:
            agent_panel = Layout(self._render_agent_panel(agents), name="agents")
            agent_panel.size = min(len(agents) + 2, 10)
            blocks.append(agent_panel)
        if todos:
            todo_panel = Layout(self._render_todo_panel(todos), name="todos")
            todo_panel.size = min(len(todos) + 3, 14)
            blocks.append(todo_panel)
        blocks.append(Layout(self._render_status_bar(), name="status", size=1))
        layout.split_column(*blocks)
        return layout

    def _render_agent_panel(self, agents: list["AgentState"]) -> Panel:
        table = Table(box=None, padding=(0, 1), show_header=True, header_style="dim")
        table.add_column("ID", style="dim", width=8)
        table.add_column("TYPE", width=14)
        table.add_column("STATUS", width=14)
        table.add_column("ELAPSED", width=7)
        table.add_column("LAST OUTPUT", no_wrap=True)
        for state in agents:
            color = AGENT_COLORS.get(state.agent_type, "white")
            emoji = AGENT_EMOJIS.get(state.agent_type, "")
            emoji_prefix = f"{emoji} " if emoji else ""
            status, status_style = _format_status(state)
            table.add_row(
                state.id[:6],
                f"[bold {color}]{emoji_prefix}{state.agent_type}[/bold {color}]",
                f"[{status_style}]{status}[/{status_style}]",
                _elapsed(state),
                _last_line(state.stdout_log),
            )
        return Panel(
            table, title="[dim]agents[/dim]", border_style="dim", padding=(0, 1)
        )

    def _render_todo_panel(self, todos: list[dict]) -> Panel:
        lines = Text()
        for i, task in enumerate(todos):
            status = task.get("status", "pending")
            desc = task.get("description", "")
            blocked_by = task.get("blocked_by") or []

            if status == "in_progress":
                marker = "\u25a0"
                style = "bold yellow"
            elif status == "completed":
                marker = "\u2713"
                style = "green"
            elif status == "blocked":
                marker = "\u25a1"
                style = "dim red"
            else:  # pending
                marker = "\u25a1"
                style = "dim"

            if i > 0:
                lines.append("\n")
            # Tree connector: \u2514 for first item, then indented siblings
            if i == 0:
                lines.append("  \u2514 ", "dim")
            else:
                lines.append("    ", "dim")
            lines.append(f"{marker} ", style)
            lines.append(desc, style)
            if blocked_by:
                refs = ", ".join(f"#{b}" for b in blocked_by)
                lines.append(f" \u203a blocked by {refs}", "dim red")

        return Panel(lines, title="[dim]todo[/dim]", border_style="dim", padding=(0, 1))

    def _render_status_bar(self) -> Text:
        elapsed = time.monotonic() - self._start
        pct = self._ctx.pct
        ctx_color = "red" if pct >= 80 else "yellow" if pct >= 60 else "cyan"
        running = self._manager.running_count()
        total = len(self._manager.list_all())
        total_prefix = "~" if self._ctx.has_estimate else ""
        parts: list[tuple[str, str]] = []
        if self._phase == "thinking":
            frames = "\u280b\u2819\u2839\u2838\u283c\u2834\u2826\u2827\u2807\u280f"
            frame = frames[int(elapsed * 8) % len(frames)]
            parts.append((f"{frame} Thinking ", "cyan"))
            parts.append((" \u2502 ", "dim"))
        parts.extend(
            [
                (" ctx: ", "dim"),
                (
                    f"{total_prefix}{self._ctx.total:,}/{self._ctx.model_limit:,} ({pct:.0f}%)",
                    ctx_color,
                ),
                ("  \u2502  ", "dim"),
                (
                    f"agents: {running} running / {total} total",
                    "yellow" if running else "dim",
                ),
            ]
        )
        if self._compacting:
            parts.extend([("  \u2502  ", "dim"), ("compacting...", "yellow")])
        parts.extend(
            [
                ("  \u2502  ", "dim"),
                (f"turn: {self._turn}", "dim"),
                ("  \u2502  ", "dim"),
                (_fmt_elapsed(elapsed), "green"),
                ("  \u2502  ", "dim"),
                (f"model: {self._ctx.model_id}", "dim"),
            ]
        )
        return Text.assemble(*parts)

    def _load_todos(self) -> list[dict]:
        todo_path = self._run_dir / "todo.json"
        if not todo_path.exists():
            return []
        return json.loads(todo_path.read_text())


def _fmt_args(args: dict) -> str:
    return ", ".join(f"{key}={value!r}" for key, value in args.items())


def _fmt_elapsed(seconds: float) -> str:
    value = int(seconds)
    return f"{value // 60}:{value % 60:02d}"


def _elapsed(state: "AgentState") -> str:
    end = state.finished_at or datetime.now(timezone.utc)
    total_seconds = int((end - state.spawn_time).total_seconds())
    return f"{total_seconds // 60}:{total_seconds % 60:02d}"


def _format_status(state: "AgentState") -> tuple[str, str]:
    if state.exit_code is None:
        return ("\u25cf running", "bold yellow")
    if state.exit_code == 0:
        return ("\u2713 done(0)", "green")
    return (f"\u2717 failed({state.exit_code})", "red")


def _last_line(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, os.path.getsize(path) - 512))
            tail = handle.read().decode(errors="replace")
        lines = [line for line in tail.splitlines() if line.strip()]
        return lines[-1][:60] if lines else ""
    except OSError:
        return ""


def make_console(
    ctx: "ContextTracker | None" = None,
    manager: "AgentManager | None" = None,
    run_dir: "Path | None" = None,
    *,
    mode: str = "auto",
) -> ConsoleBase:
    """Create a console instance based on the requested mode.

    Modes:
        silent  -- all output discarded (SDK default)
        plain   -- line-oriented plain-text output
        rich    -- rich TUI with live panels
        auto    -- rich if stdout is a TTY, plain otherwise
    """
    if mode == "silent":
        return SilentConsole()
    if ctx is None or manager is None or run_dir is None:
        return SilentConsole()
    if mode == "plain":
        return PlainConsole(ctx=ctx, manager=manager, run_dir=run_dir)
    if mode == "rich":
        return HarnessConsole(ctx=ctx, manager=manager, run_dir=run_dir)
    # auto
    if sys.stdout.isatty():
        return HarnessConsole(ctx=ctx, manager=manager, run_dir=run_dir)
    return PlainConsole(ctx=ctx, manager=manager, run_dir=run_dir)
