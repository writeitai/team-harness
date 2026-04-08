# pyright: reportMissingParameterType=false

import pytest

from team_harness.config import Config
from team_harness.tracking.context import ContextTracker
from team_harness.tracking.context import resolve_model_limit


class ClientWithModels:
    async def get_models(self) -> dict:
        return {"data": [{"id": "x", "context_length": 321}]}


class ClientFail:
    async def get_models(self) -> dict:
        raise RuntimeError("nope")


@pytest.mark.asyncio
async def test_resolve_model_limit_fallbacks():
    assert await resolve_model_limit("x", ClientWithModels(), Config()) == 321
    assert await resolve_model_limit("openai/gpt-4o", ClientFail(), Config()) == 128_000
    with pytest.warns(UserWarning):
        assert (
            await resolve_model_limit(
                "unknown", ClientFail(), Config(context_limit=999)
            )
            == 999
        )
    with pytest.warns(UserWarning):
        assert await resolve_model_limit("unknown", ClientFail(), Config()) == 128_000


def test_context_tracker_properties():
    tracker = ContextTracker(model_id="m", model_limit=100)
    assert tracker.pct == 0.0
    tracker.update({"prompt_tokens": 40, "completion_tokens": 40})
    assert tracker.total == 80
    assert tracker.at_warning is True
