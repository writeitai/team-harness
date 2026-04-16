# ruff: noqa: A002

import asyncio
import sys

from prompt_toolkit import PromptSession
from prompt_toolkit.application.current import get_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.history import History
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.keys import Keys
from prompt_toolkit.output import Output

from team_harness.ui.paste import PasteBuffer


def _build_key_bindings(*, paste_buffer: PasteBuffer | None = None) -> KeyBindings:
    kb = KeyBindings()

    @kb.add("escape", "enter")
    def _newline(event: KeyPressEvent) -> None:
        event.current_buffer.insert_text("\n")

    @kb.add("escape", "escape")
    def _clear(event: KeyPressEvent) -> None:
        event.current_buffer.reset()

    if paste_buffer is not None:

        @kb.add(Keys.BracketedPaste)
        def _paste(event: KeyPressEvent) -> None:
            text = paste_buffer.store_and_placeholder(event.data)
            if text:
                event.current_buffer.insert_text(text)

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

    paste_buffer = PasteBuffer()
    original_key_bindings = session.key_bindings
    session.key_bindings = _build_key_bindings(paste_buffer=paste_buffer)
    original_accept_handler = session.default_buffer.accept_handler

    def _accept(buff: Buffer) -> bool:
        buff.text = paste_buffer.expand(buff.text)
        if original_accept_handler is not None:
            return bool(original_accept_handler(buff))
        get_app().exit(result=buff.document.text)
        return False

    session.default_buffer.accept_handler = _accept
    try:
        text = await session.prompt_async(prompt)
        return paste_buffer.expand(text).strip()
    except (KeyboardInterrupt, EOFError):
        return None
    finally:
        session.default_buffer.accept_handler = original_accept_handler
        session.key_bindings = original_key_bindings
