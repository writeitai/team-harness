"""Paste-preview state for the interactive REPL.

Collapses long bracketed pastes into a compact ``[Pasted text #N +M lines]``
placeholder so the prompt buffer stays readable while editing, and expands the
placeholder back to the original pasted payload on submit. Pure module — no
prompt_toolkit dependency so it can be unit-tested in isolation.
"""

import re

PASTE_LINE_THRESHOLD = 4
PLACEHOLDER_FORMAT = "[Pasted text #{id} +{lines} lines]"
# Regex is exact — the ` +N lines` suffix is required so user-typed text like
# ``[Pasted text #1]`` never accidentally triggers expansion.
PLACEHOLDER_RE = re.compile(r"\[Pasted text #(?P<id>\d+) \+\d+ lines\]")


class PasteBuffer:
    """Per-prompt registry of collapsed pastes.

    One instance lives for the duration of a single ``read_user_input`` call:
    ``counter`` and ``entries`` reset naturally by constructing a fresh buffer
    for the next prompt.
    """

    def __init__(self) -> None:
        self.entries: dict[int, str] = {}
        self.counter = 0

    def store_and_placeholder(self, data: str) -> str:
        """Normalize a pasted block and return either the raw text or a placeholder.

        CRLF and bare CR are normalized to LF first. Empty input is a no-op
        (returns ``""`` and stores nothing). Below ``PASTE_LINE_THRESHOLD``
        newlines the raw normalized text is returned for inline editing.
        Otherwise the payload is stashed under an incrementing id and the
        formatted placeholder is returned.
        """
        normalized = data.replace("\r\n", "\n").replace("\r", "\n")
        if not normalized:
            return ""

        line_count = normalized.count("\n")
        if line_count < PASTE_LINE_THRESHOLD:
            return normalized

        self.counter += 1
        self.entries[self.counter] = normalized
        return PLACEHOLDER_FORMAT.format(id=self.counter, lines=line_count)

    def expand(self, text: str) -> str:
        """Replace every known placeholder in ``text`` with its stored payload.

        Splices each match from the rightmost offset backward so indices stay
        valid and placeholder-looking substrings inside already-expanded payloads
        are never re-scanned. Unknown ids are left untouched; partial or malformed
        placeholders simply fail to match and pass through literally.
        """
        expanded = text
        matches = list(re.finditer(PLACEHOLDER_RE, text))
        for match in reversed(matches):
            entry = self.entries.get(int(match.group("id")))
            if entry is None:
                continue
            expanded = expanded[: match.start()] + entry + expanded[match.end() :]
        return expanded

    def reset(self) -> None:
        """Drop all stored pastes and reset numbering.

        Not used in the standard per-prompt flow, where a fresh instance is
        constructed instead; kept for callers that want to reuse one buffer.
        """
        self.entries.clear()
        self.counter = 0
