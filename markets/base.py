"""Abstract base classes for the market adapter plugin system."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, time
from typing import List, Optional, Tuple

import pandas as pd


@dataclass(frozen=True)
class Order:
    symbol: str
    side: str
    quantity: int
    order_type: str
    limit_price: Optional[float] = None
    strategy_name: str = ""


@dataclass(frozen=True)
class OrderResult:
    order_id: str
    status: str
    filled_price: float
    filled_quantity: int
    timestamp: datetime
    reject_reason: Optional[str] = None


@dataclass(frozen=True)
class Bar:
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    timeframe: str = "15m"


class MarketAdapter(ABC):
    """Contract every market plugin (NSE, crypto, forex, ...) must implement."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable market name, e.g. 'National Stock Exchange'."""
        raise NotImplementedError

    @property
    @abstractmethod
    def code(self) -> str:
        """Short market code, e.g. 'NSE'."""
        raise NotImplementedError

    @abstractmethod
    def get_universe(self, top_n: int = 20, criteria: dict = None) -> List[str]:
        """Return the list of tradable symbols for this market."""
        raise NotImplementedError

    @abstractmethod
    def get_ohlcv(self, symbol: str, timeframe: str = "15m", bars: int = 100) -> pd.DataFrame:
        """Return historical OHLCV bars for a symbol as a DataFrame."""
        raise NotImplementedError

    @abstractmethod
    def get_latest_price(self, symbol: str) -> float:
        """Return the latest traded price for a symbol."""
        raise NotImplementedError

    @abstractmethod
    def is_market_open(self) -> bool:
        """Return whether this market is currently open for trading."""
        raise NotImplementedError

    @abstractmethod
    def place_order(self, order: Order) -> OrderResult:
        """Submit an order and return its execution result."""
        raise NotImplementedError

    @abstractmethod
    def get_market_hours(self) -> Tuple[time, time]:
        """Return the (open, close) trading hours for this market."""
        raise NotImplementedError

    @abstractmethod
    def get_supported_timeframes(self) -> List[str]:
        """Return the list of timeframes this market's data feed supports."""
        raise NotImplementedError
