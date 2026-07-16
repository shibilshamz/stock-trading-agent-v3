"""Historical batch data feed: runs a strategy over a historical date range in
one pass, feeding it the exact same Bar/Signal lifecycle a live session would."""

import json
import math
import sqlite3
import statistics
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd

from data_feeds.base import DataFeed
from markets.base import Bar

DateLike = Any


class _PointInTimeMarketView:
    """OHLCV view that only exposes bars up to a movable 'now' cursor.

    A live strategy's `market_adapter.get_ohlcv(...)` naturally can't see the
    future -- time just hasn't happened yet. Feeding it a real market_adapter
    during a backtest would leak future bars into every indicator calculation
    (look-ahead bias) and would also hit yfinance for "latest" data instead of
    the simulated point in time. This class recreates that same "can't see
    ahead" constraint so orb_vwap.py's own get_ohlcv calls stay honest.
    """

    def __init__(self, data: Dict[str, pd.DataFrame]):
        self._data = {symbol: df for symbol, df in data.items()}
        self._now: Optional[pd.Timestamp] = None

    def advance(self, timestamp: DateLike) -> None:
        ts = pd.Timestamp(timestamp)
        self._now = ts.tz_localize(None) if ts.tz is not None else ts

    def get_ohlcv(self, symbol: str, timeframe: str = "15m", bars: int = 100) -> pd.DataFrame:
        df = self._data.get(symbol)
        if df is None or df.empty or self._now is None:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        return df[df.index <= self._now].tail(bars)

    def get_latest_price(self, symbol: str) -> float:
        df = self._data.get(symbol)
        if df is None or df.empty or self._now is None:
            return 0.0
        visible = df[df.index <= self._now]
        return float(visible["close"].iloc[-1]) if not visible.empty else 0.0


class HistoricalBatchFeed(DataFeed):
    """Runs a strategy once, start to finish, over historical bars and returns
    aggregate performance metrics -- no callback pacing, no live subscription."""

    DEFAULT_CONFIG = {
        "timeframe": "15m",
        "starting_balance": 50000,
        "position_size_pct": 0.10,
        "fallback_bars": 5000,
    }

    def __init__(self, market_adapter: Any, cache: Any = None, config: Optional[dict] = None):
        self.market_adapter = market_adapter
        self.cache = cache
        self.config = {**self.DEFAULT_CONFIG, **(config or {})}
        self._last_run: Optional[Dict[str, Any]] = None

    @property
    def mode(self) -> str:
        return "historical_batch"

    def subscribe(self, symbols, callback) -> None:
        raise NotImplementedError(
            "HistoricalBatchFeed is batch-only; call run_backtest(...) instead of subscribe()."
        )

    def unsubscribe(self, symbols) -> None:
        raise NotImplementedError(
            "HistoricalBatchFeed is batch-only; there is no live subscription to cancel."
        )

    def run_backtest(
        self, strategy: Any, symbols: List[str], start_date: DateLike, end_date: DateLike
    ) -> Dict[str, Any]:
        timeframe = self.config["timeframe"]
        preloaded = {
            symbol: self._strip_tz(self._load_symbol_data(symbol, timeframe, start_date, end_date))
            for symbol in symbols
        }

        market_view = _PointInTimeMarketView(preloaded)
        strategy_config = getattr(strategy, "config", None) or strategy.get_default_parameters()
        strategy.on_init(config=strategy_config, market_adapter=market_view)

        bars = self._sort_bars(symbols, preloaded, timeframe)

        equity = float(self.config["starting_balance"])
        positions: Dict[str, Dict[str, Any]] = {}
        trades: List[Dict[str, Any]] = []
        equity_curve: List[Dict[str, Any]] = []
        last_price: Dict[str, float] = {}
        last_seen_date = None

        def close_position(symbol: str, exit_price: float, exit_time: Any, reason: str) -> None:
            nonlocal equity
            pos = positions.pop(symbol, None)
            if pos is None:
                return
            pnl = (exit_price - pos["entry_price"]) * pos["quantity"]
            equity += pnl
            trades.append(
                {
                    "symbol": symbol,
                    "entry_price": pos["entry_price"],
                    "exit_price": exit_price,
                    "quantity": pos["quantity"],
                    "entry_time": pos["entry_time"],
                    "exit_time": exit_time,
                    "pnl": round(pnl, 2),
                    "pnl_pct": (
                        round((exit_price - pos["entry_price"]) / pos["entry_price"] * 100, 4)
                        if pos["entry_price"]
                        else 0.0
                    ),
                    "exit_reason": reason,
                }
            )

        last_bar_timestamp = None
        for bar in bars:
            bar_date = bar.timestamp.date()
            if last_seen_date is not None and bar_date != last_seen_date:
                for signal in strategy.on_market_close():
                    close_position(signal.symbol, last_price.get(signal.symbol, 0.0), last_bar_timestamp, signal.reason)
                equity_curve.append({"date": last_seen_date.isoformat(), "equity": round(equity, 2)})
            last_seen_date = bar_date

            market_view.advance(bar.timestamp)
            last_price[bar.symbol] = bar.close

            entry_signal = strategy.on_bar(bar)
            if entry_signal is not None and entry_signal.action == "BUY" and bar.symbol not in positions:
                self._open_position(positions, bar.symbol, bar.close, bar.timestamp, equity)

            if bar.symbol in positions:
                pos = positions[bar.symbol]
                unrealized_pnl = (bar.close - pos["entry_price"]) * pos["quantity"]
                exit_signal = strategy.on_position_update(
                    bar.symbol, bar.close, pos["entry_price"], unrealized_pnl, pos["quantity"]
                )
                if exit_signal is not None and exit_signal.action == "SELL":
                    close_position(bar.symbol, bar.close, bar.timestamp, exit_signal.reason)

            last_bar_timestamp = bar.timestamp

        last_bar_time = last_bar_timestamp
        for signal in strategy.on_market_close():
            close_position(signal.symbol, last_price.get(signal.symbol, 0.0), last_bar_time, signal.reason)
        for symbol in list(positions.keys()):
            close_position(symbol, last_price.get(symbol, positions[symbol]["entry_price"]), last_bar_time, "END_OF_BACKTEST")
        if last_seen_date is not None:
            equity_curve.append({"date": last_seen_date.isoformat(), "equity": round(equity, 2)})

        metrics = self._calculate_metrics(trades, equity_curve)
        results = {
            **metrics,
            "trades": trades,
            "strategy_name": strategy.name,
            "strategy_code": strategy.code,
            "symbols": list(symbols),
            "start_date": str(start_date),
            "end_date": str(end_date),
            "parameters": strategy_config,
            "starting_balance": self.config["starting_balance"],
            "ending_balance": round(equity, 2),
        }

        self._last_run = {
            "strategy_code": strategy.code,
            "symbols": list(symbols),
            "start_date": str(start_date),
            "end_date": str(end_date),
            "timestamp": datetime.now().isoformat(),
            "total_trades": metrics["total_trades"],
        }

        return results

    def save_backtest_result(
        self, run_id: str, results: Dict[str, Any], db_path: str = "data/trading_agent.db"
    ) -> None:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            # backtest_results.run_id has a FK against strategy_runs.run_id; ensure a
            # row exists there first (OR IGNORE so a fuller row inserted upstream,
            # e.g. by BacktestRunner, is left untouched).
            self._ensure_strategy_run(conn, run_id, results)
            conn.execute(
                """INSERT OR REPLACE INTO backtest_results
                   (run_id, strategy_name, strategy_code, market, start_date, end_date,
                    total_trades, winning_trades, losing_trades, gross_pnl, net_pnl,
                    win_rate, max_drawdown, sharpe_ratio, profit_factor,
                    avg_win, avg_loss, largest_win, largest_loss, parameters, equity_curve)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    results.get("strategy_name", ""),
                    results.get("strategy_code", ""),
                    results.get("market", "unknown"),
                    results.get("start_date", ""),
                    results.get("end_date", ""),
                    results.get("total_trades", 0),
                    results.get("winning_trades", 0),
                    results.get("losing_trades", 0),
                    results.get("gross_pnl", 0.0),
                    results.get("net_pnl", 0.0),
                    results.get("win_rate", 0.0),
                    results.get("max_drawdown", 0.0),
                    results.get("sharpe_ratio", 0.0),
                    results.get("profit_factor", 0.0),
                    results.get("avg_win", 0.0),
                    results.get("avg_loss", 0.0),
                    results.get("largest_win", 0.0),
                    results.get("largest_loss", 0.0),
                    json.dumps(results.get("parameters", {}), default=str),
                    json.dumps(results.get("equity_curve", [])),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _ensure_strategy_run(conn: sqlite3.Connection, run_id: str, results: Dict[str, Any]) -> None:
        conn.execute(
            """INSERT OR IGNORE INTO strategy_runs
               (run_id, strategy_name, strategy_code, market, data_mode, status,
                parameters, backtest_start_date, backtest_end_date,
                total_trades, winning_trades, losing_trades, gross_pnl, net_pnl, win_rate)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id,
                results.get("strategy_name", ""),
                results.get("strategy_code", ""),
                results.get("market", "unknown"),
                "backtest",
                "completed",
                json.dumps(results.get("parameters", {}), default=str),
                results.get("start_date", ""),
                results.get("end_date", ""),
                results.get("total_trades", 0),
                results.get("winning_trades", 0),
                results.get("losing_trades", 0),
                results.get("gross_pnl", 0.0),
                results.get("net_pnl", 0.0),
                results.get("win_rate", 0.0),
            ),
        )

    def get_historical(self, symbol: str, start: DateLike, end: DateLike, timeframe: str = "15m") -> pd.DataFrame:
        if self.cache is not None:
            return self.cache.get_ohlcv(symbol, timeframe, start, end)
        df = self.market_adapter.get_ohlcv(symbol, timeframe, bars=self.config["fallback_bars"])
        return self._filter_range(df, start, end)

    def stop(self) -> None:
        pass

    def get_status(self) -> Dict[str, Any]:
        return {"mode": self.mode, "last_run": self._last_run}

    def _open_position(
        self, positions: Dict[str, Dict[str, Any]], symbol: str, price: float, timestamp: Any, equity: float
    ) -> None:
        if price <= 0:
            return
        quantity = int((equity * self.config["position_size_pct"]) / price)
        if quantity <= 0:
            return
        positions[symbol] = {"entry_price": price, "entry_time": timestamp, "quantity": quantity}

    def _load_symbol_data(self, symbol: str, timeframe: str, start_date: DateLike, end_date: DateLike) -> pd.DataFrame:
        if self.cache is not None:
            return self.cache.get_ohlcv(symbol, timeframe, start_date, end_date)
        df = self.market_adapter.get_ohlcv(symbol, timeframe, bars=self.config["fallback_bars"])
        return self._filter_range(df, start_date, end_date)

    @staticmethod
    def _sort_bars(symbols: List[str], preloaded: Dict[str, pd.DataFrame], timeframe: str) -> List[Bar]:
        bars: List[Bar] = []
        for symbol in symbols:
            df = preloaded[symbol]
            for ts, row in df.iterrows():
                bars.append(
                    Bar(
                        symbol=symbol,
                        timestamp=ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts,
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=int(row["volume"]),
                        timeframe=timeframe,
                    )
                )
        bars.sort(key=lambda b: b.timestamp)
        return bars

    @staticmethod
    def _calculate_metrics(trades: List[Dict[str, Any]], equity_curve: List[Dict[str, Any]]) -> Dict[str, Any]:
        total_trades = len(trades)
        winners = [t for t in trades if t["pnl"] > 0]
        losers = [t for t in trades if t["pnl"] <= 0]
        gross_profit = sum(t["pnl"] for t in winners)
        gross_loss = sum(t["pnl"] for t in losers)
        gross_pnl = round(gross_profit + gross_loss, 2)

        return {
            "total_trades": total_trades,
            "winning_trades": len(winners),
            "losing_trades": len(losers),
            "gross_pnl": gross_pnl,
            "net_pnl": gross_pnl,  # no commission/slippage model in this engine
            "win_rate": round(len(winners) / total_trades * 100, 2) if total_trades else 0.0,
            "max_drawdown": HistoricalBatchFeed._max_drawdown(equity_curve),
            "sharpe_ratio": HistoricalBatchFeed._sharpe_ratio(equity_curve),
            "profit_factor": HistoricalBatchFeed._profit_factor(gross_profit, gross_loss),
            "avg_win": round(gross_profit / len(winners), 2) if winners else 0.0,
            "avg_loss": round(gross_loss / len(losers), 2) if losers else 0.0,
            "largest_win": round(max((t["pnl"] for t in winners), default=0.0), 2),
            "largest_loss": round(min((t["pnl"] for t in losers), default=0.0), 2),
            "equity_curve": equity_curve,
        }

    @staticmethod
    def _profit_factor(gross_profit: float, gross_loss: float) -> float:
        if gross_loss == 0:
            return round(gross_profit, 4) if gross_profit > 0 else 0.0
        return round(gross_profit / abs(gross_loss), 4)

    @staticmethod
    def _max_drawdown(equity_curve: List[Dict[str, Any]]) -> float:
        if not equity_curve:
            return 0.0
        peak = equity_curve[0]["equity"]
        max_dd = 0.0
        for point in equity_curve:
            peak = max(peak, point["equity"])
            if peak > 0:
                max_dd = max(max_dd, (peak - point["equity"]) / peak * 100)
        return round(max_dd, 2)

    @staticmethod
    def _sharpe_ratio(equity_curve: List[Dict[str, Any]]) -> float:
        if len(equity_curve) < 2:
            return 0.0
        equities = [p["equity"] for p in equity_curve]
        returns = [
            (equities[i] - equities[i - 1]) / equities[i - 1]
            for i in range(1, len(equities))
            if equities[i - 1] != 0
        ]
        if len(returns) < 2:
            return 0.0
        mean_r = statistics.mean(returns)
        std_r = statistics.stdev(returns)
        if std_r == 0:
            return 0.0
        return round((mean_r / std_r) * math.sqrt(252), 4)

    @staticmethod
    def _strip_tz(df: pd.DataFrame) -> pd.DataFrame:
        if isinstance(df.index, pd.DatetimeIndex) and df.index.tz is not None:
            df = df.copy()
            df.index = df.index.tz_localize(None)
        return df

    @staticmethod
    def _filter_range(df: pd.DataFrame, start: DateLike, end: DateLike) -> pd.DataFrame:
        start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
        if df.index.tz is not None:
            if start_ts.tz is None:
                start_ts = start_ts.tz_localize(df.index.tz)
            if end_ts.tz is None:
                end_ts = end_ts.tz_localize(df.index.tz)
        return df[(df.index >= start_ts) & (df.index <= end_ts)]
