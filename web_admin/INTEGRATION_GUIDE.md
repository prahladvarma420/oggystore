# OGGY PREMIUM STORE — Web Admin

A dark, glowy, animated web dashboard that controls the **same** shop your
Telegram bot runs. It does **not** replace your bot — it plugs into it by
reading and writing the **same `shopbot.db` database** your bot already uses,
and by reusing your bot's own encryption and helper code.

That means: add a product on the website → it instantly appears in the bot.
Add balance on the website → the user's wallet updates in the bot. And so on.

---

## 1. How the integration works (the big picture)

```
                ┌──────────────────────────┐
                │     shopbot.db (SQLite)   │  ← single shared source of truth
                └────────────┬─────────────┘
                 reads/writes │ reads/writes
            ┌────────────────┴─────────────────┐
            │                                   │
   ┌────────▼─────────┐               ┌─────────▼──────────┐
   │  Telegram bot     │               │   Web Admin (Flask)│
   │  (bot.py)         │               │   (web_admin/app.py)│
   │  python-telegram  │               │   your browser     │
   └───────────────────┘               └────────────────────┘
```

Key points that make them "integrated":

1. **Same database file.** The web admin opens the exact same `shopbot.db`.
   Both processes use SQLite in **WAL mode**, so the bot and the website can
   read/write at the same time without corrupting anything.
2. **Same encryption.** Stock codes are stored encrypted with your
   `DATA_ENCRYPTION_KEY`. The web admin imports your bot's `utils/crypto.py`,
   so it can show and add codes correctly.
3. **Same business logic.** The web admin imports your bot's `database/db.py`
   helpers (balances, coupons, tiers, tickets, backups, etc.) — so the rules
   stay identical everywhere.
4. **Pushing messages to users.** When the web admin needs to message a user
   (broadcasts, ticket replies, refund/balance notices), it works two ways:
   - **Broadcasts** are inserted into your bot's `scheduled_broadcasts` table.
     Your bot already runs `run_scheduled_broadcasts` every 60s and sends them.
   - **Direct notices** (ticket reply, refund, balance change) are sent
     immediately through the Telegram Bot API using your `BOT_TOKEN`.

You keep running your bot exactly as before. The web admin is an **extra**
process you start alongside it.

---

## 2. One-time setup

### Step 1 — Put the `web_admin/` folder inside your project

Your project should look like this (web_admin sits next to bot.py):

```
oggystore_bot/
├── bot.py
├── config.py
├── database/db.py
├── utils/crypto.py
├── shopbot.db
├── tools/generate_secrets.py
├── tools/generate_password_hash.py
└── web_admin/          ← this folder
    ├── app.py
    ├── requirements.txt
    ├── .env.example
    ├── templates/
    └── static/
```

### Step 2 — Create your `.env` in the PROJECT ROOT

The `.env` lives next to `bot.py` (NOT inside `web_admin/`). Both the bot and
the web admin read it. Copy `web_admin/.env.example` to the root as `.env`:

```bash
cp web_admin/.env.example .env
```

Then fill it in. The most important values:

| Variable | What it is |
|---|---|
| `BOT_TOKEN` | Your Telegram bot token (already used by the bot). Lets the web admin push messages. |
| `ADMIN_ID` | Your Telegram numeric user id (the owner). |
| `DATA_ENCRYPTION_KEY` | **Must match the key your bot already uses** so stock codes decrypt. |
| `WEB_ADMIN_SECRET` | A long random string for login session security. |
| `WEB_ADMIN_USERNAME` | The username you'll log in with (default `admin`). |
| `WEB_ADMIN_PASSWORD` | Your login password (quick start). |

> ⚠️ If your bot is already live, you **already have** a `DATA_ENCRYPTION_KEY`.
> Use the same one — otherwise existing stock codes will show as
> `[encrypted: invalid key]`. If you never set one, your codes are stored in
> plain text and you can leave it blank (but set one going forward).

### Step 3 — Generate strong secrets (recommended)

```bash
python tools/generate_secrets.py          # prints WEB_ADMIN_SECRET + keys
python tools/generate_password_hash.py    # prints a hashed admin password
```

For the **hashed password** (more secure than plaintext), put the two printed
lines into `.env` and delete the `WEB_ADMIN_PASSWORD` line:

```
WEB_ADMIN_PASSWORD_SALT=....
WEB_ADMIN_PASSWORD_HASH=....
```

### Step 4 — Install the web admin requirements

```bash
pip install -r web_admin/requirements.txt
```

---

## 3. Running it

You run **two** processes. Keep your bot running as usual, and start the web
admin in a second terminal **from the project root**:

```bash
# Terminal 1 — your bot (unchanged)
python bot.py

# Terminal 2 — the web admin
python web_admin/app.py
```

Then open **http://127.0.0.1:5000** in your browser and log in.

> Running the web admin from the project root matters — that's how it finds
> `config.py`, `database/`, `utils/` and `shopbot.db`.

---

## 4. Every bot admin feature, now on the web

| Bot admin feature | Web admin location |
|---|---|
| Smart Dashboard (revenue, orders, users, low stock) | **Dashboard** (with 7-day chart + top sellers) |
| Products & Stock — add / edit / delete / hide / feature | **Products & Stock** |
| Add / bulk add / manage stock codes (encrypted) | Products → **Stock** |
| Price offers (bulk tiers) | Products → **Offers** |
| Flash sales | Products → **Flash** |
| Orders, refund, resend | **Orders** |
| Users, search, ban/unban | **Users** |
| Add / remove balance | Users → **Balance** |
| Coupons (create/enable/disable/delete) | **Coupons** |
| Broadcast + scheduled broadcasts | **Broadcasts** |
| Message templates | **Templates** |
| Support tickets (reply/close) | **Support** |
| Referral withdrawals (approve/reject) | **Withdrawals** |
| Referral %, loyalty, low-stock, auto-marketing settings | **Settings** |
| Manage admins (add/remove) | **Admins** |
| Admin logs (+ clear) | **Logs** |
| Create / download backups | **Backups** |
| Export orders / users / products to CSV | Export buttons on each list |

All actions are also written to your **admin logs** (tagged `[WEB]`) so you can
see what was done from the website vs. from Telegram.

---

## 5. Deploying it online (optional)

For production, don't expose Flask's dev server directly. Two easy options:

**A. Behind Gunicorn + Nginx (Linux VPS):**
```bash
pip install gunicorn
cd /path/to/oggystore_bot
gunicorn -w 2 -b 127.0.0.1:5000 web_admin.app:app
```
Then put Nginx in front with HTTPS (Let's Encrypt) and proxy to port 5000.

**B. Same machine as the bot, private access only:**
Keep `WEB_ADMIN_HOST=127.0.0.1` and reach it through an SSH tunnel:
```bash
ssh -L 5000:127.0.0.1:5000 user@your-server
```

Security tips:
- Always use the **hashed password** option in production.
- Use HTTPS so your password isn't sent in plain text.
- The web admin must run on the **same machine / same disk** as `shopbot.db`
  (because they share the file). If your bot runs on a server, run the web
  admin there too.

---

## 6. Troubleshooting

| Symptom | Fix |
|---|---|
| Stock codes show `[encrypted: invalid key]` | `DATA_ENCRYPTION_KEY` in `.env` doesn't match the one the bot used. Use the original key. |
| "Bot push: offline" in the sidebar | `BOT_TOKEN` is empty. Add it so ticket replies / notifications send. |
| Broadcast doesn't arrive | The **bot must be running** — it delivers queued broadcasts every ~60s. |
| `ModuleNotFoundError: config` | You started it from the wrong folder. Run `python web_admin/app.py` from the project root. |
| `database is locked` | Rare; WAL mode handles concurrency. Make sure you don't have many other processes writing at once. |
| Login fails | Check `WEB_ADMIN_USERNAME` / `WEB_ADMIN_PASSWORD` (or the hash pair) in `.env`. |

---

## 7. What was NOT changed in your bot

Nothing. The web admin is purely additive — it's a new `web_admin/` folder.
Your `bot.py`, handlers, payments, and database are untouched. You can delete
the `web_admin/` folder at any time and your bot keeps working exactly as before.

---

## 🆕 Update: New Features & Security (v2)

### New sidebar sections
- **Deposits** — every USDT deposit/payment with status (paid / pending) and totals.
- **Restock** — pending "notify me when back in stock" requests, grouped per product.
- **Profit & Cost** — set a **Cost** per product; the panel shows revenue, cost, profit and margin per product and overall.
- **Security** — turn on 2FA, see recent login attempts (with IP), and your security status.
- **Dashboard charts** — Sales vs Deposits (14d), New users (14d), Busiest hours, and Top products.

### New: themes & mobile
- Click the 🎨 button (top bar) to cycle 5 neon themes: neon, cyber, sunset, matrix, candy. Your choice is remembered.
- On phones a ☰ menu button opens the sidebar.

### New environment variable
```
# Set to 1 ONLY when you serve the panel over HTTPS (recommended in production).
# Enables Secure cookies. Leave at 0 for local http testing.
WEB_ADMIN_HTTPS=0
```

### Two-Factor Authentication (Google Authenticator)
1. Log in, open **Security** in the sidebar.
2. Click **Enable 2FA** — scan the QR code with Google Authenticator (or any TOTP app).
3. Enter the 6-digit code to confirm. Done.
4. From now on the login page asks for your 6-digit code after username/password.
5. To turn it off: Security → **Disable 2FA** → enter a current code.

> Lost your phone? Set `web_2fa_enabled` to `0` in the bot's `settings` table (or via the bot DB) to disable it.

### Other security added
- CSRF protection on all write actions, security headers, and HttpOnly/SameSite cookies.
- Account lockout after 5 failed logins (15 min) per IP.
- Login alerts: every login attempt is logged (with IP) and a Telegram alert is sent to the admin (ADMIN_ID).


---

## Update: Maximum Security & Encryption (v3)

This build hardens the panel with several extra layers. All are controlled from `.env` with safe defaults, so nothing locks you out unless you turn it on. Confirm you are on this build: the startup banner shows `BUILD: v3-2026-06-14`, or open `/api/version`.

### What is now protected
| Layer | What it does |
|---|---|
| **Encryption at rest** | Stock codes/keys are stored encrypted (Fernet, `DATA_ENCRYPTION_KEY`). The bot and panel decrypt only when needed. |
| **Encrypted backups** | "Create backup" now produces a `.enc` file encrypted with `BACKUP_ENCRYPTION_KEY`. Useless if stolen. |
| **2FA (TOTP)** | Google Authenticator / Authy login codes. |
| **2FA recovery codes** | 10 one-time codes shown when you enable 2FA, in case you lose your phone. Regenerate anytime. |
| **Mandatory 2FA** | Optionally force 2FA and forbid disabling it (`WEB_ADMIN_FORCE_2FA=1`). |
| **Brute-force lockout** | 5 failed logins per IP = 15-minute lock. |
| **Rate limiting** | Caps requests per IP per minute (`WEB_ADMIN_RATE_LIMIT`). |
| **IP allowlist** | Restrict the panel to specific IPs (`WEB_ADMIN_IP_ALLOWLIST`). |
| **Idle auto-logout** | Logs you out after inactivity (`WEB_ADMIN_IDLE_MIN`). |
| **CSRF + secure cookies** | Blocks cross-site request forgery; HttpOnly/SameSite cookies. |
| **Security headers + CSP** | Content-Security-Policy, anti-clickjacking, no-sniff, HSTS (with HTTPS). |
| **Audit log** | Every admin action is logged with the action, details, and IP (Logs tab). |
| **Login alerts** | A Telegram message to the owner on every login attempt. |

### New .env settings
See `.env.example` for the full list: `WEB_ADMIN_IDLE_MIN`, `WEB_ADMIN_FORCE_2FA`, `WEB_ADMIN_HSTS`, `WEB_ADMIN_IP_ALLOWLIST`, `WEB_ADMIN_RATE_LIMIT`.

> Safety: `127.0.0.1` is always allowed even if you set an IP allowlist, so you can never lock yourself out on the same PC. Enroll 2FA before setting `WEB_ADMIN_FORCE_2FA=1`.

### Using encrypted backups
1. In the **Backups** tab, click **Create backup** -> you get a `shopbot_backup_YYYYMMDD_HHMMSS.zip.enc`.
2. To restore/open it, either:
   - Download it and run: `python tools/decrypt_backup.py backups/<file>.zip.enc` (uses your `.env` key), or
   - Download with decryption via the panel: open `/api/backup/download/<file>.zip.enc?decrypt=1`.
3. Keep `BACKUP_ENCRYPTION_KEY` safe and OFF the server's public folders. Without it, the backup cannot be opened by anyone.

### 2FA recovery codes flow
- When you enable 2FA, a list of 10 codes appears once. Copy and store them safely.
- At login, you can type a recovery code instead of the 6-digit code if you lost your phone (each works once).
- Generate a fresh set anytime from Security -> **New recovery codes**.

### Step-by-step: turn on HTTPS / SSL (recommended for any internet-facing use)
Running Flask's dev server on `http://` is fine on your own PC, but for remote access use HTTPS. Easiest options:

**Option A - Local self-signed (quick, for testing):**
```
pip install pyopenssl
# edit the bottom of app.py app.run(...) to add: ssl_context="adhoc"
```
Then set `WEB_ADMIN_HTTPS=1` so cookies become Secure. Your browser will warn about the self-signed cert (accept it).

**Option B - Real domain with a reverse proxy (recommended for production):**
1. Point a domain/subdomain (e.g. `admin.yourstore.com`) at your server's IP.
2. Run the panel behind **Nginx** or **Caddy**:
   - **Caddy** (auto HTTPS via Lets Encrypt) - `Caddyfile`:
     ```
     admin.yourstore.com {
         reverse_proxy 127.0.0.1:5000
     }
     ```
     Run `caddy run`. Caddy fetches a real certificate automatically.
   - **Nginx**: get a cert with `certbot --nginx -d admin.yourstore.com`, then `proxy_pass http://127.0.0.1:5000;`.
3. Set in `.env`: `WEB_ADMIN_HTTPS=1` and `WEB_ADMIN_HSTS=1`.
4. Run the Flask app bound to localhost only: `WEB_ADMIN_HOST=127.0.0.1` (so only the proxy can reach it).
5. For real production, run with a WSGI server instead of the dev server: `pip install waitress` then `waitress-serve --port=5000 app:app`.

**Recommended hardened .env for internet-facing use:**
```
WEB_ADMIN_HTTPS=1
WEB_ADMIN_HSTS=1
WEB_ADMIN_FORCE_2FA=1
WEB_ADMIN_IDLE_MIN=15
WEB_ADMIN_RATE_LIMIT=120
WEB_ADMIN_IP_ALLOWLIST=<your home/office IP>
WEB_ADMIN_SECRET=<a long random string>   # keep fixed so you stay logged in
```
