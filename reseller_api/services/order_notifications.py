"""Shared 16.2 failure formatter; bounded safe worker Telegram transport."""
import html
import json
import os
from database.reseller_db import from_cents


def notify_order_failed(tg_uid, oid, row, reason, refunded_cents=0, *, send=None):
    """Same message/escaping as 16.2; no transaction or financial work."""
    try:
        text = ("⚠️ <b>Order failed</b>\n"
            f"Order <code>ORD-{int(oid):08d}</code> "
            f"({html.escape(str(row.get('product_name') or ''))}) "
            "could not be fulfilled.\n"
            f"Reason: {html.escape(str(reason)[:300])}\n"
            f"Refund: <b>{from_cents(int(refunded_cents))} USDT</b> "
            "was returned to your wallet.\n")
        ok, _ = (send or send_worker_telegram)(int(tg_uid),text)
        return bool(ok)
    except Exception:
        return False


def send_worker_telegram(chat_id, text):
    # Reuse pinned TLS, deadline, debug protection and redaction; never import
    # the WebAdmin HTTP application just to send a notification.
    from fulfillment.external_clients.generic_api_client import GenericApiClient, _result
    from fulfillment.router import dispatch_client
    token = os.getenv('BOT_TOKEN')
    if not token:
        return False, 'unconfigured'
    class TelegramClient(GenericApiClient):
        def _build_payload(self, order_id, cfg, quantity):
            return {'chat_id':chat_id,'text':text,'parse_mode':'HTML','disable_web_page_preview':True}
        def _parse_response(self,data,http_status,quantity,cfg):
            return _result('fulfilled' if isinstance(data,dict) and data.get('ok') is True else 'terminal_error')
    cfg = {'endpoint':f'https://api.telegram.org/bot{token}/sendMessage',
           'retries':0,'dispatch_budget':5,'connect_timeout':2,'read_timeout':2,
           'idempotency_prefix':'telegram-notify'}
    client = TelegramClient(cfg)
    client.sensitive_values = (token,text)
    result = dispatch_client(client,cfg,int(chat_id),1)
    return result.get('status')=='fulfilled', 'sent' if result.get('status')=='fulfilled' else 'unavailable'
