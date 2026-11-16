"""
Backtesting mode: replay historical (or synthetic) candles through the exact
same indicator -> decision -> risk pipeline the live bot uses, and report stats.

Uses the deterministic rule engine (no API calls) so results are reproducible.
One position at a time, matching the live MAX_OPEN_TRADES=1 default.
"""
import config
from indicators.indicators import compute
from strategies import registry
from risk.risk_manager import build_trade_plan
from trading.executor import _pnl
from data.market_data import SimProvider


def run_backtest(csv_path=None, candles=1500, seed=42,
                 start_balance=None, lookback=None, strategy="ai"):
    if strategy == "tradingview":
        raise ValueError("TradingView alerts can't be backtested here — use "
                         "TradingView's own Strategy Tester on your Pine strategy.")
    lookback = lookback or config.CANDLE_LOOKBACK
    balance = start_balance if start_balance is not None else config.PAPER_START_BALANCE

    # Build one long synthetic (or CSV) series, then slide a window across it.
    if csv_path:
        base = SimProvider(csv_path=csv_path)
        full = base._df
    else:
        base = SimProvider(seed=seed)
        full = base._synth(candles)

    trades = []
    open_t = None
    equity_curve = [balance]
    peak = balance
    max_dd = 0.0

    for i in range(lookback, len(full)):
        window = full.iloc[i - lookback:i].reset_index(drop=True)
        price = float(window["close"].iloc[-1])

        # manage open trade against this candle's high/low
        if open_t:
            hi = float(full["high"].iloc[i])
            lo = float(full["low"].iloc[i])
            result = exit_price = None
            if open_t["side"] == "BUY":
                if lo <= open_t["stop_loss"]:
                    result, exit_price = "LOSS", open_t["stop_loss"]
                elif hi >= open_t["take_profit"]:
                    result, exit_price = "WIN", open_t["take_profit"]
            else:
                if hi >= open_t["stop_loss"]:
                    result, exit_price = "LOSS", open_t["stop_loss"]
                elif lo <= open_t["take_profit"]:
                    result, exit_price = "WIN", open_t["take_profit"]
            if result:
                pnl = _pnl(open_t["side"], open_t["entry"], exit_price, open_t["lot"])
                balance += pnl
                open_t.update(result=result, exit_price=round(exit_price, 2),
                              pnl=round(pnl, 2))
                trades.append(open_t)
                open_t = None
                equity_curve.append(balance)
                peak = max(peak, balance)
                max_dd = max(max_dd, (peak - balance) / peak * 100 if peak else 0)

        # look for a new entry only when flat
        if not open_t:
            ind = compute(window)
            # offline=True: the AI strategy uses its rule fallback (no API calls)
            decision = registry.decide(strategy, window, ind, offline=True)
            plan = build_trade_plan(decision, ind, balance)
            if plan["ok"]:
                open_t = {
                    "side": plan["side"], "entry": plan["entry"],
                    "stop_loss": plan["stop_loss"], "take_profit": plan["take_profit"],
                    "lot": plan["lot"], "risk_reward": plan["risk_reward"],
                    "confidence": decision["confidence"], "reason": decision["reason"],
                }

    closed = [t for t in trades]
    wins = [t for t in closed if t["result"] == "WIN"]
    total_pnl = sum(t["pnl"] for t in closed)
    return {
        "strategy": strategy,
        "candles_tested": len(full) - lookback,
        "start_balance": round(start_balance if start_balance is not None
                               else config.PAPER_START_BALANCE, 2),
        "end_balance": round(balance, 2),
        "total_trades": len(closed),
        "wins": len(wins),
        "losses": len(closed) - len(wins),
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
        "total_pnl": round(total_pnl, 2),
        "return_pct": round((balance / (start_balance if start_balance is not None
                            else config.PAPER_START_BALANCE) - 1) * 100, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "trades": closed[-50:],
    }


if __name__ == "__main__":
    import json
    print(json.dumps(run_backtest(), indent=2, default=str)[:2000])
