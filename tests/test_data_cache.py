"""Unit tests for data/cache.py.

DataCache previously always fetched fresh data via yfinance directly,
ignoring whichever market adapter a run had actually selected -- these
tests guard against that regressing (backtest/replay modes picking a
non-yfinance adapter, e.g. Upstox, would silently still hit yfinance).
"""

import pandas as pd
import pytest

from data.cache import DataCache


class _StubAdapter:
    def __init__(self, df):
        self.df = df
        self.calls = []

    def get_ohlcv(self, symbol, timeframe, bars=100, end=None):
        self.calls.append((symbol, timeframe, bars, end))
        return self.df


def _make_df(periods=10, start="2026-06-01", freq="D"):
    idx = pd.date_range(start, periods=periods, freq=freq)
    return pd.DataFrame(
        {
            "open": [100.0] * periods,
            "high": [101.0] * periods,
            "low": [99.0] * periods,
            "close": [100.5] * periods,
            "volume": [1000] * periods,
        },
        index=idx,
    )


@pytest.fixture
def cache(tmp_path):
    return DataCache(db_path=str(tmp_path / "cache.db"))


def test_get_ohlcv_uses_provided_market_adapter_not_yfinance(monkeypatch, cache):
    df = _make_df(periods=10, start="2026-06-01")
    adapter = _StubAdapter(df)

    def _fail_if_called(*a, **kw):
        raise AssertionError("yfinance should not be called when a market_adapter is provided")

    monkeypatch.setattr("data.cache.yf.download", _fail_if_called)

    result = cache.get_ohlcv("RELIANCE.NS", "1d", "2026-06-01", "2026-06-10", market_adapter=adapter)

    assert not result.empty
    assert adapter.calls  # adapter.get_ohlcv was actually invoked


def test_get_ohlcv_falls_back_to_yfinance_when_no_adapter_given(monkeypatch, cache):
    df = _make_df(periods=10, start="2026-06-01")
    monkeypatch.setattr("data.cache.yf.download", lambda *a, **kw: df)

    result = cache.get_ohlcv("RELIANCE.NS", "1d", "2026-06-01", "2026-06-10")

    assert not result.empty


def test_get_ohlcv_backfills_cache_from_adapter(monkeypatch, cache):
    df = _make_df(periods=10, start="2026-06-01")
    adapter = _StubAdapter(df)

    cache.get_ohlcv("RELIANCE.NS", "1d", "2026-06-01", "2026-06-10", market_adapter=adapter)
    coverage = cache.get_cache_coverage("RELIANCE.NS", "1d", "2026-06-01", "2026-06-10")

    assert coverage["cached_bars"] > 0
