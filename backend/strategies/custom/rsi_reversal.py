"""Example custom strategy: RSI extremes back inside the range, with own SL/TP."""
NAME = "RSI Reversal"
DESCRIPTION = "Buys when RSI climbs back above 30, sells when it drops back below 70. Sets its own SL/TP."


def _rsi(close, period=14):
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, float("nan"))
    return (100 - 100 / (1 + rs)).fillna(50)


def generate_signal(df, ind):
    r = _rsi(df["close"])
    prev, now = r.iloc[-2], r.iloc[-1]
    price, atr = ind["price"], ind["atr"]

    if prev < 30 <= now:
        return {"signal": "BUY", "confidence": 70,
                "reason": f"RSI recovered from oversold ({prev:.0f} -> {now:.0f})",
                "stop_loss": price - 1.2 * atr, "take_profit": price + 2.0 * atr}
    if prev > 70 >= now:
        return {"signal": "SELL", "confidence": 70,
                "reason": f"RSI fell from overbought ({prev:.0f} -> {now:.0f})",
                "stop_loss": price + 1.2 * atr, "take_profit": price - 2.0 * atr}
    return {"signal": "WAIT", "confidence": 0, "reason": f"RSI {now:.0f}, no reversal"}
