"""
Technical indicators computed with pandas/numpy (no TA-Lib needed).
EMA20, EMA50, RSI14, MACD, ATR, and simple support/resistance.
"""
import numpy as np
import pandas as pd

import config


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50)


def macd(series: pd.Series, fast=12, slow=26, signal=9):
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def support_resistance(df: pd.DataFrame, lookback: int = 50):
    """Nearest swing low (support) and swing high (resistance) in the window."""
    window = df.tail(lookback)
    support = float(window["low"].min())
    resistance = float(window["high"].max())
    return support, resistance


def compute(df: pd.DataFrame) -> dict:
    """Return the latest indicator snapshot + light-weight features for the AI."""
    close = df["close"]
    e20 = ema(close, 20)
    e50 = ema(close, 50)
    r = rsi(close, config.ATR_PERIOD if False else 14)
    macd_line, signal_line, hist = macd(close)
    a = atr(df, config.ATR_PERIOD)
    support, resistance = support_resistance(df)

    price = float(close.iloc[-1])
    snap = {
        "price": price,
        "ema20": float(e20.iloc[-1]),
        "ema50": float(e50.iloc[-1]),
        "rsi": float(r.iloc[-1]),
        "macd": float(macd_line.iloc[-1]),
        "macd_signal": float(signal_line.iloc[-1]),
        "macd_hist": float(hist.iloc[-1]),
        "atr": float(a.iloc[-1]),
        "support": support,
        "resistance": resistance,
        # trend helpers
        "ema20_above_ema50": bool(e20.iloc[-1] > e50.iloc[-1]),
        "macd_bullish": bool(hist.iloc[-1] > 0),
        "price_above_ema20": bool(price > e20.iloc[-1]),
    }
    return snap
