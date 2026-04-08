from collections.abc import Awaitable
from collections.abc import Callable


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, tuple[dict, Callable[..., Awaitable[str]]]] = {}

    def register(self, schema: dict, fn: Callable[..., Awaitable[str]]) -> None:
        function = schema["function"]
        self._tools[function["name"]] = (schema, fn)

    def get_all_schemas(self) -> list[dict]:
        return [schema for schema, _ in self._tools.values()]

    async def execute(self, name: str, arguments: dict) -> str:
        if name not in self._tools:
            return f"ERROR: unknown tool: {name!r}"
        _, fn = self._tools[name]
        return await fn(**arguments)
