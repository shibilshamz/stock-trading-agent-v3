"""Unit tests for markets/nse_adapter.py. All yfinance calls are mocked."""

from datetime import datetime as real_datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import markets.nse_adapter as nse_adapter_module
from markets.base import Order
from markets.nse_adapter import NSEAdapter

IST = ZoneInfo("Asia/Kolkata")


def _make_ohlcv_df(periods=10, start="2026-07-16 09:15", freq="15min", start_price=100.0, step=0.5):
    idx = pd.date_range(start, periods=periods, freq=freq)
    closes = [start_price + i * step for i in range(periods)]
    return pd.DataFrame(
        {
            "Open": [c - 0.2 for c in closes],
            "High": [c + 0.3 for c in closes],
            "Low": [c - 0.3 for c in closes],
            "Close": closes,
            "Volume": [1000 + i * 10 for i in range(periods)],
        },
        index=idx,
    )


def _make_batch_df(symbol_dfs):
    return pd.concat(symbol_dfs, axis=1)


def _freeze_now(monkeypatch, fixed_dt):
    class _FrozenDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_dt.astimezone(tz) if tz else fixed_dt

    monkeypatch.setattr(nse_adapter_module, "datetime", _FrozenDatetime)


# -- get_ohlcv ------------------------------------------------------------


def test_get_ohlcv_normalizes_columns_and_respects_bars(monkeypatch):
    fake_df = _make_ohlcv_df(periods=10)
    monkeypatch.setattr(nse_adapter_module.yf, "download", lambda *a, **kw: fake_df)

    adapter = NSEAdapter()
    df = adapter.get_ohlcv("RELIANCE.NS", timeframe="15m", bars=4)

    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert len(df) == 4


# -- get_universe -----------------------------------------------------------


def test_get_universe_ranks_by_composite_score(monkeypatch):
    idx = pd.date_range("2026-06-01", periods=20, freq="D")
    strong = pd.DataFrame(
        {
            "Open": [100] * 19 + [130],
            "High": [105] * 19 + [135],
            "Low": [95] * 19 + [125],
            "Close": [100] * 19 + [130],
            "Volume": [1_000_000] * 19 + [5_000_000],
        },
        index=idx,
    )
    flat = pd.DataFrame(
        {"Open": [100] * 20, "High": [101] * 20, "Low": [99] * 20, "Close": [100] * 20, "Volume": [10_000] * 20},
        index=idx,
    )
    batch_df = _make_batch_df({"STRONG.NS": strong, "FLAT.NS": flat})
    monkeypatch.setattr(nse_adapter_module.yf, "download", lambda *a, **kw: batch_df)

    adapter = NSEAdapter(config={"universe_symbols": ["STRONG.NS", "FLAT.NS"], "universe_min_bars": 5})
    universe = adapter.get_universe(top_n=2)

    assert universe[0] == "STRONG.NS"


# -- is_market_open -----------------------------------------------------------


def test_is_market_open_true_during_session(monkeypatch):
    _freeze_now(monkeypatch, real_datetime(2026, 7, 20, 10, 0, tzinfo=IST))  # Monday, 10:00 IST
    assert NSEAdapter().is_market_open() is True


def test_is_market_open_false_outside_session_hours(monkeypatch):
    _freeze_now(monkeypatch, real_datetime(2026, 7, 20, 16, 0, tzinfo=IST))  # Monday, after close
    assert NSEAdapter().is_market_open() is False


def test_is_market_open_false_on_weekend(monkeypatch):
    _freeze_now(monkeypatch, real_datetime(2026, 7, 18, 10, 0, tzinfo=IST))  # Saturday
    assert NSEAdapter().is_market_open() is False


# -- place_order -----------------------------------------------------------


def test_place_order_fills_with_bounded_slippage(monkeypatch):
    fake_df = _make_ohlcv_df(periods=2, start_price=100.0, step=0.0)
    monkeypatch.setattr(nse_adapter_module.yf, "download", lambda *a, **kw: fake_df)

    adapter = NSEAdapter()
    order = Order(symbol="RELIANCE.NS", side="BUY", quantity=10, order_type="MARKET")
    result = adapter.place_order(order)

    assert result.status == "FILLED"
    assert result.filled_quantity == 10
    assert 99.5 < result.filled_price < 100.5  # +/- 0.1% slippage around 100
