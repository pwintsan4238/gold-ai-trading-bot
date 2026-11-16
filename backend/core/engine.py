"""
The trading engine: one decision cycle + a background loop.

Signal source = the active strategy (AI, rules, TradingView alerts or your own
custom Python strategy). Whatever the source, every trade passes the same
safety gates, in order:
  1. Auto-trading master switch must be ON.
  2. Daily loss limit not reached (else halt for the day).
  3. Trades-per-day cap not reached.
  4. No more than MAX_OPEN_TRADES open.
  5. Risk manager must approve a clean plan (confidence + R:R).
Lot size is ALWAYS a fixed % of balance — never increased after a loss.

The loop also runs the EA guardian when a MetaTrader EA is running.
"""
import json
import threading
from datetime import date, datetime

import config
import db.database as db
from data.market_data import get_provider
from indicators.indicators import compute
from risk.risk_manager import build_trade_plan
from trading.executor import get_executor
from strategies import registry
from ea import manager as ea


class Engine:
    def __init__(self):
        db.init_db()
        self.provider, self.data_note = get_provider()
        self.executor = get_executor()
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self.last_cycle = {}
        self._day = date.today().isoformat()

    # --- daily counters -----------------------------------------------------
    def _reset_day_if_needed(self):
        today = date.today().isoformat()
        if today != self._day:
            self._day = today
            db.set_state("trading_halted_reason", "")

    def _daily_loss_hit(self) -> bool:
        realised = sum((t["pnl"] or 0) for t in db.trades_today(mode=self.executor.mode)
                       if t["result"] in ("WIN", "LOSS", "CLOSED"))
        limit = -abs(self.executor.balance() * config.MAX_DAILY_LOSS)
        return realised <= limit

    def _trades_today_count(self) -> int:
        return len(db.trades_today(mode=self.executor.mode))

    # --- signal -------------------------------------------------------------
    def _tradingview_decision(self, ind, price, closed):
        """Turn the newest fresh TradingView alert into a decision."""
        trend = "BULLISH" if ind["ema20_above_ema50"] else "BEARISH"
        sig = db.next_pending_signal(config.TV_SIGNAL_MAX_AGE_SEC)
        if not sig:
            return None, {"signal": "WAIT", "confidence": 0, "trend": trend,
                          "source": "tradingview",
                          "reason": "Waiting for a TradingView alert."}
        if sig["action"] == "CLOSE":
            n = self.executor.close_all(price, "TradingView CLOSE")
            closed.extend(n)
            db.update_signal(sig["id"], "executed", f"Closed {len(n)} trade(s).")
            return None, {"signal": "WAIT", "confidence": 0, "trend": trend,
                          "source": "tradingview",
                          "reason": f"TradingView CLOSE alert — closed {len(n)} trade(s)."}

        if config.TV_CLOSE_ON_OPPOSITE and db.get_auto_trade():
            opposite = [t for t in db.open_trades(mode=self.executor.mode)
                        if t["side"] != sig["action"]]
            if opposite:
                closed.extend(self.executor.close_all(price, "Reverse TradingView alert"))
        reason = f"TradingView {sig['action']}" + (f": {sig['comment']}" if sig["comment"] else "")
        return sig, {"signal": sig["action"], "confidence": sig["confidence"],
                     "trend": trend, "reason": reason, "source": "tradingview",
                     "stop_loss": sig["stop_loss"], "take_profit": sig["take_profit"]}

    # --- one cycle ----------------------------------------------------------
    def run_cycle(self, trigger: str = "timer") -> dict:
        with self._lock:
            return self._run_cycle(trigger)

    def _run_cycle(self, trigger) -> dict:
        self._reset_day_if_needed()
        df = self.provider.get_candles()
        ind = compute(df)
        price = ind["price"]

        # 1. manage existing trades first (SL/TP)
        closed = self.executor.manage(price)

        # 2. get a signal from the active strategy
        strategy = registry.get_active()
        sig = None
        if strategy == "tradingview":
            sig, decision = self._tradingview_decision(ind, price, closed)
        else:
            decision = registry.decide(strategy, df, ind)
        db.set_state("last_decision", json.dumps(decision))

        action = "none"
        plan = {"ok": False, "reject_reason": "Auto-trading off."}
        auto = db.get_auto_trade()
        halted_reason = ""

        if auto:
            if self._daily_loss_hit():
                db.set_auto_trade(False)
                halted_reason = "Daily loss limit reached — auto-trading stopped."
                db.set_state("trading_halted_reason", halted_reason)
                action = "halted"
                plan = {"ok": False, "reject_reason": halted_reason}
            elif self._trades_today_count() >= config.MAX_TRADES_PER_DAY:
                plan = {"ok": False, "reject_reason": "Max trades/day reached."}
                action = "limit"
            elif len(db.open_trades(mode=self.executor.mode)) >= config.MAX_OPEN_TRADES:
                plan = {"ok": False, "reject_reason": "A trade is already open."}
                action = "holding"
            else:
                plan = build_trade_plan(decision, ind, self.executor.balance())
                if plan["ok"]:
                    try:
                        self.executor.open_trade(plan, decision)
                        action = "opened"
                    except Exception as e:
                        plan = {"ok": False, "reject_reason": f"Order failed: {e}"}
                        action = "error"
                else:
                    action = "rejected"

        if sig:  # record what happened to the TradingView alert
            if action == "opened":
                db.update_signal(sig["id"], "executed",
                                 f"{plan['side']} {plan['lot']} lot @ {plan['entry']}")
            elif not auto:
                db.update_signal(sig["id"], "ignored", "Auto-trading is OFF.")
            else:
                db.update_signal(sig["id"], "rejected", plan.get("reject_reason") or action)

        cycle = {
            "ts": datetime.utcnow().isoformat(),
            "trigger": trigger,
            "strategy": strategy,
            "price": round(price, 2),
            "indicators": ind,
            "decision": decision,
            "plan": plan,
            "action": action,
            "closed": closed,
            "halted_reason": halted_reason or db.get_state("trading_halted_reason", ""),
            "mode": self.executor.mode,
            "data_note": self.data_note,
        }
        self.last_cycle = cycle
        return cycle

    # --- background loop ----------------------------------------------------
    def _loop(self):
        while not self._stop.is_set():
            try:
                self.run_cycle()
            except Exception as e:
                self.last_cycle = {"error": str(e), "ts": datetime.utcnow().isoformat()}
            try:
                g = ea.guardian_check()
                db.set_json("ea_guardian_status", g)
            except Exception as e:
                db.set_json("ea_guardian_status", {"active": False, "note": f"error: {e}"})
            self._stop.wait(config.LOOP_INTERVAL_SEC)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
