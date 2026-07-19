"""Unit tests for data_feeds/historical_replay.py."""

import time

import pandas as pd
import pytest

from data_feeds.historical_replay import HistoricalReplayFeed


class _StubCache:
    def __init__(self, data):
        self.data = data

    def get_ohlcv(self, symbol, timeframe, start, end):
        return self.data[symbol]


def _flat_df(periods, freq="1min", start="2026-07-16 09:15"):
    idx = pd.date_range(start, periods=periods, freq=freq)
    return pd.DataFrame(
        {"open": [10] * periods, "high": [10.5] * periods, "low": [9.5] * periods, "close": [10.2] * periods, "volume": [100] * periods},
        index=idx,
    )


def test_constructor_rejects_start_after_end():
    with pytest.raises(ValueError):
        HistoricalReplayFeed(market_adapter=None, start_date="2026-01-02", end_date="2026-01-01")


# -- bar sequence -----------------------------------------------------------


def test_bars_are_replayed_in_chronological_order_across_symbols():
    idx_a = pd.to_datetime(["2026-07-16 09:15:00", "2026-07-16 09:16:00", "2026-07-16 09:17:00"])
    idx_b = pd.to_datetime(["2026-07-16 09:15:30", "2026-07-16 09:16:30", "2026-07-16 09:17:30"])
    df_a = pd.DataFrame({"open": [10] * 3, "high": [10.5] * 3, "low": [9.5] * 3, "close": [10.2] * 3, "volume": [100] * 3}, index=idx_a)
    df_b = pd.DataFrame({"open": [20] * 3, "high": [20.5] * 3, "low": [19.5] * 3, "close": [20.2] * 3, "volume": [200] * 3}, index=idx_b)
    cache = _StubCache({"A.NS": df_a, "B.NS": df_b})

    feed = HistoricalReplayFeed(
        market_adapter=None, start_date="2026-07-16 09:15", end_date="2026-07-16 09:18", speed_multiplier=3600, cache=cache
    )
    received = []
    feed.subscribe(["A.NS", "B.NS"], lambda bar: received.append(bar.symbol))
    feed._thread.join(timeout=5)

    assert received == ["A.NS", "B.NS", "A.NS", "B.NS", "A.NS", "B.NS"]


# -- replay speed -----------------------------------------------------------


def test_higher_speed_multiplier_replays_faster():
    df = _flat_df(periods=3)
    cache = _StubCache({"X.NS": df})

    feed = HistoricalReplayFeed(market_adapter=None, start_date=df.index[0], end_date=df.index[-1], speed_multiplier=6000, cache=cache)
    start = time.monotonic()
    feed.subscribe(["X.NS"], lambda bar: None)
    feed._thread.join(timeout=5)
    elapsed = time.monotonic() - start

    # 2 gaps of 60s at speed_multiplier=6000 -> ~0.02s total; generous bound
    assert elapsed < 1.0


# -- progress tracking -----------------------------------------------------------


def test_progress_reaches_100_percent_on_completion():
    df = _flat_df(periods=5)
    cache = _StubCache({"X.NS": df})

    feed = HistoricalReplayFeed(market_adapter=None, start_date=df.index[0], end_date=df.index[-1], speed_multiplier=6000, cache=cache)
    feed.subscribe(["X.NS"], lambda bar: None)
    feed._thread.join(timeout=5)

    progress = feed.get_progress()
    assert progress["is_running"] is False
    assert progress["progress_pct"] == 100.0
    assert progress["processed_bars"] == progress["total_bars"] == 5


def test_record_trade_updates_progress_stats():
    df = _flat_df(periods=3)
    cache = _StubCache({"X.NS": df})

    feed = HistoricalReplayFeed(market_adapter=None, start_date=df.index[0], end_date=df.index[-1], speed_multiplier=6000, cache=cache)
    feed.subscribe(["X.NS"], lambda bar: feed.record_trade(50.0))
    feed._thread.join(timeout=5)

    progress = feed.get_progress()
    assert progress["trades_so_far"] == 3
    assert progress["pnl_so_far"] == 150.0
    assert progress["win_rate"] == 100.0


def test_stop_interrupts_replay_before_completion():
    df = _flat_df(periods=100, freq="15min")  # long real-time gaps at speed_multiplier=1
    cache = _StubCache({"X.NS": df})

    feed = HistoricalReplayFeed(market_adapter=None, start_date=df.index[0], end_date=df.index[-1], speed_multiplier=1, cache=cache)
    feed.subscribe(["X.NS"], lambda bar: None)
    time.sleep(0.1)
    feed.stop()

    progress = feed.get_progress()
    assert progress["is_running"] is False
    assert progress["processed_bars"] < progress["total_bars"]
