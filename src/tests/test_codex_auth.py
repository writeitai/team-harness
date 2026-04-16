# pyright: reportMissingParameterType=false

import base64
import json
from pathlib import Path
import time

import pytest

from team_harness.coordinator.auth import CodexAuthError
from team_harness.coordinator.auth import load_codex_auth
from team_harness.coordinator.auth import resolve_codex_auth_path


def _make_jwt(payload: dict) -> str:
    header = {"alg": "HS256", "typ": "JWT"}

    def _encode(value: dict) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{_encode(header)}.{_encode(payload)}.signature"


def _write_auth(path: Path, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"tokens": {"access_token": token}}))


def test_resolve_codex_auth_path_prefers_configured_relative_path(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HARNESS_CODEX_AUTH_PATH", "env-auth.json")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))

    path = resolve_codex_auth_path("nested/auth.json", cwd=str(tmp_path / "project"))

    assert path == (tmp_path / "project" / "nested" / "auth.json").resolve()


def test_resolve_codex_auth_path_uses_env_then_codex_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_CODEX_AUTH_PATH", "env-auth.json")

    env_path = resolve_codex_auth_path(None, cwd=str(tmp_path))
    assert env_path == (tmp_path / "env-auth.json").resolve()

    monkeypatch.delenv("HARNESS_CODEX_AUTH_PATH")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))

    home_path = resolve_codex_auth_path(None, cwd=str(tmp_path))
    assert home_path == tmp_path / "codex-home" / "auth.json"


def test_load_codex_auth_success(tmp_path):
    token = _make_jwt(
        {
            "exp": int(time.time()) + 3600,
            "https://api.openai.com/auth": {"chatgpt_account_id": "acct_123"},
        }
    )
    path = tmp_path / "auth.json"
    _write_auth(path=path, token=token)

    auth = load_codex_auth(str(path), cwd=str(tmp_path))

    assert auth.token == token
    assert auth.account_id == "acct_123"


@pytest.mark.parametrize(
    ("payload_text", "message"),
    [
        ("{", "invalid JSON"),
        ('{"tokens": {}}', "does not contain an access token"),
        ('{"tokens": {"access_token": "bad-token"}}', "not a valid JWT"),
        (
            json.dumps(
                {
                    "tokens": {
                        "access_token": _make_jwt({"exp": int(time.time()) + 3600})
                    }
                }
            ),
            "missing chatgpt_account_id",
        ),
        (
            json.dumps(
                {
                    "tokens": {
                        "access_token": _make_jwt(
                            {
                                "exp": int(time.time()) - 3600,
                                "https://api.openai.com/auth": {
                                    "chatgpt_account_id": "acct_123"
                                },
                            }
                        )
                    }
                }
            ),
            "expired",
        ),
    ],
)
def test_load_codex_auth_validation_errors(tmp_path, payload_text, message):
    path = tmp_path / "auth.json"
    path.write_text(payload_text)

    with pytest.raises(CodexAuthError, match=message):
        load_codex_auth(str(path), cwd=str(tmp_path))


def test_load_codex_auth_handles_missing_file(tmp_path):
    with pytest.raises(CodexAuthError, match="not found"):
        load_codex_auth(str(tmp_path / "missing.json"), cwd=str(tmp_path))


def test_load_codex_auth_handles_permission_error(monkeypatch, tmp_path):
    path = tmp_path / "auth.json"
    path.write_text("{}")
    original_read_text = Path.read_text

    def fake_read_text(self, *args, **kwargs):
        if self == path:
            raise PermissionError("denied")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)

    with pytest.raises(CodexAuthError, match="not readable"):
        load_codex_auth(str(path), cwd=str(tmp_path))
