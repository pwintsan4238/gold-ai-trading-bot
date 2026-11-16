"""
One paper-trading cycle for the GitHub-hosted bot (run by GitHub Actions).

  - Fetches real gold candles (Yahoo Finance, gold futures GC=F by default).
  - Checks open paper trades against every candle since the last run (SL/TP).
  - Runs the chosen strategy -> risk manager -> opens a paper trade if allowed.
  - Saves state + a dashboard snapshot (status.json) into BOT_DATA_DIR, which
    the workflow commits to the `bot-data` branch.

Same safety gates as the PC bot: auto-trade switch, daily loss stop, max trades
per day, one open trade, confidence + reward:risk checks, fixed-% lot size.
Simulated money only: nothing here connects to a broker.
"""
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
os.environ.setdefault("DB_PATH", "/tmp/goldbot_cloud_unused.db")

import pandas as pd  # noqa: E402
import config  # noqa: E402
from indicators.indicators import compute  # noqa: E402
from risk.risk_manager import build_trade_plan  # noqa: E402
from trading.executor import _pnl  # noqa: E402
from strategies import registry  # noqa: E402

DATA = Path(os.environ.get("BOT_DATA_DIR", ROOT / "bot-data"))
SETTINGS_FILE = ROOT / "cloud" / "settings.json"
NOW = datetime.now(timezone.utc)
TODAY = NOW.date().isoformat()


def load(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def iso(ts):
    return pd.Timestamp(ts).tz_convert("UTC").isoformat() if pd.Timestamp(ts).tzinfo \
        else pd.Timestamp(ts).tz_localize("UTC").isoformat()


def fetch_candles(symbol, interval):
    if os.environ.get("BOT_FAKE_DATA"):          # for local testing only
        from data.market_data import SimProvider
        df = SimProvider(seed=int(os.environ["BOT_FAKE_DATA"]))._synth(400)
        df["time"] = pd.to_datetime(df["time"]).dt.tz_localize("UTC")
        df["time"] = df["time"] + (NOW - df["time"].iloc[-1]) - timedelta(minutes=5)
        return df, "simulator (test)"
    import yfinance as yf
    h = yf.Ticker(symbol).history(period="10d", interval=interval, auto_adjust=False)
    if h is None or h.empty:
        raise RuntimeError(f"No price data returned for {symbol}.")
    h = h.reset_index()
    tcol = "Datetime" if "Datetime" in h.columns else h.columns[0]
    df = pd.DataFrame({"time": pd.to_datetime(h[tcol], utc=True),
                       "open": h["Open"], "high": h["High"],
                       "low": h["Low"], "close": h["Close"]}).dropna()
    # the newest bar is still forming — use closed candles only
    return df.iloc[:-1].reset_index(drop=True), f"Yahoo Finance {symbol} ({interval})"


def manage_open(state, df):
    """Close trades whose SL/TP was touched by any candle after they opened."""
    closed = []
    for t in state["trades"]:
        if t["result"] != "OPEN":
            continue
        after = df[df["time"] > pd.Timestamp(t["opened_at"])]
        for _, c in after.iterrows():
            hit = None
            if t["side"] == "BUY":
                if c["low"] <= t["stop_loss"]:
                    hit = ("LOSS", t["stop_loss"])
                elif c["high"] >= t["take_profit"]:
                    hit = ("WIN", t["take_profit"])
            else:
                if c["high"] >= t["stop_loss"]:
                    hit = ("LOSS", t["stop_loss"])
                elif c["low"] <= t["take_profit"]:
                    hit = ("WIN", t["take_profit"])
            if hit:
                pnl = round(_pnl(t["side"], t["entry"], hit[1], t["lot"]), 2)
                t.update(result=hit[0], exit_price=hit[1], pnl=pnl,
                         closed_at=iso(c["time"]))
                state["balance"] = round(state["balance"] + pnl, 2)
                state["equity"].append({"ts": t["closed_at"], "balance": state["balance"]})
                closed.append(t)
                break
    return closed


def stats(trades):
    closed = [t for t in trades if t["result"] in ("WIN", "LOSS")]
    wins = [t for t in closed if t["result"] == "WIN"]
    return {
        "total_trades": len(closed), "wins": len(wins), "losses": len(closed) - len(wins),
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
        "total_pnl": round(sum(t["pnl"] for t in closed), 2),
        "today_pnl": round(sum(t["pnl"] for t in closed
                               if (t.get("closed_at") or "")[:10] == TODAY), 2),
    }


def main():
    settings = {"strategy": "rules", "auto_trade": True, "yahoo_symbol": "GC=F",
                "interval": "15m", "start_balance": 10000}
    settings.update({k: v for k, v in load(SETTINGS_FILE, {}).items() if not k.startswith("_")})
    DATA.mkdir(parents=True, exist_ok=True)
    state = load(DATA / "state.json", None) or {
        "balance": float(settings["start_balance"]), "trades": [], "equity": [],
        "last_candle": None, "halted_day": None, "halted_reason": "", "runs": 0,
        "started_at": NOW.isoformat()}
    state["runs"] = state.get("runs", 0) + 1

    strategy = settings["strategy"]
    names = {s["key"]: s["name"] for s in registry.list_strategies()}
    note = ""
    if strategy == "tradingview" or strategy not in names:
        note = f"Strategy '{strategy}' isn't available in the cloud bot — using Rule Engine."
        strategy = "rules"

    decision = {"signal": "WAIT", "confidence": 0, "trend": "NEUTRAL", "reason": "—"}
    plan = {"ok": False, "reject_reason": "No new candle yet."}
    action, price, ind, source, error, closed = "none", None, {}, "", "", []
    try:
        df, source = fetch_candles(settings["yahoo_symbol"], settings["interval"])
        if len(df) < 60:
            raise RuntimeError("Not enough candles returned.")
        closed = manage_open(state, df)
        last_t = iso(df["time"].iloc[-1])
        price = round(float(df["close"].iloc[-1]), 2)
        market_open = (NOW - pd.Timestamp(last_t)) < timedelta(minutes=90)
        ind = compute(df.tail(config.CANDLE_LOOKBACK).reset_index(drop=True))
        decision = registry.decide(strategy, df.tail(config.CANDLE_LOOKBACK)
                                   .reset_index(drop=True), ind)

        if state.get("halted_day") and state["halted_day"] != TODAY:
            state["halted_day"], state["halted_reason"] = None, ""

        today_trades = [t for t in state["trades"] if t["opened_at"][:10] == TODAY]
        realised_today = sum(t.get("pnl") or 0 for t in state["trades"]
                             if (t.get("closed_at") or "")[:10] == TODAY)
        open_now = [t for t in state["trades"] if t["result"] == "OPEN"]

        if not market_open:
            plan = {"ok": False, "reject_reason": "Market closed (no fresh candles)."}
            action = "market_closed"
        elif last_t == state.get("last_candle"):
            plan = {"ok": False, "reject_reason": "Already decided on this candle."}
        elif not settings["auto_trade"]:
            plan = {"ok": False, "reject_reason": "Auto-trading off (cloud/settings.json)."}
        elif state.get("halted_day") == TODAY:
            plan = {"ok": False, "reject_reason": state["halted_reason"]}
            action = "halted"
        elif realised_today <= -abs(state["balance"] * config.MAX_DAILY_LOSS):
            state["halted_day"] = TODAY
            state["halted_reason"] = "Daily loss limit reached — no new trades until tomorrow (UTC)."
            plan = {"ok": False, "reject_reason": state["halted_reason"]}
            action = "halted"
        elif len(today_trades) >= config.MAX_TRADES_PER_DAY:
            plan = {"ok": False, "reject_reason": "Max trades/day reached."}
            action = "limit"
        elif len(open_now) >= config.MAX_OPEN_TRADES:
            plan = {"ok": False, "reject_reason": "A trade is already open."}
            action = "holding"
        else:
            plan = build_trade_plan(decision, ind, state["balance"])
            if plan["ok"]:
                state["trades"].append({
                    "id": len(state["trades"]) + 1, "opened_at": last_t,
                    "side": plan["side"], "symbol": settings["yahoo_symbol"],
                    "entry": plan["entry"], "stop_loss": plan["stop_loss"],
                    "take_profit": plan["take_profit"], "lot": plan["lot"],
                    "confidence": decision.get("confidence"),
                    "risk_reward": plan["risk_reward"], "reason": decision.get("reason"),
                    "source": decision.get("source", strategy), "mode": "cloud-paper",
                    "result": "OPEN", "pnl": None})
                action = "opened"
            else:
                action = "rejected"
        state["last_candle"] = last_t
    except Exception as e:  # never crash the workflow; show the problem on the dashboard
        error = f"{type(e).__name__}: {e}"
        market_open = False
        last_t = state.get("last_candle")

    state["trades"] = state["trades"][-500:]
    state["equity"] = state["equity"][-1000:]
    trades_desc = list(reversed(state["trades"]))
    strategies = [s for s in registry.list_strategies() if s["key"] != "tradingview"]

    status = {
        "mode": "paper", "data_note": source or "price feed unavailable",
        "symbol": "XAUUSD",
        "timeframe": {"1m": "M1", "5m": "M5", "15m": "M15", "30m": "M30", "60m": "H1",
                      "1h": "H1"}.get(settings["interval"], "M15"),
        "tv_chart_symbol": config.TV_CHART_SYMBOL, "price": price,
        "trend": decision.get("trend", "NEUTRAL"), "decision": decision, "plan": plan,
        "action": action, "error": error or None,
        "strategy": {"key": strategy, "name": names.get(strategy, strategy)},
        "auto_trade": bool(settings["auto_trade"]),
        "halted_reason": state.get("halted_reason", "") if state.get("halted_day") == TODAY else "",
        "balance": state["balance"],
        "open_trades": [t for t in state["trades"] if t["result"] == "OPEN"],
        "stats": stats(state["trades"]), "ea": {"running": None},
        "config": {"risk_per_trade": config.RISK_PER_TRADE,
                   "max_trades_per_day": config.MAX_TRADES_PER_DAY,
                   "max_daily_loss": config.MAX_DAILY_LOSS,
                   "min_risk_reward": config.MIN_RISK_REWARD,
                   "min_confidence": config.MIN_CONFIDENCE},
        "last_updated": NOW.isoformat(),
        "cloud": {"last_run": NOW.isoformat(), "last_candle": last_t,
                  "market_open": market_open, "runs": state["runs"],
                  "started_at": state.get("started_at"), "note": note,
                  "settings": settings, "closed_this_run": len(closed),
                  "start_balance": settings["start_balance"],
                  "equity": state["equity"][-200:]},
    }
    (DATA / "state.json").write_text(json.dumps(state, indent=1, default=str), encoding="utf-8")
    (DATA / "status.json").write_text(json.dumps(
        {"status": status, "trades": trades_desc[:200], "strategies": strategies},
        default=str), encoding="utf-8")
    print(f"[{NOW:%Y-%m-%d %H:%M}Z] {strategy} price={price} signal={decision.get('signal')} "
          f"action={action} balance={state['balance']} {error}")


if __name__ == "__main__":
    main()
