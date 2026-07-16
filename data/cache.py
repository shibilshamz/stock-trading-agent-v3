"""SQLite-backed OHLCV cache, backed by yfinance for anything not yet cached."""

import sqlite3
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Union

import pandas as pd
import yfinance as yf

TRADING_MINUTES_PER_DAY = 375  # NSE session: 09:15-15:30
MINUTES_PER_BAR = {"1m": 1, "5m": 5, "15m": 15, "1h": 60}

DateLike = Union[str, datetime, pd.Timestamp]

SCHEMA = """
CREATE TABLE IF NOT EXISTS ohlcv_cache (
    symbol TEXT,
    timeframe TEXT,
    timestamp TIMESTAMP,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume INTEGER,
    PRIMARY KEY (symbol, timeframe, timestamp)
)
"""


class DataCache:
    """Caches OHLCV bars in SQLite; transparently backfills gaps from yfinance."""

    def __init__(self, db_path: str = "data/market_cache.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute(SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def get_ohlcv(
        self, symbol: str, timeframe: str, start: DateLike, end: DateLike
    ) -> pd.DataFrame:
        start_ts, end_ts = self._normalize(start), self._normalize(end)
        coverage = self.get_cache_coverage(symbol, timeframe, start_ts, end_ts)

        if coverage["missing_bars"] <= 0:
            cached = self._query_cache(symbol, timeframe, start_ts, end_ts)
            if cached is not None:
                return cached

        try:
            fresh = self._download(symbol, timeframe, start_ts, end_ts)
        except Exception as exc:
            warnings.warn(
                f"yfinance download failed for {symbol}/{timeframe} ({exc}); "
                "returning cached data only"
            )
            cached = self._query_cache(symbol, timeframe, start_ts, end_ts)
            return cached if cached is not None else self._empty_frame()

        if not fresh.empty:
            self._save_to_cache(symbol, timeframe, fresh)

        merged = self._query_cache(symbol, timeframe, start_ts, end_ts)
        return merged if merged is not None else self._empty_frame()

    def _save_to_cache(self, symbol: str, timeframe: str, df: pd.DataFrame) -> None:
        if df.empty:
            return
        index = df.index.tz_localize(None) if df.index.tz is not None else df.index
        rows = [
            (
                symbol,
                timeframe,
                ts.isoformat(),
                float(row["open"]),
                float(row["high"]),
                float(row["low"]),
                float(row["close"]),
                int(row["volume"]),
            )
            for ts, (_, row) in zip(index, df.iterrows())
        ]
        conn = self._connect()
        try:
            conn.executemany(
                """INSERT OR REPLACE INTO ohlcv_cache
                   (symbol, timeframe, timestamp, open, high, low, close, volume)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
            conn.commit()
        finally:
            conn.close()

    def _query_cache(
        self, symbol: str, timeframe: str, start: DateLike, end: DateLike
    ) -> Optional[pd.DataFrame]:
        start_ts, end_ts = self._normalize(start), self._normalize(end)
        conn = self._connect()
        try:
            df = pd.read_sql_query(
                """SELECT timestamp, open, high, low, close, volume FROM ohlcv_cache
                   WHERE symbol = ? AND timeframe = ? AND timestamp >= ? AND timestamp <= ?
                   ORDER BY timestamp""",
                conn,
                params=(symbol, timeframe, start_ts.isoformat(), end_ts.isoformat()),
            )
        finally:
            conn.close()

        if df.empty:
            return None
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        return df.set_index("timestamp")

    def get_cache_coverage(
        self, symbol: str, timeframe: str, start: DateLike, end: DateLike
    ) -> Dict[str, Any]:
        start_ts, end_ts = self._normalize(start), self._normalize(end)
        cached = self._query_cache(symbol, timeframe, start_ts, end_ts)
        cached_bars = len(cached) if cached is not None else 0
        expected_bars = self._expected_bar_count(start_ts, end_ts, timeframe)
        missing_bars = max(0, expected_bars - cached_bars)
        coverage_pct = round((cached_bars / expected_bars * 100), 2) if expected_bars > 0 else 100.0
        return {
            "cached_bars": cached_bars,
            "missing_bars": missing_bars,
            "coverage_pct": coverage_pct,
        }

    @staticmethod
    def _download(symbol: str, timeframe: str, start: DateLike, end: DateLike) -> pd.DataFrame:
        df = yf.download(
            symbol, start=start, end=end, interval=timeframe, progress=False, auto_adjust=True
        )
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]

    @staticmethod
    def _expected_bar_count(start: pd.Timestamp, end: pd.Timestamp, timeframe: str) -> int:
        business_days = max(len(pd.bdate_range(start, end)), 1)
        if timeframe in MINUTES_PER_BAR:
            bars_per_day = TRADING_MINUTES_PER_DAY // MINUTES_PER_BAR[timeframe]
            return business_days * bars_per_day
        return business_days

    @staticmethod
    def _normalize(value: DateLike) -> pd.Timestamp:
        ts = pd.Timestamp(value)
        return ts.tz_localize(None) if ts.tz is not None else ts

    @staticmethod
    def _empty_frame() -> pd.DataFrame:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
