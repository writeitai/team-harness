from dataclasses import dataclass
from typing import Any
import warnings

from team_harness.config import Config

KNOWN_LIMITS: dict[str, int] = {
    "openai/gpt-4.1": 1_047_576,
    "openai/gpt-4.1-mini": 1_047_576,
    "openai/gpt-4o": 128_000,
    "openai/o3": 200_000,
    "anthropic/claude-opus-4": 200_000,
    "anthropic/claude-sonnet-4": 200_000,
    "google/gemini-2.5-pro": 1_048_576,
}


async def resolve_model_limit(model_id: str, client: Any, config: Config) -> int:
    try:
        if hasattr(client, "get_models"):
            models = await client.get_models()
            for model in models.get("data", []):
                if model.get("id") == model_id:
                    limit = model.get("context_length") or model.get("context_window")
                    if limit:
                        return int(limit)
    except Exception:
        pass

    if model_id in KNOWN_LIMITS:
        return KNOWN_LIMITS[model_id]
    if config.context_limit:
        warnings.warn(
            f"Context limit for {model_id!r} unknown — using config value {config.context_limit:,}.",
            stacklevel=2,
        )
        return config.context_limit
    warnings.warn(
        f"Context limit for {model_id!r} unknown — defaulting to 128,000 tokens.",
        stacklevel=2,
    )
    return 128_000


@dataclass
class ContextTracker:
    model_id: str
    model_limit: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    at_warning_emitted: bool = False

    def update(self, usage: object) -> None:
        if isinstance(usage, dict):
            self.prompt_tokens += int(usage.get("prompt_tokens", 0))
            self.completion_tokens += int(usage.get("completion_tokens", 0))
            return
        self.prompt_tokens += int(getattr(usage, "prompt_tokens", 0))
        self.completion_tokens += int(getattr(usage, "completion_tokens", 0))

    def reset(self) -> None:
        """Reset token tracking after a conversation reset."""
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.at_warning_emitted = False

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def pct(self) -> float:
        if self.model_limit == 0:
            return 0.0
        return self.total / self.model_limit * 100

    @property
    def at_warning(self) -> bool:
        return self.pct >= 80.0
