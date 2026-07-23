"""Unit tests for markets/upstox_auth.py. All HTTP calls are mocked."""

import json
from datetime import datetime

import pytest
from zoneinfo import ZoneInfo

import markets.upstox_auth as upstox_auth
from markets.upstox_auth import UpstoxAuthError

IST = ZoneInfo("Asia/Kolkata")


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def token_path(monkeypatch, tmp_path):
    path = tmp_path / "upstox_token.json"
    monkeypatch.setattr(upstox_auth, "_TOKEN_PATH", path)
    return path


# -- build_login_url -----------------------------------------------------------


def test_build_login_url_includes_required_params():
    url = upstox_auth.build_login_url("my-client-id", "http://host/callback", state="xyz")
    assert url.startswith(upstox_auth.AUTHORIZE_URL)
    assert "client_id=my-client-id" in url
    assert "redirect_uri=http%3A%2F%2Fhost%2Fcallback" in url
    assert "response_type=code" in url
    assert "state=xyz" in url


def test_build_login_url_omits_state_when_blank():
    url = upstox_auth.build_login_url("id", "http://host/callback")
    assert "state=" not in url


# -- exchange_code -----------------------------------------------------------


def test_exchange_code_saves_token_on_success(monkeypatch, token_path):
    monkeypatch.setattr(
        upstox_auth.httpx, "post", lambda *a, **kw: _FakeResponse(200, {"access_token": "tok-123"})
    )

    upstox_auth.exchange_code("code", "id", "secret", "http://host/callback")

    assert token_path.exists()
    saved = json.loads(token_path.read_text())
    assert saved["access_token"] == "tok-123"
    assert upstox_auth.get_access_token() == "tok-123"


def test_exchange_code_raises_on_non_200(monkeypatch):
    monkeypatch.setattr(upstox_auth.httpx, "post", lambda *a, **kw: _FakeResponse(400, {}, text="bad request"))
    with pytest.raises(UpstoxAuthError):
        upstox_auth.exchange_code("code", "id", "secret", "http://host/callback")


def test_exchange_code_raises_when_access_token_missing(monkeypatch):
    monkeypatch.setattr(upstox_auth.httpx, "post", lambda *a, **kw: _FakeResponse(200, {"foo": "bar"}))
    with pytest.raises(UpstoxAuthError):
        upstox_auth.exchange_code("code", "id", "secret", "http://host/callback")


# -- expiry -----------------------------------------------------------


def test_get_access_token_none_when_no_token_file():
    assert upstox_auth.get_access_token() is None


def test_token_valid_before_daily_cutoff(monkeypatch, token_path):
    obtained_at = datetime(2026, 7, 22, 20, 0, tzinfo=IST)  # 8 PM Wednesday
    upstox_auth._save_token("tok", obtained_at=obtained_at)

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 7, 23, 2, 0, tzinfo=tz)  # 2 AM Thursday, before 03:30 cutoff

    monkeypatch.setattr(upstox_auth, "datetime", _FrozenDatetime)
    assert upstox_auth.get_access_token() == "tok"


def test_token_expired_after_daily_cutoff(monkeypatch, token_path):
    obtained_at = datetime(2026, 7, 22, 20, 0, tzinfo=IST)  # 8 PM Wednesday
    upstox_auth._save_token("tok", obtained_at=obtained_at)

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 7, 23, 4, 0, tzinfo=tz)  # 4 AM Thursday, past 03:30 cutoff

    monkeypatch.setattr(upstox_auth, "datetime", _FrozenDatetime)
    assert upstox_auth.get_access_token() is None


def test_token_generated_before_cutoff_expires_same_day(monkeypatch, token_path):
    obtained_at = datetime(2026, 7, 23, 2, 30, tzinfo=IST)  # 2:30 AM Thursday
    upstox_auth._save_token("tok", obtained_at=obtained_at)

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 7, 23, 3, 45, tzinfo=tz)  # 3:45 AM same Thursday, past cutoff

    monkeypatch.setattr(upstox_auth, "datetime", _FrozenDatetime)
    assert upstox_auth.get_access_token() is None


# -- get_status / clear_token -----------------------------------------------------------


def test_get_status_reports_disconnected_with_no_token():
    status = upstox_auth.get_status()
    assert status == {"connected": False, "expires_at": None}


def test_clear_token_removes_file(token_path):
    upstox_auth._save_token("tok")
    assert token_path.exists()
    upstox_auth.clear_token()
    assert not token_path.exists()
