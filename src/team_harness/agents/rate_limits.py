"""Detect hard worker rate limits and track run-scoped circuit state."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import json
import math
from pathlib import Path


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


def _is_429_result(event: dict[str, object]) -> bool:
    if event.get("type") != "result":
        return False
    status = event.get("api_error_status")
    return not isinstance(status, bool) and str(status) == "429"


def detect_rate_limit(stdout_bytes: bytes) -> RateLimitSignal | None:
    """Return the last hard rate-limit signal in a worker JSONL byte stream.

    Invalid and partial lines are ignored. Later valid rate-limit records replace
    earlier ones; a terminal 429 result retains the reset time from the preceding
    rejected rate-limit event when the result itself omits it.
    """

    latest: RateLimitSignal | None = None
    rejected_reset: datetime | None = None
    for raw_line in stdout_bytes.splitlines():
        try:
            event = json.loads(raw_line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(event, dict):
            continue
        if _is_rejected_rate_limit_event(event):
            rejected_reset = _reset_from_event(event)
            rejected_fields = _rejected_fields(event)
            latest = RateLimitSignal(
                resets_at=rejected_reset,
                reason=(
                    "rate_limit_event reported "
                    + ", ".join(f"{name}=rejected" for name in rejected_fields)
                ),
            )
        if _is_429_result(event):
            latest = RateLimitSignal(
                resets_at=_reset_from_event(event) or rejected_reset,
                reason="worker result reported api_error_status=429",
            )
    return latest


def detect_rate_limit_from_path(stdout_path: Path) -> RateLimitSignal | None:
    """Scan a finished worker's stdout JSONL without loading the file at once."""

    latest: RateLimitSignal | None = None
    rejected_reset: datetime | None = None
    with stdout_path.open("rb") as handle:
        for raw_line in handle:
            signal = detect_rate_limit(raw_line)
            if signal is None:
                continue
            if signal.reason.startswith("rate_limit_event"):
                rejected_reset = signal.resets_at
            if signal.resets_at is None and signal.reason.endswith("=429"):
                signal = RateLimitSignal(resets_at=rejected_reset, reason=signal.reason)
            latest = signal
    return latest


class RateLimitCircuitBreaker:
    """Run-local rate-limit evidence keyed by family/model, blocking families."""

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
        self._trips: dict[tuple[str, str | None], RateLimitTrip] = {}

    def trip(
        self, *, family: str, model: str | None, signal: RateLimitSignal
    ) -> RateLimitTrip | None:
        if not self.enabled:
            return None
        tripped_at = self._now()
        resets_at = signal.resets_at or (
            tripped_at + timedelta(seconds=self.default_cooldown_s)
        )
        trip = RateLimitTrip(
            family=family,
            model=model,
            tripped_at=tripped_at,
            resets_at=resets_at,
            reason=signal.reason,
        )
        self._trips[(family, model)] = trip
        return trip

    def active_trip(self, family: str) -> RateLimitTrip | None:
        """Return an active family trip, clearing it once its window passes."""

        if not self.enabled:
            return None
        self._expire_stale()
        family_trips = [trip for trip in self._trips.values() if trip.family == family]
        if not family_trips:
            return None
        return max(family_trips, key=lambda trip: trip.tripped_at)

    def active_trips(self) -> dict[str, RateLimitTrip]:
        """Return a copy of all active trips after expiring stale windows."""

        if not self.enabled:
            return {}
        self._expire_stale()
        active: dict[str, RateLimitTrip] = {}
        for trip in self._trips.values():
            current = active.get(trip.family)
            if current is None or trip.tripped_at > current.tripped_at:
                active[trip.family] = trip
        return active

    def _expire_stale(self) -> None:
        now = self._now()
        for key, trip in tuple(self._trips.items()):
            if now >= trip.resets_at:
                del self._trips[key]
