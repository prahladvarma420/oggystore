"""Post-commit business handoff. Network dispatch never owns a DB transaction."""
import time
import uuid
from database.db import get_conn, get_cursor
from database.pgcompat import atomic
from fulfillment.router import route_fulfillment
from reseller_api.services.order_delivery import complete_manual_delivery, has_stored_delivery
from reseller_api.services.order_failure import handle_post_debit_failure
from reseller_api.services.order_idempotency import claim_worker_job, WORKER_JOB_TTL_SECONDS
from reseller_api.services.order_status import persisted_terminal_status

_CATEGORIES = {'timeout','connection','tls','dns','4xx','5xx','3xx','unknown','ssrf_blocked'}


def _fail_order(order_id, category):
    # Durable automatic intent also covers a post-status crash during initial
    # API dispatch. The worker repairs/refunds and sends its best-effort notice.
    with atomic(get_conn()):
        get_cursor().execute('INSERT INTO settings(key,value) VALUES(?,?) '
            'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
            (f'fulfillment.failure.{int(order_id)}',category))
    return handle_post_debit_failure(order_id,reason='fulfillment_error: '+category)


def dispatch_order(order_id, product_id, quantity):
    """Only called for newly committed external/supplier orders, never replay.

    Pending is durably represented by order + delivery.pending outbox and
    encrypted order/supplier reference metadata for the reconciliation worker.
    A late network-only result never performs financial/delivery writes.
    """
    routed = route_fulfillment(order_id,product_id,quantity)
    ftype = routed['type'];result = routed.get('result') or {}
    status = result.get('status')
    public = {'type':ftype,'status':'pending'}
    if status=='pending':
        if result.get('reference'):
            from fulfillment.references import persist_reference
            try:
                persist_reference(order_id,result['reference'])
            except Exception:
                # Initial correlation is already committed; status lookup by
                # stable idempotency/order ID recovers a lost supplier ref.
                pass
        return public
    terminal_error = status=='terminal_error' or (status=='retryable_error' and result.get('exhausted'))
    if status!='fulfilled' and not terminal_error:
        return public
    # Same claim key used by owner fail/cancel/deliver, held ONLY around local
    # lifecycle writes, never around external I/O. Release is owner-qualified.
    cur = get_cursor();conn = get_conn()
    key = f'admin-order-lifecycle:{int(order_id)}';token = uuid.uuid4().hex
    cur.execute('SELECT heartbeat_at FROM worker_jobs WHERE job_key=?',(key,));existing = cur.fetchone()
    if existing and int(existing[0])>=int(time.time())-WORKER_JOB_TTL_SECONDS:
        return public
    if not claim_worker_job(key,token):
        return public
    try:
        cur.execute('SELECT master_status,delivery FROM orders WHERE id=?',(int(order_id),))
        row = cur.fetchone()
        if not row or persisted_terminal_status(row[0]):
            return {'type':ftype,'status':'terminal_error'}
        if has_stored_delivery(row[1]):
            return {'type':ftype,'status':'fulfilled'}
        if terminal_error:
            category = result.get('category')
            if category not in _CATEGORIES:
                category = 'unknown'
            _fail_order(order_id,category)
            return {'type':ftype,'status':'terminal_error'}
        content = result.get('delivery')
        if not isinstance(content,str) or not content.strip() or len(content)>10000:
            _fail_order(order_id,'unknown')
            return {'type':ftype,'status':'terminal_error'}
        # Reuse 16.4 atomic encryption/write/status/strict outbox behavior.
        complete_manual_delivery(order_id,content)
        return {'type':ftype,'status':'fulfilled'}
    except Exception:
        # Local completion failure stays pending. A financial-engine crash
        # may already have persisted Failed; report that terminal state, never
        # claim the refund succeeded. Its existing recovery path can resume.
        try:
            cur.execute('SELECT master_status FROM orders WHERE id=?',(int(order_id),))
            latest = cur.fetchone()
            if latest and persisted_terminal_status(latest[0]):
                return {'type':ftype,'status':'terminal_error'}
        except Exception:
            pass
        return public
    finally:
        cur.execute('DELETE FROM worker_jobs WHERE job_key=? AND worker_id=?',(key,token))
        conn.commit()
