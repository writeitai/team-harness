"""Classify agent failures as API errors to enable failover.

When a worker agent exits with a non-zero code, this module scans its
stderr (and optionally stdout) for patterns that indicate the failure
was caused by an upstream API error — rate limits, auth failures, server
errors, quota exhaustion, etc.

The classification is advisory: it helps the coordinator decide whether
to retry the task with a different agent type, but the coordinator still
reads the full output and makes the final call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class FailureClassification:
    """Structured result of scanning agent output for API error signals."""

    is_api_error: bool
    category: str  # rate_limit | overloaded | auth | quota | server_error | model_unavailable
    detail: str  # human-readable one-liner


# Each tuple: (compiled regex, category, human-readable detail).
# Tested against the combined stderr+stdout of a *failed* agent process.
# Order matters — first match wins.
_API_ERROR_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    # Rate limiting (most common transient failure)
    (
        re.compile(
            r"rate.?limit|too many requests|rate_limit_exceeded"
            r"|throttl(ed|ing)|RESOURCE_EXHAUSTED"
            r"|status[:\s]+429\b|\b429\b.*error",
            re.IGNORECASE,
        ),
        "rate_limit",
        "API rate limit exceeded",
    ),
    # Overloaded / capacity (Anthropic 529, generic)
    (
        re.compile(
            r"overloaded|overloaded_error|at capacity"
            r"|temporarily unavail|status[:\s]+529\b|\b529\b.*error",
            re.IGNORECASE,
        ),
        "overloaded",
        "API overloaded or at capacity",
    ),
    # Auth / permission errors
    (
        re.compile(
            r"unauthorized|forbidden|invalid.?api.?key"
            r"|authentication.?fail|PERMISSION_DENIED|UNAUTHENTICATED"
            r"|status[:\s]+40[13]\b",
            re.IGNORECASE,
        ),
        "auth",
        "API authentication or authorization failure",
    ),
    # Quota / billing
    (
        re.compile(
            r"insufficient.?quota|billing|insufficient.?(funds|credits)"
            r"|exceeded.?budget|payment.?required"
            r"|status[:\s]+402\b",
            re.IGNORECASE,
        ),
        "quota",
        "API quota or billing limit reached",
    ),
    # Server errors (5xx)
    (
        re.compile(
            r"internal.?server.?error|bad.?gateway|service.?unavail"
            r"|gateway.?timeout|UNAVAILABLE|INTERNAL"
            r"|status[:\s]+50[0234]\b",
            re.IGNORECASE,
        ),
        "server_error",
        "API server error",
    ),
    # Model unavailable
    (
        re.compile(
            r"model.?(not.?found|unavail|does.?not.?exist)"
            r"|decommission|model_not_found|NOT_FOUND",
            re.IGNORECASE,
        ),
        "model_unavailable",
        "Requested model is not available",
    ),
]


def classify_agent_failure(
    stderr_text: str, stdout_text: str = ""
) -> FailureClassification | None:
    """Scan agent output for API error patterns.

    Returns a ``FailureClassification`` when the output matches a known
    API error pattern, or ``None`` when the failure appears to be a
    non-API issue (code bug, test failure, git conflict, etc.).

    Should only be called for agents that exited with a non-zero code.
    """
    combined = f"{stderr_text}\n{stdout_text}"
    for pattern, category, detail in _API_ERROR_PATTERNS:
        if pattern.search(combined):
            return FailureClassification(
                is_api_error=True, category=category, detail=detail
            )
    return None
