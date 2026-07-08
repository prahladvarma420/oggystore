import html
import time
from datetime import datetime, timedelta
from decimal import Decimal

from web3 import Web3

from utils.error_logger import log_error
from utils.timezone import now, now_text

from utils.crypto import encrypt_text
from database.db import cur, conn, add_balance, reserve_stock_items, get_available_stock_count
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


# Public BSC RPCs reject large eth_getLogs ranges with {'code': -32005,
# 'message': 'limit exceeded'}. Scan the window in small chunks instead.
LOG_SCAN_CHUNK_BLOCKS = 450


def _get_recent_incoming_usdt_logs(w3: Web3):
    latest_block = w3.eth.block_number
    to_block = max(latest_block - PAYMENT_REQUIRED_CONFIRMATIONS, 0)
    from_block = max(to_block - PAYMENT_SCAN_BLOCKS, 0)

    logs = []
    start = from_block
    while start <= to_block:
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
        start = end + 1

    return logs




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

    pending = _pending_deposits()
    if not pending:
        return

    try:
        w3 = _get_web3(context)
        logs = _get_recent_incoming_usdt_logs(w3)
    except Exception as exc:
        # Drop the cached connection so the next run rotates to a backup RPC.
        _drop_cached_web3(context)
        log_error("Payment checker RPC/log scan error", exc)
        await _notify_admin_error(
            context,
            f"{exc}\n\nSet the RPC_URL variable on the Railway worker to a working BSC RPC "
            f"if this keeps happening.",
        )
        return

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
                        text=(
                            "✅ <b>DEPOSIT CONFIRMED</b>\n\n"
                            "━━━━━━━━━━━━━━━\n\n"
                            f"🧾 <b>Invoice:</b> <code>#{deposit_id}</code>\n"
                            f"💵 <b>Paid:</b> <code>{paid_amount:.5f} USDT</code>\n"
                            f"💰 <b>Balance Added:</b> <code>{float(base_amount):.2f} USDT</code>\n"
                            f"🔗 <b>TX:</b> <a href='{tx_link}'>{short_tx}</a>\n\n"
                            "━━━━━━━━━━━━━━━\n\n"
                            "🛍 <i>You can now buy products from Shop.</i>"
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

            break
