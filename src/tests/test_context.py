# pyright: reportMissingParameterType=false

import pytest

from team_harness.config import Config
from team_harness.coordinator.loop import _should_compact
from team_harness.tracking.context import ContextTracker
from team_harness.tracking.context import get_auto_compact_threshold
from team_harness.tracking.context import KNOWN_LIMITS
from team_harness.tracking.context import KNOWN_MAX_OUTPUT_TOKENS
from team_harness.tracking.context import resolve_model_limit


class ClientWithModels:
    def __init__(self, data: list[dict] | None = None) -> None:
        self.data = data or [{"id": "x", "context_length": 321}]

    async def get_models(self) -> dict:
        return {"data": self.data}


class ClientFail:
    async def get_models(self) -> dict:
        raise RuntimeError("nope")


@pytest.mark.asyncio
async def test_resolve_model_limit_fallbacks():
    assert (
        await resolve_model_limit(
            model_id="x", client=ClientWithModels(), config=Config()
        )
        == 321
    )
    assert (
        await resolve_model_limit(
            model_id="openai/gpt-4o", client=ClientFail(), config=Config()
        )
        == 128_000
    )
    with pytest.warns(UserWarning):
        assert (
            await resolve_model_limit(
                model_id="unknown",
                client=ClientFail(),
                config=Config(context_limit=999),
            )
            == 999
        )
    with pytest.warns(UserWarning):
        assert (
            await resolve_model_limit(
                model_id="unknown", client=ClientFail(), config=Config()
            )
            == 128_000
        )


@pytest.mark.asyncio
async def test_resolve_model_limit_fuzzy_matches_openrouter_prefix():
    assert (
        await resolve_model_limit(
            model_id="gpt-5.4",
            client=ClientWithModels(
                [{"id": "openai/gpt-5.4", "context_length": 1_050_000}]
            ),
            config=Config(),
        )
        == 1_050_000
    )


@pytest.mark.asyncio
async def test_resolve_model_limit_exact_still_preferred():
    assert (
        await resolve_model_limit(
            model_id="gpt-5.4",
            client=ClientWithModels(
                [
                    {"id": "gpt-5.4", "context_length": 900_000},
                    {"id": "openai/gpt-5.4", "context_length": 1_050_000},
                ]
            ),
            config=Config(),
        )
        == 900_000
    )


@pytest.mark.asyncio
async def test_resolve_model_limit_ambiguous_fuzzy_falls_through(monkeypatch):
    monkeypatch.setitem(KNOWN_LIMITS, "gpt-5.4", 777_777)
    assert (
        await resolve_model_limit(
            model_id="gpt-5.4",
            client=ClientWithModels(
                [
                    {"id": "openai/gpt-5.4", "context_length": 1_050_000},
                    {"id": "other/gpt-5.4", "context_length": 900_000},
                ]
            ),
            config=Config(),
        )
        == 777_777
    )


@pytest.mark.asyncio
async def test_resolve_model_limit_uses_codex_specific_gpt_5_5_limit():
    assert (
        await resolve_model_limit(
            model_id="gpt-5.5", client=ClientFail(), config=Config(provider="codex")
        )
        == 400_000
    )


def test_context_tracker_uses_latest_turn_usage_not_cumulative():
    tracker = ContextTracker(model_id="m", model_limit=200)

    tracker.update({"prompt_tokens": 40, "completion_tokens": 10})
    tracker.update({"prompt_tokens": 90, "completion_tokens": 20})

    assert tracker.prompt_tokens == 90
    assert tracker.completion_tokens == 20
    assert tracker.total == 110
    assert tracker.cumulative_prompt_tokens == 130
    assert tracker.cumulative_completion_tokens == 30


def test_context_tracker_reset_clears_warning_and_compaction_state():
    tracker = ContextTracker(model_id="m", model_limit=100)
    tracker.update({"prompt_tokens": 60, "completion_tokens": 30})
    tracker.at_warning_emitted = True
    tracker.usage_warning_emitted = True
    tracker.set_estimated_total(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "follow up"}]
    )
    tracker.consecutive_compact_failures = 2
    tracker.breaker_tripped = True

    tracker.reset()

    assert tracker.prompt_tokens == 0
    assert tracker.completion_tokens == 0
    assert tracker.cumulative_prompt_tokens == 0
    assert tracker.cumulative_completion_tokens == 0
    assert tracker.estimated_total_tokens is None
    assert tracker.at_warning_emitted is False
    assert tracker.usage_warning_emitted is False
    assert tracker.consecutive_compact_failures == 0
    assert tracker.breaker_tripped is False


def test_context_tracker_reset_rearms_compaction_breaker():
    tracker = ContextTracker(model_id="m", model_limit=100)
    tracker.breaker_tripped = True

    tracker.reset()

    assert tracker.breaker_tripped is False


def test_estimate_is_set_only_after_local_append():
    tracker = ContextTracker(model_id="m", model_limit=200)
    tracker.update({"prompt_tokens": 50, "completion_tokens": 10})

    assert tracker.estimated_total_tokens is None

    tracker.set_estimated_total(
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "another local append"},
        ]
    )

    assert tracker.estimated_total_tokens is not None
    assert tracker.has_estimate is True


def test_estimate_is_cleared_when_api_usage_arrives():
    tracker = ContextTracker(model_id="m", model_limit=200)
    tracker.set_estimated_total(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "u"}]
    )

    tracker.update({"prompt_tokens": 15, "completion_tokens": 5})

    assert tracker.estimated_total_tokens is None
    assert tracker.has_estimate is False


def test_should_compact_uses_ctx_total_with_estimate():
    tracker = ContextTracker(model_id="m", model_limit=100)
    tracker.update({"prompt_tokens": 0, "completion_tokens": 0})
    tracker.estimated_total_tokens = 500

    should_compact = _should_compact(
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "u"},
        ],
        ctx=tracker,
        threshold=50,
    )

    assert should_compact is True


def test_should_compact_uses_exact_when_estimate_is_none():
    tracker = ContextTracker(model_id="m", model_limit=100)
    tracker.update({"prompt_tokens": 40, "completion_tokens": 20})

    should_compact = _should_compact(
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "u"},
        ],
        ctx=tracker,
        threshold=50,
    )

    assert should_compact is True


def test_auto_compact_threshold_for_codex_mini_latest():
    assert KNOWN_MAX_OUTPUT_TOKENS["codex-mini-latest"] == 100_000
    assert get_auto_compact_threshold("codex-mini-latest", 200_000) == 167_000


def test_auto_compact_threshold_for_gpt_4o_uses_exact_output_cap():
    assert KNOWN_MAX_OUTPUT_TOKENS["openai/gpt-4o"] == 16_384
    assert get_auto_compact_threshold("openai/gpt-4o", 128_000) == 98_616


def test_auto_compact_threshold_for_gpt_4_1():
    assert get_auto_compact_threshold("openai/gpt-4.1", 1_047_576) == 1_014_576


def test_auto_compact_threshold_for_gpt_5_4():
    assert KNOWN_LIMITS["gpt-5.4"] == 1_050_000
    assert KNOWN_LIMITS["openai/gpt-5.4"] == 1_050_000
    assert KNOWN_MAX_OUTPUT_TOKENS["gpt-5.4"] == 128_000
    assert KNOWN_MAX_OUTPUT_TOKENS["openai/gpt-5.4"] == 128_000
    assert get_auto_compact_threshold("gpt-5.4", 1_050_000) == 1_017_000


def test_auto_compact_threshold_for_gpt_5_5():
    assert KNOWN_LIMITS["gpt-5.5"] == 1_000_000
    assert KNOWN_LIMITS["openai/gpt-5.5"] == 1_000_000
    assert KNOWN_LIMITS["gpt-5.5-pro"] == 1_000_000
    assert KNOWN_LIMITS["openai/gpt-5.5-pro"] == 1_000_000
    assert KNOWN_MAX_OUTPUT_TOKENS["gpt-5.5"] == 128_000
    assert KNOWN_MAX_OUTPUT_TOKENS["openai/gpt-5.5"] == 128_000
    assert KNOWN_MAX_OUTPUT_TOKENS["gpt-5.5-pro"] == 128_000
    assert KNOWN_MAX_OUTPUT_TOKENS["openai/gpt-5.5-pro"] == 128_000
    assert get_auto_compact_threshold("gpt-5.5", 1_000_000) == 967_000


def test_auto_compact_threshold_for_unknown_model_limit_uses_fallback_reserve():
    assert get_auto_compact_threshold("unknown", 50_000) == 29_000
    assert get_auto_compact_threshold("unknown", 500_000) == 467_000


def test_known_limits_updates_gpt_5_1_codex_models_to_400k():
    assert KNOWN_LIMITS["gpt-5.1-codex-mini"] == 400_000
    assert KNOWN_LIMITS["openai/gpt-5.1-codex-mini"] == 400_000
    assert KNOWN_LIMITS["gpt-5.1-codex-max"] == 400_000
    assert KNOWN_LIMITS["openai/gpt-5.1-codex-max"] == 400_000
