import re

PASTE_LINE_THRESHOLD = 4
PLACEHOLDER_FORMAT = "[Pasted text #{id} +{lines} lines]"
PLACEHOLDER_RE = re.compile(r"\[Pasted text #(?P<id>\d+) \+\d+ lines\]")


class PasteBuffer:
    def __init__(self) -> None:
        self.entries: dict[int, str] = {}
        self.counter = 0

    def store_and_placeholder(self, data: str) -> str:
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
        expanded = text
        matches = list(re.finditer(PLACEHOLDER_RE, text))
        for match in reversed(matches):
            entry = self.entries.get(int(match.group("id")))
            if entry is None:
                continue
            expanded = expanded[: match.start()] + entry + expanded[match.end() :]
        return expanded

    def reset(self) -> None:
        self.entries.clear()
        self.counter = 0
