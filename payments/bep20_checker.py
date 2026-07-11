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
        WHERE status='pending' AND created >= ?
        ORDER BY id ASC
        """,
        (cutoff,),
    )
    return cur.fetchall()


async def _expire_old_deposits(context):
    """Mark stale pending invoices as expired and notify their owners."""
    cutoff = (now() - timedelta(hours=DEPOSIT_EXPIRE_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    cur.execute(
        "SELECT id, user_id, amount FROM deposits WHERE status='pending' AND created < ?",
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
MAX_CHUNKS_PER_CYCLE = 12


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

    print(f"[payscan] scanning blocks {from_block}-{to_block} (latest {latest_block})", flush=True)

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

    print(f"[payscan] scanned {chunks} chunk(s) up to block {scanned_to}, found {len(logs)} incoming transfer log(s)", flush=True)
    return logs, scanned_to




async def _maybe_low_stock_alert(context, product_id: int):
    try:
        cur.execute("SELECT value FROM settings WHERE key='low_stock_alert_threshold'")
        row = cur.fetchone()
        threshold = int(row[0]) if row and row[0] else 3

        stock = get_available_stock_count(product_id)

        cur.execute("SELECT name, low_stock_notified FROM products WHERE id=?", (product_id,))
        p = cur.fetchone()
        if not p:
            return

        name, notified = p

        if stock <= threshold and not notified:
            cur.execute("UPDATE products SET low_stock_notified=1 WHERE id=?", (product_id,))
            conn.commit()
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=f"""⚠️ <b>Low Stock Alert</b>

📦 <b>{name}</b>
🆔 Product ID: <code>{product_id}</code>
📊 Remaining: <code>{stock}</code>

Please restock soon.""",
                parse_mode="HTML"
            )
        elif stock > threshold and notified:
            cur.execute("UPDATE products SET low_stock_notified=0 WHERE id=?", (product_id,))
            conn.commit()
    except Exception as exc:
        log_error("Low stock alert failed", exc, extra=f"product={product_id}")


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
    logs, scanned_to = [], None
    fails = context.bot_data.get("logscan_failures", 0)
    cycle = context.bot_data.get("payscan_cycle", 0)
    context.bot_data["payscan_cycle"] = cycle + 1
    if fails < 5 or cycle % 20 == 0:
        try:
            logs, scanned_to = _get_recent_incoming_usdt_logs(context, w3)
            context.bot_data["logscan_failures"] = 0
        except Exception as exc:
            context.bot_data["logscan_failures"] = fails + 1
            # Drop the cached connection so the next run rotates to a backup RPC.
            _drop_cached_web3(context)
            print(f"[payscan] log scan failed ({fails + 1} in a row): {exc}", flush=True)
            log_error("Payment checker RPC/log scan error", exc)
    else:
        print("[payscan] log scan disabled after repeated failures; using balance check only", flush=True)

    print(f"[payscan] pending invoices: {[(d[0], d[3]) for d in pending]}", flush=True)
    for log in logs:
        print(f"[payscan] log tx={_tx_hash_from_log(log)} amount={_amount_from_log(log)}", flush=True)

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
                            "UPDATE deposits SET status='paid', tx_hash=?, paid_at=?, checkout_done=1 WHERE id=?",
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
                                    f"📦 <b>{product_name}</b>\n"
                                    f"🔢 <b>Quantity:</b> <code>{quantity}</code>\n"
                                    f"💰 <b>Paid:</b> <code>${float(base_amount):.2f}</code>\n"
                                    f"🔗 <b>TX:</b> <a href='{tx_link}'>{short_tx}</a>\n\n"
                                    "🔑 <b>Code(s):</b>\n"
                                    f"<code>{delivery_text}</code>\n\n"
                                    "━━━━━━━━━━━━━━━━━━━━\n"
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
                                    f"📦 Product: <b>{product_name}</b>\n"
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
                            "UPDATE deposits SET status='paid', tx_hash=?, paid_at=?, checkout_done=1 WHERE id=?",
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
                        "UPDATE deposits SET status='paid', tx_hash=?, paid_at=?, checkout_done=1 WHERE id=?",
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
                    "UPDATE deposits SET status='paid', tx_hash=?, paid_at=? WHERE id=?",
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
        balance_now = _wallet_usdt_balance(w3)
        prev = context.bot_data.get("last_wallet_balance")
        if prev is None:
            stored = get_setting("last_wallet_balance")
            prev = Decimal(str(stored)) if stored else None

        if prev is not None:
            delta = balance_now - prev
            if delta > 0:
                matches = [
                    d for d in pending
                    if d[0] not in credited_ids and not d[4] and _matches_amount(delta, d[3])
                ]
                if len(matches) == 1:
                    deposit_id, user_id, base_amount, unique_amount, _cpid, _cqty, _cdone = matches[0]
                    pseudo_tx = f"balance-delta-{deposit_id}-{int(time.time())}"

                    add_balance(user_id, float(base_amount))
                    cur.execute(
                        "UPDATE deposits SET status='paid', tx_hash=?, paid_at=? WHERE id=?",
                        (pseudo_tx, now_text(), deposit_id),
                    )
                    cur.execute(
                        "INSERT OR IGNORE INTO processed_txs(tx_hash, deposit_id, user_id, amount) VALUES(?,?,?,?)",
                        (pseudo_tx, deposit_id, user_id, float(delta)),
                    )
                    conn.commit()
                    credited_ids.add(deposit_id)
                    print(f"[payscan] credited deposit #{deposit_id} via balance check (+{delta} USDT)", flush=True)

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
                    await _notify_admin_error(
                        context,
                        f"Wallet balance increased by {delta} USDT but multiple pending "
                        f"invoices match. Credit manually via Admin -> Add Balance.",
                    )

        context.bot_data["last_wallet_balance"] = balance_now
        set_setting("last_wallet_balance", str(balance_now))
        print(f"[payscan] wallet balance {balance_now} USDT", flush=True)
    except Exception as exc:
        log_error("Balance fallback check failed", exc)

    # Only advance the scan pointer after processing finished successfully.
    if scanned_to:
        _remember_scanned_block(context, scanned_to)
