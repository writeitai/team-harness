import base64
from dataclasses import dataclass
import json
import os
from pathlib import Path
import time
from typing import Any

JWT_AUTH_CLAIM = "https://api.openai.com/auth"


class CodexAuthError(Exception):
    pass


@dataclass(frozen=True)
class CodexAuth:
    token: str
    account_id: str


def resolve_codex_auth_path(configured_path: str | None, *, cwd: str) -> Path:
    if configured_path and configured_path.strip():
        return _resolve_auth_path(configured_path, cwd=cwd)

    env_path = os.environ.get("TEAM_HARNESS_CODEX_AUTH_PATH", "").strip()
    if env_path:
        return _resolve_auth_path(env_path, cwd=cwd)

    codex_home = os.environ.get("CODEX_HOME", "").strip()
    if codex_home:
        return Path(codex_home).expanduser() / "auth.json"

    return Path("~/.codex/auth.json").expanduser()


def load_codex_auth(configured_path: str | None, *, cwd: str) -> CodexAuth:
    path = resolve_codex_auth_path(configured_path, cwd=cwd)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CodexAuthError(
            f"Codex auth file not found at {path}. Run `codex login` and retry."
        ) from exc
    except PermissionError as exc:
        raise CodexAuthError(
            f"Codex auth file is not readable at {path}. Run `codex login` and retry."
        ) from exc
    except OSError as exc:
        raise CodexAuthError(
            f"Could not read Codex auth file at {path}: {exc}. "
            "Run `codex login` and retry."
        ) from exc
    except json.JSONDecodeError as exc:
        raise CodexAuthError(
            f"Codex auth file at {path} contains invalid JSON. "
            "Run `codex login` and retry."
        ) from exc

    if not isinstance(payload, dict):
        raise CodexAuthError(
            f"Codex auth file at {path} is invalid. Run `codex login` and retry."
        )

    token = _extract_token(payload)
    if not token:
        raise CodexAuthError(
            f"Codex auth file at {path} does not contain an access token. "
            "Run `codex login` and retry."
        )

    try:
        account_id = _extract_account_id(token)
        expires_at_ms = _decode_jwt_expiry(token)
    except ValueError as exc:
        raise CodexAuthError(f"{exc} Run `codex login` and retry.") from exc

    if expires_at_ms <= int(time.time() * 1000):
        raise CodexAuthError(
            "Codex auth token is expired. Run `codex login` and retry."
        )

    return CodexAuth(token=token, account_id=account_id)


def _resolve_auth_path(path_text: str, *, cwd: str) -> Path:
    path = Path(path_text).expanduser()
    if path.is_absolute():
        return path
    return Path(cwd).expanduser().resolve() / path


def _extract_token(payload: dict[str, Any]) -> str:
    tokens = payload.get("tokens")
    if isinstance(tokens, dict):
        token = str(tokens.get("access_token", "") or "").strip()
        if token:
            return token
    return str(payload.get("OPENAI_API_KEY", "") or "").strip()


def _extract_account_id(token: str) -> str:
    payload = _decode_json_web_token_payload(token)
    auth_claim = payload.get(JWT_AUTH_CLAIM)
    if not isinstance(auth_claim, dict):
        raise ValueError("Codex auth token is missing chatgpt_account_id.")
    account_id = auth_claim.get("chatgpt_account_id")
    if not isinstance(account_id, str) or not account_id.strip():
        raise ValueError("Codex auth token is missing chatgpt_account_id.")
    return account_id


def _decode_jwt_expiry(token: str) -> int:
    exp = _decode_json_web_token_claim(token, ["exp"])
    if isinstance(exp, int):
        return exp * 1000
    if isinstance(exp, float):
        return int(exp * 1000)
    if isinstance(exp, str) and exp.strip().isdigit():
        return int(exp.strip()) * 1000
    raise ValueError("Codex auth token is not a valid JWT.")


def _decode_json_web_token_claim(token: str, path: list[str]) -> Any:
    payload = _decode_json_web_token_payload(token)
    current: Any = payload
    for key in path:
        if isinstance(current, dict) and key in current:
            current = current[key]
            continue
        raise ValueError("Codex auth token is not a valid JWT.")
    return current


def _decode_json_web_token_payload(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Codex auth token is not a valid JWT.")
    try:
        encoded = parts[1]
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = json.loads(
            base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        )
    except Exception as exc:
        raise ValueError("Codex auth token is not a valid JWT.") from exc
    if not isinstance(payload, dict):
        raise ValueError("Codex auth token is not a valid JWT.")
    return payload
