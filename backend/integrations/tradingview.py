"""
TradingView webhook integration.

TradingView alerts POST to  <your public URL>/api/tv/webhook .
Accepted body (JSON — recommended):

  {"passphrase": "YOUR_SECRET", "action": "BUY", "symbol": "XAUUSD",
   "price": 2345.6, "sl": 2338.0, "tp": 2357.0, "confidence": 80,
   "comment": "EMA cross"}

 - action: BUY / SELL / LONG / SHORT / CLOSE / EXIT / FLAT  (case-insensitive)
   Strategy alerts can send {{strategy.order.action}} ("buy"/"sell").
 - sl / tp / price / confidence / comment / symbol are optional.

Plain-text bodies like "BUY XAUUSD passphrase=SECRET" also work, but JSON is
safer. Every alert must carry the passphrase or it is rejected.
"""
import hmac
import json
import re
import secrets
from pathlib import Path

import config
import db.database as db

ACTIONS = {"BUY": "BUY", "LONG": "BUY", "SELL": "SELL", "SHORT": "SELL",
           "CLOSE": "CLOSE", "EXIT": "CLOSE", "FLAT": "CLOSE", "CLOSE_ALL": "CLOSE"}

PINE_FILE = Path(__file__).resolve().parents[2] / "tradingview" / "goldbot_alerts.pine"


class WebhookError(Exception):
    pass


def get_passphrase() -> str:
    if config.TV_PASSPHRASE:
        return config.TV_PASSPHRASE
    p = db.get_state("tv_passphrase")
    if not p:
        p = secrets.token_urlsafe(18)
        db.set_state("tv_passphrase", p)
    return p


def regenerate_passphrase() -> str:
    if config.TV_PASSPHRASE:
        raise WebhookError("Passphrase is fixed by TV_PASSPHRASE in .env — edit it there.")
    p = secrets.token_urlsafe(18)
    db.set_state("tv_passphrase", p)
    return p


def _num(v):
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def parse(body: bytes) -> dict:
    """Parse + authenticate a webhook body. Returns a normalized signal dict."""
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        raise WebhookError("Empty body.")
    data = None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        pass

    if isinstance(data, dict):
        d = {str(k).lower(): v for k, v in data.items()}
        pw = str(d.get("passphrase") or d.get("secret") or d.get("key") or "")
        action = str(d.get("action") or d.get("signal") or d.get("side") or "")
        sig = {
            "symbol": str(d.get("symbol") or d.get("ticker") or "") or None,
            "price": _num(d.get("price") or d.get("close")),
            "stop_loss": _num(d.get("sl") or d.get("stop_loss") or d.get("stop")),
            "take_profit": _num(d.get("tp") or d.get("take_profit") or d.get("target")),
            "confidence": d.get("confidence"),
            "comment": str(d.get("comment") or d.get("message") or "")[:200] or None,
        }
    else:  # plain text:  "BUY XAUUSD passphrase=abc sl=1 tp=2"
        kv = dict(re.findall(r"(\w+)\s*[=:]\s*([^\s,]+)", text))
        kv = {k.lower(): v for k, v in kv.items()}
        pw = kv.get("passphrase") or kv.get("secret") or ""
        words = re.findall(r"[A-Za-z_]+", text)
        action = next((w for w in words if w.upper() in ACTIONS), "")
        sig = {"symbol": kv.get("symbol") or kv.get("ticker"),
               "price": _num(kv.get("price")), "stop_loss": _num(kv.get("sl")),
               "take_profit": _num(kv.get("tp")), "confidence": kv.get("confidence"),
               "comment": text[:200]}

    if not hmac.compare_digest(pw.encode(), get_passphrase().encode()):
        raise WebhookError("Wrong or missing passphrase.")

    act = ACTIONS.get(action.strip().upper())
    if not act:
        raise WebhookError(f"Unknown action '{action}'. Use BUY, SELL or CLOSE.")
    sig["action"] = act

    try:
        conf = int(float(sig["confidence"])) if sig["confidence"] not in (None, "") \
            else config.TV_DEFAULT_CONFIDENCE
    except (TypeError, ValueError):
        conf = config.TV_DEFAULT_CONFIDENCE
    sig["confidence"] = max(0, min(100, conf))

    sym = (sig["symbol"] or "").upper()
    if sym and config.SYMBOL[:3].upper() not in sym and "GOLD" not in sym:
        raise WebhookError(f"Alert symbol {sym} doesn't match bot symbol {config.SYMBOL}.")

    sig["source"] = "tradingview"
    sig["raw"] = text[:2000].replace(pw, "***") if pw else text[:2000]
    return sig


def pine_script() -> str:
    try:
        return PINE_FILE.read_text(encoding="utf-8")
    except Exception:
        return "// goldbot_alerts.pine not found next to the backend folder."


def alert_templates(passphrase: str) -> dict:
    return {
        "indicator_alert": "Use the GoldBot Pine script below. In the alert dialog "
                           "choose 'Any alert() function call'.",
        "strategy_alert_message": json.dumps({
            "passphrase": passphrase, "action": "{{strategy.order.action}}",
            "symbol": "{{ticker}}", "price": "{{close}}",
            "comment": "{{strategy.order.comment}}"}).replace('"{{close}}"', "{{close}}"),
        "manual_buy": json.dumps({"passphrase": passphrase, "action": "BUY",
                                  "symbol": "{{ticker}}"}),
        "manual_sell": json.dumps({"passphrase": passphrase, "action": "SELL",
                                   "symbol": "{{ticker}}"}),
        "manual_close": json.dumps({"passphrase": passphrase, "action": "CLOSE"}),
    }
