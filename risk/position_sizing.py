"""ATR-based position sizing risk engine plugin."""

from typing import Any, Dict, Optional

from indicators.technical import calculate_atr
from markets.base import Order
from risk.base import RiskCheckResult, RiskEngine


class ATRPositionSizing(RiskEngine):
    """Sizes positions so a stop-loss hit at `stop_loss_atr_mult` * ATR away
    loses no more than `max_risk_per_trade_pct` of the account, capped by
    `max_position_size_pct` of the account per position."""

    DEFAULT_CONFIG = {
        "max_risk_per_trade_pct": 0.01,
        "stop_loss_atr_mult": 1.5,
        "max_position_size_pct": 0.10,
        "default_balance": 50000,
        "atr_period": 14,
        "atr_timeframe": "15m",
    }

    def __init__(self, config: Optional[dict] = None, market_adapter: Any = None):
        self.config = {**self.DEFAULT_CONFIG, **(config or {})}
        self.market_adapter = market_adapter

    @property
    def name(self) -> str:
        return "ATR Position Sizing"

    @property
    def code(self) -> str:
        return "atr_sizing"

    def check_order(self, order: Order, portfolio_state: Dict[str, Any]) -> RiskCheckResult:
        balance = portfolio_state.get("balance", self.config["default_balance"])

        atr = self._resolve_atr(order.symbol, portfolio_state)
        if atr is None or atr <= 0:
            return RiskCheckResult(approved=False, reject_reason="ATR unavailable for position sizing")

        risk_amount = balance * self.config["max_risk_per_trade_pct"]
        stop_distance = atr * self.config["stop_loss_atr_mult"]
        position_size = int(risk_amount / stop_distance)

        price = order.limit_price if order.limit_price is not None else self._resolve_price(order.symbol, portfolio_state)
        if price:
            max_position_value = balance * self.config["max_position_size_pct"]
            if position_size * price > max_position_value:
                position_size = int(max_position_value / price)

        if position_size <= 0:
            return RiskCheckResult(approved=False, reject_reason="Computed position size is zero")

        return RiskCheckResult(approved=True, adjusted_quantity=position_size)

    def on_trade_closed(self, trade_data: Dict[str, Any]) -> None:
        pass

    def reset_daily(self) -> None:
        pass

    def get_status(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "max_risk_per_trade_pct": self.config["max_risk_per_trade_pct"] * 100,
            "max_position_size_pct": self.config["max_position_size_pct"] * 100,
        }

    def _resolve_atr(self, symbol: str, portfolio_state: Dict[str, Any]) -> Optional[float]:
        atr_data = portfolio_state.get("atr")
        if isinstance(atr_data, dict):
            if symbol in atr_data:
                return atr_data[symbol]
        elif atr_data is not None:
            return atr_data

        if self.market_adapter is not None:
            try:
                df = self.market_adapter.get_ohlcv(
                    symbol, timeframe=self.config["atr_timeframe"], bars=100
                )
                atr_series = calculate_atr(df, period=self.config["atr_period"])
                value = atr_series.iloc[-1]
                return float(value) if value == value else None  # NaN check
            except Exception:
                return None
        return None

    def _resolve_price(self, symbol: str, portfolio_state: Dict[str, Any]) -> Optional[float]:
        price_data = portfolio_state.get("price")
        if isinstance(price_data, dict):
            if symbol in price_data:
                return price_data[symbol]
        elif price_data is not None:
            return price_data

        if self.market_adapter is not None:
            try:
                return self.market_adapter.get_latest_price(symbol)
            except Exception:
                return None
        return None
