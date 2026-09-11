"""Automatic button colors (Telegram Bot API 9.4+, python-telegram-bot 22.7+).

Color scheme used across the whole bot:
- blue   ('primary'): buy / pay buttons and main menu actions
- green  ('success'): refresh, apply coupon, change qty, deposit amounts,
                      confirm / approve
- red    ('danger') : back buttons, cancel / close / delete / remove /
                      reject / block / clear
- no style (default): everything else

Shop list product buttons get explicit colors (red = out of stock,
blue = in stock) passed via style=.

Older Telegram apps (released before Feb 9, 2026) simply show the normal
default buttons - no errors, fully backward compatible.
"""

from telegram import InlineKeyboardButton as _InlineKeyboardButton

# Exact callback_data values that are "main actions" -> blue
_BLUE_CB_EXACT = {
    "home", "shop", "deposit", "wallet", "orders", "profile",
    "support_ticket", "search", "admin_panel", "deposit_history",
}

# Buy / pay / quantity actions -> blue
_BLUE_CB_PREFIXES = ("previewbuy_", "directpay_", "buy_", "paywallet", "walletpay", "pay_", "qtymenu_", "customqty_")
_BLUE_TEXT_PARTS = ("buy", "pay", "change qty", "quantity")

# Destructive / negative actions -> red (checked first, strongest signal)
_RED_CB_PARTS = ("cancel", "close", "delete", "remove", "reject", "block", "clear")
_RED_TEXT_PARTS = ("\u274c", "\U0001f5d1", "cancel", "close", "delete", "remove", "reject", "block")

# Helpful / positive actions -> green
_GREEN_CB_PREFIXES = ("depneed_", "approve", "confirm")
_GREEN_CB_PARTS = ("approve", "confirm", "coupon", "refresh")
_GREEN_TEXT_PARTS = ("\u2705", "confirm", "approve", "refresh", "coupon")


def _infer_style(text, callback_data):
    t = str(text or "").lower()
    cb = callback_data.lower() if isinstance(callback_data, str) else ""

    # unblock must not match "block"-red; treat it as green (positive)
    if "unblock" in cb or "unblock" in t:
        return "success"

    # Back buttons -> red (but not "Backup" or "Notify Me When Back")
    if "back" in t and "backup" not in t and "notify" not in t:
        return "danger"

    if any(p in cb for p in _RED_CB_PARTS) or any(p in t for p in _RED_TEXT_PARTS):
        return "danger"

    if (
        cb.startswith(_GREEN_CB_PREFIXES)
        or any(p in cb for p in _GREEN_CB_PARTS)
        or any(p in t for p in _GREEN_TEXT_PARTS)
    ):
        return "success"

    if (
        cb in _BLUE_CB_EXACT
        or cb.startswith(_BLUE_CB_PREFIXES)
        or any(p in t for p in _BLUE_TEXT_PARTS)
    ):
        return "primary"

    return None


class AutoStyledButton(_InlineKeyboardButton):
    """Drop-in InlineKeyboardButton that colors itself automatically.

    Pass style="primary"/"success"/"danger" explicitly to override, or
    style="" to force the default (uncolored) look.
    """

    __slots__ = ()

    def __init__(self, text, url=None, callback_data=None, *args, style=None, **kwargs):
        if style is None:
            style = _infer_style(text, callback_data)
        elif style == "":
            style = None
        super().__init__(text, url, callback_data, *args, style=style, **kwargs)


def copy_amount_text(value):
    """Exact invoice amount for copy buttons: full precision, only trailing
    zeros trimmed (the numeric value is never altered)."""
    try:
        return f"{float(value):.6f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return str(value or "")


def make_copy_button(text, value, fallback_callback):
    """Inline button that copies ``value`` straight to the clipboard when
    tapped (Bot API 7.10+ ``copy_text`` buttons): Telegram shows a native
    \"Copied\" toast and NO message is sent to the chat -- the user just
    pastes, on phone, desktop or web.

    If the installed python-telegram-bot is too old for copy buttons, a
    normal callback button with ``fallback_callback`` is returned instead;
    its handler sends the value in a tap-to-copy <code> message (legacy UX).
    """
    try:
        from telegram import CopyTextButton
        try:
            return _InlineKeyboardButton(text, copy_text=CopyTextButton(text=str(value)))
        except TypeError:
            # PTB variant whose copy_text takes the plain string directly
            return _InlineKeyboardButton(text, copy_text=str(value))
    except Exception:
        return _InlineKeyboardButton(text, callback_data=fallback_callback)
