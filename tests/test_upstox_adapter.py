"""Unit tests for markets/upstox_adapter.py. All HTTP calls are mocked."""

import gzip
import json
from datetime import datetime as real_datetime

import pandas as pd
import pytest
from zoneinfo import ZoneInfo

import markets.nse_adapter as nse_adapter_module
import markets.upstox_adapter as upstox_adapter_module
from markets.base import Order
from markets.upstox_adapter import UpstoxAdapter, UpstoxNotConnectedError

IST = ZoneInfo("Asia/Kolkata")


class _FakeResponse:
    def __init__(self, payload=None, content=b"", status_code=200):
        self._payload = payload or {}
        self.content = content
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _freeze_now(monkeypatch, fixed_dt):
    class _FrozenDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_dt.astimezone(tz) if tz else fixed_dt

    monkeypatch.setattr(nse_adapter_module, "datetime", _FrozenDatetime)


@pytest.fixture(autouse=True)
def _with_token(monkeypatch):
    monkeypatch.setattr(upstox_adapter_module, "get_access_token", lambda: "test-token")


@pytest.fixture
def adapter(tmp_path):
    return UpstoxAdapter(config={"instruments_cache_path": str(tmp_path / "instruments.json")})


# -- auth -----------------------------------------------------------


def test_auth_headers_raises_when_not_connected(monkeypatch, adapter):
    monkeypatch.setattr(upstox_adapter_module, "get_access_token", lambda: None)
    with pytest.raises(UpstoxNotConnectedError):
        adapter._auth_headers()


def test_auth_headers_uses_bearer_token(adapter):
    assert adapter._auth_headers()["Authorization"] == "Bearer test-token"


# -- instrument key lookup -----------------------------------------------------------


def test_instrument_key_strips_suffix_and_looks_up(adapter):
    adapter._instrument_map = {"RELIANCE": "NSE_EQ|INE002A01018"}
    assert adapter._instrument_key("RELIANCE.NS") == "NSE_EQ|INE002A01018"


def test_instrument_key_raises_for_unknown_symbol(adapter):
    adapter._instrument_map = {}
    with pytest.raises(KeyError):
        adapter._instrument_key("UNKNOWN.NS")


def test_download_instrument_map_filters_nse_eq(monkeypatch, adapter):
    records = [
        {"trading_symbol": "RELIANCE", "instrument_key": "NSE_EQ|INE002A01018", "segment": "NSE_EQ"},
        {"trading_symbol": "RELIANCE-FUT", "instrument_key": "NSE_FO|XYZ", "segment": "NSE_FO"},
    ]
    gz = gzip.compress(json.dumps(records).encode())
    monkeypatch.setattr(upstox_adapter_module.httpx, "get", lambda *a, **kw: _FakeResponse(content=gz))

    result = UpstoxAdapter._download_instrument_map()

    assert result == {"RELIANCE": "NSE_EQ|INE002A01018"}


def test_load_instrument_map_caches_to_disk(monkeypatch, adapter):
    monkeypatch.setattr(
        upstox_adapter_module.UpstoxAdapter, "_download_instrument_map", staticmethod(lambda: {"X": "Y"})
    )
    result = adapter._load_instrument_map()
    assert result == {"X": "Y"}
    assert adapter.config["instruments_cache_path"]


# -- get_latest_price -----------------------------------------------------------


def test_get_latest_price_parses_ltp_response(monkeypatch, adapter):
    adapter._instrument_map = {"RELIANCE": "NSE_EQ|INE002A01018"}
    monkeypatch.setattr(
        upstox_adapter_module.httpx,
        "get",
        lambda *a, **kw: _FakeResponse({"data": {"NSE_EQ:RELIANCE": {"last_price": 2500.5}}}),
    )
    assert adapter.get_latest_price("RELIANCE.NS") == 2500.5


def test_get_latest_price_raises_without_token(monkeypatch, adapter):
    monkeypatch.setattr(upstox_adapter_module, "get_access_token", lambda: None)
    adapter._instrument_map = {"RELIANCE": "NSE_EQ|INE002A01018"}
    with pytest.raises(UpstoxNotConnectedError):
        adapter.get_latest_price("RELIANCE.NS")


# -- historical candles -----------------------------------------------------------


def test_fetch_candles_parses_response_into_dataframe(monkeypatch, adapter):
    payload = {
        "data": {
            "candles": [
                ["2026-07-20T09:30:00+05:30", 100, 105, 99, 104, 1000, 0],
                ["2026-07-20T09:15:00+05:30", 98, 101, 97, 100, 900, 0],
            ]
        }
    }
    monkeypatch.setattr(upstox_adapter_module.httpx, "get", lambda *a, **kw: _FakeResponse(payload))

    from datetime import date

    df = adapter._fetch_candles("NSE_EQ|X", "minutes", 15, date(2026, 7, 20), date(2026, 7, 20))

    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert len(df) == 2
    assert df.index.is_monotonic_increasing  # sorted despite reverse-chronological input


def test_get_ohlcv_trims_to_requested_bars(monkeypatch, adapter):
    adapter._instrument_map = {"RELIANCE": "NSE_EQ|X"}
    idx = pd.date_range("2026-07-01 09:15", periods=10, freq="15min")
    fake_df = pd.DataFrame(
        {"open": range(10), "high": range(10), "low": range(10), "close": range(10), "volume": [100] * 10},
        index=idx,
    )
    monkeypatch.setattr(adapter, "_fetch_candles", lambda *a, **kw: fake_df)

    result = adapter.get_ohlcv("RELIANCE.NS", timeframe="15m", bars=3)

    assert len(result) == 3
    assert list(result["close"]) == [7, 8, 9]


def test_get_ohlcv_anchors_window_to_end_param_not_today(monkeypatch, adapter):
    """DataCache relies on get_ohlcv(..., end=...) to fetch a specific past
    window -- without this, bars=N always anchors to "today", so a backtest
    asking for e.g. Jan-Feb 2026 data would silently get June-July instead."""
    adapter._instrument_map = {"RELIANCE": "NSE_EQ|X"}
    captured = {}

    def fake_fetch(instrument_key, unit, interval, start_date, end_date):
        captured["start"] = start_date
        captured["end"] = end_date
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    monkeypatch.setattr(adapter, "_fetch_candles", fake_fetch)

    from datetime import date

    adapter.get_ohlcv("RELIANCE.NS", timeframe="15m", bars=25, end=date(2026, 2, 23))

    assert captured["end"] <= date(2026, 2, 23)
    assert captured["start"] < captured["end"]


def test_get_ohlcv_returns_empty_frame_when_no_candles(monkeypatch, adapter):
    adapter._instrument_map = {"RELIANCE": "NSE_EQ|X"}
    monkeypatch.setattr(
        adapter, "_fetch_candles", lambda *a, **kw: pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    )
    result = adapter.get_ohlcv("RELIANCE.NS", timeframe="15m", bars=10)
    assert result.empty


# -- place_order -----------------------------------------------------------


def test_place_order_fills_with_bounded_slippage(monkeypatch, adapter):
    monkeypatch.setattr(adapter, "get_latest_price", lambda symbol: 100.0)
    order = Order(symbol="RELIANCE.NS", side="BUY", quantity=10, order_type="MARKET")

    result = adapter.place_order(order)

    assert result.status == "FILLED"
    assert result.filled_quantity == 10
    assert 99.5 < result.filled_price < 100.5


# -- market hours (delegated to NSEAdapter) -----------------------------------------------------------


def test_is_market_open_true_during_session(monkeypatch, adapter):
    _freeze_now(monkeypatch, real_datetime(2026, 7, 20, 10, 0, tzinfo=IST))  # Monday, 10:00 IST
    assert adapter.is_market_open() is True


def test_is_market_open_false_on_weekend(monkeypatch, adapter):
    _freeze_now(monkeypatch, real_datetime(2026, 7, 18, 10, 0, tzinfo=IST))  # Saturday
    assert adapter.is_market_open() is False


def test_get_market_hours_matches_nse_session(adapter):
    from datetime import time

    assert adapter.get_market_hours() == (time(9, 15), time(15, 30))


# -- universe ranking -----------------------------------------------------------


def test_get_universe_ranks_by_composite_score(monkeypatch, adapter):
    idx = pd.date_range("2026-06-01", periods=20, freq="D")
    strong = pd.DataFrame(
        {
            "open": [100] * 19 + [130],
            "high": [105] * 19 + [135],
            "low": [95] * 19 + [125],
            "close": [100] * 19 + [130],
            "volume": [1_000_000] * 19 + [5_000_000],
        },
        index=idx,
    )
    flat = pd.DataFrame(
        {"open": [100] * 20, "high": [101] * 20, "low": [99] * 20, "close": [100] * 20, "volume": [10_000] * 20},
        index=idx,
    )

    def fake_get_ohlcv(symbol, timeframe="1d", bars=100):
        return strong if symbol == "STRONG.NS" else flat

    monkeypatch.setattr(adapter, "get_ohlcv", fake_get_ohlcv)
    adapter.config["universe_symbols"] = ["STRONG.NS", "FLAT.NS"]
    adapter.config["universe_min_bars"] = 5

    universe = adapter.get_universe(top_n=2)

    assert universe[0] == "STRONG.NS"
