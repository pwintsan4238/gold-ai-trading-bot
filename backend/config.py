"""
Central configuration for the Gold (XAU/USD) AI trading bot.

Values are read from environment variables (see .env.example). Everything has a
safe default so the bot runs in PAPER mode out of the box with no setup.
"""
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    # python-dotenv is optional; env vars still work without it.
    pass


def _get_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _get_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = os.getenv("DB_PATH", str(BASE_DIR / "goldbot.db"))

# --- Mode -------------------------------------------------------------------
# "paper"  -> built-in simulator, no MT5 needed (DEFAULT, safe)
# "mt5"    -> connect to MetaTrader 5 terminal (Windows only). Still uses the
#             broker's DEMO account unless you point MT5 at a live account.
TRADING_MODE = os.getenv("TRADING_MODE", "paper").strip().lower()

SYMBOL = os.getenv("SYMBOL", "XAUUSD")
TIMEFRAME = os.getenv("TIMEFRAME", "M15")        # M1, M5, M15, M30, H1, H4, D1
CANDLE_LOOKBACK = _get_int("CANDLE_LOOKBACK", 300)

# --- Account / risk ---------------------------------------------------------
PAPER_START_BALANCE = _get_float("PAPER_START_BALANCE", 10000.0)
RISK_PER_TRADE = _get_float("RISK_PER_TRADE", 0.01)      # 1% of balance per trade
MAX_TRADES_PER_DAY = _get_int("MAX_TRADES_PER_DAY", 5)
MAX_DAILY_LOSS = _get_float("MAX_DAILY_LOSS", 0.03)      # 3% of balance -> stop for the day
MIN_RISK_REWARD = _get_float("MIN_RISK_REWARD", 1.5)     # reject setups below this R:R
MIN_CONFIDENCE = _get_int("MIN_CONFIDENCE", 60)          # reject AI signals below this
MAX_OPEN_TRADES = _get_int("MAX_OPEN_TRADES", 1)         # avoid unnecessary multiple trades

# Gold contract details (standard: 100 oz per 1.0 lot; $1 move = $100 per lot)
CONTRACT_SIZE = _get_float("CONTRACT_SIZE", 100.0)
MIN_LOT = _get_float("MIN_LOT", 0.01)
MAX_LOT = _get_float("MAX_LOT", 1.0)
LOT_STEP = _get_float("LOT_STEP", 0.01)

# Stop-loss / take-profit sizing (ATR-based). TP = SL * MIN_RISK_REWARD by default.
ATR_PERIOD = _get_int("ATR_PERIOD", 14)
SL_ATR_MULT = _get_float("SL_ATR_MULT", 1.5)

# --- Auto-trading -----------------------------------------------------------
# Master switch. Even when True, the bot still respects every safety limit.
AUTO_TRADE_DEFAULT = _get_bool("AUTO_TRADE_DEFAULT", False)
LOOP_INTERVAL_SEC = _get_int("LOOP_INTERVAL_SEC", 15)

# --- AI (Gemini) ------------------------------------------------------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
# If no key is set, the bot uses the built-in deterministic rule engine.

# --- Strategy ---------------------------------------------------------------
# Default signal source: ai | rules | tradingview | custom:<file_name>
# (Changeable live from the dashboard; this is only the first-run default.)
DEFAULT_STRATEGY = os.getenv("DEFAULT_STRATEGY", "ai").strip()

# --- TradingView ------------------------------------------------------------
# Secret that every TradingView webhook alert must include. Leave blank and the
# bot generates one for you (shown on the dashboard's TradingView tab).
TV_PASSPHRASE = os.getenv("TV_PASSPHRASE", "").strip()
TV_SIGNAL_MAX_AGE_SEC = _get_int("TV_SIGNAL_MAX_AGE_SEC", 300)   # ignore stale alerts
TV_DEFAULT_CONFIDENCE = _get_int("TV_DEFAULT_CONFIDENCE", 80)    # if alert has none
TV_CLOSE_ON_OPPOSITE = _get_bool("TV_CLOSE_ON_OPPOSITE", True)   # flip on reverse alert
TV_CHART_SYMBOL = os.getenv("TV_CHART_SYMBOL", "OANDA:XAUUSD")

# --- MetaTrader EAs ---------------------------------------------------------
# Paths are auto-detected on Windows; set these only if detection misses them.
MT5_TERMINAL_PATH = os.getenv("MT5_TERMINAL_PATH", "").strip()
MT5_METAEDITOR_PATH = os.getenv("MT5_METAEDITOR_PATH", "").strip()
MT5_DATA_PATH = os.getenv("MT5_DATA_PATH", "").strip()
MT4_TERMINAL_PATH = os.getenv("MT4_TERMINAL_PATH", "").strip()
MT4_METAEDITOR_PATH = os.getenv("MT4_METAEDITOR_PATH", "").strip()
MT4_DATA_PATH = os.getenv("MT4_DATA_PATH", "").strip()
# EAs refuse to start on a LIVE (real-money) MT5 account unless this is true.
ALLOW_LIVE_ACCOUNT = _get_bool("ALLOW_LIVE_ACCOUNT", False)
# Guardian: while an EA runs, watch account equity and close everything +
# stop the terminal if the day's loss exceeds MAX_DAILY_LOSS (needs MetaTrader5 pkg).
EA_GUARDIAN = _get_bool("EA_GUARDIAN", True)

# --- Server -----------------------------------------------------------------
HOST = os.getenv("HOST", "127.0.0.1")
PORT = _get_int("PORT", 8000)
