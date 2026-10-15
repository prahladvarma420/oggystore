"""Best-effort Telegram notifications (17.5); no financial work or polling loop.

At most eight daemon one-shot jobs/process. HTTP has an overall two-second
async deadline. Jobs may be lost on shutdown/overflow; attempted logical order
notifications are permanently deduplicated in existing settings, not retried.
Nothing modifies webhook delivery state or business/ledger transactions.
"""
import asyncio
import contextvars
import html
import logging
import re
import threading
import time

log = logging.getLogger(__name__)
NOTIFICATION_BUDGET_SECONDS = 2.0
_MAX_JOBS = threading.BoundedSemaphore(8)
_TRANSPORT_SCOPE = contextvars.ContextVar('telegram_notification_transport', default=False)
_JOB_DEADLINE = contextvars.ContextVar('telegram_notification_deadline', default=None)
_FACTORY_LOCK = threading.Lock()
_FACTORY_INSTALLED = False
_EVENTS = frozenset(('order.created', 'order.failed', 'order.cancelled'))
_STATUS_TEXT = {
    'approve': '✅ Your reseller application is approved. Open the Reseller Center to manage your account and API access.',
    'suspend': '⏸ Your reseller account is suspended. Reseller access is unavailable while suspended. Contact support for assistance.',
    'reactivate': '✅ Your reseller account is reactivated. Open the Reseller Center to continue using your account.',
    'block': 'Your reseller account has been closed. Reseller access is no longer available. Contact support if you need assistance.',
}


def _guard_transport_logging():
    """Redact records only in this task context, including HTTP debug URLs.

    Chain the prior factory; other threads/tasks retain their existing logging.
    No global logging.disable()/level changes and no raw Telegram exceptions.
    """
    global _FACTORY_INSTALLED
    with _FACTORY_LOCK:
        if _FACTORY_INSTALLED:
            return
        previous = logging.getLogRecordFactory()
        def factory(*args, **kwargs):
            record = previous(*args, **kwargs)
            if _TRANSPORT_SCOPE.get():
                record.msg = 'Telegram notification transport activity (redacted)'
                record.args = ()
                record.exc_info = record.exc_text = record.stack_info = None
            return record
        logging.setLogRecordFactory(factory)
        _FACTORY_INSTALLED = True


def _safe(value, budget=500):
    """Redact PII/credential-shaped text, then bound escaped HTML atomically."""
    import config
    from reseller_api.services.observability import sanitize_log_text
    raw = str(value or '')
    token = config.BOT_TOKEN
    if token:
        raw = raw.replace(str(token), '[REDACTED]')
    raw = re.sub(r'\b(?:rk|sk)_(?:test|live)_[A-Za-z0-9_-]+', '[REDACTED]', raw)
    raw = re.sub(r'(?i)\b(?:api[_ -]?key|token|password|secret|authorization)\s*[:=]\s*\S+', '[REDACTED]', raw)
    raw = sanitize_log_text(raw, 10000)
    out = ''; truncated = False
    for char in raw:
        part = html.escape(char)
        if len(out) + len(part) > budget - 1:
            truncated = True
            break
        out += part
    return out + ('…' if truncated else '')


def _public_reseller_id(value):
    value = str(value or '')
    return value if re.fullmatch(r'RSR-[A-HJ-NP-Z2-9]{10}', value) else 'Unavailable'


def build_status_notification(action, reseller_public_id):
    if action not in _STATUS_TEXT:
        raise ValueError('unsupported status notification')
    return (f'<b>Reseller account update</b>\n'
            f'<code>{_public_reseller_id(reseller_public_id)}</code>\n\n' + _STATUS_TEXT[action])


def build_owner_order_notification(event, snapshot):
    if event not in _EVENTS:
        raise ValueError('unsupported order notification')
    from database.reseller_db import from_cents
    public_order_id = str(snapshot['order_id'])
    if not re.fullmatch(r'ORD-[0-9]{8,20}', public_order_id):
        raise ValueError('invalid public order id')
    quantity = int(snapshot['quantity'])
    cents = int(snapshot['total_cents'])
    if not (0 < quantity < 10**15 and 0 <= cents < 10**24):
        raise ValueError('invalid order totals')
    title = {'order.created':'🛒 New reseller API order',
             'order.failed':'⚠️ Reseller API order failed',
             'order.cancelled':'❌ Reseller API order cancelled'}[event]
    return (f'<b>{title}</b>\n'
            f'Reseller: {_safe(snapshot.get("reseller_name"), 700)}\n'
            f'Reseller ID: <code>{_public_reseller_id(snapshot.get("reseller_public_id"))}</code>\n'
            f'Product: {_safe(snapshot.get("product_name"), 1500)}\n'
            f'Quantity: <code>{quantity}</code>\n'
            f'Total: <b>{from_cents(cents)} USDT</b>\n'
            f'Order: <code>{public_order_id}</code>')


def _configured():
    import config
    return bool(config.BOT_TOKEN)


def _transaction_open():
    from database.db import get_conn
    conn = get_conn()
    return bool(getattr(conn, 'in_transaction', False) or getattr(conn, '_transaction_depth', 0))


async def _bot_send(chat_id, text):
    # Import/create inside the background job; API has no running bot loop.
    from telegram import Bot
    import config
    async with Bot(config.BOT_TOKEN) as bot:
        await bot.send_message(chat_id=int(chat_id), text=text, parse_mode='HTML',
                               disable_web_page_preview=True,
                               connect_timeout=2, read_timeout=2,
                               write_timeout=2, pool_timeout=2)


def _send_once(chat_id, text):
    if not _configured():
        log.info('notification category=unconfigured')
        return False
    deadline = _JOB_DEADLINE.get()
    budget = min(NOTIFICATION_BUDGET_SECONDS, deadline-time.monotonic()) if deadline is not None else NOTIFICATION_BUDGET_SECONDS
    if budget <= 0:
        log.warning('notification category=preparation_timeout')
        return False
    _guard_transport_logging()
    async def bounded():
        await asyncio.wait_for(_bot_send(chat_id, text), timeout=budget)
    scope = _TRANSPORT_SCOPE.set(True)
    try:
        asyncio.run(bounded())
    except Exception:
        outcome = False
    else:
        outcome = True
    finally:
        _TRANSPORT_SCOPE.reset(scope)
    if not outcome:
        log.warning('notification category=telegram_unavailable')
    return outcome


def _start_job(job):
    if not _MAX_JOBS.acquire(blocking=False):
        log.warning('notification category=capacity_skipped')
        return False
    def run():
        deadline = _JOB_DEADLINE.set(time.monotonic()+NOTIFICATION_BUDGET_SECONDS)
        try:
            job()
        except Exception:
            log.warning('notification category=job_failed')
        finally:
            _JOB_DEADLINE.reset(deadline)
            # Each daemon owns its database connection, if one was opened.
            from database.db import close_thread_connection
            try:
                close_thread_connection()
            finally:
                _MAX_JOBS.release()
    try:
        threading.Thread(target=run, name='reseller-notification', daemon=True).start()
        return True
    except Exception:
        _MAX_JOBS.release()
        log.warning('notification category=dispatch_failed')
        return False


def enqueue_message(chat_id, text):
    """Mockable off-request send seam; accepted does not mean delivered."""
    try:
        if not chat_id:
            log.info('notification category=no_telegram_link')
            return False
        if not _configured():
            log.info('notification category=unconfigured')
            return False
        if len(text) > 4096:
            log.warning('notification category=invalid_message')
            return False
        return _start_job(lambda: _send_once(int(chat_id), text))
    except Exception:
        log.warning('notification category=dispatch_failed')
        return False


def notify_reseller_status(reseller, action):
    """Called only after a successful conditional status update commits."""
    try:
        if not reseller.get('telegram_user_id'):
            log.info('notification category=no_telegram_link')
            return False
        if _transaction_open():
            log.warning('notification category=uncommitted_status_skipped')
            return False
        return enqueue_message(reseller['telegram_user_id'],
                               build_status_notification(action, reseller.get('public_id')))
    except Exception:
        log.warning('notification category=status_dispatch_failed')
        return False


def _claim_order_event(oid, event, audience):
    """Permanent at-most-once attempt marker tied to an existing logical event.

    Independent short transaction AFTER the business commit; never spans HTTP.
    INSERT ON CONFLICT works through existing SQLite/PostgreSQL compatibility.
    This is not a claim_worker_job lease (which expires and could resend).
    """
    from database.db import get_conn, get_cursor
    from database.pgcompat import atomic
    conn, cur = get_conn(), get_cursor()
    with atomic(conn):
        cur.execute('SELECT 1 FROM webhook_outbox e JOIN orders o ON o.reseller_id=e.reseller_id '
                    "WHERE o.id=? AND o.source='reseller_api' AND e.event_type=? AND e.aggregate_id=?",
                    (int(oid), event, f'ORD-{int(oid):08d}'))
        if not cur.fetchone():
            return False
        cur.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO NOTHING',
                    (f'telegram.{audience}.{event}.ORD-{int(oid):08d}', 'attempted'))
        return cur.rowcount == 1


def queue_owner_order_event(oid, event):
    """No SQL reads/writes or Telegram I/O on the request path."""
    try:
        import config
        if event not in _EVENTS:
            return False
        if not config.ADMIN_ID or not _configured():
            return False
        if _transaction_open():
            log.warning('notification category=uncommitted_order_skipped')
            return False
        def job():
            from database.db import get_setting, get_cursor
            started = time.monotonic()
            if str(get_setting('reseller_api_owner_notify', '1')).strip().lower() in ('0','false','off'):
                return  # intentionally silent when disabled, including no marker
            cur = get_cursor()
            cur.execute('SELECT o.reseller_id,r.public_id,r.name,o.product_name,o.quantity,o.unit_price_cents '
                        'FROM orders o JOIN resellers r ON r.id=o.reseller_id '
                        "WHERE o.id=? AND o.source='reseller_api'", (int(oid),))
            row = cur.fetchone()
            if not row:
                return
            snapshot = dict(reseller_public_id=row[1],reseller_name=row[2],product_name=row[3],
                            quantity=row[4],total_cents=int(row[4])*int(row[5]),order_id=f'ORD-{int(oid):08d}')
            text = build_owner_order_notification(event, snapshot)
            if time.monotonic()-started >= NOTIFICATION_BUDGET_SECONDS:
                log.warning('notification category=preparation_timeout')
                return
            if _claim_order_event(oid, event, 'owner'):
                _send_once(config.ADMIN_ID, text)
        return _start_job(job)
    except Exception:
        log.warning('notification category=owner_dispatch_failed')
        return False


def _notify_reseller_order(tg_uid, oid, event, text, *, send=None):
    # Explicit send is the historical isolated transport seam. Production callers
    # no longer pass the synchronous WebAdmin tg_send transport.
    if send is not None:
        try:
            ok, _ = send(int(tg_uid), text)
            return bool(ok)
        except Exception:
            return False
    return _enqueue_reseller_order(tg_uid, oid, event, text)


def _enqueue_reseller_order(tg_uid, oid, event, text):
    try:
        if not tg_uid:
            log.info('notification category=no_telegram_link')
            return False
        if _transaction_open():
            log.warning('notification category=uncommitted_order_skipped')
            return False
        if not _configured():
            log.info('notification category=unconfigured')
            return False
        def job():
            if _claim_order_event(oid,event,'reseller'):
                _send_once(tg_uid, text)
        return _start_job(job)
    except Exception:
        log.warning('notification category=reseller_dispatch_failed')
        return False


def notify_order_failed(tg_uid, oid, row, reason, refunded_cents=0, *, send=None):
    try:
        from database.reseller_db import from_cents
        text = ('⚠️ <b>Order failed</b>\n'
                f'Order <code>ORD-{int(oid):08d}</code> ({_safe(row.get("product_name"),1500)}) could not be fulfilled.\n'
                f'Reason: {_safe(reason,700)}\n'
                f'Refund: <b>{from_cents(int(refunded_cents))} USDT</b> was returned to your wallet.\n')
        return _notify_reseller_order(tg_uid,oid,'order.failed',text,send=send)
    except Exception:
        return False


def notify_order_cancelled(tg_uid, oid, row, reason, refunded_cents=0, *, send=None):
    try:
        from database.reseller_db import from_cents
        text = ('❌ <b>Order cancelled</b>\n'
                f'Order <code>ORD-{int(oid):08d}</code>\n'
                f'Product: {_safe(row.get("product_name"),1500)}\n'
                f'Refund: <b>{from_cents(int(refunded_cents))} USDT</b>\n'
                f'Reason: {_safe(reason,700)}')
        return _notify_reseller_order(tg_uid,oid,'order.cancelled',text,send=send)
    except Exception:
        return False


def send_worker_telegram(chat_id, text):
    """Legacy external caller seam; now uses the bounded off-path mechanism."""
    accepted = enqueue_message(chat_id,text)
    return accepted, 'queued' if accepted else 'skipped'
