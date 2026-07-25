"""Simple moving-average crossover strategy plugin.

A deliberately minimal test strategy: a fast SMA and a slow SMA, nothing else.
- BUY  when the fast SMA crosses ABOVE the slow SMA.
- SELL/EXIT when the fast SMA crosses BELOW the slow SMA.

No volume, RSI, ATR, VWAP or AI filters -- kept bare on purpose so its signals
are easy to cross-check by hand against a chart. Plugs into the same
StrategyPlugin contract as orb_vwap.py and is auto-discovered by the registry
under the code "ma_crossover"; it does not touch or depend on ORB+VWAP.
"""

import logging
from typing import Any, Dict, List, Optional

import pandas as pd

from markets.base import Bar
from strategies.base import Signal, StrategyPlugin

logger = logging.getLogger("ma_crossover")


class MACrossoverStrategy(StrategyPlugin):
    """Fast/slow simple-moving-average crossover. Long-only, minimal by design."""

    DEFAULT_PARAMETERS: Dict[str, Any] = {
        "fast_period": 9,
        "slow_period": 21,
        "lookback_bars": 100,
    }

    def __init__(self):
        self.config: Dict[str, Any] = dict(self.DEFAULT_PARAMETERS)
        self.market_adapter: Any = None
        # Symbols we currently consider "in position" (mirrors the engine's view
        # so on_market_close can flatten if ever configured to).
        self.open_positions: Dict[str, Dict[str, Any]] = {}
        # Per-symbol cache of the most recent on_bar evaluation, so
        # on_position_update reuses the same MA values/crossover computed for the
        # current bar instead of recomputing (and double-logging) them.
        self._last_eval: Dict[str, Dict[str, Any]] = {}

    @property
    def name(self) -> str:
        return "MA Crossover"

    @property
    def code(self) -> str:
        return "ma_crossover"

    def on_init(self, config: Dict[str, Any], market_adapter: Any) -> None:
        self.config = {**self.get_default_parameters(), **(config or {})}
        self.market_adapter = market_adapter
        self.open_positions = {}
        self._last_eval = {}

    def on_bar(self, bar: Bar) -> Optional[Signal]:
        if self.market_adapter is None:
            return None

        evaluation = self._evaluate(bar)
        if evaluation is None:
            return None

        # Enter long on a fresh bullish crossover, only when flat on this symbol.
        if evaluation["cross"] == "BULLISH" and bar.symbol not in self.open_positions:
            self.open_positions[bar.symbol] = {"entry_time": bar.timestamp}
            return Signal(
                symbol=bar.symbol,
                action="BUY",
                confidence=1.0,
                reason="MA_CROSS_UP",
                parameters_used=dict(self.config),
                timestamp=bar.timestamp,
            )

        return None

    def on_position_update(
        self,
        symbol: str,
        current_price: float,
        entry_price: float,
        unrealized_pnl: float,
        position_size: int,
    ) -> Optional[Signal]:
        # Reuse the crossover already computed for this bar in on_bar; the engine
        # calls on_position_update immediately after on_bar within the same bar.
        evaluation = self._last_eval.get(symbol)
        if not evaluation:
            return None

        if evaluation["cross"] == "BEARISH":
            self.open_positions.pop(symbol, None)
            return Signal(
                symbol=symbol,
                action="SELL",
                confidence=1.0,
                reason="MA_CROSS_DOWN",
                parameters_used=dict(self.config),
                timestamp=evaluation["timestamp"],
            )

        return None

    def on_market_close(self) -> List[Signal]:
        # Minimal by design: no forced end-of-day flatten. Positions are held
        # until a bearish crossover exits them (the backtest engine still
        # force-closes anything still open at the very end of the run).
        return []

    def _evaluate(self, bar: Bar) -> Optional[Dict[str, Any]]:
        """Compute fast/slow SMA for this symbol at the current bar, detect any
        crossover vs the prior bar, log both, and cache the result.

        Returns a dict {timestamp, fast_ma, slow_ma, cross} or None if there is
        not yet enough data to form two consecutive SMA points.
        """
        fast_period = int(self.config["fast_period"])
        slow_period = int(self.config["slow_period"])

        df = self.market_adapter.get_ohlcv(
            bar.symbol,
            timeframe=bar.timeframe,
            bars=int(self.config.get("lookback_bars", 100)),
        )
        # Need slow_period bars for one SMA value, plus one more bar to compare
        # against for crossover detection.
        if df is None or df.empty or len(df) < slow_period + 1:
            return None

        fast_sma = df["close"].rolling(fast_period).mean()
        slow_sma = df["close"].rolling(slow_period).mean()

        fast_prev, fast_curr = fast_sma.iloc[-2], fast_sma.iloc[-1]
        slow_prev, slow_curr = slow_sma.iloc[-2], slow_sma.iloc[-1]

        if pd.isna(fast_prev) or pd.isna(slow_prev) or pd.isna(fast_curr) or pd.isna(slow_curr):
            return None

        # Verification log: every MA calculation.
        logger.info(
            "MA calc | %s | %s | fast_ma(%d)=%.4f slow_ma(%d)=%.4f",
            bar.symbol, bar.timestamp, fast_period, fast_curr, slow_period, slow_curr,
        )

        cross: Optional[str] = None
        if fast_prev <= slow_prev and fast_curr > slow_curr:
            cross = "BULLISH"
        elif fast_prev >= slow_prev and fast_curr < slow_curr:
            cross = "BEARISH"

        if cross is not None:
            signal_type = "BUY" if cross == "BULLISH" else "SELL"
            # Verification log: every crossover / signal event.
            logger.info(
                "MA CROSS | %s | %s | %s (%s) | fast_ma=%.4f slow_ma=%.4f",
                bar.symbol, bar.timestamp, cross, signal_type, fast_curr, slow_curr,
            )

        evaluation = {
            "timestamp": bar.timestamp,
            "fast_ma": float(fast_curr),
            "slow_ma": float(slow_curr),
            "cross": cross,
        }
        self._last_eval[bar.symbol] = evaluation
        return evaluation

    def get_required_indicators(self) -> List[str]:
        return ["SMA"]

    def get_default_parameters(self) -> Dict[str, Any]:
        return dict(self.DEFAULT_PARAMETERS)

    def get_supported_markets(self) -> List[str]:
        return ["nse"]
