"""Unit tests for risk/position_sizing.py and risk/circuit_breakers.py."""

from markets.base import Order
from risk.circuit_breakers import CircuitBreakers
from risk.position_sizing import ATRPositionSizing


# -- ATR position sizing -----------------------------------------------------------


def test_atr_sizing_computes_and_clamps_position_size():
    engine = ATRPositionSizing()
    order = Order(symbol="X.NS", side="BUY", quantity=0, order_type="LIMIT", limit_price=100.0)

    result = engine.check_order(order, {"balance": 50000, "atr": 2.0})

    # risk_amount = 50000*0.01 = 500; stop_distance = 2*1.5 = 3; raw = 166
    # position_value = 166*100 = 16600 > max_position_value (5000) -> clamp to 50
    assert result.approved is True
    assert result.adjusted_quantity == 50


def test_atr_sizing_no_clamp_needed_for_wide_atr():
    engine = ATRPositionSizing()
    order = Order(symbol="X.NS", side="BUY", quantity=0, order_type="LIMIT", limit_price=100.0)

    result = engine.check_order(order, {"balance": 50000, "atr": 50.0})

    # risk_amount = 500; stop_distance = 75; raw = 6; value = 600 < 5000 -> no clamp
    assert result.approved is True
    assert result.adjusted_quantity == 6


def test_atr_sizing_rejects_when_atr_unavailable():
    engine = ATRPositionSizing()
    order = Order(symbol="X.NS", side="BUY", quantity=0, order_type="MARKET")

    result = engine.check_order(order, {"balance": 50000})

    assert result.approved is False
    assert result.reject_reason is not None


def test_atr_sizing_uses_default_balance_when_missing():
    engine = ATRPositionSizing()
    order = Order(symbol="X.NS", side="BUY", quantity=0, order_type="LIMIT", limit_price=100.0)

    result = engine.check_order(order, {"atr": 2.0})

    assert result.approved is True
    assert result.adjusted_quantity == 50  # same as default_balance=50000 case


# -- circuit breakers -----------------------------------------------------------


def test_circuit_breaker_blocks_after_daily_loss_limit():
    engine = CircuitBreakers(config={"market_close": "01:00"})
    engine.daily_pnl = -1600  # breaches -3% of 50000 (-1500)

    result = engine.check_order(Order(symbol="X.NS", side="BUY", quantity=10, order_type="MARKET"), {"balance": 50000})

    assert result.approved is False
    assert result.reject_reason == "Daily loss limit reached"


def test_circuit_breaker_blocks_after_max_open_positions():
    engine = CircuitBreakers(config={"market_close": "01:00"})
    engine.open_positions = 5

    result = engine.check_order(Order(symbol="X.NS", side="BUY", quantity=10, order_type="MARKET"), {"balance": 50000})

    assert result.approved is False
    assert result.reject_reason == "Max open positions reached"


def test_circuit_breaker_approves_normal_order_and_tracks_position():
    engine = CircuitBreakers(config={"market_close": "01:00"})

    result = engine.check_order(Order(symbol="X.NS", side="BUY", quantity=10, order_type="MARKET"), {"balance": 50000})

    assert result.approved is True
    assert engine.open_positions == 1


def test_circuit_breaker_sell_order_does_not_increment_positions():
    engine = CircuitBreakers(config={"market_close": "01:00"})

    engine.check_order(Order(symbol="X.NS", side="SELL", quantity=10, order_type="MARKET"), {"balance": 50000})

    assert engine.open_positions == 0


def test_circuit_breaker_on_trade_closed_updates_state():
    engine = CircuitBreakers()
    engine.open_positions = 1

    engine.on_trade_closed({"pnl": -250})

    assert engine.daily_pnl == -250
    assert engine.daily_trades == 1
    assert engine.open_positions == 0


def test_circuit_breaker_reset_daily_clears_all_counters():
    engine = CircuitBreakers()
    engine.daily_pnl = -500
    engine.daily_trades = 3
    engine.open_positions = 2

    engine.reset_daily()

    assert engine.daily_pnl == 0.0
    assert engine.daily_trades == 0
    assert engine.open_positions == 0
