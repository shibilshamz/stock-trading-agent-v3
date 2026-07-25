"""Tests for the run-configuration timeframe selector.

Covers the three pieces added for the dashboard timeframe dropdown:
  - GET /api/markets/{code}/timeframes exposes each market's supported list
  - RunManager._resolve_timeframe defaults + validates against the market
  - BacktestRunner.run forwards a chosen timeframe into the feed config
"""

import pytest
from fastapi.testclient import TestClient

from core.backtest_runner import BacktestRunner
from dashboard.api import RunManager, app


client = TestClient(app)


# -- /api/markets/{code}/timeframes ----------------------------------------


def test_timeframes_endpoint_returns_nse_supported_list():
    resp = client.get("/api/markets/nse/timeframes")
    assert resp.status_code == 200
    tfs = resp.json()
    assert "15m" in tfs and "1m" in tfs and "1d" in tfs


def test_timeframes_endpoint_includes_30m_for_upstox():
    resp = client.get("/api/markets/upstox/timeframes")
    assert resp.status_code == 200
    assert "30m" in resp.json()


def test_timeframes_endpoint_unknown_market_is_404():
    assert client.get("/api/markets/nope/timeframes").status_code == 404


# -- RunManager._resolve_timeframe -----------------------------------------


class _FakeMarket:
    code = "fake"

    def get_supported_timeframes(self):
        return ["1m", "15m", "1d"]


def test_resolve_timeframe_defaults_to_config_when_none():
    mgr = RunManager()
    assert mgr._resolve_timeframe(None, _FakeMarket()) == mgr.config["timeframe"]


def test_resolve_timeframe_passes_supported_value_through():
    mgr = RunManager()
    assert mgr._resolve_timeframe("1m", _FakeMarket()) == "1m"


def test_resolve_timeframe_rejects_unsupported_value():
    mgr = RunManager()
    with pytest.raises(ValueError):
        mgr._resolve_timeframe("5s", _FakeMarket())


# -- BacktestRunner.run forwards timeframe into the feed --------------------


class _CapturingFeed:
    """Stands in for HistoricalBatchFeed to capture the config it's built with."""

    last_config = None

    def __init__(self, market_adapter, cache=None, config=None):
        _CapturingFeed.last_config = config

    def run_backtest(self, strategy, symbols, start_date, end_date):
        return {
            "total_trades": 0,
            "trades": [],
            "strategy_name": strategy.name,
            "strategy_code": strategy.code,
            "parameters": {},
        }

    def save_backtest_result(self, run_id, results, db_path="data/trading_agent.db"):
        pass


def test_run_forwards_timeframe_into_feed_config(monkeypatch, tmp_path):
    monkeypatch.setattr("core.backtest_runner.HistoricalBatchFeed", _CapturingFeed)
    runner = BacktestRunner(db_path=str(tmp_path / "t.db"))
    runner.run("ma_crossover", "nse", ["RELIANCE.NS"], "2026-01-01", "2026-01-02", timeframe="5m")
    assert _CapturingFeed.last_config.get("timeframe") == "5m"


def test_run_without_timeframe_leaves_feed_default(monkeypatch, tmp_path):
    _CapturingFeed.last_config = None
    monkeypatch.setattr("core.backtest_runner.HistoricalBatchFeed", _CapturingFeed)
    runner = BacktestRunner(db_path=str(tmp_path / "t.db"))
    runner.run("ma_crossover", "nse", ["RELIANCE.NS"], "2026-01-01", "2026-01-02")
    # No override -> feed keeps its own "15m" default (key absent from config).
    assert "timeframe" not in _CapturingFeed.last_config