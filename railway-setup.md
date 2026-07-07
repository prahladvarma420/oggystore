# OGGY Store - Full Railway Setup (bot + web admin + Postgres)

This hosts EVERYTHING on Railway in one project. You do NOT need a VPS.

```
One Railway project
 |-- Postgres        (managed database)
 |-- bot             (always-on worker  -> python bot.py)
 |-- web-admin       (public website     -> gunicorn ... web_admin.app:app)
```
Both app services deploy from the SAME GitHub repo and share the same Postgres.
The code auto-switches: if DATABASE_URL is set it uses Postgres, otherwise SQLite.

> Cost: Railway is NOT free long-term. Hobby plan is ~$5/month (usage-based).
> An always-on bot + web + database will use credit. Fine for a real store.

==============================================================================
## PART A - Prerequisites (one time)

### A1. Install Git (Windows)
Open Command Prompt and run:
```
winget install --id Git.Git -e
```
Close and reopen Command Prompt, then verify:
```
git --version
```
(If winget is unavailable, get the installer from git-scm.com/download/win and
click Next through the defaults.)

### A2. A GitHub account
Create one at github.com if you do not have it. Then create a new PRIVATE repo
(e.g. "oggystore"). Do not add a README/license (keep it empty).

==============================================================================
## PART B - Push the code to GitHub

From your project folder (the one with bot.py):
```
cd C:\path\to\oggystore_bot_latest
git init
git add .
git status
```
IMPORTANT: in the git status list you must NOT see `.env` or `shopbot.db`.
The included .gitignore already excludes them. If you see them, stop and fix
.gitignore before continuing (those files contain your secrets and user data).

Then:
```
git commit -m "oggystore"
git branch -M main
git remote add origin https://github.com/<your-username>/oggystore.git
git push -u origin main
```

==============================================================================
## PART C - Create the Railway project + Postgres

1. Go to railway.com -> New Project -> Deploy from GitHub repo -> pick your repo.
   This creates your first service (we will make it the web admin).
2. In the project, click New -> Database -> Add PostgreSQL.
   Railway provisions it and gives a DATABASE_URL you reference as:
        ${{Postgres.DATABASE_URL}}

==============================================================================
## PART D - Configure the WEB ADMIN service

Open the service that was created from your repo.

**Settings:**
- Rename it to: web-admin
- Deploy -> Custom Start Command:
  ```
  gunicorn --workers 1 --threads 8 --timeout 120 -b 0.0.0.0:$PORT web_admin.app:app
  ```
- Networking -> Generate Domain  (this is your public https URL).
  Do NOT set a PORT variable yourself - Railway provides $PORT automatically.

**Variables (Settings -> Variables):**
```
DATABASE_URL        = ${{Postgres.DATABASE_URL}}
DATA_ENCRYPTION_KEY = <same value the bot uses>      <-- must match the bot
WEB_ADMIN_SECRET    = <a long random string>
WEB_ADMIN_USERNAME  = <your panel username>
WEB_ADMIN_PASSWORD  = <your panel password>
BOT_TOKEN           = <your bot token>               (lets the panel send msgs)
FORCE_2FA           = 1                               (optional)
```

==============================================================================
## PART E - Add the BOT service (same repo, second service)

1. In the project: New -> GitHub Repo -> pick the SAME repo again.
2. Settings -> rename it: bot
3. Settings -> Deploy -> Custom Start Command:
   ```
   python bot.py
   ```
4. Networking: no domain needed (it is a background worker, not a website).
5. Variables -> add ALL of the bot's values:
   ```
   DATABASE_URL          = ${{Postgres.DATABASE_URL}}
   BOT_TOKEN             = <token>
   ADMIN_ID             = <your telegram id>
   ADMIN_USERNAME       = <...>
   OWNER_USERNAME       = <...>
   USDT_BEP20_ADDRESS   = <...>
   TIMEZONE             = Asia/Kolkata
   DATA_ENCRYPTION_KEY  = <same as web-admin>
   BACKUP_ENCRYPTION_KEY = <...>
   ```
   (You do NOT need TELEGRAM_PROXY on Railway - leave it out unless needed.)

==============================================================================
## PART F - Load your existing data into Railway Postgres (run ONCE)

Railway's Postgres starts empty. Copy your data in from your PC:

1. In Railway, open the Postgres service -> Connect/Variables -> copy the
   PUBLIC connection URL. It looks like:
        postgresql://postgres:...@maglev.proxy.rlwy.net:PORT/railway
   Do NOT use the `*.railway.internal` URL from your PC (that only works
   inside Railway). The ${{Postgres.DATABASE_URL}} reference inside the services automatically
   resolves to the internal URL - which is exactly what you want for them.

2. On your PC, in your project folder:
   ```
   set DATABASE_URL=<the PUBLIC railway url>
   python tools\migrate_sqlite_to_postgres.py
   ```
   (If you re-run this it safely wipes and reloads from your local shopbot.db.
    You need your shopbot.db present in the folder to run it.)

3. Confirm it prints real row counts (products, users, etc.).

==============================================================================
## PART G - CRITICAL: run the bot in only ONE place

The bot uses Telegram long-polling and Telegram allows only ONE poller per bot
token. Before/after deploying to Railway, STOP the bot everywhere else (your
VPS, your PC). Two running bots = "Conflict: terminated by other getUpdates"
errors and missed messages.

The web admin only SENDS messages via the token (no polling), so it never
conflicts with the bot.

==============================================================================
## PART H - Smoke test (do this before relying on it)

- [ ] Open the web-admin domain -> log in.
- [ ] Dashboard shows your real products / users / orders.
- [ ] Add a product in the web admin -> it appears in the Telegram bot.
- [ ] Add stock in the bot -> the count updates in the web admin.
- [ ] Open an order -> the delivered code decrypts and shows correctly.
- [ ] Reply to a ticket in the web admin -> the user receives it in Telegram.
- [ ] Send a small test broadcast -> it sends.

==============================================================================
## NOTES & TROUBLESHOOTING

- DATA_ENCRYPTION_KEY MUST be identical on the bot and web-admin services, or
  the panel cannot decrypt stock / wallets / tickets the bot encrypted.
- Railway auto-redeploys both services every time you `git push`.
- Custom domain: web-admin -> Settings -> Networking -> Custom Domain.
- "Conflict: terminated by other getUpdates" -> the bot is running in two
  places. Keep only the Railway bot service running.
- Web service crashes on boot -> check it bound `0.0.0.0:$PORT` (the start
  command above does) and that DATABASE_URL is set.
- ROTATE any database password that was shared in chat once it works.
- ROLLBACK: remove DATABASE_URL and the app falls back to local SQLite.
