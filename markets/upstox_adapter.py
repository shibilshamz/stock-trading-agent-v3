"""NSE market adapter backed by the Upstox API (v3 historical candles, v2 LTP
quotes) instead of yfinance.

Registered under market code "upstox" -- distinct from the existing "nse"
(yfinance-backed) adapter -- so a run can pick either backend. The main
reason to prefer this one: Upstox serves intraday (minute/hour) history back
to January 2022, versus yfinance's 60-day cap on 15m data.

Real order placement (POST /v2/order/place) is intentionally not wired in
yet -- place_order() here paper-fills against a live Upstox LTP quote, same
as NSEAdapter. Routing real orders is a deliberately separate, higher-stakes
step meant to come after paper trading on real Upstox data has been
validated.
"""

import gzip
import json
import random
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import httpx
import pandas as pd
from zoneinfo import ZoneInfo

from markets.base import MarketAdapter, Order, OrderResult
from markets.nse_adapter import NSEAdapter
from markets.upstox_auth import get_access_token

IST = ZoneInfo("Asia/Kolkata")
BASE_URL = "https://api.upstox.com"
INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
TRADING_MINUTES_PER_DAY = 375  # NSE session: 09:15-15:30


class UpstoxNotConnectedError(RuntimeError):
    """Raised when an API call is made without a valid (non-expired) access token."""


class UpstoxAdapter(MarketAdapter):
    """Market adapter for NSE via the Upstox API. Requires a daily OAuth login
    through the dashboard's Upstox panel (see markets/upstox_auth.py) --
    Upstox access tokens always expire at 03:30 IST with no refresh token."""

    DEFAULT_CONFIG = {
        "market_open": "09:15",
        "market_close": "15:30",
        "slippage_pct": 0.001,
        "instruments_cache_path": "data/upstox_instruments.json",
        "instruments_cache_max_age_hours": 20,
        "universe_symbols": None,  # None => use NSEAdapter.NIFTY_50_SYMBOLS
        "universe_min_bars": 15,
        "universe_atr_period": 14,
        "universe_weights": {"gap": 0.4, "volume": 0.3, "atr": 0.3},
    }

    # timeframe -> (unit, interval, max days per request) per Upstox v3 limits:
    # 1-15min candles: 1 month per request; 30min/1h: 1 quarter; 1d: effectively unbounded.
    _TIMEFRAME_MAP: Dict[str, Tuple[str, int, int]] = {
        "1m": ("minutes", 1, 30),
        "5m": ("minutes", 5, 30),
        "15m": ("minutes", 15, 30),
        "30m": ("minutes", 30, 90),
        "1h": ("hours", 1, 90),
        "1d": ("days", 1, 3650),
    }

    def __init__(self, config: Optional[dict] = None):
        self.config = {**self.DEFAULT_CONFIG, **(config or {})}
        self._instrument_map: Optional[Dict[str, str]] = None
        self._hours_helper = NSEAdapter(
            config={"market_open": self.config["market_open"], "market_close": self.config["market_close"]}
        )

    @property
    def name(self) -> str:
        return "Indian (NSE via Upstox)"

    @property
    def code(self) -> str:
        return "upstox"

    # -- MarketAdapter contract -----------------------------------------------

    def get_universe(self, top_n: int = 20, criteria: dict = None) -> List[str]:
        symbols = self.config["universe_symbols"] or NSEAdapter.NIFTY_50_SYMBOLS
        weights = {**self.config["universe_weights"], **(criteria or {})}
        min_bars = self.config["universe_min_bars"]
        atr_period = self.config["universe_atr_period"]

        metrics = {}
        for symbol in symbols:
            try:
                df = self.get_ohlcv(symbol, timeframe="1d", bars=atr_period + 5)
                if len(df) < min_bars:
                    continue
                gap_pct = abs((df["open"].iloc[-1] - df["close"].iloc[-2]) / df["close"].iloc[-2]) * 100
                volume = float(df["volume"].iloc[-1])
                atr = NSEAdapter._true_range_atr(df, atr_period)
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
        unit, interval, max_window_days = self._TIMEFRAME_MAP.get(timeframe, ("minutes", 15, 30))
        instrument_key = self._instrument_key(symbol)

        end_date = pd.Timestamp(end).date() if end is not None else date.today()
        start_date = end_date - timedelta(days=self._estimate_span_days(timeframe, bars))

        frames = []
        window_start = start_date
        while window_start <= end_date:
            window_end = min(window_start + timedelta(days=max_window_days - 1), end_date)
            frames.append(self._fetch_candles(instrument_key, unit, interval, window_start, window_end))
            window_start = window_end + timedelta(days=1)

        if not frames or all(f.empty for f in frames):
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        df = pd.concat(frames).sort_index()
        df = df[~df.index.duplicated(keep="last")]
        return df.tail(bars)

    def get_latest_price(self, symbol: str) -> float:
        instrument_key = self._instrument_key(symbol)
        response = httpx.get(
            f"{BASE_URL}/v2/market-quote/ltp",
            params={"instrument_key": instrument_key},
            headers=self._auth_headers(),
            timeout=15.0,
        )
        response.raise_for_status()
        data = response.json().get("data", {})
        for entry in data.values():
            return float(entry["last_price"])
        raise KeyError(f"No LTP data returned for {symbol!r}")

    def is_market_open(self) -> bool:
        return self._hours_helper.is_market_open()

    def place_order(self, order: Order) -> OrderResult:
        price = self.get_latest_price(order.symbol)
        slippage_pct = self.config["slippage_pct"]
        fill_price = round(price * (1 + random.uniform(-slippage_pct, slippage_pct)), 2)
        return OrderResult(
            order_id=str(uuid.uuid4()),
            status="FILLED",
            filled_price=fill_price,
            filled_quantity=order.quantity,
            timestamp=datetime.now(IST),
        )

    def get_market_hours(self) -> tuple:
        return self._hours_helper.get_market_hours()

    def get_supported_timeframes(self) -> List[str]:
        return list(self._TIMEFRAME_MAP)

    # -- auth -----------------------------------------------------------------

    def _auth_headers(self) -> Dict[str, str]:
        token = get_access_token()
        if not token:
            raise UpstoxNotConnectedError(
                "No valid Upstox access token -- log in via the dashboard's Upstox panel "
                "first (tokens expire daily at 03:30 IST)."
            )
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    # -- historical candle fetch ------------------------------------------------

    def _fetch_candles(
        self, instrument_key: str, unit: str, interval: int, start_date: date, end_date: date
    ) -> pd.DataFrame:
        url = (
            f"{BASE_URL}/v3/historical-candle/{instrument_key}/{unit}/{interval}/"
            f"{end_date.isoformat()}/{start_date.isoformat()}"
        )
        response = httpx.get(url, headers=self._auth_headers(), timeout=15.0)
        response.raise_for_status()
        candles = response.json().get("data", {}).get("candles", [])
        if not candles:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        rows = [
            {"timestamp": pd.Timestamp(c[0]), "open": c[1], "high": c[2], "low": c[3], "close": c[4], "volume": c[5]}
            for c in candles
        ]
        return pd.DataFrame(rows).set_index("timestamp").sort_index()

    @staticmethod
    def _estimate_span_days(timeframe: str, bars: int) -> int:
        minutes_per_bar = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "1d": TRADING_MINUTES_PER_DAY}
        per_trading_day = max(TRADING_MINUTES_PER_DAY // minutes_per_bar.get(timeframe, 15), 1)
        trading_days_needed = max(-(-bars // per_trading_day), 1)  # ceil division
        return int(trading_days_needed * 1.6) + 5  # pad for weekends/holidays

    # -- instrument key lookup -------------------------------------------------

    def _instrument_key(self, symbol: str) -> str:
        trading_symbol = symbol.split(".")[0].upper()
        instruments = self._load_instrument_map()
        key = instruments.get(trading_symbol)
        if key is None:
            raise KeyError(f"No Upstox instrument_key found for symbol {symbol!r}")
        return key

    def _load_instrument_map(self) -> Dict[str, str]:
        if self._instrument_map is not None:
            return self._instrument_map

        cache_path = Path(self.config["instruments_cache_path"])
        max_age = timedelta(hours=self.config["instruments_cache_max_age_hours"])
        if cache_path.exists():
            age = datetime.now() - datetime.fromtimestamp(cache_path.stat().st_mtime)
            if age < max_age:
                self._instrument_map = json.loads(cache_path.read_text())
                return self._instrument_map

        self._instrument_map = self._download_instrument_map()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(self._instrument_map))
        return self._instrument_map

    @staticmethod
    def _download_instrument_map() -> Dict[str, str]:
        response = httpx.get(INSTRUMENTS_URL, timeout=30.0, follow_redirects=True)
        response.raise_for_status()
        records = json.loads(gzip.decompress(response.content))
        return {r["trading_symbol"]: r["instrument_key"] for r in records if r.get("segment") == "NSE_EQ"}
