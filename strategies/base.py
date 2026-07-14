"""Abstract base classes for the strategy plugin system."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, Any, List

from markets.base import Bar


@dataclass
class Signal:
    symbol: str
    action: str
    confidence: float
    reason: str
    suggested_stop: Optional[float] = None
    suggested_target: Optional[float] = None
    parameters_used: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)


class StrategyPlugin(ABC):
    """Contract every trading strategy plugin must implement."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable strategy name, e.g. 'Opening Range Breakout'."""
        raise NotImplementedError

    @property
    @abstractmethod
    def code(self) -> str:
        """Short strategy code, e.g. 'ORB'."""
        raise NotImplementedError

    @abstractmethod
    def on_init(self, config: Dict[str, Any], market_adapter: Any) -> None:
        """Called once before the strategy starts running."""
        raise NotImplementedError

    @abstractmethod
    def on_bar(self, bar: Bar) -> Optional[Signal]:
        """Called on each new bar; may return a trade Signal."""
        raise NotImplementedError

    @abstractmethod
    def on_position_update(
        self,
        symbol: str,
        current_price: float,
        entry_price: float,
        unrealized_pnl: float,
        position_size: int,
    ) -> Optional[Signal]:
        """Called on each price update for an open position; may return an exit Signal."""
        raise NotImplementedError

    @abstractmethod
    def on_market_close(self) -> List[Signal]:
        """Called at market close; may return Signals to flatten open positions."""
        raise NotImplementedError

    @abstractmethod
    def get_required_indicators(self) -> List[str]:
        """Return the indicator names this strategy needs computed for it."""
        raise NotImplementedError

    @abstractmethod
    def get_default_parameters(self) -> Dict[str, Any]:
        """Return this strategy's default parameter values."""
        raise NotImplementedError

    @abstractmethod
    def get_supported_markets(self) -> List[str]:
        """Return the market codes this strategy can run on, e.g. ['NSE', 'CRYPTO']."""
        raise NotImplementedError
