"""Unit tests for strategies/orb_vwap.py."""

import pandas as pd

from markets.base import Bar
from strategies.orb_vwap import ORBVWAPStrategy


class _StubAdapter:
    def __init__(self, df):
        self.df = df

    def get_ohlcv(self, symbol, timeframe="15m", bars=100):
        return self.df.tail(bars)


def _uptrend_df():
    idx = pd.date_range("2026-07-16 09:15", periods=25, freq="15min")
    closes = [100 + i * 0.8 for i in range(25)]
    return pd.DataFrame(
        {
            "open": [c - 0.3 for c in closes],
            "high": [c + 0.5 for c in closes],
            "low": [c - 0.5 for c in closes],
            "close": closes,
            "volume": [1500] * 25,
        },
        index=idx,
    )


def _bar_from_last_row(symbol, df):
    row = df.iloc[-1]
    return Bar(
        symbol=symbol,
        timestamp=df.index[-1],
        open=row["open"],
        high=row["high"],
        low=row["low"],
        close=row["close"],
        volume=row["volume"],
        timeframe="15m",
    )


# -- on_bar -----------------------------------------------------------------


def test_on_bar_generates_buy_signal_on_uptrend():
    df = _uptrend_df()
    strategy = ORBVWAPStrategy()
    strategy.on_init(config={}, market_adapter=_StubAdapter(df))

    signal = strategy.on_bar(_bar_from_last_row("TEST.NS", df))

    assert signal is not None
    assert signal.action == "BUY"
    assert signal.confidence >= 0.55
    assert signal.suggested_stop < signal.suggested_target


def test_on_bar_returns_none_on_flat_market():
    idx = pd.date_range("2026-07-16 09:15", periods=25, freq="15min")
    flat_df = pd.DataFrame(
        {"open": [100] * 25, "high": [100.3] * 25, "low": [99.7] * 25, "close": [100] * 25, "volume": [1000] * 25},
        index=idx,
    )
    strategy = ORBVWAPStrategy()
    strategy.on_init(config={}, market_adapter=_StubAdapter(flat_df))

    assert strategy.on_bar(_bar_from_last_row("FLAT.NS", flat_df)) is None


def test_on_bar_records_open_position_on_signal():
    df = _uptrend_df()
    strategy = ORBVWAPStrategy()
    strategy.on_init(config={}, market_adapter=_StubAdapter(df))

    strategy.on_bar(_bar_from_last_row("TEST.NS", df))

    assert "TEST.NS" in strategy.open_positions


# -- on_position_update -----------------------------------------------------


def test_on_position_update_triggers_stop_loss():
    strategy = ORBVWAPStrategy()
    strategy.on_init(config={}, market_adapter=None)
    strategy.open_positions["X.NS"] = {"stop_loss": 95.0, "take_profit": 110.0}

    signal = strategy.on_position_update("X.NS", current_price=94.0, entry_price=100.0, unrealized_pnl=-60.0, position_size=10)

    assert signal is not None
    assert signal.action == "SELL"
    assert signal.reason == "STOP_LOSS"
    assert "X.NS" not in strategy.open_positions


def test_on_position_update_triggers_take_profit():
    strategy = ORBVWAPStrategy()
    strategy.on_init(config={}, market_adapter=None)
    strategy.open_positions["X.NS"] = {"stop_loss": 95.0, "take_profit": 110.0}

    signal = strategy.on_position_update("X.NS", current_price=111.0, entry_price=100.0, unrealized_pnl=110.0, position_size=10)

    assert signal is not None
    assert signal.reason == "TAKE_PROFIT"
    assert "X.NS" not in strategy.open_positions


def test_on_position_update_no_exit_within_bounds():
    strategy = ORBVWAPStrategy()
    strategy.on_init(config={}, market_adapter=None)
    strategy.open_positions["X.NS"] = {"stop_loss": 95.0, "take_profit": 110.0}

    signal = strategy.on_position_update("X.NS", current_price=102.0, entry_price=100.0, unrealized_pnl=20.0, position_size=10)

    assert signal is None
    assert "X.NS" in strategy.open_positions


def test_on_position_update_ignores_untracked_symbol():
    strategy = ORBVWAPStrategy()
    strategy.on_init(config={}, market_adapter=None)

    signal = strategy.on_position_update("UNKNOWN.NS", current_price=50, entry_price=50, unrealized_pnl=0, position_size=10)

    assert signal is None


# -- on_market_close -----------------------------------------------------------


def test_on_market_close_flattens_all_open_positions():
    strategy = ORBVWAPStrategy()
    strategy.on_init(config={}, market_adapter=None)
    strategy.open_positions["A.NS"] = {"stop_loss": 90, "take_profit": 110}
    strategy.open_positions["B.NS"] = {"stop_loss": 90, "take_profit": 110}

    signals = strategy.on_market_close()

    assert len(signals) == 2
    assert all(s.action == "SELL" and s.reason == "MARKET_CLOSE" for s in signals)
    assert strategy.open_positions == {}


def test_on_market_close_returns_empty_list_when_flat():
    strategy = ORBVWAPStrategy()
    strategy.on_init(config={}, market_adapter=None)

    assert strategy.on_market_close() == []
