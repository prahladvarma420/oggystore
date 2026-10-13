"""Private reseller adapters for the EXISTING invite queues and browser lane.

Runs only in the bot's maintenance process, alongside customer browser jobs.
No API browser threads, new queues/tables/columns or duplicated browser logic.
"""
import asyncio
import os
import time
import uuid
from database.db import get_conn,get_cursor
from database.pgcompat import atomic
from utils.crypto import encrypt_text,_fernet
from utils.email_privacy import protected
from services import pw_lane

_TABLE={'m365':'m365_invites','canva':'canva_invites'}
CLAIM_TTL=900

def is_api_order(oid):
    cur=get_cursor();cur.execute('SELECT source FROM orders WHERE id=?',(int(oid),));row=cur.fetchone()
    return bool(row and row[0]=='reseller_api')

def prepare_queue(kind,email):
    """Initialize only an existing queue OUTSIDE the order atomic transaction."""
    from database import db
    if not (os.getenv('DATA_ENCRYPTION_KEY') or '').strip():
        raise ValueError('Private fulfillment storage unavailable.')
    if kind=='m365':db.ensure_m365_table()
    elif kind=='canva':db.ensure_canva_table()
    else:raise ValueError('Private fulfillment type unavailable.')
    return encrypt_text(email,force=True)

def enqueue_atomic(kind,oid,encrypted_email,pid):
    """Caller owns order/debit atomic block. Never commits or decrypts here."""
    if not encrypted_email.startswith('enc:'):raise ValueError('Private fulfillment storage unavailable.')
    cur=get_cursor()
    if kind=='m365':
        from database.db import get_m365_product_warranty
        cur.execute("INSERT INTO m365_invites(user_id,order_id,email,status,warranty) VALUES(0,?,?,'api_pending',?)",
                    (int(oid),encrypted_email,get_m365_product_warranty(pid)))
    else:
        cur.execute("INSERT INTO canva_invites(user_id,order_id,email,status) VALUES(0,?,?,'api_pending')",(int(oid),encrypted_email))

def _read(kind,iid):
    table=_TABLE[kind];cur=get_cursor()
    cur.execute(f'SELECT i.id,i.order_id,i.email,i.status,o.created,o.master_status,o.delivery '
                f'FROM {table} i JOIN orders o ON o.id=i.order_id WHERE i.id=? AND o.source=?',(int(iid),'reseller_api'))
    row=cur.fetchone()
    return dict(zip(('id','oid','email','status','created','master_status','delivery'),row)) if row else None

def _status(kind,iid,status):
    with atomic(get_conn()):get_cursor().execute(f'UPDATE {_TABLE[kind]} SET status=?,error=NULL WHERE id=?',(status,int(iid)))

def _due(oid):
    from database.db import get_setting
    try:return float(get_setting(f'email.next.{oid}') or 0)<=time.time()
    except Exception:return True

def _defer(oid):
    with atomic(get_conn()):
        get_cursor().execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                            (f'email.next.{oid}',str(int(time.time())+60)))

def _environment_safe():
    # Playwright driver subprocess debug output bypasses Python log filters.
    # Refuse API browser work in such an environment, without changing the
    # existing customer/runtime environment or exposing email to driver logs.
    return not any(os.getenv(k) for k in ('DEBUG','PWDEBUG','DEBUGP'))

def _complete(kind,iid,oid):
    from reseller_api.workers import fulfillment_worker as worker
    from reseller_api.services.order_delivery import complete_manual_delivery
    label='Microsoft 365 Family' if kind=='m365' else 'Canva'
    if worker._lifecycle(oid,lambda row:complete_manual_delivery(oid,f'{label} invitation sent. Check the buyer inbox (including spam) and accept the invitation.')):
        _status(kind,iid,'api_completed');return True
    return False

def _fail(kind,iid,oid,category):
    from reseller_api.workers import fulfillment_worker as worker
    if worker._failure(oid,category):
        _status(kind,iid,'api_failed');return True
    return False

def _execute(kind,iid):
    """Already owns pw_lane; unknown remote outcomes are never blindly resent."""
    from reseller_api.workers import fulfillment_worker as worker
    from reseller_api.services.order_idempotency import claim_worker_job
    from reseller_api.services.order_status import persisted_terminal_status
    from reseller_api.services.order_delivery import has_stored_delivery
    row=_read(kind,iid)
    if not row:return False
    oid=int(row['oid']);key=f'email-invite:{oid}';token=uuid.uuid4().hex
    if not claim_worker_job(key,token,ttl_seconds=CLAIM_TTL):return False
    try:
        row=_read(kind,iid)
        if persisted_terminal_status(row['master_status']) or has_stored_delivery(row['delivery']):
            _status(kind,iid,'api_closed');return False
        # Success was persisted before local completion: resume WITHOUT inviting.
        if row['status']=='api_sent':return _complete(kind,iid,oid)
        if row['status']=='api_rejected':return _fail(kind,iid,oid,'4xx')
        if worker._expired(row):return _fail(kind,iid,oid,'expired')
        if row['status'] in ('api_running','api_uncertain'):
            # Browser side effects have no provider idempotency receipt. A
            # stale running/crashed job requires owner review; max age still
            # fails/refunds. Never silently repeat an ambiguous invitation.
            _status(kind,iid,'api_uncertain');_defer(oid);return False
        if not _due(oid) or not _environment_safe():return False
        cipher=_fernet()
        if not cipher or not isinstance(row['email'],str) or not row['email'].startswith('enc:'):
            _defer(oid);return False
        try:email=cipher.decrypt(row['email'][4:].encode('ascii')).decode('utf-8')
        except Exception:_defer(oid);return False
        _status(kind,iid,'api_running')
        try:
            with protected():
                if kind=='m365':
                    from services import m365_family
                    from database.db import get_m365_invite_warranty
                    ok,_,_,queueable=m365_family._invite_with_failover_locked(email,get_m365_invite_warranty(iid))
                else:
                    from services import canva_invite
                    if not canva_invite.has_session():
                        ok,queueable=False,True
                    else:
                        ok,_,_=canva_invite._send_canva_with_retries_locked(email);queueable=False
        finally:
            # Do not leave API proof screenshots for later interactive retries.
            from services import m365_family,canva_invite
            target=m365_family if kind=='m365' else canva_invite
            target._LAST_SCREENSHOT=None
        # Discard raw provider messages/screenshots; they may contain email.
        if ok:
            _status(kind,iid,'api_sent')
            return _complete(kind,iid,oid)
        if queueable:
            _status(kind,iid,'api_pending');_defer(oid);return False
        _status(kind,iid,'api_rejected')
        return _fail(kind,iid,oid,'4xx')
    except Exception:
        # No raw exception logging. Preserve api_sent to retry local commit.
        latest=_read(kind,iid)
        if latest and latest['status']=='api_running':_status(kind,iid,'api_uncertain')
        _defer(oid);return False
    finally:
        get_cursor().execute('DELETE FROM worker_jobs WHERE job_key=? AND worker_id=?',(key,token));get_conn().commit()

def _run_owned(kind,iid):
    try:return _execute(kind,iid)
    finally:
        from database.db import close_thread_connection
        close_thread_connection();pw_lane.release()

def _local_call(callback,*args):
    try:return callback(*args)
    finally:
        from database.db import close_thread_connection
        close_thread_connection()

async def process_invite(kind,iid):
    # Age/refund and sent-receipt recovery are local operations and must still
    # progress when a permanently busy browser lane cannot be acquired.
    row=_read(kind,iid)
    if not row:return False
    from reseller_api.workers import fulfillment_worker as worker
    from reseller_api.services.order_status import persisted_terminal_status
    from reseller_api.services.order_delivery import has_stored_delivery
    if persisted_terminal_status(row['master_status']) or has_stored_delivery(row['delivery']):
        _status(kind,iid,'api_closed');return False
    if row['status']=='api_sent':
        return await asyncio.to_thread(_local_call,_complete,kind,iid,int(row['oid']))
    if row['status']=='api_rejected':
        return await asyncio.to_thread(_local_call,_fail,kind,iid,int(row['oid']),'4xx')
    if worker._expired(row):
        return await asyncio.to_thread(_local_call,_fail,kind,iid,int(row['oid']),'expired')
    # Acquire BEFORE creating a browser thread; never stack a thread behind a
    # hung/abandoned browser job. API response never executes this function.
    if not pw_lane.acquire('API email invite',wait_seconds=0):return False
    task=None
    try:
        task=asyncio.create_task(asyncio.to_thread(_run_owned,kind,int(iid)))
        return await asyncio.wait_for(asyncio.shield(task),timeout=300)
    except asyncio.TimeoutError:
        # Thread still owns lane and releases only when it actually exits.
        # Next maintenance pass cannot stack another browser thread.
        return False
    except BaseException:
        if task is None:pw_lane.release()
        raise

async def drain_pending(limit=5):
    """Called ONLY by existing bot maintenance; API/supplier worker does not launch Chromium."""
    from database import db
    db.ensure_m365_table();db.ensure_canva_table()
    from database.db import get_setting
    try:cursor=max(0,int(get_setting('email.scan.cursor') or 0))
    except Exception:cursor=0
    maximum=min(25,max(1,int(limit)))
    from reseller_api.workers import fulfillment_worker as worker
    eligible=" AND (o.delivery IS NULL OR o.delivery='') AND "+worker._not_terminal_sql().replace('orders.','o.')
    def select(after):
        cur=get_cursor();cur.execute("SELECT kind,iid,oid FROM ("
            "SELECT 'm365' AS kind,i.id AS iid,o.id AS oid FROM m365_invites i JOIN orders o ON o.id=i.order_id "
            "WHERE o.source='reseller_api' AND i.status IN ('api_pending','api_running','api_sent','api_uncertain','api_rejected') "
             +eligible+" UNION ALL SELECT 'canva' AS kind,i.id AS iid,o.id AS oid FROM canva_invites i JOIN orders o ON o.id=i.order_id "
            "WHERE o.source='reseller_api' AND i.status IN ('api_pending','api_running','api_sent','api_uncertain','api_rejected')"
            +eligible+") jobs WHERE oid>? ORDER BY oid LIMIT ?",(after,maximum))
        return cur.fetchall()
    selected=select(cursor)
    if not selected and cursor:selected=select(0)
    rows=[(kind,iid) for kind,iid,_ in selected]
    with atomic(get_conn()):
        get_cursor().execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
            ('email.scan.cursor',str(selected[-1][2] if selected else 0)))
    for kind,iid in rows:
        await process_invite(kind,int(iid))
    return len(rows)
