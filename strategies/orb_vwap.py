"""ORB + VWAP + Momentum strategy plugin."""

from typing import Any, Dict, List, Optional

import pandas as pd

from indicators.composite_scorer import (
    composite_score,
    score_momentum,
    score_orb,
    score_vwap,
)
from indicators.technical import calculate_atr
from markets.base import Bar
from strategies.base import Signal, StrategyPlugin


class ORBVWAPStrategy(StrategyPlugin):
    """Composite-score strategy combining opening-range breakout, VWAP position,
    and EMA/RSI momentum into a single weighted BUY signal."""

    DEFAULT_PARAMETERS: Dict[str, Any] = {
        "orb_weight": 0.4,
        "vwap_weight": 0.3,
        "momentum_weight": 0.3,
        "signal_threshold": 0.55,
        "orb_period_minutes": 30,
        "ema_fast": 9,
        "ema_slow": 21,
        "rsi_period": 14,
        "stop_loss_atr_mult": 1.5,
        "take_profit_rr": 2.0,
        "use_ai_validation": True,
        "ai_confidence_threshold": 0.6,
    }

    def __init__(self):
        self.config: Dict[str, Any] = dict(self.DEFAULT_PARAMETERS)
        self.market_adapter: Any = None
        self.open_positions: Dict[str, Dict[str, float]] = {}

    @property
    def name(self) -> str:
        return "ORB + VWAP + Momentum"

    @property
    def code(self) -> str:
        return "orb_vwap"

    def on_init(self, config: Dict[str, Any], market_adapter: Any) -> None:
        self.config = {**self.get_default_parameters(), **(config or {})}
        self.market_adapter = market_adapter
        self.open_positions = {}

    def on_bar(self, bar: Bar) -> Optional[Signal]:
        if self.market_adapter is None:
            return None

        df = self.market_adapter.get_ohlcv(
            bar.symbol, timeframe=bar.timeframe, bars=self.config.get("lookback_bars", 100)
        )
        min_bars = max(self.config["ema_slow"], self.config["rsi_period"]) + 1
        if df.empty or len(df) < min_bars:
            return None

        atr = calculate_atr(df, period=self.config.get("atr_period", 14)).iloc[-1]
        if pd.isna(atr) or atr <= 0:
            return None

        orb = score_orb(df, orb_period_minutes=self.config["orb_period_minutes"])
        vwap = score_vwap(df)
        momentum = score_momentum(
            df,
            ema_fast=self.config["ema_fast"],
            ema_slow=self.config["ema_slow"],
            rsi_period=self.config["rsi_period"],
        )
        composite = composite_score(
            orb,
            vwap,
            momentum,
            orb_weight=self.config["orb_weight"],
            vwap_weight=self.config["vwap_weight"],
            momentum_weight=self.config["momentum_weight"],
        )

        if composite < self.config["signal_threshold"]:
            return None

        if self.config.get("use_ai_validation", True) and not self._ai_validate(bar.symbol, composite):
            return None

        close = df["close"].iloc[-1]
        stop_mult = self.config["stop_loss_atr_mult"]
        stop = close - (atr * stop_mult)
        target = close + (atr * stop_mult * self.config["take_profit_rr"])

        self.open_positions[bar.symbol] = {"stop_loss": stop, "take_profit": target}

        return Signal(
            symbol=bar.symbol,
            action="BUY",
            confidence=composite,
            reason="ORB+VWAP+Momentum",
            suggested_stop=stop,
            suggested_target=target,
            parameters_used=dict(self.config),
        )

    def _ai_validate(self, symbol: str, composite: float) -> bool:
        """Placeholder AI validation hook -- always approves until a model is wired in."""
        return True

    def on_position_update(
        self,
        symbol: str,
        current_price: float,
        entry_price: float,
        unrealized_pnl: float,
        position_size: int,
    ) -> Optional[Signal]:
        position = self.open_positions.get(symbol)
        if not position:
            return None

        if current_price <= position["stop_loss"]:
            self.open_positions.pop(symbol, None)
            return Signal(
                symbol=symbol,
                action="SELL",
                confidence=1.0,
                reason="STOP_LOSS",
                parameters_used=dict(self.config),
            )

        if current_price >= position["take_profit"]:
            self.open_positions.pop(symbol, None)
            return Signal(
                symbol=symbol,
                action="SELL",
                confidence=1.0,
                reason="TAKE_PROFIT",
                parameters_used=dict(self.config),
            )

        return None

    def on_market_close(self) -> List[Signal]:
        signals = [
            Signal(
                symbol=symbol,
                action="SELL",
                confidence=1.0,
                reason="MARKET_CLOSE",
                parameters_used=dict(self.config),
            )
            for symbol in self.open_positions
        ]
        self.open_positions = {}
        return signals

    def get_required_indicators(self) -> List[str]:
        return ["VWAP", "ATR", "RSI", "EMA", "ORB"]

    def get_default_parameters(self) -> Dict[str, Any]:
        return dict(self.DEFAULT_PARAMETERS)

    def get_supported_markets(self) -> List[str]:
        return ["nse"]
