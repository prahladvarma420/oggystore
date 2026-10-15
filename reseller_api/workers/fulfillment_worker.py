"""Dedicated bounded supplier/generic reconciliation; never the webhook worker."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone, timedelta
import json
import logging
import math
import os
import random
import signal
import threading
import uuid

log = logging.getLogger(__name__)


def _validate_runtime():
    if os.getenv('RESELLER_API_REQUIRE_POSTGRES','').lower() in ('1','true','yes'):
        if not os.getenv('DATABASE_URL','').lower().startswith(('postgres://','postgresql://')):
            raise RuntimeError('Shared PostgreSQL configuration is required.')
    if not (os.getenv('DATA_ENCRYPTION_KEY') or '').strip():
        raise RuntimeError('Delivery/reference encryption is required.')


# Fail closed BEFORE legacy database import-time bootstrap can create SQLite.
if __name__=='__main__':
    try:
        _validate_runtime()
    except Exception:
        print('fulfillment-worker: configuration unavailable; startup refused.')
        raise SystemExit(1)

from database.db import get_conn, get_cursor, get_setting
from database.pgcompat import atomic
from fulfillment.router import dispatch_client, route_fulfillment
from fulfillment.references import persist_reference, decode_reference
from fulfillment.external_clients.generic_api_client import load_config, validate_config
from fulfillment.external_clients.supplier_api_client import StatusClient, validate_supplier_config
from reseller_api.services.order_idempotency import claim_worker_job, WORKER_JOB_TTL_SECONDS
from reseller_api.services.order_status import persisted_terminal_status, status_filter_sql
from reseller_api.services.order_delivery import has_stored_delivery, complete_manual_delivery
from reseller_api.services.order_failure import handle_post_debit_failure
from reseller_api.services.order_notifications import notify_order_failed
from utils.timezone import now as shop_now


def _env_int(name, default, minimum, maximum):
    try:
        value = int(os.getenv(name,str(default)))
        if not minimum<=value<=maximum:
            raise ValueError()
        return value
    except Exception:
        raise RuntimeError('Invalid fulfillment worker configuration.') from None

POLL_INTERVAL_SECONDS = _env_int('FULFILLMENT_POLL_INTERVAL',5,1,60)
BATCH_SIZE = _env_int('FULFILLMENT_BATCH_SIZE',20,1,100)
MAX_AGE_SECONDS = _env_int('FULFILLMENT_MAX_AGE_SECONDS',86400,1,31536000)
BACKOFF_BASE_SECONDS = _env_int('FULFILLMENT_BACKOFF_BASE',15,1,3600)
BACKOFF_CAP_SECONDS = _env_int('FULFILLMENT_BACKOFF_CAP',3600,1,86400)
CLAIM_TTL_SECONDS = 120
_STOP = threading.Event()
_CATEGORY = {'timeout','connection','tls','dns','4xx','5xx','3xx','unknown','ssrf_blocked',
             'dispatch_budget','dispatch_busy','not_found','reference_invalid','database','expired'}


def _clock():
    return shop_now()


def _epoch():
    return _clock().timestamp()


def _set(key,value):
    get_cursor().execute('INSERT INTO settings(key,value) VALUES(?,?) '
                         'ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,str(value)))


def _delete_state(oid):
    with atomic(get_conn()):
        get_cursor().execute('DELETE FROM settings WHERE key IN (?,?,?)',
            (f'fulfillment.next.{oid}',f'fulfillment.attempt.{oid}',f'fulfillment.failure.{oid}'))


def _schedule(oid,category='unknown'):
    key = f'fulfillment.attempt.{oid}'
    try:
        attempt = min(30,max(0,int(get_setting(key) or 0)))
    except (TypeError,ValueError):
        attempt = 0
    window = min(BACKOFF_CAP_SECONDS,BACKOFF_BASE_SECONDS*2**attempt)
    next_at = math.ceil(_epoch()+random.uniform(1,max(1,window)))
    with atomic(get_conn()):
        _set(key,attempt+1);_set(f'fulfillment.next.{oid}',next_at)
    safe = category if category in _CATEGORY else 'unknown'
    log.info('order_id=%s category=%s',int(oid),safe)


def _not_terminal_sql():
    return 'NOT ('+status_filter_sql('failed')+' OR '+status_filter_sql('cancelled')+')'


def _bootstrap_missing_references():
    """Bounded recovery for 16.6 pending and pre-dispatch crashes, not delivery cleanup."""
    cur = get_cursor()
    cur.execute("SELECT orders.id FROM orders JOIN products p ON p.id=orders.product_id "
        "WHERE orders.source='reseller_api' AND (orders.fulfillment_ref IS NULL OR orders.fulfillment_ref='') "
        "AND (orders.delivery IS NULL OR orders.delivery='') AND p.fulfillment_type IN ('external_api','supplier_api') "
        'AND '+_not_terminal_sql()+' ORDER BY orders.id LIMIT ?', (BATCH_SIZE,))
    for (oid,) in cur.fetchall():
        persist_reference(int(oid))


def _poll_candidates():
    # Cursor + LIMIT and indexed settings-key lookups prevent starvation and
    # unbounded Python loading. No (source,fulfillment_ref) index at this scale.
    try:
        after_id = max(0,int(get_setting('fulfillment.scan.cursor') or 0))
    except (TypeError,ValueError):
        after_id = 0
    due = int(_epoch())
    cutoff = (_clock()-timedelta(seconds=MAX_AGE_SECONDS)).astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    def read(cursor):
        cur = get_cursor()
        cur.execute("SELECT orders.id FROM orders LEFT JOIN settings due ON due.key='fulfillment.next.' || CAST(orders.id AS TEXT) "
            "WHERE orders.id>? AND orders.source='reseller_api' "
            "AND orders.fulfillment_ref IS NOT NULL AND orders.fulfillment_ref<>'' "
            "AND (orders.delivery IS NULL OR orders.delivery='') AND "+_not_terminal_sql()+
            " AND (due.value IS NULL OR CAST(due.value AS BIGINT)<=? OR orders.created<=?) "
            'ORDER BY orders.id LIMIT ?', (cursor,due,cutoff,BATCH_SIZE))
        return [int(r[0]) for r in cur.fetchall()]
    rows = read(after_id)
    if not rows and after_id:
        rows = read(0)
    with atomic(get_conn()):
        _set('fulfillment.scan.cursor',rows[-1] if rows else 0)
    return rows


def _read(oid):
    cur = get_cursor()
    cur.execute('SELECT orders.id,orders.product_id,orders.quantity,orders.created,orders.master_status,orders.delivery,'
        'orders.fulfillment_ref,p.fulfillment_type,p.fulfillment_config,orders.reseller_id,orders.product_name '
        'FROM orders LEFT JOIN products p ON p.id=orders.product_id WHERE orders.id=? AND orders.source=?',(int(oid),'reseller_api'))
    row = cur.fetchone()
    return dict(zip(('id','product_id','quantity','created','master_status','delivery','ref','type','config','reseller_id','product_name'),row)) if row else None


def _expired(row):
    # v1 created timestamps are UTC (SQLite CURRENT_TIMESTAMP and deployed PG
    # UTC session contract). Convert to the same aware shop clock explicitly.
    created = datetime.fromisoformat(str(row['created']).replace('Z','+00:00'))
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (_clock()-created.astimezone(_clock().tzinfo)).total_seconds()>=MAX_AGE_SECONDS


def _lifecycle(oid,callback,*,allow_failed=False):
    token = uuid.uuid4().hex;key = f'admin-order-lifecycle:{int(oid)}'
    cur = get_cursor()
    cur.execute('SELECT heartbeat_at FROM worker_jobs WHERE job_key=?',(key,));existing = cur.fetchone()
    if existing and int(existing[0])>=int(_epoch())-WORKER_JOB_TTL_SECONDS:
        return False
    if not claim_worker_job(key,token):
        return False
    try:
        row = _read(oid)
        if not row or has_stored_delivery(row['delivery']):
            return False
        terminal = persisted_terminal_status(row['master_status'])
        if terminal and not (allow_failed and terminal=='failed'):
            return False
        callback(row)
        return True
    finally:
        cur.execute('DELETE FROM worker_jobs WHERE job_key=? AND worker_id=?',(key,token));get_conn().commit()


def _failure(oid,reason):
    # Persist intent BEFORE the existing engine's multi-step crash-resumable
    # failure path. Only automatic intent rows are repaired; admin terminal
    # orders without such an intent are never polled or refunded by this worker.
    outcome = {}
    def apply(row):
        with atomic(get_conn()):
            _set(f'fulfillment.failure.{oid}',reason)
        result = handle_post_debit_failure(oid,reason='fulfillment_error: '+reason)
        outcome.update(row=row,result=result)
    if not _lifecycle(oid,apply,allow_failed=True):
        return False
    row = outcome['row']
    if persisted_terminal_status(_read(oid)['master_status'])!='failed':
        return False
    cur = get_cursor()
    from database.adjustment_db import get_refund_by_ledger_reference
    refund = get_refund_by_ledger_reference(f'rapi.refund.order.{oid}')
    amount = int(refund['amount_cents']) if refund else 0
    cur.execute('SELECT telegram_user_id FROM resellers WHERE id=?',(row['reseller_id'],))
    linked = cur.fetchone();tg_uid = linked[0] if linked else None
    # At-most-once best effort; marking before send can lose a notification on
    # process death, never a refund. No DB transaction spans Telegram.
    with atomic(get_conn()):
        key = f'fulfillment.notified.{oid}'
        cur.execute('SELECT value FROM settings WHERE key=?',(key,));notified=cur.fetchone()
        if not notified:
            _set(key,'1')
    _delete_state(oid)
    if tg_uid and not notified:
        notify_order_failed(int(tg_uid),oid,{'product_name':''},'order failed, refunded',amount)
    log.info('order_id=%s category=failed',int(oid))
    return True


def _process_order(oid):
    token=uuid.uuid4().hex;key=f'fulfillment-job:{int(oid)}'
    if not claim_worker_job(key,token,ttl_seconds=CLAIM_TTL_SECONDS):
        return False
    try:
        row=_read(oid)
        if not row:
            _delete_state(oid);return False
        intent=get_setting(f'fulfillment.failure.{oid}')
        terminal=persisted_terminal_status(row['master_status'])
        if terminal:
            if terminal=='failed' and intent:
                return _failure(oid,str(intent) if str(intent) in _CATEGORY else 'unknown')
            _delete_state(oid);return False
        if has_stored_delivery(row['delivery']):
            _delete_state(oid);return False
        if _expired(row):
            return _failure(oid,'expired')
        if row['type'] not in ('external_api','supplier_api'):
            _schedule(oid,'unknown');return False
        cfg=load_config(row['config'])
        cfg=(validate_supplier_config if row['type']=='supplier_api' else validate_config)(cfg,resolve=False)
        cfg={**cfg,'retries':0}  # durable worker backoff, not tight in-call retry
        reference=decode_reference(row['ref'])
        if reference['kind']=='order' and reference['value']!=f'ORD-{int(oid):08d}':
            raise ValueError()
        ref_value=reference['value'] if reference['kind']=='supplier' else None
        result=dispatch_client(StatusClient(cfg,ref_value,supplier=row['type']=='supplier_api'),cfg,oid,int(row['quantity']))
        if result.get('category')=='not_found':
            # A pre-dispatch/late-response crash is repaired only by the SAME
            # stable idempotency key. Providers MUST honor this contract.
            result=route_fulfillment(oid,int(row['product_id']),int(row['quantity']))['result']
        if result.get('reference'):
            persist_reference(oid,result['reference'])
        status=result.get('status')
        if status=='fulfilled':
            def complete(latest):
                complete_manual_delivery(oid,result['delivery'])
            if _lifecycle(oid,complete):
                _delete_state(oid);log.info('order_id=%s category=completed',int(oid));return True
            _schedule(oid,'database');return False
        if status=='terminal_error' and result.get('category')!='unknown':
            category=result.get('category');return _failure(oid,category if category in _CATEGORY else 'unknown')
        _schedule(oid,result.get('category','unknown'));return False
    except Exception:
        _schedule(oid,'unknown')
        return False
    finally:
        cur=get_cursor();cur.execute('DELETE FROM worker_jobs WHERE job_key=? AND worker_id=?',(key,token));get_conn().commit()


def run_once():
    _bootstrap_missing_references()
    # Bounded repair of auto-failure intents (including post-status crashes).
    cur=get_cursor();cur.execute("SELECT orders.id FROM orders JOIN settings s ON s.key='fulfillment.failure.' || CAST(orders.id AS TEXT) "
        "LEFT JOIN settings due ON due.key='fulfillment.next.' || CAST(orders.id AS TEXT) "
        "WHERE orders.source='reseller_api' AND (due.value IS NULL OR CAST(due.value AS BIGINT)<=?) ORDER BY orders.id LIMIT ?",(int(_epoch()),max(1,BATCH_SIZE//2)))
    repairs=[int(r[0]) for r in cur.fetchall()]
    rows=list(dict.fromkeys(repairs+_poll_candidates()))[:BATCH_SIZE]
    for oid in rows:
        try:_process_order(oid)
        except Exception:log.warning('order_id=%s category=database',int(oid))
    return len(rows)


def _shutdown(signum,frame):
    _STOP.set()


def run_forever(*,once=False):
    _validate_runtime()
    from database.db import init_db, close_thread_connection
    init_db()
    if once:
        try:return run_once()
        finally:close_thread_connection()
    signal.signal(signal.SIGTERM,_shutdown);signal.signal(signal.SIGINT,_shutdown)
    log.info('category=started')
    try:
        while not _STOP.is_set():
            try:run_once()
            except Exception:log.warning('category=database')
            _STOP.wait(POLL_INTERVAL_SECONDS)
    finally:
        close_thread_connection();log.info('category=stopped')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--once',action='store_true');args=parser.parse_args()
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    try:run_forever(once=args.once)
    except Exception:
        log.error('category=startup_unavailable');raise SystemExit(1)
