"""Bulk delivery as a numbered .txt file.

Shared layer for every purchase path (wallet checkout in
products/product_handlers.py, direct crypto checkouts in payments/*) and for
order history (handlers/user_handlers.py).

Why this exists
---------------
Telegram messages are length-limited, so an order with more than a handful of
codes/links cannot be dumped into a chat message. For quantity > 5 (or any
delivery text that is simply too long) the bot now sends:

    successful purchase message  +  numbered .txt attachment

The order row in the database already stores the COMPLETE delivery (encrypted)
-- nothing here changes that. This module only builds the human-readable file
content from the delivered items, and can rebuild it later from the stored
delivery text (see split_delivery_items), so the file is always regeneratable
even if the original attachment is long gone.
"""

import io
import re

from utils.premium_emoji import strip_premium

# Quantity strictly greater than this is delivered as a .txt file.
BULK_DELIVERY_MAX_INLINE_QTY = 5

# Same guard for the text itself: a "small" order whose content would still
# overflow Telegram's message limit also goes out as a file.
MAX_INLINE_DELIVERY_CHARS = 3500

# Matches the separators produced by format_delivery_items() for multi-line
# account-style stock:
#   "────────────────────────────\n👤 ACCOUNT i of N\n────────────────────────────\n<item>"
_ACCOUNT_SEP = "\u2500" * 28
_ACCOUNT_HEADER_RE = re.compile(
    r"\n{0,2}" + re.escape(_ACCOUNT_SEP) + r"\n\U0001F464 ACCOUNT \d+ of \d+\n" + re.escape(_ACCOUNT_SEP) + r"\n"
)


def is_bulk_delivery(quantity, delivery_text=None):
    """True when this delivery must go out as a .txt file, not inline."""
    try:
        if int(quantity) > BULK_DELIVERY_MAX_INLINE_QTY:
            return True
    except Exception:
        pass
    if delivery_text is not None and len(str(delivery_text)) > MAX_INLINE_DELIVERY_CHARS:
        return True
    return False


def split_delivery_items(delivery_text):
    """Recover the individual delivered items from a stored delivery text.

    Reverses format_delivery_items():
      - account-style stock was joined with '👤 ACCOUNT i of N' separators
        -> split on the separator header (exact, even if an item contains
        blank lines);
      - plain one-line codes/links were joined with a blank line -> split on
        blank lines.

    Never raises and never drops content: unknown formats come back as a
    single item so nothing is ever lost.
    """
    text = str(delivery_text or "")
    if not text.strip():
        return []

    if ("\U0001F464 ACCOUNT " in text) and (_ACCOUNT_SEP in text):
        parts = _ACCOUNT_HEADER_RE.split(text)
        items = [p.strip("\n") for p in parts if p and p.strip()]
        if items:
            return items

    items = [p for p in text.split("\n\n") if p.strip()]
    return items or [text]


def build_delivery_txt(order_id, product_name, quantity, items, paid, balance):
    """Build the numbered .txt delivery file content for one order.

    All values are dynamic and come from the actual order; nothing is
    hardcoded. `items` is the list of delivered links/credentials.
    """
    try:
        quantity = int(quantity)
    except Exception:
        quantity = len(items or [])
    items = [str(it) for it in (items or [])]
    plain_name = strip_premium(str(product_name or "")) or "Product"

    lines = [
        "\u2714\ufe0f Payment Successful!",
        f"\u270f\ufe0f Order ID: #{order_id}",
        f"\U0001f6cd Product: {plain_name}",
        f"\U0001f4e6 Quantity: {quantity}",
        "\U0001f48e Code:",
    ]

    for i, item in enumerate(items, 1):
        item = item.strip("\n")
        if "\n" in item:
            # Multi-line credential block: keep the number on its own line so
            # the block stays exactly as delivered.
            lines.append(f"{i}.")
            lines.append(item)
        else:
            lines.append(f"{i}. {item}")

    try:
        paid_text = f"{float(paid):.2f}"
    except Exception:
        paid_text = str(paid)
    try:
        balance_text = f"{float(balance):.2f}"
    except Exception:
        balance_text = str(balance)

    lines += [
        f"\U0001F3D0 Paid: {paid_text} USDT",
        f"\u26a1\ufe0f Balance: {balance_text} USDT",
        "\U0001f389 Thank you for your purchase",
    ]
    return "\n".join(lines)


def delivery_txt_filename(order_id):
    return f"order_{order_id}_delivery.txt"


def checkout_codes_section(quantity, delivery_text):
    """The 'Code(s)' section of a direct crypto checkout message (BEP20/TRC20/
    Binance Pay). Small orders get the codes inline exactly as before; bulk
    orders get a note that the numbered TXT file is attached instead."""
    import html as _html
    if is_bulk_delivery(quantity, delivery_text):
        return (
            f"\U0001f4ce <b>Your {int(quantity)} items are attached as a "
            "numbered TXT file below.</b>\n\n"
        )
    return (
        "\U0001f511 <b>Code(s):</b>\n"
        f"<code>{_html.escape(str(delivery_text), quote=False)}</code>\n\n"
    )


def bot_document_sender(bot, chat_id):
    """Adapt context.bot.send_document into the callable send_delivery_txt()
    expects (used by the payment checkers, which have no message object)."""
    async def _send(**kwargs):
        return await bot.send_document(chat_id=chat_id, **kwargs)
    return _send


async def send_delivery_txt(send_document, *, order_id, product_name, quantity,
                            items, paid, balance, caption=None):
    """Build the numbered delivery TXT and send it as a document.

    `send_document` is a reply_document-style async callable, e.g.
    message.reply_document or bot_document_sender(context.bot, chat_id).
    Returns the TXT content (useful for tests). Never logs the content.
    """
    txt = build_delivery_txt(
        order_id=order_id,
        product_name=product_name,
        quantity=quantity,
        items=items,
        paid=paid,
        balance=balance,
    )
    data = io.BytesIO(txt.encode("utf-8"))
    filename = delivery_txt_filename(order_id)
    data.name = filename
    await send_document(
        document=data,
        filename=filename,
        caption=caption or f"\U0001f511 Codes for Order #{order_id} ({quantity} items)",
    )
    return txt
