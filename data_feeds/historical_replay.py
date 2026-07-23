"""Historical replay data feed: pushes past bars to a callback at wall-clock
pace (scaled by `speed_multiplier`), so live strategy code can run unmodified
against historical data."""

import threading
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

from data_feeds.base import DataFeed
from markets.base import Bar

DateLike = Any


class HistoricalReplayFeed(DataFeed):
    """Loads and chronologically sorts historical bars for a symbol set, then
    replays them to a callback in a background thread at scaled real time."""

    DEFAULT_CONFIG = {
        "timeframe": "15m",
        "fallback_bars": 5000,
        "max_gap_seconds": None,  # None => sleep the full inter-bar gap (spec default)
        "stop_join_timeout": 5,
    }

    def __init__(
        self,
        market_adapter: Any,
        start_date: DateLike,
        end_date: DateLike,
        speed_multiplier: float = 1.0,
        cache: Any = None,
        config: Optional[dict] = None,
    ):
        if pd.Timestamp(start_date) >= pd.Timestamp(end_date):
            raise ValueError("start_date must be before end_date")

        self.market_adapter = market_adapter
        self.start_date = start_date
        self.end_date = end_date
        self.speed_multiplier = speed_multiplier
        self.cache = cache
        self.config = {**self.DEFAULT_CONFIG, **(config or {})}

        self._bars: List[Bar] = []
        self._symbols: List[str] = []
        self._callback: Optional[Callable[[Bar], None]] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._is_running = False
        self._processed_bars = 0
        self._start_time: Optional[datetime] = None

        self._trades_so_far = 0
        self._wins = 0
        self._pnl_so_far = 0.0

    @property
    def mode(self) -> str:
        return "historical_replay"

    def subscribe(self, symbols: List[str], callback: Callable[[Bar], None]) -> None:
        self._symbols = list(symbols)
        self._callback = callback
        self._bars = self._load_and_sort_bars(self._symbols)

        self._stop_event.clear()
        self._processed_bars = 0
        self._trades_so_far = 0
        self._wins = 0
        self._pnl_so_far = 0.0
        self._is_running = True
        self._start_time = datetime.now()

        self._thread = threading.Thread(target=self._replay_loop, daemon=True)
        self._thread.start()

    def unsubscribe(self, symbols: List[str]) -> None:
        self._symbols = [s for s in self._symbols if s not in symbols]

    def record_trade(self, pnl: float) -> None:
        """Lets the replay's caller report a closed trade's P&L back into
        get_progress()'s running stats -- the feed itself only ever pushes
        bars, it doesn't know what the strategy/portfolio did with them."""
        self._trades_so_far += 1
        self._pnl_so_far += pnl
        if pnl > 0:
            self._wins += 1

    def _replay_loop(self) -> None:
        max_gap = self.config["max_gap_seconds"]

        for i, bar in enumerate(self._bars):
            if self._stop_event.is_set():
                break

            if i > 0:
                elapsed = (bar.timestamp - self._bars[i - 1].timestamp).total_seconds()
                sleep_time = max(elapsed, 0) / self.speed_multiplier
                if max_gap is not None:
                    sleep_time = min(sleep_time, max_gap)
                if sleep_time > 0 and self._stop_event.wait(sleep_time):
                    break

            self._callback(bar)
            self._processed_bars += 1

        self._is_running = False

    def get_progress(self) -> Dict[str, Any]:
        total = len(self._bars)
        processed = self._processed_bars
        progress_pct = round(processed / total * 100, 2) if total > 0 else 0.0
        current_date = (
            self._bars[processed - 1].timestamp.date().isoformat() if 0 < processed <= total else None
        )
        win_rate = round(self._wins / self._trades_so_far * 100, 2) if self._trades_so_far > 0 else 0.0

        return {
            "is_running": self._is_running,
            "progress_pct": progress_pct,
            "current_date": current_date,
            "total_bars": total,
            "processed_bars": processed,
            "trades_so_far": self._trades_so_far,
            "pnl_so_far": self._pnl_so_far,
            "win_rate": win_rate,
            "estimated_completion": self._estimate_completion(),
        }

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self.config["stop_join_timeout"])
        self._is_running = False

    def get_historical(
        self, symbol: str, start: DateLike, end: DateLike, timeframe: str = "15m"
    ) -> pd.DataFrame:
        if self.cache is not None:
            return self.cache.get_ohlcv(symbol, timeframe, start, end, market_adapter=self.market_adapter)
        df = self.market_adapter.get_ohlcv(symbol, timeframe, bars=self.config["fallback_bars"])
        return self._filter_range(df, start, end)

    def get_status(self) -> Dict[str, Any]:
        return {"mode": self.mode, **self.get_progress()}

    def _load_and_sort_bars(self, symbols: List[str]) -> List[Bar]:
        timeframe = self.config["timeframe"]
        bars: List[Bar] = []
        for symbol in symbols:
            df = self._fetch_symbol_history(symbol, timeframe)
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

    def _fetch_symbol_history(self, symbol: str, timeframe: str) -> pd.DataFrame:
        if self.cache is not None:
            return self.cache.get_ohlcv(
                symbol, timeframe, self.start_date, self.end_date, market_adapter=self.market_adapter
            )
        df = self.market_adapter.get_ohlcv(symbol, timeframe, bars=self.config["fallback_bars"])
        return self._filter_range(df, self.start_date, self.end_date)

    def _estimate_completion(self) -> Optional[str]:
        if self._start_time is None or self._processed_bars == 0:
            return None
        if not self._is_running:
            return "completed"

        elapsed_real = (datetime.now() - self._start_time).total_seconds()
        total = len(self._bars)
        remaining_bars = total - self._processed_bars
        seconds_per_bar = elapsed_real / self._processed_bars
        eta = datetime.now() + timedelta(seconds=seconds_per_bar * remaining_bars)
        return eta.isoformat()

    @staticmethod
    def _filter_range(df: pd.DataFrame, start: DateLike, end: DateLike) -> pd.DataFrame:
        start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
        if df.index.tz is not None:
            if start_ts.tz is None:
                start_ts = start_ts.tz_localize(df.index.tz)
            if end_ts.tz is None:
                end_ts = end_ts.tz_localize(df.index.tz)
        return df[(df.index >= start_ts) & (df.index <= end_ts)]
