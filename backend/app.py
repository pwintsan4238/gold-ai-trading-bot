"""
FastAPI server: REST API + serves the dashboard + runs the engine in the
background. Start with:  python app.py   (or: uvicorn app:app)
Then open http://127.0.0.1:8000
"""
import json
import threading
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

import config
import db.database as db
from core.engine import Engine
from backtest.backtester import run_backtest
from strategies import registry
from strategies.registry import StrategyError
from integrations import tradingview as tv
from integrations.tradingview import WebhookError
from ea import manager as ea
from ea.manager import EAError

app = FastAPI(title="Gold AI Trading Bot")

engine = Engine()
FRONTEND = Path(__file__).resolve().parent.parent / "frontend" / "index.html"
WEBHOOK_PATH = "/api/tv/webhook"
_LOCAL = {"127.0.0.1", "::1", "localhost", "testclient"}
_FWD = ("cf-connecting-ip", "x-forwarded-for", "x-real-ip", "forwarded")


@app.middleware("http")
async def remote_guard(request: Request, call_next):
    """
    When you expose the bot with a tunnel (cloudflared / ngrok) so TradingView
    can reach the webhook, ONLY the webhook is reachable from the internet.
    The dashboard and every other API stay local-only.
    """
    client = request.client.host if request.client else ""
    remote = client not in _LOCAL or any(h in request.headers for h in _FWD)
    if remote and not (request.url.path == WEBHOOK_PATH and request.method == "POST"):
        return JSONResponse({"detail": "Not available remotely."}, status_code=403)
    return await call_next(request)


def _err(e: Exception, code=400):
    raise HTTPException(status_code=code, detail=str(e))


@app.on_event("startup")
def _startup():
    tv.get_passphrase()          # make sure one exists
    try:
        ea.auto_configure()      # find MetaTrader on Windows
    except Exception:
        pass
    engine.start()


@app.get("/")
def index():
    return FileResponse(str(FRONTEND))


# =============================================================================
# Core
# =============================================================================
@app.get("/api/status")
def status():
    last = engine.last_cycle or {}
    decision = json.loads(db.get_state("last_decision", "{}"))
    s = db.stats(mode=engine.executor.mode)
    active = registry.get_active()
    names = {x["key"]: x["name"] for x in registry.list_strategies()}
    return {
        "mode": engine.executor.mode,
        "data_note": engine.data_note,
        "symbol": config.SYMBOL,
        "timeframe": config.TIMEFRAME,
        "tv_chart_symbol": config.TV_CHART_SYMBOL,
        "price": last.get("price"),
        "trend": decision.get("trend", "NEUTRAL"),
        "decision": decision,
        "plan": last.get("plan"),
        "action": last.get("action"),
        "error": last.get("error"),
        "strategy": {"key": active, "name": names.get(active, active)},
        "auto_trade": db.get_auto_trade(),
        "halted_reason": db.get_state("trading_halted_reason", ""),
        "balance": round(engine.executor.balance(), 2),
        "open_trades": db.open_trades(mode=engine.executor.mode),
        "stats": s,
        "ea": ea.status(),
        "config": {
            "risk_per_trade": config.RISK_PER_TRADE,
            "max_trades_per_day": config.MAX_TRADES_PER_DAY,
            "max_daily_loss": config.MAX_DAILY_LOSS,
            "min_risk_reward": config.MIN_RISK_REWARD,
            "min_confidence": config.MIN_CONFIDENCE,
        },
        "last_updated": last.get("ts"),
    }


@app.get("/api/trades")
def trades(limit: int = 100):
    return db.recent_trades(limit=limit, mode=engine.executor.mode)


class AutoBody(BaseModel):
    enabled: bool


@app.post("/api/autotrade")
def set_auto(body: AutoBody):
    db.set_auto_trade(body.enabled)
    if body.enabled:
        db.set_state("trading_halted_reason", "")
    return {"auto_trade": db.get_auto_trade()}


@app.post("/api/analyze")
def analyze_now():
    return engine.run_cycle(trigger="manual")


@app.post("/api/close_all")
def close_all():
    price = (engine.last_cycle or {}).get("price") or engine.provider.get_price()
    closed = engine.executor.close_all(price, "Manual close")
    return {"closed": len(closed)}


class BacktestBody(BaseModel):
    candles: int = 1500
    seed: int = 42
    start_balance: float | None = None
    strategy: str | None = None


@app.post("/api/backtest")
def backtest(body: BacktestBody):
    try:
        return run_backtest(candles=max(400, min(body.candles, 20000)), seed=body.seed,
                            start_balance=body.start_balance,
                            strategy=body.strategy or registry.get_active())
    except (ValueError, StrategyError) as e:
        _err(e)


# =============================================================================
# Strategies
# =============================================================================
@app.get("/api/strategies")
def strategies():
    return {"active": registry.get_active(), "items": registry.list_strategies()}


class KeyBody(BaseModel):
    key: str


@app.post("/api/strategies/active")
def set_strategy(body: KeyBody):
    try:
        registry.set_active(body.key)
    except StrategyError as e:
        _err(e)
    return {"active": registry.get_active()}


class CodeBody(BaseModel):
    filename: str
    code: str


@app.post("/api/strategies/custom")
def save_strategy(body: CodeBody):
    try:
        return registry.save_custom(body.filename.strip(), body.code)
    except StrategyError as e:
        _err(e)


@app.get("/api/strategies/custom/{filename}")
def get_strategy(filename: str):
    try:
        return {"filename": filename, "code": registry.read_custom(filename)}
    except StrategyError as e:
        _err(e, 404)


@app.delete("/api/strategies/custom/{filename}")
def del_strategy(filename: str):
    try:
        registry.delete_custom(filename)
    except StrategyError as e:
        _err(e)
    return {"ok": True}


# =============================================================================
# TradingView
# =============================================================================
def _kick_engine():
    if registry.get_active() == "tradingview":
        threading.Thread(target=lambda: engine.run_cycle(trigger="tradingview"),
                         daemon=True).start()


@app.post(WEBHOOK_PATH)
async def tv_webhook(request: Request):
    body = await request.body()
    try:
        sig = tv.parse(body)
    except WebhookError as e:
        # log the attempt (without trusting it) so you can debug alert formats
        db.insert_signal({"source": "tradingview", "action": "INVALID",
                          "raw": body.decode("utf-8", "replace")[:500]
                          .replace(tv.get_passphrase(), "***")},
                         status="rejected", note=str(e))
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    note = "" if registry.get_active() == "tradingview" else \
        "Received — switch the active strategy to 'TradingView Alerts' to trade it."
    sid = db.insert_signal(sig, status="pending" if not note else "ignored", note=note)
    _kick_engine()
    return {"ok": True, "id": sid}


@app.get("/api/tv/info")
def tv_info():
    p = tv.get_passphrase()
    return {"webhook_path": WEBHOOK_PATH, "passphrase": p,
            "public_url": db.get_state("public_url", ""),
            "templates": tv.alert_templates(p), "pine": tv.pine_script(),
            "signals": db.recent_signals(30),
            "max_age_sec": config.TV_SIGNAL_MAX_AGE_SEC,
            "chart_symbol": config.TV_CHART_SYMBOL}


class UrlBody(BaseModel):
    url: str


@app.post("/api/tv/public_url")
def set_public_url(body: UrlBody):
    db.set_state("public_url", body.url.strip().rstrip("/"))
    return {"public_url": db.get_state("public_url")}


@app.post("/api/tv/passphrase/regenerate")
def regen():
    try:
        return {"passphrase": tv.regenerate_passphrase()}
    except WebhookError as e:
        _err(e)


class TestSignal(BaseModel):
    action: str = "BUY"


@app.post("/api/tv/test")
def tv_test(body: TestSignal):
    """Simulate a TradingView alert locally (no TradingView needed)."""
    payload = json.dumps({"passphrase": tv.get_passphrase(), "action": body.action,
                          "symbol": config.SYMBOL, "comment": "dashboard test"})
    sig = tv.parse(payload.encode())
    sig["source"] = "test"
    note = "" if registry.get_active() == "tradingview" else \
        "Test received — switch strategy to 'TradingView Alerts' to trade it."
    sid = db.insert_signal(sig, status="pending" if not note else "ignored", note=note)
    if not note:
        engine.run_cycle(trigger="tradingview-test")
    return {"ok": True, "id": sid, "signal": [s for s in db.recent_signals(5)
                                              if s["id"] == sid][0]}


# =============================================================================
# EA manager (MetaTrader Expert Advisors)
# =============================================================================
@app.get("/api/ea")
def ea_overview():
    return {"files": ea.list_eas(), "settings": ea.get_settings(), **ea.status()}


class EAUpload(BaseModel):
    filename: str
    code: str | None = None
    b64: str | None = None


@app.post("/api/ea/upload")
def ea_upload(body: EAUpload):
    try:
        return ea.save_ea(body.filename, code=body.code, b64=body.b64)
    except EAError as e:
        _err(e)


@app.get("/api/ea/file/{filename}")
def ea_read(filename: str):
    try:
        return {"filename": filename, "code": ea.read_ea(filename)}
    except EAError as e:
        _err(e, 404)


@app.delete("/api/ea/file/{filename}")
def ea_delete(filename: str):
    try:
        ea.delete_ea(filename)
    except EAError as e:
        _err(e)
    return {"ok": True}


@app.post("/api/ea/detect")
def ea_detect():
    return ea.auto_configure()


class EASettings(BaseModel):
    platform: str
    terminal_path: str = ""
    metaeditor_path: str = ""
    data_path: str = ""
    portable: bool = False


@app.post("/api/ea/settings")
def ea_settings(body: EASettings):
    try:
        return ea.save_settings(body.platform, body.model_dump())
    except EAError as e:
        _err(e)


class EAName(BaseModel):
    name: str


@app.post("/api/ea/compile")
def ea_compile(body: EAName):
    try:
        return ea.compile_ea(body.name)
    except EAError as e:
        _err(e)


@app.post("/api/ea/deploy")
def ea_deploy(body: EAName):
    try:
        return ea.deploy(body.name)
    except EAError as e:
        _err(e)


class EARun(BaseModel):
    name: str
    symbol: str = config.SYMBOL
    period: str = config.TIMEFRAME
    preset: str | None = None
    restart: bool = False
    confirm_demo: bool = False


@app.post("/api/ea/run")
def ea_run(body: EARun):
    try:
        return ea.run_ea(body.name, body.symbol, body.period, body.preset or None,
                         body.restart, body.confirm_demo)
    except EAError as e:
        _err(e)


@app.post("/api/ea/stop")
def ea_stop():
    try:
        return ea.stop_ea()
    except EAError as e:
        _err(e)


@app.get("/api/ea/account")
def ea_account(platform: str = "MT5"):
    return ea.account_check(platform.upper())


class EATest(BaseModel):
    name: str
    symbol: str = config.SYMBOL
    period: str = config.TIMEFRAME
    date_from: str
    date_to: str
    deposit: float = 10000
    model: int = 1
    preset: str | None = None


@app.post("/api/ea/test")
def ea_test(body: EATest):
    try:
        return ea.run_tester(body.name, body.symbol, body.period, body.date_from,
                             body.date_to, body.deposit, body.model, body.preset or None)
    except EAError as e:
        _err(e)


@app.get("/api/ea/reports")
def ea_reports():
    return ea.list_reports()


@app.get("/api/ea/reports/{filename}")
def ea_report(filename: str):
    try:
        return FileResponse(str(ea.report_path(filename)))
    except EAError as e:
        _err(e, 404)


if __name__ == "__main__":
    import uvicorn
    print(f"\n  Gold AI Trading Bot — open http://{config.HOST}:{config.PORT}\n")
    uvicorn.run(app, host=config.HOST, port=config.PORT)
