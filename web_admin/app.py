"""
OGGY PREMIUM STORE - Web Admin
================================
A Flask web admin panel that talks to the SAME shopbot.db your Telegram bot uses.
It reuses the bot's own config, encryption (utils.crypto) and database helpers
(database.db) so the bot and the web admin stay perfectly in sync.

Run from the project root:  python web_admin/app.py
"""

import os
import re as _re
import sys
import csv
import io
import time
import json
import hmac
import hashlib
import secrets
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
import database.db as botdb

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
    try:
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
        return
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
        "img-src 'self' data: https://api.qrserver.com; "
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


def _client_ip():
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
    plain = os.getenv("WEB_ADMIN_PASSWORD", "admin")
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
            pwd = hash_web_password(os.getenv("WEB_ADMIN_PASSWORD", "admin"))
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
        import urllib.parse as _u
        qr = "https://api.qrserver.com/v1/create-qr-code/?size=190x190&data=" + _u.quote(otpauth, safe="")
        return (
            '<input type="hidden" name="step" value="setup_2fa">\n'
            '    <p style="color:#9aa;margin:0 0 10px;font-size:13px">Scan this QR in Google Authenticator or Authy (or type the key), then enter the 6-digit code to finish.</p>\n'
            '    <div style="text-align:center;margin:4px 0 12px"><img src="' + qr + '" alt="QR" style="border-radius:12px;background:#fff;padding:8px"></div>\n'
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
        return render_page("login.html", STORE=store, FIELDS=fields, BTN_LABEL=btn,
                           SUBTITLE=subtitle, ERROR_BLOCK=block)

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
            if verify_totp(admin.get("twofa_secret"), code) or consume_admin_recovery(admin["id"], code):
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
               (secret, json.dumps([sha256_hex(c) for c in codes]), admin["id"]))
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
                       CSRF=csrf_token(),
                       BOT=("online" if TELEGRAM_API else "offline"))


# ============================================================== API: DASHBOARD
@app.route("/api/dashboard")
@login_required
def api_dashboard():
    stats = botdb.smart_dashboard_stats()
    # 7-day revenue series
    series = []
    for i in range(6, -1, -1):
        day = (now() - dt.timedelta(days=i)).strftime("%Y-%m-%d")
        row = q("SELECT COALESCE(SUM(price),0) AS rev, COUNT(*) AS cnt "
                "FROM orders WHERE DATE(created)=?", (day,), one=True)
        series.append({"date": day, "revenue": round(row["rev"] or 0, 2),
                       "orders": row["cnt"] or 0})
    totals = {
        "users": (q("SELECT COUNT(*) c FROM users", one=True) or {}).get("c", 0),
        "products": (q("SELECT COUNT(*) c FROM products", one=True) or {}).get("c", 0),
        "active_products": (q("SELECT COUNT(*) c FROM products WHERE active=1", one=True) or {}).get("c", 0),
        "deposits": (q("SELECT COALESCE(SUM(amount),0) s FROM deposits WHERE status='paid'", one=True) or {}).get("s", 0),
        "blocked": (q("SELECT COUNT(*) c FROM users WHERE blocked=1", one=True) or {}).get("c", 0),
        "pending_withdrawals": (q("SELECT COUNT(*) c FROM withdrawal_requests WHERE status='pending'", one=True) or {}).get("c", 0),
    }
    # top sellers
    top = q("SELECT product_name, COUNT(*) sold, COALESCE(SUM(price),0) revenue "
            "FROM orders GROUP BY product_name ORDER BY sold DESC LIMIT 5")
    recent = q("SELECT o.id, o.user_id, o.product_name, o.price, o.created, "
               "u.first_name, u.username FROM orders o LEFT JOIN users u "
               "ON o.user_id=u.user_id ORDER BY o.id DESC LIMIT 8")
    return jsonify({"ok": True, "stats": stats, "series": series,
                    "totals": totals, "top": top, "recent": recent,
                    "bot_connected": bool(TELEGRAM_API)})


# =============================================================== API: PRODUCTS
def product_row(p):
    pid = p["id"]
    avail = botdb.get_available_stock_count(pid)
    sold = botdb.get_sold_stock_count(pid)
    tiers = [{"quantity": t[1], "price": t[2]} for t in botdb.get_product_price_tiers(pid)]
    p = dict(p)
    p["available_stock"] = avail
    p["sold_count"] = sold
    p["tiers"] = tiers
    return p


@app.route("/api/products")
@login_required
def api_products():
    search = (request.args.get("search") or "").strip()
    if search:
        rows = q("SELECT * FROM products WHERE name LIKE ? ORDER BY id DESC",
                 ("%" + search + "%",))
    else:
        rows = q("SELECT * FROM products ORDER BY id DESC")
    return jsonify({"ok": True, "products": [product_row(p) for p in rows]})


def _broadcast_to_all(msg, pid=None, button_text=None):
    """Send a message to every eligible bot user directly from the web admin."""
    kb = None
    if pid and button_text:
        kb = {"inline_keyboard": [[{"text": button_text, "callback_data": "product_{}".format(pid), "style": "primary"}]]}
    sent = failed = 0
    try:
        users = q("SELECT user_id FROM users WHERE COALESCE(blocked,0)=0 AND COALESCE(notifications,1)=1")
    except Exception:
        return 0, 0
    for u in users:
        ok_send, _info = tg_send(u["user_id"], msg, reply_markup=kb)
        if ok_send:
            sent += 1
        else:
            failed += 1
    return sent, failed


def _product_alert_message(name, price, added, available, mode):
    if mode == "product":
        title = "\U0001f195 <b>New Product Added!</b>"
        intro = "\u26a1 New product is now available!"
    else:
        title = "\U0001f195 <b>New Stock Added!</b>"
        intro = "\u26a1 Grab yours before they\u2019re gone!"
    return ("{}\n\n"
            "\U0001f4e6 <b>{}</b>\n"
            "\u2795 <b>Added:</b> <b>{}</b>\n"
            "\u2705 <b>Available:</b> <b>{}</b>\n"
            "\U0001f4b0 <b>Price:</b> <b>${:.2f}</b>\n\n"
            "<i>{}</i>").format(title, name, added, available, float(price or 0), intro)


def _publish_offer_alert(pid):
    """Broadcast the current price offers of a product to all users (one message)."""
    try:
        prow = q("SELECT name, price FROM products WHERE id=?", (pid,))
        tiers = botdb.get_product_price_tiers(pid)
        if not prow or not tiers:
            return 0, 0
        available = botdb.get_available_stock_count(pid)
        msg = _offer_alert_message(prow[0]["name"], prow[0]["price"], tiers, available)
        sent, failed = _broadcast_to_all(msg, pid=pid, button_text="\U0001f6d2 Tap to Buy Now")
        log_action("offer_broadcast", "#{} sent={} failed={}".format(pid, sent, failed))
        return sent, failed
    except Exception:
        return 0, 0


def _offer_alert_message(name, price, tiers, available):
    lines = []
    for _tid, qty, tier_price in tiers:
        each = tier_price / qty if qty else tier_price
        lines.append("\u2022 <b>{}</b> for <b>${:.2f}</b> (${:.2f} each)".format(qty, tier_price, each))
    return ("\U0001f525 <b>NEW OFFER ALERT!</b>\n\n"
            "\U0001f4e6 <b>{}</b>\n"
            "\U0001f4b0 <b>Price:</b> <b>${:.2f}</b> / code\n"
            "\u2705 <b>Available:</b> <b>{}</b>\n\n"
            "\U0001f381 <b>BULK DISCOUNT ACTIVE</b>\n{}\n\n"
            "\u26a1 <i>Grab this deal before it\u2019s gone!</i>").format(
        name, float(price or 0), available, "\n".join(lines))


def _price_change_message(name, old_price, new_price, available):
    if new_price < old_price:
        pct = round((old_price - new_price) / old_price * 100) if old_price > 0 else 0
        title = "\U0001f525 <b>PRICE DROP ALERT!</b>"
        change_line = "\U0001f4c9 <b>Price Dropped:</b> <b>-{}%</b> \U0001f389".format(pct)
    else:
        pct = round((new_price - old_price) / old_price * 100) if old_price > 0 else 0
        title = "\U0001f4e2 <b>PRICE UPDATE!</b>"
        change_line = "\U0001f4c8 <b>Price Increased:</b> <b>+{}%</b>".format(pct)
    bar = "\u2501" * 18
    return ("{}\n{}\n\n"
            "\U0001f4e6 <b>{}</b>\n\n"
            "\U0001f4b0 <b>Old Price:</b> <s>${:.2f}</s>\n"
            "\u2728 <b>New Price:</b> <b>${:.2f}</b>\n"
            "{}\n"
            "\U0001f4ca <b>Available Stock:</b> <b>{}</b>\n\n"
            "{}\n"
            "\U0001f6d2 <i>Tap below to grab it now!</i> \U0001f447").format(
        title, bar, name, old_price, new_price, change_line, available, bar)


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
            available = botdb.get_available_stock_count(pid)
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
    allowed = {"name": str, "price": float, "delivery": str, "active": int,
               "featured": int, "description": str, "validity": str,
               "warranty": str, "rules": str, "redeem_link": str,
               "max_per_order": int, "cost": float}
    for k, caster in allowed.items():
        if k in d and d[k] is not None:
            fields.append("{}=?".format(k))
            vals.append(caster(d[k]) if caster != str else str(d[k]))
    if not fields:
        return jsonify({"ok": False, "error": "Nothing to update"}), 400

    old_price = None
    if "price" in d and d["price"] is not None:
        prev = q("SELECT price FROM products WHERE id=?", (pid,), one=True)
        if prev:
            old_price = float(prev["price"] or 0)

    vals.append(pid)
    ex("UPDATE products SET {} WHERE id=?".format(", ".join(fields)), tuple(vals))
    log_action("edit_product", "#{} -> {}".format(pid, list(d.keys())))

    # Auto-broadcast price changes to all bot users instantly.
    if old_price is not None:
        try:
            new_price = float(d["price"])
            if abs(new_price - old_price) >= 0.005:
                prod = q("SELECT name FROM products WHERE id=?", (pid,), one=True)
                pname = prod["name"] if prod else "Product"
                available = botdb.get_available_stock_count(pid)
                msg = _price_change_message(pname, old_price, new_price, available)
                sent, failed = _broadcast_to_all(msg, pid=pid, button_text="\U0001f6d2 Buy Now")
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
    codes = (request.get_json(force=True).get("codes") or "").strip()
    if not codes:
        return jsonify({"ok": False, "error": "No codes provided"}), 400
    added = botdb.restock_product(pid, codes)
    log_action("add_stock", "#{} +{}".format(pid, added))
    available = botdb.get_available_stock_count(pid)

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


# ---- price tiers (Price Offers)
@app.route("/api/products/<int:pid>/tiers/set", methods=["POST"])
@login_required
def api_tier_set(pid):
    d = request.get_json(force=True)
    botdb.set_product_price_tier(pid, int(d["quantity"]), float(d["price"]))
    log_action("set_tier", "#{} q{} ${}".format(pid, d["quantity"], d["price"]))
    sent = failed = 0
    if d.get("notify", True):
        sent, failed = _publish_offer_alert(pid)
    return jsonify({"ok": True, "sent": sent, "failed": failed})


@app.route("/api/products/<int:pid>/tiers/bulk_set", methods=["POST"])
@login_required
def api_tier_bulk_set(pid):
    """Save multiple price offers at once, then publish a single broadcast."""
    d = request.get_json(force=True)
    items = d.get("tiers") or []
    saved = 0
    for it in items:
        try:
            qty = int(it["quantity"])
            price = float(it["price"])
            if qty <= 0 or price <= 0:
                continue
            botdb.set_product_price_tier(pid, qty, price)
            saved += 1
        except Exception:
            continue
    if not saved:
        return jsonify({"ok": False, "error": "No valid offers"}), 400
    log_action("set_tiers_bulk", "#{} saved={}".format(pid, saved))
    sent = failed = 0
    if d.get("notify", True):
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
@app.route("/api/orders")
@login_required
def api_orders():
    search = (request.args.get("search") or "").strip()
    limit = int(request.args.get("limit", 100))
    base = ("SELECT o.*, u.first_name, u.username FROM orders o "
            "LEFT JOIN users u ON o.user_id=u.user_id ")
    if search:
        rows = q(base + "WHERE o.product_name LIKE ? OR CAST(o.user_id AS TEXT) LIKE ? "
                 "OR CAST(o.id AS TEXT)=? ORDER BY o.id DESC LIMIT ?",
                 ("%" + search + "%", "%" + search + "%", search, limit))
    else:
        rows = q(base + "ORDER BY o.id DESC LIMIT ?", (limit,))
    for r in rows:
        if r.get("delivery") is not None:
            r["delivery"] = decrypt_text(r["delivery"])
    return jsonify({"ok": True, "orders": rows})


@app.route("/api/orders/<int:oid>/refund", methods=["POST"])
@login_required
def api_order_refund(oid):
    row = q("SELECT * FROM orders WHERE id=?", (oid,), one=True)
    if not row:
        return jsonify({"ok": False, "error": "not found"}), 404
    botdb.add_balance(row["user_id"], float(row["price"] or 0))
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
    limit = int(request.args.get("limit", 100))
    if search:
        rows = q("SELECT * FROM users WHERE CAST(user_id AS TEXT) LIKE ? OR "
                 "first_name LIKE ? OR username LIKE ? ORDER BY joined DESC LIMIT ?",
                 ("%" + search + "%", "%" + search + "%", "%" + search + "%", limit))
    else:
        rows = q("SELECT * FROM users ORDER BY joined DESC LIMIT ?", (limit,))
    return jsonify({"ok": True, "users": rows})


@app.route("/api/users/<int:uid>")
@login_required
def api_user_detail(uid):
    user = q("SELECT * FROM users WHERE user_id=?", (uid,), one=True)
    orders = q("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 50", (uid,))
    deposits = q("SELECT * FROM deposits WHERE user_id=? ORDER BY id DESC LIMIT 50", (uid,))
    return jsonify({"ok": True, "user": user, "orders": orders, "deposits": deposits})


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
    ex("INSERT OR REPLACE INTO coupons(code, discount_type, value, active, max_uses, "
       "used_count, expires_at) VALUES(?,?,?,?,?,COALESCE((SELECT used_count FROM "
       "coupons WHERE code=?),0),?)",
       (code, dtype, value, 1, max_uses, code, expires_at))
    log_action("create_coupon", code)

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
    status = request.args.get("status", "all")
    base = ("SELECT t.*, u.first_name, u.username FROM tickets t "
            "LEFT JOIN users u ON t.user_id=u.user_id ")
    if status == "open":
        rows = q(base + "WHERE t.status IN ('open','answered') ORDER BY t.id DESC")
    else:
        rows = q(base + "ORDER BY t.id DESC LIMIT 100")
    for r in rows:
        for f in ("subject", "message", "admin_reply"):
            if r.get(f) is not None:
                r[f] = decrypt_text(r[f])
    return jsonify({"ok": True, "tickets": rows})


@app.route("/api/tickets/<int:tid>/reply", methods=["POST"])
@login_required
def api_ticket_reply(tid):
    reply = (request.get_json(force=True).get("reply") or "").strip()
    if not reply:
        return jsonify({"ok": False, "error": "Reply required"}), 400
    botdb.reply_ticket(tid, reply)
    t = botdb.get_ticket(tid)
    if t:
        tg_send(t[1], "\U0001f3ab <b>Support reply (Ticket #{})</b>\n\n{}".format(tid, reply))
    log_action("reply_ticket", "#{}".format(tid))
    return jsonify({"ok": True})


@app.route("/api/tickets/<int:tid>/close", methods=["POST"])
@login_required
def api_ticket_close(tid):
    botdb.close_ticket(tid)
    log_action("close_ticket", "#{}".format(tid))
    return jsonify({"ok": True})


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
def api_admins():
    rows = botdb.get_admins()
    out = [{"user_id": r[0], "first_name": r[1], "username": r[2],
            "added_by": r[3], "added_at": r[4]} for r in rows]
    owner = int(os.getenv("ADMIN_ID", "0") or 0)
    return jsonify({"ok": True, "admins": out, "owner_id": owner})


@app.route("/api/admins/add", methods=["POST"])
@login_required
def api_admin_add():
    uid = int(request.get_json(force=True).get("user_id"))
    botdb.add_admin(uid, session.get("admin_id", 0))
    log_action("add_admin", str(uid))
    return jsonify({"ok": True})


@app.route("/api/admins/<int:uid>/remove", methods=["POST"])
@login_required
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
def api_logs():
    rows = q("SELECT * FROM admin_logs ORDER BY id DESC LIMIT 200")
    return jsonify({"ok": True, "logs": rows})


@app.route("/api/logs/clear", methods=["POST"])
@login_required
def api_logs_clear():
    botdb.clear_admin_logs()
    log_action("clear_logs", "")
    return jsonify({"ok": True})


# ================================================================ API: BACKUPS
@app.route("/api/backup/create", methods=["POST"])
@login_required
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
    base = ("SELECT d.id, d.user_id, d.amount, d.unique_amount, d.status, "
            "d.paid_at, d.created, u.first_name, u.username "
            "FROM deposits d LEFT JOIN users u ON d.user_id=u.user_id ")
    if status:
        rows = q(base + "WHERE d.status=? ORDER BY d.id DESC LIMIT 500", (status,))
    else:
        rows = q(base + "ORDER BY d.id DESC LIMIT 500")
    total_paid = (q("SELECT COALESCE(SUM(amount),0) s FROM deposits WHERE status=?", ("paid",), one=True) or {}).get("s", 0)
    pending = (q("SELECT COUNT(*) c FROM deposits WHERE status=?", ("pending",), one=True) or {}).get("c", 0)
    return jsonify({"ok": True, "deposits": rows,
                    "total_paid": round(total_paid or 0, 2), "pending": pending or 0})


@app.route("/api/restock")
@login_required
def api_restock():
    rows = q("SELECT r.product_id, COUNT(*) requests, p.name "
             "FROM restock_notifications r LEFT JOIN products p ON r.product_id=p.id "
             "WHERE COALESCE(r.notified,0)=0 GROUP BY r.product_id, p.name "
             "ORDER BY requests DESC")
    out = []
    for r in rows:
        pid = r.get("product_id")
        out.append({"name": r.get("name") or ("#" + str(pid)),
                    "requests": r.get("requests", 0),
                    "available": botdb.get_available_stock_count(pid)})
    return jsonify({"ok": True, "restock": out})


@app.route("/api/profit")
@login_required
def api_profit():
    prods = q("SELECT id, name, price, COALESCE(cost,0) cost FROM products ORDER BY id DESC")
    rows = []
    tot_rev = tot_cost = 0.0
    for p in prods:
        pid = p["id"]
        units = botdb.get_sold_stock_count(pid) or 0
        rev_row = q("SELECT COALESCE(SUM(price),0) s FROM orders WHERE product_name=?",
                    (p["name"],), one=True) or {}
        revenue = rev_row.get("s", 0) or 0
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
    sales_deposits = []
    user_growth = []
    for i in range(13, -1, -1):
        day = (now() - dt.timedelta(days=i)).strftime("%Y-%m-%d")
        sales = (q("SELECT COALESCE(SUM(price),0) s FROM orders WHERE DATE(created)=?", (day,), one=True) or {}).get("s", 0)
        deps = (q("SELECT COALESCE(SUM(amount),0) s FROM deposits WHERE status=? AND DATE(COALESCE(paid_at,created))=?", ("paid", day), one=True) or {}).get("s", 0)
        sales_deposits.append({"date": day, "sales": round(sales or 0, 2), "deposits": round(deps or 0, 2)})
        new_u = (q("SELECT COUNT(*) c FROM users WHERE DATE(joined)=?", (day,), one=True) or {}).get("c", 0)
        total_u = (q("SELECT COUNT(*) c FROM users WHERE DATE(joined)<=?", (day,), one=True) or {}).get("c", 0)
        user_growth.append({"date": day, "new": new_u or 0, "total": total_u or 0})
    busy = [0] * 24
    for r in q("SELECT CAST(strftime('%H', created) AS INTEGER) h, COUNT(*) c FROM orders GROUP BY h"):
        h = r.get("h")
        if h is not None and 0 <= h < 24:
            busy[h] = r.get("c", 0)
    top = q("SELECT product_name, COUNT(*) sold FROM orders GROUP BY product_name ORDER BY sold DESC LIMIT 8")
    try:
        threshold = int(botdb.get_setting("low_stock_alert_threshold", "5") or 5)
    except Exception:
        threshold = 5
    low = []
    for p in q("SELECT id, name FROM products WHERE COALESCE(active,1)=1 ORDER BY id DESC"):
        avail = botdb.get_available_stock_count(p["id"])
        if avail <= threshold:
            low.append({"name": p["name"], "available": avail})
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
    return jsonify({"ok": True, "secret": secret, "otpauth": otpauth})


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
       (secret, json.dumps([sha256_hex(c) for c in codes]), session.get("wa_id")))
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
    if not verify_totp(admin.get("twofa_secret"), d.get("code")):
        return jsonify({"ok": False, "error": "Invalid code"}), 400
    codes = gen_recovery_codes()
    ex("UPDATE web_admins SET twofa_recovery=? WHERE id=?",
       (json.dumps([sha256_hex(c) for c in codes]), admin["id"]))
    log_action("regen_recovery", "Web admin recovery codes regenerated")
    return jsonify({"ok": True, "recovery_codes": codes})


# ======================================= Web admin accounts (owner-only) ======
@app.route("/api/webadmins")
@login_required
def api_webadmins():
    if not _is_owner():
        return jsonify({"ok": False, "error": "Only an owner can manage admin accounts"}), 403
    return jsonify({"ok": True, "me": session.get("username", ""), "admins": list_web_admins()})


@app.route("/api/webadmins/create", methods=["POST"])
@login_required
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
    if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"):
        return jsonify({"ok": False, "error": "Use PNG, JPG, WEBP, GIF or SVG"}), 400
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
