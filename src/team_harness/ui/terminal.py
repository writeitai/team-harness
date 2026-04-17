"""iTerm2 and terminal integration helpers."""

import atexit
import os
import sys

_ESC = "\033"
_BEL = "\007"
_atexit_registered = False


def is_iterm2() -> bool:
    """Check if running in iTerm2."""
    return os.environ.get("TERM_PROGRAM") == "iTerm.app" or bool(
        os.environ.get("ITERM_SESSION_ID")
    )


def _can_emit_progress() -> bool:
    """Check if OSC 9;4 progress sequences can be emitted."""
    return sys.stdout.isatty() and is_iterm2() and not os.environ.get("TMUX")


def set_terminal_progress() -> None:
    """Set indeterminate progress indicator in iTerm2 tab."""
    if not _can_emit_progress():
        return
    sys.stdout.write(f"{_ESC}]9;4;3;{_BEL}")
    sys.stdout.flush()


def clear_terminal_progress() -> None:
    """Clear progress indicator in iTerm2 tab."""
    if not _can_emit_progress():
        return
    sys.stdout.write(f"{_ESC}]9;4;0;{_BEL}")
    sys.stdout.flush()


def register_progress_cleanup() -> None:
    """Register atexit handler to clear progress on exit."""
    global _atexit_registered
    if _atexit_registered:
        return
    atexit.register(clear_terminal_progress)
    _atexit_registered = True
