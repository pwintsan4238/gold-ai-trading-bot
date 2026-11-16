"""Example custom strategy: trade fresh EMA 9 / EMA 21 crossovers."""
NAME = "EMA 9/21 Cross"
DESCRIPTION = "Buys a fresh EMA9-over-EMA21 cross in an EMA50 uptrend; sells the mirror."


def generate_signal(df, ind):
    close = df["close"]
    fast = close.ewm(span=9, adjust=False).mean()
    slow = close.ewm(span=21, adjust=False).mean()
    crossed_up = fast.iloc[-2] <= slow.iloc[-2] and fast.iloc[-1] > slow.iloc[-1]
    crossed_dn = fast.iloc[-2] >= slow.iloc[-2] and fast.iloc[-1] < slow.iloc[-1]

    if crossed_up and ind["price"] > ind["ema50"] and ind["rsi"] < 70:
        return {"signal": "BUY", "confidence": 75,
                "reason": "EMA9 crossed above EMA21, price above EMA50"}
    if crossed_dn and ind["price"] < ind["ema50"] and ind["rsi"] > 30:
        return {"signal": "SELL", "confidence": 75,
                "reason": "EMA9 crossed below EMA21, price below EMA50"}
    return {"signal": "WAIT", "confidence": 0, "reason": "No fresh crossover"}
