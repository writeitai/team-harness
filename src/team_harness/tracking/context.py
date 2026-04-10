from dataclasses import dataclass
import json
from typing import Any
import warnings

from team_harness.config import Config

KNOWN_LIMITS: dict[str, int] = {
    "codex-mini-latest": 200_000,
    "openai/codex-mini-latest": 200_000,
    "gpt-5.1-codex-mini": 400_000,
    "openai/gpt-5.1-codex-mini": 400_000,
    "gpt-5.1-codex-max": 400_000,
    "openai/gpt-5.1-codex-max": 400_000,
    "openai/gpt-4.1": 1_047_576,
    "openai/gpt-4.1-mini": 1_047_576,
    "openai/gpt-4o": 128_000,
    "openai/o3": 200_000,
    "anthropic/claude-opus-4": 200_000,
    "anthropic/claude-sonnet-4": 200_000,
    "google/gemini-2.5-pro": 1_048_576,
}

KNOWN_MAX_OUTPUT_TOKENS: dict[str, int] = {
    "codex-mini-latest": 100_000,
    "openai/codex-mini-latest": 100_000,
    "gpt-5.1-codex-mini": 128_000,
    "openai/gpt-5.1-codex-mini": 128_000,
    "gpt-5.1-codex-max": 128_000,
    "openai/gpt-5.1-codex-max": 128_000,
    "openai/gpt-4.1": 32_768,
    "openai/gpt-4.1-mini": 32_768,
    "openai/gpt-4o": 16_384,
    "openai/o3": 100_000,
    "anthropic/claude-opus-4": 32_768,
    "anthropic/claude-sonnet-4": 64_000,
    "google/gemini-2.5-pro": 65_536,
}

KNOWN_CODEX_MODELS = {
    "codex-mini-latest",
    "openai/codex-mini-latest",
    "gpt-5.1-codex-mini",
    "openai/gpt-5.1-codex-mini",
    "gpt-5.1-codex-max",
    "openai/gpt-5.1-codex-max",
}


async def resolve_model_limit(model_id: str, client: Any, config: Config) -> int:
    try:
        if config.provider == "openai_compat" and hasattr(client, "get_models"):
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


def resolve_max_output_tokens(model_id: str, model_limit: int) -> int:
    if model_id in KNOWN_MAX_OUTPUT_TOKENS:
        return KNOWN_MAX_OUTPUT_TOKENS[model_id]
    return min(20_000, max(8_000, model_limit // 10))


def get_auto_compact_threshold(model_id: str, model_limit: int) -> int:
    max_output_tokens = resolve_max_output_tokens(model_id, model_limit)
    return model_limit - min(max_output_tokens, 20_000) - 13_000


def _estimate_message_tokens(messages: list[dict]) -> int:
    chars = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            chars += len(content)
        tool_calls = message.get("tool_calls")
        if tool_calls:
            chars += len(json.dumps(tool_calls, sort_keys=True))
        tool_call_id = message.get("tool_call_id")
        if isinstance(tool_call_id, str):
            chars += len(tool_call_id)
        role = message.get("role")
        if isinstance(role, str):
            chars += len(role)
    return max(1, chars // 4)


@dataclass
class ContextTracker:
    model_id: str
    model_limit: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cumulative_prompt_tokens: int = 0
    cumulative_completion_tokens: int = 0
    estimated_total_tokens: int | None = None
    at_warning_emitted: bool = False
    consecutive_compact_failures: int = 0
    breaker_tripped: bool = False

    def update(self, usage: object) -> None:
        if isinstance(usage, dict):
            prompt_tokens = int(usage.get("prompt_tokens", 0))
            completion_tokens = int(usage.get("completion_tokens", 0))
        else:
            prompt_tokens = int(getattr(usage, "prompt_tokens", 0))
            completion_tokens = int(getattr(usage, "completion_tokens", 0))
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.cumulative_prompt_tokens += prompt_tokens
        self.cumulative_completion_tokens += completion_tokens
        self.estimated_total_tokens = None

    def set_estimated_total(self, messages: list[dict]) -> None:
        self.estimated_total_tokens = _estimate_message_tokens(messages)

    def clear_estimate(self) -> None:
        self.estimated_total_tokens = None

    def reset(self) -> None:
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cumulative_prompt_tokens = 0
        self.cumulative_completion_tokens = 0
        self.estimated_total_tokens = None
        self.at_warning_emitted = False
        self.consecutive_compact_failures = 0
        self.breaker_tripped = False

    @property
    def total(self) -> int:
        if self.estimated_total_tokens is not None:
            return self.estimated_total_tokens
        return self.prompt_tokens + self.completion_tokens

    @property
    def pct(self) -> float:
        if self.model_limit == 0:
            return 0.0
        return self.total / self.model_limit * 100

    @property
    def at_warning(self) -> bool:
        return self.pct >= 80.0

    @property
    def has_estimate(self) -> bool:
        return self.estimated_total_tokens is not None
