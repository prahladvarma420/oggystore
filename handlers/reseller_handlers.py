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
from utils.states import user_state, RES_TOPUP_AMOUNT, RES_TOPUP_PAYMENT
from utils.ui import edit_or_send
from utils.error_logger import log_error
from keyboards.menus import back_home, back_button

# In-memory application drafts — same convention as utils.states.user_state.
_apply_drafts = {}  # uid -> shop/brand name
_topup_drafts = {}  # uid -> selected allowlisted method; no payment proof


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

    # Active -> supported Reseller Center actions only.
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
        [InlineKeyboardButton("📦 Products", callback_data="res_products:page:1"),
         InlineKeyboardButton("💵 Pricing", callback_data="res_pricing:page:1")],
        [InlineKeyboardButton("📊 Statistics", callback_data="res_stats"),
         InlineKeyboardButton("🔔 Webhooks", callback_data="res_webhooks")],
        [InlineKeyboardButton("📚 API Docs", callback_data="res_api_docs")],
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
    """Ledger-derived balance with independently verified self-service deposits."""
    bal = rdb.from_cents(rdb.get_wallet_balance_cents(res["id"]))
    text = (
        "💰 <b>Reseller Wallet</b>\n\n"
        f"Balance: <b>${bal}</b> USDT\n\n"
        "Top up using a supported payment method. "
        "Your customer balance stays separate."
    )
    return text, InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Top up", callback_data="res_topup")],
        [InlineKeyboardButton("🔄 Refresh", callback_data="res_wallet")],
        [InlineKeyboardButton("◀️ Reseller Center", callback_data="res_center")],
        [back_button("home")],
    ])


def build_reseller_topup_methods():
    from payments.reseller_deposits import rails
    methods=rails()
    text="➕ <b>Top up reseller wallet</b>\n\nChoose a payment method. Pay only the server-generated invoice; never send payment proof in chat."
    if not methods:text += "\n\nNo payment method is currently configured. Please contact support."
    from payments.unique_bep20 import requested
    if requested():
        text += '\n\nPermanent BEP20 receiving requires verified custody setup; an unavailable screen does not provide payment instructions.'
    return text, InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data="res_topup_method_"+method)] for method,label in methods
    ]+[[InlineKeyboardButton("◀️ Wallet",callback_data="res_wallet")]])


def build_reseller_topup_invoice(saved):
    """Reuse unchanged customer copy; substitute the persisted destination."""
    from payments import deposit_handlers as customer
    from utils.timezone import now,TZ,TZ_LABEL
    from datetime import datetime
    method=saved['method']
    text=customer._invoice_text_for(method,float(saved['amount']),float(saved['expected_amount']),int(saved['id']),html.escape(str(saved['memo'] or '')))
    current={'bep20':customer.USDT_BEP20_ADDRESS,'trc20':customer.USDT_TRC20_ADDRESS,'binance':customer.BINANCE_PAY_ID}[method]
    text=text.replace('<code>'+str(current)+'</code>','<code>'+html.escape(str(saved['address']))+'</code>')
    remaining=max(0,int((datetime.strptime(saved['expires_at'],'%Y-%m-%d %H:%M:%S').replace(tzinfo=TZ)-now()).total_seconds()))
    text += "\n\n💼 <b>Reseller wallet deposit</b>\n"+f"Expires: <code>{html.escape(saved['expires_at'])} {html.escape(TZ_LABEL)}</code>\nRemaining: <b>{remaining//3600}h {(remaining%3600)//60}m</b>\nDo not pay an expired invoice."
    return text,InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh instructions",callback_data=f"res_topup_show_{saved['id']}")],
        [InlineKeyboardButton("❌ Cancel payment session",callback_data=f"res_topup_cancel_{saved['id']}")],
        [InlineKeyboardButton("◀️ Wallet",callback_data="res_wallet")],
    ])


def reseller_topup_qr(saved):
    """Existing qrcode dependency; QR contains saved address/Pay ID only.

    Binance Pay QR is an ID helper, not an official payment/request token.
    Exact amount and required memo must still be entered from the invoice.
    """
    import io,qrcode
    output=io.BytesIO();qrcode.make(str(saved['address'])).save(output,format='PNG');output.seek(0);output.name='reseller-payment.png'
    return output


async def _show_reseller_topup(q,uid,saved,*,qr=False):
    # Caller fetched saved session through the service immediately beforehand.
    user_state[uid]=RES_TOPUP_PAYMENT
    text,markup=build_reseller_topup_invoice(saved)
    await edit_or_send(q,text,markup)
    if qr:
        try:
            await q.message.reply_photo(photo=reseller_topup_qr(saved),caption="Destination / Pay ID only. Verify the network, exact amount and required memo in your invoice.")
        except Exception:
            pass  # Text payment instructions remain available.


async def _handle_reseller_topup_callback(q,uid,data):
    from payments import reseller_deposits as deposits
    try:
        deposits.active(uid)  # every callback, including navigation and retry
        from payments import unique_bep20 as permanent
        if data == 'res_topup_unique_bep20' or (data == 'res_topup_method_bep20' and permanent.requested()):
            try:
                cfg, _adapter = permanent.runtime()
                view = permanent.account_view(uid, cfg)
                text, markup = build_reseller_permanent_bep20(view, cfg)
                _topup_drafts.pop(uid, None); user_state.pop(uid, None)
                await edit_or_send(q, text, markup)
                if data == 'res_topup_method_bep20':
                    try:
                        await q.message.reply_photo(photo=reseller_topup_qr(view['assignment']),
                            caption='Permanent BNB Smart Chain receiving address. Verify the token and network above.')
                    except Exception:
                        pass  # Text/copy instructions remain available; no payment mutation.
            except permanent.DepositUnavailable:
                await edit_or_send(q, 'Permanent BEP20 deposits are unavailable pending verified custody setup. No new receiving address has been enabled.',
                    InlineKeyboardMarkup([[InlineKeyboardButton('◀️ Wallet', callback_data='res_wallet')]]))
            return True
        if data=='res_topup':
            saved=deposits.session(uid)
            if saved:await _show_reseller_topup(q,uid,saved,qr=True)
            else:
                _topup_drafts.pop(uid,None);user_state.pop(uid,None)
                text,markup=build_reseller_topup_methods();await edit_or_send(q,text,markup)
        elif data.startswith('res_topup_method_'):
            method=data.removeprefix('res_topup_method_')
            if method not in dict(deposits.rails()):raise deposits.DepositError('Unsupported or unavailable payment method.')
            saved=deposits.session(uid)
            if saved:
                if saved['method']!=method:raise deposits.DepositError('Cancel the existing session before choosing a different method.')
                await _show_reseller_topup(q,uid,saved)
            else:
                _topup_drafts[uid]=method;user_state[uid]=RES_TOPUP_AMOUNT
                await edit_or_send(q,'➕ <b>Reseller wallet top-up</b>\n\nSend an amount from <b>0.10 to 5000.00 USDT</b>, with at most two decimals. Example: <code>10.00</code>',InlineKeyboardMarkup([[InlineKeyboardButton('❌ Back to wallet',callback_data='res_topup_back')]]))
        elif data=='res_topup_back':
            _topup_drafts.pop(uid,None);user_state.pop(uid,None)
            text,markup=build_reseller_wallet(deposits.active(uid));await edit_or_send(q,text,markup)
        elif data.startswith('res_topup_show_'):
            try:depid=int(data.removeprefix('res_topup_show_'))
            except ValueError:raise deposits.DepositError('Deposit not found.') from None
            await _show_reseller_topup(q,uid,deposits.session(uid,depid))
        elif data.startswith('res_topup_cancel_'):
            try:depid=int(data.removeprefix('res_topup_cancel_'))
            except ValueError:raise deposits.DepositError('Deposit not found.') from None
            deposits.cancel_session(uid,depid);_topup_drafts.pop(uid,None)
            await edit_or_send(q,'✅ Payment session cancelled. Do not send funds to this invoice. A payment already received cannot be reversed here.',InlineKeyboardMarkup([[InlineKeyboardButton('◀️ Wallet',callback_data='res_wallet')]]))
        else:raise deposits.DepositError('Unknown top-up action.')
    except deposits.DepositError as exc:
        _topup_drafts.pop(uid,None);user_state.pop(uid,None)
        await edit_or_send(q,'❌ '+html.escape(str(exc)),InlineKeyboardMarkup([[InlineKeyboardButton('◀️ Wallet',callback_data='res_wallet')]]))
    return True


async def _handle_reseller_topup_message(update,uid,text,state):
    from payments import reseller_deposits as deposits
    try:
        deposits.active(uid)
        if state==RES_TOPUP_AMOUNT:
            method=_topup_drafts.get(uid)
            if method is None:raise deposits.DepositError('Reopen Top up and select a payment method.')
            saved=deposits.create_session(uid,method,text.strip())
            _topup_drafts.pop(uid,None);user_state[uid]=RES_TOPUP_PAYMENT
            invoice,markup=build_reseller_topup_invoice(saved)
            await update.message.reply_text(invoice,parse_mode='HTML',reply_markup=markup,disable_web_page_preview=True)
            try:
                await update.message.reply_photo(photo=reseller_topup_qr(saved),caption='Destination / Pay ID only. Use the invoice network, exact amount and memo.')
            except Exception:pass
        else:
            saved=deposits.session(uid)
            if not saved:raise deposits.DepositError('No payable session remains. Reopen your wallet.')
            invoice,markup=build_reseller_topup_invoice(saved)
            await update.message.reply_text(invoice,parse_mode='HTML',reply_markup=markup,disable_web_page_preview=True)
    except deposits.DepositError as exc:
        # Invalid amounts can be corrected; denied/expired access cannot persist.
        if not deposits.active_or_none(uid) or state!=RES_TOPUP_AMOUNT or uid not in _topup_drafts:
            _topup_drafts.pop(uid,None);user_state.pop(uid,None)
        await update.message.reply_text('❌ '+html.escape(str(exc)),parse_mode='HTML')
    return True


CATALOG_PAGE_SIZE = 8


class CatalogViewError(ValueError):
    """Safe catalog navigation/initialization message, not an engine error."""
    pass


def _catalog_page_number(value):
    """Untrusted, bounded navigation hint. Never a product/access assertion."""
    import re
    if isinstance(value, bool) or not re.fullmatch(r"-?[0-9]{1,10}", str(value)):
        raise CatalogViewError("Invalid page. Reopen Products or Pricing.")
    return max(1, int(value))


def read_reseller_telegram_catalog(uid, view="products"):
    """Fresh authorized service reads; no API-key impersonation, HTTP or writes.

    my-products is a management inventory containing inactive mapping records.
    Products displays its currently enabled/access-enabled/master-active subset.
    Pricing preserves pricing API's broader active owner-access scope.
    """
    from reseller_api.services.product_import import read_product_mappings_for_reseller
    from reseller_api.services.pricing import read_product_prices_for_reseller
    res=get_active_reseller(uid)
    if not res:raise PermissionError("Active reseller access required.")
    if view not in ("products", "pricing"):raise CatalogViewError("Invalid catalog view.")
    if view=="pricing":
        prices=read_product_prices_for_reseller(res['id'],allocate_missing_ids=False)
        items=[{key:price[key] for key in ('product_id','name','price','currency')} for price in prices]
    else:
        mappings=read_product_mappings_for_reseller(res['id'])
        items=[]
        for mapping in mappings:
            if not (mapping['enabled'] and mapping['master_active'] and mapping['access_enabled']):continue
            # The my-products service already uses the same integer pricing
            # resolver as pricing API. No second mapper or price calculation.
            items.append({'product_id':mapping['product_id'],
                'name':mapping['custom_name'] or mapping['master_name'],
                'price':mapping['price'],'currency':mapping['currency'],
                'availability':mapping['availability'],'enabled':mapping['enabled']})
    from reseller_api.services.product_import import _PUBLIC_ID_RE
    if any(not _PUBLIC_ID_RE.fullmatch(str(item['product_id'])) for item in items):
        raise CatalogViewError('Product identifiers need owner/API initialization before this view is available.')
    # Revalidate mutable Telegram authorization immediately before hand-off.
    if not get_active_reseller(uid):raise PermissionError("Active reseller access required.")
    return items


def _catalog_page(items, requested_page):
    page=_catalog_page_number(requested_page)
    pages=max(1,(len(items)+CATALOG_PAGE_SIZE-1)//CATALOG_PAGE_SIZE)
    page=min(page,pages)
    return items[(page-1)*CATALOG_PAGE_SIZE:page*CATALOG_PAGE_SIZE],page,pages


def _catalog_navigation(kind, page, pages):
    buttons=[]
    if page>1:buttons.append(InlineKeyboardButton("◀️ Previous",callback_data=f"res_{kind}:page:{page-1}"))
    if page<pages:buttons.append(InlineKeyboardButton("Next ▶️",callback_data=f"res_{kind}:page:{page+1}"))
    rows=[buttons] if buttons else []
    rows += [[InlineKeyboardButton("🔄 Refresh",callback_data=f"res_{kind}:page:{page}")],
        [InlineKeyboardButton("📚 API Docs",callback_data="res_api_docs")],
        [InlineKeyboardButton("◀️ Reseller Center",callback_data="res_center")]]
    return rows


def _catalog_name_html(name, budget=220):
    """Display-only ellipsis at whole-character/entity boundaries.

    Service records retain full API names. Bound rendered message length even
    for legacy master names without limits and heavily HTML-escaped input.
    """
    value=str(name or '—');escaped=html.escape(value)
    if len(escaped)<=budget:return escaped
    parts=[];length=0
    for character in value:
        piece=html.escape(character)
        if length+len(piece)>budget-1:break
        parts.append(piece);length+=len(piece)
    return ''.join(parts)+'…'


def build_reseller_products(items, requested_page=1, notice=None):
    """Pure display over validated current safe records; at most eight rows."""
    current,page,pages=_catalog_page(items,requested_page)
    text="📦 <b>Your products</b>\n"+f"Page {page}/{pages} · {len(items)} products\n\n"
    if notice:text+=html.escape(str(notice))+"\n\n"
    rows=[]
    if not items:
        text+="No products imported yet. Import products through the reseller API to get started. See 📚 API Docs for instructions."
    for product in current:
        name=_catalog_name_html(product['name']);pid=html.escape(str(product['product_id']))
        available="In stock" if product['availability']=='in_stock' else "Out of stock"
        text+=f"<b>{name}</b>\n<code>{pid}</code>\nPrice: <b>{html.escape(str(product['price']))} {html.escape(str(product['currency']))}</b>\nAvailability: {available} · Enabled: ✅\n\n"
        rows.append([InlineKeyboardButton("📦 "+str(product['product_id']),callback_data=f"res_product:detail:{product['product_id']}:{page}")])
    return text,InlineKeyboardMarkup(rows+_catalog_navigation('products',page,pages))


def build_reseller_product_detail(product, return_page=1):
    """Pure detail card; absence/authorization is handled before calling."""
    page=_catalog_page_number(return_page)
    available="In stock" if product['availability']=='in_stock' else "Out of stock"
    text=("📦 <b>Product details</b>\n\n"+f"<b>{_catalog_name_html(product['name'],2500)}</b>\n"
        +f"PRD ID: <code>{html.escape(str(product['product_id']))}</code>\n"
        +f"Availability: {available}\nPrice: <b>{html.escape(str(product['price']))} {html.escape(str(product['currency']))}</b>")
    return text,InlineKeyboardMarkup([[InlineKeyboardButton('◀️ Products',callback_data=f'res_products:page:{page}')],
        [InlineKeyboardButton('🔄 Refresh',callback_data=f"res_product:detail:{product['product_id']}:{page}")]])


def build_reseller_pricing(items, requested_page=1):
    current,page,pages=_catalog_page(items,requested_page)
    text="💵 <b>Your pricing</b>\n"+f"Page {page}/{pages} · {len(items)} products\n\nPrices are managed by the owner and reflect your current effective reseller prices.\n\n"
    if not items:text+="No products imported yet. Import products through the reseller API to get started. See 📚 API Docs for instructions."
    for product in current:
        text+=f"<b>{_catalog_name_html(product['name'])}</b>\n<code>{html.escape(str(product['product_id']))}</code> — <b>{html.escape(str(product['price']))} {html.escape(str(product['currency']))}</b>\n\n"
    return text,InlineKeyboardMarkup(_catalog_navigation('pricing',page,pages))


async def _handle_reseller_catalog_callback(q,uid,data):
    import re
    from reseller_api.services.pricing import PricingError
    try:
        # Check even malformed callbacks: navigation never establishes access.
        if not get_active_reseller(uid):raise PermissionError('Active reseller access required.')
        match=re.fullmatch(r'res_(products|pricing):page:(-?[0-9]{1,10})',data)
        detail=re.fullmatch(r'res_product:detail:(PRD-[A-HJ-NP-Z2-9]{12}):(-?[0-9]{1,10})',data)
        if match:
            kind,page=match.groups();page=_catalog_page_number(page)
            items=read_reseller_telegram_catalog(uid,kind)
            text,markup=(build_reseller_products(items,page) if kind=='products' else build_reseller_pricing(items,page))
        elif detail:
            pid,page=detail.groups();page=_catalog_page_number(page)
            items=read_reseller_telegram_catalog(uid,'products')
            product=next((item for item in items if item['product_id']==pid),None)
            if product:text,markup=build_reseller_product_detail(product,page)
            else:text,markup=build_reseller_products(items,page,'Product is no longer accessible. The current catalog is shown below.')
        else:raise CatalogViewError('Invalid catalog link. Reopen Products or Pricing.')
        await edit_or_send(q,text,markup)
    except (PermissionError,CatalogViewError,PricingError) as exc:
        await edit_or_send(q,'❌ '+html.escape(exc.message if isinstance(exc,PricingError) else str(exc)),InlineKeyboardMarkup([[InlineKeyboardButton('◀️ Reseller Center',callback_data='res_center')]]))
    except Exception:
        # Do not reveal internal master prices/rule values through engine errors.
        log_error("Reseller catalog read failed",extra="category=catalog_read_error")
        await edit_or_send(q,'❌ Catalog is temporarily unavailable. Please try again.',InlineKeyboardMarkup([[InlineKeyboardButton('◀️ Reseller Center',callback_data='res_center')]]))
    return True


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


# Canonical public resource metadata used by rendering and route-coverage tests.
# A focused quick-start, not a second router or a list of private admin actions.
API_DOC_RESOURCES = (
    ("Products", (("GET", "/api/v1/products"), ("GET", "/api/v1/products/{product_id}"), ("POST", "/api/v1/products/{product_id}/import"))),
    ("My products", (("GET", "/api/v1/my-products"), ("GET", "/api/v1/my-products/{product_id}"), ("PATCH", "/api/v1/my-products/{product_id}"), ("DELETE", "/api/v1/my-products/{product_id}"))),
    ("Pricing", (("GET", "/api/v1/pricing"), ("GET", "/api/v1/pricing/{product_id}"))),
    ("Orders", (("POST", "/api/v1/orders"), ("GET", "/api/v1/orders"), ("GET", "/api/v1/orders/{order_id}"))),
    ("Quote", (("POST", "/api/v1/orders/quote"),)),
    ("Delivery", (("GET", "/api/v1/orders/{order_id}/delivery"),)),
    ("Balance", (("GET", "/api/v1/balance"), ("GET", "/api/v1/wallet/summary"))),
    ("Transactions", (("GET", "/api/v1/transactions"),)),
    ("Refunds", (("GET", "/api/v1/refunds"), ("GET", "/api/v1/refunds/{refund_id}"))),
    ("Invoices", (("GET", "/api/v1/invoices"), ("GET", "/api/v1/invoices/{invoice_id}"), ("GET", "/api/v1/invoices/{invoice_id}/document"))),
    ("Statements", (("GET", "/api/v1/statements"), ("GET", "/api/v1/statements/{statement_id}"), ("GET", "/api/v1/statements/{statement_id}/document"))),
    ("Usage", (("GET", "/api/v1/usage"),)),
    ("Analytics", (("GET", "/api/v1/analytics/sales"),)),
    ("Exports", (("GET", "/api/v1/exports/orders.csv"),)),
    ("Webhooks", (("GET", "/api/v1/webhooks"), ("POST", "/api/v1/webhooks"), ("GET", "/api/v1/webhooks/analytics"), ("GET", "/api/v1/webhooks/{webhook_id}"), ("PATCH", "/api/v1/webhooks/{webhook_id}"), ("DELETE", "/api/v1/webhooks/{webhook_id}"), ("POST", "/api/v1/webhooks/{webhook_id}/test"), ("POST", "/api/v1/webhooks/{webhook_id}/rotate-secret"), ("POST", "/api/v1/webhooks/events/{outbox_id}/retry"), ("GET", "/api/v1/webhooks/{webhook_id}/deliveries"), ("POST", "/api/v1/webhooks/{webhook_id}/deliveries/{delivery_id}/retry"))),
)


def public_api_docs_base_url(raw):
    """Presentation-only validation; no network requests or inferred hostname.

    The operator must provide the verified public HTTPS URL. Syntax checks
    reject obvious private/internal destinations, but do not attest to DNS or
    deployment reachability. Unsafe raw configuration is never echoed.
    """
    import ipaddress
    import re
    from urllib.parse import urlsplit
    if not isinstance(raw, str) or not raw or len(raw) > 240:
        return None
    if any(c.isspace() or ord(c) < 32 for c in raw):
        return None
    try:
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").lower().rstrip(".")
        if (parsed.scheme != "https" or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.port not in (None, 443)
                or parsed.path.rstrip("/") != "/api/v1"):
            return None
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if (not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host)
                    or "." not in host or ".." in host
                    or host.endswith((".localhost", ".local", ".internal", ".invalid", ".test", ".example", ".lan", ".home", ".svc", ".cluster.local"))
                    or host == "localhost" or re.fullmatch(r"[0-9.]+", host)
                    or re.fullmatch(r"0x[0-9a-f]+",host)):
                return None
        else:
            if not address.is_global:
                return None
        return raw.rstrip("/")
    except (ValueError, TypeError):
        return None


def read_reseller_telegram_statistics(uid):
    """Fresh authorized reads through the four authoritative API aggregators.

    No HTTP/API-key impersonation and no parallel SQL. Only allowlisted numeric
    summary fields leave this seam; endpoint/key/product breakdowns never do.
    Read failures are unavailable, not fabricated zeroes. Reads are sequential,
    as with separate API requests, rather than a promised snapshot transaction.
    """
    reseller = get_active_reseller(uid)
    if not reseller:
        raise PermissionError("Active reseller access required.")
    rid = int(reseller["id"])
    sources = (
        ("usage", lambda: rdb.get_reseller_api_usage(rid), (
            "requests_today", "requests_total", "orders_today", "orders_this_month",
            "quota_daily", "quota_monthly", "quota_daily_remaining", "quota_monthly_remaining")),
        ("sales", lambda: rdb.get_reseller_sales_analytics(rid, include_breakdowns=False), ("gross_sales", "net_sales", "currency")),
        ("wallet", lambda: rdb.get_wallet_financial_summary(rid), ("balance", "currency")),
        ("webhooks", lambda: rdb.get_reseller_webhook_analytics(rid), (
            "window_days", "events_total", "delivered", "pending", "dead_lettered",
            "delivery_attempts", "attempts_succeeded", "attempts_failed", "webhooks_configured", "webhooks_enabled")),
    )
    output = {}
    for name, read, fields in sources:
        try:
            source = read()
            output[name] = {field: source[field] for field in fields if field in source}
        except Exception:
            # Do not pass exception text/tracebacks or source values to logs.
            log_error("Reseller statistics read unavailable", extra=f"category={name}_read_error")
            output[name] = None
    current = get_active_reseller(uid)
    if not current or int(current["id"]) != rid:
        raise PermissionError("Active reseller access required.")
    return output


def _statistics_number(value, *, money=False):
    """Bounded output from validated numeric summaries; missing != zero."""
    from decimal import Decimal, InvalidOperation
    if value is None or isinstance(value, bool):
        return "Unavailable"
    try:
        if len(str(value)) > 40:
            return "Unavailable"
        amount = Decimal(str(value))
        if not amount.is_finite() or abs(amount) >= Decimal("1e24"):
            return "Unavailable"
        if money:
            # Services already produce their canonical two-decimal strings.
            if amount != amount.quantize(Decimal("0.01")):
                return "Unavailable"
            return format(amount, ".2f")
        if amount < 0 or amount != amount.to_integral_value():
            return "Unavailable"
        return str(int(amount))
    except (InvalidOperation, ValueError, TypeError):
        return "Unavailable"


def build_reseller_statistics(metrics):
    """Pure bounded HTML summary. No endpoint lists or misleading health grade."""
    u = metrics.get("usage") or {}; sales = metrics.get("sales") or {}
    wallet = metrics.get("wallet") or {}; wh = metrics.get("webhooks") or {}
    n = _statistics_number
    def quota(scope, used):
        if (metrics.get("usage") is None or "quota_" + scope not in u
                or "quota_" + scope + "_remaining" not in u):
            return "Unavailable"
        limit = u.get("quota_" + scope)
        remaining = u.get("quota_" + scope + "_remaining")
        return f"used {n(u.get(used))}; remaining {'Unlimited' if limit is None else n(remaining)}"
    def currency(data):
        return "USDT" if data.get("currency") == "USDT" else "(currency unavailable)"
    text = (
        "📊 <b>Reseller Statistics</b>\n\n"
        f"<b>API requests</b>\nToday: {n(u.get('requests_today'))}\nTotal: {n(u.get('requests_total'))}\n\n"
        f"<b>Orders / quota</b>\nOrders today: {n(u.get('orders_today'))}\nOrders this month: {n(u.get('orders_this_month'))}\n"
        f"Daily quota: {quota('daily', 'orders_today')}\nMonthly quota: {quota('monthly', 'orders_this_month')}\n"
        "Usage/quota follows the API's shop-clock boundaries.\n\n"
        f"<b>Sales — rolling 30 days</b>\nGross sales: {n(sales.get('gross_sales'), money=True)} {currency(sales)}\n"
        f"Net sales: {n(sales.get('net_sales'), money=True)} {currency(sales)}\n"
        "Same default window and refund rules as Analytics.\n\n"
        f"<b>Wallet</b>\nBalance: {n(wallet.get('balance'), money=True)} {currency(wallet)}\n\n"
        "<b>Webhook health — rolling 30 days</b>\n"
    )
    if metrics.get("webhooks") is None:
        text += "Unavailable\n"
    else:
        text += (
            f"Configured: {n(wh.get('webhooks_configured'))}; enabled: {n(wh.get('webhooks_enabled'))}\n"
            f"Events: {n(wh.get('events_total'))}; delivered: {n(wh.get('delivered'))}\n"
            f"Pending: {n(wh.get('pending'))}; dead-lettered: {n(wh.get('dead_lettered'))}\n"
            f"Attempts: {n(wh.get('delivery_attempts'))}; succeeded: {n(wh.get('attempts_succeeded'))}; failed: {n(wh.get('attempts_failed'))}\n"
        )
        if wh.get("webhooks_configured") == 0:
            text += "No webhooks configured.\n"
        elif wh.get("events_total") == 0 and wh.get("delivery_attempts") == 0:
            text += "No recent delivery activity; health not yet established.\n"
        text += "Failed attempts may later succeed; dead-lettered means exhausted/terminal events.\n"
    text += "\nRefresh for current values; separate service reads are not one snapshot."
    return text, InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data="res_stats")],
        [InlineKeyboardButton("◀️ Reseller Center", callback_data="res_center")],
        [back_button("home")],
    ])


def build_reseller_api_docs(page=1, public_base_url=None):
    """Pure four-page quick-start, using explicitly configured public v1 URL."""
    page = min(max(_catalog_page_number(page), 1), 4)
    base = public_api_docs_base_url(public_base_url)
    heading = f"📚 <b>Reseller API v1 — {page}/4</b>\n\n"
    if page == 1:
        body = (
            (f"<b>Public base URL</b>\n<code>{html.escape(base)}</code>\n\n" if base else
             "<b>Public base URL unavailable</b>\nThe owner must configure a verified public v1 URL. No host is inferred.\n\n")
            + "<b>Authentication</b>\n<code>Authorization: Bearer &lt;key_id&gt;.&lt;secret&gt;</code>\n"
            "Keep credentials private on your server. Never post them in Telegram chats, support messages, source control or logs.\n\n"
            "<b>Environments</b>\nThe API supports test (sandbox) and production. Use keys for the matching environment. "
            "Each deployment must use its own database; the startup environment marker refuses mismatched databases. "
            "Orders, stock, wallet balances, delivery records and ledger state are then separate.\n"
            "Sandbox is a real order pipeline, not a mock: the owner must provide test products and balances. "
            "Database separation does NOT automatically isolate external suppliers, payment addresses, Telegram or webhook destinations. "
            "The owner must configure safe test integrations; do not send real funds or use live suppliers for testing.\n\n"
            "Use the resource pages for public methods and paths."
        )
    elif page in (2, 3):
        subset = API_DOC_RESOURCES[:8] if page == 2 else API_DOC_RESOURCES[8:]
        body = "<b>Public resources</b>\nPaths below include /api/v1.\n\n"
        for category, operations in subset:
            body += f"<b>{html.escape(category)}</b>\n"
            for method, path in operations:
                body += f"<code>{method} {html.escape(path)}</code>\n"
            body += "\n"
        body += "Read-only financial resources do not permit withdrawals. See RESELLER_API.md for all filters, subresources and examples."
    else:
        body = (
            "<b>Order idempotency</b>\nSend client_order_id on POST /api/v1/orders (printable ASCII, max 128). "
            "It is unique per reseller account. A committed replay returns HTTP 200 with idempotent: true and the original order, "
            "even if order parameters differ; it never changes the buyer email or charges twice. Use a fresh ID per logical order. "
            "A conflicting internal wallet reference may return 409 idempotency_conflict; this is not a blanket payload-mismatch rejection for committed replays. "
            "Without an ID, each request is new. No equivalent guarantee is implied for unrelated operations.\n\n"
            "<b>Error envelope</b>\n<code>{\"ok\": false, \"error\": {\"code\": \"…\", \"message\": \"…\"}, \"request_id\": \"…\"}</code>\n"
            "400 validation; 401 missing/invalid credentials or wrong environment; 402 insufficient balance; "
            "403 denied; 404 missing/inaccessible resource; 409 conflict; 413 body too large; 415 non-JSON; "
            "422 stock/business rule or missing required buyer email; 429 rate/quota; 500 sanitized internal error; 503 unavailable.\n\n"
            "<b>Rate limiting</b>\nReseller-wide request buckets and endpoint categories apply across keys; limits depend on configuration. "
            "After request-rate evaluation: RateLimit-Limit is the applicable limit; RateLimit-Remaining is available units; "
            "RateLimit-Reset is seconds until reset. Rate denials include Retry-After (seconds); category denials also identify RateLimit-Category. "
            "Authentication/early errors need not carry rate headers. Respect 429 and avoid immediate retries.\n\n"
            "<b>Full reference</b>\nRESELLER_API.md is the project API guide; request the current copy from the owner. "
            "No verified hosted documentation URL is configured. This guide names no private service or filesystem location."
        )
    rows = []
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("◀️ Previous", callback_data=f"res_api_docs:page:{page-1}"))
    if page < 4:
        nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"res_api_docs:page:{page+1}"))
    if nav:
        rows.append(nav)
    rows += [[InlineKeyboardButton("🔑 Manage API Keys", callback_data="res_keys")],
             [InlineKeyboardButton("◀️ Reseller Center", callback_data="res_center")], [back_button("home")]]
    return heading + body, InlineKeyboardMarkup(rows)


async def _handle_reseller_stats_docs_callback(q, uid, data):
    try:
        if not get_active_reseller(uid):
            raise PermissionError("Active reseller access required.")
        if data == "res_stats":
            text, markup = build_reseller_statistics(read_reseller_telegram_statistics(uid))
        else:
            import re
            match = re.fullmatch(r"res_api_docs:page:(-?[0-9]{1,10})", data)
            if data != "res_api_docs" and not match:
                raise CatalogViewError("Invalid documentation link.")
            import config
            text, markup = build_reseller_api_docs(match.group(1) if match else 1,
                                                   config.RESELLER_API_PUBLIC_BASE_URL)
        # Mutable active/blocked state is checked again before message delivery.
        if not get_active_reseller(uid):
            raise PermissionError("Active reseller access required.")
    except (PermissionError, CatalogViewError) as exc:
        text = "❌ " + html.escape(str(exc))
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Reseller Center", callback_data="res_center")]])
    except Exception:
        log_error("Reseller statistics/documentation unavailable", extra="category=stats_docs_read_error")
        text = "❌ This view is temporarily unavailable. Please try again."
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Reseller Center", callback_data="res_center")]])
    await edit_or_send(q, text, markup)
    return True


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
        if data.startswith(("res_products", "res_pricing", "res_product:")):
            return await _handle_reseller_catalog_callback(q,uid,data)

        if data == "res_topup" or data.startswith("res_topup_"):
            return await _handle_reseller_topup_callback(q,uid,data)

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
            # Rotation is exempt from the MAX_ACTIVE_KEYS cap: the old key
            # auto-expires after a 24h grace window (rotate_api_key_for_reseller),
            # so the two-key overlap closes itself even if the user never
            # revokes the old key manually.
            try:
                rotated = rdb.rotate_api_key_for_reseller(res["id"], old_kid)
            except Exception as exc:
                # NEVER include key material in logs/errors
                log_error("reseller key rotation failed", exc, extra=f"uid={uid}")
                await edit_or_send(q, "❌ Could not rotate right now. Please try again.",
                                   build_reseller_keys(res)[1])
                return True
            if not rotated:
                await q.answer("❌ Key not found.", show_alert=True)
                return True
            key_id, secret, old_expires = rotated
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
                "integration uses the new one.\n\n"
                "🕓 Safety net: the old key expires automatically 24 hours after "
                "rotation even if you never revoke it.",
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

        if data == "res_stats" or data == "res_api_docs" or data.startswith("res_api_docs:"):
            return await _handle_reseller_stats_docs_callback(q, uid, data)

        if data == "res_wallet":
            if str(user_state.get(uid,"" )).startswith("res_topup:"):
                user_state.pop(uid,None);_topup_drafts.pop(uid,None)
            res = get_active_reseller(uid)   # server-side status re-check
            if not res:
                text, markup = build_reseller_home(uid)
                await edit_or_send(q, text, markup)
                return True
            text, markup = build_reseller_wallet(res)
            await edit_or_send(q, text, markup)
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
        if str(state).startswith("res_topup:"):
            return await _handle_reseller_topup_message(update,uid,text,state)

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


def build_reseller_permanent_bep20(view, cfg):
    """Pure, bounded rendering; handler/service enforces current reseller identity."""
    from payments.unique_bep20 import exact_text
    from telegram import CopyTextButton
    saved = view['assignment']
    text = ('💼 <b>Permanent reseller BEP20 deposits</b>\n\n'
        'Network: <b>BNB Smart Chain (56)</b>\nToken contract: <code>'+html.escape(cfg.token)+'</code>\n'
        'Your permanent address: <code>'+html.escape(saved['address'])+'</code>\n'
        'Copy the full address above or scan the QR. Send only the supported token on this network.\n'
        'Minimum: <b>'+html.escape(exact_text(cfg.minimum_units))+' USDT</b>\n'
        'Verification requires at least '+str(cfg.confirmations)+' confirmations and custody acceptance.\n'
        'No exact-amount invoice or customer gas deposit is required.\n\n'
        'Spendable wallet: <b>'+html.escape(rdb.from_cents(view['balance_cents']))+' USDT</b>\n'
        'Retained fraction: <b>'+html.escape(exact_text(view['remainder_units']))+' USDT</b>\n'
        'Fractions accumulate exactly until a whole cent can be credited.\n')
    for event in view['events'][:5]:
        state = ('Wallet credited' if event['credit_cents'] else 'Accounted; fraction retained') if event['state']=='accounted' else ('Under review' if event['state']=='review' else 'Awaiting custody acceptance')
        tx=event['tx_hash']
        text += ('\n'+html.escape(exact_text(event['raw_units']))+' USDT — '+state+
                 '\nTX <code>'+html.escape(tx)+'</code> / log '+str(event['log_index'])+
                 '\n<a href="https://bscscan.com/tx/'+html.escape(tx,quote=True)+'">View transaction</a>\n'+
                 ('Accounted: '+html.escape(str(event['accounted_at']))+'\n' if event['accounted_at'] else ''))
    return text, InlineKeyboardMarkup([
        [InlineKeyboardButton('📋 Copy address', copy_text=CopyTextButton(saved['address']))],
        [InlineKeyboardButton('🔄 Refresh',callback_data='res_topup_unique_bep20')],
        [InlineKeyboardButton('◀️ Wallet',callback_data='res_wallet')]])
