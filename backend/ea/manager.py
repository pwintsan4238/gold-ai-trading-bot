"""
EA Manager — run MetaTrader Expert Advisors (MQL4 / MQL5) from the dashboard.

What it does
  1. Library   : upload or paste .mq5 / .mq4 source (or compiled .ex5 / .ex4,
                 .mqh includes, .set presets). Stored in backend/ea_library/.
  2. Detect    : finds your MetaTrader 4/5 installs and their data folders.
  3. Deploy    : copies the file into <data>/MQL5/Experts/GoldBot/ (or MQL4).
  4. Compile   : runs MetaEditor's command-line compiler, returns the errors.
  5. Run       : restarts the terminal with a startup config that attaches the
                 EA to a chart (symbol + timeframe) with Algo Trading enabled,
                 so it trades automatically.
  6. Test      : runs the EA in MetaTrader's Strategy Tester and saves a report.
  7. Guardian  : while an EA runs, watches account equity and — if the day's
                 loss passes MAX_DAILY_LOSS — closes all positions and stops the
                 terminal. (Needs the MetaTrader5 Python package; MT5 only.)

EAs are compiled programs that run INSIDE MetaTrader, not inside this bot. They
manage their own entries, lot sizes and stops; the bot's 1% risk rule does not
apply to them. The guardian is the only limit the bot can enforce on an EA.
"""
import base64
import os
import re
import shutil
import subprocess
import time
from datetime import date, datetime
from pathlib import Path

import config
import db.database as db

LIB = Path(__file__).resolve().parents[1] / "ea_library"
RUNTIME = LIB / "_runtime"
LIB.mkdir(exist_ok=True)
RUNTIME.mkdir(exist_ok=True)

TEXT_EXT = {".mq5", ".mq4", ".mqh", ".set"}
BIN_EXT = {".ex5", ".ex4"}
ALLOWED = TEXT_EXT | BIN_EXT
SUBFOLDER = "GoldBot"           # our EAs live in Experts/GoldBot/
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _\-.()]{0,80}$")
PERIODS = ["M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1", "MN1"]
MT4_PERIOD_MIN = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240,
                  "D1": 1440, "W1": 10080, "MN1": 43200}


class EAError(Exception):
    pass


IS_WINDOWS = os.name == "nt"


def _need_windows():
    if not IS_WINDOWS:
        raise EAError("This needs Windows with MetaTrader installed.")


# =============================================================================
# Library
# =============================================================================
def _safe(name: str) -> Path:
    name = (name or "").strip()
    ext = Path(name).suffix.lower()
    if not _NAME_RE.match(name) or ext not in ALLOWED or ".." in name:
        raise EAError("Use a plain file name ending in .mq5, .mq4, .ex5, .ex4, "
                      ".mqh or .set (letters, numbers, space, _ - . allowed).")
    return LIB / name


def platform_of(name: str) -> str:
    ext = Path(name).suffix.lower()
    if ext in (".mq4", ".ex4"):
        return "MT4"
    if ext in (".mq5", ".ex5"):
        return "MT5"
    return "ANY"


def list_eas() -> list:
    out = []
    for f in sorted(LIB.iterdir()):
        if f.is_file() and f.suffix.lower() in ALLOWED:
            info = {"name": f.name, "size": f.stat().st_size,
                    "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
                    "platform": platform_of(f.name),
                    "kind": {".mqh": "include", ".set": "preset"}.get(
                        f.suffix.lower(), "source" if f.suffix.lower() in
                        (".mq5", ".mq4") else "compiled"),
                    "editable": f.suffix.lower() in TEXT_EXT}
            if f.suffix.lower() in (".mq5", ".mq4"):
                info.update(_describe_source(f.read_text(encoding="utf-8", errors="replace")))
            out.append(info)
    return out


def _describe_source(code: str) -> dict:
    """Light static checks so problems show before compiling."""
    inputs = re.findall(r"^\s*(?:input|extern)\s+[\w ]+?\s+(\w+)\s*=\s*([^;]+);",
                        code, re.M)
    return {
        "inputs": [{"name": n, "default": v.strip()} for n, v in inputs][:40],
        "has_ontick": bool(re.search(r"\b(OnTick|start)\s*\(", code)),
        "uses_dll": bool(re.search(r"#import\s+\"[^\"]+\.dll\"", code, re.I)),
        "is_indicator": "#property indicator_" in code,
    }


def save_ea(filename: str, code: str | None = None, b64: str | None = None) -> dict:
    path = _safe(filename)
    ext = path.suffix.lower()
    if ext in TEXT_EXT:
        if code is None and b64:
            raw = base64.b64decode(b64)
            code = _decode_text(raw)
        if not code or not code.strip():
            raise EAError("File is empty.")
        if len(code) > 2_000_000:
            raise EAError("File too large (2 MB max).")
        path.write_text(code, encoding="utf-8")
    else:
        if not b64:
            raise EAError("Compiled .ex4/.ex5 files must be uploaded, not pasted.")
        raw = base64.b64decode(b64)
        if len(raw) > 10_000_000:
            raise EAError("File too large (10 MB max).")
        path.write_bytes(raw)
    warnings = []
    if ext in (".mq5", ".mq4"):
        d = _describe_source(code)
        if d["is_indicator"]:
            warnings.append("This looks like an indicator, not an EA — it won't trade.")
        elif not d["has_ontick"]:
            warnings.append("No OnTick() found — an EA needs it to trade.")
        if d["uses_dll"]:
            warnings.append("Uses DLL imports. DLLs stay disabled for safety; "
                            "this EA may not work.")
    return {"saved": path.name, "warnings": warnings}


def _decode_text(raw: bytes) -> str:
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def read_ea(filename: str) -> str:
    path = _safe(filename)
    if path.suffix.lower() not in TEXT_EXT:
        raise EAError("Compiled files can't be viewed.")
    if not path.exists():
        raise EAError("Not found.")
    return path.read_text(encoding="utf-8", errors="replace")


def delete_ea(filename: str):
    path = _safe(filename)
    if path.exists():
        path.unlink()


# =============================================================================
# MetaTrader detection & settings
# =============================================================================
def _read_origin(folder: Path) -> str | None:
    f = folder / "origin.txt"
    if not f.exists():
        return None
    try:
        return _decode_text(f.read_bytes()).strip()
    except Exception:
        return None


def detect() -> list:
    """Find MetaTrader installs. Windows only; returns [] elsewhere."""
    if not IS_WINDOWS:
        return []
    found = {}
    roots = [os.environ.get("ProgramFiles", r"C:\Program Files"),
             os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")]
    for root in roots:
        rp = Path(root)
        if not rp.exists():
            continue
        for exe_name, plat in (("terminal64.exe", "MT5"), ("terminal.exe", "MT4")):
            for exe in rp.glob(f"*/{exe_name}"):
                inst = exe.parent
                editor = inst / ("metaeditor64.exe" if plat == "MT5" else "metaeditor.exe")
                if plat == "MT4" and not editor.exists():
                    editor = inst / "metaeditor64.exe"
                found[str(inst).lower()] = {
                    "platform": plat, "name": inst.name, "install_path": str(inst),
                    "terminal_path": str(exe),
                    "metaeditor_path": str(editor) if editor.exists() else "",
                    "data_path": "", "portable": False}
    # data folders (%APPDATA%\MetaQuotes\Terminal\<hash>) point back via origin.txt
    appdata = os.environ.get("APPDATA")
    if appdata:
        term_root = Path(appdata) / "MetaQuotes" / "Terminal"
        if term_root.exists():
            for d in term_root.iterdir():
                if not d.is_dir() or not ((d / "MQL5").exists() or (d / "MQL4").exists()):
                    continue
                origin = _read_origin(d)
                key = (origin or "").lower()
                if key in found and not found[key]["data_path"]:
                    found[key]["data_path"] = str(d)
    # portable installs keep MQL5/MQL4 next to the exe
    for inst in found.values():
        if not inst["data_path"]:
            ip = Path(inst["install_path"])
            if (ip / "MQL5").exists() or (ip / "MQL4").exists():
                inst["data_path"] = str(ip)
                inst["portable"] = True
    return list(found.values())


def get_settings() -> dict:
    s = db.get_json("ea_settings", {}) or {}
    for plat in ("MT5", "MT4"):
        cur = s.get(plat) or {}
        env = {"terminal_path": getattr(config, f"{plat}_TERMINAL_PATH"),
               "metaeditor_path": getattr(config, f"{plat}_METAEDITOR_PATH"),
               "data_path": getattr(config, f"{plat}_DATA_PATH")}
        for k, v in env.items():
            if v:
                cur[k] = v       # .env always wins
        cur.setdefault("terminal_path", "")
        cur.setdefault("metaeditor_path", "")
        cur.setdefault("data_path", "")
        cur.setdefault("portable", False)
        s[plat] = cur
    return s


def save_settings(plat: str, values: dict) -> dict:
    plat = plat.upper()
    if plat not in ("MT5", "MT4"):
        raise EAError("Platform must be MT5 or MT4.")
    s = db.get_json("ea_settings", {}) or {}
    s[plat] = {k: str(values.get(k, "") or "").strip().strip('"') for k in
               ("terminal_path", "metaeditor_path", "data_path")}
    s[plat]["portable"] = bool(values.get("portable", False))
    db.set_json("ea_settings", s)
    return get_settings()


def auto_configure() -> dict:
    """Detect installs and fill any empty settings with the first match."""
    installs = detect()
    s = db.get_json("ea_settings", {}) or {}
    for plat in ("MT5", "MT4"):
        if (s.get(plat) or {}).get("terminal_path"):
            continue
        match = next((i for i in installs if i["platform"] == plat and i["data_path"]), None)
        if match:
            s[plat] = {k: match[k] for k in
                       ("terminal_path", "metaeditor_path", "data_path", "portable")}
    db.set_json("ea_settings", s)
    return {"installs": installs, "settings": get_settings()}


def _cfg(plat: str) -> dict:
    s = get_settings()[plat]
    if not s["data_path"] or not Path(s["data_path"]).exists():
        raise EAError(f"{plat} data folder not set. Click 'Detect MetaTrader' or set it "
                      f"(MetaTrader: File → Open Data Folder).")
    return s


def _mql(plat):
    return "MQL5" if plat == "MT5" else "MQL4"


def _ea_platform(name: str) -> str:
    plat = platform_of(name)
    if plat == "ANY":
        raise EAError("Pick an EA file (.mq5/.ex5 for MT5, .mq4/.ex4 for MT4).")
    return plat


# =============================================================================
# Deploy & compile
# =============================================================================
def deploy(name: str) -> dict:
    """Copy an EA (plus all .mqh includes and .set presets) into MetaTrader."""
    src = _safe(name)
    if not src.exists():
        raise EAError("Not found in library.")
    plat = platform_of(name)
    plats = ["MT5", "MT4"] if plat == "ANY" else [plat]
    copied = []
    for p in plats:
        try:
            cfg = _cfg(p)
        except EAError:
            if plat == "ANY":
                continue
            raise
        mql = Path(cfg["data_path"]) / _mql(p)
        experts = mql / "Experts" / SUBFOLDER
        experts.mkdir(parents=True, exist_ok=True)
        # includes & presets travel with every deploy
        for f in LIB.iterdir():
            ext = f.suffix.lower()
            if ext == ".mqh":
                dst = mql / "Include" / SUBFOLDER
                dst.mkdir(parents=True, exist_ok=True)
                _copy_source(f, dst / f.name)
                _copy_source(f, experts / f.name)   # so #include "x.mqh" works too
            elif ext == ".set":
                dst = mql / "Presets"
                dst.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dst / f.name)
                if p == "MT5":   # MT5 tester reads presets from MQL5/Profiles/Tester
                    t = mql / "Profiles" / "Tester"
                    t.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, t / f.name)
        if src.suffix.lower() not in (".mqh", ".set"):
            dst = experts / src.name
            if src.suffix.lower() in TEXT_EXT:
                _copy_source(src, dst)
            else:
                shutil.copy2(src, dst)
            copied.append(str(dst))
    return {"deployed": copied}


def _copy_source(src: Path, dst: Path):
    """MetaEditor reads UTF-16LE (with BOM) reliably, incl. non-English comments."""
    dst.write_bytes(b"\xff\xfe" + src.read_text(encoding="utf-8", errors="replace")
                    .encode("utf-16-le"))


def compile_ea(name: str) -> dict:
    _need_windows()
    if Path(name).suffix.lower() not in (".mq5", ".mq4"):
        raise EAError("Only .mq5 / .mq4 source files can be compiled.")
    plat = _ea_platform(name)
    cfg = _cfg(plat)
    editor = cfg["metaeditor_path"]
    if not editor or not Path(editor).exists():
        raise EAError(f"MetaEditor not found for {plat}. Set its path in EA settings.")
    deploy(name)
    target = Path(cfg["data_path"]) / _mql(plat) / "Experts" / SUBFOLDER / name
    log = RUNTIME / f"compile_{Path(name).stem}.log"
    log.unlink(missing_ok=True)
    out_ext = ".ex5" if plat == "MT5" else ".ex4"
    compiled = target.with_suffix(out_ext)
    before = compiled.stat().st_mtime if compiled.exists() else 0
    args = [editor, f"/compile:{target}", f"/log:{log}"]
    if cfg.get("portable"):
        args.append("/portable")
    try:
        subprocess.run(args, timeout=180, capture_output=True)
    except subprocess.TimeoutExpired:
        raise EAError("MetaEditor timed out after 3 minutes.")
    text = _decode_text(log.read_bytes()) if log.exists() else ""
    errors = [l.strip() for l in text.splitlines() if re.search(r"\berror\b", l, re.I)
              and not re.search(r"\b0 errors?\b", l, re.I)]
    warns = [l.strip() for l in text.splitlines() if re.search(r"\bwarning\b", l, re.I)
             and not re.search(r"\b0 warnings?\b", l, re.I)]
    m = re.search(r"(\d+)\s+errors?,\s*(\d+)\s+warnings?", text, re.I)
    n_err = int(m.group(1)) if m else len(errors)
    ok = compiled.exists() and compiled.stat().st_mtime > before and n_err == 0
    if ok:  # keep a copy of the compiled file in the library
        shutil.copy2(compiled, LIB / compiled.name)
    return {"ok": ok, "errors": errors[:50], "warnings": warns[:50],
            "summary": m.group(0) if m else ("Compiled." if ok else "Compile failed."),
            "compiled_file": compiled.name if ok else None, "log": text[-6000:]}


# =============================================================================
# Run / stop / test
# =============================================================================
def _exe_name(plat):
    return Path(get_settings()[plat]["terminal_path"] or
                ("terminal64.exe" if plat == "MT5" else "terminal.exe")).name


def terminal_running(plat: str) -> bool:
    if not IS_WINDOWS:
        return False
    exe = _exe_name(plat)
    try:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {exe}", "/NH"],
                             capture_output=True, text=True, timeout=15).stdout
        return exe.lower() in out.lower()
    except Exception:
        return False


def stop_terminal(plat: str, wait=20):
    """Ask the terminal to close, then force it if it doesn't."""
    _need_windows()
    exe = _exe_name(plat)
    subprocess.run(["taskkill", "/IM", exe], capture_output=True, timeout=15)
    for _ in range(wait):
        if not terminal_running(plat):
            return
        time.sleep(1)
    subprocess.run(["taskkill", "/F", "/IM", exe], capture_output=True, timeout=15)
    time.sleep(2)


def _write_ini(path: Path, lines: list, plat: str):
    text = "\r\n".join(lines) + "\r\n"
    if plat == "MT5":
        path.write_bytes(b"\xff\xfe" + text.encode("utf-16-le"))
    else:
        path.write_text(text, encoding="cp1252", errors="replace")


def _ensure_compiled(name: str, plat: str) -> str:
    """Return the Experts-relative EA name (no extension) ready to run."""
    cfg = _cfg(plat)
    stem = Path(name).stem
    out_ext = ".ex5" if plat == "MT5" else ".ex4"
    compiled = Path(cfg["data_path"]) / _mql(plat) / "Experts" / SUBFOLDER / (stem + out_ext)
    if Path(name).suffix.lower() in BIN_EXT:
        deploy(name)
    elif not compiled.exists() or compiled.stat().st_mtime < _safe(name).stat().st_mtime:
        res = compile_ea(name)
        if not res["ok"]:
            raise EAError("Compile failed: " + "; ".join(res["errors"][:3] or [res["summary"]]))
    if not compiled.exists():
        raise EAError(f"{compiled.name} not found after deploy/compile.")
    return f"{SUBFOLDER}\\{stem}"


def account_check(plat: str) -> dict:
    """Use the MetaTrader5 package (if installed) to see if the account is demo."""
    if plat != "MT5":
        return {"known": False, "demo": None, "note": "MT4 account type can't be read "
                "from Python — confirm it's a demo account yourself."}
    try:
        import MetaTrader5 as mt5
    except Exception:
        return {"known": False, "demo": None,
                "note": "Install the MetaTrader5 package to auto-check the account type."}
    cfg = get_settings()["MT5"]
    ok = mt5.initialize(path=cfg["terminal_path"]) if cfg["terminal_path"] else mt5.initialize()
    if not ok:
        return {"known": False, "demo": None, "note": f"MT5 not reachable: {mt5.last_error()}"}
    info = mt5.account_info()
    if info is None:
        return {"known": False, "demo": None, "note": "Not logged into an account."}
    demo = info.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO
    return {"known": True, "demo": demo, "login": info.login, "server": info.server,
            "balance": info.balance, "equity": info.equity, "currency": info.currency,
            "note": "Demo account." if demo else "LIVE (real-money) account!"}


def run_ea(name: str, symbol: str, period: str, preset: str | None = None,
           restart: bool = False, confirm_demo: bool = False) -> dict:
    """Restart MetaTrader with the EA attached to a chart and Algo Trading on."""
    _need_windows()
    plat = _ea_platform(name)
    period = period.upper()
    if period not in PERIODS:
        raise EAError(f"Timeframe must be one of {', '.join(PERIODS)}.")
    if not re.match(r"^[A-Za-z0-9._#\-]{1,30}$", symbol or ""):
        raise EAError("Invalid symbol.")
    cfg = _cfg(plat)
    if not cfg["terminal_path"] or not Path(cfg["terminal_path"]).exists():
        raise EAError(f"{plat} terminal not found. Set its path in EA settings.")

    acct = account_check(plat)
    if acct["known"] and not acct["demo"] and not config.ALLOW_LIVE_ACCOUNT:
        raise EAError("MetaTrader is logged into a LIVE account. EAs are blocked on live "
                      "accounts unless ALLOW_LIVE_ACCOUNT=true in .env.")
    if not acct["known"] and not confirm_demo:
        raise EAError("Can't verify the account type. Tick 'This terminal is on a DEMO "
                      "account' to continue.")

    if terminal_running(plat) and not restart:
        return {"ok": False, "needs_restart": True,
                "message": f"{plat} is open. It must restart to load the EA — open "
                           "positions stay at your broker with their SL/TP."}

    expert = _ensure_compiled(name, plat)
    if preset:
        _safe(preset)
        deploy(preset)
    if terminal_running(plat):
        stop_terminal(plat)

    ini = RUNTIME / f"start_{plat}.ini"
    if plat == "MT5":
        lines = ["[Experts]", "AllowLiveTrading=1", "AllowDllImport=0", "Enabled=1",
                 "Account=0", "Profile=0", "", "[StartUp]", f"Expert={expert}",
                 f"Symbol={symbol}", f"Period={period}"]
        if preset:
            lines.append(f"ExpertParameters={preset}")
    else:
        lines = ["[Experts]", "AllowLiveTrading=1", "AllowDllImport=0", "Enabled=1",
                 "", "[StartUp]", f"Expert={expert}", f"Symbol={symbol}",
                 f"Period={MT4_PERIOD_MIN[period]}"]
        if preset:
            lines.append(f"ExpertParameters={preset}")
    _write_ini(ini, lines, plat)

    args = [cfg["terminal_path"], f"/config:{ini}"]
    if cfg.get("portable"):
        args.append("/portable")
    subprocess.Popen(args, cwd=str(Path(cfg["terminal_path"]).parent))

    state = {"name": name, "platform": plat, "symbol": symbol, "period": period,
             "preset": preset, "started_at": datetime.utcnow().isoformat(),
             "account": {k: acct.get(k) for k in ("login", "server", "demo")}}
    db.set_json("ea_running", state)
    db.set_state("ea_halted_reason", "")
    return {"ok": True, "running": state,
            "message": f"{plat} restarted with {Path(name).stem} on {symbol} {period}. "
                       "Check the chart's top-right corner for the EA name and a "
                       "blue/green hat icon."}


def stop_ea(reason: str = "") -> dict:
    run = db.get_json("ea_running")
    plat = (run or {}).get("platform", "MT5")
    if IS_WINDOWS and terminal_running(plat):
        stop_terminal(plat)
    db.set_json("ea_running", None)
    if reason:
        db.set_state("ea_halted_reason", reason)
    return {"ok": True, "message": f"{plat} closed — the EA is no longer trading. "
            "Open positions remain at your broker with their SL/TP."}


def run_tester(name: str, symbol: str, period: str, date_from: str, date_to: str,
               deposit: float = 10000, model: int = 1, preset: str | None = None) -> dict:
    """Backtest the EA in MetaTrader's Strategy Tester (terminal closes after)."""
    _need_windows()
    plat = _ea_platform(name)
    period = period.upper()
    if period not in PERIODS:
        raise EAError("Invalid timeframe.")
    for d in (date_from, date_to):
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", d or ""):
            raise EAError("Dates must be YYYY-MM-DD.")
    cfg = _cfg(plat)
    if terminal_running(plat):
        raise EAError(f"Close {plat} first — the Strategy Tester needs to start it fresh.")
    expert = _ensure_compiled(name, plat)
    if preset:
        _safe(preset)
        deploy(preset)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report = f"goldbot_{Path(name).stem}_{stamp}"
    f_, t_ = date_from.replace("-", "."), date_to.replace("-", ".")
    ini = RUNTIME / f"tester_{plat}.ini"
    if plat == "MT5":
        lines = ["[Tester]", f"Expert={expert}", f"Symbol={symbol}", f"Period={period}",
                 f"Model={int(model)}", f"FromDate={f_}", f"ToDate={t_}",
                 "ForwardMode=0", f"Deposit={float(deposit):.0f}", "Leverage=100",
                 "Optimization=0", "Visual=0", f"Report=reports\\{report}",
                 "ReplaceReport=1", "ShutdownTerminal=1"]
        if preset:
            lines.insert(2, f"ExpertParameters={preset}")
    else:
        lines = [f"TestExpert={expert}", f"TestSymbol={symbol}",
                 f"TestPeriod={MT4_PERIOD_MIN[period]}", f"TestModel={min(int(model), 2)}",
                 "TestSpread=0", "TestOptimization=false", "TestDateEnable=true",
                 f"TestFromDate={f_}", f"TestToDate={t_}",
                 f"TestReport=reports\\{report}", "TestReplaceReport=true",
                 "TestShutdownTerminal=true", "TestVisualEnable=false"]
        if preset:
            lines.append(f"TestExpertParameters={preset}")
    (Path(cfg["data_path"]) / "reports").mkdir(exist_ok=True)
    _write_ini(ini, lines, plat)
    args = [cfg["terminal_path"], f"/config:{ini}"]
    if cfg.get("portable"):
        args.append("/portable")
    subprocess.Popen(args, cwd=str(Path(cfg["terminal_path"]).parent))
    return {"ok": True, "report": report,
            "message": f"Strategy Tester started for {Path(name).stem}. MetaTrader closes "
                       "itself when done; the report then appears under Tester reports."}


def list_reports() -> list:
    out = []
    for plat in ("MT5", "MT4"):
        dp = get_settings()[plat]["data_path"]
        if not dp:
            continue
        rdir = Path(dp) / "reports"
        if rdir.exists():
            for f in rdir.glob("goldbot_*.htm*"):
                out.append({"file": f.name, "platform": plat,
                            "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat()})
    return sorted(out, key=lambda r: r["modified"], reverse=True)


def report_path(filename: str) -> Path:
    if not re.match(r"^goldbot_[\w\-.]+\.html?$", filename):
        raise EAError("Bad report name.")
    for plat in ("MT5", "MT4"):
        dp = get_settings()[plat]["data_path"]
        if dp and (Path(dp) / "reports" / filename).exists():
            return Path(dp) / "reports" / filename
    raise EAError("Report not found.")


# =============================================================================
# Guardian
# =============================================================================
def guardian_check() -> dict | None:
    """Called from the engine loop. Enforces MAX_DAILY_LOSS on a running EA."""
    run = db.get_json("ea_running")
    if not run or not config.EA_GUARDIAN or run.get("platform") != "MT5" or not IS_WINDOWS:
        return None
    try:
        import MetaTrader5 as mt5
    except Exception:
        return {"active": False, "note": "Install MetaTrader5 package to enable the guardian."}
    cfg = get_settings()["MT5"]
    if not (mt5.initialize(path=cfg["terminal_path"]) if cfg["terminal_path"] else mt5.initialize()):
        return {"active": False, "note": "MT5 not reachable."}
    info = mt5.account_info()
    if info is None:
        return {"active": False, "note": "No account."}
    key = f"ea_day_start_equity:{date.today().isoformat()}"
    start = db.get_state(key)
    if start is None:
        db.set_state(key, f"{info.equity:.2f}")
        start = info.equity
    start = float(start)
    loss_pct = (start - info.equity) / start if start else 0
    status = {"active": True, "day_start_equity": start, "equity": info.equity,
              "loss_pct": round(loss_pct * 100, 2),
              "limit_pct": round(config.MAX_DAILY_LOSS * 100, 2)}
    if loss_pct >= config.MAX_DAILY_LOSS:
        for p in mt5.positions_get() or []:
            tick = mt5.symbol_info_tick(p.symbol)
            buy = p.type == mt5.POSITION_TYPE_BUY
            mt5.order_send({"action": mt5.TRADE_ACTION_DEAL, "symbol": p.symbol,
                            "volume": p.volume, "position": p.ticket,
                            "type": mt5.ORDER_TYPE_SELL if buy else mt5.ORDER_TYPE_BUY,
                            "price": tick.bid if buy else tick.ask, "deviation": 30,
                            "comment": "goldbot guardian",
                            "type_time": mt5.ORDER_TIME_GTC,
                            "type_filling": mt5.ORDER_FILLING_IOC})
        mt5.shutdown()
        stop_ea(f"Guardian: daily loss {loss_pct*100:.1f}% hit the "
                f"{config.MAX_DAILY_LOSS*100:.1f}% limit — positions closed, EA stopped.")
        status["tripped"] = True
    return status


def status() -> dict:
    run = db.get_json("ea_running")
    if run and IS_WINDOWS and not terminal_running(run["platform"]):
        run["terminal_closed"] = True
    return {"running": run, "halted_reason": db.get_state("ea_halted_reason", ""),
            "guardian": db.get_json("ea_guardian_status"),
            "is_windows": IS_WINDOWS, "periods": PERIODS}
