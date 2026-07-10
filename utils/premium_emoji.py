"""Premium (custom) emoji support for outgoing bot messages.

Telegram premium emojis arrive as `custom_emoji` message entities. To keep
them when the bot re-sends text (broadcasts, product details), they must be
converted to <tg-emoji emoji-id="...">fallback</tg-emoji> HTML tags and sent
with parse_mode="HTML".

Note (Telegram Bot API rule): tg-emoji sent by a bot renders as the animated
premium emoji only if the bot has purchased an additional username on
Fragment. Until then every user simply sees the normal fallback emoji.
Messages are delivered without errors either way.
"""


def has_premium_emoji(message) -> bool:
    """True when the message contains at least one premium (custom) emoji."""
    try:
        entities = list(message.entities or []) + list(message.caption_entities or [])
        return any(getattr(e, "type", "") == "custom_emoji" for e in entities)
    except Exception:
        return False


def premium_html(update, fallback_text):
    """Return the message text as safe HTML with premium emojis preserved
    as <tg-emoji> tags.

    When the message contains no premium emoji, returns fallback_text
    unchanged so all existing plain-text behaviour stays identical.
    """
    try:
        msg = getattr(update, "message", None)
        if msg is None or not has_premium_emoji(msg):
            return fallback_text
        if msg.text:
            html_text = msg.text_html
        elif msg.caption:
            html_text = msg.caption_html
        else:
            return fallback_text
        return (html_text or fallback_text).strip()
    except Exception:
        return fallback_text
