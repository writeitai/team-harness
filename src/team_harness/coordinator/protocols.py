from typing import Any
from typing import Protocol

from team_harness.coordinator.client import ChatResponse


class CoordinatorLike(Protocol):
    model: str
    api_base: str
    provider: str

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        stream: bool = False,
        token_callback: Any = None,
    ) -> ChatResponse: ...

    async def aclose(self) -> None: ...

    async def get_models(self) -> dict[str, Any]: ...
