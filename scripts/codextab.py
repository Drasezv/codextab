#!/usr/bin/env python3
"""Codextab hooks, setup, Telegram receiver, and local usage helpers."""
import argparse
import datetime as dt
import hashlib
import html
import json
import os
import pathlib
import plistlib
import re
import secrets
import selectors
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

os.umask(0o077)

ROOT = pathlib.Path.home() / ".codextab"
CONFIG = ROOT / "config.json"
DB = ROOT / "data.sqlite3"
LIMIT_CACHE = ROOT / "limits.json"
LOG = ROOT / "codextab.log"
HOOKS = pathlib.Path.home() / ".codex" / "hooks.json"
SCRIPT = pathlib.Path(__file__).resolve()
CODEX = shutil.which("codex") or "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"
API = "https://api.telegram.org/bot"
BOT_MANAGER = "cctab_manager_bot"
BOT_MANAGER_CLAIM = "https://helsinki.lunace.ru/cctab/claim/"
BOT_AVATAR = pathlib.Path(__file__).resolve().parents[1] / "assets" / "avatar.jpg"
DEFAULTS = {
    "min_seconds": 1800,
    "events": {"done": True, "failed": True, "question": True, "permission": True, "limits": True},
    "approvals": False,
    "include_summary": False,
    "language": "auto",
    "poll_interval": 20,
    "approval_timeout": 60,
}
PRICES = {
    "gpt-6-astra": (10.0, 50.0), "gpt-6.1-astra": (10.0, 50.0),
    "gpt-6.1-sol": (2.0, 10.0), "gpt-6-sol": (2.0, 10.0),
    "gpt-6.1-terra": (1.0, 5.0), "gpt-6-terra": (1.0, 5.0),
    "gpt-6-luna": (0.1, 0.5), "gpt-5.6-sol": (4.0, 20.0),
    "gpt-5.5": (5.0, 30.0), "gpt-5.4": (2.5, 15.0),
    "gpt-5.3-codex": (1.75, 14.0), "gpt-5.2-codex": (1.75, 14.0),
    "gpt-5.2": (1.75, 14.0), "gpt-5.1-codex": (1.25, 10.0),
    "gpt-5.1": (1.25, 10.0), "gpt-5-codex": (1.25, 10.0),
    "gpt-5": (1.25, 10.0), "o3": (2.0, 8.0), "o4-mini": (1.1, 4.4),
}
SECRET_RE = re.compile(
    r"(?i)(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[baprs]-[\w-]{10,}|"
    r"AKIA[0-9A-Z]{16}|eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{6,}|"
    r"\d{8,10}:AA[\w-]{30,}|(?:password|passwd|token|secret|api[_-]?key|bearer)\s*[=:]\s*\S+|"
    r"://[^/\s:@]+:[^@\s]+@|\bsshpass\s+-p\s*\S+|\bmysql\w*\s[^\n]*?\s-p\S+|"
    r"\b(?:docker|helm|npm|gh)\s+login[^\n]*?(?:-p|--password|--password-stdin)[=\s]+\S+|"
    r"\bPrivateKey\s*=\s*\S+|\b(?:vless|vmess|trojan|ss)://\S+|aws_secret_access_key\s*[=: ]\s*\S+)"
)


def config():
    out = dict(DEFAULTS)
    out["events"] = dict(DEFAULTS["events"])
    try:
        raw = json.loads(CONFIG.read_text())
        out.update({k: v for k, v in raw.items() if k != "events"})
        out["events"].update(raw.get("events", {}))
    except (OSError, ValueError, TypeError):
        pass
    return out


def save_config(data):
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(ROOT, 0o700)
    path = ROOT / "config.json"
    tmp = ROOT / "config.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def db():
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(ROOT, 0o700)
    con = sqlite3.connect(DB, timeout=10)
    os.chmod(DB, 0o600)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript("""
      CREATE TABLE IF NOT EXISTS turns (
        session_id TEXT NOT NULL, turn_id TEXT NOT NULL, started REAL NOT NULL,
        finished REAL, cwd TEXT, model TEXT, status TEXT, error TEXT,
        input_tokens INTEGER DEFAULT 0, cached_tokens INTEGER DEFAULT 0, write_tokens INTEGER DEFAULT 0,
        output_tokens INTEGER DEFAULT 0, total_tokens INTEGER DEFAULT 0,
        answer TEXT DEFAULT '', PRIMARY KEY(session_id, turn_id));
      CREATE TABLE IF NOT EXISTS approvals (
        nonce TEXT PRIMARY KEY, session_id TEXT, turn_id TEXT, tool TEXT,
        detail TEXT, digest TEXT, created REAL, expires REAL, verdict TEXT, message_id INTEGER);
      CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
      CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
    """)
    columns = {row[1] for row in con.execute("PRAGMA table_info(turns)")}
    if "write_tokens" not in columns:
        con.execute("ALTER TABLE turns ADD COLUMN write_tokens INTEGER DEFAULT 0")
    approval_columns = {row[1] for row in con.execute("PRAGMA table_info(approvals)")}
    if "message_id" not in approval_columns:
        con.execute("ALTER TABLE approvals ADD COLUMN message_id INTEGER")
    return con


def log(msg):
    try:
        ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
        with LOG.open("a") as f:
            f.write(f"{dt.datetime.now().isoformat(timespec='seconds')} {msg[:500]}\n")
        os.chmod(LOG, 0o600)
    except OSError:
        pass


def scrub(value, limit=700):
    """Redact likely credentials before any optional Telegram display."""
    text = SECRET_RE.sub("[redacted]", str(value or ""))
    text = text.strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def tg(method, payload=None, token=None, timeout=10):
    token = token or config().get("bot_token")
    if not token:
        return None
    data = urllib.parse.urlencode(payload or {}).encode()
    req = urllib.request.Request(API + token + "/" + method, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = json.loads(response.read(1_000_000))
        if not body.get("ok"):
            log("Telegram API refused request: " + str(body.get("description", "")))
            return None
        return body.get("result")
    except Exception as exc:
        log("Telegram request failed: " + type(exc).__name__)
        return None


def send(text, markup=None, edit_id=None):
    cfg = config()
    chat = cfg.get("chat_id")
    if not chat:
        return None
    method = "editMessageText" if edit_id else "sendMessage"
    payload = {"chat_id": chat, "text": text[:3900], "parse_mode": "HTML", "disable_web_page_preview": "true"}
    if edit_id:
        payload["message_id"] = edit_id
    if markup:
        payload["reply_markup"] = json.dumps(markup)
    return tg(method, payload)


def language(cfg=None):
    lang = (cfg or config()).get("language", "auto")
    if lang != "auto":
        return "ru" if str(lang).startswith("ru") else "en"
    return "ru" if (cfg or config()).get("tg_lang", "").lower().startswith("ru") else "en"


def greeting_text(cfg=None):
    if language(cfg) == "ru":
        return ("🟢 <b>ПОДКЛЮЧЕНО</b>\n<i>Codextab</i>\n\n"
                "Буду писать о завершённых ходах, ошибках и лимитах Codex.\n\n"
                "<blockquote>/settings — настройки · /spend — расход</blockquote>")
    return ("🟢 <b>CONNECTED</b>\n<i>Codextab</i>\n\n"
            "I will notify you about finished Codex turns, errors, and limits.\n\n"
            "<blockquote>/settings · preferences\n/spend · usage estimate</blockquote>")


def help_text(cfg=None):
    if language(cfg) == "ru":
        return ("🔹 <b>КОМАНДЫ CODEXTAB</b>\n\n<blockquote>"
                "/settings · настройки уведомлений\n/spend · оценка расхода\n"
                "/limits · лимиты аккаунта\n/threshold N · порог в минутах\n"
                "/approvals · удалённые подтверждения\n/summary · фрагмент ответа</blockquote>")
    return ("🔹 <b>CODEXTAB COMMANDS</b>\n\n<blockquote>"
            "/settings · notification preferences\n/spend · usage estimate\n"
            "/limits · account limits\n/threshold N · threshold in minutes\n"
            "/approvals · remote approvals\n/summary · answer excerpt</blockquote>")


def limits_read():
    """Read current account limits using the supported local app-server RPC."""
    if not pathlib.Path(CODEX).exists() and not shutil.which("codex"):
        return None
    proc = None
    try:
        proc = subprocess.Popen([CODEX, "app-server", "--listen", "stdio://"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ)
        pending = b""
        def send_rpc(req_id, method, params=None):
            msg = {"jsonrpc": "2.0", "method": method, "id": req_id}
            if params is not None:
                msg["params"] = params
            proc.stdin.write((json.dumps(msg) + "\n").encode()); proc.stdin.flush()
        def response(req_id, seconds=12):
            nonlocal pending
            deadline = time.time() + seconds
            while time.time() < deadline:
                if b"\n" not in pending:
                    ready = selector.select(max(0, deadline-time.time()))
                    if not ready: break
                    chunk = os.read(proc.stdout.fileno(), 4096)
                    if not chunk: break
                    pending += chunk
                line, pending = pending.split(b"\n", 1)
                try: row = json.loads(line)
                except ValueError: continue
                if row.get("id") == req_id: return row.get("result")
            return None
        hello = {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": {
            "clientInfo": {"name": "codextab", "title": "Codextab", "version": "1.0.0"},
            "capabilities": {}}}
        proc.stdin.write((json.dumps(hello) + "\n").encode()); proc.stdin.flush()
        if response(1) is None:
            return None
        proc.stdin.write(b'{"jsonrpc":"2.0","method":"initialized","params":{}}\n'); proc.stdin.flush()
        send_rpc(2, "account/rateLimits/read")
        return response(2)
    except Exception as exc:
        log("app-server limits unavailable: " + type(exc).__name__)
        return None
    finally:
        if proc:
            try: proc.stdin.close()
            except Exception: pass
            try: proc.terminate(); proc.wait(timeout=2)
            except Exception:
                try: proc.kill()
                except Exception: pass


def current_limits(force=False):
    if not force:
        try:
            cached = json.loads(LIMIT_CACHE.read_text())
            if time.time() - float(cached.get("captured_at", 0)) < 300:
                return cached.get("data")
        except (OSError, ValueError, TypeError):
            pass
    data = limits_read()
    if data:
        ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = LIMIT_CACHE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"captured_at": time.time(), "data": data}))
        os.chmod(tmp, 0o600); os.replace(tmp, LIMIT_CACHE)
    return data


def limits_text(data, lang=None):
    lang = lang or language()
    if not data:
        return ("🔴 <b>ЛИМИТЫ НЕДОСТУПНЫ</b>\n<i>Codex</i>" if lang == "ru"
                else "🔴 <b>LIMITS UNAVAILABLE</b>\n<i>Codex</i>")
    rows = data.get("rateLimitsByLimitId") or {}
    if not rows and isinstance(data.get("rateLimits"), dict):
        rows = {"account": data["rateLimits"]}
    lines = ["🔵 <b>ЛИМИТЫ CODEX</b>" if lang == "ru" else "🔵 <b>CODEX LIMITS</b>"]
    for name, block in list(rows.items())[:5]:
        if not isinstance(block, dict): continue
        title = html.escape(str(block.get("limitName") or block.get("planType") or name))
        lines.append("\n<i>" + title + "</i>")
        for key, label in (("primary", "основное" if lang == "ru" else "primary"), ("secondary", "дополнительное" if lang == "ru" else "secondary")):
            win = block.get(key)
            if not isinstance(win, dict): continue
            try: pct = max(0, min(100, int(win.get("usedPercent", 0))))
            except (TypeError, ValueError): continue
            reset = win.get("resetsAt")
            when = ""
            if reset:
                try:
                    remaining = max(0, int(float(reset) - time.time()))
                    hours, rem = divmod(remaining, 3600)
                    left = f"{hours} ч {rem // 60} мин" if lang == "ru" else f"{hours}h {rem // 60}m"
                    when = f" · {'сбросится через' if lang == 'ru' else 'resets in'} {left}"
                except (TypeError, ValueError, OverflowError):
                    pass
            filled = round(pct / 10)
            bar = "▰" * filled + "▱" * (10 - filled)
            lines.append(f"<b>{label}</b> {bar} {pct}%{when}")
    if len(lines) == 1:
        lines.append("Данные аккаунта пусты." if lang == "ru" else "No account limit data returned.")
    return "\n".join(lines)


def estimate_cost(model, input_tokens, cached_tokens, output_tokens, write_tokens=0):
    model = str(model or "").lower()
    price = next((v for key, v in PRICES.items() if model.startswith(key)), None)
    if price is None: return None
    regular = max(0, int(input_tokens or 0) - int(cached_tokens or 0))
    cached = max(0, int(cached_tokens or 0))
    writes = max(0, int(write_tokens or 0))
    # Hypothetical published API price comparison, not ChatGPT plan billing.
    return (regular * price[0] + cached * price[0] * 0.1 + writes * price[0] * 1.25 + int(output_tokens or 0) * price[1]) / 1_000_000


def api_estimate_tokens(payload):
    if not isinstance(payload, dict): return (0, 0, 0, 0, 0)
    usage = payload.get("token_usage")
    if not isinstance(usage, dict): return (0, 0, 0, 0, 0)
    inp = usage.get("input_tokens", usage.get("input", 0)) or 0
    cached = usage.get("cached_input_tokens", usage.get("cached_tokens", 0)) or 0
    writes = usage.get("cache_write_input_tokens", usage.get("cache_write_tokens", 0)) or 0
    out = usage.get("output_tokens", usage.get("output", 0)) or 0
    total = usage.get("total_tokens", int(inp or 0) + int(out or 0)) or 0
    try: return tuple(int(x) for x in (inp, cached, writes, out, total))
    except (TypeError, ValueError): return (0, 0, 0, 0, 0)


def hook():
    try: event = json.load(sys.stdin)
    except Exception: return 0
    name = event.get("hook_event_name", "")
    session = str(event.get("session_id") or "unknown")
    turn = str(event.get("turn_id") or "unknown")
    con = db()
    try:
        if name == "UserPromptSubmit":
            con.execute("INSERT OR REPLACE INTO turns(session_id,turn_id,started,cwd,model,status) VALUES(?,?,?,?,?,?)",
                        (session, turn, time.time(), pathlib.Path(str(event.get("cwd") or "Codex")).name,
                         event.get("model", ""), "running"))
            con.commit()
        elif name == "PostModelRequest":
            usage = api_estimate_tokens(event)
            con.execute("INSERT OR IGNORE INTO turns(session_id,turn_id,started,cwd,model,status) VALUES(?,?,?,?,?,?)",
                        (session, turn, time.time(), pathlib.Path(str(event.get("cwd") or "Codex")).name,
                         event.get("model", ""), "running"))
            row = con.execute("SELECT input_tokens,cached_tokens,write_tokens,output_tokens,total_tokens FROM turns WHERE session_id=? AND turn_id=?", (session, turn)).fetchone()
            base = row or (0, 0, 0, 0, 0)
            status = str(event.get("status") or "unknown")
            err = scrub(event.get("error") or "", 250) if status == "failed" else ""
            con.execute("UPDATE turns SET model=?,status=?,error=?,input_tokens=?,cached_tokens=?,write_tokens=?,output_tokens=?,total_tokens=? WHERE session_id=? AND turn_id=?",
                        (event.get("model", ""), status, err,
                         *(int(base[i] or 0) + usage[i] for i in range(5)), session, turn))
            con.commit()
        elif name == "Stop":
            row = con.execute("SELECT started,cwd,model,status,error,input_tokens,cached_tokens,write_tokens,output_tokens,total_tokens FROM turns WHERE session_id=? AND turn_id=?", (session, turn)).fetchone()
            if not row:
                con.execute("INSERT OR IGNORE INTO turns(session_id,turn_id,started,cwd,model,status) VALUES(?,?,?,?,?,?)",
                            (session, turn, time.time(), pathlib.Path(str(event.get("cwd") or "Codex")).name, event.get("model", ""), "completed"))
                row = (time.time(), pathlib.Path(str(event.get("cwd") or "Codex")).name, event.get("model", ""), "completed", "", 0, 0, 0, 0, 0)
            started, cwd, model, status, error, inp, cached, writes, out, total = row
            status = status or "completed"
            finished = time.time(); elapsed = max(0, int(finished - float(started)))
            raw_answer = event.get("last_assistant_message") or ""
            is_waiting = is_question(raw_answer)
            answer = (question_excerpt(raw_answer) if is_waiting and not config().get("include_summary")
                      else scrub(raw_answer, 700) if config().get("include_summary") else "")
            con.execute("UPDATE turns SET finished=?,answer=? WHERE session_id=? AND turn_id=?", (finished, answer, session, turn)); con.commit()
            cfg = config()
            failed = status in ("failed", "cancelled")
            question = is_waiting
            should_send = ((failed and cfg["events"].get("failed", True)) or
                           (question and cfg["events"].get("question", True)) or
                           (not failed and not question and elapsed >= int(cfg.get("min_seconds", 1800)) and cfg["events"].get("done", True)))
            lim = current_limits() if cfg.get("chat_id") and cfg["events"].get("limits", True) else None
            if lim: rate_limit_alerts(lim)
            if should_send and cfg.get("chat_id"):
                send(turn_message(cwd, model, elapsed, status, error, (inp, cached, writes, out, total), answer, lim, question))
        elif name == "PermissionRequest":
            return permission_hook(con, event, session, turn)
        elif name == "Interrupt":
            con.execute("UPDATE turns SET status='cancelled',finished=? WHERE session_id=? AND turn_id=?", (time.time(), session, turn)); con.commit()
            cfg = config()
            if cfg.get("chat_id") and cfg.get("events", {}).get("failed", True):
                row = con.execute("SELECT started,cwd,model,input_tokens,cached_tokens,write_tokens,output_tokens,total_tokens FROM turns WHERE session_id=? AND turn_id=?", (session, turn)).fetchone()
                started, cwd, model, inp, cached, writes, out, total = row or (time.time(), pathlib.Path(str(event.get('cwd') or 'Codex')).name, event.get('model', ''), 0, 0, 0, 0, 0)
                elapsed = max(0, int(time.time() - float(started or time.time())))
                send(turn_message(cwd, model, elapsed, "cancelled", "", (inp, cached, writes, out, total), "", current_limits(), False))
    except Exception as exc:
        log("hook error: " + type(exc).__name__)
    finally:
        con.close()
    if name == "Stop":
        print(json.dumps({"continue": True}))
    return 0


def turn_message(cwd, model, elapsed, status, error, usage, answer, limit_data=None, question=False):
    cfg = config(); lang = language(cfg)
    failed = status in ("failed", "cancelled")
    if lang == "ru":
        title = "🔴 <b>ЗАДАЧА УПАЛА</b>" if status == "failed" else "🟡 <b>ХОД ПРЕРВАН</b>" if status == "cancelled" else "🟠 <b>ЖДЁТ ТВОЕГО ОТВЕТА</b>" if question else "🟢 <b>ЗАДАЧА ГОТОВА</b>"
    else:
        title = "🔴 <b>TASK FAILED</b>" if status == "failed" else "🟡 <b>TURN INTERRUPTED</b>" if status == "cancelled" else "🟠 <b>WANTS YOUR ANSWER</b>" if question else "🟢 <b>TASK DONE</b>"
    inp, cached, writes, out, total = usage
    cost = estimate_cost(model, inp, cached, out, writes)
    duration = f"{elapsed // 3600} ч {(elapsed % 3600)//60} мин" if lang == "ru" else f"{elapsed//3600}h {(elapsed%3600)//60}m"
    token_label = "Токенов" if lang == "ru" else "Tokens"
    cost_text = f"≈ ${cost:.4f}" if cost is not None else ("цена модели неизвестна" if lang == "ru" else "model price unavailable")
    body = [title, f"<i>{html.escape(str(cwd or 'Codex'))}</i>", "",
            f"<blockquote><b>{token_label}:</b> {total:,} · {cost_text}</blockquote>"]
    if limit_data:
        info = notification_limit_rows(limit_data, lang)
        if info: body.extend(info)
    else:
        body.append(f"<i>{'лимиты недоступны' if lang == 'ru' else 'rate limits unavailable'}</i>")
    body.extend(["", f"<i>{html.escape(str(model or 'Codex'))} · {'работал' if lang == 'ru' else 'ran for'} {duration}</i>"])
    if failed and error: body.extend(["", "<b>Ошибка:</b> " + html.escape(scrub(error, 400))])
    if answer and not failed: body.extend(["", "<blockquote expandable>" + html.escape(answer) + "</blockquote>"])
    return "\n".join(body)


def notification_limit_rows(data, lang):
    """Compact limit bars matching the cctab task notification layout."""
    rows = data.get("rateLimitsByLimitId") or {}
    if not rows and isinstance(data.get("rateLimits"), dict):
        rows = {"account": data["rateLimits"]}
    lines = []
    for block in rows.values():
        if not isinstance(block, dict):
            continue
        for key in ("primary", "secondary"):
            win = block.get(key)
            if not isinstance(win, dict):
                continue
            try:
                pct = max(0, min(100, int(win.get("usedPercent", 0))))
            except (TypeError, ValueError):
                continue
            duration = win.get("windowDurationMins")
            if duration == 300:
                label = "5 часов" if lang == "ru" else "5 hours"
            elif isinstance(duration, (int, float)) and duration >= 1000:
                label = "Неделя" if lang == "ru" else "Week"
            else:
                name = block.get("limitName") or block.get("planType") or ("Лимит" if lang == "ru" else "Limit")
                label = html.escape(str(name))
            reset = win.get("resetsAt")
            reset_text = ""
            if reset:
                try:
                    remaining = max(0, int(float(reset) - time.time()))
                    hours, rem = divmod(remaining, 3600)
                    minutes = rem // 60
                    left = f"{hours} ч {minutes} мин" if lang == "ru" else f"{hours}h {minutes}m"
                    reset_text = f" · {'сбросится через' if lang == 'ru' else 'resets in'} {left}"
                except (TypeError, ValueError, OverflowError):
                    pass
            filled = round(pct / 10)
            bar = "▰" * filled + "▱" * (10 - filled)
            lines.append(f"<b>{label}</b> {bar} {pct}%{reset_text}")
    return lines


def is_question(message):
    """Heuristic only: Codex has no hook equivalent to Claude's question notification."""
    text = scrub(message, 1000).strip()
    if not text or len(text) > 450 or "?" not in text:
        return False
    first = text.lower().splitlines()[0].strip()
    starts = ("which ", "do you ", "would you ", "can you ", "should i ", "please choose",
              "what would", "how would", "как тебе", "какой вариант", "какой из", "выбери ",
              "хочешь ", "нужно ли", "предпочитаешь ", "что выбираешь")
    return first.startswith(starts)


def question_excerpt(message):
    """Return only the detected question when full answer sharing is off."""
    text = scrub(message, 1000).strip()
    sentences = re.split(r"(?<=[.!?])\s+", text)
    for sentence in reversed(sentences):
        sentence = sentence.strip()
        if sentence.endswith("?"):
            return scrub(sentence, 280)
    return scrub(text, 280)


def permission_hook(con, event, session, turn):
    cfg = config()
    if not cfg.get("chat_id") or not cfg.get("events", {}).get("permission", True): return 0
    tool = scrub(event.get("tool_name") or "tool", 80)
    raw = event.get("tool_input")
    if isinstance(raw, dict):
        detail = scrub(raw.get("command") or raw.get("description") or json.dumps(raw, ensure_ascii=False), 800)
    else: detail = scrub(raw, 800)
    project = html.escape(pathlib.Path(str(event.get("cwd") or "Codex")).name)
    ru = language(cfg) == "ru"
    if not cfg.get("approvals"):
        send(f"🟡 <b>{'НУЖНО РАЗРЕШЕНИЕ' if ru else 'NEEDS PERMISSION'}</b>\n<i>{project}</i>\n\n{('Codex хочет выполнить' if ru else 'Codex wants to run')} <b>{html.escape(tool)}</b>\n<blockquote>{html.escape(detail)}</blockquote>\n\n<i>{'Ответь в Codex' if ru else 'Respond in Codex'}</i>")
        return 0
    nonce = secrets.token_urlsafe(12)
    digest = hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    now = time.time(); expires = now + min(60, max(10, int(cfg.get("approval_timeout", 60))))
    con.execute("INSERT INTO approvals(nonce,session_id,turn_id,tool,detail,digest,created,expires,verdict) VALUES(?,?,?,?,?,?,?,?,NULL)", (nonce, session, turn, tool, detail, digest, now, expires)); con.commit()
    markup = {"inline_keyboard": [[
        {"text": "✅ Разрешить" if language(cfg)=="ru" else "✅ Allow", "callback_data": "a:allow:" + nonce},
        {"text": "⛔ Отклонить" if language(cfg)=="ru" else "⛔ Deny", "callback_data": "a:deny:" + nonce}]]}
    sent = send(f"🟡 <b>{'НУЖНО РАЗРЕШЕНИЕ' if ru else 'NEEDS PERMISSION'}</b>\n<i>{project}</i>\n\n{('Codex хочет выполнить' if ru else 'Codex wants to run')} <b>{html.escape(tool)}</b>\n<blockquote>{html.escape(detail)}</blockquote>\n\n<i>{'Кнопки активны одну минуту' if ru else 'Buttons stay active for one minute'}</i>", markup)
    if not sent:
        con.execute("DELETE FROM approvals WHERE nonce=?", (nonce,)); con.commit(); return 0
    message_id = sent.get("message_id") if isinstance(sent, dict) else None
    con.execute("UPDATE approvals SET message_id=? WHERE nonce=?", (message_id, nonce)); con.commit()
    while time.time() < expires:
        row = con.execute("SELECT verdict,session_id,turn_id,tool,digest,expires FROM approvals WHERE nonce=?", (nonce,)).fetchone()
        if row and row[0] and row[1] == session and row[2] == turn and row[3] == tool and row[4] == digest and row[5] >= time.time():
            verdict = row[0]
            con.execute("DELETE FROM approvals WHERE nonce=?", (nonce,)); con.commit()
            if message_id:
                tg("editMessageReplyMarkup", {"chat_id":cfg["chat_id"], "message_id":message_id, "reply_markup":json.dumps({"inline_keyboard":[]})})
            behavior = "allow" if verdict == "allow" else "deny"
            out = {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": behavior}}}
            if behavior == "deny": out["hookSpecificOutput"]["decision"]["message"] = "Denied from Codextab"
            print(json.dumps(out)); return 0
        time.sleep(0.25)
    con.execute("DELETE FROM approvals WHERE nonce=?", (nonce,)); con.commit()
    if message_id:
        tg("editMessageReplyMarkup", {"chat_id":cfg["chat_id"], "message_id":message_id, "reply_markup":json.dumps({"inline_keyboard":[]})})
    return 0


def rate_limit_alerts(data):
    con = db(); rows = data.get("rateLimitsByLimitId") or {}
    for key, block in rows.items():
        for win_key in ("primary", "secondary"):
            win = (block or {}).get(win_key) or {}
            try: pct = int(win.get("usedPercent", 0))
            except (ValueError, TypeError): continue
            if pct < 100: continue
            reset = win.get("resetsAt") or 0
            stamp = f"{key}:{win_key}:{reset}"
            old = con.execute("SELECT value FROM kv WHERE key='limit:'||?", (key + ":" + win_key,)).fetchone()
            if old and old[0] == str(reset): continue
            con.execute("INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)", ("limit:" + key + ":" + win_key, str(reset)))
            con.commit()
            lang = language()
            heading = "🔴 <b>ЛИМИТ ИСЧЕРПАН</b>" if lang == "ru" else "🔴 <b>LIMIT REACHED</b>"
            details = notification_limit_rows(data, lang)
            note = "Ничего не запустится до сброса лимита." if lang == "ru" else "Nothing will run until the limit resets."
            send("\n".join([heading, "<i>Codex</i>", "", *details, "", f"<i>{note}</i>"]))
    con.close()


def limits_brief(data, lang):
    blocks = data.get("rateLimitsByLimitId") or {}
    if not blocks and isinstance(data.get("rateLimits"), dict):
        blocks = {"account": data["rateLimits"]}
    pieces = []
    for block in blocks.values():
        if not isinstance(block, dict): continue
        label = "осталось" if lang == "ru" else "remaining"
        for key in ("primary", "secondary"):
            win = block.get(key)
            if not isinstance(win, dict): continue
            try: pct = max(0, min(100, int(win.get("usedPercent", 0))))
            except (ValueError, TypeError): continue
            duration = win.get("windowDurationMins")
            suffix = f" {duration // 60}h" if isinstance(duration, int) and duration >= 60 else ""
            pieces.append(f"{label}{suffix}: {100-pct}%")
    return " · ".join(pieces[:4])


MENU_TABS = (("time", "Время", "Time"), ("events", "Сообщения", "Messages"), ("spend", "Расход", "Spend"))
MENU_STEPS = (("Всё", "Everything", 0), ("5 мин", "5 min", 300), ("30 мин", "30 min", 1800),
              ("1 ч", "1 hour", 3600), ("2 ч", "2 hours", 7200))
MENU_EVENTS = (("done", "Готово", "Done"), ("failed", "Ошибки", "Failures"),
               ("question", "Вопросы", "Questions"), ("permission", "Разрешения", "Permissions"),
               ("approvals", "Подтверждения", "Approvals"), ("limits", "Лимиты", "Limits"),
               ("include_summary", "Фрагмент ответа", "Answer excerpt"))


def settings_text():
    cfg = config(); lang = language(cfg); view = cfg.get("menu_view", "home")
    def title(ru, en): return f"<b>{ru if lang == 'ru' else en}</b>\n\n"
    if view == "time":
        mins = int(cfg.get("min_seconds", 1800)) // 60
        value = (f"{mins} мин" if lang == "ru" else f"{mins} min") if mins else ("всё" if lang == "ru" else "everything")
        note = "Короткие задачи проходят молча. Ошибки приходят независимо от порога." if lang == "ru" else "Short turns stay quiet. Errors are reported regardless of the threshold."
        return title("Время", "Time") + f"<b>{'Тихо до' if lang == 'ru' else 'Quiet below'} {html.escape(value)}</b>\n\n<blockquote>{note}</blockquote>"
    if view == "events":
        lines = []
        for key, ru, en in MENU_EVENTS:
            val = cfg.get("approvals", False) if key == "approvals" else cfg.get("include_summary", False) if key == "include_summary" else cfg["events"].get(key, True)
            state = ("вкл" if val else "выкл") if lang == "ru" else ("on" if val else "off")
            lines.append(f"{ru if lang == 'ru' else en}: {state}")
        return title("Сообщения", "Messages") + "<blockquote>" + html.escape("\n".join(lines)) + "</blockquote>"
    if view == "spend":
        return spend_menu_text()
    enabled = sum(bool(cfg["events"].get(key, True)) for key in ("done", "failed", "question", "permission", "limits"))
    total = 5
    threshold = int(cfg.get("min_seconds", 1800)) // 60
    threshold_label = f"{threshold} мин" if lang == "ru" else f"{threshold} min"
    note = (f"Тихо до {threshold_label}, включены все {total} уведомлений." if enabled == total else f"Тихо до {threshold_label}, включено {enabled} из {total}.") if lang == "ru" else (f"Quiet below {threshold_label}, all {total} notifications on." if enabled == total else f"Quiet below {threshold_label}, {enabled} of {total} notifications on.")
    footer = "Выбери вкладку, чтобы настроить Codextab." if lang == "ru" else "Pick a tab to tune Codextab."
    return title("Настройки", "Settings") + html.escape(note) + f"\n\n<blockquote>{footer}</blockquote>"


def settings_keyboard():
    cfg = config(); lang = language(cfg); here = cfg.get("menu_view", "home")
    def label(key):
        pair = next((ru, en) for k, ru, en in MENU_EVENTS if k == key)
        return pair[0] if lang == "ru" else pair[1]
    tabs = [{"text": (f"‹ {ru if lang == 'ru' else en} ›" if key == here else (ru if lang == 'ru' else en)), "callback_data": "v:" + key}
            for key, ru, en in MENU_TABS]
    rows = [tabs]
    if here == "time":
        row = []
        for ru, en, seconds in MENU_STEPS:
            text = ru if lang == "ru" else en
            if int(cfg.get("min_seconds", 1800)) == seconds: text = "• " + text
            row.append({"text": text, "callback_data": f"q:{seconds}"})
            if len(row) == 3: rows.append(row); row = []
        if row: rows.append(row)
        rows.append([{"text": "Своё" if lang == "ru" else "Custom", "callback_data": "q:custom"}])
    elif here == "events":
        for i in range(0, len(MENU_EVENTS), 2):
            row = []
            for key, _, _ in MENU_EVENTS[i:i+2]:
                val = cfg.get("approvals", False) if key == "approvals" else cfg.get("include_summary", False) if key == "include_summary" else cfg["events"].get(key, True)
                row.append({"text": ("✓ " if val else "✗ ") + label(key), "callback_data": "e:" + key})
            rows.append(row)
    elif here == "spend":
        rows.append([{"text": "Обновить" if lang == "ru" else "Refresh", "callback_data": "v:spend!"}])
    else:
        rows.append([{"text": ("‹ RU ›" if cfg.get("language") == "ru" else "RU"), "callback_data": "l:ru"},
                     {"text": ("‹ EN ›" if cfg.get("language") == "en" else "EN"), "callback_data": "l:en"},
                     {"text": ("‹ AUTO ›" if cfg.get("language") == "auto" else "AUTO"), "callback_data": "l:auto"}])
    return {"inline_keyboard": rows}


def spend_menu_text():
    rows = spend_rows(); lang = language()
    if not rows:
        return "<b>Расход</b>\n\n<i>Пока ничего не записано.</i>" if lang == "ru" else "<b>Spend</b>\n\n<i>Nothing recorded yet.</i>"
    head = ("Проект                 Цена     Токены" if lang == "ru" else "Project                 Cost     Tokens")
    lines = [head]
    for cwd, cost, tokens, stamp in rows[:6]:
        name = (cwd or "Codex")[:18]
        price = f"${cost:.2f}" if cost is not None else "н/д" if lang == "ru" else "n/a"
        lines.append(f"{name:<20}{price:>7}{int(tokens or 0):>10,}")
    total = sum((cost or 0) for _, cost, _, _ in rows)
    tail = f"\nИтого по API ценам: ~${total:.2f}. Это не списания ChatGPT." if lang == "ru" else f"\nAPI price estimate: ~${total:.2f}. Not ChatGPT plan billing."
    return "<b>Расход Codex</b>\n<pre>" + html.escape("\n".join(lines)) + "</pre>" + tail


def spend_text():
    rows = spend_rows()
    lang = language(); title = "РАСХОД CODEX" if lang == "ru" else "CODEX SPEND"
    lines = ["💳 <b>" + title + "</b>", "<i>Оценка по API ценам · не списания ChatGPT</i>" if lang == "ru" else "<i>API price estimate · not ChatGPT plan billing</i>"]
    if not rows:
        lines.append("\n<i>Нет завершённых ходов.</i>" if lang == "ru" else "\n<i>No finished turns recorded yet.</i>")
        return "\n".join(lines)
    total = 0.0; any_unknown = False
    for cwd, cost, toks, stamp in rows:
        if cost is None: any_unknown = True
        else: total += cost
        when = dt.datetime.fromtimestamp(stamp).strftime("%d.%m %H:%M")
        cost_text = f"${cost:.4f}" if cost is not None else ("н/д" if lang == "ru" else "n/a")
        lines.append(f"{html.escape(cwd or 'Codex')[:22]} · {cost_text} · {int(toks or 0):,} · {when}")
    if any_unknown:
        lines.append("\nЕсть модели без известной цены, итог неполный." if lang == "ru" else "Some model prices are unknown; total is partial.")
    total_text = f"Итого: ~${total:.4f}" if lang == "ru" else f"Total: ~${total:.4f}"
    known_text = f"Известная часть: ~${total:.4f}" if lang == "ru" else f"Known portion: ~${total:.4f}"
    lines.append("\n<b>" + (known_text if any_unknown else total_text) + "</b>")
    return "\n".join(lines)


def spend_rows():
    """Aggregate token usage from Codex session records without reading chat text."""
    rows = {}
    history_dir = pathlib.Path.home() / ".codex" / "sessions"
    for path in history_dir.glob("**/*.jsonl"):
        sid = path.stem
        cwd = path.parent.name
        models = {}; totals = {}
        try:
            with path.open(errors="replace") as stream:
                for line in stream:
                    try: record = json.loads(line)
                    except ValueError: continue
                    payload = record.get("payload") or {}
                    if record.get("type") == "session_meta":
                        sid = str(payload.get("session_id") or sid)
                        cwd = pathlib.Path(str(payload.get("cwd") or cwd)).name
                    elif record.get("type") == "turn_context":
                        tid = str(payload.get("turn_id") or "")
                        if tid: models[tid] = str(payload.get("model") or "")
                        cwd = pathlib.Path(str(payload.get("cwd") or cwd)).name
                    elif record.get("type") == "token_usage_record":
                        tid = str(payload.get("turn_id") or "")
                        usage = payload.get("usage") or {}
                        if not isinstance(usage, dict): continue
                        model = models.get(tid, "")
                        acc = totals.setdefault(model, [0, 0, 0, 0, 0])
                        for i, key in enumerate(("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "total_tokens")):
                            try: acc[i] += int(usage.get(key) or 0)
                            except (ValueError, TypeError): pass
            if totals:
                estimates = [estimate_cost(model, values[0], values[1], values[3], values[2]) for model, values in totals.items()]
                cost = sum(x for x in estimates if x is not None) if all(x is not None for x in estimates) else None
                tokens = sum(v[4] for v in totals.values())
                rows[sid] = [cwd, cost, tokens, path.stat().st_mtime]
        except OSError:
            continue
    con = db()
    old = con.execute("SELECT session_id,cwd,model,SUM(input_tokens),SUM(cached_tokens),SUM(write_tokens),SUM(output_tokens),SUM(total_tokens),MAX(finished) FROM turns WHERE finished IS NOT NULL GROUP BY session_id,cwd,model").fetchall()
    con.close()
    unseen = {}
    for sid, cwd, model, inp, cached, writes, out, toks, stamp in old:
        if sid in rows: continue
        item = unseen.setdefault(sid, [cwd, 0.0, 0, stamp or 0])
        estimate = estimate_cost(model, inp, cached, out, writes)
        if estimate is None: item[1] = None
        elif item[1] is not None: item[1] += estimate
        item[2] += int(toks or 0); item[3] = max(item[3], stamp or 0)
    rows.update(unseen)
    return sorted(rows.values(), key=lambda row: row[3], reverse=True)[:30]


def handle_update(update):
    cfg = config()
    chat_obj = ((update.get("message") or {}).get("chat") or
                ((update.get("callback_query") or {}).get("message") or {}).get("chat") or {})
    chat = str(chat_obj.get("id") or "")
    tap = update.get("callback_query")
    if not cfg.get("chat_id"):
        msg = update.get("message") or {}; text = (msg.get("text") or "").strip()
        expected = str(cfg.get("pair_code") or "")
        if chat_obj.get("type") == "private" and text == "/start " + expected and expected:
            cfg["chat_id"] = chat
            cfg["tg_lang"] = (msg.get("from") or {}).get("language_code", "")
            cfg["owner_user_id"] = str((msg.get("from") or {}).get("id") or "")
            cfg.pop("pair_code", None)
            save_config(cfg)
            tg("sendMessage", {"chat_id": chat, "text": greeting_text(cfg), "parse_mode": "HTML"})
        return
    sender = str((tap or {}).get("from", {}).get("id") or (update.get("message") or {}).get("from", {}).get("id") or "")
    if chat != str(cfg.get("chat_id")) or sender != str(cfg.get("owner_user_id")):
        if tap: tg("answerCallbackQuery", {"callback_query_id": tap.get("id"), "text": "Not authorized"})
        return
    if tap:
        data = str(tap.get("data") or "")
        if data.startswith("a:"):
            _, verdict, nonce = data.split(":", 2)
            con = db()
            row = con.execute("SELECT expires,verdict FROM approvals WHERE nonce=?", (nonce,)).fetchone()
            if row and not row[1] and row[0] >= time.time():
                con.execute("UPDATE approvals SET verdict=? WHERE nonce=?", ("allow" if verdict == "allow" else "deny", nonce)); con.commit()
                tg("answerCallbackQuery", {"callback_query_id": tap.get("id"), "text": "Решение принято" if language(cfg)=="ru" else "Decision recorded"})
            else: tg("answerCallbackQuery", {"callback_query_id": tap.get("id"), "text": "Запрос истёк" if language(cfg)=="ru" else "Request expired"})
            con.close(); return
        if data.startswith("v:"):
            raw_view = data[2:]
            refreshing = raw_view.endswith("!")
            view = raw_view.rstrip("!")
            if view not in {key for key, _, _ in MENU_TABS}: return
            current = cfg.get("menu_view", "home")
            cfg["menu_view"] = view if refreshing or current != view else "home"
            save_config(cfg)
            tg("editMessageText", {"chat_id":chat,"message_id":(tap.get("message") or {}).get("message_id"),
                "text":settings_text(),"parse_mode":"HTML","reply_markup":json.dumps(settings_keyboard())})
            note = ("Считаю…" if language(cfg)=="ru" else "Counting…") if view == "spend" else ""
            tg("answerCallbackQuery", {"callback_query_id":tap.get("id"),"text":note})
            return
        if data == "q:custom":
            cfg["awaiting_threshold"] = True; save_config(cfg)
            tg("answerCallbackQuery", {"callback_query_id":tap.get("id"),"text":"Пришли число минут" if language(cfg)=="ru" else "Send minutes"})
            return
        if data.startswith("q:"):
            try: seconds = int(data[2:])
            except ValueError: return
            cfg["min_seconds"] = max(0, min(86400, seconds)); cfg["menu_view"] = "time"; save_config(cfg)
            tg("editMessageText", {"chat_id":chat,"message_id":(tap.get("message") or {}).get("message_id"),"text":settings_text(),"parse_mode":"HTML","reply_markup":json.dumps(settings_keyboard())})
            tg("answerCallbackQuery", {"callback_query_id":tap.get("id"),"text":"Готово" if language(cfg)=="ru" else "Saved"})
            return
        if data.startswith("e:"):
            key = data[2:]
            if key == "approvals": cfg["approvals"] = not cfg.get("approvals", False)
            elif key == "include_summary": cfg["include_summary"] = not cfg.get("include_summary", False)
            elif key in cfg["events"]: cfg["events"][key] = not cfg["events"].get(key, True)
            else: return
            cfg["menu_view"] = "events"; save_config(cfg)
            tg("editMessageText", {"chat_id":chat,"message_id":(tap.get("message") or {}).get("message_id"),"text":settings_text(),"parse_mode":"HTML","reply_markup":json.dumps(settings_keyboard())})
            tg("answerCallbackQuery", {"callback_query_id":tap.get("id")}); return
        if data.startswith("s:"):
            key = data[2:]
            if key == "approvals": cfg["approvals"] = not cfg.get("approvals", False)
            elif key == "include_summary": cfg["include_summary"] = not cfg.get("include_summary", False)
            elif key in cfg["events"]: cfg["events"][key] = not cfg["events"].get(key, True)
            save_config(cfg)
            tg("editMessageText", {"chat_id":chat, "message_id":(tap.get("message") or {}).get("message_id"),
                "text":settings_text(), "parse_mode":"HTML", "reply_markup":json.dumps(settings_keyboard())})
            tg("answerCallbackQuery", {"callback_query_id":tap.get("id")})
            return
        if data.startswith("m:"):
            delta = data[2:]
            if delta != "0": cfg["min_seconds"] = max(60, min(86400, int(cfg.get("min_seconds",1800)) + int(delta)*60)); save_config(cfg)
            tg("editMessageText", {"chat_id":chat, "message_id":(tap.get("message") or {}).get("message_id"),
                "text":settings_text(), "parse_mode":"HTML", "reply_markup":json.dumps(settings_keyboard())})
            tg("answerCallbackQuery", {"callback_query_id":tap.get("id")})
            return
        if data.startswith("l:"):
            cfg["language"] = data[2:]; save_config(cfg)
            tg("editMessageText", {"chat_id":chat, "message_id":(tap.get("message") or {}).get("message_id"),
                "text":settings_text(), "parse_mode":"HTML", "reply_markup":json.dumps(settings_keyboard())})
            tg("answerCallbackQuery", {"callback_query_id":tap.get("id")})
            return
        tg("answerCallbackQuery", {"callback_query_id": tap.get("id")}); return
    message = update.get("message") or {}; raw = (message.get("text") or "").strip(); parts = raw.split(); cmd = parts[0].split("@")[0].lower() if parts else ""
    if cfg.get("awaiting_threshold"):
        try:
            minutes = int(raw)
            if not 0 <= minutes <= 1440: raise ValueError
            cfg["min_seconds"] = minutes * 60; cfg["awaiting_threshold"] = False; cfg["menu_view"] = "time"; save_config(cfg)
            sent = send(settings_text(), settings_keyboard())
            if sent and isinstance(sent, dict): cfg["settings_message_id"] = sent.get("message_id"); save_config(cfg)
        except ValueError:
            send(("🟠 <b>ПОРОГ УВЕДОМЛЕНИЙ</b>\n\nПришли число минут от 0 до 1440." if language(cfg)=="ru"
                  else "🟠 <b>NOTIFICATION THRESHOLD</b>\n\nSend a number from 0 to 1440 minutes."))
        return
    if cmd in ("/start", "/settings"):
        cfg["menu_view"] = "home"; save_config(cfg)
        sent = send(settings_text(), settings_keyboard())
        if sent and isinstance(sent, dict): cfg["settings_message_id"] = sent.get("message_id"); save_config(cfg)
    elif cmd == "/spend":
        cfg["menu_view"] = "spend"; save_config(cfg)
        sent = send(settings_text(), settings_keyboard())
        if sent and isinstance(sent, dict): cfg["settings_message_id"] = sent.get("message_id"); save_config(cfg)
    elif cmd == "/limits": send(limits_text(current_limits(force=True)))
    elif cmd == "/threshold" and len(parts) > 1:
        try: cfg["min_seconds"] = max(0, min(86400, int(parts[1]) * 60)); save_config(cfg); send(settings_text(), settings_keyboard())
        except ValueError: send(("🟠 <b>НЕВЕРНЫЙ ПОРОГ</b>\n\nПример: <code>/threshold 30</code>" if language(cfg)=="ru"
                                 else "🟠 <b>INVALID THRESHOLD</b>\n\nExample: <code>/threshold 30</code>"))
    elif cmd in ("/done", "/failed", "/question", "/permission", "/limits_notify", "/approvals", "/summary"):
        key = {"/done":"done", "/failed":"failed", "/question":"question", "/permission":"permission", "/limits_notify":"limits"}.get(cmd)
        if key: cfg["events"][key] = not cfg["events"].get(key, True)
        elif cmd == "/approvals": cfg["approvals"] = not cfg.get("approvals", False)
        else: cfg["include_summary"] = not cfg.get("include_summary", False)
        save_config(cfg); send(settings_text(), settings_keyboard())
    elif cmd == "/lang" and len(parts) > 1 and parts[1] in ("ru", "en", "auto"):
        cfg["language"] = parts[1]; save_config(cfg); send(settings_text(), settings_keyboard())
    elif cmd == "/help": send(help_text(cfg))


def daemon():
    cfg = config()
    if not cfg.get("bot_token"):
        log("receiver stopped: no bot token configured"); return 1
    if not cfg.get("pair_code") and not cfg.get("chat_id"):
        cfg["pair_code"] = secrets.token_urlsafe(6); save_config(cfg)
    import fcntl
    lock = (ROOT / "daemon.lock").open("a")
    try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("receiver already running"); return 0
    con = db()
    row = con.execute("SELECT value FROM kv WHERE key='telegram_offset'").fetchone()
    offset = int(row[0]) if row else 0
    while True:
        expired = con.execute("SELECT message_id FROM approvals WHERE expires < ? AND message_id IS NOT NULL", (time.time(),)).fetchall()
        for (message_id,) in expired:
            tg("editMessageReplyMarkup", {"chat_id":config().get("chat_id"), "message_id":message_id,
                                           "reply_markup":json.dumps({"inline_keyboard":[]})})
        con.execute("DELETE FROM approvals WHERE expires < ?", (time.time(),)); con.commit()
        result = tg("getUpdates", {"timeout": 20, "offset": offset, "allowed_updates": json.dumps(["message", "callback_query"])}, timeout=27)
        if result is None:
            time.sleep(3)
            continue
        for update in result or []:
            offset = int(update.get("update_id", offset - 1)) + 1
            try: handle_update(update)
            except Exception as exc: log("update handler failed: " + type(exc).__name__)
            con.execute("INSERT OR REPLACE INTO kv(key,value) VALUES('telegram_offset',?)", (str(offset),)); con.commit()


def save_qr(text):
    try:
        import qrcode
    except ImportError:
        return ""
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = ROOT / f"codextab-create-{secrets.token_hex(4)}.png"
    qrcode.make(text).save(path)
    os.chmod(path, 0o600)
    if sys.platform == "darwin":
        subprocess.run(["open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return str(path)


def create_link(want_qr=False):
    cfg = config()
    code = secrets.token_hex(3)
    cfg["create_code"] = code
    cfg["create_until"] = int(time.time()) + 180
    save_config(cfg)
    link = f"https://t.me/newbot/{BOT_MANAGER}/cctab_{code}_bot?name=codextab"
    print(f"LINK {link}")
    if want_qr:
        path = save_qr(link)
        print(f"QR {path}" if path else "QR_UNAVAILABLE qrcode package is missing")
    return 0


def manager_claim(code, until):
    while time.time() < until:
        try:
            with urllib.request.urlopen(BOT_MANAGER_CLAIM + urllib.parse.quote(code), timeout=10) as response:
                result = json.loads(response.read())
            if result.get("token"):
                return result, ""
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                return None, "unreachable"
        except Exception:
            return None, "unreachable"
        time.sleep(3)
    return None, "expired"


def upload_bot_avatar(token):
    if not BOT_AVATAR.exists():
        return False
    boundary = secrets.token_hex(16)
    line = "--" + boundary
    head = (f'{line}\r\nContent-Disposition: form-data; name="photo"\r\n\r\n'
            '{"type":"static","photo":"attach://pic"}\r\n'
            f'{line}\r\nContent-Disposition: form-data; name="pic"; filename="codextab.jpg"\r\n'
            "Content-Type: image/jpeg\r\n\r\n")
    try:
        body = head.encode() + BOT_AVATAR.read_bytes() + f"\r\n{line}--\r\n".encode()
        request = urllib.request.Request(API + token + "/setMyProfilePhoto", data=body,
                                         headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(request, timeout=20) as response:
            return bool(json.loads(response.read()).get("ok"))
    except Exception as exc:
        log("Telegram avatar upload failed: " + type(exc).__name__)
        return False


def activate_managed_bot(result):
    token = result.get("token", "")
    me = tg("getMe", {}, token=token)
    owner = str(result.get("owner") or "")
    if not me or not me.get("username") or not owner:
        print("BAD_MANAGER_RESPONSE менеджер не вернул бота и владельца")
        return 1
    cfg = config()
    cfg.update({"bot_token": token, "chat_id": owner, "owner_user_id": owner,
                "bot_username": me["username"], "tg_lang": result.get("lang") or "",
                "create_code": "", "create_until": 0})
    cfg.pop("pair_code", None)
    save_config(cfg)
    if DB.exists():
        con = db(); con.execute("DELETE FROM kv WHERE key='telegram_offset'"); con.commit(); con.close()
    dressed = []
    for method, payload, label in [
        ("setMyName", {"name": "Codextab"}, "name"),
        ("setMyShortDescription", {"short_description": "Уведомления и статистика Codex"}, "short description"),
        ("setMyDescription", {"description": "Сообщает о долгих ходах Codex, ошибках и лимитах. Команды Telegram управляют Codextab на вашем компьютере."}, "description"),
        ("setMyCommands", {"commands": json.dumps([
        {"command": "settings", "description": "Настройки"}, {"command": "spend", "description": "Оценка расхода"},
        {"command": "limits", "description": "Лимиты Codex"}, {"command": "threshold", "description": "Порог уведомлений"},
        {"command": "approvals", "description": "Подтверждения выключены по умолчанию"}, {"command": "help", "description": "Помощь"}])}, "commands"),
    ]:
        if tg(method, payload, token=token) is not None:
            dressed.append(label)
    avatar_ok = upload_bot_avatar(token)
    try:
        tg("sendMessage", {"chat_id": owner, "text": greeting_text(cfg), "parse_mode": "HTML"}, token=token)
    except Exception:
        pass
    install_hooks(apply=True)
    plist = launch_agent_plist()
    agent_path = pathlib.Path.home() / "Library/LaunchAgents/com.drasez.codextab.plist"
    agent_path.parent.mkdir(parents=True, exist_ok=True)
    agent_path.write_bytes(plist)
    os.chmod(agent_path, 0o600)
    uid = os.getuid()
    subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(agent_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/com.drasez.codextab"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"BOT @{me['username']}")
    if avatar_ok:
        dressed.append("avatar")
    print("DRESSED " + ", ".join(dressed) if dressed else "DRESS_FAILED профиль бота не обновился")
    print("CONNECTED Codextab is running in the background")
    print("RESTART_CODEX Restart Codex once to load its hooks")
    return 0


def wait_for_managed_bot():
    cfg = config()
    code = cfg.get("create_code")
    if not code:
        print("NO_LINK run codextab link first")
        return 1
    result, why = manager_claim(str(code), min(float(cfg.get("create_until", 0)), time.time() + 180))
    if why == "expired":
        print("EXPIRED Создание бота не подтверждено за 3 минуты. Запусти codextab setup, чтобы получить новую ссылку.")
        return 2
    if why:
        print("UNREACHABLE cctab-manager сейчас недоступен. Попробуй ещё раз позже или создай бота вручную через @BotFather.")
        return 3
    return activate_managed_bot(result)


def setup():
    return create_link("--qr" in sys.argv[2:])


def launch_agent_plist():
    return plistlib.dumps({"Label":"com.drasez.codextab", "ProgramArguments":[sys.executable,str(SCRIPT),"daemon"],
                           "RunAtLoad":True,"KeepAlive":True,"StandardOutPath":str(LOG),"StandardErrorPath":str(LOG)})


def hook_entries():
    cmd = f"{sys.executable!r} {str(SCRIPT)!r} hook"
    entries = {}
    for event in ("UserPromptSubmit", "PostModelRequest", "Stop", "Interrupt", "PermissionRequest"):
        timeout = 75 if event == "PermissionRequest" else 35 if event == "Stop" else 3 if event == "Interrupt" else 10
        entries[event] = [{"hooks":[{"type":"command","command":cmd,"timeout":timeout}]}]
    return entries


def is_ours_hook(item):
    try: return str(SCRIPT) in item.get("command", "")
    except (AttributeError, TypeError): return False


def ours(item):
    try: return any(is_ours_hook(h) for h in item.get("hooks", []))
    except (AttributeError, TypeError): return False


def strip_ours(item):
    """Remove Codextab hook commands while retaining sibling hooks and matchers."""
    if not isinstance(item, dict) or not isinstance(item.get("hooks"), list): return item
    remaining = [hook for hook in item["hooks"] if not is_ours_hook(hook)]
    if len(remaining) == len(item["hooks"]): return item
    if not remaining: return None
    cleaned = dict(item); cleaned["hooks"] = remaining
    return cleaned


def install_hooks(apply=False, remove=False):
    try: data = json.loads(HOOKS.read_text()) if HOOKS.exists() else {"hooks":{}}
    except (OSError, ValueError) as exc: raise RuntimeError(f"Cannot read hooks.json: {exc}")
    hooks = data.setdefault("hooks", {})
    for event, new_entries in hook_entries().items():
        old = hooks.get(event, [])
        if not isinstance(old, list): raise RuntimeError(f"hooks.{event} is not a list")
        remain = []
        for item in old:
            cleaned = strip_ours(item)
            if cleaned is not None: remain.append(cleaned)
        hooks[event] = remain + ([] if remove else new_entries)
        if not hooks[event]: hooks.pop(event)
    if not hooks: data.pop("hooks", None)
    rendered = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if not apply and not remove:
        print(rendered); return
    HOOKS.parent.mkdir(parents=True, exist_ok=True)
    tmp = HOOKS.with_suffix(".json.codextab.tmp")
    tmp.write_text(rendered)
    if HOOKS.exists(): os.chmod(tmp, HOOKS.stat().st_mode & 0o777)
    else: os.chmod(tmp, 0o600)
    os.replace(tmp, HOOKS)


def status():
    cfg = config()
    if not cfg.get("bot_token"):
        print("NO_BOT")
        return 0
    if not cfg.get("chat_id"):
        print("NOT_PAIRED")
        return 0
    try:
        hooks = json.loads(HOOKS.read_text()).get("hooks", {})
        hooks_ok = all(any(ours(item) for item in hooks.get(event, [])) for event in hook_entries())
    except (OSError, ValueError, TypeError):
        hooks_ok = False
    if not hooks_ok:
        print("HOOKS_MISSING")
        return 0
    proc = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/com.drasez.codextab"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if proc.returncode:
        print("RECEIVER_STOPPED")
        return 0
    print(f"READY @{cfg.get('bot_username', 'Codextab')}")
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__); sub = p.add_subparsers(dest="cmd", required=True)
    setup_parser = sub.add_parser("setup"); setup_parser.add_argument("--qr", action="store_true")
    link_parser = sub.add_parser("link"); link_parser.add_argument("--qr", action="store_true")
    sub.add_parser("wait"); sub.add_parser("status"); sub.add_parser("daemon"); sub.add_parser("hook")
    sub.add_parser("hooks-preview"); sub.add_parser("uninstall")
    a = p.parse_args()
    if a.cmd == "setup": return create_link(a.qr)
    if a.cmd == "link": return create_link(a.qr)
    if a.cmd == "wait": return wait_for_managed_bot()
    if a.cmd == "status": return status()
    if a.cmd == "daemon": return daemon()
    if a.cmd == "hook": return hook()
    if a.cmd == "hooks-preview": return install_hooks()
    if a.cmd == "uninstall":
        install_hooks(apply=True, remove=True)
        agent = pathlib.Path.home()/"Library/LaunchAgents/com.drasez.codextab.plist"
        uid = os.getuid(); subprocess.run(["launchctl","bootout",f"gui/{uid}",str(agent)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        if agent.exists(): agent.unlink()
        print("Хуки и фоновый обработчик Codextab отключены. Данные и токен оставлены в ~/.codextab."); return 0


if __name__ == "__main__":
    raise SystemExit(main() or 0)
