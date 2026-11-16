"""
Market data layer for XAU/USD.

Two providers, same interface:
  - MT5Provider  : real candles from a running MetaTrader 5 terminal (Windows).
  - SimProvider  : synthetic gold candles (random walk) OR a CSV file. Lets the
                   whole bot run with zero setup and powers backtesting.

get_provider() picks MT5 when TRADING_MODE=mt5 and the package is importable,
otherwise falls back to the simulator (and says so).
"""
import os
import math
import random
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

import config

_TF_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}


class SimProvider:
    """Synthetic or CSV-backed candles. Deterministic when seeded."""
    name = "simulator"

    def __init__(self, symbol=None, timeframe=None, csv_path=None, seed=None):
        self.symbol = symbol or config.SYMBOL
        self.timeframe = timeframe or config.TIMEFRAME
        self.csv_path = csv_path
        self._df = None
        self._cursor = None
        self._last_price = 2000.0  # starting gold price (USD/oz)
        if seed is not None:
            random.seed(seed)
            np.random.seed(seed)
        if csv_path and os.path.exists(csv_path):
            self._load_csv(csv_path)

    def _load_csv(self, path):
        df = pd.read_csv(path)
        df.columns = [c.lower() for c in df.columns]
        need = {"time", "open", "high", "low", "close"}
        if not need.issubset(df.columns):
            raise ValueError(f"CSV must have columns {need}, got {set(df.columns)}")
        df["time"] = pd.to_datetime(df["time"])
        self._df = df.reset_index(drop=True)
        self._cursor = config.CANDLE_LOOKBACK

    def _synth(self, n):
        """Generate n candles of a plausible gold random walk with volatility."""
        minutes = _TF_MINUTES.get(self.timeframe, 15)
        now = datetime.utcnow()
        rows = []
        price = self._last_price
        drift = random.uniform(-0.02, 0.02)
        for i in range(n):
            t = now - timedelta(minutes=minutes * (n - i))
            vol = price * 0.0012
            o = price
            # trending + noise
            change = np.random.normal(drift, 1.0) * vol
            c = max(500.0, o + change)
            hi = max(o, c) + abs(np.random.normal(0, 0.5)) * vol
            lo = min(o, c) - abs(np.random.normal(0, 0.5)) * vol
            rows.append((t, o, hi, lo, c))
            price = c
        self._last_price = price
        return pd.DataFrame(rows, columns=["time", "open", "high", "low", "close"])

    def get_candles(self, count=None) -> pd.DataFrame:
        count = count or config.CANDLE_LOOKBACK
        if self._df is not None:
            # replay CSV up to cursor
            end = min(self._cursor, len(self._df))
            start = max(0, end - count)
            return self._df.iloc[start:end].reset_index(drop=True)
        return self._synth(count)

    def advance(self) -> bool:
        """For backtesting over a CSV: step one candle forward."""
        if self._df is None:
            return True  # synthetic is always 'live'
        if self._cursor < len(self._df):
            self._cursor += 1
            return True
        return False

    def get_price(self) -> float:
        df = self.get_candles(2)
        return float(df["close"].iloc[-1])


class MT5Provider:
    """Live candles from a running MetaTrader 5 terminal."""
    name = "mt5"

    def __init__(self, symbol=None, timeframe=None):
        import MetaTrader5 as mt5  # noqa: Windows-only, imported lazily
        self.mt5 = mt5
        self.symbol = symbol or config.SYMBOL
        self.timeframe = timeframe or config.TIMEFRAME
        if not mt5.initialize():
            raise RuntimeError(f"MT5 initialize() failed: {mt5.last_error()}")
        if not mt5.symbol_select(self.symbol, True):
            raise RuntimeError(f"Symbol {self.symbol} not found in Market Watch")
        self._tf = {
            "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5,
            "M15": mt5.TIMEFRAME_M15, "M30": mt5.TIMEFRAME_M30,
            "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4, "D1": mt5.TIMEFRAME_D1,
        }[self.timeframe]

    def get_candles(self, count=None) -> pd.DataFrame:
        count = count or config.CANDLE_LOOKBACK
        rates = self.mt5.copy_rates_from_pos(self.symbol, self._tf, 0, count)
        if rates is None or len(rates) == 0:
            raise RuntimeError(f"No rates from MT5: {self.mt5.last_error()}")
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        return df[["time", "open", "high", "low", "close"]]

    def get_price(self) -> float:
        tick = self.mt5.symbol_info_tick(self.symbol)
        if tick is None:
            return self.get_candles(2)["close"].iloc[-1]
        return float((tick.bid + tick.ask) / 2)

    def advance(self) -> bool:
        return True


def get_provider(force_sim=False, csv_path=None, seed=None):
    """Return (provider, note). Falls back to the simulator gracefully."""
    if not force_sim and config.TRADING_MODE == "mt5":
        try:
            return MT5Provider(), "Connected to MetaTrader 5."
        except Exception as e:  # pragma: no cover - needs Windows/MT5
            return (SimProvider(csv_path=csv_path, seed=seed),
                    f"MT5 unavailable ({e}); using simulator.")
    return SimProvider(csv_path=csv_path, seed=seed), "Paper simulator active."
