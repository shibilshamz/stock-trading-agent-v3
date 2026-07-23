"""NSE (National Stock Exchange, India) market adapter plugin."""

import random
import uuid
from datetime import datetime, time
from typing import List, Optional

import pandas as pd
import yfinance as yf
from zoneinfo import ZoneInfo

from markets.base import MarketAdapter, Order, OrderResult

IST = ZoneInfo("Asia/Kolkata")


class NSEAdapter(MarketAdapter):
    """Market adapter for the Indian National Stock Exchange, backed by yfinance."""

    NIFTY_50_SYMBOLS = [
        "RELIANCE.NS", "TCS.NS", "INFY.NS", "HDFCBANK.NS", "ICICIBANK.NS",
        "HINDUNILVR.NS", "ITC.NS", "SBIN.NS", "BHARTIARTL.NS", "KOTAKBANK.NS",
        "LT.NS", "AXISBANK.NS", "BAJFINANCE.NS", "ASIANPAINT.NS", "MARUTI.NS",
        "TATAMOTORS.NS", "SUNPHARMA.NS", "TITAN.NS", "ULTRACEMCO.NS", "NESTLEIND.NS",
        "WIPRO.NS", "POWERGRID.NS", "NTPC.NS", "COALINDIA.NS", "BAJAJFINSV.NS",
        "ADANIENT.NS", "ADANIPORTS.NS", "HCLTECH.NS", "TECHM.NS", "INDUSINDBK.NS",
        "JSWSTEEL.NS", "GRASIM.NS", "TATASTEEL.NS", "BRITANNIA.NS", "CIPLA.NS",
        "EICHERMOT.NS", "DIVISLAB.NS", "APOLLOHOSP.NS", "HEROMOTOCO.NS", "BPCL.NS",
        "DRREDDY.NS", "M&M.NS", "HINDALCO.NS", "ONGC.NS", "TATACONSUM.NS",
        "SBILIFE.NS", "HDFCLIFE.NS", "UPL.NS", "SHREECEM.NS", "DABUR.NS",
    ]

    DEFAULT_CONFIG = {
        "market_open": "09:15",
        "market_close": "15:30",
        "supported_timeframes": ["1m", "5m", "15m", "1h", "1d"],
        "slippage_pct": 0.001,
        "universe_symbols": None,  # None => use NIFTY_50_SYMBOLS
        "universe_lookback_period": "1mo",
        "universe_min_bars": 15,
        "universe_atr_period": 14,
        "universe_weights": {"gap": 0.4, "volume": 0.3, "atr": 0.3},
    }

    def __init__(self, config: Optional[dict] = None):
        self.config = {**self.DEFAULT_CONFIG, **(config or {})}

    @property
    def name(self) -> str:
        return "Indian (NSE)"

    @property
    def code(self) -> str:
        return "nse"

    def get_universe(self, top_n: int = 20, criteria: dict = None) -> List[str]:
        symbols = self.config["universe_symbols"] or self.NIFTY_50_SYMBOLS
        weights = {**self.config["universe_weights"], **(criteria or {})}
        min_bars = self.config["universe_min_bars"]
        atr_period = self.config["universe_atr_period"]

        raw = yf.download(
            symbols,
            period=self.config["universe_lookback_period"],
            interval="1d",
            group_by="ticker",
            progress=False,
            auto_adjust=True,
            threads=True,
        )

        metrics = {}
        for symbol in symbols:
            try:
                df = raw[symbol] if isinstance(raw.columns, pd.MultiIndex) else raw
                df = df.rename(columns=str.lower).dropna()
                if len(df) < min_bars:
                    continue
                gap_pct = abs(
                    (df["open"].iloc[-1] - df["close"].iloc[-2]) / df["close"].iloc[-2]
                ) * 100
                volume = float(df["volume"].iloc[-1])
                atr = self._true_range_atr(df, atr_period)
                if atr is None:
                    continue
                metrics[symbol] = {"gap": gap_pct, "volume": volume, "atr": atr}
            except Exception:
                continue

        if not metrics:
            return []

        metrics_df = pd.DataFrame(metrics).T
        spread = metrics_df.max() - metrics_df.min()
        normalized = (metrics_df - metrics_df.min()) / spread.replace(0, 1)

        scores = (
            normalized["gap"] * weights.get("gap", 0)
            + normalized["volume"] * weights.get("volume", 0)
            + normalized["atr"] * weights.get("atr", 0)
        )
        return scores.sort_values(ascending=False).head(top_n).index.tolist()

    def get_ohlcv(
        self, symbol: str, timeframe: str = "15m", bars: int = 100, end: Optional[datetime] = None
    ) -> pd.DataFrame:
        if end is not None:
            end_ts = pd.Timestamp(end)
            start_ts = end_ts - pd.Timedelta(days=self._resolve_days(timeframe, bars))
            df = yf.download(
                symbol, start=start_ts, end=end_ts, interval=timeframe, progress=False, auto_adjust=True
            )
        else:
            period = self._resolve_period(timeframe, bars)
            df = yf.download(
                symbol, period=period, interval=timeframe, progress=False, auto_adjust=True
            )
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
        return df.tail(bars)

    def get_latest_price(self, symbol: str) -> float:
        df = self.get_ohlcv(symbol, timeframe="15m", bars=1)
        return float(df["close"].iloc[-1])

    def is_market_open(self) -> bool:
        now = datetime.now(IST)
        if now.weekday() > 4:  # Saturday=5, Sunday=6
            return False
        open_time, close_time = self.get_market_hours()
        return open_time <= now.time() <= close_time

    def place_order(self, order: Order) -> OrderResult:
        price = self.get_latest_price(order.symbol)
        slippage_pct = self.config["slippage_pct"]
        slippage = random.uniform(-slippage_pct, slippage_pct)
        fill_price = round(price * (1 + slippage), 2)
        return OrderResult(
            order_id=str(uuid.uuid4()),
            status="FILLED",
            filled_price=fill_price,
            filled_quantity=order.quantity,
            timestamp=datetime.now(IST),
        )

    def get_market_hours(self) -> tuple:
        return (
            self._parse_time(self.config["market_open"]),
            self._parse_time(self.config["market_close"]),
        )

    def get_supported_timeframes(self) -> List[str]:
        return self.config["supported_timeframes"]

    @staticmethod
    def _parse_time(value: str) -> time:
        hour, minute = value.split(":")
        return time(int(hour), int(minute))

    @staticmethod
    def _true_range_atr(df: pd.DataFrame, period: int) -> Optional[float]:
        if len(df) < period + 1:
            return None
        prev_close = df["close"].shift(1)
        tr = pd.concat(
            [
                df["high"] - df["low"],
                (df["high"] - prev_close).abs(),
                (df["low"] - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        return float(tr.rolling(period).mean().iloc[-1])

    @staticmethod
    def _resolve_period(timeframe: str, bars: int) -> str:
        intraday_max = {"1m": "7d", "5m": "60d", "15m": "60d", "1h": "2y"}
        if timeframe in intraday_max:
            return intraday_max[timeframe]
        if bars <= 22:
            return "1mo"
        if bars <= 65:
            return "3mo"
        if bars <= 130:
            return "6mo"
        if bars <= 250:
            return "1y"
        if bars <= 500:
            return "2y"
        return "5y"

    @staticmethod
    def _resolve_days(timeframe: str, bars: int) -> int:
        """Same intent as _resolve_period, expressed as a day count for use
        with an explicit end-anchored start date instead of yfinance's
        relative-to-now `period` shorthand."""
        intraday_max_days = {"1m": 7, "5m": 60, "15m": 60, "1h": 730}
        if timeframe in intraday_max_days:
            return intraday_max_days[timeframe]
        trading_days = max(bars, 1)
        return int(trading_days * 1.6) + 5  # pad for weekends/holidays
