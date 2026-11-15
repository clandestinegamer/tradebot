"""Technical indicators on a candle DataFrame with columns open, high, low, close, volume."""
from __future__ import annotations

import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss
    out = 100 - 100 / (1 + rs)
    out = out.where(avg_loss != 0, 100.0)
    return out


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's ADX — trend strength regardless of direction. <20 ≈ choppy, >25 ≈ trending."""
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    a = atr(df, period)
    alpha = 1 / period
    plus_di = 100 * plus_dm.ewm(alpha=alpha, adjust=False, min_periods=period).mean() / a
    minus_di = 100 * minus_dm.ewm(alpha=alpha, adjust=False, min_periods=period).mean() / a
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    return dx.ewm(alpha=alpha, adjust=False, min_periods=period).mean()


def add_all(df: pd.DataFrame, fast: int = 20, slow: int = 50, long_ma: int = 0) -> pd.DataFrame:
    out = df.copy()
    out["ema_fast"] = ema(out["close"], fast)
    out["ema_slow"] = ema(out["close"], slow)
    out["rsi"] = rsi(out["close"], 14)
    out["atr"] = atr(out, 14)
    out["adx"] = adx(out, 14)
    out["vol_avg"] = sma(out["volume"], 20).shift(1)  # average of the 20 candles BEFORE this one
    if long_ma:
        out["long_ma"] = sma(out["close"], long_ma)
    return out
