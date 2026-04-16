# ruff: noqa: A002
"""prompt_toolkit-backed REPL input for ``th repl``.

Exposes two entry points:

* :func:`make_prompt_session` — build a ``PromptSession`` when running in a
  real TTY (or explicit input/output for tests); returns ``None`` otherwise so
  callers can fall back to the plain ``input()`` path.
* :func:`read_user_input` — read one line (or multi-line block) from the user,
  expanding collapsed pastes before returning.
"""

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
    """Assemble the REPL key bindings, optionally including paste-collapse.

    When ``paste_buffer`` is provided, a ``Keys.BracketedPaste`` handler
    intercepts pasted payloads and inserts either the raw text or a placeholder,
    delegating the decision to the buffer. Without it (e.g. at session
    construction time, before a prompt begins), only the standard editing
    shortcuts are installed.
    """
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
    """Construct a ``PromptSession`` if interactive, else ``None``.

    When ``input`` or ``output`` is provided (the test-injection path), a
    session is always returned. Otherwise a session is returned only when
    stdin and stdout are both TTYs; a non-TTY caller should fall through to
    the synchronous ``input()`` path in :func:`read_user_input`.
    """
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
    """Read one submission from the user, expanding any collapsed pastes.

    With ``session is None`` the function falls back to a threaded ``input()``
    call and returns the stripped line (or ``None`` on EOF) — bracketed paste
    detection is a TTY-only feature and is skipped here.

    With a real session, a per-prompt :class:`PasteBuffer` is installed via the
    key bindings and a wrapping ``accept_handler`` that expands the buffer's
    text in place before the default handler fires. This guarantees prompt_toolkit
    exits the prompt and appends to history with the expanded text, so ``Up``
    recall and run logs see what was actually submitted. The original key
    bindings and accept handler are restored in ``finally`` so state does not
    leak across prompts even on ``KeyboardInterrupt`` or EOF.

    Returns the expanded, stripped text, or ``None`` if the user interrupted.
    """
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
