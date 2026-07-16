"""Orchestrates a single backtest run: wires strategy + market + historical
batch feed together, persists results, and builds comparison/report views."""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from core.registry import get_market, get_strategy
from data_feeds.historical_batch import HistoricalBatchFeed

DateLike = Any


class BacktestRunner:
    """Runs a strategy against historical data for a market/symbol set and
    saves the resulting metrics + trades to SQLite."""

    DEFAULT_CONFIG = {
        "reports_dir": "data/reports",
        "feed_config": {},
    }

    def __init__(self, db_path: str = "data/trading_agent.db", cache: Any = None, config: Optional[dict] = None):
        self.db_path = db_path
        self.cache = cache
        self.config = {**self.DEFAULT_CONFIG, **(config or {})}

    def run(
        self,
        strategy_code: str,
        market_code: str,
        symbols: List[str],
        start_date: DateLike,
        end_date: DateLike,
        parameters: Optional[Dict[str, Any]] = None,
    ) -> str:
        strategy = get_strategy(strategy_code)
        market = get_market(market_code)

        # Seeds strategy.config with defaults + user overrides; run_backtest()
        # re-inits the strategy internally with a point-in-time market view but
        # reuses this same config, so parameters flow through either way.
        strategy.on_init(config=parameters or {}, market_adapter=market)

        feed = HistoricalBatchFeed(market_adapter=market, cache=self.cache, config=self.config["feed_config"])
        results = feed.run_backtest(strategy, symbols, start_date, end_date)
        results["market"] = market_code

        run_id = self._generate_run_id(strategy_code, market_code, start_date, end_date)
        feed.save_backtest_result(run_id, results, db_path=self.db_path)
        self._save_trades(run_id, market_code, results)

        return run_id

    def get_results(self, run_id: str) -> Optional[Dict[str, Any]]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM backtest_results WHERE run_id = ?", (run_id,)
            ).fetchone()
        finally:
            conn.close()

        if row is None:
            return None

        result = dict(row)
        result["parameters"] = json.loads(result["parameters"]) if result.get("parameters") else {}
        result["equity_curve"] = json.loads(result["equity_curve"]) if result.get("equity_curve") else []
        return result

    def compare_runs(self, run_ids: List[str]) -> pd.DataFrame:
        rows = []
        for run_id in run_ids:
            result = self.get_results(run_id)
            if result is None:
                continue
            rows.append(
                {
                    "run_id": run_id,
                    "strategy": result.get("strategy_name"),
                    "market": result.get("market"),
                    "start_date": result.get("start_date"),
                    "end_date": result.get("end_date"),
                    "total_trades": result.get("total_trades"),
                    "win_rate": result.get("win_rate"),
                    "gross_pnl": result.get("gross_pnl"),
                    "net_pnl": result.get("net_pnl"),
                    "max_drawdown": result.get("max_drawdown"),
                    "sharpe_ratio": result.get("sharpe_ratio"),
                    "profit_factor": result.get("profit_factor"),
                }
            )
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(rows).set_index("run_id")

    def generate_report(self, run_id: str) -> str:
        results = self.get_results(run_id)
        if results is None:
            raise ValueError(f"No backtest result found for run_id={run_id!r}")

        metrics_df = pd.DataFrame(
            [
                {
                    "run_id": run_id,
                    "strategy_name": results.get("strategy_name"),
                    "strategy_code": results.get("strategy_code"),
                    "market": results.get("market"),
                    "start_date": results.get("start_date"),
                    "end_date": results.get("end_date"),
                    "total_trades": results.get("total_trades"),
                    "winning_trades": results.get("winning_trades"),
                    "losing_trades": results.get("losing_trades"),
                    "gross_pnl": results.get("gross_pnl"),
                    "net_pnl": results.get("net_pnl"),
                    "win_rate": results.get("win_rate"),
                    "max_drawdown": results.get("max_drawdown"),
                    "sharpe_ratio": results.get("sharpe_ratio"),
                    "profit_factor": results.get("profit_factor"),
                    "avg_win": results.get("avg_win"),
                    "avg_loss": results.get("avg_loss"),
                    "largest_win": results.get("largest_win"),
                    "largest_loss": results.get("largest_loss"),
                }
            ]
        )
        trades_df = self._load_trades(run_id)
        equity_df = pd.DataFrame(results.get("equity_curve", []))

        reports_dir = Path(self.config["reports_dir"])
        reports_dir.mkdir(parents=True, exist_ok=True)
        report_path = reports_dir / f"{run_id}.xlsx"

        with pd.ExcelWriter(report_path, engine="openpyxl") as writer:
            metrics_df.to_excel(writer, sheet_name="Metrics", index=False)
            trades_df.to_excel(writer, sheet_name="Trades", index=False)
            if not equity_df.empty:
                equity_df.to_excel(writer, sheet_name="Equity Curve", index=False)

        return str(report_path)

    def _save_trades(self, run_id: str, market_code: str, results: Dict[str, Any]) -> None:
        trades = results.get("trades", [])
        if not trades:
            return

        parameters_json = json.dumps(results.get("parameters", {}), default=str)
        rows = []
        for i, trade in enumerate(trades, start=1):
            entry_time, exit_time = trade["entry_time"], trade["exit_time"]
            duration_minutes = None
            if entry_time is not None and exit_time is not None:
                duration_minutes = round((exit_time - entry_time).total_seconds() / 60, 1)
            rows.append(
                (
                    run_id,
                    f"{run_id}-TRADE-{i:04d}",
                    trade["symbol"],
                    market_code,
                    results.get("strategy_name", ""),
                    results.get("strategy_code", ""),
                    "BUY",
                    trade["entry_price"],
                    trade["exit_price"],
                    trade["quantity"],
                    entry_time.isoformat() if hasattr(entry_time, "isoformat") else entry_time,
                    exit_time.isoformat() if hasattr(exit_time, "isoformat") else exit_time,
                    trade["pnl"],
                    trade["pnl_pct"],
                    "CLOSED",
                    duration_minutes,
                    parameters_json,
                    trade.get("exit_reason"),
                )
            )

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.executemany(
                """INSERT OR REPLACE INTO trades
                   (run_id, trade_id, symbol, market, strategy_name, strategy_code, side,
                    entry_price, exit_price, quantity, entry_time, exit_time, pnl, pnl_pct,
                    status, duration_minutes, parameters, exit_reason)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
            conn.commit()
        finally:
            conn.close()

    def _load_trades(self, run_id: str) -> pd.DataFrame:
        conn = sqlite3.connect(self.db_path)
        try:
            return pd.read_sql_query(
                "SELECT * FROM trades WHERE run_id = ? ORDER BY entry_time", conn, params=(run_id,)
            )
        finally:
            conn.close()

    @staticmethod
    def _generate_run_id(strategy_code: str, market_code: str, start_date: DateLike, end_date: DateLike) -> str:
        start_str = pd.Timestamp(start_date).strftime("%Y%m%d")
        end_str = pd.Timestamp(end_date).strftime("%Y%m%d")
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        return f"backtest_{strategy_code}_{market_code}_{start_str}_{end_str}_{timestamp}"
