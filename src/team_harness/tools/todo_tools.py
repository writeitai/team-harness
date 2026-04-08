import asyncio
import json
from pathlib import Path

_todo_path: Path | None = None
_VALID_STATUSES = {"pending", "in_progress", "done", "blocked"}

TODO_WRITE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "todo_write",
        "description": "Replace the current todo list.",
        "parameters": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "description": {"type": "string"},
                            "status": {"type": "string"},
                            "priority": {"type": "string"},
                        },
                        "required": ["id", "description", "status"],
                    },
                }
            },
            "required": ["tasks"],
        },
    },
}
TODO_READ_SCHEMA = {
    "type": "function",
    "function": {
        "name": "todo_read",
        "description": "Read the current todo list.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


def setup(run_dir: Path) -> None:
    global _todo_path
    _todo_path = run_dir / "todo.json"


async def todo_write(tasks: list[dict]) -> str:
    for task in tasks:
        if task.get("status") not in _VALID_STATUSES:
            return f"ERROR: invalid status {task.get('status')!r}"
    if _todo_path is None:
        return "ERROR: todo store not configured"

    def _write() -> str:
        assert _todo_path is not None
        _todo_path.parent.mkdir(parents=True, exist_ok=True)
        _todo_path.write_text(json.dumps(tasks, indent=2))
        return f"Todo list updated ({len(tasks)} tasks)."

    return await asyncio.to_thread(_write)


async def todo_read() -> str:
    if _todo_path is None:
        return "[]"

    def _read() -> str:
        assert _todo_path is not None
        if not _todo_path.exists():
            return "[]"
        return _todo_path.read_text()

    return await asyncio.to_thread(_read)
