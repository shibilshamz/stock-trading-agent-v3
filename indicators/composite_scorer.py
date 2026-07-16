"""Signal-scoring functions built on top of indicators/technical.py."""

from datetime import time
from typing import Optional

import pandas as pd

from indicators.technical import calculate_ema, calculate_rsi, calculate_vwap


def score_orb(
    df: pd.DataFrame,
    orb_period_minutes: int = 30,
    current_time: Optional[pd.Timestamp] = None,
    session_start: time = time(9, 15),
) -> float:
    """Score an opening-range breakout: 0 inside the range, up to 1 the further
    price has broken beyond the first `orb_period_minutes` high/low."""
    if df.empty:
        return 0.0

    as_of = pd.Timestamp(current_time) if current_time is not None else df.index[-1]
    session_date = as_of.date()
    day_df = df[df.index.date == session_date]
    if day_df.empty:
        return 0.0

    session_start_ts = pd.Timestamp.combine(session_date, session_start)
    if day_df.index.tz is not None:
        session_start_ts = session_start_ts.tz_localize(day_df.index.tz)
    orb_end_ts = session_start_ts + pd.Timedelta(minutes=orb_period_minutes)

    orb_bars = day_df[(day_df.index >= session_start_ts) & (day_df.index < orb_end_ts)]
    if orb_bars.empty:
        return 0.0

    orb_high = orb_bars["high"].max()
    orb_low = orb_bars["low"].min()
    orb_range = orb_high - orb_low
    if orb_range <= 0:
        return 0.0

    current_bars = day_df[day_df.index <= as_of]
    if current_bars.empty:
        return 0.0
    current_price = current_bars["close"].iloc[-1]

    if current_price > orb_high:
        score = (current_price - orb_high) / orb_range
    elif current_price < orb_low:
        score = (orb_low - current_price) / orb_range
    else:
        score = 0.0

    return float(max(0.0, min(1.0, score)))


def score_vwap(df: pd.DataFrame) -> float:
    """Score how far price has broken above VWAP; 0 if at or below VWAP."""
    vwap = calculate_vwap(df)
    current_vwap = vwap.iloc[-1]
    close = df["close"].iloc[-1]

    if pd.isna(current_vwap) or current_vwap == 0 or close <= current_vwap:
        return 0.0

    score = (close - current_vwap) / current_vwap * 10
    return float(max(0.0, min(1.0, score)))


def score_momentum(
    df: pd.DataFrame, ema_fast: int = 9, ema_slow: int = 21, rsi_period: int = 14
) -> float:
    """Score trend + momentum: EMA crossover gives a 0.5 base, RSI adjusts it up
    (RSI > 50) or down (RSI < 30)."""
    fast = calculate_ema(df, ema_fast).iloc[-1]
    slow = calculate_ema(df, ema_slow).iloc[-1]
    rsi = calculate_rsi(df, rsi_period).iloc[-1]

    score = 0.5 if fast > slow else 0.0

    if pd.notna(rsi):
        if rsi > 50:
            score += min((rsi - 50) / 50, 1.0) * 0.5
        elif rsi < 30:
            score -= min((30 - rsi) / 30, 1.0) * 0.5

    return float(max(0.0, min(1.0, score)))


def composite_score(
    orb_score: float,
    vwap_score: float,
    momentum_score: float,
    orb_weight: float = 0.4,
    vwap_weight: float = 0.3,
    momentum_weight: float = 0.3,
) -> float:
    """Weighted sum of the individual sub-scores."""
    return (
        orb_score * orb_weight
        + vwap_score * vwap_weight
        + momentum_score * momentum_weight
    )
