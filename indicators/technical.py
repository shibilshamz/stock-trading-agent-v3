"""Technical indicator functions operating on OHLCV DataFrames.

Every function accepts a DataFrame with columns [open, high, low, close, volume].
"""

from typing import Tuple

import numpy as np
import pandas as pd


def calculate_vwap(df: pd.DataFrame, anchor: str = "D") -> pd.Series:
    """Volume-weighted average price, cumulative within each anchor period."""
    price_volume = df["close"] * df["volume"]

    if anchor and isinstance(df.index, pd.DatetimeIndex):
        naive_index = df.index.tz_localize(None) if df.index.tz is not None else df.index
        period = naive_index.to_period(anchor)
        cum_pv = price_volume.groupby(period).cumsum()
        cum_volume = df["volume"].groupby(period).cumsum()
    else:
        cum_pv = price_volume.cumsum()
        cum_volume = df["volume"].cumsum()

    return cum_pv / cum_volume


def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range: simple moving average of the true range."""
    prev_close = df["close"].shift(1)
    true_range = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.rolling(period).mean()


def calculate_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Relative Strength Index using Wilder's smoothing of gains/losses."""
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calculate_ema(df: pd.DataFrame, period: int) -> pd.Series:
    """Exponential moving average of close."""
    return df["close"].ewm(span=period, adjust=False).mean()


def calculate_bollinger(
    df: pd.DataFrame, period: int = 20, std_dev: float = 2
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """Bollinger Bands: (upper_band, middle_band, lower_band)."""
    middle_band = df["close"].rolling(period).mean()
    band_std = df["close"].rolling(period).std()
    upper_band = middle_band + std_dev * band_std
    lower_band = middle_band - std_dev * band_std
    return upper_band, middle_band, lower_band


def calculate_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average Directional Index, using Wilder's smoothing throughout."""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)

    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = ((up_move > down_move) & (up_move > 0)) * up_move.clip(lower=0)
    minus_dm = ((down_move > up_move) & (down_move > 0)) * down_move.clip(lower=0)

    true_range = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    smoothed_tr = true_range.ewm(alpha=1 / period, adjust=False).mean()

    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / smoothed_tr
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / smoothed_tr

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    return dx.ewm(alpha=1 / period, adjust=False).mean()


def calculate_obv(df: pd.DataFrame) -> pd.Series:
    """On-Balance Volume: cumulative signed volume by close direction."""
    direction = np.sign(df["close"].diff().fillna(0))
    return (direction * df["volume"]).cumsum()
