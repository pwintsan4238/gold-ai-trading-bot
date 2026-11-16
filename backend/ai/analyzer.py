"""
AI market analysis.

Primary: Google Gemini (if GEMINI_API_KEY is set) — reads the indicator snapshot
and returns a strict JSON decision.

Fallback: a deterministic rule engine that returns the SAME JSON schema, so the
bot (and the backtester) works with no API key and no network.

Decision schema:
{
  "signal": "BUY" | "SELL" | "WAIT",
  "confidence": 0-100,
  "trend": "BULLISH" | "BEARISH" | "NEUTRAL",
  "reason": "..."
}
(entry/stop_loss/take_profit/risk_reward are filled by the risk manager.)
"""
import json

import config


SYSTEM_PROMPT = """You are a disciplined gold (XAU/USD) trading analyst.
You are given technical indicators for the latest candle. Decide ONE action.
Rules:
- Only return BUY or SELL when EMA trend, MACD and RSI agree and the setup is clean.
- If signals conflict or the move looks overextended, return WAIT.
- Be conservative. A missed trade is better than a bad trade.
Return ONLY compact JSON with keys: signal, confidence (0-100), trend, reason.
No markdown, no extra text."""


def _rule_engine(ind: dict) -> dict:
    """Transparent, deterministic baseline analyst."""
    score = 0
    reasons = []

    # Trend via EMAs
    if ind["ema20_above_ema50"]:
        score += 1; reasons.append("EMA20>EMA50 (uptrend)")
    else:
        score -= 1; reasons.append("EMA20<EMA50 (downtrend)")

    # Price vs EMA20
    if ind["price_above_ema20"]:
        score += 1
    else:
        score -= 1

    # MACD momentum
    if ind["macd_hist"] > 0:
        score += 1; reasons.append("MACD bullish")
    else:
        score -= 1; reasons.append("MACD bearish")

    # RSI
    rsi = ind["rsi"]
    if rsi > 70:
        score -= 1; reasons.append(f"RSI {rsi:.0f} overbought")
    elif rsi < 30:
        score += 1; reasons.append(f"RSI {rsi:.0f} oversold")
    elif 45 <= rsi <= 65:
        reasons.append(f"RSI {rsi:.0f} neutral-healthy")

    abs_score = abs(score)
    confidence = min(95, 50 + abs_score * 12)

    if score >= 3:
        signal, trend = "BUY", "BULLISH"
    elif score <= -3:
        signal, trend = "SELL", "BEARISH"
    else:
        signal = "WAIT"
        trend = "BULLISH" if score > 0 else ("BEARISH" if score < 0 else "NEUTRAL")
        confidence = 0
        reasons.append("signals not aligned")

    return {
        "signal": signal,
        "confidence": int(confidence),
        "trend": trend,
        "reason": ", ".join(reasons[:4]) or "No clear setup.",
    }


def _gemini_engine(ind: dict) -> dict:
    import google.generativeai as genai
    genai.configure(api_key=config.GEMINI_API_KEY)
    model = genai.GenerativeModel(config.GEMINI_MODEL,
                                  system_instruction=SYSTEM_PROMPT)
    payload = {k: (round(v, 3) if isinstance(v, float) else v)
               for k, v in ind.items()}
    resp = model.generate_content(
        "Indicators:\n" + json.dumps(payload) +
        "\nReturn the JSON decision now.",
        generation_config={"temperature": 0.2, "response_mime_type": "application/json"},
    )
    text = (resp.text or "").strip()
    data = json.loads(text)
    # normalise / validate
    sig = str(data.get("signal", "WAIT")).upper()
    if sig not in ("BUY", "SELL", "WAIT"):
        sig = "WAIT"
    return {
        "signal": sig,
        "confidence": int(data.get("confidence", 0) or 0),
        "trend": str(data.get("trend", "NEUTRAL")).upper(),
        "reason": str(data.get("reason", ""))[:300],
    }


def analyze(indicators: dict) -> dict:
    """Return a validated decision dict. Never raises — falls back to rules."""
    if config.GEMINI_API_KEY:
        try:
            return _gemini_engine(indicators)
        except Exception as e:
            d = _rule_engine(indicators)
            d["reason"] = f"[AI fallback: {e}] " + d["reason"]
            return d
    return _rule_engine(indicators)
