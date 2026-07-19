"""Paper-trading data feed: polls a market adapter on a schedule and simulates fills."""

import random
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

import pandas as pd
from apscheduler.schedulers.background import BackgroundScheduler

from data_feeds.base import DataFeed
from markets.base import Bar, Order, OrderResult

TRADING_MINUTES_PER_DAY = 375  # NSE session: 09:15-15:30


class PaperFeed(DataFeed):
    """Simulated data feed: polls `market_adapter` for fresh bars on a cron
    schedule and fills orders against the latest price plus random slippage."""

    DEFAULT_CONFIG = {
        "poll_minute": "0,15,30,45",
        "poll_hour": "9-15",
        # NSE hours are inherently IST -- pinning the scheduler's timezone here
        # keeps the poll window correct regardless of the server's own system
        # timezone (e.g. a UTC-default VPS would otherwise poll during IST
        # evening hours instead of the actual 09:15-15:30 IST session).
        "timezone": "Asia/Kolkata",
        "timeframe": "15m",
        "slippage_pct": 0.001,
        "job_id": "paper_feed_poll",
    }

    def __init__(
        self,
        market_adapter: Any = None,
        paper_balance: float = 50000,
        config: Optional[dict] = None,
    ):
        self.market_adapter = market_adapter
        self.paper_balance = paper_balance
        self.config = {**self.DEFAULT_CONFIG, **(config or {})}
        self.portfolio: Dict[str, Any] = {"balance": paper_balance, "positions": {}}
        self.subscribed_symbols: List[str] = []
        self._callback: Optional[Callable[[Bar], None]] = None
        self.scheduler = BackgroundScheduler(timezone=ZoneInfo(self.config["timezone"]))

    @property
    def mode(self) -> str:
        return "paper"

    def subscribe(self, symbols: List[str], callback: Callable[[Bar], None]) -> None:
        self.subscribed_symbols = list(dict.fromkeys([*self.subscribed_symbols, *symbols]))
        self._callback = callback

        if not self.scheduler.running:
            self.scheduler.start()

        self.scheduler.add_job(
            self._fetch_and_push,
            trigger="cron",
            minute=self.config["poll_minute"],
            hour=self.config["poll_hour"],
            args=[self.subscribed_symbols, callback],
            id=self.config["job_id"],
            replace_existing=True,
        )

    def unsubscribe(self, symbols: List[str]) -> None:
        self.subscribed_symbols = [s for s in self.subscribed_symbols if s not in symbols]
        job_id = self.config["job_id"]

        if not self.subscribed_symbols:
            if self.scheduler.get_job(job_id):
                self.scheduler.remove_job(job_id)
        elif self.scheduler.get_job(job_id):
            self.scheduler.modify_job(job_id, args=[self.subscribed_symbols, self._callback])

    def _fetch_and_push(self, symbols: List[str], callback: Callable[[Bar], None]) -> None:
        timeframe = self.config["timeframe"]
        for symbol in symbols:
            try:
                df = self.market_adapter.get_ohlcv(symbol, timeframe, bars=2)
                if df.empty:
                    continue
                latest = df.iloc[-1]
                bar = Bar(
                    symbol=symbol,
                    timestamp=df.index[-1].to_pydatetime(),
                    open=float(latest["open"]),
                    high=float(latest["high"]),
                    low=float(latest["low"]),
                    close=float(latest["close"]),
                    volume=int(latest["volume"]),
                    timeframe=timeframe,
                )
                callback(bar)
            except Exception:
                continue

    def simulate_fill(self, order: Order) -> OrderResult:
        base_price = (
            order.limit_price
            if order.limit_price is not None
            else self.market_adapter.get_latest_price(order.symbol)
        )
        slippage_pct = self.config["slippage_pct"]
        fill_price = round(base_price * (1 + random.uniform(-slippage_pct, slippage_pct)), 2)
        return OrderResult(
            order_id=str(uuid.uuid4()),
            status="FILLED",
            filled_price=fill_price,
            filled_quantity=order.quantity,
            timestamp=datetime.now(),
        )

    def get_historical(
        self, symbol: str, start: datetime, end: datetime, timeframe: str = "15m"
    ) -> pd.DataFrame:
        if self.market_adapter is not None:
            bars = self._estimate_bars(start, end, timeframe)
            df = self.market_adapter.get_ohlcv(symbol, timeframe, bars=bars)
        else:
            df = self._download_yfinance(symbol, start, end, timeframe)

        start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
        if df.index.tz is not None:
            start_ts = start_ts.tz_localize(df.index.tz) if start_ts.tz is None else start_ts
            end_ts = end_ts.tz_localize(df.index.tz) if end_ts.tz is None else end_ts
        return df[(df.index >= start_ts) & (df.index <= end_ts)]

    def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def get_status(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "running": self.scheduler.running,
            "symbols": list(self.subscribed_symbols),
        }

    @staticmethod
    def _download_yfinance(symbol: str, start: datetime, end: datetime, timeframe: str) -> pd.DataFrame:
        import yfinance as yf

        df = yf.download(symbol, start=start, end=end, interval=timeframe, progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]

    @staticmethod
    def _estimate_bars(start: datetime, end: datetime, timeframe: str) -> int:
        span_days = max((end - start).days, 1)
        minutes_per_bar = {"1m": 1, "5m": 5, "15m": 15, "1h": 60}
        if timeframe in minutes_per_bar:
            bars_per_day = TRADING_MINUTES_PER_DAY // minutes_per_bar[timeframe]
            return max(span_days * bars_per_day, 2)
        return max(span_days, 2)
