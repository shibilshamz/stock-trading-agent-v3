"""FastAPI backend for the trading dashboard.

Wires the existing plugin registry, data feeds, risk engines, and backtest
runner together into HTTP endpoints. `RunManager` is the orchestration layer
that doesn't exist elsewhere yet: it drives a single live/paper/replay run's
bar-by-bar loop (entry via strategy.on_bar + risk checks, exit via
strategy.on_position_update) and persists positions/trades to SQLite using
the same schema backtest_runner.py writes to.
"""

import asyncio
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import markets.upstox_auth as upstox_auth
from alerts.telegram_bot import TelegramBot
from core.backtest_runner import BacktestRunner
from core.registry import (
    data_feed_registry,
    get_market,
    get_strategy,
    market_registry,
    strategy_registry,
)
from data.cache import DataCache
from data_feeds.historical_batch import _PointInTimeMarketView
from data_feeds.historical_replay import HistoricalReplayFeed
from markets.base import Bar, Order
from risk.circuit_breakers import CircuitBreakers
from risk.position_sizing import ATRPositionSizing

DASHBOARD_DIR = Path(__file__).resolve().parent
MODES = ["paper", "live", "historical_replay", "backtest"]

logger = logging.getLogger("dashboard")

_notify_loop: Optional[asyncio.AbstractEventLoop] = None


def _build_telegram_bot() -> Optional[TelegramBot]:
    """Constructs a TelegramBot from .env credentials, if present and enabled.

    dashboard/api.py is runnable standalone (`uvicorn dashboard.api:app`),
    not only via main.py's CLI, so it loads its own secrets here rather than
    depending on main.py having already loaded them into some shared state
    this module has no way to reach.
    """
    load_dotenv()
    if os.environ.get("ENABLE_TELEGRAM_ALERTS", "true").strip().lower() not in ("1", "true", "yes"):
        return None
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return None
    try:
        return TelegramBot(token=token, chat_id=chat_id)
    except ValueError as exc:
        logger.warning("Could not initialize Telegram bot: %s", exc)
        return None


def _notify(coro: Any) -> None:
    """Fire-and-forget a TelegramBot coroutine from RunManager's synchronous
    methods. Reuses one persistent event loop across calls rather than
    asyncio.run()'s create-a-loop-then-close-it-every-time -- TelegramBot's
    underlying HTTP client lazily binds internal resources to whichever loop
    first runs a request, so a second asyncio.run() call (a new, different
    loop) breaks with "Event loop is closed" on the next notification. Safe
    to call from multiple RunManager methods since they're all already
    serialized by RunManager's own lock.
    """
    global _notify_loop
    try:
        if _notify_loop is None or _notify_loop.is_closed():
            _notify_loop = asyncio.new_event_loop()
        _notify_loop.run_until_complete(coro)
    except Exception as exc:
        logger.warning("Telegram notification failed: %s", exc)


class RunRequest(BaseModel):
    market: str
    strategy: str
    mode: str
    parameters: Optional[Dict[str, Any]] = None
    symbols: Optional[List[str]] = None
    date_range: Optional[Dict[str, str]] = None
    replay_speed: Optional[float] = 1.0


class RunAlreadyActiveError(Exception):
    pass


class RunNotFoundError(Exception):
    pass


class RunManager:
    """Owns at most one active live/paper/replay run at a time, plus one-shot
    backtests. Positions/trades are tracked in memory during the run and
    mirrored into the `positions`/`trades`/`strategy_runs` tables."""

    DEFAULT_CONFIG = {
        "paper_balance": 50000,
        "default_universe_size": 10,
        "timeframe": "15m",
    }

    def __init__(
        self,
        db_path: str = "data/trading_agent.db",
        cache: Any = None,
        config: Optional[dict] = None,
        risk_config: Optional[dict] = None,
    ):
        self.db_path = db_path
        self.cache = cache if cache is not None else DataCache()
        self.config = {**self.DEFAULT_CONFIG, **(config or {})}
        # Forwarded verbatim to ATRPositionSizing/CircuitBreakers -- both merge
        # this over their own DEFAULT_CONFIG, so a caller (e.g. main.py reading
        # config.yaml) can override risk limits without RunManager needing to
        # know their individual key names.
        self.risk_config = risk_config
        self.telegram_bot = _build_telegram_bot()
        self._lock = threading.RLock()
        self._run: Optional[Dict[str, Any]] = None
        self._backtest_runner = BacktestRunner(db_path=db_path, cache=self.cache)

    # -- run lifecycle ----------------------------------------------------

    def start_run(self, request: RunRequest) -> Dict[str, Any]:
        with self._lock:
            if self._run is not None and self._run.get("status") == "running":
                raise RunAlreadyActiveError(
                    f"Run {self._run['run_id']!r} is already active; stop it first."
                )

            market = get_market(request.market)
            symbols = request.symbols or market.get_universe(top_n=self.config["default_universe_size"])
            parameters = request.parameters or {}

            if request.mode == "backtest":
                return self._run_backtest(request, symbols, parameters)
            if request.mode in ("paper", "live", "historical_replay"):
                return self._start_continuous_run(request, market, symbols, parameters)
            raise ValueError(f"Unsupported mode: {request.mode!r}. Expected one of {MODES}.")

    def stop_run(self, run_id: str) -> Dict[str, Any]:
        with self._lock:
            if self._run is None or self._run["run_id"] != run_id:
                raise RunNotFoundError(f"No active run with run_id={run_id!r}")

            run = self._run
            feed = run.get("feed")
            if feed is not None:
                feed.stop()
            run["status"] = "stopped"
            run["stopped_at"] = datetime.now().isoformat()
            self._finalize_strategy_run(run)
            if self.telegram_bot is not None:
                _notify(self.telegram_bot.send_kill_switch(run_id))
            return self._public_state()

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            if self._run is None:
                return {"status": "idle"}
            return self._public_state()

    def get_positions(self) -> List[Dict[str, Any]]:
        with self._lock:
            if self._run is None:
                return []
            return [
                {"symbol": symbol, **{k: v for k, v in pos.items() if k != "trade_id"}}
                for symbol, pos in self._run.get("positions", {}).items()
            ]

    def get_replay_progress(self) -> Dict[str, Any]:
        with self._lock:
            if self._run is not None and self._run["mode"] == "historical_replay":
                return self._run["feed"].get_progress()
            return {
                "is_running": False,
                "progress_pct": 0.0,
                "current_date": None,
                "total_bars": 0,
                "processed_bars": 0,
                "trades_so_far": 0,
                "pnl_so_far": 0.0,
                "win_rate": 0.0,
                "estimated_completion": None,
            }

    # -- backtest mode ------------------------------------------------------

    def _run_backtest(self, request: RunRequest, symbols: List[str], parameters: Dict[str, Any]) -> Dict[str, Any]:
        date_range = request.date_range or {}
        start_date, end_date = date_range.get("start_date"), date_range.get("end_date")
        if not start_date or not end_date:
            raise ValueError("backtest mode requires date_range.start_date and date_range.end_date")

        run_id = self._backtest_runner.run(
            request.strategy, request.market, symbols, start_date, end_date, parameters=parameters
        )
        results = self._backtest_runner.get_results(run_id) or {}
        self._run = {
            "run_id": run_id,
            "mode": "backtest",
            "status": "completed",
            "market": request.market,
            "strategy_code": request.strategy,
            "symbols": symbols,
            "started_at": datetime.now().isoformat(),
            "completed_at": datetime.now().isoformat(),
            "positions": {},
            "trades": [],
            "summary": {
                "total_trades": results.get("total_trades", 0),
                "winning_trades": results.get("winning_trades", 0),
                "win_rate": results.get("win_rate", 0.0),
                "gross_pnl": results.get("gross_pnl", 0.0),
                "net_pnl": results.get("net_pnl", 0.0),
            },
        }
        return self._public_state()

    # -- paper / live / historical_replay modes ------------------------------

    def _start_continuous_run(
        self, request: RunRequest, market: Any, symbols: List[str], parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        strategy = get_strategy(request.strategy)
        run_id = self._generate_run_id(request.mode, request.strategy, request.market)
        replay_view = None

        if request.mode == "historical_replay":
            date_range = request.date_range or {}
            start_date, end_date = date_range.get("start_date"), date_range.get("end_date")
            if not start_date or not end_date:
                raise ValueError("historical_replay mode requires date_range.start_date and date_range.end_date")

            # A strategy's own get_ohlcv() calls must only see bars up to the
            # replay's simulated "now" -- the same look-ahead constraint
            # historical_batch.py enforces for one-shot backtests. Wiring the
            # real market adapter in here would leak live/future data (or, for
            # symbols that only exist in test fixtures, hit yfinance directly).
            replay_view = self._build_replay_view(symbols, self.config["timeframe"], start_date, end_date, market)
            strategy.on_init(config=parameters, market_adapter=replay_view)

            feed = HistoricalReplayFeed(
                market_adapter=market,
                start_date=start_date,
                end_date=end_date,
                speed_multiplier=request.replay_speed or 1.0,
                cache=self.cache,
            )
        else:  # paper, live -- both execute through the same paper-fill mechanism;
            # there is no live broker integration in this system yet.
            strategy.on_init(config=parameters, market_adapter=market)
            feed = data_feed_registry.create("paper", market_adapter=market, paper_balance=self.config["paper_balance"])

        run = {
            "run_id": run_id,
            "mode": request.mode,
            "status": "running",
            "market": request.market,
            "strategy_code": request.strategy,
            "strategy_name": strategy.name,
            "symbols": symbols,
            "started_at": datetime.now().isoformat(),
            "equity": float(self.config["paper_balance"]),
            "positions": {},
            "trades": [],
            "feed": feed,
            "strategy": strategy,
            "market_adapter": market,
            "replay_view": replay_view,
            # ATRPositionSizing pulls OHLCV for its ATR calc, so it must see the
            # same point-in-time view as the strategy during replay. CircuitBreakers
            # only reads get_market_hours() (a fixed schedule, not time-sensitive
            # price data), so the real adapter is fine there in every mode.
            "risk_engines": [
                ATRPositionSizing(
                    config=self.risk_config, market_adapter=replay_view if replay_view is not None else market
                ),
                CircuitBreakers(config=self.risk_config, market_adapter=market),
            ],
            "parameters": parameters,
        }
        self._run = run

        self._ensure_strategy_run_row(run)
        feed.subscribe(symbols, self._make_bar_callback(run_id))

        if self.telegram_bot is not None:
            _notify(self.telegram_bot.send_message(f"Started {request.mode} run: {run_id}"))

        return self._public_state()

    def _make_bar_callback(self, run_id: str):
        def on_bar(bar: Bar) -> None:
            with self._lock:
                run = self._run
                if run is None or run["run_id"] != run_id or run["status"] != "running":
                    return
                self._process_bar(run, bar)

        return on_bar

    def _process_bar(self, run: Dict[str, Any], bar: Bar) -> None:
        strategy = run["strategy"]
        positions = run["positions"]

        if run.get("replay_view") is not None:
            run["replay_view"].advance(bar.timestamp)

        if bar.symbol in positions:
            pos = positions[bar.symbol]
            unrealized_pnl = (bar.close - pos["entry_price"]) * pos["quantity"]
            exit_signal = strategy.on_position_update(
                bar.symbol, bar.close, pos["entry_price"], unrealized_pnl, pos["quantity"]
            )
            if exit_signal is not None and exit_signal.action == "SELL":
                self._close_position(run, bar.symbol, bar.close, bar.timestamp, exit_signal.reason)

        entry_signal = strategy.on_bar(bar)
        if entry_signal is not None and entry_signal.action == "BUY" and bar.symbol not in positions:
            self._try_open_position(run, bar, entry_signal)

    def _try_open_position(self, run: Dict[str, Any], bar: Bar, signal: Any) -> None:
        portfolio_state = {"balance": run["equity"]}
        probe_order = Order(symbol=bar.symbol, side="BUY", quantity=0, order_type="MARKET")

        quantity: Optional[int] = None
        for engine in run["risk_engines"]:
            result = engine.check_order(probe_order, portfolio_state)
            if not result.approved:
                return
            if result.adjusted_quantity is not None:
                quantity = result.adjusted_quantity

        if not quantity or quantity <= 0:
            return

        fill_price, fill_quantity = self._execute_fill(run, bar.symbol, "BUY", quantity, bar)
        if fill_price is None:
            return

        trade_id = self._insert_open_trade(run, bar.symbol, fill_price, fill_quantity, bar.timestamp, signal)
        run["positions"][bar.symbol] = {
            "entry_price": fill_price,
            "entry_time": bar.timestamp,
            "quantity": fill_quantity,
            "stop_loss": signal.suggested_stop,
            "take_profit": signal.suggested_target,
            "trade_id": trade_id,
        }
        self._insert_position_row(run, bar.symbol)

    def _close_position(self, run: Dict[str, Any], symbol: str, price: float, timestamp: Any, reason: str) -> None:
        pos = run["positions"].pop(symbol, None)
        if pos is None:
            return

        fake_bar_for_fill = Bar(symbol=symbol, timestamp=timestamp, open=price, high=price, low=price, close=price, volume=0)
        fill_price, _ = self._execute_fill(run, symbol, "SELL", pos["quantity"], fake_bar_for_fill)
        exit_price = fill_price if fill_price is not None else price

        pnl = (exit_price - pos["entry_price"]) * pos["quantity"]
        pnl_pct = (exit_price - pos["entry_price"]) / pos["entry_price"] * 100 if pos["entry_price"] else 0.0
        run["equity"] += pnl

        trade = {
            "symbol": symbol,
            "entry_price": pos["entry_price"],
            "exit_price": exit_price,
            "quantity": pos["quantity"],
            "entry_time": pos["entry_time"],
            "exit_time": timestamp,
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 4),
            "exit_reason": reason,
        }
        run["trades"].append(trade)

        for engine in run["risk_engines"]:
            engine.on_trade_closed({"pnl": pnl})
        if run["mode"] == "historical_replay":
            run["feed"].record_trade(pnl)

        self._close_trade_row(pos["trade_id"], trade)
        self._delete_position_row(pos["trade_id"])
        self._update_run_summary(run)

    def _execute_fill(self, run: Dict[str, Any], symbol: str, side: str, quantity: int, bar: Bar):
        """Returns (price, quantity) for a fill. Paper/live route through the
        market adapter's own paper-fill simulation (current price + slippage,
        appropriate for real-time data). Replay mode fills at the replayed
        bar's close -- routing through the real market adapter here would
        price the trade off today's actual market instead of the simulated
        historical moment."""
        if run["mode"] == "historical_replay":
            return bar.close, quantity

        order = Order(symbol=symbol, side=side, quantity=quantity, order_type="MARKET", strategy_name=run["strategy_name"])
        fill = run["market_adapter"].place_order(order)
        if fill.status != "FILLED":
            return None, None
        return fill.filled_price, fill.filled_quantity

    def _build_replay_view(
        self, symbols: List[str], timeframe: str, start_date: Any, end_date: Any, market: Any
    ) -> _PointInTimeMarketView:
        data = {}
        for symbol in symbols:
            if self.cache is not None:
                df = self.cache.get_ohlcv(symbol, timeframe, start_date, end_date)
            else:
                df = market.get_ohlcv(symbol, timeframe, bars=5000)
                df = self._filter_range(df, start_date, end_date)
            if isinstance(df.index, pd.DatetimeIndex) and df.index.tz is not None:
                df = df.copy()
                df.index = df.index.tz_localize(None)
            data[symbol] = df
        return _PointInTimeMarketView(data)

    @staticmethod
    def _filter_range(df: pd.DataFrame, start: Any, end: Any) -> pd.DataFrame:
        start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
        if df.index.tz is not None:
            if start_ts.tz is None:
                start_ts = start_ts.tz_localize(df.index.tz)
            if end_ts.tz is None:
                end_ts = end_ts.tz_localize(df.index.tz)
        return df[(df.index >= start_ts) & (df.index <= end_ts)]

    # -- persistence --------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _ensure_strategy_run_row(self, run: Dict[str, Any]) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """INSERT OR IGNORE INTO strategy_runs
                   (run_id, strategy_name, strategy_code, market, data_mode, status, parameters)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    run["run_id"],
                    run["strategy_name"],
                    run["strategy_code"],
                    run["market"],
                    run["mode"],
                    "running",
                    json.dumps(run.get("parameters", {}), default=str),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _finalize_strategy_run(self, run: Dict[str, Any]) -> None:
        self._update_run_summary(run)
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE strategy_runs SET status = ?, end_time = CURRENT_TIMESTAMP WHERE run_id = ?",
                (run["status"], run["run_id"]),
            )
            conn.commit()
        finally:
            conn.close()

    def _update_run_summary(self, run: Dict[str, Any]) -> None:
        """Persists total/winning/losing trade counts and P&L to the run's
        strategy_runs row. Called after every trade close (not just on stop)
        so the summary reflects reality even if the process restarts mid-run
        -- otherwise a still-"running" row would be stuck showing zeros for
        trades that already closed in a prior process lifetime."""
        trades = run.get("trades", [])
        winners = [t for t in trades if t["pnl"] > 0]
        gross_pnl = round(sum(t["pnl"] for t in trades), 2)
        win_rate = round(len(winners) / len(trades) * 100, 2) if trades else 0.0

        conn = self._connect()
        try:
            conn.execute(
                """UPDATE strategy_runs
                   SET total_trades = ?, winning_trades = ?, losing_trades = ?,
                       gross_pnl = ?, net_pnl = ?, win_rate = ?
                   WHERE run_id = ?""",
                (
                    len(trades),
                    len(winners),
                    len(trades) - len(winners),
                    gross_pnl,
                    gross_pnl,
                    win_rate,
                    run["run_id"],
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _insert_open_trade(
        self, run: Dict[str, Any], symbol: str, fill_price: float, fill_quantity: int, entry_time: Any, signal: Any
    ) -> str:
        trade_id = f"{run['run_id']}-{symbol}-{entry_time.strftime('%Y%m%d%H%M%S') if hasattr(entry_time, 'strftime') else entry_time}"
        conn = self._connect()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO trades
                   (run_id, trade_id, symbol, market, strategy_name, strategy_code, side,
                    entry_price, quantity, entry_time, status, stop_loss, take_profit, parameters)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run["run_id"],
                    trade_id,
                    symbol,
                    run["market"],
                    run["strategy_name"],
                    run["strategy_code"],
                    "BUY",
                    fill_price,
                    fill_quantity,
                    entry_time.isoformat() if hasattr(entry_time, "isoformat") else entry_time,
                    "OPEN",
                    signal.suggested_stop,
                    signal.suggested_target,
                    json.dumps(run.get("parameters", {}), default=str),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return trade_id

    def _close_trade_row(self, trade_id: str, trade: Dict[str, Any]) -> None:
        entry_time, exit_time = trade["entry_time"], trade["exit_time"]
        duration_minutes = None
        if hasattr(entry_time, "isoformat") and hasattr(exit_time, "isoformat"):
            duration_minutes = round((exit_time - entry_time).total_seconds() / 60, 1)

        conn = self._connect()
        try:
            conn.execute(
                """UPDATE trades
                   SET exit_price = ?, exit_time = ?, pnl = ?, pnl_pct = ?, status = 'CLOSED',
                       duration_minutes = ?, exit_reason = ?
                   WHERE trade_id = ?""",
                (
                    trade["exit_price"],
                    exit_time.isoformat() if hasattr(exit_time, "isoformat") else exit_time,
                    trade["pnl"],
                    trade["pnl_pct"],
                    duration_minutes,
                    trade["exit_reason"],
                    trade_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _insert_position_row(self, run: Dict[str, Any], symbol: str) -> None:
        pos = run["positions"][symbol]
        conn = self._connect()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO positions
                   (run_id, trade_id, symbol, market, strategy_name, entry_price, current_price,
                    quantity, stop_loss, take_profit, entry_time)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run["run_id"],
                    pos["trade_id"],
                    symbol,
                    run["market"],
                    run["strategy_name"],
                    pos["entry_price"],
                    pos["entry_price"],
                    pos["quantity"],
                    pos["stop_loss"],
                    pos["take_profit"],
                    pos["entry_time"].isoformat() if hasattr(pos["entry_time"], "isoformat") else pos["entry_time"],
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _delete_position_row(self, trade_id: str) -> None:
        conn = self._connect()
        try:
            conn.execute("DELETE FROM positions WHERE trade_id = ?", (trade_id,))
            conn.commit()
        finally:
            conn.close()

    # -- helpers --------------------------------------------------------

    def _public_state(self) -> Dict[str, Any]:
        run = self._run
        summary = run.get("summary")
        if summary is not None:
            total_trades, win_rate = summary["total_trades"], summary["win_rate"]
        else:
            trades = run.get("trades", [])
            winners = [t for t in trades if t["pnl"] > 0]
            total_trades = len(trades)
            win_rate = round(len(winners) / len(trades) * 100, 2) if trades else 0.0

        state = {
            "run_id": run["run_id"],
            "mode": run["mode"],
            "status": run["status"],
            "market": run.get("market"),
            "strategy": run.get("strategy_code"),
            "symbols": run.get("symbols", []),
            "started_at": run.get("started_at"),
            "equity": round(run["equity"], 2) if "equity" in run else None,
            "open_positions": len(run.get("positions", {})),
            "total_trades": total_trades,
            "win_rate": win_rate,
        }
        if run["mode"] == "historical_replay" and "feed" in run:
            state["replay_progress"] = run["feed"].get_progress()
        return state

    @staticmethod
    def _generate_run_id(mode: str, strategy_code: str, market_code: str) -> str:
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        return f"{mode}_{strategy_code}_{market_code}_{timestamp}"


run_manager = RunManager()

app = FastAPI(title="Trading Agent Dashboard")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

if (DASHBOARD_DIR / "static").exists():
    app.mount("/static", StaticFiles(directory=str(DASHBOARD_DIR / "static")), name="static")


@app.get("/")
def index() -> FileResponse:
    index_path = DASHBOARD_DIR / "templates" / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Dashboard UI not built yet (dashboard/templates/index.html missing).")
    return FileResponse(index_path)


@app.get("/api/markets")
def api_markets() -> List[Dict[str, str]]:
    return [{"code": code, "name": market_registry.create(code).name} for code in market_registry.list_available()]


@app.get("/api/strategies")
def api_strategies() -> List[Dict[str, str]]:
    return [{"code": code, "name": strategy_registry.create(code).name} for code in strategy_registry.list_available()]


@app.get("/api/modes")
def api_modes() -> List[str]:
    return MODES


@app.get("/api/upstox/login")
def upstox_login() -> RedirectResponse:
    load_dotenv()
    client_id = os.environ.get("UPSTOX_API_KEY")
    redirect_uri = os.environ.get("UPSTOX_REDIRECT_URI")
    if not client_id or not redirect_uri:
        raise HTTPException(
            status_code=400,
            detail="UPSTOX_API_KEY / UPSTOX_REDIRECT_URI not configured in .env",
        )
    return RedirectResponse(upstox_auth.build_login_url(client_id, redirect_uri))


@app.get("/api/upstox/callback")
def upstox_callback(code: Optional[str] = Query(None), error: Optional[str] = Query(None)) -> RedirectResponse:
    if error:
        raise HTTPException(status_code=400, detail=f"Upstox login failed: {error}")
    if not code:
        raise HTTPException(status_code=400, detail="Missing 'code' query parameter from Upstox redirect")

    load_dotenv()
    client_id = os.environ.get("UPSTOX_API_KEY")
    client_secret = os.environ.get("UPSTOX_API_SECRET")
    redirect_uri = os.environ.get("UPSTOX_REDIRECT_URI")
    try:
        upstox_auth.exchange_code(code, client_id, client_secret, redirect_uri)
    except upstox_auth.UpstoxAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse("/")


@app.get("/api/upstox/status")
def upstox_status() -> Dict[str, Any]:
    return upstox_auth.get_status()


@app.get("/api/strategies/{code}/parameters")
def api_strategy_parameters(code: str) -> Dict[str, Any]:
    try:
        strategy = strategy_registry.create(code)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No strategy registered under code {code!r}")
    return strategy.get_default_parameters()


@app.post("/api/run")
def api_start_run(request: RunRequest) -> Dict[str, Any]:
    try:
        return run_manager.start_run(request)
    except RunAlreadyActiveError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/stop/{run_id}")
def api_stop_run(run_id: str) -> Dict[str, Any]:
    try:
        return run_manager.stop_run(run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/api/status")
def api_status() -> Dict[str, Any]:
    return run_manager.get_status()


@app.get("/api/positions")
def api_positions() -> List[Dict[str, Any]]:
    return run_manager.get_positions()


@app.get("/api/trades")
def api_trades(start_date: Optional[str] = Query(None), end_date: Optional[str] = Query(None)) -> List[Dict[str, Any]]:
    conditions = ["status = 'CLOSED'"]
    params: List[Any] = []
    if start_date:
        conditions.append("exit_time >= ?")
        params.append(start_date)
    if end_date:
        conditions.append("exit_time <= ?")
        params.append(end_date)

    conn = sqlite3.connect(run_manager.db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"SELECT * FROM trades WHERE {' AND '.join(conditions)} ORDER BY exit_time DESC", params
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


@app.get("/api/report")
def api_report(start_date: Optional[str] = Query(None), end_date: Optional[str] = Query(None)) -> FileResponse:
    from dashboard.reports import generate_excel_report  # deferred: reports.py lands in Step 2

    report_path = generate_excel_report(db_path=run_manager.db_path, start_date=start_date, end_date=end_date)
    return FileResponse(
        report_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=Path(report_path).name,
    )


@app.get("/api/backtest/results")
def api_backtest_results(limit: int = Query(50, ge=1, le=500)) -> List[Dict[str, Any]]:
    conn = sqlite3.connect(run_manager.db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM backtest_results ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    finally:
        conn.close()

    results = []
    for row in rows:
        result = dict(row)
        result["parameters"] = json.loads(result["parameters"]) if result.get("parameters") else {}
        result["equity_curve"] = json.loads(result["equity_curve"]) if result.get("equity_curve") else []
        results.append(result)
    return results


@app.get("/api/replay/progress")
def api_replay_progress() -> Dict[str, Any]:
    return run_manager.get_replay_progress()
