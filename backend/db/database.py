"""
SQLite persistence: trades, bot state, and equity snapshots.
Single-file DB, no server needed.
"""
import sqlite3
import json
import threading
from datetime import datetime, date
from contextlib import contextmanager

import config

_lock = threading.Lock()


@contextmanager
def _conn():
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with _lock, _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                opened_at   TEXT NOT NULL,
                closed_at   TEXT,
                side        TEXT NOT NULL,          -- BUY / SELL
                symbol      TEXT NOT NULL,
                entry       REAL NOT NULL,
                stop_loss   REAL NOT NULL,
                take_profit REAL NOT NULL,
                lot         REAL NOT NULL,
                exit_price  REAL,
                pnl         REAL,                   -- account currency
                result      TEXT,                   -- WIN / LOSS / OPEN / CLOSED
                confidence  INTEGER,
                risk_reward REAL,
                reason      TEXT,
                mode        TEXT                    -- paper / mt5 / backtest
            );

            CREATE TABLE IF NOT EXISTS state (
                key   TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS equity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts      TEXT NOT NULL,
                balance REAL NOT NULL
            );

            -- incoming external signals (TradingView webhooks, manual tests)
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                received_at  TEXT NOT NULL,
                source       TEXT NOT NULL,         -- tradingview / test
                action       TEXT NOT NULL,         -- BUY / SELL / CLOSE
                symbol       TEXT,
                price        REAL,
                stop_loss    REAL,
                take_profit  REAL,
                confidence   INTEGER,
                comment      TEXT,
                raw          TEXT,
                status       TEXT NOT NULL,         -- pending/executed/rejected/ignored/expired
                note         TEXT,
                processed_at TEXT
            );
            """
        )
        # migrations for databases created by the first version
        _ensure_column(c, "trades", "source", "TEXT")
        _ensure_column(c, "trades", "ticket", "INTEGER")
        # Seed default state
        defaults = {
            "auto_trade": "1" if config.AUTO_TRADE_DEFAULT else "0",
            "balance": str(config.PAPER_START_BALANCE),
            "trading_halted_reason": "",
            "last_decision": json.dumps({"signal": "WAIT", "confidence": 0,
                                         "reason": "Bot just started."}),
        }
        for k, v in defaults.items():
            c.execute("INSERT OR IGNORE INTO state(key, value) VALUES (?, ?)", (k, v))


def _ensure_column(c, table, col, ctype):
    cols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
    if col not in cols:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ctype}")


# --- state helpers ----------------------------------------------------------
def get_state(key, default=None):
    with _conn() as c:
        row = c.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_state(key, value):
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO state(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


def get_json(key, default=None):
    raw = get_state(key)
    if raw is None or raw == "":
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def set_json(key, value):
    set_state(key, json.dumps(value))


def get_balance() -> float:
    return float(get_state("balance", config.PAPER_START_BALANCE))


def set_balance(bal: float):
    set_state("balance", f"{bal:.2f}")
    with _lock, _conn() as c:
        c.execute("INSERT INTO equity(ts, balance) VALUES(?,?)",
                  (datetime.utcnow().isoformat(), bal))


def get_auto_trade() -> bool:
    return get_state("auto_trade", "0") == "1"


def set_auto_trade(enabled: bool):
    set_state("auto_trade", "1" if enabled else "0")


# --- trade helpers ----------------------------------------------------------
def insert_trade(t: dict) -> int:
    with _lock, _conn() as c:
        cur = c.execute(
            """INSERT INTO trades
               (opened_at, side, symbol, entry, stop_loss, take_profit, lot,
                result, confidence, risk_reward, reason, mode, source, ticket)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (t["opened_at"], t["side"], t["symbol"], t["entry"], t["stop_loss"],
             t["take_profit"], t["lot"], "OPEN", t.get("confidence"),
             t.get("risk_reward"), t.get("reason"), t.get("mode"),
             t.get("source"), t.get("ticket")),
        )
        return cur.lastrowid


def close_trade(trade_id: int, exit_price: float, pnl: float, result: str):
    with _lock, _conn() as c:
        c.execute(
            """UPDATE trades SET closed_at=?, exit_price=?, pnl=?, result=?
               WHERE id=?""",
            (datetime.utcnow().isoformat(), exit_price, pnl, result, trade_id),
        )


def open_trades(mode=None):
    q = "SELECT * FROM trades WHERE result='OPEN'"
    args = ()
    if mode:
        q += " AND mode=?"
        args = (mode,)
    with _conn() as c:
        return [dict(r) for r in c.execute(q, args).fetchall()]


def recent_trades(limit=100, mode=None):
    q = "SELECT * FROM trades"
    args = []
    if mode:
        q += " WHERE mode=?"
        args.append(mode)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with _conn() as c:
        return [dict(r) for r in c.execute(q, args).fetchall()]


def trades_today(mode=None):
    today = date.today().isoformat()
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM trades WHERE substr(opened_at,1,10)=?", (today,)
        ).fetchall()
    rows = [dict(r) for r in rows]
    if mode:
        rows = [r for r in rows if r["mode"] == mode]
    return rows


def stats(mode=None):
    rows = recent_trades(limit=100000, mode=mode)
    closed = [r for r in rows if r["result"] in ("WIN", "LOSS", "CLOSED")]
    wins = [r for r in closed if r["result"] == "WIN"]
    total_pnl = sum((r["pnl"] or 0) for r in closed)
    win_rate = (len(wins) / len(closed) * 100) if closed else 0.0
    # today's realised pnl
    today = date.today().isoformat()
    today_pnl = sum((r["pnl"] or 0) for r in closed
                    if (r["closed_at"] or "")[:10] == today)
    return {
        "total_trades": len(closed),
        "wins": len(wins),
        "losses": len(closed) - len(wins),
        "win_rate": round(win_rate, 1),
        "total_pnl": round(total_pnl, 2),
        "today_pnl": round(today_pnl, 2),
    }


# --- external signals (TradingView) -----------------------------------------
def insert_signal(sig: dict, status="pending", note="") -> int:
    with _lock, _conn() as c:
        cur = c.execute(
            """INSERT INTO signals (received_at, source, action, symbol, price,
                 stop_loss, take_profit, confidence, comment, raw, status, note)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (datetime.utcnow().isoformat(), sig.get("source", "tradingview"),
             sig["action"], sig.get("symbol"), sig.get("price"),
             sig.get("stop_loss"), sig.get("take_profit"), sig.get("confidence"),
             sig.get("comment"), sig.get("raw"), status, note),
        )
        return cur.lastrowid


def update_signal(sig_id: int, status: str, note: str = ""):
    with _lock, _conn() as c:
        c.execute("UPDATE signals SET status=?, note=?, processed_at=? WHERE id=?",
                  (status, note, datetime.utcnow().isoformat(), sig_id))


def next_pending_signal(max_age_sec: int):
    """Return the newest fresh pending signal; expire/supersede the rest."""
    now = datetime.utcnow()
    with _conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM signals WHERE status='pending' ORDER BY id DESC").fetchall()]
    chosen = None
    for r in rows:
        age = (now - datetime.fromisoformat(r["received_at"])).total_seconds()
        if age > max_age_sec:
            update_signal(r["id"], "expired", f"Older than {max_age_sec}s.")
        elif chosen is None:
            chosen = r
        else:
            update_signal(r["id"], "ignored", "Superseded by a newer alert.")
    return chosen


def recent_signals(limit=50):
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
