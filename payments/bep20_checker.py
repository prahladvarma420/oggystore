import html
import time
from datetime import datetime, timedelta
from decimal import Decimal

from telegram import InlineKeyboardMarkup
from utils.button_styles import AutoStyledButton as InlineKeyboardButton
from web3 import Web3

from utils.error_logger import log_error
from utils.timezone import now, now_text

from utils.crypto import encrypt_text
from utils.premium_emoji import strip_premium
from database.db import cur, conn, add_balance, reserve_stock_items, get_available_stock_count, get_setting, set_setting, get_balance, render_message_template
from config import (
    ADMIN_ID,
    RPC_URL,
    BACKUP_RPC_URLS,
    USDT_BEP20_ADDRESS,
    USDT_BEP20_CONTRACT,
    USDT_DECIMALS,
    DEPOSIT_EXPIRE_HOURS,
    PAYMENT_AMOUNT_TOLERANCE,
    PAYMENT_REQUIRED_CONFIRMATIONS,
    PAYMENT_SCAN_BLOCKS,
)

TRANSFER_TOPIC = "0x" + Web3.keccak(text="Transfer(address,address,uint256)").hex().replace("0x", "")

DEPOSIT_SUCCESS_DEFAULT = (
    "✅ <b>DEPOSIT CONFIRMED</b>\n\n🧾 <b>Invoice:</b> <code>#{invoice_id}</code>\n💵 <b>Paid:</b> <code>{paid} USDT</code>\n💰 <b>Balance Added:</b> <code>{amount} USDT</code>\n💼 <b>New Balance:</b> <code>{balance} USDT</code>\n🔗 <b>TX:</b> {tx}\n\n🛍 <i>You can now buy products from Shop.</i>"
)



def _to_checksum(address: str) -> str:
    return Web3.to_checksum_address(address)


def _topic_address(address: str) -> str:
    address = address.lower().replace("0x", "")
    return "0x" + address.rjust(64, "0")


def _config_ready() -> bool:
    return (
        isinstance(USDT_BEP20_ADDRESS, str)
        and USDT_BEP20_ADDRESS.startswith("0x")
        and len(USDT_BEP20_ADDRESS) == 42
        and isinstance(USDT_BEP20_CONTRACT, str)
        and USDT_BEP20_CONTRACT.startswith("0x")
        and len(USDT_BEP20_CONTRACT) == 42
    )


def _drop_cached_web3(context):
    context.bot_data.pop("web3_rpc", None)
    context.bot_data.pop("web3_rpc_url", None)


def _get_web3(context):
    cached = context.bot_data.get("web3_rpc")
    if cached:
        try:
            if cached.is_connected():
                return cached
        except Exception:
            pass
        _drop_cached_web3(context)

    rpc_urls = [RPC_URL] + list(BACKUP_RPC_URLS or [])
    last_error = None

    for url in rpc_urls:
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 20}))
            # Force a real RPC call: is_connected() alone can miss 403 blocks.
            w3.eth.block_number
            context.bot_data["web3_rpc"] = w3
            context.bot_data["web3_rpc_url"] = url
            return w3
        except Exception as exc:
            last_error = f"{url} -> {exc}"

    raise RuntimeError(f"Could not connect to any BSC RPC. Last error: {last_error}")


def _pending_deposits():
    cutoff = (now() - timedelta(hours=DEPOSIT_EXPIRE_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    cur.execute(
        """
        SELECT id, user_id, amount, unique_amount, checkout_product_id, checkout_quantity, checkout_done
        FROM deposits
        WHERE status='pending' AND (method IS NULL OR method='' OR method='bep20') AND created >= ?
        ORDER BY id ASC
        """,
        (cutoff,),
    )
    return cur.fetchall()


async def _expire_old_deposits(context):
    """Mark stale pending invoices as expired and notify their owners."""
    cutoff = (now() - timedelta(hours=DEPOSIT_EXPIRE_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    cur.execute(
        "SELECT id, user_id, amount FROM deposits WHERE status='pending' "
        "AND (method IS NULL OR method='' OR method='bep20') AND created < ?",
        (cutoff,),
    )
    rows = cur.fetchall()

    for dep_id, dep_uid, amount in rows:
        cur.execute(
            "UPDATE deposits SET status='expired' WHERE id=? AND status='pending'",
            (dep_id,)
        )
        conn.commit()

        try:
            await context.bot.send_message(
                chat_id=dep_uid,
                text=(
                    "⌛ <b>Invoice Expired</b>\n\n"
                    f"🧾 Invoice <b>#{dep_id}</b> for <b>{float(amount):.2f} USDT</b> "
                    "expired without payment.\n\n"
                    "Still want to deposit? Create a fresh invoice below.\n"
                    "⚠️ If you already paid this invoice, contact support."
                ),
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("💰 Create New Invoice", callback_data="deposit")]]
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass


def _not_processed(tx_hash: str) -> bool:
    cur.execute("SELECT 1 FROM processed_txs WHERE tx_hash=?", (tx_hash,))
    return cur.fetchone() is None


def _amount_from_log(log) -> Decimal:
    data = log["data"]
    if hasattr(data, "hex"):
        raw_value = int(data.hex().replace("0x", ""), 16)
    else:
        raw_value = int(str(data).replace("0x", ""), 16)

    return Decimal(raw_value) / (Decimal(10) ** Decimal(USDT_DECIMALS))


def _tx_hash_from_log(log) -> str:
    tx_hash = log.get("transactionHash")
    if hasattr(tx_hash, "hex"):
        return "0x" + tx_hash.hex().replace("0x", "")
    return str(tx_hash)


def _matches_amount(paid: Decimal, expected: float) -> bool:
    diff = abs(paid - Decimal(str(expected)))
    return diff <= Decimal(str(PAYMENT_AMOUNT_TOLERANCE))


# ERC-20 balanceOf(address) selector, used as a fallback verification method
# because free public BSC RPCs sometimes return empty eth_getLogs results.
BALANCE_OF_SELECTOR = "0x70a08231"


def _wallet_usdt_balance(w3: Web3) -> Decimal:
    data = BALANCE_OF_SELECTOR + _topic_address(USDT_BEP20_ADDRESS).replace("0x", "")
    raw = w3.eth.call({"to": _to_checksum(USDT_BEP20_CONTRACT), "data": data})
    if hasattr(raw, "hex"):
        raw_int = int(raw.hex().replace("0x", "") or "0", 16)
    else:
        raw_int = int(str(raw).replace("0x", "") or "0", 16)
    return Decimal(raw_int) / (Decimal(10) ** Decimal(USDT_DECIMALS))


# Public BSC RPCs reject large eth_getLogs ranges with {'code': -32005,
# 'message': 'limit exceeded'}. Scan the window in small chunks instead.
LOG_SCAN_CHUNK_BLOCKS = 450
# Cap catch-up work per cycle; anything left is scanned on the next cycles.
# Kept small: a 12-request burst every 30s is what tripped the free-RPC
# 'limit exceeded' rate limit and disabled log scanning entirely.
MAX_CHUNKS_PER_CYCLE = 6


def _load_last_scanned_block(context) -> int:
    cached = context.bot_data.get("last_scanned_block")
    if cached:
        return int(cached)
    try:
        return int(get_setting("last_scanned_block") or 0)
    except Exception:
        return 0


def _remember_scanned_block(context, block_number: int):
    context.bot_data["last_scanned_block"] = int(block_number)
    try:
        set_setting("last_scanned_block", int(block_number))
    except Exception:
        pass


def _get_recent_incoming_usdt_logs(context, w3: Web3):
    latest_block = w3.eth.block_number
    to_block = max(latest_block - PAYMENT_REQUIRED_CONFIRMATIONS, 0)

    # Resume from the last scanned block so payments are never skipped, but
    # never look further back than PAYMENT_SCAN_BLOCKS behind the chain tip.
    last_scanned = _load_last_scanned_block(context)
    from_block = last_scanned + 1 if last_scanned else 0
    from_block = max(from_block, to_block - PAYMENT_SCAN_BLOCKS, 0)

    if from_block > to_block:
        return [], None

    logs = []
    start = from_block
    chunks = 0
    scanned_to = None
    while start <= to_block and chunks < MAX_CHUNKS_PER_CYCLE:
        end = min(start + LOG_SCAN_CHUNK_BLOCKS - 1, to_block)
        transfer_filter = {
            "fromBlock": start,
            "toBlock": end,
            "address": _to_checksum(USDT_BEP20_CONTRACT),
            "topics": [
                TRANSFER_TOPIC,
                None,
                _topic_address(USDT_BEP20_ADDRESS),
            ],
        }
        logs.extend(w3.eth.get_logs(transfer_filter))
        scanned_to = end
        start = end + 1
        chunks += 1

    return logs, scanned_to




async def _maybe_low_stock_alert(context, product_id: int):
    try:
        # FIX59: delegate to the single canonical low-stock implementation.
        # This function used to treat products.low_stock_notified as a BOOLEAN
        # (0/1), while notify_low_stock() treats the SAME column as an ALERT
        # LEVEL (e.g. 5 or 1). Whichever ran first corrupted the other: once
        # this code wrote 1, notify_low_stock() saw last_notified=1, which is
        # <= every configured level, and went permanently silent.
        # Imported locally to avoid a circular import at module load time.
        from products.product_handlers import notify_low_stock

        cur.execute("SELECT name FROM products WHERE id=?", (product_id,))
        p = cur.fetchone()
        if not p:
            return

        stock = get_available_stock_count(product_id)
        await notify_low_stock(context.bot, product_id, p[0], stock)
    except Exception as exc:
        log_error("Low stock alert failed", exc, extra=f"product={product_id}")


async def _alert_unattributed(context, delta, reason: str):
    """Warn the admin about wallet funds that could not be matched.

    Uses its own dedupe key so these money alerts are never swallowed by the
    shared 30-minute RPC-error throttle inside _notify_admin_error().
    """
    key = "%.6f" % delta
    seen = context.bot_data.setdefault("unattributed_alerts", {})
    nowts = int(time.time())
    if nowts - seen.get(key, 0) < 1800:
        return
    if len(seen) > 200:
        seen.clear()
    seen[key] = nowts
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                "\u26a0\ufe0f <b>UNMATCHED WALLET DEPOSIT</b>\n\n"
                f"\U0001f4b5 <b>Amount:</b> <code>{delta:.6f} USDT</code>\n"
                f"\u2753 <b>Reason:</b> {html.escape(str(reason), quote=False)}\n\n"
                "These funds are NOT credited. The balance baseline is being "
                "held so the payment is not lost \u2014 check BscScan and credit "
                "manually via Admin \u2192 Add Balance."
            ),
            parse_mode="HTML",
        )
    except Exception:
        pass


async def _notify_admin_error(context, message: str):
    now = int(time.time())
    last = context.bot_data.get("last_payment_rpc_error", 0)

    if now - last < 1800:
        return

    context.bot_data["last_payment_rpc_error"] = now

    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"⚠️ <b>Web3 Payment Checker Error</b>\n\n<code>{html.escape(str(message), quote=False)}</code>",
            parse_mode="HTML",
        )
    except Exception:
        pass


async def check_payments(context):
    if not _config_ready():
        return

    # Expire stale invoices and notify their owners (best-effort).
    try:
        await _expire_old_deposits(context)
    except Exception as exc:
        log_error("Deposit expiry sweep failed", exc)

    pending = _pending_deposits()
    if not pending:
        # Keep the wallet balance baseline fresh even while idle, so that a
        # payment sent immediately after invoice creation is detected by the
        # balance check on the very next cycle.
        try:
            w3 = _get_web3(context)
            balance_now = _wallet_usdt_balance(w3)
            prev_idle = context.bot_data.get("last_wallet_balance")
            if prev_idle is None:
                try:
                    stored = get_setting("last_wallet_balance")
                    prev_idle = Decimal(str(stored)) if stored else None
                except Exception:
                    prev_idle = None
            # Never advance the baseline over money that landed while no
            # invoice was pending. Users routinely pay within seconds of the
            # invoice being created, and the invoice row may not exist yet on
            # this cycle. Burning the delta here made that payment impossible
            # to match on any later cycle.
            if prev_idle is not None and balance_now > prev_idle:
                print(
                    "[payscan] holding baseline while idle: +%s USDT arrived with no pending invoice"
                    % (balance_now - prev_idle),
                    flush=True,
                )
            else:
                context.bot_data["last_wallet_balance"] = balance_now
                set_setting("last_wallet_balance", str(balance_now))
        except Exception:
            _drop_cached_web3(context)
        return

    try:
        w3 = _get_web3(context)
    except Exception as exc:
        _drop_cached_web3(context)
        print(f"[payscan] RPC connection failed: {exc}", flush=True)
        log_error("Payment checker RPC connection error", exc)
        return

    # The event-log scan is best-effort only: free public BSC RPCs often
    # reject or stall on eth_getLogs. A failure here must NOT abort the
    # cycle, because the wallet-balance check below still verifies payments
    # reliably on every RPC.
    # A cheap balanceOf probe now drives the expensive work. Free public BSC
    # RPCs rate-limit eth_getLogs ("limit exceeded"), and the old code called
    # it on every 30s cycle whether or not any money had arrived - which kept
    # the scanner permanently throttled off and left the balance check as the
    # only working path. balanceOf is one cheap call that keeps succeeding, so
    # use it as the trigger and only scan logs when the balance actually moved.
    balance_now = None
    try:
        balance_now = _wallet_usdt_balance(w3)
    except Exception as exc:
        _drop_cached_web3(context)
        print(f"[payscan] balance probe failed: {exc}", flush=True)

    prev_balance = context.bot_data.get("last_wallet_balance")
    if prev_balance is None:
        try:
            stored = get_setting("last_wallet_balance")
            prev_balance = Decimal(str(stored)) if stored else None
        except Exception:
            prev_balance = None

    balance_rose = (
        balance_now is not None
        and prev_balance is not None
        and balance_now > prev_balance
    )

    logs, scanned_to = [], None
    fails = context.bot_data.get("logscan_failures", 0)
    cycle = context.bot_data.get("payscan_cycle", 0)
    context.bot_data["payscan_cycle"] = cycle + 1

    # Scan when money arrived, when there is no baseline yet, or as a periodic
    # safety sweep in case the balance probe itself is failing.
    should_scan = balance_rose or prev_balance is None or cycle % 10 == 0
    if should_scan:
        try:
            logs, scanned_to = _get_recent_incoming_usdt_logs(context, w3)
            if fails:
                print(f"[payscan] log scan recovered after {fails} failures", flush=True)
            context.bot_data["logscan_failures"] = 0
        except Exception as exc:
            context.bot_data["logscan_failures"] = fails + 1
            # Drop the cached connection so the next run rotates to a backup RPC.
            _drop_cached_web3(context)
            print(f"[payscan] log scan failed ({fails + 1} in a row): {exc}", flush=True)
            log_error("Payment checker RPC/log scan error", exc)
            if balance_rose:
                print("[payscan] balance rose but log scan failed; balance check will handle it", flush=True)

    # Re-read pending invoices AFTER the RPC work. The list fetched at the top
    # of this cycle is now several seconds old: connecting to the RPC, probing
    # the balance and chunk-scanning event logs all take real time. An invoice
    # created inside that window -- then paid immediately, which is the normal
    # case -- was missing from the stale list, so a payment for the exact
    # requested amount was reported as "no pending invoice matches this amount"
    # and held instead of credited.
    try:
        refreshed = _pending_deposits()
        if refreshed:
            pending = refreshed
    except Exception as exc:
        log_error("Pending invoice re-read failed", exc)

    credited_ids = set()
    for deposit_id, user_id, base_amount, unique_amount, checkout_product_id, checkout_quantity, checkout_done in pending:
        for log in logs:
            tx_hash = _tx_hash_from_log(log)

            if not tx_hash or not _not_processed(tx_hash):
                continue

            paid_amount = _amount_from_log(log)

            if not _matches_amount(paid_amount, unique_amount):
                continue

            tx_link = f"https://bscscan.com/tx/{tx_hash}"
            short_tx = tx_hash[:10] + "..." + tx_hash[-8:]

            if checkout_product_id and int(checkout_quantity or 0) > 0 and not checkout_done:
                product_id = int(checkout_product_id)
                quantity = int(checkout_quantity)

                cur.execute("SELECT name FROM products WHERE id=? AND active=1", (product_id,))
                product = cur.fetchone()

                if product:
                    product_name = product[0]
                    deliveries = reserve_stock_items(product_id, user_id, quantity)

                    if deliveries:
                        delivery_text = "\n\n".join([str(x) for x in deliveries])
                        remaining_stock = get_available_stock_count(product_id)
                        cur.execute("UPDATE products SET stock=? WHERE id=?", (remaining_stock, product_id))
                        await _maybe_low_stock_alert(context, product_id)
                        cur.execute(
                            "INSERT INTO orders(user_id, product_name, price, delivery) VALUES(?,?,?,?)",
                            (user_id, f"{product_name} x{quantity}", float(base_amount), encrypt_text(delivery_text))
                        )
                        order_id = cur.lastrowid

                        cur.execute(
                            "UPDATE deposits SET status='paid', tx_hash=?, paid_at=?, checkout_done=1 WHERE id=? AND status='pending'",
                            (tx_hash, now_text(), deposit_id),
                        )
                        cur.execute(
                            "INSERT OR IGNORE INTO processed_txs(tx_hash, deposit_id, user_id, amount) VALUES(?,?,?,?)",
                            (tx_hash, deposit_id, user_id, float(paid_amount)),
                        )
                        conn.commit()

                        try:
                            await context.bot.send_message(
                                chat_id=user_id,
                                text=(
                                    "✅ <b>Payment Successful!</b>\n"
                                    "━━━━━━━━━━━━━━━━━━━━\n\n"
                                    f"🧾 <b>Order ID:</b> <code>#{order_id}</code>\n\n"
                                    f"📦 <b>{html.escape(strip_premium(product_name), quote=False)}</b>\n"
                                    f"🔢 <b>Quantity:</b> <code>{quantity}</code>\n"
                                    f"💰 <b>Paid:</b> <code>${float(base_amount):.2f}</code>\n"
                                    f"🔗 <b>TX:</b> <a href='{tx_link}'>{short_tx}</a>\n\n"
                                    "🔑 <b>Code(s):</b>\n"
                                    f"<code>{html.escape(delivery_text, quote=False)}</code>\n\n"
                                    "━━━━━━━━━━━━━━━━━━━\n"
                                    "Thank you for your purchase! 🎉"
                                ),
                                parse_mode="HTML",
                                disable_web_page_preview=True,
                            )
                        except Exception as exc:
                            log_error("Failed to send direct checkout delivery", exc, extra=f"user={user_id} deposit={deposit_id}")

                        try:
                            await context.bot.send_message(
                                chat_id=ADMIN_ID,
                                text=(
                                    "🛒 <b>DIRECT CHECKOUT CONFIRMED</b>\n\n"
                                    f"👤 User: <code>{user_id}</code>\n"
                                    f"🧾 Invoice: <code>#{deposit_id}</code>\n"
                                    f"📦 Product: <b>{html.escape(strip_premium(product_name), quote=False)}</b>\n"
                                    f"🔢 Quantity: <code>{quantity}</code>\n"
                                    f"💰 Paid: <code>{float(base_amount):.2f} USDT</code>\n"
                                    f"🔗 TX: <a href='{tx_link}'>{short_tx}</a>"
                                ),
                                parse_mode="HTML",
                                disable_web_page_preview=True,
                            )
                        except Exception:
                            pass

                    else:
                        add_balance(user_id, float(base_amount))
                        cur.execute(
                            "UPDATE deposits SET status='paid', tx_hash=?, paid_at=?, checkout_done=1 WHERE id=? AND status='pending'",
                            (tx_hash, now_text(), deposit_id),
                        )
                        cur.execute(
                            "INSERT OR IGNORE INTO processed_txs(tx_hash, deposit_id, user_id, amount) VALUES(?,?,?,?)",
                            (tx_hash, deposit_id, user_id, float(paid_amount)),
                        )
                        conn.commit()

                        try:
                            await context.bot.send_message(
                                chat_id=user_id,
                                text=(
                                    "✅ <b>Payment Received</b>\n\n"
                                    "⚠️ Stock became unavailable before delivery.\n"
                                    f"💰 <b>{float(base_amount):.2f} USDT</b> was added to your wallet."
                                ),
                                parse_mode="HTML"
                            )
                        except Exception:
                            pass
                else:
                    add_balance(user_id, float(base_amount))
                    cur.execute(
                        "UPDATE deposits SET status='paid', tx_hash=?, paid_at=?, checkout_done=1 WHERE id=? AND status='pending'",
                        (tx_hash, now_text(), deposit_id),
                    )
                    cur.execute(
                        "INSERT OR IGNORE INTO processed_txs(tx_hash, deposit_id, user_id, amount) VALUES(?,?,?,?)",
                        (tx_hash, deposit_id, user_id, float(paid_amount)),
                    )
                    conn.commit()
            else:
                add_balance(user_id, float(base_amount))

                cur.execute(
                    "UPDATE deposits SET status='paid', tx_hash=?, paid_at=? WHERE id=? AND status='pending'",
                    (tx_hash, now_text(), deposit_id),
                )

                cur.execute(
                    "INSERT OR IGNORE INTO processed_txs(tx_hash, deposit_id, user_id, amount) VALUES(?,?,?,?)",
                    (tx_hash, deposit_id, user_id, float(paid_amount)),
                )

                conn.commit()

                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=render_message_template(
                            "deposit_success",
                            DEPOSIT_SUCCESS_DEFAULT,
                            invoice_id=deposit_id,
                            paid=f"{paid_amount:.5f}",
                            amount=f"{float(base_amount):.2f}",
                            balance=f"{get_balance(user_id):.2f}",
                            tx=f"<a href='{tx_link}'>{short_tx}</a>",
                        ),
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                except Exception as exc:
                    log_error("Failed to notify user about deposit", exc, extra=f"user={user_id} deposit={deposit_id}")

                try:
                    await context.bot.send_message(
                        chat_id=ADMIN_ID,
                        text=(
                            "💰 <b>AUTO PAYMENT CONFIRMED</b>\n\n"
                            "━━━━━━━━━━━━━━━\n\n"
                            f"👤 <b>User:</b> <code>{user_id}</code>\n"
                            f"🧾 <b>Invoice:</b> <code>#{deposit_id}</code>\n"
                            f"💵 <b>Paid:</b> <code>{paid_amount:.5f} USDT</code>\n"
                            f"💰 <b>Credit:</b> <code>{float(base_amount):.2f} USDT</code>\n"
                            f"🔗 <b>TX:</b> <a href='{tx_link}'>{short_tx}</a>"
                        ),
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                except Exception as exc:
                    log_error("Failed to notify admin about deposit", exc, extra=f"user={user_id} deposit={deposit_id}")

            credited_ids.add(deposit_id)
            break

    # Alert the admin about wallet payments that matched no pending invoice
    # (wrong amount sent, invoice expired or cancelled, etc.).
    alerted = context.bot_data.setdefault("unmatched_tx_alerts", set())
    for log in logs:
        tx_hash = _tx_hash_from_log(log)
        if not tx_hash or tx_hash in alerted or not _not_processed(tx_hash):
            continue
        alerted.add(tx_hash)
        paid_amount = _amount_from_log(log)
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    "⚠️ <b>Unmatched Payment Received</b>\n\n"
                    f"💵 Amount: <code>{paid_amount:.5f} USDT</code>\n"
                    f"🔗 TX: <code>{tx_hash}</code>\n\n"
                    "No pending invoice matches this amount. If this is a real "
                    "customer payment, credit it manually via Admin → Add Balance."
                ),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception:
            pass

    # Fallback verification: detect payments by wallet balance increase.
    # This works even when the RPC returns empty event logs.
    try:
        if balance_now is None:
            balance_now = _wallet_usdt_balance(w3)
        prev = prev_balance
        attributed = True

        if prev is not None:
            delta = balance_now - prev
            if delta > 0:
                # Direct-checkout invoices used to be excluded here via
                # "not d[4]". Once the log scan was rate-limited off that left
                # checkout payments with no working confirmation path at all,
                # so they are now matched too.
                matches = [
                    d for d in pending
                    if d[0] not in credited_ids and _matches_amount(delta, d[3])
                ]
                attributed = bool(credited_ids)
                if len(matches) == 1:
                    deposit_id, user_id, base_amount, unique_amount, _cpid, _cqty, _cdone = matches[0]
                    # Deterministic: including a timestamp made every pseudo tx unique,
                    # so INSERT OR IGNORE into processed_txs never deduped and a
                    # deposit could be credited twice on repeated balance deltas.
                    pseudo_tx = f"balance-delta-{deposit_id}"

                    add_balance(user_id, float(base_amount))
                    cur.execute(
                        "UPDATE deposits SET status='paid', tx_hash=?, paid_at=?, checkout_done=1 WHERE id=? AND status='pending'",
                        (pseudo_tx, now_text(), deposit_id),
                    )
                    cur.execute(
                        "INSERT OR IGNORE INTO processed_txs(tx_hash, deposit_id, user_id, amount) VALUES(?,?,?,?)",
                        (pseudo_tx, deposit_id, user_id, float(delta)),
                    )
                    conn.commit()
                    credited_ids.add(deposit_id)
                    attributed = True
                    print(f"[payscan] credited deposit #{deposit_id} via balance check (+{delta} USDT)", flush=True)

                    if _cpid and int(_cqty or 0) > 0 and not _cdone:
                        # The balance-only path cannot safely reserve stock and
                        # deliver codes, so the money lands in the wallet and
                        # the admin is told to finish the order.
                        try:
                            await context.bot.send_message(
                                chat_id=ADMIN_ID,
                                text=(
                                    "\U0001f6d2 <b>CHECKOUT PAID (balance check)</b>\n\n"
                                    f"\U0001f464 User: <code>{user_id}</code>\n"
                                    f"\U0001f9fe Invoice: <code>#{deposit_id}</code>\n"
                                    f"\U0001f4e6 Product ID: <code>{int(_cpid)}</code>\n"
                                    f"\U0001f522 Quantity: <code>{int(_cqty)}</code>\n"
                                    f"\U0001f4b5 Paid: <code>{delta:.5f} USDT</code>\n\n"
                                    "Credited to the user wallet balance. Deliver "
                                    "the product manually if it was not sent."
                                ),
                                parse_mode="HTML",
                            )
                        except Exception:
                            pass

                    try:
                        await context.bot.send_message(
                            chat_id=user_id,
                            text=render_message_template(
                                "deposit_success",
                                DEPOSIT_SUCCESS_DEFAULT,
                                invoice_id=deposit_id,
                                paid=f"{delta:.5f}",
                                amount=f"{float(base_amount):.2f}",
                                balance=f"{get_balance(user_id):.2f}",
                                tx="verified by balance check",
                            ),
                            parse_mode="HTML",
                        )
                    except Exception as exc:
                        log_error("Failed to notify user about deposit", exc, extra=f"user={user_id} deposit={deposit_id}")

                    try:
                        await context.bot.send_message(
                            chat_id=ADMIN_ID,
                            text=(
                                "💰 <b>AUTO PAYMENT CONFIRMED</b> (balance check)\n\n"
                                f"👤 <b>User:</b> <code>{user_id}</code>\n"
                                f"🧾 <b>Invoice:</b> <code>#{deposit_id}</code>\n"
                                f"💵 <b>Paid:</b> <code>{delta:.5f} USDT</code>\n"
                                f"💰 <b>Credit:</b> <code>{float(base_amount):.2f} USDT</code>"
                            ),
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
                elif len(matches) > 1:
                    await _alert_unattributed(
                        context, delta,
                        f"{len(matches)} pending invoices match this exact amount",
                    )
                else:
                    await _alert_unattributed(
                        context, delta,
                        "no pending invoice matches this amount",
                    )

                if not attributed:
                    # Never move the baseline past money we could not attribute.
                    # Advancing it unconditionally is what made unmatched
                    # payments permanently unrecoverable: the funds disappeared
                    # from the delta on the very next cycle, so no later run
                    # could ever match them. Carry the delta forward instead.
                    since = context.bot_data.get("unattributed_since")
                    if not since:
                        context.bot_data["unattributed_since"] = time.time()
                        print(f"[payscan] holding baseline: {delta} USDT unattributed", flush=True)
                    elif time.time() - since > DEPOSIT_EXPIRE_HOURS * 3600:
                        # Stop carrying a stale delta forever, otherwise it
                        # poisons amount matching for every future payment.
                        print(f"[payscan] dropping stale unattributed delta {delta} USDT", flush=True)
                        attributed = True

        if attributed:
            context.bot_data["unattributed_since"] = None
            context.bot_data["last_wallet_balance"] = balance_now
            set_setting("last_wallet_balance", str(balance_now))
    except Exception as exc:
        log_error("Balance fallback check failed", exc)

    # Only advance the scan pointer after processing finished successfully.
    if scanned_to:
        _remember_scanned_block(context, scanned_to)
