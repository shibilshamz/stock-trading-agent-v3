"""Circuit-breaker risk engine plugin: daily loss limit, position count cap,
and a pre-close trading freeze."""

from datetime import datetime, time, timedelta
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from markets.base import Order
from risk.base import RiskCheckResult, RiskEngine

IST = ZoneInfo("Asia/Kolkata")


class CircuitBreakers(RiskEngine):
    """Blocks new orders once the daily loss limit, open-position cap, or
    pre-close trading freeze is hit."""

    DEFAULT_CONFIG = {
        "max_daily_loss_pct": 0.03,
        "max_open_positions": 5,
        "closing_buffer_minutes": 30,
        "market_close": "15:30",
        "default_balance": 50000,
    }

    def __init__(self, config: Optional[dict] = None, market_adapter: Any = None):
        self.config = {**self.DEFAULT_CONFIG, **(config or {})}
        self.market_adapter = market_adapter
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self.open_positions = 0

    @property
    def name(self) -> str:
        return "Circuit Breakers"

    @property
    def code(self) -> str:
        return "circuit_breakers"

    def check_order(self, order: Order, portfolio_state: Dict[str, Any]) -> RiskCheckResult:
        balance = portfolio_state.get("balance", self.config["default_balance"])
        max_daily_loss = self.config["max_daily_loss_pct"] * balance

        if self.daily_pnl <= -max_daily_loss:
            return RiskCheckResult(approved=False, reject_reason="Daily loss limit reached")

        if self.open_positions >= self.config["max_open_positions"]:
            return RiskCheckResult(approved=False, reject_reason="Max open positions reached")

        if self._closing_soon():
            return RiskCheckResult(approved=False, reject_reason="Market closing soon")

        # No dedicated "position opened" hook exists on this interface, so an
        # approved entry is counted here; on_trade_closed balances it back out.
        if order.side == "BUY":
            self.open_positions += 1

        return RiskCheckResult(approved=True)

    def on_trade_closed(self, trade_data: Dict[str, Any]) -> None:
        self.daily_pnl += trade_data.get("pnl", 0)
        self.daily_trades += 1
        self.open_positions = max(0, self.open_positions - 1)

    def reset_daily(self) -> None:
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self.open_positions = 0

    def get_status(self) -> Dict[str, Any]:
        return {
            "daily_pnl": self.daily_pnl,
            "daily_trades": self.daily_trades,
            "open_positions": self.open_positions,
        }

    def _closing_soon(self) -> bool:
        now = datetime.now(IST)
        if self.market_adapter is not None:
            _, close_time = self.market_adapter.get_market_hours()
        else:
            close_time = self._parse_time(self.config["market_close"])

        close_dt = datetime.combine(now.date(), close_time, tzinfo=IST)
        buffer = timedelta(minutes=self.config["closing_buffer_minutes"])
        return close_dt - buffer <= now <= close_dt

    @staticmethod
    def _parse_time(value: str) -> time:
        hour, minute = value.split(":")
        return time(int(hour), int(minute))
