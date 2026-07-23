"""Unit tests for portfolio-management behavior.

There is no standalone portfolio_manager.py module in this codebase --
create-run / record-trade / close-trade / get-open-positions logic lives in
dashboard/api.py's RunManager (the orchestration layer built in Phase 4),
persisted through the same trades/positions/strategy_runs tables a
portfolio manager would use. These tests exercise RunManager's equivalents:
_ensure_strategy_run_row ("create_run"), _try_open_position ("record_trade"),
_close_position ("close_trade"), and get_positions ("get_open_positions").
"""

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from dashboard.api import RunManager
from markets.base import Bar, OrderResult
from risk.base import RiskCheckResult
from strategies.base import Signal

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "database" / "schema.sql"


class _StubMarketAdapter:
    def place_order(self, order):
        price = 100.0 if order.side == "BUY" else 110.0
        return OrderResult(order_id="x", status="FILLED", filled_price=price, filled_quantity=order.quantity, timestamp=datetime.now())


class _ApprovingRiskEngine:
    name = "stub-risk"

    def check_order(self, order, portfolio_state):
        return RiskCheckResult(approved=True, adjusted_quantity=10)

    def on_trade_closed(self, trade_data):
        pass


@pytest.fixture
def temp_db(tmp_path):
    db_path = tmp_path / "test_trading_agent.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()
    conn.close()
    return str(db_path)


@pytest.fixture
def run_manager(temp_db):
    return RunManager(db_path=temp_db)


@pytest.fixture
def bare_run(run_manager):
    run = {
        "run_id": "test-run",
        "mode": "paper",
        "status": "running",
        "market": "nse",
        "strategy_code": "orb_vwap",
        "strategy_name": "ORB + VWAP + Momentum",
        "symbols": ["X.NS"],
        "equity": 50000.0,
        "positions": {},
        "trades": [],
        "market_adapter": _StubMarketAdapter(),
        "risk_engines": [_ApprovingRiskEngine()],
        "parameters": {},
    }
    run_manager._ensure_strategy_run_row(run)  # "create_run"
    return run_manager, run


def _open_position(run_manager, run):
    bar = Bar(symbol="X.NS", timestamp=datetime(2026, 7, 17, 10, 0), open=99, high=101, low=98, close=100, volume=1000)
    signal = Signal(symbol="X.NS", action="BUY", confidence=0.8, reason="test", suggested_stop=95, suggested_target=110)
    run_manager._try_open_position(run, bar, signal)


# -- create_run -----------------------------------------------------------


def test_create_run_inserts_strategy_runs_row(temp_db, bare_run):
    run_manager, run = bare_run
    conn = sqlite3.connect(temp_db)
    row = conn.execute("SELECT * FROM strategy_runs WHERE run_id = ?", (run["run_id"],)).fetchone()
    conn.close()
    assert row is not None


# -- record_trade (open) -----------------------------------------------------------


def test_record_trade_opens_position_and_trade_row(temp_db, bare_run):
    run_manager, run = bare_run
    _open_position(run_manager, run)

    assert "X.NS" in run["positions"]
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    trade = conn.execute("SELECT * FROM trades WHERE run_id = ?", (run["run_id"],)).fetchone()
    position = conn.execute("SELECT * FROM positions WHERE run_id = ?", (run["run_id"],)).fetchone()
    conn.close()

    assert trade["status"] == "OPEN"
    assert trade["entry_price"] == 100.0
    assert position is not None
    assert position["quantity"] == 10


def test_record_trade_rejected_by_risk_engine_opens_nothing(temp_db, bare_run):
    run_manager, run = bare_run

    class _RejectingRiskEngine:
        name = "reject"

        def check_order(self, order, portfolio_state):
            return RiskCheckResult(approved=False, reject_reason="blocked")

        def on_trade_closed(self, trade_data):
            pass

    run["risk_engines"] = [_RejectingRiskEngine()]
    _open_position(run_manager, run)

    assert run["positions"] == {}


# -- close_trade -----------------------------------------------------------


def test_close_trade_updates_row_and_removes_position(temp_db, bare_run):
    run_manager, run = bare_run
    _open_position(run_manager, run)

    run_manager._close_position(run, "X.NS", 110.0, datetime(2026, 7, 17, 11, 0), "TAKE_PROFIT")

    assert "X.NS" not in run["positions"]
    assert run["trades"][0]["pnl"] == 100.0  # (110 - 100) * 10

    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    trade = conn.execute("SELECT * FROM trades WHERE run_id = ?", (run["run_id"],)).fetchone()
    position = conn.execute("SELECT * FROM positions WHERE run_id = ?", (run["run_id"],)).fetchone()
    conn.close()

    assert trade["status"] == "CLOSED"
    assert trade["exit_price"] == 110.0
    assert trade["exit_reason"] == "TAKE_PROFIT"
    assert position is None


def test_close_trade_updates_strategy_runs_summary(temp_db, bare_run):
    run_manager, run = bare_run
    _open_position(run_manager, run)

    run_manager._close_position(run, "X.NS", 110.0, datetime(2026, 7, 17, 11, 0), "TAKE_PROFIT")

    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM strategy_runs WHERE run_id = ?", (run["run_id"],)).fetchone()
    conn.close()

    # Summary must be persisted as trades close, not only when the run stops --
    # otherwise a still-"running" row stays stuck at zero if the process restarts.
    assert row["status"] == "running"
    assert row["total_trades"] == 1
    assert row["winning_trades"] == 1
    assert row["losing_trades"] == 0
    assert row["net_pnl"] == 100.0
    assert row["win_rate"] == 100.0


# -- get_open_positions -----------------------------------------------------------


def test_get_open_positions_reflects_in_memory_state(bare_run):
    run_manager, run = bare_run
    _open_position(run_manager, run)
    run_manager._run = run

    positions = run_manager.get_positions()

    assert len(positions) == 1
    assert positions[0]["symbol"] == "X.NS"
    assert positions[0]["entry_price"] == 100.0
    assert "trade_id" not in positions[0]  # internal bookkeeping shouldn't leak


def test_get_open_positions_empty_when_no_active_run(temp_db):
    run_manager = RunManager(db_path=temp_db)
    assert run_manager.get_positions() == []
