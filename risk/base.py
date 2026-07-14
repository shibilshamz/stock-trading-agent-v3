"""Abstract base classes for the risk engine plugin system."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Dict, Any

from markets.base import Order


@dataclass
class RiskCheckResult:
    approved: bool
    adjusted_quantity: Optional[int] = None
    adjusted_price: Optional[float] = None
    reject_reason: Optional[str] = None


class RiskEngine(ABC):
    """Contract every risk engine plugin must implement."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable risk engine name."""
        raise NotImplementedError

    @abstractmethod
    def check_order(self, order: Order, portfolio_state: Dict[str, Any]) -> RiskCheckResult:
        """Validate (and optionally adjust) an order against current portfolio state."""
        raise NotImplementedError

    @abstractmethod
    def on_trade_closed(self, trade_data: Dict[str, Any]) -> None:
        """Called when a trade closes, so the engine can update its internal state."""
        raise NotImplementedError

    @abstractmethod
    def reset_daily(self) -> None:
        """Reset any daily-scoped counters/limits (e.g. at market open)."""
        raise NotImplementedError

    @abstractmethod
    def get_status(self) -> Dict[str, Any]:
        """Return the engine's current risk state (limits used, remaining capacity, etc.)."""
        raise NotImplementedError
