"""
Reseller Center — Telegram side (Phase 3).

Scope of this step: the APPLICATION flow + the center shell.
  /reseller command, the conditional main-menu button (🤝 Reseller Center —
  visible only to users who already have a reseller record) and the Profile
  row (🤝 Become a Reseller) all land here.

Status routing (Phase-1 resellers.status, linked by telegram_user_id — the
existing Telegram identity, no duplicate user system):
  no record -> apply intro      pending -> "pending review" view
  active    -> Reseller Center  suspended/closed -> status view, no functions

Approval is NEVER automatic and never happens in the bot: submission creates
status='pending' and notifies the Telegram admin to review in Web Admin.
"""

import html

from telegram import InlineKeyboardMarkup
from utils.button_styles import AutoStyledButton as InlineKeyboardButton

import database.reseller_db as rdb
from database.db import ensure_user, is_blocked
from config import ADMIN_ID
from utils.states import user_state
from utils.ui import edit_or_send
from utils.error_logger import log_error
from keyboards.menus import back_home, back_button

# In-memory application drafts — same convention as utils.states.user_state.
_apply_drafts = {}  # uid -> shop/brand name


def build_reseller_home(uid: int):
    """Pure view builder: returns (text, markup) for the reseller entry point.
    Routes purely by the caller's OWN reseller record (IDOR-safe by design —
    no ids are ever taken from callback data)."""
    res = rdb.get_reseller_by_telegram(uid)

    if not res:
        text = (
            "🤝 <b>Become a Reseller</b>\n\n"
            "Sell our catalog through your own shop or app via our API.\n\n"
            "<b>What resellers get:</b>\n"
            "• API access with your own keys\n"
            "• Wholesale pricing rules\n"
            "• A prepaid reseller wallet\n"
            "• Product access control\n\n"
            "Applications are reviewed manually — tap below to apply."
        )
        return text, InlineKeyboardMarkup([
            [InlineKeyboardButton("🚀 Apply Now", callback_data="res_apply")],
            [back_button("home")],
        ])

    pub = res["public_id"]
    st = res["status"]

    if st == "pending":
        text = (
            "⏳ <b>Application pending review</b>\n\n"
            f"🆔 <code>{pub}</code>\n"
            f"📅 Submitted: {res['created']}\n\n"
            "Our team reviews applications manually. You'll be notified right "
            "here as soon as a decision is made — no action needed from you."
        )
        return text, InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh status", callback_data="res_center")],
            [back_button("home")],
        ])

    if st == "suspended":
        text = (
            "⏸ <b>Reseller account suspended</b>\n\n"
            f"🆔 <code>{pub}</code>\n\n"
            "Reseller functions are currently disabled for this account. "
            "If you believe this is a mistake, please contact support."
        )
        return text, InlineKeyboardMarkup([
            [InlineKeyboardButton("🆘 Contact Support", callback_data="support_ticket")],
            [back_button("home")],
        ])

    if st == "closed":
        text = (
            "❌ <b>Reseller application closed</b>\n\n"
            f"🆔 <code>{pub}</code>\n\n"
            "Your application was not approved, or the reseller account was "
            "closed. You can contact support for details."
        )
        return text, InlineKeyboardMarkup([
            [InlineKeyboardButton("🆘 Contact Support", callback_data="support_ticket")],
            [back_button("home")],
        ])

    # active -> the Reseller Center (menu + info + balance; the remaining
    # sections are wired but intentionally show "coming soon" for now)
    bal = rdb.from_cents(rdb.get_wallet_balance_cents(res["id"]))
    keys = len(rdb.list_api_keys(res["id"]))
    text = (
        "🤝 <b>Reseller Center</b>\n\n"
        f"🏪 <b>{html.escape(res['name'] or '—')}</b>\n"
        f"🆔 <code>{pub}</code>\n"
        + (f"👤 @{html.escape(res['username'])}\n" if res.get("username") else "")
        + "📊 Status: <b>✅ Active</b>\n"
        f"📅 Member since: {res['created']}\n"
        f"💰 Balance: <b>${bal}</b> USDT\n"
        f"🔑 API keys: <b>{keys}</b>\n\n"
        "Choose a section:"
    )
    return text, InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Balance", callback_data="res_wallet"),
         InlineKeyboardButton("🔑 API Keys", callback_data="res_keys")],
        [InlineKeyboardButton("📦 Products", callback_data="res_soon"),
         InlineKeyboardButton("💵 Pricing", callback_data="res_soon")],
        [InlineKeyboardButton("📊 Statistics", callback_data="res_soon"),
         InlineKeyboardButton("🔔 Webhooks", callback_data="res_webhooks")],
        [InlineKeyboardButton("📚 API Docs", callback_data="res_api_docs"),
         InlineKeyboardButton("⚙️ Settings", callback_data="res_soon")],
        [back_button("home")],
    ])


def get_active_reseller(uid: int):
    """Server-side gate for EVERY protected reseller feature.

    Hidden buttons are never the boundary: this re-reads the caller's OWN
    reseller record fresh from the database on every access and returns it
    only when status == 'active'. Pending/suspended/closed/blocked resellers
    (and users with no record) get None -> the status view, never a feature.
    """
    if is_blocked(uid):
        return None
    res = rdb.get_reseller_by_telegram(uid)
    if not res or res["status"] != "active":
        return None
    return res


def build_reseller_wallet(res: dict):
    """💰 Balance view (read-only). Top-ups stay admin-side in Web Admin."""
    bal = rdb.from_cents(rdb.get_wallet_balance_cents(res["id"]))
    text = (
        "💰 <b>Reseller Wallet</b>\n\n"
        f"Balance: <b>${bal}</b> USDT\n\n"
        "Top-ups and adjustments are handled by our team — "
        "contact support to fund your wallet."
    )
    return text, InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data="res_wallet")],
        [InlineKeyboardButton("◀️ Reseller Center", callback_data="res_center")],
        [back_button("home")],
    ])


MAX_ACTIVE_KEYS = 10  # sane cap per reseller (abuse control)


def _env_label(env: str) -> str:
    return "🚀 production" if env == "production" else "🧪 test"


def build_reseller_keys(res: dict):
    """🔑 Key list: identifier / environment / status / created / last used.
    Contains NO secret material by construction — only the hash is ever
    stored, so the plaintext cannot appear here (or anywhere) after creation."""
    keys = rdb.list_api_keys(res["id"], include_revoked=True)
    active = [k for k in keys if k["status"] == "active"]
    lines = [f"🔑 <b>API Keys</b>  ({len(active)} active)\n"]
    if not keys:
        lines.append("No API keys yet — create your first one below.")
    for k in keys:
        status = "✅ active" if k["status"] == "active" else "❌ revoked"
        last_used = k["last_used_at"] or "never"
        lines.append(
            f"<code>{k['key_id']}</code> · {_env_label(k['environment'])} · {status}\n"
            f"   created {k['created']} · last used {last_used}"
        )
    rows = []
    if len(active) < MAX_ACTIVE_KEYS:
        rows.append([InlineKeyboardButton("➕ Create API Key", callback_data="res_keys_new")])
    for k in active:
        rows.append([InlineKeyboardButton(f"🚫 Revoke {k['key_id']}", callback_data=f"res_rev_{k['key_id']}"),
                     InlineKeyboardButton("🔄 Rotate", callback_data=f"res_rotate_{k['key_id']}")])
    rows.append([InlineKeyboardButton("◀️ Reseller Center", callback_data="res_center")])
    rows.append([back_button("home")])
    return "\n\n".join(lines), InlineKeyboardMarkup(rows)


def build_key_env_picker():
    text = (
        "➕ <b>Create API Key</b>\n\n"
        "Choose the environment:\n\n"
        "🧪 <b>Test</b> — build and verify your integration safely.\n"
        "🚀 <b>Production</b> — live selling with real orders."
    )
    return text, InlineKeyboardMarkup([
        [InlineKeyboardButton("🧪 Test Key", callback_data="res_keys_create_test")],
        [InlineKeyboardButton("🚀 Production Key", callback_data="res_keys_create_live")],
        [InlineKeyboardButton("◀️ API Keys", callback_data="res_keys")],
    ])


def build_key_reveal(key_id: str, secret: str, env: str, done_cb: str = "res_keys_saved"):
    """The ONLY place the complete secret ever appears. The confirm button is
    deliberately the ONLY way forward — leaving this screen scrubs it."""
    text = (
        "🔐 <b>New API key created</b>\n\n"
        "API Key ID:\n"
        f"<code>{key_id}</code>\n\n"
        "API Secret:\n"
        f"<code>{secret}</code>\n\n"
        "⚠️ <b>Save this secret now. It will not be shown again.</b>\n\n"
        "Tap the button below only after you have stored it somewhere safe — "
        "this message will be scrubbed and the secret gone forever."
    )
    return text, InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ I saved it — hide the secret forever", callback_data=done_cb)],
    ])


def build_key_saved():
    return (
        "✅ <b>Saved.</b> The secret has been scrubbed from this message and "
        "can never be retrieved again.\n\n"
        "Manage your keys anytime from 🔑 API Keys.",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("🔑 Back to API Keys", callback_data="res_keys")],
            [back_button("home")],
        ]),
    )


def build_revoke_confirm(key: dict):
    text = (
        f"🚫 <b>Revoke <code>{key['key_id']}</code>?</b>\n\n"
        f"Environment: {_env_label(key['environment'])}\n"
        f"Created: {key['created']}\n\n"
        "This is <b>permanent</b> — any integration using this key stops "
        "working immediately."
    )
    return text, InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, revoke it forever", callback_data=f"res_revok_yes_{key['key_id']}")],
        [InlineKeyboardButton("◀️ Cancel", callback_data="res_keys")],
    ])


def build_rotate_confirm(key: dict):
    """Safe rotation explainer: the replacement is generated FIRST, the old
    key dies only when the reseller revokes it — zero-downtime rotation."""
    text = (
        f"🔄 <b>Rotate <code>{key['key_id']}</code>?</b>\n\n"
        f"Environment: {_env_label(key['environment'])}\n\n"
        "A brand-new key is generated in the same environment. "
        "The old key keeps working until YOU revoke it:\n\n"
        "1️⃣ New key is created and shown once\n"
        "2️⃣ You update your integration\n"
        "3️⃣ You revoke the old key\n"
    )
    return text, InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Create the replacement key", callback_data=f"res_rotate_go_{key['key_id']}")],
        [InlineKeyboardButton("◀️ Cancel", callback_data="res_keys")],
    ])


MAX_WEBHOOKS = 5


def build_reseller_webhooks(res: dict):
    hooks = rdb.list_webhooks(res["id"])
    lines = [f"🔔 <b>Webhooks</b>  ({len(hooks)}/{MAX_WEBHOOKS})\n"]
    if not hooks:
        lines.append("No webhook configured. Add an HTTPS endpoint below.")
    for hook in hooks:
        status = "✅ enabled" if hook["enabled"] else "⏸ disabled"
        lines.append(
            f"<b>#{hook['id']}</b> · {status}\n"
            f"<code>{html.escape(hook['url'])}</code>"
        )
    rows = []
    if len(hooks) < MAX_WEBHOOKS:
        rows.append([InlineKeyboardButton("➕ Add Webhook", callback_data="res_wh_add")])
    for hook in hooks:
        wid = hook["id"]
        action = "⏸ Disable" if hook["enabled"] else "▶️ Enable"
        rows.append([
            InlineKeyboardButton(f"✏️ Edit #{wid}", callback_data=f"res_wh_edit_{wid}"),
            InlineKeyboardButton(action, callback_data=f"res_wh_toggle_{wid}"),
        ])
        rows.append([
            InlineKeyboardButton(f"🗑 Remove #{wid}", callback_data=f"res_wh_remove_{wid}")
        ])
    rows.append([InlineKeyboardButton("◀️ Reseller Center", callback_data="res_center")])
    rows.append([back_button("home")])
    return "\n\n".join(lines), InlineKeyboardMarkup(rows)


def build_webhook_secret_reveal(hook: dict, secret: str):
    text = (
        "🔐 <b>Webhook created</b>\n\n"
        f"Endpoint:\n<code>{html.escape(hook['url'])}</code>\n\n"
        "Signing secret:\n"
        f"<code>{secret}</code>\n\n"
        "⚠️ <b>Save it now.</b> It is shown only once and will be used to "
        "verify signed webhook events when delivery is introduced."
    )
    return text, InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ I saved it — hide secret", callback_data="res_wh_saved")
    ]])


def build_reseller_api_docs():
    """Safe Phase-3 developer overview; it deliberately documents no route or
    response until the public reseller API gateway actually exists."""
    text = (
        "📚 <b>Reseller Developer Center</b>\n\n"
        "🚧 <b>API availability: Coming soon</b>\n"
        "No public reseller API endpoints are available yet.\n\n"
        "<b>API version</b>\n"
        "Coming soon\n\n"
        "<b>Base API path</b>\n"
        "Coming soon\n\n"
        "<b>Authentication concept</b>\n"
        "Requests will use your API Key ID together with its private API secret. "
        "The authentication gateway and request format are <b>Coming soon</b>.\n\n"
        "<b>Available environments</b>\n"
        "🧪 <b>Test</b> — intended for developing and checking integrations "
        "without live selling. API processing is <b>Coming soon</b>.\n\n"
        "🚀 <b>Production</b> — intended for live integrations after the reseller "
        "API launches. API processing is <b>Coming soon</b>.\n\n"
        "<b>Basic security requirements</b>\n"
        "• Keep API secrets on your secure server only\n"
        "• Never put secrets in URLs, logs, chat messages, or frontend code\n"
        "• Use HTTPS for every future API request\n"
        "• Keep test and production credentials separate\n"
        "• Rotate exposed credentials and revoke old keys immediately\n\n"
        "Endpoint reference and examples: <b>Coming soon</b>"
    )
    return text, InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑 Manage API Keys", callback_data="res_keys")],
        [InlineKeyboardButton("◀️ Reseller Center", callback_data="res_center")],
        [back_button("home")],
    ])


async def reseller_command(update, context):
    """/reseller — application entry point + center access."""
    if update.message is None or update.message.from_user is None:
        return
    user = update.message.from_user
    uid = user.id
    if is_blocked(uid):  # a bot-level ban beats any reseller status
        try:
            await update.message.reply_text(
                "🚫 <b>Access Denied</b>\n\nYour account is blocked from using this bot.",
                parse_mode="HTML")
        except Exception:
            pass
        return
    try:
        ensure_user(user)
        text, markup = build_reseller_home(uid)
        await update.message.reply_text(
            text, reply_markup=markup, parse_mode="HTML",
            disable_web_page_preview=True)
    except Exception as exc:
        log_error("reseller command failed", exc)


async def handle_reseller_callback(q, uid, data, context) -> bool:
    """All res_* callbacks. Returns True when handled."""
    try:
        if data == "res_center":
            text, markup = build_reseller_home(uid)
            await edit_or_send(q, text, markup)
            return True

        if data == "res_apply":
            if rdb.get_reseller_by_telegram(uid):
                # already has a record -> never start a second application
                text, markup = build_reseller_home(uid)
                await edit_or_send(q, text, markup)
                return True
            user_state[uid] = "res_apply:name"
            await edit_or_send(
                q,
                "📝 <b>Reseller application</b>\n\n"
                "Send me your <b>shop / brand name</b> (2-60 characters).\n\n"
                "This name appears on your reseller profile.",
                InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Cancel", callback_data="res_apply_cancel")
                ]]),
            )
            return True

        if data == "res_apply_cancel":
            _apply_drafts.pop(uid, None)
            user_state.pop(uid, None)
            text, markup = build_reseller_home(uid)
            await edit_or_send(q, text, markup)
            return True

        if data == "res_apply_confirm":
            name = _apply_drafts.get(uid)
            if not name or rdb.get_reseller_by_telegram(uid):
                # no draft, or a record appeared meanwhile -> just show home
                _apply_drafts.pop(uid, None)
                text, markup = build_reseller_home(uid)
                await edit_or_send(q, text, markup)
                return True
            user = q.from_user
            username = getattr(user, "username", "") or ""
            try:
                rec = rdb.create_reseller(name, telegram_user_id=uid, username=username)
            except Exception:
                # UNIQUE(telegram_user_id) backstop: applied in a parallel session
                _apply_drafts.pop(uid, None)
                text, markup = build_reseller_home(uid)
                await edit_or_send(q, text, markup)
                return True
            _apply_drafts.pop(uid, None)
            user_state.pop(uid, None)
            try:
                rdb.log_security_event(
                    rec["id"], "application_submitted",
                    description=f"telegram application by {uid} (@{username}) name={name!r}")
            except Exception:
                pass
            try:
                bot = q.message.get_bot()
                await bot.send_message(
                    chat_id=ADMIN_ID,
                    text=("🤝 <b>New reseller application</b>\n\n"
                          f"🏪 <b>{html.escape(name)}</b>\n"
                          f"👤 <code>{uid}</code> (@{html.escape(username) or 'no-username'})\n"
                          f"🆔 <code>{rec['public_id']}</code>\n\n"
                          "Review it in Web Admin → 🤝 Resellers."),
                    parse_mode="HTML")
            except Exception as exc:
                log_error("reseller application admin notify failed", exc)
            await edit_or_send(
                q,
                "✅ <b>Application submitted!</b>\n\n"
                f"🏪 <b>{html.escape(name)}</b>\n"
                f"🆔 <code>{rec['public_id']}</code>\n"
                "📊 Status: <b>⏳ Pending review</b>\n\n"
                "Our team will review your application and you'll be notified here. "
                "Open 🤝 Reseller Center (or /reseller) anytime to check your status.",
                back_home())
            return True

        if data == "res_keys":
            res = get_active_reseller(uid)   # server-side status re-check
            if not res:
                text, markup = build_reseller_home(uid)
                await edit_or_send(q, text, markup)
                return True
            text, markup = build_reseller_keys(res)
            await edit_or_send(q, text, markup)
            return True

        if data == "res_keys_new":
            res = get_active_reseller(uid)
            if not res:
                text, markup = build_reseller_home(uid)
                await edit_or_send(q, text, markup)
                return True
            if len(rdb.list_api_keys(res["id"])) >= MAX_ACTIVE_KEYS:
                await q.answer(f"⚠️ Maximum {MAX_ACTIVE_KEYS} active keys — revoke one first.",
                               show_alert=True)
                return True
            text, markup = build_key_env_picker()
            await edit_or_send(q, text, markup)
            return True

        if data in ("res_keys_create_test", "res_keys_create_live"):
            res = get_active_reseller(uid)
            if not res:
                text, markup = build_reseller_home(uid)
                await edit_or_send(q, text, markup)
                return True
            env = "test" if data.endswith("_test") else "production"
            if len(rdb.list_api_keys(res["id"])) >= MAX_ACTIVE_KEYS:
                await q.answer(f"⚠️ Maximum {MAX_ACTIVE_KEYS} active keys — revoke one first.",
                               show_alert=True)
                return True
            try:
                key_id, secret = rdb.create_api_key(res["id"], env)
            except Exception as exc:
                # NEVER include the secret (or any key material) in logs/errors
                log_error("reseller key creation failed", exc, extra=f"uid={uid} env={env}")
                await edit_or_send(q, "❌ Could not create a key right now. Please try again.",
                                   build_reseller_keys(res)[1])
                return True
            try:
                rdb.log_security_event(res["id"], "api_key_created",
                                       description=f"key {key_id} ({env}) created via telegram by {uid}")
            except Exception:
                pass
            text, markup = build_key_reveal(key_id, secret, env)
            await edit_or_send(q, text, markup)
            return True

        if data == "res_keys_saved":
            # Scrub the reveal message — the secret is never retrievable again.
            if not get_active_reseller(uid):
                text, markup = build_reseller_home(uid)
                await edit_or_send(q, text, markup)
                return True
            text, markup = build_key_saved()
            await edit_or_send(q, text, markup)
            return True

        if data.startswith("res_revok_yes_"):
            res = get_active_reseller(uid)
            if not res:
                text, markup = build_reseller_home(uid)
                await edit_or_send(q, text, markup)
                return True
            kid = data[len("res_revok_yes_"):]
            key = rdb.get_api_key(kid)
            if not key or key["reseller_id"] != res["id"]:
                await q.answer("❌ Key not found.", show_alert=True)
                return True
            if rdb.revoke_api_key_for_reseller(res["id"], kid):
                try:
                    rdb.log_security_event(res["id"], "api_key_revoked",
                                           description=f"key {kid} revoked via telegram by {uid}")
                except Exception:
                    pass
                await q.answer("🚫 Key revoked — it stopped working instantly.", show_alert=True)
            else:
                await q.answer("Already revoked.", show_alert=True)
            text, markup = build_reseller_keys(res)
            await edit_or_send(q, text, markup)
            return True

        if data.startswith("res_rev_"):
            res = get_active_reseller(uid)
            if not res:
                text, markup = build_reseller_home(uid)
                await edit_or_send(q, text, markup)
                return True
            kid = data[len("res_rev_"):]
            key = rdb.get_api_key(kid)
            if not key or key["reseller_id"] != res["id"]:
                await q.answer("❌ Key not found.", show_alert=True)
                return True
            if key["status"] != "active":
                await q.answer("Already revoked.", show_alert=True)
                text, markup = build_reseller_keys(res)
                await edit_or_send(q, text, markup)
                return True
            text, markup = build_revoke_confirm(key)
            await edit_or_send(q, text, markup)
            return True

        if data.startswith("res_rotate_go_"):
            res = get_active_reseller(uid)
            if not res:
                text, markup = build_reseller_home(uid)
                await edit_or_send(q, text, markup)
                return True
            old_kid = data[len("res_rotate_go_"):]
            old = rdb.get_api_key(old_kid)
            if not old or old["reseller_id"] != res["id"] or old["status"] != "active":
                await q.answer("❌ Key not found.", show_alert=True)
                return True
            if len(rdb.list_api_keys(res["id"])) >= MAX_ACTIVE_KEYS:
                await q.answer(f"⚠️ Maximum {MAX_ACTIVE_KEYS} active keys — revoke one first.",
                               show_alert=True)
                return True
            try:
                key_id, secret = rdb.create_api_key(res["id"], old["environment"])
            except Exception as exc:
                # NEVER include key material in logs/errors
                log_error("reseller key rotation failed", exc, extra=f"uid={uid}")
                await edit_or_send(q, "❌ Could not rotate right now. Please try again.",
                                   build_reseller_keys(res)[1])
                return True
            try:
                rdb.log_security_event(res["id"], "api_key_rotated",
                                       description=f"key {old_kid} rotated -> {key_id} via telegram by {uid}")
            except Exception:
                pass
            text, markup = build_key_reveal(key_id, secret, old["environment"],
                                            done_cb=f"res_rot_done_{old_kid}")
            await edit_or_send(q, text, markup)
            return True

        if data.startswith("res_rot_done_"):
            res = get_active_reseller(uid)
            if not res:
                text, markup = build_reseller_home(uid)
                await edit_or_send(q, text, markup)
                return True
            old_kid = data[len("res_rot_done_"):]
            old = rdb.get_api_key(old_kid)
            if not old or old["reseller_id"] != res["id"]:
                await q.answer("❌ Key not found.", show_alert=True)
                return True
            rows = []
            if old["status"] == "active":
                rows.append([InlineKeyboardButton(f"🚫 Revoke old key {old_kid} now",
                                                  callback_data=f"res_revok_yes_{old_kid}")])
            rows.append([InlineKeyboardButton("🔑 Back to API Keys", callback_data="res_keys")])
            await edit_or_send(
                q,
                "✅ <b>New key saved.</b> The secret has been scrubbed and can never "
                "be retrieved again.\n\n"
                f"Final step: revoke the old key <code>{old_kid}</code> once your "
                "integration uses the new one.",
                InlineKeyboardMarkup(rows))
            return True

        if data.startswith("res_rotate_"):
            res = get_active_reseller(uid)
            if not res:
                text, markup = build_reseller_home(uid)
                await edit_or_send(q, text, markup)
                return True
            kid = data[len("res_rotate_"):]
            key = rdb.get_api_key(kid)
            if not key or key["reseller_id"] != res["id"] or key["status"] != "active":
                await q.answer("❌ Key not found.", show_alert=True)
                return True
            text, markup = build_rotate_confirm(key)
            await edit_or_send(q, text, markup)
            return True

        if data == "res_webhooks":
            res = get_active_reseller(uid)
            if not res:
                text, markup = build_reseller_home(uid)
                await edit_or_send(q, text, markup)
                return True
            user_state.pop(uid, None)
            text, markup = build_reseller_webhooks(res)
            await edit_or_send(q, text, markup)
            return True

        if data == "res_wh_add":
            res = get_active_reseller(uid)
            if not res:
                text, markup = build_reseller_home(uid)
                await edit_or_send(q, text, markup)
                return True
            if len(rdb.list_webhooks(res["id"])) >= MAX_WEBHOOKS:
                await q.answer(f"Maximum {MAX_WEBHOOKS} webhooks reached.", show_alert=True)
                return True
            user_state[uid] = "res_webhook:add"
            await edit_or_send(
                q,
                "➕ <b>Add Webhook</b>\n\nSend the complete <b>HTTPS</b> endpoint URL.\n\n"
                "Local/private addresses, credentials and URL fragments are not allowed.",
                InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Cancel", callback_data="res_webhooks")
                ]]),
            )
            return True

        if data.startswith("res_wh_edit_"):
            res = get_active_reseller(uid)
            if not res:
                text, markup = build_reseller_home(uid)
                await edit_or_send(q, text, markup)
                return True
            raw_id = data[len("res_wh_edit_"):]
            if not raw_id.isdigit():
                await q.answer("Invalid webhook.", show_alert=True)
                return True
            hook = rdb.get_webhook_for_reseller(res["id"], int(raw_id))
            if not hook:
                await q.answer("Webhook not found.", show_alert=True)
                return True
            user_state[uid] = f"res_webhook:edit:{hook['id']}"
            await edit_or_send(
                q,
                "✏️ <b>Update Webhook</b>\n\n"
                f"Current URL:\n<code>{html.escape(hook['url'])}</code>\n\n"
                "Send the new complete HTTPS URL. The existing signing secret is retained.",
                InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Cancel", callback_data="res_webhooks")
                ]]),
            )
            return True

        if data.startswith("res_wh_toggle_"):
            res = get_active_reseller(uid)
            if not res:
                text, markup = build_reseller_home(uid)
                await edit_or_send(q, text, markup)
                return True
            raw_id = data[len("res_wh_toggle_"):]
            hook = (rdb.get_webhook_for_reseller(res["id"], int(raw_id))
                    if raw_id.isdigit() else None)
            if not hook:
                await q.answer("Webhook not found.", show_alert=True)
                return True
            enabled = not bool(hook["enabled"])
            if not rdb.set_webhook_enabled_for_reseller(res["id"], hook["id"], enabled):
                await q.answer("Webhook not found.", show_alert=True)
                return True
            try:
                rdb.log_security_event(
                    res["id"], "webhook_enabled" if enabled else "webhook_disabled",
                    description=f"webhook {hook['id']} changed via telegram by {uid}")
            except Exception:
                pass
            await q.answer("Webhook enabled." if enabled else "Webhook disabled.",
                           show_alert=True)
            text, markup = build_reseller_webhooks(res)
            await edit_or_send(q, text, markup)
            return True

        if data.startswith("res_wh_remove_yes_"):
            res = get_active_reseller(uid)
            if not res:
                text, markup = build_reseller_home(uid)
                await edit_or_send(q, text, markup)
                return True
            raw_id = data[len("res_wh_remove_yes_"):]
            hook = (rdb.get_webhook_for_reseller(res["id"], int(raw_id))
                    if raw_id.isdigit() else None)
            if not hook or not rdb.delete_webhook_for_reseller(res["id"], hook["id"]):
                await q.answer("Webhook not found.", show_alert=True)
                return True
            try:
                rdb.log_security_event(
                    res["id"], "webhook_removed",
                    description=f"webhook {hook['id']} removed via telegram by {uid}")
            except Exception:
                pass
            await q.answer("Webhook removed.", show_alert=True)
            text, markup = build_reseller_webhooks(res)
            await edit_or_send(q, text, markup)
            return True

        if data.startswith("res_wh_remove_"):
            res = get_active_reseller(uid)
            if not res:
                text, markup = build_reseller_home(uid)
                await edit_or_send(q, text, markup)
                return True
            raw_id = data[len("res_wh_remove_"):]
            hook = (rdb.get_webhook_for_reseller(res["id"], int(raw_id))
                    if raw_id.isdigit() else None)
            if not hook:
                await q.answer("Webhook not found.", show_alert=True)
                return True
            await edit_or_send(
                q,
                "🗑 <b>Remove this webhook?</b>\n\n"
                f"<code>{html.escape(hook['url'])}</code>\n\n"
                "Its encrypted signing secret will also be deleted. This cannot be undone.",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("🗑 Yes, remove", callback_data=f"res_wh_remove_yes_{hook['id']}")],
                    [InlineKeyboardButton("◀️ Cancel", callback_data="res_webhooks")],
                ]),
            )
            return True

        if data == "res_wh_saved":
            res = get_active_reseller(uid)
            if not res:
                text, markup = build_reseller_home(uid)
                await edit_or_send(q, text, markup)
                return True
            text, markup = build_reseller_webhooks(res)
            await edit_or_send(q, text, markup)
            return True

        if data == "res_api_docs":
            if not get_active_reseller(uid):
                text, markup = build_reseller_home(uid)
                await edit_or_send(q, text, markup)
                return True
            text, markup = build_reseller_api_docs()
            await edit_or_send(q, text, markup)
            return True

        if data == "res_wallet":
            res = get_active_reseller(uid)   # server-side status re-check
            if not res:
                text, markup = build_reseller_home(uid)
                await edit_or_send(q, text, markup)
                return True
            text, markup = build_reseller_wallet(res)
            await edit_or_send(q, text, markup)
            return True

        if data == "res_soon":
            if not get_active_reseller(uid):  # server-side status re-check
                # non-active callers never even reach the coming-soon surface
                text, markup = build_reseller_home(uid)
                await edit_or_send(q, text, markup)
                return True
            await q.answer(
                "🚧 Coming soon — this section is being finalized.",
                show_alert=True)
            return True

        return False
    except Exception as exc:
        log_error("reseller callback failed", exc, extra=f"data={data} uid={uid}")
        try:
            await edit_or_send(q, "❌ Something went wrong. Please try again.", back_home())
        except Exception:
            pass
        return True


async def handle_reseller_message(update, uid, text, state) -> bool:
    """Consumes res_* conversation states. Returns True when handled."""
    try:
        if state == "res_apply:name":
            name = text.strip()
            if not (2 <= len(name) <= 60):
                await update.message.reply_text(
                    "❌ Name must be 2-60 characters. Please try again "
                    "(or tap ❌ Cancel on the message above).",
                    parse_mode="HTML")
                return True
            _apply_drafts[uid] = name
            user_state[uid] = "res_apply:confirm"
            await update.message.reply_text(
                "📋 <b>Confirm your application</b>\n\n"
                f"🏪 Shop/brand: <b>{html.escape(name)}</b>\n\n"
                "Review is manual — there is no automatic approval.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Submit Application", callback_data="res_apply_confirm")],
                    [InlineKeyboardButton("❌ Cancel", callback_data="res_apply_cancel")],
                ]))
            return True

        if state == "res_apply:confirm":
            await update.message.reply_text(
                "Use the buttons above to submit or cancel your application. 👆",
                parse_mode="HTML")
            return True

        if state == "res_webhook:add":
            res = get_active_reseller(uid)
            if not res:
                user_state.pop(uid, None)
                home_text, home_markup = build_reseller_home(uid)
                await update.message.reply_text(
                    home_text, parse_mode="HTML", reply_markup=home_markup,
                    disable_web_page_preview=True)
                return True
            if len(rdb.list_webhooks(res["id"])) >= MAX_WEBHOOKS:
                user_state.pop(uid, None)
                await update.message.reply_text(
                    f"❌ Maximum {MAX_WEBHOOKS} webhooks reached.", parse_mode="HTML")
                return True
            try:
                normalized = rdb.validate_webhook_url(text)
            except ValueError as exc:
                await update.message.reply_text(
                    f"❌ <b>Invalid webhook URL:</b> {html.escape(str(exc))}\n\n"
                    "Send a valid public HTTPS URL or cancel from the previous message.",
                    parse_mode="HTML", disable_web_page_preview=True)
                return True
            try:
                hook, secret = rdb.create_webhook(res["id"], normalized)
            except Exception:
                # The signing secret is never attached to errors or logs.
                await update.message.reply_text(
                    "❌ Could not add this webhook. It may already be configured.",
                    parse_mode="HTML")
                return True
            user_state.pop(uid, None)
            try:
                rdb.log_security_event(
                    res["id"], "webhook_created",
                    description=f"webhook {hook['id']} created via telegram by {uid}")
            except Exception:
                pass
            reveal_text, reveal_markup = build_webhook_secret_reveal(hook, secret)
            await update.message.reply_text(
                reveal_text, parse_mode="HTML", reply_markup=reveal_markup,
                disable_web_page_preview=True)
            return True

        if state.startswith("res_webhook:edit:"):
            res = get_active_reseller(uid)
            if not res:
                user_state.pop(uid, None)
                home_text, home_markup = build_reseller_home(uid)
                await update.message.reply_text(
                    home_text, parse_mode="HTML", reply_markup=home_markup,
                    disable_web_page_preview=True)
                return True
            raw_id = state.rsplit(":", 1)[-1]
            hook = (rdb.get_webhook_for_reseller(res["id"], int(raw_id))
                    if raw_id.isdigit() else None)
            if not hook:
                user_state.pop(uid, None)
                await update.message.reply_text("❌ Webhook not found.", parse_mode="HTML")
                return True
            try:
                normalized = rdb.validate_webhook_url(text)
            except ValueError as exc:
                await update.message.reply_text(
                    f"❌ <b>Invalid webhook URL:</b> {html.escape(str(exc))}\n\n"
                    "Send a valid public HTTPS URL or cancel from the previous message.",
                    parse_mode="HTML", disable_web_page_preview=True)
                return True
            try:
                updated = rdb.update_webhook_url(res["id"], hook["id"], normalized)
            except Exception:
                await update.message.reply_text(
                    "❌ Could not update this webhook. The URL may already be configured.",
                    parse_mode="HTML")
                return True
            user_state.pop(uid, None)
            if not updated:
                await update.message.reply_text("❌ Webhook not found.", parse_mode="HTML")
                return True
            try:
                rdb.log_security_event(
                    res["id"], "webhook_updated",
                    description=f"webhook {hook['id']} URL updated via telegram by {uid}")
            except Exception:
                pass
            list_text, list_markup = build_reseller_webhooks(res)
            await update.message.reply_text(
                "✅ <b>Webhook updated.</b>\n\n" + list_text,
                parse_mode="HTML", reply_markup=list_markup,
                disable_web_page_preview=True)
            return True

        return False
    except Exception as exc:
        log_error("reseller message flow failed", exc, extra=f"state={state} uid={uid}")
        return True
