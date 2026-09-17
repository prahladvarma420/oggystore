"""
OGGY PREMIUM STORE - Web Admin
================================
A Flask web admin panel that talks to the SAME shopbot.db your Telegram bot uses.
It reuses the bot's own config, encryption (utils.crypto) and database helpers
(database.db) so the bot and the web admin stay perfectly in sync.

Run from the project root:  python web_admin/app.py
"""

import os
import re
import re as _re
import sys
import csv
import io
import time
import json
import html
import hmac
import hashlib
import secrets
import base64
import sqlite3
import datetime as dt
from functools import wraps
import threading

# --- Make the bot's own modules importable (config, utils, database) ----------
HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Load .env from the project root before importing config (config reads env vars)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
except Exception:
    pass

from flask import (
    Flask, request, session, jsonify, redirect, url_for,
    render_template, send_file, abort, Response,
)

# Import the bot's shared code -------------------------------------------------
import config

# CRITICAL: make the bot's DB helpers (database.db) open the SAME absolute
# database file as this web app, regardless of which folder the server is
# launched from. Without this, launching from inside web_admin/ makes
# database.db read/write a different (empty) shopbot.db, so ticket replies,
# closing tickets, clearing logs and broadcasts silently hit the wrong file.
config.DB_PATH = os.path.join(PROJECT_ROOT, getattr(config, "DB_PATH", "shopbot.db"))
try:
    os.chdir(PROJECT_ROOT)
except Exception:
    pass

from utils.crypto import encrypt_text, decrypt_text
from utils.timezone import now_text, now
from utils.premium_emoji import premium_safe
import database.db as botdb
import database.reseller_db as rdb  # Phase-1 reseller foundation (tested helpers)

# Ensure all tables / migrations exist (safe + idempotent)
botdb.init_db()

DB_PATH = os.path.join(PROJECT_ROOT, getattr(config, "DB_PATH", "shopbot.db"))

# Telegram Bot API base (used to push notifications/broadcasts straight away)
BOT_TOKEN = (getattr(config, "BOT_TOKEN", "") or os.getenv("BOT_TOKEN", "") or "").strip()
TELEGRAM_API = ("https://api.telegram.org/bot" + BOT_TOKEN + "/") if BOT_TOKEN else None


# ------------------------------------------------------------------ DB helpers
def get_db():
    """Fresh connection per request. WAL mode keeps bot + web from blocking."""
    from database.pgcompat import connect as _compat_connect  # SQLite/Postgres switch
    conn = _compat_connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=8000;")
    except Exception:
        pass
    return conn


def q(sql, params=(), one=False):
    conn = get_db()
    try:
        cur = conn.execute(sql, params)
        rows = cur.fetchall()
        return (dict(rows[0]) if rows else None) if one else [dict(r) for r in rows]
    finally:
        conn.close()


def ex(sql, params=()):
    conn = get_db()
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid, cur.rowcount
    finally:
        conn.close()


def has_column(table, column):
    """Backend-aware column probe.

    Was PRAGMA-only, so on Postgres it always returned False and _migrate()
    re-ran ALTER TABLE on every boot (silently swallowed).
    """
    try:
        from database.pgcompat import use_postgres
    except Exception:
        def use_postgres():
            return False
    try:
        if use_postgres():
            rows = q("SELECT column_name FROM information_schema.columns "
                     "WHERE table_schema='public' AND table_name=?", (table,))
            return any((r["column_name"] if isinstance(r, dict) else r[0]) == column
                       for r in rows)
        rows = q("PRAGMA table_info({})".format(table))
        return any(r["name"] == column for r in rows)
    except Exception:
        return False


def log_action(action, details=""):
    try:
        admin_id = session.get("admin_id", 0)
        try:
            ip = _client_ip()
        except Exception:
            ip = "?"
        detail = (str(details) + " | ip=" + ip) if details else ("ip=" + ip)
        ex("INSERT INTO admin_logs(admin_id, action, details) VALUES(?,?,?)",
           (admin_id, "[WEB] " + action, detail))
    except Exception:
        pass


TG_EMOJI_RE = _re.compile(r'<tg-emoji emoji-id="(\d+)">(.*?)</tg-emoji>', _re.S)


def strip_tg_emoji(text):
    """Replace <tg-emoji> tags with their plain fallback emoji."""
    return TG_EMOJI_RE.sub(lambda m: m.group(2), str(text or ""))


def button_name_icon(name):
    """Button-safe product name. First premium emoji becomes the button icon,
    remaining tags collapse to their fallback emoji (buttons cannot render HTML)."""
    s = str(name or "")
    m = TG_EMOJI_RE.search(s)
    icon = m.group(1) if m else None
    if m:
        s = s[:m.start()] + s[m.end():]
    s = TG_EMOJI_RE.sub(lambda mm: mm.group(2), s)
    return " ".join(s.split()), icon


def tg_send(chat_id, text, reply_markup=None):
    """Best-effort Telegram message. Never breaks the request if offline/unset."""
    if not TELEGRAM_API:
        return False, "BOT_TOKEN not set"
    try:
        import requests
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        prox = os.getenv("TELEGRAM_PROXY") or os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
        proxies = {"https": prox, "http": prox} if prox else None
        r = requests.post(TELEGRAM_API + "sendMessage", json=payload, timeout=(10, 20), proxies=proxies)
        ok = r.ok and r.json().get("ok", False)
        return ok, ("sent" if ok else r.text)
    except Exception as e:
        return False, str(e)


def _tg_reachable():
    """Quick check that THIS process can actually reach the Telegram API.
    Returns (ok, reason). Used to fail a broadcast fast with a clear message
    instead of timing out once per user."""
    if not TELEGRAM_API:
        return False, "BOT_TOKEN is not set in the web admin, so messages cannot be sent."
    try:
        import requests
        prox = os.getenv("TELEGRAM_PROXY") or os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
        proxies = {"https": prox, "http": prox} if prox else None
        r = requests.get(TELEGRAM_API + "getMe", timeout=(8, 12), proxies=proxies)
        if r.ok and r.json().get("ok"):
            return True, "ok"
        return False, "Telegram rejected the bot token: " + r.text[:200]
    except Exception as e:
        return False, ("This server cannot reach api.telegram.org (connection timed out). "
                       "Run the web admin on the same server as your bot, or set TELEGRAM_PROXY "
                       "in .env to the proxy your bot uses. Details: " + str(e)[:200])


# ----------------------------------------------------------------------- auth
def tg_notify(chat_id, text, reply_markup=None):
    """Fire-and-forget Telegram message: runs in a background thread so it
    never blocks (or slows) the web response."""
    def _worker():
        try:
            tg_send(chat_id, text, reply_markup)
        except Exception:
            pass
    try:
        threading.Thread(target=_worker, daemon=True).start()
    except Exception:
        pass


app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.getenv("WEB_ADMIN_SECRET") or secrets.token_hex(32)
app.permanent_session_lifetime = dt.timedelta(days=7)

# --- Security hardening ------------------------------------------------------
import base64
import struct
import glob

WEB_ADMIN_HTTPS = os.getenv("WEB_ADMIN_HTTPS", "0")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=(WEB_ADMIN_HTTPS == "1"),
    MAX_CONTENT_LENGTH=8 * 1024 * 1024,
)
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)

# --- Extra hardening knobs (optional via .env, safe defaults) ----------------
WEB_ADMIN_IDLE_MIN = int(os.getenv("WEB_ADMIN_IDLE_MIN", "30") or 0)
WEB_ADMIN_FORCE_2FA = os.getenv("WEB_ADMIN_FORCE_2FA", "0") == "1"
WEB_ADMIN_HSTS = os.getenv("WEB_ADMIN_HSTS", "0") == "1"
_allow_raw = os.getenv("WEB_ADMIN_IP_ALLOWLIST", "").strip()
IP_ALLOWLIST = [x.strip() for x in _allow_raw.replace(";", ",").split(",") if x.strip()]
RATE_LIMIT_MAX = int(os.getenv("WEB_ADMIN_RATE_LIMIT", "300") or 300)
RATE_LIMIT_WINDOW = 60
_RATE = {}


def _ip_allowed(ip):
    if ip in ("127.0.0.1", "::1", "localhost"):
        return True
    for entry in IP_ALLOWLIST:
        if entry == ip:
            return True
        if entry.endswith("*") and ip.startswith(entry[:-1]):
            return True
    return False


def _migrate():
    # Add new columns/tables the web admin needs (idempotent, safe to re-run).
    try:
        if not has_column("products", "cost"):
            ex("ALTER TABLE products ADD COLUMN cost REAL DEFAULT 0")
    except Exception:
        pass
    try:
        ex("CREATE TABLE IF NOT EXISTS web_login_log("
           "id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT, ip TEXT, "
           "user_agent TEXT, status TEXT, created TEXT DEFAULT (datetime(CURRENT_TIMESTAMP)))")
    except Exception:
        pass
    try:
        ex("CREATE TABLE IF NOT EXISTS web_sessions("
           "sid TEXT PRIMARY KEY, username TEXT, ip TEXT, user_agent TEXT, "
           "created TEXT DEFAULT (datetime(CURRENT_TIMESTAMP)), "
           "last_seen TEXT DEFAULT (datetime(CURRENT_TIMESTAMP)))")
    except Exception:
        pass


def sha256_hex(s):
    return hashlib.sha256(str(s).encode("utf-8")).hexdigest()


def csrf_token():
    tok = session.get("csrf")
    if not tok:
        tok = secrets.token_hex(16)
        session["csrf"] = tok
    return tok


@app.before_request
def _ip_and_rate_guard():
    if request.path.startswith("/static/"):
        return
    ip = _client_ip()
    if IP_ALLOWLIST and not _ip_allowed(ip):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "Your IP is not allowed"}), 403
        return Response("Forbidden", status=403)
    now_s = time.time()
    # Prevent the process-local limiter from growing forever when exposed to
    # many one-off source IPs. This keeps the existing lightweight design while
    # removing an avoidable memory-exhaustion vector.
    if len(_RATE) > 10000:
        stale = [key for key, value in _RATE.items()
                 if now_s - value[1] > RATE_LIMIT_WINDOW]
        for key in stale:
            _RATE.pop(key, None)
    bucket = _RATE.get(ip)
    if not bucket or now_s - bucket[1] > RATE_LIMIT_WINDOW:
        _RATE[ip] = [1, now_s]
    else:
        bucket[0] += 1
        if bucket[0] > RATE_LIMIT_MAX:
            return jsonify({"ok": False, "error": "Too many requests. Slow down."}), 429


@app.before_request
def _csrf_protect():
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    if request.path == "/login":
        # Login is no longer blanket-exempt. A token is issued on GET /login and
        # verified here, which blocks login-CSRF / forced-login.
        token = session.get("csrf")
        sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
        if token and sent and hmac.compare_digest(str(token), str(sent)):
            return
        if not token:
            # First ever POST in a brand new session: allow, then pin a token.
            session["csrf"] = secrets.token_urlsafe(32)
            return
        return jsonify({"ok": False, "error": "Invalid or missing CSRF token"}), 403
    token = session.get("csrf")
    sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
    if not token or not sent or not hmac.compare_digest(str(token), str(sent)):
        return jsonify({"ok": False, "error": "Invalid or missing CSRF token"}), 403


@app.after_request
def _security_headers(resp):
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-XSS-Protection"] = "1; mode=block"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "connect-src 'self'; base-uri 'self'; frame-ancestors 'none'"
    )
    if WEB_ADMIN_HSTS and WEB_ADMIN_HTTPS == "1":
        resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return resp


def _b32decode(secret):
    s = secret.strip().replace(" ", "").upper()
    pad = "=" * ((8 - len(s) % 8) % 8)
    return base64.b32decode(s + pad)


def totp_at(secret, for_time=None, step=30, digits=6):
    if for_time is None:
        for_time = time.time()
    counter = int(for_time // step)
    key = _b32decode(secret)
    h = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = h[-1] & 0x0F
    code = (struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def verify_totp(secret, code):
    code = str(code or "").strip()
    if not secret or not code:
        return False
    for drift in (-1, 0, 1):
        try:
            if hmac.compare_digest(totp_at(secret, time.time() + drift * 30), code):
                return True
        except Exception:
            continue
    return False


def gen_totp_secret():
    return base64.b32encode(os.urandom(20)).decode("utf-8").rstrip("=")


def twofa_enabled():
    try:
        return botdb.get_setting("web_2fa_enabled", "0") == "1"
    except Exception:
        return False


def twofa_secret():
    try:
        return botdb.get_setting("web_2fa_secret", "") or ""
    except Exception:
        return ""


def _recovery_hashes():
    try:
        return json.loads(botdb.get_setting("web_2fa_recovery", "[]") or "[]")
    except Exception:
        return []


def gen_recovery_codes(n=10):
    return ["-".join(secrets.token_hex(2) for _ in range(2)) for _ in range(n)]


def store_recovery_codes(codes):
    botdb.set_setting("web_2fa_recovery", json.dumps([sha256_hex(c) for c in codes]))


def consume_recovery_code(code):
    code = (code or "").strip().lower()
    if not code:
        return False
    h = sha256_hex(code)
    hashes = _recovery_hashes()
    if h in hashes:
        hashes.remove(h)
        botdb.set_setting("web_2fa_recovery", json.dumps(hashes))
        return True
    return False


_LOGIN_FAILS = {}
LOCK_THRESHOLD = 5
LOCK_WINDOW = 900


# Only honour X-Forwarded-For when explicitly told we sit behind a trusted
# proxy. Trusting it unconditionally let anyone bypass the IP allowlist, the
# rate limiter and the login lockout by spoofing a header.
TRUST_PROXY = (os.getenv("WEB_ADMIN_TRUST_PROXY", "") or "").strip().lower() in ("1", "true", "yes")


def _client_ip():
    if TRUST_PROXY:
        xff = request.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[0].strip()
    return request.remote_addr or "?"


def is_locked(ip):
    rec = _LOGIN_FAILS.get(ip)
    if not rec:
        return False
    if time.time() - rec[1] > LOCK_WINDOW:
        _LOGIN_FAILS.pop(ip, None)
        return False
    return rec[0] >= LOCK_THRESHOLD


def record_fail(ip):
    rec = _LOGIN_FAILS.get(ip)
    if not rec or time.time() - rec[1] > LOCK_WINDOW:
        _LOGIN_FAILS[ip] = [1, time.time()]
    else:
        rec[0] += 1


def clear_fails(ip):
    _LOGIN_FAILS.pop(ip, None)


def log_login(username, status):
    ip = _client_ip()
    ua = request.headers.get("User-Agent", "")[:300]
    try:
        ex("INSERT INTO web_login_log(username, ip, user_agent, status) VALUES(?,?,?,?)",
           (username, ip, ua, status))
    except Exception:
        pass
    try:
        if ADMIN_ID:
            tg_notify(ADMIN_ID, "Web admin login " + str(status) + " | user: " + str(username) + " | ip: " + str(ip))
    except Exception:
        pass


_migrate()


TEMPLATE_DIR = os.path.join(HERE, "templates")


def _logo_url():
    try:
        return botdb.get_setting("web_logo", "") or ""
    except Exception:
        return ""


def _login_logo_html():
    u = _logo_url()
    if u:
        return ('<img src="' + u + '?v=' + str(int(time.time())) +
                '" alt="logo" style="max-width:170px;max-height:90px;border-radius:14px;'
                'display:block;margin:4px auto 2px">')
    return '<div class="logo-glow">\u26a1</div>'


def _dash_logo_html():
    u = _logo_url()
    if u:
        return ('<img src="' + u + '?v=' + str(int(time.time())) +
                '" alt="logo" style="width:40px;height:40px;border-radius:10px;object-fit:cover">')
    return '<span class="brand-bolt">\u26a1</span>'


def _sess_now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def record_session(sid):
    try:
        ex("INSERT OR REPLACE INTO web_sessions(sid, username, ip, user_agent, created, last_seen) "
           "VALUES(?,?,?,?,?,?)",
           (sid, session.get("username", "admin"), _client_ip(),
            (request.headers.get("User-Agent") or "")[:300], _sess_now(), _sess_now()))
    except Exception:
        pass


def touch_session(sid):
    try:
        ex("UPDATE web_sessions SET last_seen=? WHERE sid=?", (_sess_now(), sid))
    except Exception:
        pass


def session_active(sid):
    if not sid:
        return False
    try:
        return bool(q("SELECT sid FROM web_sessions WHERE sid=?", (sid,), one=True))
    except Exception:
        return True


def list_sessions():
    try:
        return q("SELECT sid, username, ip, user_agent, created, last_seen "
                 "FROM web_sessions ORDER BY last_seen DESC")
    except Exception:
        return []


def revoke_session(sid):
    try:
        ex("DELETE FROM web_sessions WHERE sid=?", (sid,))
    except Exception:
        pass


def revoke_sessions(except_sid=None):
    try:
        if except_sid:
            ex("DELETE FROM web_sessions WHERE sid<>?", (except_sid,))
        else:
            ex("DELETE FROM web_sessions")
    except Exception:
        pass


def render_page(filename, **subs):
    with open(os.path.join(TEMPLATE_DIR, filename), encoding="utf-8") as f:
        html = f.read()
    defaults = {
        "LOGO": _login_logo_html(),
        "LOGO_DASH": _dash_logo_html(),
        "STORE": getattr(config, "STORE_NAME", "Store"),
    }
    defaults.update(subs)
    for k, v in defaults.items():
        html = html.replace("%%" + k + "%%", str(v))
    return html


def verify_password(password):
    """Supports hashed (recommended) or plaintext credentials from .env."""
    salt = os.getenv("WEB_ADMIN_PASSWORD_SALT")
    hashed = os.getenv("WEB_ADMIN_PASSWORD_HASH")
    if salt and hashed:
        calc = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), 300_000
        ).hex()
        return hmac.compare_digest(calc, hashed)
    plain = os.getenv("WEB_ADMIN_PASSWORD", "")
    if not plain:
        # Refuse to authenticate against the old "admin" fallback. A deploy that
        # forgot WEB_ADMIN_PASSWORD used to be reachable with admin/admin.
        print("[web_admin] REFUSING LOGIN: WEB_ADMIN_PASSWORD / "
              "WEB_ADMIN_PASSWORD_HASH is not configured.", flush=True)
        return False
    return hmac.compare_digest(password, plain)


# -------- DB-backed, hashed web admin credentials (no plaintext anywhere) -----
WEB_CRED_ITERS = 600_000


def _cred_pepper():
    # Server-side secret pepper, kept in .env (never in the DB). With it, a stolen
    # database alone is useless; without the DB, the pepper alone is useless too.
    return (os.getenv("DATA_ENCRYPTION_KEY") or os.getenv("WEB_ADMIN_SECRET") or "").encode("utf-8")


def hash_web_password(password):
    """One-way, salted + peppered PBKDF2-SHA256 hash. Cannot be decrypted."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8") + _cred_pepper(), salt, WEB_CRED_ITERS)
    return "pbkdf2_sha256$%d$%s$%s" % (WEB_CRED_ITERS, salt.hex(), dk.hex())


def verify_web_password(password, stored):
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8") + _cred_pepper(), bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


def hash_web_username(username):
    """Keyed (peppered) HMAC of the username so it is never stored in the clear."""
    return hmac.new(_cred_pepper() or b"oggy-admin", (username or "").strip().encode("utf-8"), hashlib.sha256).hexdigest()


def store_web_credentials(username, password):
    botdb.set_setting("web_admin_username_h", hash_web_username(username))
    botdb.set_setting("web_admin_pwd", hash_web_password(password))


def verify_web_credentials(username, password):
    """Check login against the hashed credentials stored in the database.
    On first use (DB empty) it validates the legacy .env credentials once and
    silently migrates them into the database in hashed form."""
    uh = botdb.get_setting("web_admin_username_h", "")
    ph = botdb.get_setting("web_admin_pwd", "")
    if uh and ph:
        return hmac.compare_digest(hash_web_username(username), uh) and verify_web_password(password, ph)
    # one-time migration: validate against .env, then move into the DB (hashed)
    if username == os.getenv("WEB_ADMIN_USERNAME", "admin") and verify_password(password):
        try:
            store_web_credentials(username, password)
            log_action("migrate_credentials", "Web admin credentials moved to encrypted DB storage")
        except Exception:
            pass
        return True
    return False


# ============================ Multiple web admin accounts (each with own 2FA) =
def _ensure_web_admins_table():
    ex("CREATE TABLE IF NOT EXISTS web_admins("
       "id INTEGER PRIMARY KEY AUTOINCREMENT, "
       "username TEXT UNIQUE NOT NULL, "
       "pwd TEXT NOT NULL, "
       "role TEXT DEFAULT 'admin', "
       "active INTEGER DEFAULT 1, "
       "twofa_secret TEXT DEFAULT '', "
       "twofa_enabled INTEGER DEFAULT 0, "
       "twofa_recovery TEXT DEFAULT '[]', "
       "created TEXT DEFAULT (datetime(CURRENT_TIMESTAMP)), "
       "last_login TEXT)")


def get_web_admin(username):
    return q("SELECT * FROM web_admins WHERE username=? AND active=1", (username,), one=True)


def get_web_admin_any(username):
    return q("SELECT * FROM web_admins WHERE username=?", (username,), one=True)


def get_web_admin_by_id(aid):
    return q("SELECT * FROM web_admins WHERE id=?", (aid,), one=True)


def list_web_admins():
    return q("SELECT id, username, role, active, twofa_enabled, created, last_login "
             "FROM web_admins ORDER BY id ASC")


def _active_owner_count():
    row = q("SELECT COUNT(*) c FROM web_admins WHERE role='owner' AND active=1", one=True)
    return (row or {}).get("c", 0)


def _is_owner():
    return session.get("web_role") == "owner"


def _admin_recovery_hashes(aid):
    row = get_web_admin_by_id(aid)
    try:
        return json.loads((row or {}).get("twofa_recovery") or "[]")
    except Exception:
        return []


def consume_admin_recovery(aid, code):
    code = (code or "").strip().lower()
    if not code:
        return False
    h = sha256_hex(code)
    hashes = _admin_recovery_hashes(aid)
    if h in hashes:
        hashes.remove(h)
        ex("UPDATE web_admins SET twofa_recovery=? WHERE id=?", (json.dumps(hashes), aid))
        return True
    return False


def _ensure_web_admins_seeded():
    """On first run, create the web_admins table and seed the first OWNER from the
    existing single-admin credentials (or .env), inheriting any 2FA already set."""
    try:
        _ensure_web_admins_table()
        row = q("SELECT COUNT(*) c FROM web_admins", one=True)
        if row and row.get("c"):
            return
        username = os.getenv("WEB_ADMIN_USERNAME", "admin")
        pwd = botdb.get_setting("web_admin_pwd", "")
        if not pwd:
            _seed_pw = os.getenv("WEB_ADMIN_PASSWORD", "")
            if not _seed_pw:
                # Never seed a well-known default owner account.
                print("[web_admin] Skipping owner seed: set WEB_ADMIN_PASSWORD first.",
                      flush=True)
                return
            pwd = hash_web_password(_seed_pw)
        g_secret = botdb.get_setting("web_2fa_secret", "") or ""
        g_enabled = 1 if botdb.get_setting("web_2fa_enabled", "0") == "1" else 0
        g_recovery = botdb.get_setting("web_2fa_recovery", "[]") or "[]"
        ex("INSERT INTO web_admins(username, pwd, role, active, twofa_secret, twofa_enabled, twofa_recovery) "
           "VALUES(?,?,?,?,?,?,?)",
           (username, pwd, "owner", 1, g_secret, g_enabled, g_recovery))
    except Exception:
        pass


_ensure_web_admins_seeded()


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("logged_in"):
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "unauthorized"}), 401
            return redirect(url_for("login"))
        if WEB_ADMIN_IDLE_MIN > 0:
            now_s = time.time()
            last = session.get("last_active", 0)
            if last and now_s - last > WEB_ADMIN_IDLE_MIN * 60:
                session.clear()
                if request.path.startswith("/api/"):
                    return jsonify({"ok": False, "error": "session expired"}), 401
                return redirect(url_for("login"))
            session["last_active"] = now_s
        sid = session.get("sid")
        if sid and not session_active(sid):
            session.clear()
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "session revoked"}), 401
            return redirect(url_for("login"))
        if sid:
            touch_session(sid)
        return fn(*a, **kw)
    return wrapper


def owner_required(fn):
    """Restrict security-sensitive operations to the owner role."""
    @wraps(fn)
    def wrapper(*a, **kw):
        if session.get("web_role") != "owner":
            return jsonify({"ok": False, "error": "Owner permission required"}), 403
        return fn(*a, **kw)
    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    store = getattr(config, "STORE_NAME", "Store")

    def _otpauth(secret, label):
        issuer = store.replace(":", " ")
        enc_issuer = issuer.replace(" ", "%20")
        return "otpauth://totp/" + enc_issuer + ":" + label + "?secret=" + secret + "&issuer=" + enc_issuer

    def _fields_credentials():
        return (
            '<input type="hidden" name="step" value="credentials">\n'
            '    <label class="field">\n'
            '      <span>Username</span>\n'
            '      <input type="text" name="username" autocomplete="username" placeholder="admin" required autofocus>\n'
            '    </label>\n'
            '    <label class="field">\n'
            '      <span>Password</span>\n'
            '      <input type="password" name="password" autocomplete="current-password" placeholder="password" required>\n'
            '    </label>'
        )

    def _fields_twofa():
        return (
            '<input type="hidden" name="step" value="twofa">\n'
            '    <label class="field">\n'
            '      <span>2FA code (Google Authenticator)</span>\n'
            '      <input type="text" name="code" inputmode="numeric" autocomplete="one-time-code" placeholder="123456" required autofocus>\n'
            '    </label>'
        )

    def _fields_setup(secret, otpauth):
        # Generate the enrollment QR locally. The previous implementation sent
        # the full otpauth URI (including the TOTP seed) to a third-party QR
        # service, which disclosed the second-factor secret.
        try:
            import qrcode
            qr_io = io.BytesIO()
            qrcode.make(otpauth).save(qr_io, format="PNG")
            qr = "data:image/png;base64," + base64.b64encode(qr_io.getvalue()).decode("ascii")
            qr_html = '<div style="text-align:center;margin:4px 0 12px"><img src="' + qr + '" alt="Authenticator enrollment QR code" style="width:190px;height:190px;border-radius:12px;background:#fff;padding:8px"></div>\n'
        except Exception:
            # Manual enrollment remains fully functional if the optional QR
            # renderer is unavailable during a partial/legacy deployment.
            qr_html = '<p class="alert error">QR generation is unavailable. Enter the manual key in your authenticator app.</p>\n'
        return (
            '<input type="hidden" name="step" value="setup_2fa">\n'
            '    <p style="color:#9aa;margin:0 0 10px;font-size:13px">Scan this QR in Google Authenticator or Authy (or type the key), then enter the 6-digit code to finish.</p>\n'
            + qr_html +
            '    <label class="field">\n'
            '      <span>Manual key</span>\n'
            '      <input type="text" value="' + secret + '" readonly onclick="this.select()">\n'
            '    </label>\n'
            '    <label class="field">\n'
            '      <span>6-digit code</span>\n'
            '      <input type="text" name="code" inputmode="numeric" autocomplete="one-time-code" placeholder="123456" required autofocus>\n'
            '    </label>'
        )

    def _show(fields, subtitle, btn, error=""):
        block = ('<div class="alert error">%s</div>' % error) if error else ""
        # csrf_token() issues (and pins) the session token, which the hidden
        # csrf_token field in login.html posts back. Without this the login
        # form submits no token and _csrf_protect rejects every attempt.
        return render_page("login.html", STORE=store, FIELDS=fields, BTN_LABEL=btn,
                           SUBTITLE=subtitle, ERROR_BLOCK=block,
                           CSRF=csrf_token())

    def _show_step1(error=""):
        return _show(_fields_credentials(), "Web Admin Control Center", "Login", error)

    def _show_step2(error=""):
        return _show(_fields_twofa(), "Enter your 2FA code to continue", "Verify & Enter", error)

    def _show_setup(error=""):
        secret = session.get("setup_2fa_secret")
        if not secret:
            return _show_step1("Your session expired. Please log in again.")
        label = session.get("setup_2fa_user", "admin")
        return _show(_fields_setup(secret, _otpauth(secret, label)),
                     "Set up two-factor authentication (required)", "Confirm & Continue", error)

    def _clear_pending():
        for k in ("pre_2fa", "pre_2fa_user", "pre_2fa_id", "pre_2fa_time",
                  "setup_2fa_secret", "setup_2fa_user", "setup_2fa_id", "setup_2fa_time"):
            session.pop(k, None)

    def _set_session(admin):
        session.permanent = True
        session["logged_in"] = True
        session["username"] = admin["username"]
        session["wa_id"] = admin["id"]
        session["web_role"] = admin.get("role", "admin")
        session["admin_id"] = ADMIN_ID
        sid = secrets.token_hex(16)
        session["sid"] = sid
        record_session(sid)
        try:
            ex("UPDATE web_admins SET last_login=? WHERE id=?", (now_text(), admin["id"]))
        except Exception:
            pass

    def _complete_login(admin):
        clear_fails(_client_ip())
        _clear_pending()
        _set_session(admin)
        log_action("login", "Web admin login: " + admin["username"])
        log_login(admin["username"], "success")
        return redirect(url_for("index"))

    def _recovery_page(codes):
        items = "".join('<code style="display:inline-block;margin:5px 8px;font-size:15px;letter-spacing:1px">%s</code>' % c for c in codes)
        html = (
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>Recovery codes</title><style>'
            'body{background:#0b0b14;color:#e8e8f0;font-family:system-ui,Segoe UI,Arial,sans-serif;'
            'display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}'
            '.card{background:#15151f;border:1px solid #2a2a3a;border-radius:16px;padding:28px;max-width:540px;'
            'box-shadow:0 0 40px rgba(120,80,255,.25)}h2{margin:0 0 8px}p{color:#9aa;line-height:1.5}'
            '.codes{background:#0e0e18;border-radius:12px;padding:14px;text-align:center;margin:14px 0}'
            'a.btn{display:inline-block;margin-top:8px;background:linear-gradient(90deg,#7a5cff,#00d4ff);'
            'color:#fff;text-decoration:none;padding:11px 20px;border-radius:10px;font-weight:600}'
            '</style></head><body><div class="card">'
            '<h2>Save your recovery codes</h2>'
            '<p>Two-factor authentication is now enabled on your account. Store these codes somewhere safe \u2014 '
            'each one works once if you ever lose your authenticator app. They will not be shown again.</p>'
            '<div class="codes">' + items + '</div>'
            '<a class="btn" href="/">Continue to dashboard</a>'
            '</div></body></html>'
        )
        return Response(html)

    if request.method == "POST":
        ip = _client_ip()
        if is_locked(ip):
            log_login((request.form.get("username") or "").strip() or "?", "locked")
            return _show_step1("Too many attempts. Please wait a few minutes and try again.")

        step = (request.form.get("step") or "credentials").strip()

        # ---- Step 2a: verify the 2FA code for an account that already has 2FA --
        if step == "twofa":
            if not session.get("pre_2fa") or not session.get("pre_2fa_id"):
                return _show_step1("Your session expired. Please log in again.")
            if time.time() - session.get("pre_2fa_time", 0) > 300:
                _clear_pending()
                return _show_step1("Your session expired. Please log in again.")
            admin = get_web_admin_by_id(session.get("pre_2fa_id"))
            if not admin or int(admin.get("active") or 0) != 1:
                _clear_pending()
                return _show_step1("Account not found. Please log in again.")
            code = (request.form.get("code") or "").strip()
            if verify_totp(decrypt_text(admin.get("twofa_secret")), code) or consume_admin_recovery(admin["id"], code):
                return _complete_login(admin)
            record_fail(ip)
            log_login(admin["username"], "bad_2fa")
            return _show_step2("Invalid 2FA code")

        # ---- Step 2b: mandatory first-login 2FA setup --------------------------
        if step == "setup_2fa":
            if not session.get("setup_2fa_secret") or not session.get("setup_2fa_id"):
                return _show_step1("Your session expired. Please log in again.")
            if time.time() - session.get("setup_2fa_time", 0) > 600:
                _clear_pending()
                return _show_step1("Setup timed out. Please log in again.")
            admin = get_web_admin_by_id(session.get("setup_2fa_id"))
            if not admin or int(admin.get("active") or 0) != 1:
                _clear_pending()
                return _show_step1("Account not found. Please log in again.")
            secret = session.get("setup_2fa_secret")
            code = (request.form.get("code") or "").strip()
            if not verify_totp(secret, code):
                record_fail(ip)
                return _show_setup("That code did not match. Enter the current 6-digit code.")
            codes = gen_recovery_codes()
            ex("UPDATE web_admins SET twofa_secret=?, twofa_enabled=1, twofa_recovery=? WHERE id=?",
               (encrypt_text(secret), json.dumps([sha256_hex(c) for c in codes]), admin["id"]))
            log_action("enable_2fa", "2FA set up at first login: " + admin["username"])
            log_login(admin["username"], "success")
            _set_session(get_web_admin_by_id(admin["id"]))
            clear_fails(ip)
            _clear_pending()
            return _recovery_page(codes)

        # ---- Step 1: verify username + password --------------------------------
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        admin = get_web_admin(username)
        if admin and verify_web_password(password, admin.get("pwd") or ""):
            if int(admin.get("twofa_enabled") or 0) == 1 and admin.get("twofa_secret"):
                session["pre_2fa"] = True
                session["pre_2fa_user"] = admin["username"]
                session["pre_2fa_id"] = admin["id"]
                session["pre_2fa_time"] = time.time()
                return _show_step2()
            # No 2FA yet -> force the admin to set it up before entering.
            secret = gen_totp_secret()
            session["setup_2fa_secret"] = secret
            session["setup_2fa_user"] = admin["username"]
            session["setup_2fa_id"] = admin["id"]
            session["setup_2fa_time"] = time.time()
            return _show_setup()
        else:
            # Defend against username enumeration timing differences.
            verify_web_password(password, "pbkdf2_sha256$600000$00$00")

        record_fail(ip)
        log_login(username or "?", "failed")
        return _show_step1("Invalid username or password")

    # GET -> always start clean at step 1
    if session.get("logged_in"):
        return redirect(url_for("index"))
    _clear_pending()
    return _show_step1()


@app.route("/logout")
def logout():
    try:
        revoke_session(session.get("sid"))
    except Exception:
        pass
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    return render_page("index.html",
                       STORE=getattr(config, "STORE_NAME", "Store"),
                       USERNAME=session.get("username", "admin"),
                       ROLE=session.get("web_role", "admin"),
                       CSRF=csrf_token(),
                       BOT=("online" if TELEGRAM_API else "offline"))


# ============================================================== API: DASHBOARD
@app.route("/api/dashboard")
@login_required
def api_dashboard():
    stats = botdb.smart_dashboard_stats()
    # Owner + promoted admins: their own purchases are shown separately and
    # excluded from the revenue counters/series so numbers reflect real sales.
    _admin_ids = sorted(botdb._admin_user_ids())
    not_admin = (" AND user_id NOT IN (%s)" % ",".join(str(i) for i in _admin_ids)) if _admin_ids else ""
    # Revenue reset marker (UTC, same clock as orders.created). Empty = all-time.
    reset_setting = botdb.get_setting("revenue_reset_at", "") or ""
    reset_at = reset_setting or "1970-01-01 00:00:00"
    # 7-day revenue series in one grouped query (formerly seven connections
    # and seven scans, which was especially slow against remote Postgres).
    series_days = [(now() - dt.timedelta(days=i)).strftime("%Y-%m-%d")
                   for i in range(6, -1, -1)]
    grouped = q("SELECT DATE(created) AS day, COALESCE(SUM(price),0) AS rev, "
                "COUNT(*) AS cnt FROM orders WHERE DATE(created)>=? AND created>=?" +
                not_admin + " GROUP BY DATE(created)", (series_days[0], reset_at))
    grouped_map = {str(r["day"]): r for r in grouped}
    series = [{"date": day,
               "revenue": round((grouped_map.get(day) or {}).get("rev", 0) or 0, 2),
               "orders": (grouped_map.get(day) or {}).get("cnt", 0) or 0}
              for day in series_days]
    totals = {
        "users": (q("SELECT COUNT(*) c FROM users", one=True) or {}).get("c", 0),
        "products": (q("SELECT COUNT(*) c FROM products", one=True) or {}).get("c", 0),
        "active_products": (q("SELECT COUNT(*) c FROM products WHERE active=1", one=True) or {}).get("c", 0),
        "deposits": (q("SELECT COALESCE(SUM(amount),0) s FROM deposits WHERE status='paid' AND created >= ?", (reset_at,), one=True) or {}).get("s", 0),
        "blocked": (q("SELECT COUNT(*) c FROM users WHERE blocked=1", one=True) or {}).get("c", 0),
        "pending_withdrawals": (q("SELECT COUNT(*) c FROM withdrawal_requests WHERE status='pending'", one=True) or {}).get("c", 0),
    }
    # top sellers
    top = q("SELECT product_name, COUNT(*) sold, COALESCE(SUM(price),0) revenue "
            "FROM orders WHERE created >= ?" + not_admin + " GROUP BY product_name ORDER BY sold DESC LIMIT 5", (reset_at,))
    recent = q("SELECT o.id, o.user_id, o.product_name, o.price, o.created, "
               "u.first_name, u.username FROM orders o LEFT JOIN users u "
               "ON o.user_id=u.user_id ORDER BY o.id DESC LIMIT 8")
    return jsonify({"ok": True, "stats": stats, "series": series,
                    "totals": totals, "top": top, "recent": recent,
                    "revenue_reset_at": reset_setting,
                    "bot_connected": bool(TELEGRAM_API)})


@app.route("/api/revenue/reset", methods=["POST"])
@login_required
def api_revenue_reset():
    """Reset revenue stats from now. Non-destructive: only stores a marker,
    order history is never deleted and the reset can be undone anytime."""
    stamp = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    botdb.set_setting("revenue_reset_at", stamp)
    log_action("revenue_reset", stamp)
    return jsonify({"ok": True, "reset_at": stamp})


@app.route("/api/revenue/restore", methods=["POST"])
@login_required
def api_revenue_restore():
    """Undo the revenue reset and show all-time stats again."""
    botdb.set_setting("revenue_reset_at", "")
    log_action("revenue_restore", "all-time stats restored")
    return jsonify({"ok": True})


# =============================================================== API: PRODUCTS
def product_row(p, avail_map=None, sold_map=None, tiers_map=None):
    """Serialize one product row. When the *_map lookups are provided (from the
    grouped queries in api_products) NO extra queries run per product; without
    them it falls back to the per-product helpers (backward compatible)."""
    pid = p["id"]
    if avail_map is None:
        avail = botdb.get_available_stock_count(pid)
        sold = botdb.get_sold_stock_count(pid)
        tiers = [{"quantity": t[1], "price": t[2]} for t in botdb.get_product_price_tiers(pid)]
    else:
        avail = avail_map.get(pid, 0)
        sold = (sold_map or {}).get(pid, 0)
        tiers = (tiers_map or {}).get(pid, [])
    p = dict(p)
    p["available_stock"] = avail
    p["sold_count"] = sold
    p["tiers"] = tiers
    return p


@app.route("/api/products")
@login_required
def api_products():
    search = (request.args.get("search") or "").strip()
    base = ("SELECT p.*, c.name AS category_name FROM products p "
            "LEFT JOIN categories c ON c.id = p.category_id")
    if search:
        rows = q(base + " WHERE p.name LIKE ? ORDER BY p.id DESC",
                 ("%" + search + "%",))
    else:
        rows = q(base + " ORDER BY p.id DESC")
    # Stock counts + price tiers via 3 grouped queries TOTAL instead of 3 per
    # product -- the per-product round trips made this page lag badly on
    # remote PostgreSQL.
    avail_map = {r["product_id"]: r["c"] for r in q("SELECT product_id, COUNT(*) AS c FROM product_stock WHERE sold=0 GROUP BY product_id")}
    sold_map = {r["product_id"]: r["c"] for r in q("SELECT product_id, COUNT(*) AS c FROM product_stock WHERE sold=1 GROUP BY product_id")}
    tiers_map = {}
    for t in q("SELECT product_id, quantity, price FROM product_price_tiers ORDER BY quantity ASC"):
        tiers_map.setdefault(t["product_id"], []).append({"quantity": t["quantity"], "price": t["price"]})
    return jsonify({"ok": True, "products": [product_row(p, avail_map, sold_map, tiers_map) for p in rows]})


# ---------------- Product categories (same data the bot shop uses) ----------------

@app.route("/api/categories")
@login_required
def api_categories():
    rows = q(
        "SELECT c.*, (SELECT COUNT(*) FROM products p WHERE p.category_id = c.id) AS product_count "
        "FROM categories c ORDER BY c.sort_order ASC, c.id ASC"
    )
    return jsonify({"ok": True, "categories": rows})


@app.route("/api/categories/create", methods=["POST"])
@login_required
def api_category_create():
    d = request.get_json(force=True)
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "Name required"}), 400
    if botdb.get_category_by_name(name):
        return jsonify({"ok": False, "error": "A category with this name already exists"}), 400
    cid = botdb.create_category(name, d.get("icon") or "")
    try:
        if d.get("sort_order") not in (None, ""):
            botdb.set_category_sort(cid, int(d.get("sort_order")))
    except (TypeError, ValueError):
        pass
    log_action("create_category", "#{} {}".format(cid, name))
    return jsonify({"ok": True, "id": cid})


@app.route("/api/categories/<int:cid>/update", methods=["POST"])
@login_required
def api_category_update(cid):
    d = request.get_json(force=True)
    if not botdb.get_category(cid):
        return jsonify({"ok": False, "error": "Category not found"}), 404
    if "name" in d and d["name"] is not None:
        name = str(d["name"]).strip()
        if not name:
            return jsonify({"ok": False, "error": "Name required"}), 400
        existing = botdb.get_category_by_name(name)
        if existing and int(existing[0]) != int(cid):
            return jsonify({"ok": False, "error": "A category with this name already exists"}), 400
        botdb.rename_category(cid, name)
    if "icon" in d and d["icon"] is not None:
        botdb.set_category_icon(cid, d["icon"])
    if "sort_order" in d and d["sort_order"] not in (None, ""):
        try:
            botdb.set_category_sort(cid, int(d["sort_order"]))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "Invalid sort order"}), 400
    if "active" in d and d["active"] is not None:
        botdb.set_category_active(cid, 1 if bool(d["active"]) else 0)
    log_action("update_category", "#{}".format(cid))
    return jsonify({"ok": True})


@app.route("/api/categories/<int:cid>/delete", methods=["POST"])
@login_required
def api_category_delete(cid):
    if not botdb.get_category(cid):
        return jsonify({"ok": False, "error": "Category not found"}), 404
    # Products are NOT deleted: they fall back to Uncategorized (NULL) and
    # stay visible in the bot shop under "Other".
    botdb.delete_category(cid)
    log_action("delete_category", "#{}".format(cid))
    return jsonify({"ok": True})


@app.route("/api/categories/<int:cid>/move", methods=["POST"])
@login_required
def api_category_move(cid):
    d = request.get_json(force=True)
    direction = "up" if str(d.get("direction", "")).lower() == "up" else "down"
    botdb.move_category(cid, direction)
    return jsonify({"ok": True})


def _broadcast_to_all(msg, pid=None, button_text=None, button_callback=None):
    """Send a message to every eligible bot user directly from the web admin."""
    kb = None
    if pid and button_text:
        kb = {"inline_keyboard": [[{"text": button_text, "callback_data": button_callback or "product_{}".format(pid), "style": "primary"}]]}
    sent = failed = 0
    try:
        users = q("SELECT user_id FROM users WHERE COALESCE(blocked,0)=0 AND COALESCE(notifications,1)=1")
    except Exception:
        return 0, 0
    for _i, u in enumerate(users):
        # Telegram allows ~30 msg/sec. Blasting with no pacing guaranteed 429s
        # and made every subsequent send count as "failed".
        if _i and _i % 25 == 0:
            time.sleep(1.0)
        ok_send, _info = tg_send(u["user_id"], msg, reply_markup=kb)
        if ok_send:
            sent += 1
        else:
            failed += 1
    return sent, failed


def _product_alert_message(name, price, added, available, mode):
    if mode == "product":
        key = "new_product_broadcast"
        default_msg = "🆕 <b>New Product Added!</b>\n\n📦 <b>{product}</b>\n➕ <b>Added:</b> <code>{added}</code>\n✅ <b>Available:</b> <code>{available}</code>\n💰 <b>Price:</b> <code>{price}</code>\n\n<i>⚡ New product is now available!</i>"
    else:
        key = "stock_added_broadcast"
        default_msg = "🆕 <b>New Stock Added!</b>\n\n📦 <b>{product}</b>\n➕ <b>Added:</b> <code>{added}</code>\n✅ <b>Available:</b> <code>{available}</code>\n💰 <b>Price:</b> <code>{price}</code>\n\n<i>⚡ Grab yours before they’re gone!</i>"
    return botdb.render_message_template(
        key,
        default_msg,
        product_name=premium_safe(name),
        added=added,
        available=available,
        price="${:.2f}".format(float(price or 0)),
    )


def _publish_offer_alert(pid):
    """Broadcast the current price offers of a product to all users (one message)."""
    try:
        prow = q("SELECT name, price FROM products WHERE id=?", (pid,))
        tiers = botdb.get_product_price_tiers(pid)
        if not prow or not tiers:
            return 0, 0
        available = botdb.get_display_stock(pid)
        msg = _offer_alert_message(prow[0]["name"], prow[0]["price"], tiers, available)
        sent, failed = _broadcast_to_all(msg, pid=pid, button_text="\U0001f6d2 Tap to Buy Now")
        log_action("offer_broadcast", "#{} sent={} failed={}".format(pid, sent, failed))
        return sent, failed
    except Exception:
        return 0, 0


def _offer_alert_message(name, price, tiers, available):
    # Show exactly what the buy flow charges for each quantity (same math as
    # the bot's calculate_total_price), so the alert can never advertise
    # stale or contradictory prices.
    base = float(price or 0)
    lines = []
    for _tid, qty, tier_price in tiers:
        qty = int(qty)
        best_unit = base if base > 0 else (float(tier_price) / qty if qty else float(tier_price))
        for _t2, q2, p2 in tiers:
            q2 = int(q2)
            if 0 < q2 <= qty:
                unit = float(p2) / q2
                if unit < best_unit:
                    best_unit = unit
        shown_total = round(best_unit * qty, 2)
        each = shown_total / qty if qty else shown_total
        lines.append("\u2022 <b>{}</b> for <b>${:.2f}</b> (${:.2f} each)".format(qty, shown_total, each))
    return botdb.render_message_template(
        "new_offer_broadcast",
        "\U0001f525 <b>NEW OFFER ALERT!</b>\n\n\U0001f4e6 <b>{product}</b>\n\U0001f4b0 <b>Price:</b> <code>{price}</code> / code\n\u2705 <b>Available:</b> <code>{available}</code>\n\n\U0001f381 <b>BULK DISCOUNT ACTIVE</b>\n{offers}\n\n\u26a1 <i>Grab this deal before it\u2019s gone!</i>",
        product_name=premium_safe(name),
        price="${:.2f}".format(base),
        available=available,
        offers="\n".join(lines),
    )


def _price_change_message(name, old_price, new_price, available):
    if new_price < old_price:
        pct = round((old_price - new_price) / old_price * 100) if old_price > 0 else 0
        tpl_key, tpl_default = "price_down_broadcast", botdb.PRICE_DOWN_BROADCAST_DEFAULT
    else:
        pct = round((new_price - old_price) / old_price * 100) if old_price > 0 else 0
        tpl_key, tpl_default = "price_up_broadcast", botdb.PRICE_UP_BROADCAST_DEFAULT
    bar = "\u2501" * 18
    if available and int(available) > 0:
        tail = "\U0001f6d2 <i>Tap below to grab it now!</i> \U0001f447"
    else:
        tail = "\u23f3 <i>Out of stock \u2014 restocking soon!</i> \U0001f514"
    return botdb.render_message_template(
        tpl_key,
        tpl_default,
        product_name=premium_safe(name),
        old_price="${:.2f}".format(old_price),
        new_price="${:.2f}".format(new_price),
        pct=pct,
        stock=available,
        bar=bar,
        tail=tail,
    )


@app.route("/api/products/create", methods=["POST"])
@login_required
def api_product_create():
    d = request.get_json(force=True)
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "Name required"}), 400
    price = float(d.get("price") or 0)
    delivery = (d.get("delivery") or "manual").strip() or "manual"
    pid, _ = ex(
        "INSERT INTO products(name, price, stock, delivery, active, description, "
        "validity, warranty, rules, redeem_link, max_per_order, cost) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (name, price, 0, delivery, int(bool(d.get("active", 1))),
         d.get("description", "") or "", d.get("validity", "Not mentioned") or "Not mentioned",
         d.get("warranty", "No warranty") or "No warranty",
         d.get("rules", "No rules mentioned") or "No rules mentioned",
         d.get("redeem_link", "Not required") or "Not required",
         int(d.get("max_per_order", 1) or 1),
         float(d.get("cost", 0) or 0)),
    )
    # Optional category assignment (null/0/"" = Uncategorized)
    try:
        cat_raw = d.get("category_id")
        if cat_raw not in (None, "", 0, "0") and botdb.get_category(int(cat_raw)):
            ex("UPDATE products SET category_id=? WHERE id=?", (int(cat_raw), pid))
    except Exception:
        pass

    # Optional initial stock codes
    codes = (d.get("stock_codes") or "").strip()
    added = 0
    if codes:
        added = botdb.restock_product(pid, codes)
    log_action("create_product", "#{} {} (+{} codes)".format(pid, name, added))

    # Auto-broadcast the new product to all bot users (same as TG admin).
    sent = failed = 0
    if int(bool(d.get("active", 1))):
        try:
            available = botdb.get_display_stock(pid)
            msg = _product_alert_message(name, price, added, available, "product")
            sent, failed = _broadcast_to_all(msg, pid=pid, button_text="\U0001f6d2 Tap to Buy Now")
            log_action("product_broadcast", "#{} sent={} failed={}".format(pid, sent, failed))
        except Exception:
            pass
    return jsonify({"ok": True, "id": pid, "added_codes": added,
                    "broadcast_sent": sent, "broadcast_failed": failed})


@app.route("/api/products/<int:pid>/update", methods=["POST"])
@login_required
def api_product_update(pid):
    d = request.get_json(force=True)
    fields, vals = [], []
    # Preserve premium emoji on rename: the web form can only submit plain
    # text, so without this every web edit silently wiped the <tg-emoji>
    # tag from the stored name (and ALL later broadcasts lost the premium
    # emoji, even ones sent from the Telegram admin).
    name_kept_premium = False
    if "name" in d and d["name"] is not None:
        try:
            new_name = str(d["name"]).strip()
            prev_row = q("SELECT name FROM products WHERE id=?", (pid,), one=True)
            old_name = str(prev_row["name"]) if prev_row else ""
            if "<tg-emoji" in old_name and "<tg-emoji" not in new_name:
                if strip_tg_emoji(old_name).strip() == new_name:
                    # Text unchanged -> keep the stored premium version.
                    d.pop("name")
                    name_kept_premium = True
                else:
                    # Text changed -> re-attach the premium tag to the same
                    # fallback emoji character if it is still in the name.
                    m = TG_EMOJI_RE.search(old_name)
                    if m and m.group(2) and m.group(2) in new_name:
                        d["name"] = new_name.replace(
                            m.group(2),
                            '<tg-emoji emoji-id="{}">{}</tg-emoji>'.format(m.group(1), m.group(2)),
                            1,
                        )
        except Exception:
            pass
    # Category assignment: null/0/"" clears it (product becomes Uncategorized).
    if "category_id" in d:
        cat_raw = d.get("category_id")
        if cat_raw in (None, "", 0, "0"):
            fields.append("category_id=?")
            vals.append(None)
        else:
            try:
                cat_id = int(cat_raw)
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "Invalid category"}), 400
            if not botdb.get_category(cat_id):
                return jsonify({"ok": False, "error": "Unknown category"}), 400
            fields.append("category_id=?")
            vals.append(cat_id)
    allowed = {"name": str, "price": float, "delivery": str, "active": int,
               "featured": int, "description": str, "validity": str,
               "warranty": str, "rules": str, "redeem_link": str,
               "max_per_order": int, "cost": float}
    for k, caster in allowed.items():
        if k in d and d[k] is not None:
            fields.append("{}=?".format(k))
            vals.append(caster(d[k]) if caster != str else str(d[k]))
    if not fields:
        if name_kept_premium:
            return jsonify({"ok": True, "unchanged": True})
        return jsonify({"ok": False, "error": "Nothing to update"}), 400

    old_price = None
    if "price" in d and d["price"] is not None:
        prev = q("SELECT price FROM products WHERE id=?", (pid,), one=True)
        if prev:
            old_price = float(prev["price"] or 0)

    vals.append(pid)
    ex("UPDATE products SET {} WHERE id=?".format(", ".join(fields)), tuple(vals))
    log_action("edit_product", "#{} -> {}".format(pid, list(d.keys())))

    # Auto-scale bulk (tier) prices by the same ratio as the base price.
    if old_price is not None:
        try:
            new_price = float(d["price"])
            if (
                old_price > 0
                and new_price > 0
                and abs(new_price - old_price) >= 0.005
                and not botdb.tiers_recently_edited(pid, 5)
            ):
                # Offers hand-typed in the last few minutes are never rescaled
                # - the admin's exact numbers always win over auto-scaling.
                ratio = new_price / old_price
                for _tid, tier_qty, tier_price in botdb.get_product_price_tiers(pid):
                    # qty-1 offers always follow the base price exactly.
                    scaled = new_price if int(tier_qty) == 1 else round(float(tier_price) * ratio, 2)
                    botdb.set_product_price_tier(pid, int(tier_qty), scaled, manual=False)
        except Exception:
            pass

    # Auto-broadcast price changes to all bot users instantly.
    if old_price is not None:
        try:
            new_price = float(d["price"])
            if abs(new_price - old_price) >= 0.005:
                prod = q("SELECT name FROM products WHERE id=?", (pid,), one=True)
                pname = prod["name"] if prod else "Product"
                available = botdb.get_display_stock(pid)
                msg = _price_change_message(pname, old_price, new_price, available)
                if available and int(available) > 0:
                    btn_text, btn_cb = "\U0001f6d2 Buy Now", None
                else:
                    btn_text, btn_cb = "\U0001f514 Notify Me When Back", "notify_stock_{}".format(pid)
                sent, failed = _broadcast_to_all(msg, pid=pid, button_text=btn_text, button_callback=btn_cb)
                if sent == 0 and failed > 0:
                    # Web admin couldn't reach Telegram - let the bot send it within ~1 min instead.
                    botdb.queue_price_change_broadcast(pid, old_price, new_price)
                log_action("price_broadcast", "#{} {} -> {} sent={} failed={}".format(pid, old_price, new_price, sent, failed))
        except Exception:
            pass

    return jsonify({"ok": True})


@app.route("/api/products/<int:pid>/delete", methods=["POST"])
@login_required
def api_product_delete(pid):
    ex("DELETE FROM products WHERE id=?", (pid,))
    ex("DELETE FROM product_stock WHERE product_id=?", (pid,))
    ex("DELETE FROM product_price_tiers WHERE product_id=?", (pid,))
    log_action("delete_product", "#{}".format(pid))
    return jsonify({"ok": True})


@app.route("/api/products/<int:pid>/toggle", methods=["POST"])
@login_required
def api_product_toggle(pid):
    field = (request.get_json(force=True).get("field") or "active")
    if field not in ("active", "featured"):
        return jsonify({"ok": False, "error": "bad field"}), 400
    row = q("SELECT {} v FROM products WHERE id=?".format(field), (pid,), one=True)
    if not row:
        return jsonify({"ok": False, "error": "not found"}), 404
    newv = 0 if row["v"] else 1
    ex("UPDATE products SET {}=? WHERE id=?".format(field), (newv, pid))
    log_action("toggle_" + field, "#{} -> {}".format(pid, newv))
    return jsonify({"ok": True, "value": newv})


@app.route("/api/products/<int:pid>/flash", methods=["POST"])
@login_required
def api_product_flash(pid):
    d = request.get_json(force=True)
    if d.get("clear"):
        ex("UPDATE products SET flash_price=NULL, flash_ends_at=NULL WHERE id=?", (pid,))
        log_action("flash_clear", "#{}".format(pid))
        return jsonify({"ok": True})
    prod = q("SELECT name, price FROM products WHERE id=?", (pid,), one=True)
    if not prod:
        return jsonify({"ok": False, "error": "Product not found"}), 404
    price = float(d.get("flash_price") or 0)
    if price <= 0:
        return jsonify({"ok": False, "error": "Flash price must be greater than 0"}), 400
    hours = float(d.get("hours") or 24)
    old_price = float(prod["price"] or 0)
    ends = (now() + dt.timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    ex("UPDATE products SET flash_price=?, flash_ends_at=? WHERE id=?", (price, ends, pid))
    discount = round((old_price - price) / old_price * 100) if old_price > 0 else 0
    log_action("flash_set", "#{} {} until {} (-{}%)".format(pid, price, ends, discount))

    result = {"ok": True, "ends_at": ends, "discount": discount,
              "flash_price": price, "broadcast": False, "sent": 0, "failed": 0}

    # Optionally announce the flash sale to every eligible user (same audience the bot uses)
    if d.get("broadcast"):
        ends_text = "ends in {}h".format(int(hours)) if hours >= 1 else "limited time"
        msg = botdb.render_message_template(
            "flash_sale_broadcast",
            "\U0001f525 <b>FLASH SALE!</b>\n\n\U0001f4e6 {product}\n\n\U0001f4b0 Was: {old_price}\n\u26a1 Now: {new_price}\n\U0001f381 Discount: {discount}\n\n\u23f3 {validity}\n\nBuy before it ends \U0001f447",
            product_name=prod["name"],
            old_price="${:.2f}".format(old_price),
            new_price="${:.2f}".format(price),
            discount_text=("{}% OFF".format(discount) if discount else "SALE"),
            discount_percent=("-{}".format(discount) if discount else "SALE"),
            validity=ends_text,
            stock=botdb.get_available_stock_count(pid),
        )
        btn_text, btn_icon = button_name_icon(prod["name"])
        flash_btn = {"text": "\U0001f6d2 Buy " + btn_text[:28],
                     "callback_data": "product_{}".format(pid),
                     "style": "primary"}
        if btn_icon:
            flash_btn["icon_custom_emoji_id"] = btn_icon
        kb = {"inline_keyboard": [[flash_btn]]}
        users = q("SELECT user_id FROM users WHERE COALESCE(blocked,0)=0 AND COALESCE(notifications,1)=1")
        sent = failed = 0
        for u in users:
            ok_send, _ = tg_send(u["user_id"], msg, reply_markup=kb)
            if ok_send:
                sent += 1
            else:
                failed += 1
        log_action("flash_broadcast", "#{} sent={} failed={}".format(pid, sent, failed))
        result.update({"broadcast": True, "sent": sent, "failed": failed})

    return jsonify(result)


# ---- stock / codes
@app.route("/api/products/<int:pid>/stock/add", methods=["POST"])
@login_required
def api_stock_add(pid):
    body = request.get_json(force=True)
    codes = (body.get("codes") or "").strip()
    op_id = (body.get("op_id") or "").strip()

    if not codes:
        return jsonify({"ok": False, "error": "No codes provided"}), 400

    # ── BACKEND IDEMPOTENCY ──────────────────────────────────────────────────
    # If the frontend sent an op_id and we already recorded a completed result
    # for it, return the original result immediately — no second insertion.
    if op_id:
        existing = q(
            "SELECT added, available FROM stock_op_log WHERE op_id=? AND product_id=?",
            (op_id, pid), one=True
        )
        if existing:
            return jsonify({
                "ok": True,
                "added": existing["added"],
                "available": existing["available"],
                "broadcast_sent": 0,
                "broadcast_failed": 0,
                "replayed": True,
            })
    # ── END IDEMPOTENCY CHECK ────────────────────────────────────────────────

    added = botdb.restock_product(pid, codes)
    log_action("add_stock", "#{} +{} op={}".format(pid, added, op_id or "none"))
    available = botdb.get_available_stock_count(pid)

    # Record completed operation for idempotency (only when op_id supplied).
    if op_id and added > 0:
        try:
            admin_id = session.get("admin_id", 0)
            ex(
                "INSERT OR IGNORE INTO stock_op_log"
                "(op_id, product_id, added, available, admin_id) VALUES(?,?,?,?,?)",
                (op_id, pid, added, available, admin_id)
            )
        except Exception:
            pass

    # Auto-broadcast new stock to all bot users (same as TG admin).
    sent = failed = 0
    if added > 0:
        try:
            prod = q("SELECT name, price, active FROM products WHERE id=?", (pid,), one=True)
            if prod and int(prod["active"] or 0) == 1:
                msg = _product_alert_message(prod["name"], prod["price"], added, available, "stock")
                sent, failed = _broadcast_to_all(msg, pid=pid, button_text="\U0001f6d2 Tap to Buy Now")
                log_action("stock_broadcast", "#{} sent={} failed={}".format(pid, sent, failed))
        except Exception:
            pass
    return jsonify({"ok": True, "added": added, "available": available,
                    "broadcast_sent": sent, "broadcast_failed": failed})


@app.route("/api/products/<int:pid>/stock/add_accounts", methods=["POST"])
@login_required
def api_stock_add_accounts(pid):
    d = request.get_json(force=True)
    accounts = (d.get("accounts") or "").strip()
    delimiter = (d.get("delimiter") or ":").strip() or ":"
    op_id = (d.get("op_id") or "").strip()

    if not accounts:
        return jsonify({"ok": False, "error": "No accounts provided"}), 400

    # ── BACKEND IDEMPOTENCY ──────────────────────────────────────────────────
    if op_id:
        existing = q(
            "SELECT added, available FROM stock_op_log WHERE op_id=? AND product_id=?",
            (op_id, pid), one=True
        )
        if existing:
            return jsonify({
                "ok": True,
                "added": existing["added"],
                "available": existing["available"],
                "broadcast_sent": 0,
                "broadcast_failed": 0,
                "replayed": True,
            })
    # ── END IDEMPOTENCY CHECK ────────────────────────────────────────────────

    added = botdb.add_account_stock_items(pid, accounts, delimiter)
    if added == 0:
        return jsonify({"ok": False,
                        "error": "No valid accounts found - check the format / separator"}), 400
    log_action("add_account_stock", "#{} +{} op={}".format(pid, added, op_id or "none"))
    available = botdb.get_available_stock_count(pid)

    # Record completed operation for idempotency.
    if op_id:
        try:
            admin_id = session.get("admin_id", 0)
            ex(
                "INSERT OR IGNORE INTO stock_op_log"
                "(op_id, product_id, added, available, admin_id) VALUES(?,?,?,?,?)",
                (op_id, pid, added, available, admin_id)
            )
        except Exception:
            pass

    # Auto-broadcast new stock to all bot users (same as plain code stock).
    sent = failed = 0
    try:
        prod = q("SELECT name, price, active FROM products WHERE id=?", (pid,), one=True)
        if prod and int(prod["active"] or 0) == 1:
            msg = _product_alert_message(prod["name"], prod["price"], added, available, "stock")
            sent, failed = _broadcast_to_all(msg, pid=pid, button_text="\U0001f6d2 Tap to Buy Now")
            log_action("stock_broadcast", "#{} sent={} failed={}".format(pid, sent, failed))
    except Exception:
        pass
    return jsonify({"ok": True, "added": added, "available": available,
                    "broadcast_sent": sent, "broadcast_failed": failed})


@app.route("/api/products/<int:pid>/stock")
@login_required
def api_stock_list(pid):
    rows = q("SELECT id, delivery, sold, sold_to, sold_at, created "
             "FROM product_stock WHERE product_id=? ORDER BY id DESC", (pid,))
    for r in rows:
        r["code"] = decrypt_text(r["delivery"])
        r.pop("delivery", None)
    return jsonify({"ok": True, "items": rows})


@app.route("/api/stock/<int:stock_id>/delete", methods=["POST"])
@login_required
def api_stock_delete(stock_id):
    row = q("SELECT product_id, sold FROM product_stock WHERE id=?", (stock_id,), one=True)
    if not row:
        return jsonify({"ok": False, "error": "not found"}), 404
    ex("DELETE FROM product_stock WHERE id=?", (stock_id,))
    pid = row["product_id"]
    avail = botdb.get_available_stock_count(pid)
    ex("UPDATE products SET stock=? WHERE id=?", (avail, pid))
    log_action("delete_code", "stock#{} product#{}".format(stock_id, pid))
    return jsonify({"ok": True, "available": avail})


@app.route("/api/products/<int:pid>/stock/delete_all", methods=["POST"])
@login_required
def api_stock_delete_all(pid):
    """Delete every UNSOLD code for a product in one click.

    Sold codes are deliberately kept: they are the delivery history behind
    existing orders and are needed for warranty claims, resends and
    replacements. Codes currently mid-purchase are already flipped to
    sold=1 by the atomic claim, so they cannot be removed here.
    """
    prod = q("SELECT id FROM products WHERE id=?", (pid,), one=True)
    if not prod:
        return jsonify({"ok": False, "error": "product not found"}), 404
    _lastrow, removed = ex(
        "DELETE FROM product_stock WHERE product_id=? AND COALESCE(sold,0)=0",
        (pid,))
    try:
        removed = int(removed or 0)
    except (TypeError, ValueError):
        removed = 0
    avail = botdb.get_available_stock_count(pid)
    ex("UPDATE products SET stock=? WHERE id=?", (avail, pid))
    log_action("delete_all_stock",
               "product#{} removed={} left={}".format(pid, removed, avail))
    return jsonify({"ok": True, "deleted": removed, "available": avail})


# ---- price tiers (Price Offers)
@app.route("/api/products/<int:pid>/tiers/set", methods=["POST"])
@login_required
def api_tier_set(pid):
    d = request.get_json(force=True)
    qty = int(d["quantity"])
    price = float(d["price"])
    existing = {int(t[1]): float(t[2]) for t in botdb.get_product_price_tiers(pid)}
    unchanged = qty in existing and abs(existing[qty] - price) < 0.005
    botdb.set_product_price_tier(pid, qty, price)
    log_action("set_tier", "#{} q{} ${}".format(pid, qty, price))
    sent = failed = 0
    # Only broadcast when something actually changed - prevents re-sending
    # stale offer alerts when a form re-submits old values.
    if d.get("notify", True) and not unchanged:
        sent, failed = _publish_offer_alert(pid)
    return jsonify({"ok": True, "sent": sent, "failed": failed})


@app.route("/api/products/<int:pid>/tiers/bulk_set", methods=["POST"])
@login_required
def api_tier_bulk_set(pid):
    """Save multiple price offers at once, then publish a single broadcast."""
    d = request.get_json(force=True)
    items = d.get("tiers") or []
    existing = {int(t[1]): float(t[2]) for t in botdb.get_product_price_tiers(pid)}
    saved = 0
    changed = 0
    for it in items:
        try:
            qty = int(it["quantity"])
            price = float(it["price"])
            if qty <= 0 or price <= 0:
                continue
            if qty not in existing or abs(existing[qty] - price) >= 0.005:
                changed += 1
            botdb.set_product_price_tier(pid, qty, price)
            saved += 1
        except Exception:
            continue
    if not saved:
        return jsonify({"ok": False, "error": "No valid offers"}), 400
    log_action("set_tiers_bulk", "#{} saved={} changed={}".format(pid, saved, changed))
    sent = failed = 0
    # Only broadcast when something actually changed - prevents re-sending
    # stale offer alerts when a form re-submits old values.
    if d.get("notify", True) and changed:
        sent, failed = _publish_offer_alert(pid)
    return jsonify({"ok": True, "saved": saved, "sent": sent, "failed": failed})


@app.route("/api/products/<int:pid>/tiers/delete", methods=["POST"])
@login_required
def api_tier_delete(pid):
    qty = int(request.get_json(force=True)["quantity"])
    botdb.delete_product_price_tier(pid, qty)
    log_action("del_tier", "#{} q{}".format(pid, qty))
    return jsonify({"ok": True})


# ================================================================= API: ORDERS
_ORDER_METHOD_LABELS = {"bep20": "USDT BEP20", "trc20": "USDT TRC20", "binance": "Binance Pay", "wallet": "Wallet"}


def _order_method_label(value, product_name=None):
    """Payment method label for the orders API. New orders store the label in
    orders.payment_method; legacy rows fall back to the funding checkout
    deposit (resolved in SQL), then to Wallet."""
    v = str(value or "").strip()
    label = _ORDER_METHOD_LABELS.get(v.lower(), v) if v else "Wallet"
    if label == "Wallet" and str(product_name or "").endswith("(bundle)"):
        return "Wallet (Bundle)"
    return label


@app.route("/api/orders")
@login_required
def api_orders():
    args = request.args
    order_id = (args.get("order_id") or "").strip()
    user_id = (args.get("user_id") or "").strip()
    username = (args.get("username") or "").strip().lstrip("@")
    date_from = (args.get("from") or "").strip()
    date_to = (args.get("to") or "").strip()
    search = (args.get("search") or "").strip()  # legacy free-text box keeps working
    try:
        page = max(1, int(args.get("page", 1) or 1))
        per_page = min(100, max(5, int(args.get("per_page", 25) or 25)))
    except (TypeError, ValueError):
        page, per_page = 1, 25

    # All filters are parameterized; only the requested page is ever read.
    where = []
    params = []
    if order_id.isdigit():
        where.append("o.id=?")
        params.append(int(order_id))
    if user_id.isdigit():
        where.append("o.user_id=?")
        params.append(int(user_id))
    if username:
        where.append("LOWER(u.username)=LOWER(?)")
        params.append(username)
    if search:
        where.append("(o.product_name LIKE ? OR CAST(o.user_id AS TEXT) LIKE ? OR CAST(o.id AS TEXT)=?)")
        params.extend(["%" + search + "%", "%" + search + "%", search])
    for value, op in ((date_from, ">="), (date_to, "<=")):
        if value:
            if not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                return jsonify({"ok": False, "error": "dates must be YYYY-MM-DD"}), 400
            where.append("o.created {} ?".format(op))
            params.append(value + (" 00:00:00" if op == ">=" else " 23:59:59"))
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    total_row = q("SELECT COUNT(*) AS c FROM orders o LEFT JOIN users u ON o.user_id=u.user_id" + where_sql,
                  params, one=True)
    total = int(total_row["c"] if total_row else 0)

    rows = q(
        "SELECT o.*, u.first_name, u.username, "
        "COALESCE(o.payment_method, (SELECT d2.method FROM deposits d2 "
        "WHERE d2.user_id=o.user_id AND d2.checkout_product_id=o.product_id AND d2.status='paid' "
        "ORDER BY d2.id DESC LIMIT 1), 'wallet') AS method "
        "FROM orders o LEFT JOIN users u ON o.user_id=u.user_id" + where_sql +
        " ORDER BY o.id DESC LIMIT ? OFFSET ?",
        (*params, per_page, (page - 1) * per_page))

    # Warranty status for just this page (one grouped query -- no N+1).
    status_map = {}
    ids = [int(r["id"]) for r in rows]
    if ids:
        ph = ",".join("?" for _ in ids)
        for srow in q("SELECT order_id, status FROM replacements WHERE order_id IN ({}) ORDER BY id DESC".format(ph), ids):
            status_map.setdefault(int(srow["order_id"]), srow["status"])

    for r in rows:
        if r.get("delivery") is not None:
            r["delivery"] = decrypt_text(r["delivery"])
        m = _re.search(r"\s+x(\d+)$", str(r.get("product_name") or ""))
        r["quantity"] = int(m.group(1)) if m else 1
        r["method_label"] = _order_method_label(r.pop("method", None), r.get("product_name"))
        st = status_map.get(int(r["id"]))
        r["status_label"] = ("\U0001f6e1 Replaced" if st in ("approved", "auto")
                             else "\U0001f6e1 Warranty review" if st == "pending"
                             else "\u2705 Completed")
    pages = max(1, (total + per_page - 1) // per_page)
    return jsonify({"ok": True, "orders": rows, "total": total, "page": page, "pages": pages, "per_page": per_page})


@app.route("/api/orders/<int:oid>/refund", methods=["POST"])
@login_required
def api_order_refund(oid):
    row = q("SELECT * FROM orders WHERE id=?", (oid,), one=True)
    if not row:
        return jsonify({"ok": False, "error": "not found"}), 404
    botdb.add_balance(row["user_id"], float(row["price"] or 0))
    # Claw back any referral commission paid on this order, otherwise a
    # buy -> refund loop mints free referral earnings.
    try:
        _ref = q("SELECT referrer_id FROM referrals WHERE referred_id=?",
                 (row["user_id"],), one=True)
        if _ref:
            _pct = float(botdb.get_setting("referral_percent", "5") or 5)
            _earn = round(float(row["price"] or 0) * _pct / 100, 6)
            if _earn > 0:
                ex("UPDATE referrals SET earned=MAX(0, earned-?) WHERE referred_id=?",
                   (_earn, row["user_id"]))
                ex("UPDATE users SET referral_earnings=MAX(0, COALESCE(referral_earnings,0)-?), "
                   "referral_pending=MAX(0, COALESCE(referral_pending,0)-?) WHERE user_id=?",
                   (_earn, _earn, _ref["referrer_id"]))
    except Exception:
        pass
    ex("DELETE FROM orders WHERE id=?", (oid,))
    log_action("refund_order", "#{} ${} -> user {}".format(oid, row["price"], row["user_id"]))
    notify = request.get_json(silent=True) or {}
    if notify.get("notify"):
        tg_send(row["user_id"],
                "\u267b\ufe0f <b>Refund processed</b>\nOrder #{} (${}) was refunded to your wallet."
                .format(oid, row["price"]))
    return jsonify({"ok": True})


@app.route("/api/orders/<int:oid>/resend", methods=["POST"])
@login_required
def api_order_resend(oid):
    row = q("SELECT * FROM orders WHERE id=?", (oid,), one=True)
    if not row:
        return jsonify({"ok": False, "error": "not found"}), 404
    ok, msg = tg_send(row["user_id"],
                      "\U0001f4e6 <b>Order #{} resend</b>\n<b>{}</b>\n\n\U0001f511 <code>{}</code>"
                      .format(oid, row["product_name"], decrypt_text(row.get("delivery")) or "(no delivery stored)"))
    log_action("resend_order", "#{} -> {} ({})".format(oid, row["user_id"], msg))
    return jsonify({"ok": ok, "detail": msg})


# ================================================================== API: USERS
@app.route("/api/users")
@login_required
def api_users():
    search = (request.args.get("search") or "").strip()
    page = max(1, int(request.args.get("page", 1) or 1))
    # `limit` kept as a legacy alias for per_page.
    per_page = min(200, max(10, int(request.args.get("per_page", request.args.get("limit", 50)) or 50)))
    where = ""
    params = []
    if search:
        where = "WHERE CAST(user_id AS TEXT) LIKE ? OR first_name LIKE ? OR username LIKE ?"
        params = ["%" + search + "%"] * 3
    total = int((q("SELECT COUNT(*) AS c FROM users " + where, tuple(params), one=True) or {}).get("c", 0))
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages)
    rows = q("SELECT * FROM users " + where + " ORDER BY joined DESC LIMIT ? OFFSET ?",
             tuple(params) + (per_page, (page - 1) * per_page))
    return jsonify({"ok": True, "users": rows, "total": total, "page": page,
                    "pages": pages, "per_page": per_page})


@app.route("/api/users/<int:uid>")
@login_required
def api_user_detail(uid):
    user = q("SELECT * FROM users WHERE user_id=?", (uid,), one=True)
    orders = q("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 50", (uid,))
    deposits = q("SELECT * FROM deposits WHERE user_id=? ORDER BY id DESC LIMIT 50", (uid,))
    stats = None
    if user:
        o = q("SELECT COUNT(*) AS c, COALESCE(SUM(price),0) AS s FROM orders WHERE user_id=?", (uid,), one=True) or {}
        d = q("SELECT COUNT(*) AS c, COALESCE(SUM(amount),0) AS s FROM deposits WHERE user_id=? AND status='paid'", (uid,), one=True) or {}
        stats = {
            "total_orders": int(o.get("c") or 0),
            "total_spent": float(o.get("s") or 0),
            "deposits_count": int(d.get("c") or 0),
            "total_deposited": float(d.get("s") or 0),
        }
    return jsonify({"ok": True, "user": user, "orders": orders, "deposits": deposits, "stats": stats})


@app.route("/api/users/<int:uid>/balance", methods=["POST"])
@login_required
def api_user_balance(uid):
    d = request.get_json(force=True)
    amount = float(d.get("amount") or 0)
    botdb.add_balance(uid, amount)  # negative to remove
    log_action("adjust_balance", "user {} {:+.2f}".format(uid, amount))
    if d.get("notify") and amount:
        verb = "added to" if amount > 0 else "removed from"
        tg_send(uid, "\U0001f4b0 <b>${:.2f}</b> was {} your wallet by admin."
                .format(abs(amount), verb))
    bal = botdb.get_balance(uid)
    return jsonify({"ok": True, "balance": bal})


@app.route("/api/users/<int:uid>/block", methods=["POST"])
@login_required
def api_user_block(uid):
    block = bool(request.get_json(force=True).get("block"))
    if block:
        botdb.block_user(uid)
    else:
        botdb.unblock_user(uid)
    log_action("block_user" if block else "unblock_user", str(uid))
    return jsonify({"ok": True, "blocked": 1 if block else 0})


# ================================================================ API: COUPONS
@app.route("/api/coupons")
@login_required
def api_coupons():
    rows = q("SELECT * FROM coupons ORDER BY created DESC")
    return jsonify({"ok": True, "coupons": rows})


@app.route("/api/coupons/create", methods=["POST"])
@login_required
def api_coupon_create():
    d = request.get_json(force=True)
    code = (d.get("code") or "").upper().strip()
    if not code:
        return jsonify({"ok": False, "error": "Code required"}), 400
    dtype = d.get("discount_type", "percent")
    value = float(d.get("value") or 0)
    max_uses = int(d.get("max_uses") or 0)
    expires_at = d.get("expires_at") or None
    reseller = 1 if d.get("reseller") else 0
    ex("INSERT OR REPLACE INTO coupons(code, discount_type, value, active, max_uses, "
       "used_count, expires_at, reseller) VALUES(?,?,?,?,?,COALESCE((SELECT used_count FROM "
       "coupons WHERE code=?),0),?,?)",
       (code, dtype, value, 1, max_uses, code, expires_at, reseller))
    log_action("create_coupon", code)

    # Reseller coupons are private: never broadcast to users.
    if reseller:
        log_action("create_reseller_coupon", code)
        return jsonify({"ok": True, "reseller": True, "broadcast_sent": 0, "broadcast_failed": 0})

    # Auto-broadcast the new coupon to all bot users (same as TG admin).
    sent = failed = 0
    try:
        if dtype == "percent":
            discount_text = "{:g}% OFF".format(value)
        else:
            discount_text = "${:.2f} OFF".format(value)

        validity_text = "Limited time"
        try:
            end_dt = dt.datetime.strptime(str(expires_at), "%Y-%m-%d %H:%M:%S")
            remaining = end_dt - dt.datetime.strptime(now().strftime("%Y-%m-%d %H:%M:%S"), "%Y-%m-%d %H:%M:%S")
            hours = max(1, int(remaining.total_seconds() // 3600))
            validity_text = "Valid for {}h".format(hours)
        except Exception:
            if expires_at:
                validity_text = str(expires_at)

        uses_text = "Unlimited uses" if not max_uses else "Only {} uses".format(max_uses)

        msg = botdb.render_message_template(
            "coupon_broadcast",
            "\U0001f39f <b>NEW COUPON AVAILABLE!</b>\n\n\U0001f49d <b>Discount:</b> <code>{discount}</code>\n\U0001f3ab <b>Coupon Code:</b> <code>{code}</code>\n\u23f3 <b>Validity:</b> <code>{validity}</code>\n\U0001f525 <b>Limit:</b> <code>{limit}</code>\n\nApply this coupon before checkout and save on your next purchase \U0001f447",
            discount=discount_text,
            code=code,
            validity=validity_text,
            limit=uses_text,
        )

        kb = {"inline_keyboard": [
            [{"text": "\U0001f6cd Shop Now", "callback_data": "shop", "style": "primary"}],
            [{"text": "\U0001f39f Apply Coupon", "callback_data": "apply_coupon", "style": "success"}]
        ]}

        users = q("SELECT user_id FROM users WHERE COALESCE(blocked,0)=0 AND COALESCE(notifications,1)=1")
        for u in users:
            ok_send, _info = tg_send(u["user_id"], msg, reply_markup=kb)
            if ok_send:
                sent += 1
            else:
                failed += 1
        log_action("coupon_broadcast", "{} sent={} failed={}".format(code, sent, failed))
    except Exception:
        pass

    return jsonify({"ok": True, "broadcast_sent": sent, "broadcast_failed": failed})


@app.route("/api/coupons/<code>/toggle", methods=["POST"])
@login_required
def api_coupon_toggle(code):
    row = q("SELECT active FROM coupons WHERE code=?", (code,), one=True)
    if not row:
        return jsonify({"ok": False, "error": "not found"}), 404
    newv = 0 if row["active"] else 1
    ex("UPDATE coupons SET active=? WHERE code=?", (newv, code))
    log_action("toggle_coupon", "{} -> {}".format(code, newv))
    return jsonify({"ok": True, "active": newv})


@app.route("/api/coupons/<code>/delete", methods=["POST"])
@login_required
def api_coupon_delete(code):
    ex("DELETE FROM coupons WHERE code=?", (code,))
    log_action("delete_coupon", code)
    return jsonify({"ok": True})


# ============================================================= API: BROADCASTS
@app.route("/api/broadcasts")
@login_required
def api_broadcasts():
    rows = botdb.get_scheduled_broadcasts(30)
    out = [{"id": r[0], "message": r[1], "run_at": r[2], "status": r[3],
            "sent": r[4], "failed": r[5]} for r in rows]
    return jsonify({"ok": True, "broadcasts": out,
                    "bot_online": bool(TELEGRAM_API),
                    "last_error": botdb.get_setting("broadcast_last_error", "")})


def _start_broadcast_worker(bid, message):
    """Deliver a broadcast to every eligible user in a background thread, so the
    web request returns instantly and delivery does NOT depend on the bot
    process or its job queue (the previous cause of broadcasts stuck on
    "pending")."""
    def _worker():
        last_err = ""
        try:
            ex("UPDATE scheduled_broadcasts SET status='sending' WHERE id=?", (bid,))
            reachable, why = _tg_reachable()
            if not reachable:
                # Systemic problem (no token, or this server cannot reach
                # Telegram). Revert to pending so it can be retried after the
                # network/proxy is fixed, and report exactly why.
                ex("UPDATE scheduled_broadcasts SET status='pending' WHERE id=?", (bid,))
                try:
                    botdb.set_setting("broadcast_last_error", why)
                    log_action("broadcast_error", "#{} not sent - {}".format(bid, why))
                except Exception:
                    pass
                return
            users = q("SELECT user_id FROM users WHERE COALESCE(blocked,0)=0 AND COALESCE(notifications,1)=1")
            sent = failed = removed = 0
            # Per-recipient errors that mean the user simply cannot be reached
            # (never started the bot, blocked it, or deleted their account).
            UNREACHABLE = ("chat not found", "bot was blocked", "user is deactivated",
                           "peer_id_invalid", "user not found", "chat_id is empty",
                           "bot can't initiate", "forbidden")
            for u in users:
                ok_send, info = tg_send(u["user_id"], message)
                if ok_send:
                    sent += 1
                    continue
                failed += 1
                low = (info or "").lower()
                if any(k in low for k in UNREACHABLE):
                    # Opt them out of future broadcasts (this is NOT an admin
                    # ban) so they stop counting as failures every time.
                    removed += 1
                    try:
                        ex("UPDATE users SET notifications=0 WHERE user_id=?", (u["user_id"],))
                    except Exception:
                        pass
                else:
                    # A real/unexpected error (network, rate limit, etc.) -- worth showing.
                    last_err = str(info)[:300]
            ex("UPDATE scheduled_broadcasts SET status='sent', sent=?, failed=? WHERE id=?", (sent, failed, bid))
            try:
                # Only raise the red banner for genuine errors, not normal
                # dead-recipient attrition.
                botdb.set_setting("broadcast_last_error", last_err)
                note = "#{}: {} sent, {} failed".format(bid, sent, failed)
                if removed:
                    note += ", {} unreachable opted out".format(removed)
                if last_err:
                    note += " - " + last_err
                log_action("broadcast", note)
            except Exception:
                pass
        except Exception as e:
            try:
                ex("UPDATE scheduled_broadcasts SET status='pending' WHERE id=?", (bid,))
                botdb.set_setting("broadcast_last_error", str(e)[:300])
            except Exception:
                pass
    try:
        threading.Thread(target=_worker, daemon=True).start()
    except Exception:
        pass


@app.route("/api/broadcasts/create", methods=["POST"])
@login_required
def api_broadcast_create():
    d = request.get_json(force=True)
    message = (d.get("message") or "").strip()
    if not message:
        return jsonify({"ok": False, "error": "Message required"}), 400
    run_at = (d.get("run_at") or "").strip() or now_text()
    bid, _rc = ex("INSERT INTO scheduled_broadcasts(message, run_at, status, sent, failed) "
                  "VALUES(?,?, 'pending', 0, 0)", (message, run_at))
    # Send now if the time is now/past (does not need the bot running); otherwise
    # leave it pending for the bot's scheduler to deliver at the scheduled time.
    if run_at <= now_text():
        _start_broadcast_worker(bid, message)
        log_action("broadcast", "#{} sending now".format(bid))
        return jsonify({"ok": True, "id": bid, "run_at": run_at, "status": "sending"})
    log_action("broadcast", "#{} scheduled {}".format(bid, run_at))
    return jsonify({"ok": True, "id": bid, "run_at": run_at, "status": "scheduled"})


@app.route("/api/broadcasts/<int:bid>/send", methods=["POST"])
@login_required
def api_broadcast_send_now(bid):
    row = q("SELECT id, message, status FROM scheduled_broadcasts WHERE id=?", (bid,), one=True)
    if not row:
        return jsonify({"ok": False, "error": "Broadcast not found"}), 404
    if row["status"] == "sending":
        return jsonify({"ok": False, "error": "This broadcast is already sending"}), 400
    _start_broadcast_worker(bid, row["message"])
    log_action("broadcast", "#{} send now".format(bid))
    return jsonify({"ok": True, "status": "sending"})


# ======================================================= API: MESSAGE TEMPLATES
@app.route("/api/templates")
@login_required
def api_templates():
    rows = botdb.list_message_templates()
    out = []
    for key, title, updated in rows:
        out.append({"key": key, "title": title, "updated": updated,
                    "body": botdb.get_message_template(key, "")})
    return jsonify({"ok": True, "templates": out})


@app.route("/api/templates/<key>/update", methods=["POST"])
@login_required
def api_template_update(key):
    body = request.get_json(force=True).get("body", "")
    botdb.set_message_template(key, body)
    log_action("edit_template", key)
    return jsonify({"ok": True})


# =============================================================== API: TICKETS
@app.route("/api/tickets")
@login_required
def api_tickets():
    status = (request.args.get("status") or "all").lower()
    search = (request.args.get("search") or "").strip().lower()
    page = max(1, int(request.args.get("page") or 1))
    per_page = max(10, min(int(request.args.get("per_page") or 30), 50))
    # Summaries only (never message histories), so a bounded 500-ticket window
    # keeps total/page counts accurate without loading every message.
    raw = botdb.get_ticket_conversations(status, 500)
    rows = []
    for r in raw:
        item = dict(zip(("id","user_id","first_name","username","subject","status","created","updated",
                         "last_message","last_sender","last_message_at","message_count","unread_count"), r))
        haystack = " ".join(str(item.get(k) or "") for k in
                            ("id","user_id","first_name","username","subject","last_message")).lower()
        if not search or search in haystack:
            rows.append(item)
    total = len(rows)
    start = (page - 1) * per_page
    return jsonify({"ok": True, "tickets": rows[start:start + per_page], "page": page,
                    "pages": max(1, (total + per_page - 1) // per_page), "total": total})


@app.route("/api/tickets/<int:tid>")
@login_required
def api_ticket_conversation(tid):
    ticket = botdb.get_ticket(tid)
    if not ticket:
        return jsonify({"ok": False, "error": "Ticket not found"}), 404
    before = request.args.get("before_id")
    messages, has_more = botdb.get_ticket_messages(tid, request.args.get("limit", 100), before)
    botdb.mark_ticket_read(tid)
    keys = ("id","ticket_id","sender_type","sender_id","message","created","read_by_admin")
    return jsonify({"ok": True, "ticket": dict(zip(
        ("id","user_id","first_name","username","subject","message","status","admin_reply","created","updated"), ticket)),
        "messages": [dict(zip(keys, m)) for m in messages], "has_more": has_more})


@app.route("/api/tickets/<int:tid>/reply", methods=["POST"])
@login_required
def api_ticket_reply(tid):
    reply = (request.get_json(force=True).get("reply") or "").strip()
    if not reply:
        return jsonify({"ok": False, "error": "Reply required"}), 400
    t = botdb.get_ticket(tid)
    if not t:
        return jsonify({"ok": False, "error": "Ticket not found"}), 404
    if t[6] == "closed":
        return jsonify({"ok": False, "error": "Closed tickets cannot receive replies"}), 409
    botdb.reply_ticket(tid, reply)
    # Customer action buttons directly under the admin reply message (the same
    # support_reply_/support_close_ callbacks the bot's own support flow uses).
    kb = {"inline_keyboard": [
        [{"text": "\U0001f4ac Reply Ticket", "callback_data": "support_reply_{}".format(tid)}],
        [{"text": "✅ Close Ticket", "callback_data": "support_close_{}".format(tid)}],
    ]}
    tg_send(t[1], "\U0001f3ab <b>Support reply (Ticket #{})</b>\n\n{}".format(
        tid, html.escape(reply, quote=False)), reply_markup=kb)
    log_action("reply_ticket", "#{}".format(tid))
    messages, _ = botdb.get_ticket_messages(tid, 1)
    keys = ("id","ticket_id","sender_type","sender_id","message","created","read_by_admin")
    return jsonify({"ok": True, "message": dict(zip(keys, messages[-1])) if messages else None})


@app.route("/api/tickets/<int:tid>/close", methods=["POST"])
@login_required
def api_ticket_close(tid):
    t = botdb.get_ticket(tid)
    if not t:
        return jsonify({"ok": False, "error": "Ticket not found"}), 404
    changed = botdb.close_ticket(tid)
    if changed:
        tg_send(t[1], botdb.SUPPORT_RESOLUTION_MESSAGE)
    log_action("close_ticket", "#{}".format(tid))
    return jsonify({"ok": True, "closed": True, "resolution_sent": bool(changed),
                    "resolution_message": botdb.SUPPORT_RESOLUTION_MESSAGE if changed else None})


# ========================================================== API: WITHDRAWALS
@app.route("/api/withdrawals")
@login_required
def api_withdrawals():
    rows = q("SELECT w.*, u.first_name, u.username FROM withdrawal_requests w "
             "LEFT JOIN users u ON w.user_id=u.user_id ORDER BY w.id DESC LIMIT 100")
    for r in rows:
        if r.get("wallet") is not None:
            r["wallet"] = decrypt_text(r["wallet"])
    return jsonify({"ok": True, "withdrawals": rows})


@app.route("/api/withdrawals/<int:wid>/action", methods=["POST"])
@login_required
def api_withdrawal_action(wid):
    d = request.get_json(force=True)
    status = d.get("status")  # approved / rejected
    note = d.get("note", "")
    if status not in ("approved", "rejected"):
        return jsonify({"ok": False, "error": "bad status"}), 400
    res = botdb.update_withdrawal_status(wid, status, note)
    if res:
        uid, amount, old = res
        tg_send(uid, "\U0001f4b8 <b>Withdrawal {}</b>\nAmount: ${}\n{}".format(
            status, amount, note or ""))
    log_action("withdrawal_" + status, "#{}".format(wid))
    return jsonify({"ok": True})


# =============================================================== API: ADMINS
@app.route("/api/admins")
@login_required
@owner_required
def api_admins():
    rows = botdb.get_admins()
    out = [{"user_id": r[0], "first_name": r[1], "username": r[2],
            "added_by": r[3], "added_at": r[4]} for r in rows]
    owner = int(os.getenv("ADMIN_ID", "0") or 0)
    return jsonify({"ok": True, "admins": out, "owner_id": owner})


@app.route("/api/admins/add", methods=["POST"])
@login_required
@owner_required
def api_admin_add():
    uid = int(request.get_json(force=True).get("user_id"))
    botdb.add_admin(uid, session.get("admin_id", 0))
    log_action("add_admin", str(uid))
    return jsonify({"ok": True})


@app.route("/api/admins/<int:uid>/remove", methods=["POST"])
@login_required
@owner_required
def api_admin_remove(uid):
    ok = botdb.remove_admin(uid)
    log_action("remove_admin", str(uid))
    return jsonify({"ok": ok})


# ================================================================ API: SETTINGS
SETTING_KEYS = [
    "referral_percent", "low_stock_alert_threshold", "low_stock_alert_levels",
    "min_withdrawal", "loyalty_percent", "first_purchase_discount_percent",
    "buy_x_qty", "buy_x_free", "auto_welcome_coupon", "inactive_days",
    "inactive_coupon",
]


@app.route("/api/settings")
@login_required
def api_settings():
    out = {k: botdb.get_setting(k, "") for k in SETTING_KEYS}
    return jsonify({"ok": True, "settings": out, "logo": botdb.get_setting("web_logo", "")})


@app.route("/api/settings/update", methods=["POST"])
@login_required
def api_settings_update():
    d = request.get_json(force=True)
    changed = []
    for k, v in d.items():
        if k in SETTING_KEYS:
            botdb.set_setting(k, v)
            changed.append(k)
    log_action("update_settings", ",".join(changed))
    return jsonify({"ok": True, "changed": changed})


# ================================================================== API: LOGS
@app.route("/api/logs")
@login_required
@owner_required
def api_logs():
    rows = q("SELECT * FROM admin_logs ORDER BY id DESC LIMIT 200")
    return jsonify({"ok": True, "logs": rows})


@app.route("/api/logs/clear", methods=["POST"])
@login_required
@owner_required
def api_logs_clear():
    botdb.clear_admin_logs()
    log_action("clear_logs", "")
    return jsonify({"ok": True})


# ================================================================ API: BACKUPS
@app.route("/api/backup/create", methods=["POST"])
@login_required
@owner_required
def api_backup_create():
    cwd = os.getcwd()
    encrypted = False
    try:
        os.chdir(PROJECT_ROOT)
        path = botdb.create_backup_zip()
        key = os.getenv("BACKUP_ENCRYPTION_KEY") or os.getenv("DATA_ENCRYPTION_KEY")
        if key:
            try:
                from cryptography.fernet import Fernet
                raw = open(path, "rb").read()
                enc = Fernet(key.encode() if isinstance(key, str) else key).encrypt(raw)
                enc_path = path + ".enc"
                open(enc_path, "wb").write(enc)
                os.remove(path)
                path = enc_path
                encrypted = True
            except Exception as e:
                log_action("backup_encrypt_failed", str(e))
    finally:
        os.chdir(cwd)
    log_action("create_backup", os.path.basename(path))
    return jsonify({"ok": True, "path": os.path.basename(path), "encrypted": encrypted})


@app.route("/api/backup/download/<name>")
@login_required
@owner_required
def api_backup_download(name):
    name = os.path.basename(name)
    path = os.path.join(PROJECT_ROOT, getattr(config, "BACKUP_DIR", "backups"), name)
    if not os.path.exists(path):
        abort(404)
    if request.args.get("decrypt") == "1" and name.endswith(".enc"):
        key = os.getenv("BACKUP_ENCRYPTION_KEY") or os.getenv("DATA_ENCRYPTION_KEY")
        if not key:
            abort(400)
        try:
            from cryptography.fernet import Fernet
            raw = Fernet(key.encode() if isinstance(key, str) else key).decrypt(open(path, "rb").read())
        except Exception:
            abort(400)
        return Response(raw, mimetype="application/zip",
                        headers={"Content-Disposition": "attachment; filename=" + name[:-4]})
    return send_file(path, as_attachment=True)


@app.route("/api/backup/list")
@login_required
@owner_required
def api_backup_list():
    bdir = os.path.join(PROJECT_ROOT, getattr(config, "BACKUP_DIR", "backups"))
    items = []
    if os.path.isdir(bdir):
        for f in sorted(os.listdir(bdir), reverse=True):
            fp = os.path.join(bdir, f)
            if os.path.isfile(fp):
                items.append({"name": f, "size": os.path.getsize(fp),
                              "modified": time.strftime("%Y-%m-%d %H:%M",
                                                        time.localtime(os.path.getmtime(fp)))})
    return jsonify({"ok": True, "backups": items})


@app.route("/api/backup/delete/<name>", methods=["POST"])
@login_required
@owner_required
def api_backup_delete(name):
    name = os.path.basename(name)
    bdir = os.path.join(PROJECT_ROOT, getattr(config, "BACKUP_DIR", "backups"))
    path = os.path.join(bdir, name)
    if not os.path.isfile(path):
        return jsonify({"ok": False, "error": "Not found"}), 404
    try:
        os.remove(path)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    log_action("delete_backup", name)
    return jsonify({"ok": True})


# ================================================================ API: EXPORTS
def _csv_response(rows, filename):
    out = io.StringIO()
    if rows:
        writer = csv.DictWriter(out, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":
                             "attachment; filename={}".format(filename)})


@app.route("/api/export/<kind>")
@login_required
@owner_required
def api_export(kind):
    if kind == "orders":
        rows = q("SELECT * FROM orders ORDER BY id DESC")
    elif kind == "users":
        rows = q("SELECT user_id, first_name, username, balance, blocked, joined FROM users")
    elif kind == "products":
        rows = q("SELECT id, name, price, stock, active, featured FROM products")
    else:
        abort(404)
    log_action("export", kind)
    return _csv_response(rows, "{}.csv".format(kind))


@app.route("/api/deposits")
@login_required
def api_deposits():
    status = (request.args.get("status") or "").strip()
    method = (request.args.get("method") or "").strip().lower()
    base = ("SELECT d.id, d.user_id, d.amount, d.unique_amount, d.status, "
            "COALESCE(NULLIF(d.method,''),'bep20') AS method, "
            "d.paid_at, d.created, u.first_name, u.username "
            "FROM deposits d LEFT JOIN users u ON d.user_id=u.user_id ")
    where, params = [], []
    if status:
        where.append("d.status=?")
        params.append(status)
    if method:
        # Legacy rows have no method stored -- they were all BEP20.
        where.append("COALESCE(NULLIF(d.method,''),'bep20')=?")
        params.append(method)
    if where:
        base += "WHERE " + " AND ".join(where) + " "
    rows = q(base + "ORDER BY d.id DESC LIMIT 500", tuple(params))
    total_paid = (q("SELECT COALESCE(SUM(amount),0) s FROM deposits WHERE status=?", ("paid",), one=True) or {}).get("s", 0)
    pending = (q("SELECT COUNT(*) c FROM deposits WHERE status=?", ("pending",), one=True) or {}).get("c", 0)
    by_method = q("SELECT COALESCE(NULLIF(method,''),'bep20') AS m, "
                  "COALESCE(SUM(amount),0) AS s, COUNT(*) AS c "
                  "FROM deposits WHERE status='paid' "
                  "GROUP BY COALESCE(NULLIF(method,''),'bep20') ORDER BY s DESC")
    return jsonify({"ok": True, "deposits": rows,
                    "total_paid": round(total_paid or 0, 2), "pending": pending or 0,
                    "by_method": by_method})


@app.route("/api/restock")
@login_required
def api_restock():
    rows = q("SELECT r.product_id, COUNT(*) requests, p.name, "
             "COALESCE(s.available,0) AS available "
             "FROM restock_notifications r LEFT JOIN products p ON r.product_id=p.id "
             "LEFT JOIN (SELECT product_id, COUNT(*) AS available FROM product_stock "
             "WHERE sold=0 GROUP BY product_id) s ON s.product_id=r.product_id "
             "WHERE COALESCE(r.notified,0)=0 GROUP BY r.product_id, p.name, s.available "
             "ORDER BY requests DESC")
    out = [{"name": r.get("name") or ("#" + str(r.get("product_id"))),
            "requests": r.get("requests", 0), "available": r.get("available", 0)}
           for r in rows]
    return jsonify({"ok": True, "restock": out})


@app.route("/api/profit")
@login_required
def api_profit():
    prods = q("SELECT id, name, price, COALESCE(cost,0) cost FROM products ORDER BY id DESC")
    stock_counts = {r["product_id"]: r["units"] for r in q(
        "SELECT product_id, COUNT(*) units FROM product_stock WHERE sold=1 GROUP BY product_id")}
    order_groups = q("SELECT product_id, product_name, COALESCE(SUM(price),0) s "
                     "FROM orders GROUP BY product_id, product_name")
    revenue_by_id = {}
    legacy_revenue = []
    for order in order_groups:
        if order.get("product_id") is not None:
            revenue_by_id[order["product_id"]] = revenue_by_id.get(order["product_id"], 0) + (order.get("s") or 0)
        else:
            legacy_revenue.append(order)
    rows = []
    tot_rev = tot_cost = 0.0
    for p in prods:
        pid = p["id"]
        units = stock_counts.get(pid, 0) or 0
        # Orders store product_name as "<name> xN", so an exact match never hit
        # and revenue silently fell back to price*units for every product.
        revenue = revenue_by_id.get(pid, 0) or 0
        pname = p["name"] or ""
        revenue += sum((o.get("s") or 0) for o in legacy_revenue
                       if o.get("product_name") == pname or
                       str(o.get("product_name") or "").startswith(pname + " x"))
        if not revenue and units:
            revenue = (p["price"] or 0) * units
        cost_unit = p["cost"] or 0
        cost_total = cost_unit * units
        profit = revenue - cost_total
        margin = round(profit / revenue * 100, 1) if revenue else 0
        tot_rev += revenue
        tot_cost += cost_total
        rows.append({"name": p["name"], "units": units, "cost": round(cost_unit, 2),
                     "revenue": round(revenue, 2), "cost_total": round(cost_total, 2),
                     "profit": round(profit, 2), "margin": margin})
    tot_profit = tot_rev - tot_cost
    tot_margin = round(tot_profit / tot_rev * 100, 1) if tot_rev else 0
    return jsonify({"ok": True, "rows": rows, "totals": {
        "revenue": round(tot_rev, 2), "cost": round(tot_cost, 2),
        "profit": round(tot_profit, 2), "margin": tot_margin}})


@app.route("/api/analytics")
@login_required
def api_analytics():
    days = [(now() - dt.timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(13, -1, -1)]
    sales_map = {str(r["day"]): r.get("s", 0) or 0 for r in q(
        "SELECT DATE(created) day, COALESCE(SUM(price),0) s FROM orders "
        "WHERE DATE(created)>=? GROUP BY DATE(created)", (days[0],))}
    deposit_map = {str(r["day"]): r.get("s", 0) or 0 for r in q(
        "SELECT DATE(COALESCE(paid_at,created)) day, COALESCE(SUM(amount),0) s "
        "FROM deposits WHERE status=? AND DATE(COALESCE(paid_at,created))>=? "
        "GROUP BY DATE(COALESCE(paid_at,created))", ("paid", days[0]))}
    new_map = {str(r["day"]): r.get("c", 0) or 0 for r in q(
        "SELECT DATE(joined) day, COUNT(*) c FROM users WHERE DATE(joined)>=? "
        "GROUP BY DATE(joined)", (days[0],))}
    running = (q("SELECT COUNT(*) c FROM users WHERE DATE(joined)<?", (days[0],), one=True) or {}).get("c", 0) or 0
    sales_deposits, user_growth = [], []
    for day in days:
        running += new_map.get(day, 0)
        sales_deposits.append({"date": day, "sales": round(sales_map.get(day, 0), 2),
                               "deposits": round(deposit_map.get(day, 0), 2)})
        user_growth.append({"date": day, "new": new_map.get(day, 0), "total": running})
    busy = [0] * 24
    # Portable hour extraction (SQLite + Postgres): created is 'YYYY-MM-DD HH:MM:SS' text.
    for r in q("SELECT CAST(substr(created, 12, 2) AS INTEGER) h, COUNT(*) c FROM orders GROUP BY h"):
        h = r.get("h")
        if h is not None and 0 <= h < 24:
            busy[h] = r.get("c", 0)
    top = q("SELECT product_name, COUNT(*) sold FROM orders GROUP BY product_name ORDER BY sold DESC LIMIT 8")
    try:
        threshold = int(botdb.get_setting("low_stock_alert_threshold", "5") or 5)
    except Exception:
        threshold = 5
    low = q("SELECT p.name, COALESCE(s.available,0) available FROM products p "
            "LEFT JOIN (SELECT product_id, COUNT(*) available FROM product_stock "
            "WHERE sold=0 GROUP BY product_id) s ON s.product_id=p.id "
            "WHERE COALESCE(p.active,1)=1 AND COALESCE(s.available,0)<=? "
            "ORDER BY available ASC, p.id DESC", (threshold,))
    return jsonify({"ok": True, "sales_deposits": sales_deposits,
                    "user_growth": user_growth, "busy_hours": busy,
                    "top_products": top, "low_stock": low, "low_threshold": threshold})


@app.route("/api/security")
@login_required
def api_security():
    admin = get_web_admin_by_id(session.get("wa_id")) or {}
    logs = q("SELECT username, ip, user_agent, status, created FROM web_login_log "
             "ORDER BY id DESC LIMIT 25")
    enabled = int(admin.get("twofa_enabled") or 0) == 1
    try:
        rec = len(json.loads(admin.get("twofa_recovery") or "[]"))
    except Exception:
        rec = 0
    return jsonify({"ok": True, "twofa_enabled": enabled,
                    "https": WEB_ADMIN_HTTPS == "1", "bot_alerts": bool(TELEGRAM_API),
                    "force_2fa": True,
                    "role": admin.get("role", "admin"),
                    "me": admin.get("username", session.get("username", "")),
                    "idle_minutes": WEB_ADMIN_IDLE_MIN,
                    "ip_allowlist": bool(IP_ALLOWLIST),
                    "rate_limit": RATE_LIMIT_MAX,
                    "recovery_remaining": rec,
                    "encrypted_at_rest": bool(os.getenv("DATA_ENCRYPTION_KEY")),
                    "encrypted_backups": bool(os.getenv("BACKUP_ENCRYPTION_KEY") or os.getenv("DATA_ENCRYPTION_KEY")),
                    "logs": logs})


@app.route("/api/security/2fa/setup", methods=["POST"])
@login_required
def api_2fa_setup():
    secret = gen_totp_secret()
    session["pending_2fa"] = secret
    issuer = getattr(config, "STORE_NAME", "Store").replace(":", " ")
    label = session.get("username", "admin")
    enc_issuer = issuer.replace(" ", "%20")
    otpauth = "otpauth://totp/" + enc_issuer + ":" + label + "?secret=" + secret + "&issuer=" + enc_issuer
    qr_data = ""
    try:
        import qrcode
        qr_io = io.BytesIO()
        qrcode.make(otpauth).save(qr_io, format="PNG")
        qr_data = "data:image/png;base64," + base64.b64encode(qr_io.getvalue()).decode("ascii")
    except Exception:
        pass
    return jsonify({"ok": True, "secret": secret, "qr": qr_data})


@app.route("/api/security/2fa/enable", methods=["POST"])
@login_required
def api_2fa_enable():
    d = request.get_json(force=True) or {}
    secret = session.get("pending_2fa")
    if not secret:
        return jsonify({"ok": False, "error": "Start setup again"}), 400
    if not verify_totp(secret, d.get("code")):
        return jsonify({"ok": False, "error": "Invalid code"}), 400
    codes = gen_recovery_codes()
    ex("UPDATE web_admins SET twofa_secret=?, twofa_enabled=1, twofa_recovery=? WHERE id=?",
       (encrypt_text(secret), json.dumps([sha256_hex(c) for c in codes]), session.get("wa_id")))
    session.pop("pending_2fa", None)
    log_action("enable_2fa", "Web admin 2FA enabled")
    return jsonify({"ok": True, "recovery_codes": codes})


@app.route("/api/security/2fa/disable", methods=["POST"])
@login_required
def api_2fa_disable():
    # 2FA is mandatory for every admin and cannot be turned off.
    return jsonify({"ok": False, "error": "2FA is mandatory and cannot be disabled"}), 400


@app.route("/api/security/2fa/recovery", methods=["POST"])
@login_required
def api_2fa_recovery():
    admin = get_web_admin_by_id(session.get("wa_id")) or {}
    if int(admin.get("twofa_enabled") or 0) != 1:
        return jsonify({"ok": False, "error": "Enable 2FA first"}), 400
    d = request.get_json(force=True) or {}
    if not verify_totp(decrypt_text(admin.get("twofa_secret")), d.get("code")):
        return jsonify({"ok": False, "error": "Invalid code"}), 400
    codes = gen_recovery_codes()
    ex("UPDATE web_admins SET twofa_recovery=? WHERE id=?",
       (json.dumps([sha256_hex(c) for c in codes]), admin["id"]))
    log_action("regen_recovery", "Web admin recovery codes regenerated")
    return jsonify({"ok": True, "recovery_codes": codes})


# ======================================= Web admin accounts (owner-only) ======
@app.route("/api/webadmins")
@login_required
@owner_required
def api_webadmins():
    if not _is_owner():
        return jsonify({"ok": False, "error": "Only an owner can manage admin accounts"}), 403
    return jsonify({"ok": True, "me": session.get("username", ""), "admins": list_web_admins()})


@app.route("/api/webadmins/create", methods=["POST"])
@login_required
@owner_required
def api_webadmins_create():
    if not _is_owner():
        return jsonify({"ok": False, "error": "Only an owner can add admins"}), 403
    d = request.get_json(force=True) or {}
    username = (d.get("username") or "").strip()
    password = d.get("password") or ""
    role = "owner" if (d.get("role") == "owner") else "admin"
    if len(username) < 3:
        return jsonify({"ok": False, "error": "Username must be at least 3 characters"}), 400
    if len(password) < 6:
        return jsonify({"ok": False, "error": "Password must be at least 6 characters"}), 400
    if get_web_admin_any(username):
        return jsonify({"ok": False, "error": "That username already exists"}), 400
    ex("INSERT INTO web_admins(username, pwd, role, active, twofa_enabled) VALUES(?,?,?,1,0)",
       (username, hash_web_password(password), role))
    log_action("create_admin", "Created web admin: " + username + " (" + role + ")")
    return jsonify({"ok": True})


@app.route("/api/webadmins/<int:aid>/password", methods=["POST"])
@login_required
@owner_required
def api_webadmins_password(aid):
    if not _is_owner():
        return jsonify({"ok": False, "error": "Only an owner can reset passwords"}), 403
    d = request.get_json(force=True) or {}
    password = d.get("password") or ""
    if len(password) < 6:
        return jsonify({"ok": False, "error": "Password must be at least 6 characters"}), 400
    row = get_web_admin_by_id(aid)
    if not row:
        return jsonify({"ok": False, "error": "Account not found"}), 404
    ex("UPDATE web_admins SET pwd=? WHERE id=?", (hash_web_password(password), aid))
    log_action("reset_admin_pw", "Reset password for: " + row["username"])
    return jsonify({"ok": True})


@app.route("/api/webadmins/<int:aid>/twofa/reset", methods=["POST"])
@login_required
@owner_required
def api_webadmins_2fa_reset(aid):
    if not _is_owner():
        return jsonify({"ok": False, "error": "Only an owner can reset 2FA"}), 403
    row = get_web_admin_by_id(aid)
    if not row:
        return jsonify({"ok": False, "error": "Account not found"}), 404
    ex("UPDATE web_admins SET twofa_secret='', twofa_enabled=0, twofa_recovery='[]' WHERE id=?", (aid,))
    log_action("reset_admin_2fa", "Reset 2FA for: " + row["username"] + " (will set up again at next login)")
    return jsonify({"ok": True})


@app.route("/api/webadmins/<int:aid>/toggle", methods=["POST"])
@login_required
@owner_required
def api_webadmins_toggle(aid):
    if not _is_owner():
        return jsonify({"ok": False, "error": "Only an owner can change accounts"}), 403
    row = get_web_admin_by_id(aid)
    if not row:
        return jsonify({"ok": False, "error": "Account not found"}), 404
    if aid == session.get("wa_id"):
        return jsonify({"ok": False, "error": "You cannot deactivate your own account"}), 400
    if row["role"] == "owner" and int(row["active"]) == 1 and _active_owner_count() <= 1:
        return jsonify({"ok": False, "error": "Cannot deactivate the last owner"}), 400
    newv = 0 if int(row["active"]) == 1 else 1
    ex("UPDATE web_admins SET active=? WHERE id=?", (newv, aid))
    log_action("toggle_admin", row["username"] + " -> " + ("active" if newv else "disabled"))
    return jsonify({"ok": True, "active": newv})


@app.route("/api/webadmins/<int:aid>/delete", methods=["POST"])
@login_required
@owner_required
def api_webadmins_delete(aid):
    if not _is_owner():
        return jsonify({"ok": False, "error": "Only an owner can delete accounts"}), 403
    row = get_web_admin_by_id(aid)
    if not row:
        return jsonify({"ok": False, "error": "Account not found"}), 404
    if aid == session.get("wa_id"):
        return jsonify({"ok": False, "error": "You cannot delete your own account"}), 400
    if row["role"] == "owner" and _active_owner_count() <= 1:
        return jsonify({"ok": False, "error": "Cannot delete the last owner"}), 400
    ex("DELETE FROM web_admins WHERE id=?", (aid,))
    log_action("delete_admin", "Deleted web admin: " + row["username"])
    return jsonify({"ok": True})


@app.route("/api/branding/logo", methods=["POST"])
@login_required
def api_branding_logo():
    f = request.files.get("logo")
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "No file selected"}), 400
    ext = os.path.splitext(f.filename)[1].lower()
    # SVG removed: it is an executable document. An uploaded SVG was served from
    # /static/uploads/ and rendered inline, giving stored XSS in the admin panel.
    if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        return jsonify({"ok": False, "error": "Use PNG, JPG, WEBP or GIF"}), 400
    updir = os.path.join(app.static_folder or "static", "uploads")
    os.makedirs(updir, exist_ok=True)
    for old in glob.glob(os.path.join(updir, "logo.*")):
        try:
            os.remove(old)
        except Exception:
            pass
    fname = "logo" + ext
    f.save(os.path.join(updir, fname))
    url = "/static/uploads/" + fname
    botdb.set_setting("web_logo", url)
    log_action("update_logo", fname)
    return jsonify({"ok": True, "logo": url})


@app.route("/api/branding/logo/remove", methods=["POST"])
@login_required
def api_branding_logo_remove():
    updir = os.path.join(app.static_folder or "static", "uploads")
    for old in glob.glob(os.path.join(updir, "logo.*")):
        try:
            os.remove(old)
        except Exception:
            pass
    botdb.set_setting("web_logo", "")
    log_action("remove_logo", "")
    return jsonify({"ok": True})


@app.route("/api/sessions")
@login_required
def api_sessions():
    cur = session.get("sid")
    out = []
    for r in list_sessions():
        rr = dict(r)
        rr["current"] = (rr.get("sid") == cur)
        out.append(rr)
    return jsonify({"ok": True, "sessions": out, "current": cur})


@app.route("/api/sessions/revoke", methods=["POST"])
@login_required
def api_sessions_revoke():
    d = request.get_json(force=True) or {}
    sid = d.get("sid")
    if sid:
        revoke_session(sid)
    log_action("revoke_session", "Revoked a web session")
    return jsonify({"ok": True})


@app.route("/api/sessions/signout-all", methods=["POST"])
@login_required
def api_sessions_signout_all():
    revoke_sessions(except_sid=session.get("sid"))
    log_action("signout_all", "Signed out all other sessions")
    return jsonify({"ok": True})


@app.route("/api/version")
@login_required
def api_version():
    return jsonify({"ok": True, "build": "v4-2026-06-14",
                    "features": ["security", "analytics", "deposits", "restock", "profit",
                                 "2fa", "recovery-codes", "rate-limit", "ip-allowlist",
                                 "csp-hsts", "idle-logout", "encrypted-backups",
                                 "active-sessions", "custom-logo"]})



# ============================ Resellers (Phase 2) ============================
# Master control for the Phase-1 reseller foundation. View/search/filter plus
# the tabbed detail page (public-ID routed, IDOR-safe) with lifecycle actions.
# Pricing, limits, key issuance and the public API land in later phases.
# Every route is @login_required (server-side); resellers have no web-admin
# credentials at all. Secrets/hashes are never returned.


@app.route("/api/resellers")
@login_required
def api_resellers():
    search = (request.args.get("search") or "").strip()
    status = (request.args.get("status") or "").strip()
    page = max(1, int(request.args.get("page", 1) or 1))
    per_page = min(100, max(10, int(request.args.get("per_page", 25) or 25)))
    where, params = "WHERE 1=1", []
    if status in rdb.RESELLER_STATUSES:
        where += " AND r.status=?"
        params.append(status)
    if search:
        where += (" AND (r.public_id LIKE ? OR r.name LIKE ? OR r.username LIKE ?"
                  " OR CAST(r.telegram_user_id AS TEXT) LIKE ?)")
        params += ["%" + search + "%"] * 4
    total = int((q("SELECT COUNT(*) c FROM resellers r " + where,
                   tuple(params), one=True) or {}).get("c", 0))
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages)
    rows = q(
        "SELECT r.id, r.public_id, r.telegram_user_id, r.name, r.username, r.status,"
        " r.created, r.updated, COALESCE(w.balance_cents, 0) AS balance_cents,"
        " (SELECT COUNT(*) FROM reseller_api_keys k WHERE k.reseller_id=r.id AND k.status='active') AS keys_count,"
        " (SELECT COUNT(*) FROM orders o WHERE o.user_id=r.telegram_user_id) AS orders_count,"
        " (SELECT MAX(k2.last_used_at) FROM reseller_api_keys k2 WHERE k2.reseller_id=r.id) AS last_key_used"
        " FROM resellers r LEFT JOIN reseller_wallet w ON w.reseller_id=r.id "
        + where + " ORDER BY r.id DESC LIMIT ? OFFSET ?",
        tuple(params) + (per_page, (page - 1) * per_page),
    )
    for r in rows:
        r["balance"] = rdb.from_cents(r["balance_cents"])
        r["last_activity"] = max(x for x in (r["updated"], r["last_key_used"], r["created"]) if x)
    return jsonify({"ok": True, "resellers": rows, "total": total, "page": page,
                    "pages": pages, "per_page": per_page,
                    "statuses": list(rdb.RESELLER_STATUSES)})


_RESELLER_PUBID_RE = re.compile(r"^RSR-[A-HJ-NP-Z2-9]{10}$")


def _resolve_reseller(pubid):
    """IDOR guard: detail/status routes resolve ONLY by the unguessable public
    ID (never the sequential internal id); malformed and unknown ids both
    return a plain 404 — no enumeration oracle."""
    if not _RESELLER_PUBID_RE.match(pubid or ""):
        return None
    return rdb.get_reseller_by_public_id(pubid)


@app.route("/api/resellers/<pubid>")
@login_required
def api_reseller_detail(pubid):
    res = _resolve_reseller(pubid)
    if not res:
        return jsonify({"ok": False, "error": "Reseller not found"}), 404
    rid = res["id"]
    keys_count = int((q("SELECT COUNT(*) c FROM reseller_api_keys WHERE reseller_id=?",
                        (rid,), one=True) or {}).get("c", 0))
    access_count = int((q("SELECT COUNT(*) c FROM reseller_product_access WHERE reseller_id=? AND enabled=1",
                          (rid,), one=True) or {}).get("c", 0))
    orders_count = 0
    if res.get("telegram_user_id"):
        orders_count = int((q("SELECT COUNT(*) c FROM orders WHERE user_id=?",
                              (res["telegram_user_id"],), one=True) or {}).get("c", 0))
    api_stats = q(
        "SELECT COUNT(*) c,"
        " SUM(CASE WHEN status_code >= 400 THEN 1 ELSE 0 END) errors,"
        " AVG(response_ms) avg_ms, MAX(created) last_at"
        " FROM reseller_api_logs WHERE reseller_id=?", (rid,), one=True) or {}
    return jsonify({
        "ok": True, "reseller": res,
        "balance": rdb.from_cents(rdb.get_wallet_balance_cents(rid)),
        "orders_count": orders_count,
        "keys_count": keys_count,
        "access_count": access_count,
        "api_stats": {
            "total": int(api_stats.get("c") or 0),
            "errors": int(api_stats.get("errors") or 0),
            "avg_ms": round(float(api_stats.get("avg_ms") or 0)),
            "last_at": api_stats.get("last_at"),
        },
    })


@app.route("/api/resellers/<pubid>/status", methods=["POST"])
@login_required
def api_reseller_status(pubid):
    res = _resolve_reseller(pubid)
    if not res:
        return jsonify({"ok": False, "error": "Reseller not found"}), 404
    rid = res["id"]
    data = request.get_json(silent=True) or request.form
    action = str(data.get("action") or "").strip().lower()
    ok, msg, new_status = rdb.apply_reseller_action(rid, action)
    if not ok:
        return jsonify({"ok": False, "error": msg}), 400
    log_action("reseller_" + action,
               f"reseller {res.get('public_id', '?')} (#{rid}) -> {new_status}")
    try:
        rdb.log_security_event(
            rid, "admin_status_" + action, ip=_client_ip(),
            user_agent=(request.headers.get("User-Agent") or "")[:200],
            description=f"web admin '{session.get('username', '?')}' -> {new_status}")
    except Exception:
        pass
    return jsonify({"ok": True, "status": new_status, "message": msg})


@app.route("/api/resellers/<pubid>/products")
@login_required
def api_reseller_products(pubid):
    """Master product catalog with THIS reseller's access flags (read-only).
    Products stay the single master source — this only joins the Phase-1
    reseller_product_access permission rows."""
    res = _resolve_reseller(pubid)
    if not res:
        return jsonify({"ok": False, "error": "Reseller not found"}), 404
    rid = res["id"]
    search = (request.args.get("search") or "").strip()
    flt = (request.args.get("filter") or "").strip().lower()
    where, params = "WHERE 1=1", []
    if search:
        where += " AND p.name LIKE ?"
        params.append("%" + search + "%")
    if flt == "enabled":
        where += " AND COALESCE(a.enabled, 0) = 1"
    elif flt == "disabled":
        where += " AND COALESCE(a.enabled, 0) = 0"
    total = int((q(
        "SELECT COUNT(*) c FROM products p"
        " LEFT JOIN reseller_product_access a ON a.product_id=p.id AND a.reseller_id=? "
        + where, (rid, *params), one=True) or {}).get("c", 0))
    rows = q(
        "SELECT p.id, p.name, p.price, p.stock, p.active,"
        " COALESCE(a.enabled, 0) AS access_enabled, a.updated AS access_updated"
        " FROM products p"
        " LEFT JOIN reseller_product_access a ON a.product_id=p.id AND a.reseller_id=? "
        + where +
        " ORDER BY access_enabled DESC, p.active DESC, p.name ASC LIMIT 200",
        (rid, *params),
    )
    enabled_count = int((q(
        "SELECT COUNT(*) c FROM reseller_product_access WHERE reseller_id=? AND enabled=1",
        (rid,), one=True) or {}).get("c", 0))
    return jsonify({"ok": True, "products": rows, "total": total,
                    "enabled_count": enabled_count, "capped": total > 200})


@app.route("/api/resellers/<pubid>/products/access", methods=["POST"])
@login_required
def api_reseller_product_access(pubid):
    """Enable/disable a reseller's access to an EXISTING master product.
    Server-side validation: reseller (public-id), product exists, product is
    active for enabling. Duplicates impossible: UNIQUE(reseller_id, product_id)
    + upsert helper from Phase 1."""
    res = _resolve_reseller(pubid)
    if not res:
        return jsonify({"ok": False, "error": "Reseller not found"}), 404
    rid = res["id"]
    data = request.get_json(silent=True) or request.form
    try:
        product_id = int(data.get("product_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid product_id"}), 400
    enabled = bool(data.get("enabled"))
    prod = q("SELECT id, name, active FROM products WHERE id=?", (product_id,), one=True)
    if not prod:
        return jsonify({"ok": False, "error": "Product not found"}), 404
    if enabled and not prod.get("active"):
        return jsonify({"ok": False,
                        "error": "That product is disabled/hidden in the catalog — activate it in Products first."}), 400
    if not enabled:
        existing = q("SELECT id, enabled FROM reseller_product_access WHERE reseller_id=? AND product_id=?",
                     (rid, product_id), one=True)
        if not existing or not existing.get("enabled"):
            count = int((q("SELECT COUNT(*) c FROM reseller_product_access WHERE reseller_id=? AND enabled=1",
                           (rid,), one=True) or {}).get("c", 0))
            return jsonify({"ok": True, "product_id": product_id, "enabled": False,
                            "access_count": count, "note": "already disabled"})
    rdb.set_product_access(rid, product_id, enabled)
    count = int((q("SELECT COUNT(*) c FROM reseller_product_access WHERE reseller_id=? AND enabled=1",
                   (rid,), one=True) or {}).get("c", 0))
    log_action("reseller_access_" + ("enable" if enabled else "disable"),
               f"reseller {res.get('public_id', '?')} (#{rid}) -> product #{product_id} ({prod.get('name', '?')})")
    try:
        rdb.log_security_event(
            rid, "admin_access_" + ("enable" if enabled else "disable"), ip=_client_ip(),
            user_agent=(request.headers.get("User-Agent") or "")[:200],
            description=f"web admin '{session.get('username', '?')}' product #{product_id}")
    except Exception:
        pass
    return jsonify({"ok": True, "product_id": product_id, "enabled": enabled,
                    "access_count": count})


@app.route("/api/resellers/<pubid>/pricing")
@login_required
def api_reseller_pricing(pubid):
    """Master catalog with this reseller's pricing rules + the effective
    server-computed price for each product (rule -> tier -> master fallback)."""
    res = _resolve_reseller(pubid)
    if not res:
        return jsonify({"ok": False, "error": "Reseller not found"}), 404
    rid = res["id"]
    search = (request.args.get("search") or "").strip()
    where, params = "WHERE 1=1", []
    if search:
        where += " AND p.name LIKE ?"
        params.append("%" + search + "%")
    total = int((q(
        "SELECT COUNT(*) c FROM products p "
        "LEFT JOIN reseller_pricing_rules r ON r.product_id=p.id AND r.reseller_id=? "
        + where, (rid, *params), one=True) or {}).get("c", 0))
    rows = q(
        "SELECT p.id, p.name, p.price, p.active,"
        " r.rule_type, r.value_cents, r.value_bps, r.enabled AS rule_enabled, r.updated AS rule_updated"
        " FROM products p"
        " LEFT JOIN reseller_pricing_rules r ON r.product_id=p.id AND r.reseller_id=? "
        + where +
        " ORDER BY COALESCE(r.enabled, 0) DESC, p.active DESC, p.name ASC LIMIT 200",
        (rid, *params),
    )
    for row in rows:
        master_cents, _tier = rdb.get_master_unit_price_cents(row["id"], 1)
        unit, source = rdb.resolve_reseller_price_cents(rid, row["id"], 1)
        row["master_unit"] = rdb.from_cents(master_cents)
        row["effective_unit"] = rdb.from_cents(unit)
        row["source"] = source
        row["rule_label"] = ""
        if row.get("rule_type"):
            if row["rule_type"] == "fixed_price":
                row["rule_label"] = "$" + rdb.from_cents(row["value_cents"]) + " fixed"
            else:
                bps = int(row["value_bps"])
                pct = f"{bps // 100}.{bps % 100:02d}".rstrip("0").rstrip(".")
                sign = "-" if row["rule_type"] == "discount_percent" else "+"
                row["rule_label"] = f"{sign}{pct}% vs master"
    return jsonify({"ok": True, "products": rows, "total": total, "capped": total > 200,
                    "rule_types": list(rdb.RULE_TYPES)})


@app.route("/api/resellers/<pubid>/pricing/set", methods=["POST"])
@login_required
def api_reseller_pricing_set(pubid):
    """Create/update/enable/disable a reseller pricing rule.
    Money arrives as a STRING (JSON numbers/floats are rejected) and is parsed
    with Decimal into integer cents/bps. The rule value is validated here AND
    in rdb.set_pricing_rule AND by the table's CHECK constraint. The customer
    price is never touched."""
    res = _resolve_reseller(pubid)
    if not res:
        return jsonify({"ok": False, "error": "Reseller not found"}), 404
    rid = res["id"]
    data = request.get_json(silent=True) or request.form
    try:
        product_id = int(data.get("product_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid product_id"}), 400
    prod = q("SELECT id, name, price, active FROM products WHERE id=?", (product_id,), one=True)
    if not prod:
        return jsonify({"ok": False, "error": "Product not found"}), 404
    rule_type = str(data.get("rule_type") or "").strip()
    if rule_type not in rdb.RULE_TYPES:
        return jsonify({"ok": False, "error": f"rule_type must be one of {', '.join(rdb.RULE_TYPES)}"}), 400
    enabled = bool(data.get("enabled", True))
    raw = data.get("value")
    if not isinstance(raw, str):
        return jsonify({"ok": False, "error": 'value must be a string, e.g. "8.50" or "15" (JSON numbers are rejected on purpose)'}), 400
    try:
        if rule_type == "fixed_price":
            value = rdb.to_cents(raw)          # "8.50" -> 850
        else:
            value = rdb.percent_to_bps(raw)    # "15" -> 1500
        rdb.set_pricing_rule(rid, product_id, rule_type, value, enabled)
    except (ValueError, TypeError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    unit, source = rdb.resolve_reseller_price_cents(rid, product_id, 1)
    log_action("reseller_price_set",
               f"reseller {res.get('public_id', '?')} (#{rid}) product #{product_id} "
               f"({prod.get('name', '?')}) -> {rule_type}={raw} enabled={enabled}")
    try:
        rdb.log_security_event(
            rid, "admin_price_set", ip=_client_ip(),
            user_agent=(request.headers.get("User-Agent") or "")[:200],
            description=f"web admin '{session.get('username', '?')}' product #{product_id} {rule_type}={raw} enabled={enabled}")
    except Exception:
        pass
    return jsonify({"ok": True, "product_id": product_id,
                    "effective_unit": rdb.from_cents(unit), "source": source})


@app.route("/api/resellers/<pubid>/pricing/delete", methods=["POST"])
@login_required
def api_reseller_pricing_delete(pubid):
    """Remove a reseller pricing rule -> pricing falls back to master/tier."""
    res = _resolve_reseller(pubid)
    if not res:
        return jsonify({"ok": False, "error": "Reseller not found"}), 404
    rid = res["id"]
    data = request.get_json(silent=True) or request.form
    try:
        product_id = int(data.get("product_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid product_id"}), 400
    prod = q("SELECT id, name FROM products WHERE id=?", (product_id,), one=True)
    if not prod:
        return jsonify({"ok": False, "error": "Product not found"}), 404
    deleted = rdb.delete_pricing_rule(rid, product_id)
    if deleted:
        log_action("reseller_price_delete",
                   f"reseller {res.get('public_id', '?')} (#{rid}) product #{product_id} ({prod.get('name', '?')})")
        try:
            rdb.log_security_event(
                rid, "admin_price_delete", ip=_client_ip(),
                user_agent=(request.headers.get("User-Agent") or "")[:200],
                description=f"web admin '{session.get('username', '?')}' product #{product_id}")
        except Exception:
            pass
    unit, source = rdb.resolve_reseller_price_cents(rid, product_id, 1)
    return jsonify({"ok": True, "product_id": product_id, "deleted": bool(deleted),
                    "effective_unit": rdb.from_cents(unit), "source": source})


if __name__ == "__main__":
    port = int(os.getenv("WEB_ADMIN_PORT", "5000"))
    print("=" * 60)
    print(" OGGY Web Admin starting")
    print(" BUILD: v4-2026-06-14 (v3 + active sessions / sign-out-everywhere + custom logo)")
    print(" DB:", DB_PATH)
    print(" Telegram push:", "ENABLED" if TELEGRAM_API else "disabled (set BOT_TOKEN)")
    print(" Open: http://127.0.0.1:{}".format(port))
    print("=" * 60)
    app.run(host=os.getenv("WEB_ADMIN_HOST", "0.0.0.0"), port=port, debug=False)
