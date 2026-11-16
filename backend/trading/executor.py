"""
Trade execution + open-position management.

PaperExecutor : simulates fills and manages SL/TP against live prices. Default.
MT5Executor   : sends real orders to a MetaTrader 5 terminal (demo or live acct).

Both expose:
  open_trade(plan, decision) -> trade id
  manage(price)              -> list of trades closed by SL/TP
  close_all(price, reason)   -> list of trades closed on request
  balance()                  -> account balance used for position sizing
"""
from datetime import datetime

import config
import db.database as db


def _pnl(side, entry, exit_price, lot):
    diff = (exit_price - entry) if side == "BUY" else (entry - exit_price)
    return diff * lot * config.CONTRACT_SIZE


def _trade_row(plan, decision, mode, entry=None, ticket=None):
    return {
        "opened_at": datetime.utcnow().isoformat(),
        "side": plan["side"],
        "symbol": config.SYMBOL,
        "entry": entry if entry is not None else plan["entry"],
        "stop_loss": plan["stop_loss"],
        "take_profit": plan["take_profit"],
        "lot": plan["lot"],
        "confidence": decision.get("confidence"),
        "risk_reward": plan["risk_reward"],
        "reason": decision.get("reason"),
        "mode": mode,
        "source": decision.get("source", "ai"),
        "ticket": ticket,
    }


class PaperExecutor:
    mode = "paper"

    def balance(self) -> float:
        return db.get_balance()

    def open_trade(self, plan: dict, decision: dict) -> int:
        return db.insert_trade(_trade_row(plan, decision, self.mode))

    def _close(self, t, exit_price, result=None):
        pnl = _pnl(t["side"], t["entry"], exit_price, t["lot"])
        result = result or ("WIN" if pnl > 0 else "LOSS")
        db.close_trade(t["id"], exit_price, round(pnl, 2), result)
        db.set_balance(db.get_balance() + pnl)
        t.update(exit_price=exit_price, pnl=round(pnl, 2), result=result)
        return t

    def manage(self, price: float) -> list:
        """Close any open paper trade whose SL or TP is touched by `price`."""
        closed = []
        for t in db.open_trades(mode=self.mode):
            hit = None
            if t["side"] == "BUY":
                if price <= t["stop_loss"]:
                    hit = ("LOSS", t["stop_loss"])
                elif price >= t["take_profit"]:
                    hit = ("WIN", t["take_profit"])
            else:  # SELL
                if price >= t["stop_loss"]:
                    hit = ("LOSS", t["stop_loss"])
                elif price <= t["take_profit"]:
                    hit = ("WIN", t["take_profit"])
            if hit:
                closed.append(self._close(t, hit[1], hit[0]))
        return closed

    def close_all(self, price: float, reason: str = "") -> list:
        return [self._close(t, price) for t in db.open_trades(mode=self.mode)]


class MT5Executor:  # pragma: no cover - needs Windows/MT5
    mode = "mt5"
    MAGIC = 770077

    def __init__(self):
        import MetaTrader5 as mt5
        if not mt5.initialize():
            raise RuntimeError(f"MT5 initialize() failed: {mt5.last_error()}")
        self.mt5 = mt5

    def balance(self) -> float:
        info = self.mt5.account_info()
        return float(info.balance) if info else db.get_balance()

    def open_trade(self, plan: dict, decision: dict) -> int:
        mt5 = self.mt5
        tick = mt5.symbol_info_tick(config.SYMBOL)
        price = tick.ask if plan["side"] == "BUY" else tick.bid
        order_type = mt5.ORDER_TYPE_BUY if plan["side"] == "BUY" else mt5.ORDER_TYPE_SELL
        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": config.SYMBOL,
            "volume": plan["lot"],
            "type": order_type,
            "price": price,
            "sl": plan["stop_loss"],
            "tp": plan["take_profit"],
            "deviation": 20,
            "magic": self.MAGIC,
            "comment": "goldbot",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(req)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            code = getattr(result, "retcode", "?")
            raise RuntimeError(f"MT5 order failed: {code} {getattr(result, 'comment', '')}")
        # position identifier == ticket of the opening order
        return db.insert_trade(_trade_row(plan, decision, self.mode,
                                          entry=price, ticket=int(result.order)))

    def manage(self, price: float) -> list:
        """SL/TP live at the broker; sync positions that closed into the DB."""
        mt5 = self.mt5
        open_ids = {p.ticket for p in (mt5.positions_get(symbol=config.SYMBOL) or [])}
        closed = []
        for t in db.open_trades(mode=self.mode):
            if not t.get("ticket") or t["ticket"] in open_ids:
                continue
            deals = mt5.history_deals_get(position=t["ticket"]) or []
            if not deals:
                continue
            pnl = sum(d.profit + d.swap + d.commission for d in deals)
            exit_price = deals[-1].price
            result = "WIN" if pnl > 0 else "LOSS"
            db.close_trade(t["id"], exit_price, round(pnl, 2), result)
            t.update(exit_price=exit_price, pnl=round(pnl, 2), result=result)
            closed.append(t)
        if closed:
            db.set_balance(self.balance())
        return closed

    def close_all(self, price: float, reason: str = "") -> list:
        mt5 = self.mt5
        for p in mt5.positions_get(symbol=config.SYMBOL) or []:
            if p.magic != self.MAGIC:
                continue  # never touch positions opened by EAs or by hand
            tick = mt5.symbol_info_tick(p.symbol)
            buy = p.type == mt5.POSITION_TYPE_BUY
            mt5.order_send({
                "action": mt5.TRADE_ACTION_DEAL, "symbol": p.symbol,
                "volume": p.volume, "position": p.ticket,
                "type": mt5.ORDER_TYPE_SELL if buy else mt5.ORDER_TYPE_BUY,
                "price": tick.bid if buy else tick.ask, "deviation": 20,
                "magic": self.MAGIC, "comment": "goldbot close",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            })
        return self.manage(price)


def get_executor():
    if config.TRADING_MODE == "mt5":
        try:
            return MT5Executor()
        except Exception:
            return PaperExecutor()
    return PaperExecutor()
