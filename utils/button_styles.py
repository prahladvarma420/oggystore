"""Automatic button colors (Telegram Bot API 9.4+, python-telegram-bot 22.7+).

Color scheme used across the whole bot:
- green  ('success'): buy / pay / confirm / approve / deposit amounts
- red    ('danger') : cancel / close / delete / remove / reject / block / clear
- blue   ('primary'): main menu actions (shop, deposit, wallet, orders, ...)
- no style (default): navigation, back buttons and everything else

Older Telegram apps (released before Feb 9, 2026) simply show the normal
default buttons - no errors, fully backward compatible.
"""

from telegram import InlineKeyboardButton as _InlineKeyboardButton

# Exact callback_data values that are "main actions" -> blue
_BLUE_CB_EXACT = {
    "home", "shop", "deposit", "wallet", "orders", "profile",
    "support_ticket", "search", "admin_panel",
}

# Destructive / negative actions -> red (checked first, strongest signal)
_RED_CB_PARTS = ("cancel", "close", "delete", "remove", "reject", "block", "clear")
_RED_TEXT_PARTS = ("\u274c", "\U0001f5d1", "cancel", "close", "delete", "remove", "reject", "block")

# Positive / money actions -> green
_GREEN_CB_PREFIXES = ("previewbuy_", "directpay_", "buy_", "depneed_", "approve", "confirm", "paywallet", "walletpay", "pay_")
_GREEN_CB_PARTS = ("approve", "confirm",)
_GREEN_TEXT_PARTS = ("\u2705", "buy", "pay", "confirm", "approve", "redeem now", "i have paid")


def _infer_style(text, callback_data):
    t = str(text or "").lower()
    cb = callback_data.lower() if isinstance(callback_data, str) else ""

    # unblock must not match "block"-red; treat it as green (positive)
    if "unblock" in cb or "unblock" in t:
        return "success"

    if any(p in cb for p in _RED_CB_PARTS) or any(p in t for p in _RED_TEXT_PARTS):
        return "danger"

    if (
        cb.startswith(_GREEN_CB_PREFIXES)
        or any(p in cb for p in _GREEN_CB_PARTS)
        or any(p in t for p in _GREEN_TEXT_PARTS)
    ):
        return "success"

    if cb in _BLUE_CB_EXACT:
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
