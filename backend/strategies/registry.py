"""
Strategy registry: every signal source the engine can use, behind one API.

Built-in:
  ai           Gemini analyst (falls back to rules without a key)
  rules        deterministic rule engine, no network
  tradingview  trades the alerts your TradingView charts send (webhook)

Custom (your own Python):
  Drop a file in strategies/custom/ or upload it from the dashboard. It must
  define:

      NAME = "My Strategy"                 # optional display name
      DESCRIPTION = "What it does"         # optional

      def generate_signal(df, ind):
          # df  : pandas DataFrame of candles (time, open, high, low, close)
          # ind : dict of indicators (price, ema20, ema50, rsi, macd_hist,
          #       atr, support, resistance, ...)
          return {"signal": "BUY" | "SELL" | "WAIT",
                  "confidence": 0-100,
                  "reason": "why",
                  # optional — otherwise the risk manager sizes them from ATR:
                  "stop_loss": 1234.5, "take_profit": 1250.0}

Custom code runs inside the bot with your user's permissions. Only load files
you wrote or fully trust.
"""
import importlib.util
import re
import sys
import traceback
from pathlib import Path

import config
import db.database as db
from ai.analyzer import analyze, _rule_engine

CUSTOM_DIR = Path(__file__).resolve().parent / "custom"
CUSTOM_DIR.mkdir(parents=True, exist_ok=True)
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}\.py$")

BUILTINS = {
    "ai": {"name": "AI Analyst", "description":
           "Gemini reads the indicators (built-in rules if no API key).",
           "backtestable": True},
    "rules": {"name": "Rule Engine", "description":
              "EMA trend + MACD + RSI alignment. Deterministic, no network.",
              "backtestable": True},
    "tradingview": {"name": "TradingView Alerts", "description":
                    "Trades the BUY/SELL/CLOSE alerts your TradingView charts "
                    "send to the webhook.", "backtestable": False},
}

_cache = {}  # key -> (mtime, module)


class StrategyError(Exception):
    pass


# --- loading ----------------------------------------------------------------
def _load(path: Path):
    mtime = path.stat().st_mtime
    key = path.stem
    hit = _cache.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    mod_name = f"goldbot_custom_{key}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    if not callable(getattr(mod, "generate_signal", None)):
        raise StrategyError("File must define generate_signal(df, ind).")
    _cache[key] = (mtime, mod)
    return mod


def normalize(d, ind: dict) -> dict:
    if not isinstance(d, dict):
        raise StrategyError("generate_signal must return a dict.")
    sig = str(d.get("signal", "WAIT")).upper()
    sig = {"LONG": "BUY", "SHORT": "SELL"}.get(sig, sig)
    if sig not in ("BUY", "SELL", "WAIT"):
        sig = "WAIT"
    try:
        conf = int(float(d.get("confidence", 0) or 0))
    except (TypeError, ValueError):
        conf = 0
    conf = max(0, min(100, conf))
    trend = d.get("trend") or ("BULLISH" if ind.get("ema20_above_ema50") else "BEARISH")
    out = {"signal": sig, "confidence": conf if sig != "WAIT" else 0,
           "trend": str(trend).upper(), "reason": str(d.get("reason", ""))[:300]}
    for k in ("stop_loss", "take_profit"):
        if d.get(k) is not None:
            try:
                out[k] = float(d[k])
            except (TypeError, ValueError):
                pass
    return out


# --- public API -------------------------------------------------------------
def list_strategies() -> list:
    items = [{"key": k, "type": "builtin", **v} for k, v in BUILTINS.items()]
    for f in sorted(CUSTOM_DIR.glob("*.py")):
        if f.name.startswith("_"):
            continue
        entry = {"key": f"custom:{f.stem}", "type": "custom", "file": f.name,
                 "name": f.stem, "description": "", "backtestable": True}
        try:
            mod = _load(f)
            entry["name"] = getattr(mod, "NAME", f.stem)
            entry["description"] = getattr(mod, "DESCRIPTION", "")
        except Exception as e:
            entry["error"] = str(e)
        items.append(entry)
    return items


def get_active() -> str:
    key = db.get_state("active_strategy") or config.DEFAULT_STRATEGY
    if not _exists(key):
        key = "ai"
    return key


def set_active(key: str):
    if not _exists(key):
        raise StrategyError(f"Unknown strategy '{key}'.")
    db.set_state("active_strategy", key)


def _exists(key: str) -> bool:
    if key in BUILTINS:
        return True
    if key.startswith("custom:"):
        return (CUSTOM_DIR / f"{key[7:]}.py").exists()
    return False


def decide(key: str, df, ind: dict, offline=False) -> dict:
    """Run a strategy. offline=True (backtests) never calls the Gemini API."""
    if key == "ai":
        d = _rule_engine(ind) if offline else analyze(ind)
    elif key == "rules":
        d = _rule_engine(ind)
    elif key.startswith("custom:"):
        path = CUSTOM_DIR / f"{key[7:]}.py"
        try:
            d = normalize(_load(path).generate_signal(df.copy(), dict(ind)), ind)
        except Exception as e:
            d = {"signal": "WAIT", "confidence": 0, "trend": "NEUTRAL",
                 "reason": f"[strategy error: {e}]"}
    else:
        raise StrategyError(f"Strategy '{key}' can't produce signals on its own.")
    d["source"] = key
    return d


def read_custom(filename: str) -> str:
    _check_name(filename)
    p = CUSTOM_DIR / filename
    if not p.exists():
        raise StrategyError("Not found.")
    return p.read_text(encoding="utf-8")


def delete_custom(filename: str):
    _check_name(filename)
    p = CUSTOM_DIR / filename
    if p.exists():
        p.unlink()
    _cache.pop(p.stem, None)
    if db.get_state("active_strategy") == f"custom:{p.stem}":
        db.set_state("active_strategy", "ai")


def save_custom(filename: str, code: str) -> dict:
    """Validate (syntax, interface, a dry run on sample data) then save."""
    _check_name(filename)
    try:
        compile(code, filename, "exec")
    except SyntaxError as e:
        raise StrategyError(f"Syntax error line {e.lineno}: {e.msg}")
    path = CUSTOM_DIR / filename
    backup = path.read_text(encoding="utf-8") if path.exists() else None
    path.write_text(code, encoding="utf-8")
    _cache.pop(path.stem, None)
    try:
        from data.market_data import SimProvider
        from indicators.indicators import compute
        df = SimProvider(seed=7)._synth(config.CANDLE_LOOKBACK)
        ind = compute(df)
        sample = normalize(_load(path).generate_signal(df.copy(), dict(ind)), ind)
    except Exception as e:
        if backup is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(backup, encoding="utf-8")
        _cache.pop(path.stem, None)
        tb = traceback.format_exc(limit=2).strip().splitlines()[-1]
        raise StrategyError(f"Strategy failed its test run: {e or tb}")
    return {"key": f"custom:{path.stem}", "sample_decision": sample}


def _check_name(filename: str):
    if not _NAME_RE.match(filename or ""):
        raise StrategyError("File name must be lowercase letters, numbers or _ and "
                            "end in .py (e.g. my_strategy.py).")
