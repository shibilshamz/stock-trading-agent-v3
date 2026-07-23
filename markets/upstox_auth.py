"""Upstox OAuth2 login flow and access-token persistence.

Upstox access tokens always expire at 03:30 IST the following calendar day
(or the same day, if generated before 03:30) regardless of when they were
issued -- there is no refresh-token mechanism. This module exists so that
daily re-login is a dashboard button click (see dashboard/api.py's
/api/upstox/* routes) rather than a manual token paste into .env every
trading day.
"""

import json
import os
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx

IST = ZoneInfo("Asia/Kolkata")
AUTHORIZE_URL = "https://api.upstox.com/v2/login/authorization/dialog"
TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"
DAILY_EXPIRY_TIME = time(3, 30)

_TOKEN_PATH = Path(os.environ.get("UPSTOX_TOKEN_PATH", "data/upstox_token.json"))


class UpstoxAuthError(RuntimeError):
    """Raised when the OAuth login or token exchange fails."""


def build_login_url(client_id: str, redirect_uri: str, state: str = "") -> str:
    params = {"client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code"}
    if state:
        params["state"] = state
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def exchange_code(code: str, client_id: str, client_secret: str, redirect_uri: str) -> dict:
    """Exchanges a single-use authorization code for an access token and persists it."""
    response = httpx.post(
        TOKEN_URL,
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        headers={"Accept": "application/json"},
        timeout=15.0,
    )
    if response.status_code != 200:
        raise UpstoxAuthError(f"Token exchange failed ({response.status_code}): {response.text}")

    payload = response.json()
    access_token = payload.get("access_token")
    if not access_token:
        raise UpstoxAuthError(f"Token exchange response missing access_token: {payload}")

    _save_token(access_token)
    return payload


def get_access_token() -> Optional[str]:
    """Returns the current access token if present and not yet expired, else None."""
    stored = _load_token()
    if stored is None:
        return None
    obtained_at = datetime.fromisoformat(stored["obtained_at"])
    if datetime.now(IST) >= _expiry(obtained_at):
        return None
    return stored["access_token"]


def get_status() -> dict:
    stored = _load_token()
    if stored is None:
        return {"connected": False, "expires_at": None}
    obtained_at = datetime.fromisoformat(stored["obtained_at"])
    expires_at = _expiry(obtained_at)
    return {"connected": datetime.now(IST) < expires_at, "expires_at": expires_at.isoformat()}


def clear_token() -> None:
    if _TOKEN_PATH.exists():
        _TOKEN_PATH.unlink()


def _save_token(access_token: str, obtained_at: Optional[datetime] = None) -> None:
    obtained_at = obtained_at or datetime.now(IST)
    _TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    _TOKEN_PATH.write_text(json.dumps({"access_token": access_token, "obtained_at": obtained_at.isoformat()}))


def _load_token() -> Optional[dict]:
    if not _TOKEN_PATH.exists():
        return None
    try:
        return json.loads(_TOKEN_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _expiry(obtained_at: datetime) -> datetime:
    cutoff = obtained_at.replace(
        hour=DAILY_EXPIRY_TIME.hour, minute=DAILY_EXPIRY_TIME.minute, second=0, microsecond=0
    )
    return cutoff if obtained_at.time() < DAILY_EXPIRY_TIME else cutoff + timedelta(days=1)
