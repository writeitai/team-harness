"""Detect hard worker rate limits and track run-scoped circuit state."""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import json
import math
from pathlib import Path
import re

_MILLISECONDS_TIMESTAMP_THRESHOLD = 100_000_000_000
_EXPLICIT_429_MESSAGE = re.compile(
    r"(?i)(?:\bapi(?:\s+request)?\s+error\b|\bhttp(?:\s+error)?\b|"
    r"\bstatus(?:\s+code)?\b|[\"']?code[\"']?)\s*[:=]?\s*429(?:\.0)?\b"
)


@dataclass(frozen=True)
class RateLimitSignal:
    """A hard provider rejection extracted from one worker JSONL stream."""

    resets_at: datetime | None
    reason: str


@dataclass(frozen=True)
class RateLimitTrip:
    """The effective circuit window for one agent-template family."""

    family: str
    model: str | None
    tripped_at: datetime
    resets_at: datetime
    reason: str

    def as_dict(self) -> dict[str, str | None]:
        return {
            "family": self.family,
            "model": self.model,
            "tripped_at": self.tripped_at.isoformat(),
            "resets_at": self.resets_at.isoformat(),
            "reason": self.reason,
        }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: object) -> datetime | None:
    if isinstance(value, bool):
        return None
    try:
        timestamp = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(timestamp):
        return None
    # Provider streams are inconsistent about Unix seconds versus milliseconds.
    # Values above this threshold cannot be realistic reset dates in seconds
    # (they would be after year 5000), but are ordinary contemporary dates in ms.
    if abs(timestamp) >= _MILLISECONDS_TIMESTAMP_THRESHOLD:
        timestamp /= 1000
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _rate_limit_info(event: dict[str, object]) -> dict[str, object]:
    raw = event.get("rate_limit_info", event.get("rateLimitInfo"))
    return raw if isinstance(raw, dict) else {}


def _reset_from_event(event: dict[str, object]) -> datetime | None:
    info = _rate_limit_info(event)
    return _timestamp(info.get("resetsAt", event.get("resetsAt")))


def _is_rejected_rate_limit_event(event: dict[str, object]) -> bool:
    if event.get("type") != "rate_limit_event":
        return False
    return bool(_rejected_fields(event))


def _rejected_fields(event: dict[str, object]) -> tuple[str, ...]:
    info = _rate_limit_info(event)
    return tuple(
        name
        for name in ("status", "overageStatus")
        if info.get(name, event.get(name)) == "rejected"
    )


def _status_is_429(value: object) -> bool:
    if isinstance(value, bool):
        return False
    try:
        status = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return math.isfinite(status) and status == 429


def _result_is_error(event: dict[str, object]) -> bool:
    if event.get("type") != "result":
        return False
    is_error = event.get("is_error")
    if is_error is not None:
        return is_error is True
    status = event.get("status")
    return isinstance(status, str) and status.lower() in {"error", "failed", "failure"}


def _result_is_success(event: dict[str, object]) -> bool:
    if event.get("type") != "result":
        return False
    is_error = event.get("is_error")
    if is_error is not None:
        return is_error is False
    status = event.get("status")
    return isinstance(status, str) and status.lower() in {"success", "succeeded"}


def _result_status_is_429(event: dict[str, object]) -> bool:
    fields = (
        "api_error_status",
        "status_code",
        "statusCode",
        "http_status",
        "httpStatus",
        "code",
    )
    if any(_status_is_429(event.get(field)) for field in fields):
        return True
    error = event.get("error")
    return isinstance(error, dict) and any(
        _status_is_429(error.get(field)) for field in fields
    )


def _result_message_has_explicit_429(event: dict[str, object]) -> bool:
    """Recognize a typed terminal API 429 when a CLI omits a numeric field.

    Gemini stream-json terminal errors currently retain the explicit HTTP code
    in ``error.message`` but omit it as a separate field. This deliberately does
    not inspect arbitrary output text: the message must belong to an error
    terminal result and name an API/HTTP/status/code 429.
    """

    error = event.get("error")
    if not isinstance(error, dict):
        return False
    message = error.get("message")
    return (
        isinstance(message, str) and _EXPLICIT_429_MESSAGE.search(message) is not None
    )


def _is_429_result(event: dict[str, object]) -> bool:
    if event.get("type") != "result":
        return False
    return _result_is_error(event) and (
        _result_status_is_429(event) or _result_message_has_explicit_429(event)
    )


def _parse_events(raw_lines: Iterable[bytes]) -> Iterable[dict[str, object]]:
    for raw_line in raw_lines:
        try:
            event = json.loads(raw_line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(event, dict):
            yield event


def _later_reset(first: datetime | None, second: datetime | None) -> datetime | None:
    if first is None:
        return second
    if second is None:
        return first
    return max(first, second)


def _detect_rate_limit_events(
    events: Iterable[dict[str, object]],
) -> RateLimitSignal | None:
    """Resolve provisional rejection evidence against later terminal results."""

    pending: RateLimitSignal | None = None
    for event in events:
        if _is_rejected_rate_limit_event(event):
            rejected_fields = _rejected_fields(event)
            pending = RateLimitSignal(
                resets_at=_reset_from_event(event),
                reason=(
                    "rate_limit_event reported "
                    + ", ".join(f"{name}=rejected" for name in rejected_fields)
                ),
            )
        if _result_is_success(event):
            # A provider CLI may emit a rejection while retrying internally.
            # Its later successful terminal result means the worker recovered.
            pending = None
        elif _is_429_result(event):
            pending = RateLimitSignal(
                resets_at=_later_reset(
                    pending.resets_at if pending is not None else None,
                    _reset_from_event(event),
                ),
                reason="worker result reported api_error_status=429",
            )
    return pending


def detect_rate_limit(stdout_bytes: bytes) -> RateLimitSignal | None:
    """Return terminal hard-rate-limit evidence from worker JSONL bytes.

    Invalid and partial lines are ignored. Rejected events are provisional: a
    later successful terminal result clears them because the CLI recovered. A
    failing terminal 429 retains the most conservative reset from preceding
    rejected evidence when its own result omits or shortens the reset.
    """

    return _detect_rate_limit_events(_parse_events(stdout_bytes.splitlines()))


def detect_rate_limit_from_path(stdout_path: Path) -> RateLimitSignal | None:
    """Scan a finished worker's stdout JSONL without loading the file at once."""

    with stdout_path.open("rb") as handle:
        return _detect_rate_limit_events(_parse_events(handle))


def parse_rate_limited_spawn_result(result: str) -> dict[str, object] | None:
    """Parse the rate-limit branch of the polymorphic ``spawn_agent`` result.

    Successful spawns are bare ``agent_<id>`` strings. A circuit short-circuit
    is JSON whose ``spawned`` field is exactly false and whose ``status`` is
    ``rate_limited``. Other JSON and legacy ``ERROR:`` strings return ``None``.
    This gives programmatic tool callers a strict guard without changing the
    existing successful-spawn contract.
    """

    try:
        payload = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("spawned") is not False or payload.get("status") != "rate_limited":
        return None
    if not isinstance(payload.get("family"), str) or not isinstance(
        payload.get("resets_at"), str
    ):
        return None
    return payload


class RateLimitCircuitBreaker:
    """Run-local rate-limit evidence keyed by and blocking whole families."""

    def __init__(
        self,
        *,
        enabled: bool,
        default_cooldown_s: int,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.enabled = enabled
        self.default_cooldown_s = default_cooldown_s
        self._now = now or _utc_now
        self._trips: dict[str, RateLimitTrip] = {}

    def trip(
        self, *, family: str, model: str | None, signal: RateLimitSignal
    ) -> RateLimitTrip | None:
        if not self.enabled:
            return None
        tripped_at = self._now()
        candidate_reset = signal.resets_at or (
            tripped_at + timedelta(seconds=self.default_cooldown_s)
        )
        self._expire_stale()
        current = self._trips.get(family)
        resets_at = (
            max(current.resets_at, candidate_reset)
            if current is not None
            else candidate_reset
        )
        trip = RateLimitTrip(
            family=family,
            model=model,
            tripped_at=tripped_at,
            resets_at=resets_at,
            reason=signal.reason,
        )
        self._trips[family] = trip
        return trip

    def active_trip(self, family: str) -> RateLimitTrip | None:
        """Return an active family trip, clearing it once its window passes."""

        if not self.enabled:
            return None
        self._expire_stale()
        return self._trips.get(family)

    def active_trips(self) -> dict[str, RateLimitTrip]:
        """Return a copy of all active trips after expiring stale windows."""

        if not self.enabled:
            return {}
        self._expire_stale()
        return dict(self._trips)

    def _expire_stale(self) -> None:
        now = self._now()
        for key, trip in tuple(self._trips.items()):
            if now >= trip.resets_at:
                del self._trips[key]
