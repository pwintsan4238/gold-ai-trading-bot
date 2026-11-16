"""
Risk management: turns an AI signal + indicators into a concrete, safe trade
plan, or rejects it.

Responsibilities:
  - Entry price
  - Stop Loss (ATR-based, clamped to nearby support/resistance)
  - Take Profit (R:R multiple)
  - Risk/reward ratio
  - Lot size from a FIXED % of balance (no martingale, ever)
  - Hard reject when the setup is unclear or R:R / confidence too low
"""
import math

import config


def _round_lot(lot: float) -> float:
    steps = math.floor(lot / config.LOT_STEP)
    lot = steps * config.LOT_STEP
    lot = max(config.MIN_LOT, min(config.MAX_LOT, lot))
    return round(lot, 2)


def _valid_level(val, entry, side, kind):
    """Return float(val) if it's a usable SL/TP for this side, else None."""
    try:
        v = float(val)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    below = v < entry
    if kind == "sl":
        return v if (below if side == "BUY" else not below) else None
    return v if (not below if side == "BUY" else below) else None


def build_trade_plan(decision: dict, ind: dict, balance: float) -> dict:
    """
    Return a plan dict:
      { ok: bool, reject_reason: str|None, side, entry, stop_loss,
        take_profit, lot, risk_reward, risk_amount }
    """
    signal = decision.get("signal", "WAIT")
    confidence = int(decision.get("confidence", 0) or 0)

    if signal not in ("BUY", "SELL"):
        return {"ok": False, "reject_reason": "No directional signal (WAIT)."}

    if confidence < config.MIN_CONFIDENCE:
        return {"ok": False,
                "reject_reason": f"Confidence {confidence} < min {config.MIN_CONFIDENCE}."}

    entry = float(ind["price"])
    atr = float(ind["atr"])
    if atr <= 0:
        return {"ok": False, "reject_reason": "ATR is zero; cannot size stop."}

    # A strategy / TradingView alert may supply its own levels. They are used
    # only if they sit on the correct side of entry; otherwise ATR sizing applies.
    user_sl = _valid_level(decision.get("stop_loss"), entry, signal, "sl")
    user_tp = _valid_level(decision.get("take_profit"), entry, signal, "tp")

    if user_sl is not None:
        stop_loss = user_sl
    else:
        stop_dist = config.SL_ATR_MULT * atr
        support = ind["support"]
        resistance = ind["resistance"]
        # Base stop on ATR. Use nearby structure only to TIGHTEN the stop (never
        # to blow it out), so the risk distance stays sane and R:R is preserved.
        if signal == "BUY":
            stop_loss = entry - stop_dist
            if entry > support > stop_loss:          # support just under entry
                stop_loss = support - 0.1 * atr
        else:  # SELL
            stop_loss = entry + stop_dist
            if entry < resistance < stop_loss:       # resistance just over entry
                stop_loss = resistance + 0.1 * atr

    risk_per_unit = abs(entry - stop_loss)       # final price distance to stop
    if risk_per_unit <= 0:
        return {"ok": False, "reject_reason": "Invalid stop distance."}

    # Take-profit: the supplied level, else sized from the FINAL risk distance.
    if user_tp is not None:
        take_profit = user_tp
    elif signal == "BUY":
        take_profit = entry + risk_per_unit * config.MIN_RISK_REWARD
    else:
        take_profit = entry - risk_per_unit * config.MIN_RISK_REWARD
    reward_per_unit = abs(take_profit - entry)

    risk_reward = round(reward_per_unit / risk_per_unit, 2)
    if risk_reward < config.MIN_RISK_REWARD:
        return {"ok": False,
                "reject_reason": f"R:R {risk_reward} < min {config.MIN_RISK_REWARD}."}

    # --- position sizing: fixed % risk --------------------------------------
    risk_amount = balance * config.RISK_PER_TRADE          # e.g. 1% of balance
    # loss if stop hit (per 1.0 lot) = risk_per_unit * contract_size
    loss_per_lot = risk_per_unit * config.CONTRACT_SIZE
    raw_lot = risk_amount / loss_per_lot if loss_per_lot > 0 else 0
    lot = _round_lot(raw_lot)

    # _round_lot clamps up to MIN_LOT, which would silently risk MORE than the
    # fixed %. Reject instead when the account is too small for this stop.
    if raw_lot < config.MIN_LOT:
        return {"ok": False,
                "reject_reason": "Computed lot below minimum; risk too tight for balance."}

    # actual risk after lot rounding
    actual_risk = lot * loss_per_lot

    return {
        "ok": True,
        "reject_reason": None,
        "side": signal,
        "entry": round(entry, 2),
        "stop_loss": round(stop_loss, 2),
        "take_profit": round(take_profit, 2),
        "lot": lot,
        "risk_reward": risk_reward,
        "risk_amount": round(actual_risk, 2),
    }
