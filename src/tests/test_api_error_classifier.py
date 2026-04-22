# pyright: reportMissingParameterType=false

from team_harness.agents.api_error_classifier import classify_agent_failure


def test_rate_limit_429_detected():
    result = classify_agent_failure("Error: API request failed with status: 429")
    assert result is not None
    assert result.is_api_error is True
    assert result.category == "rate_limit"


def test_rate_limit_text_detected():
    result = classify_agent_failure("rate_limit_exceeded: too many requests")
    assert result is not None
    assert result.category == "rate_limit"


def test_overloaded_529_detected():
    result = classify_agent_failure("Error: status: 529 overloaded")
    assert result is not None
    assert result.category == "overloaded"


def test_overloaded_text_detected():
    result = classify_agent_failure("API is overloaded, try again later")
    assert result is not None
    assert result.category == "overloaded"


def test_auth_401_detected():
    result = classify_agent_failure("Error: unauthorized, status: 401")
    assert result is not None
    assert result.category == "auth"


def test_auth_invalid_key_detected():
    result = classify_agent_failure("Error: invalid API key provided")
    assert result is not None
    assert result.category == "auth"


def test_quota_detected():
    result = classify_agent_failure("insufficient_quota: billing limit reached")
    assert result is not None
    assert result.category == "quota"


def test_server_error_502_detected():
    result = classify_agent_failure("Error: bad gateway, status: 502")
    assert result is not None
    assert result.category == "server_error"


def test_server_error_503_detected():
    result = classify_agent_failure("service unavailable")
    assert result is not None
    assert result.category == "server_error"


def test_model_unavailable_detected():
    result = classify_agent_failure("Error: model not found: gpt-6")
    assert result is not None
    assert result.category == "model_unavailable"


def test_no_api_error_returns_none():
    result = classify_agent_failure("Traceback: IndexError: list index out of range")
    assert result is None


def test_empty_output_returns_none():
    result = classify_agent_failure("")
    assert result is None


def test_normal_failure_not_classified():
    result = classify_agent_failure(
        "FAILED tests/test_main.py::test_foo - AssertionError: expected 1, got 2"
    )
    assert result is None


def test_stdout_also_scanned():
    result = classify_agent_failure(
        stderr_text="Process exited with code 1",
        stdout_text="Error: rate limit exceeded",
    )
    assert result is not None
    assert result.category == "rate_limit"


def test_resource_exhausted_detected():
    result = classify_agent_failure("google.api_core.exceptions.ResourceExhausted: RESOURCE_EXHAUSTED")
    assert result is not None
    assert result.category == "rate_limit"


def test_permission_denied_detected():
    result = classify_agent_failure("PERMISSION_DENIED: caller does not have permission")
    assert result is not None
    assert result.category == "auth"
