"""Abstract base class for the data feed plugin system."""

from abc import ABC, abstractmethod
from typing import Callable, List, Dict, Any
from datetime import datetime

import pandas as pd

from markets.base import Bar


class DataFeed(ABC):
    """Contract every data feed plugin (live, paper, historical, ...) must implement."""

    @property
    @abstractmethod
    def mode(self) -> str:
        """Feed mode: 'live', 'paper', 'historical_replay', or 'historical_batch'."""
        raise NotImplementedError

    @abstractmethod
    def subscribe(self, symbols: List[str], callback: Callable[[Bar], None]) -> None:
        """Subscribe to bar updates for the given symbols, invoking callback per Bar."""
        raise NotImplementedError

    @abstractmethod
    def unsubscribe(self, symbols: List[str]) -> None:
        """Unsubscribe from bar updates for the given symbols."""
        raise NotImplementedError

    @abstractmethod
    def get_historical(
        self, symbol: str, start: datetime, end: datetime, timeframe: str = "15m"
    ) -> pd.DataFrame:
        """Return historical OHLCV bars for a symbol between start and end."""
        raise NotImplementedError

    @abstractmethod
    def stop(self) -> None:
        """Stop the feed and release any underlying resources/connections."""
        raise NotImplementedError

    @abstractmethod
    def get_status(self) -> Dict[str, Any]:
        """Return the current status of the feed (connection state, subscriptions, etc.)."""
        raise NotImplementedError
