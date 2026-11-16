# 🥇 Gold AI Trading Bot (XAU/USD)

A simple, safety-first AI trading bot for gold. It analyses the market with
classic indicators, asks an AI (Gemini, optional) for a BUY / SELL / WAIT
decision, sizes the trade by risk, and shows everything on a clean dashboard.

> ⚠️ **Read this first.** This is **educational software**. Automated trading is
> risky and most retail bots lose money. It ships in **paper (simulator) mode**
> and auto-trading starts **OFF**. Keep it on a **demo account** until you fully
> understand it. Never risk money you can't afford to lose.

---

## What it does

- Connects to **MetaTrader 5** (Windows) for live XAU/USD data & orders — or runs
  a built-in **paper simulator** with zero setup.
- Indicators: **EMA20, EMA50, RSI14, MACD, ATR, support/resistance**.
- **AI decision** returned as strict JSON (`signal, confidence, trend, reason`).
  Uses **Gemini** if you add a key, otherwise a transparent **rule engine**.
- **Risk manager**: entry, stop-loss, take-profit, risk/reward, and **lot size
  from a fixed 1% account risk**. Rejects unclear or low-R:R setups.
- **Safety**: demo-first, backtest mode, enable/disable switch, max trades/day,
  **max daily loss auto-stop**, one trade at a time, **no martingale ever**.
- **Trade log** (SQLite) + **dashboard**: price, trend, decision, entry/SL/TP,
  open trades, today's P/L, win rate, total trades, backtester.

## How it's built

```
backend/      Python — FastAPI server, engine, indicators, AI, risk, MT5, SQLite
  strategies/   strategy registry + your custom Python strategies (custom/)
  integrations/ TradingView webhook
  ea/           EA Manager (MetaTrader Expert Advisors)
  ea_library/   your uploaded .mq5/.mq4/.ex5/.ex4/.mqh/.set files
frontend/     index.html — React + Tailwind dashboard (served by the backend)
tradingview/  goldbot_alerts.pine — ready-made TradingView alert script
```

The dashboard has four tabs: **Overview** (live TradingView chart, signal,
plan, stats, backtest, trade log), **Strategies**, **TradingView** and
**EA Manager**.

---

## Strategies — choose what drives the bot

Strategies tab → click **Use** on one:

| Strategy | What it does |
|---|---|
| AI Analyst | Gemini reads the indicators (rule engine if no key) |
| Rule Engine | EMA trend + MACD + RSI, deterministic |
| TradingView Alerts | Trades the alerts your TradingView charts send |
| EMA 9/21 Cross, RSI Reversal | Example custom Python strategies |
| *your own* | Paste / upload a `.py` with `generate_signal(df, ind)` |

Whatever the source, **every trade passes the same safety gates** (auto-trade
switch, daily-loss stop, max trades/day, one trade at a time, confidence and
reward:risk checks, fixed-% lot size). Custom strategies are test-run on sample
data before they're saved, and can be backtested from the Overview tab.

Custom strategy format:

```python
NAME = "My Strategy"
def generate_signal(df, ind):
    # return {"signal": "BUY"|"SELL"|"WAIT", "confidence": 0-100, "reason": "...",
    #         "stop_loss": optional, "take_profit": optional}
```

Custom code runs inside the bot — only use code you trust.

---

## TradingView — chart + alerts that trade

1. **Chart**: shown on the Overview tab automatically (`TV_CHART_SYMBOL`).
2. **Webhook**: TradingView needs a public HTTPS URL. Run, in a second terminal:

   ```
   cloudflared tunnel --url http://127.0.0.1:8000
   ```

   Paste the `https://….trycloudflare.com` address into the TradingView tab.
   Through the tunnel **only** `/api/tv/webhook` is reachable — the dashboard and
   every other API return 403 to the internet.
3. Copy your **passphrase** from the TradingView tab.
4. In TradingView: Pine Editor → paste `tradingview/goldbot_alerts.pine` → Add to
   chart → set the passphrase input → **Create Alert** → *Any alert() function
   call* → tick **Webhook URL** → paste your webhook URL.
   Or use your own Pine strategy with the JSON message shown on the tab.
5. Strategies tab → **Use** *TradingView Alerts*, and turn Auto-Trading ON.

Alert JSON: `{"passphrase":"…","action":"BUY|SELL|CLOSE","symbol":"XAUUSD","sl":…,"tp":…}`.
Alerts older than `TV_SIGNAL_MAX_AGE_SEC` are ignored; a reverse alert closes
the open trade first (`TV_CLOSE_ON_OPPOSITE`). Every alert and what happened to
it is listed in the alert log. Use the **Test BUY/SELL/CLOSE** buttons to try it
without TradingView. Webhook alerts need a paid TradingView plan with 2FA on.

---

## EA Manager — run MetaTrader Expert Advisors automatically (Windows)

1. Install MT4 or MT5 from your broker, log into a **DEMO** account, open it once.
2. EA Manager tab → **Detect MetaTrader** (or fill in the paths;
   data folder = MetaTrader → File → Open Data Folder).
3. **Upload files** (.mq5 / .mq4 / .ex5 / .ex4 / .mqh / .set) or paste code into
   the editor → Save. A sample `GoldBot_EMA_Example.mq5` is included.
4. **Compile** — runs MetaEditor and shows any errors.
5. **Run** — choose symbol, timeframe and optional .set preset → Start EA.
   MetaTrader restarts with the EA attached to that chart and Algo Trading on,
   and it trades automatically from then on.
6. **Test** — runs the EA in MetaTrader's Strategy Tester for a date range; the
   HTML report appears under *Tester reports*.
7. **Stop EA** closes MetaTrader (open positions stay at the broker with SL/TP).

Safety:
- EAs refuse to start on a **live** MT5 account unless `ALLOW_LIVE_ACCOUNT=true`.
  If the account type can't be read, you must tick "this is a DEMO account".
- DLL imports stay disabled.
- **Guardian** (MT5 + `pip install MetaTrader5`): while an EA runs, if account
  equity falls `MAX_DAILY_LOSS` below the day's start, it closes all positions
  and stops the terminal.
- An EA controls its own lot size and stops — the bot's 1% rule only applies to
  trades the bot itself places. Read an EA's code (or test it) before running it.

---

## Quick start (paper mode — no MT5, no API key)

1. Install Python 3.10+ from python.org (tick "Add Python to PATH").
2. Open a terminal in the `backend` folder and run:

   ```
   pip install -r requirements.txt
   ```

3. Start it:

   ```
   python app.py
   ```

4. Open **http://127.0.0.1:8000** in your browser.
5. Click **Run Backtest** to sanity-check, then flip **Auto-Trading: ON** to let
   the paper bot trade the simulator.

That's it — it works immediately on simulated gold data.

---

## Turn on the AI (Gemini) — optional

1. Get a free key at https://aistudio.google.com/apikey
2. `pip install google-generativeai`
3. Copy `.env.example` to `.env` and set `GEMINI_API_KEY=your_key`
4. Restart. Without a key it uses the built-in rule engine (same JSON output).

## Connect MetaTrader 5 (live demo data & orders — Windows only)

1. Install **MetaTrader 5** from your broker and log into a **DEMO** account.
2. In MT5: Tools → Options → Expert Advisors → enable **Algo Trading**, and make
   sure **XAUUSD** is in Market Watch.
3. `pip install MetaTrader5`
4. In `.env` set `TRADING_MODE=mt5`
5. Start MT5, then run `python app.py`. The dashboard will say "Connected to
   MetaTrader 5." If MT5 isn't reachable it falls back to the simulator and tells
   you so — nothing breaks.

---

## Safety settings (edit in `.env`)

| Setting | Default | Meaning |
|---|---|---|
| `RISK_PER_TRADE` | `0.01` | Risk 1% of balance per trade |
| `MAX_TRADES_PER_DAY` | `5` | Stop opening new trades after this |
| `MAX_DAILY_LOSS` | `0.03` | Lose 3% in a day → auto-trading stops |
| `MIN_RISK_REWARD` | `1.5` | Reject setups below this reward:risk |
| `MIN_CONFIDENCE` | `60` | Reject AI signals below this confidence |
| `MAX_OPEN_TRADES` | `1` | Never stack positions |
| `AUTO_TRADE_DEFAULT` | `false` | Auto-trading off until you turn it on |

The bot **never** increases lot size after a loss and **never** uses martingale.
Position size is always a fixed fraction of the current balance.

## Build order (how the code is organised)

1. Market data → `data/market_data.py`
2. Indicators → `indicators/indicators.py`
3. AI analysis → `ai/analyzer.py`
4. Risk management → `risk/risk_manager.py`
5. Backtesting → `backtest/backtester.py`
6. Demo/paper trading → `trading/executor.py`
7. Dashboard → `frontend/index.html` + `app.py`
8. Auto-trading engine → `core/engine.py`

## New settings (optional, in `.env`)

| Setting | Default | Meaning |
|---|---|---|
| `DEFAULT_STRATEGY` | `ai` | Strategy on first run (change it live on the dashboard) |
| `TV_PASSPHRASE` | auto | Fixed webhook secret (blank = auto-generated) |
| `TV_SIGNAL_MAX_AGE_SEC` | `300` | Ignore alerts older than this |
| `TV_DEFAULT_CONFIDENCE` | `80` | Confidence for alerts that don't send one |
| `TV_CLOSE_ON_OPPOSITE` | `true` | Reverse alert closes the open trade first |
| `TV_CHART_SYMBOL` | `OANDA:XAUUSD` | Symbol on the dashboard chart |
| `MT5_TERMINAL_PATH` etc. | auto | Override MetaTrader paths if detection fails |
| `ALLOW_LIVE_ACCOUNT` | `false` | Allow EAs on a real-money MT5 account |
| `EA_GUARDIAN` | `true` | Daily-loss guardian for running EAs |

## Honest limitations

- The simple indicator strategy is **not** proven profitable — backtests show
  both winning and losing runs depending on conditions. It's a learning scaffold.
- The paper simulator uses **synthetic** gold prices, so backtest numbers are
  illustrative, not real market performance. Feed a real CSV (columns:
  `time,open,high,low,close`) for meaningful tests.
- EAs from the internet are often over-fitted or unsafe (martingale, grid). Run
  them in the Strategy Tester and on demo first.
- Past performance never predicts future results. Keep it on demo.
