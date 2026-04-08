# ruff: noqa: A002

import asyncio
import sys

from prompt_toolkit import PromptSession
from prompt_toolkit.history import History
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.output import Output


def _build_key_bindings() -> KeyBindings:
    kb = KeyBindings()

    @kb.add("escape", "enter")
    def _newline(event: KeyPressEvent) -> None:
        event.current_buffer.insert_text("\n")

    @kb.add("escape", "escape")
    def _clear(event: KeyPressEvent) -> None:
        event.current_buffer.reset()

    return kb


def make_prompt_session(
    *,
    history: History | None = None,
    input: Input | None = None,
    output: Output | None = None,
) -> "PromptSession[str] | None":
    if input is not None or output is not None:
        return PromptSession(
            history=history or InMemoryHistory(),
            key_bindings=_build_key_bindings(),
            input=input,
            output=output,
        )
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None
    return PromptSession(
        history=history or InMemoryHistory(), key_bindings=_build_key_bindings()
    )


async def read_user_input(
    session: "PromptSession[str] | None", *, prompt: str = "> "
) -> str | None:
    if session is None:
        try:
            text = await asyncio.to_thread(input, prompt)
        except EOFError:
            return None
        return text.strip()

    try:
        text = await session.prompt_async(prompt)
        return text.strip()
    except (KeyboardInterrupt, EOFError):
        return None
