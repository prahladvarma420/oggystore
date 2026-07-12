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


import html as _html
import re

_TG_EMOJI_RE = re.compile(r'<tg-emoji emoji-id="(\d+)">(.*?)</tg-emoji>', re.S)


_OTHER_TAG_RE = re.compile(r"</?(?!tg-emoji\b)[a-zA-Z][^>]*>")


def strip_premium(text):
    """Plain text for buttons/messages: premium tags -> fallback emoji, all
    other HTML tags removed, HTML entities unescaped.

    Handles names that were HTML-escaped one or more times before reaching
    here (e.g. &lt;tg-emoji ...&gt; forms): it unescapes first, then strips,
    and repeats until the text is stable so escaped tags can never survive
    and re-materialize as literal tag text."""
    s = str(text or "")
    for _ in range(4):
        prev = s
        s = _html.unescape(s)
        s = _TG_EMOJI_RE.sub(r"\2", s)
        s = _OTHER_TAG_RE.sub("", s)
        if s == prev:
            break
    return s.strip()


def sanitize_premium_name(text):
    """Sanitize a product name captured via premium_html: keep only
    <tg-emoji> tags, drop every other HTML tag (<b>, <i>, ...) that
    Telegram adds when the user copies formatted text."""
    return _OTHER_TAG_RE.sub("", str(text or "")).strip()


def button_premium(text):
    """Return (button_text, icon_id) for a name that may contain premium
    emoji. The FIRST premium emoji becomes the button icon and its fallback
    is removed from the text so it is not shown twice."""
    s = str(text or "")
    m = _TG_EMOJI_RE.search(s)
    if not m:
        return strip_premium(s), None
    return strip_premium(s[:m.start()] + s[m.end():]), m.group(1)


def first_premium_id(text):
    """Custom-emoji id of the first premium emoji in the text, or None.

    Use with InlineKeyboardButton(icon_custom_emoji_id=...) to show one
    premium emoji as the button icon (works when the bot owner has
    Telegram Premium or the bot has a Fragment username).
    """
    m = _TG_EMOJI_RE.search(str(text or ""))
    return m.group(1) if m else None
