"""Persistent reseller deposits and independently observed payment settlement.

No client payment proof accepted. Scanners below consume the existing trusted
provider feeds. Amount/expiry rules mirror customer helpers. Customer and reseller
settlement share processed_txs and the same transaction namespace lock.
"""
import html,json,re
from datetime import timedelta
from decimal import Decimal
from database import db,pgcompat
import database.reseller_db as rdb
from utils.timezone import now,now_text
from .deposit_settlement import METHODS,TOL,namespace_lock,allocate,payment_id,matches,claim

COLS=('id','reseller_id','method','amount','expected_amount','actual_amount','address','memo','status','txid','expires_at','created_at','confirmed_at','credited_amount_cents','ledger_transaction_id','audit_logged','notification_status')
class DepositError(ValueError):pass

def active(uid):
    from handlers.reseller_handlers import get_active_reseller
    res=get_active_reseller(int(uid))
    if not res:raise DepositError('Active reseller access required.')
    return res

def active_or_none(uid):
    from handlers.reseller_handlers import get_active_reseller
    return get_active_reseller(int(uid))

def rails():
    from payments.deposit_handlers import _available_methods
    return [(m,label) for m,label in _available_methods() if m in METHODS]

def destination(method):
    import config
    value={'bep20':config.USDT_BEP20_ADDRESS,'trc20':config.USDT_TRC20_ADDRESS,'binance':config.BINANCE_PAY_ID}.get(method)
    if not value:raise DepositError('Payment method is unavailable.')
    value=str(value)
    if method=='bep20' and not re.fullmatch(r'0x[0-9a-fA-F]{40}',value):raise DepositError('Payment destination is not configured correctly.')
    if method=='trc20' and not re.fullmatch(r'T[1-9A-HJ-NP-Za-km-z]{33}',value):raise DepositError('Payment destination is not configured correctly.')
    if method=='binance' and not re.fullmatch(r'[0-9]{1,30}',value):raise DepositError('Payment destination is not configured correctly.')
    return value

def row(depid,*,lock=False):
    c=db.get_cursor();c.execute('SELECT '+','.join(COLS)+' FROM reseller_deposits WHERE id=?'+(' FOR UPDATE' if lock and pgcompat.use_postgres() else ''),(int(depid),));v=c.fetchone()
    return dict(zip(COLS,v)) if v else None

def expiry(c,rid=None):
    sql="UPDATE reseller_deposits SET status='expired' WHERE status='pending' AND expires_at<?";args=[now_text()]
    if rid is not None:sql+=' AND reseller_id=?';args.append(rid)
    c.execute(sql,tuple(args));return c.rowcount

def clear_expired_navigation():
    """Navigation is disposable; do not clear an amount draft or newer session."""
    from utils.states import user_state, RES_TOPUP_PAYMENT
    c=db.get_cursor()
    c.execute("SELECT r.telegram_user_id FROM resellers r WHERE EXISTS(SELECT 1 FROM reseller_deposits d WHERE d.reseller_id=r.id AND d.status='expired') AND NOT EXISTS(SELECT 1 FROM reseller_deposits p WHERE p.reseller_id=r.id AND p.status='pending')")
    for (uid,) in c.fetchall():
        if user_state.get(uid)==RES_TOPUP_PAYMENT:user_state.pop(uid,None)

def create_session(uid,method,amount):
    res=active(uid)
    if method not in dict(rails()):raise DepositError('Unsupported or unavailable payment method.')
    try:
        value=Decimal(str(amount))
        if not value.is_finite() or value<Decimal('.1') or value>5000 or value!=value.quantize(Decimal('.01')):raise ValueError()
    except Exception:raise DepositError('Enter 0.10–5000.00 USDT, with at most two decimals.') from None
    with pgcompat.atomic(db.get_conn()):
        c=db.get_cursor();namespace_lock(c);res=active(uid)
        # Active gate is repeated under reseller row lock, not only at entry.
        c.execute('SELECT status FROM resellers WHERE id=?'+(' FOR UPDATE' if pgcompat.use_postgres() else ''),(res['id'],))
        if c.fetchone()[0]!='active':raise DepositError('Active reseller access required.')
        expiry(c,res['id']);c.execute("SELECT id,method FROM reseller_deposits WHERE reseller_id=? AND status='pending'",(res['id'],));current=c.fetchone()
        if current:
            if current[1]!=method:raise DepositError('Cancel the existing session before choosing a different method.')
            return row(current[0])
        expected,memo=allocate(c,float(value),method)
        if method=='binance':expected=float(value)
        stamp=now_text();end=(now()+timedelta(hours=__import__('config').DEPOSIT_EXPIRE_HOURS)).strftime('%Y-%m-%d %H:%M:%S')
        c.execute("INSERT INTO reseller_deposits(reseller_id,method,amount,expected_amount,address,memo,status,expires_at,created_at) VALUES(?,?,?,?,?,?,'pending',?,?)",(res['id'],method,float(value),expected,destination(method),memo,end,stamp))
        return row(c.lastrowid)

def session(uid,depid=None):
    res=active(uid)
    error=None
    with pgcompat.atomic(db.get_conn()):
        c=db.get_cursor();namespace_lock(c);active(uid);expiry(c,res['id'])
        if depid is None:
            c.execute("SELECT id FROM reseller_deposits WHERE reseller_id=? AND status='pending'",(res['id'],));current=c.fetchone()
            saved=row(current[0]) if current else None
        else:
            saved=row(depid,lock=True)
            if not saved or saved['reseller_id']!=res['id']:error='Deposit not found.'
            elif saved['status']!='pending':error='This payment session is no longer payable.'
    clear_expired_navigation()
    if error:raise DepositError(error)
    return saved


def cancel_session(uid,depid):
    res=active(uid);error=None
    with pgcompat.atomic(db.get_conn()):
        c=db.get_cursor();namespace_lock(c);active(uid);expiry(c,res['id']);saved=row(depid,lock=True)
        if not saved or saved['reseller_id']!=res['id']:error='Deposit not found.'
        elif saved['status']!='pending':error='Deposit is no longer cancellable.'
        else:
            c.execute("UPDATE reseller_deposits SET status='cancelled' WHERE id=? AND status='pending'",(depid,))
            if c.rowcount!=1:raise DepositError('Deposit state changed; refresh your wallet.')
    if error:raise DepositError(error)
    from utils.states import user_state
    user_state.pop(uid,None)
    return True


def credit_verified(depid,method,txid,paid,*,address,notes=''):
    """INTERNAL scanner seam, not user/API input; source already verifies asset.

    Never call with a user payment claim. Shared atomic claim, immutable credit,
    saved confirmation and security event all roll back together. Suspended funds
    are credited; subsequent spending authorization remains the existing policy.
    """
    txid=payment_id(method,txid);paid=Decimal(str(paid))
    with pgcompat.atomic(db.get_conn()):
        c=db.get_cursor();namespace_lock(c);saved=row(depid,lock=True)
        if not saved:return {'result':'not_found'}
        if saved['method']!=method or str(saved['address'])!=str(address):return {'result':'mismatch'}
        if saved['status']=='paid':
            return {'result':'duplicate' if saved['txid']==txid else 'conflict','deposit':saved}
        if saved['status']!='pending' or saved['expires_at']<now_text():
            if saved['status']=='pending':c.execute("UPDATE reseller_deposits SET status='expired' WHERE id=?",(depid,))
            return {'result':'late_review'}
        if not matches(method,paid,saved['expected_amount'],saved['memo'],notes):return {'result':'unmatched'}
        if not claim(c,method,txid,-int(depid),None,paid):return {'result':'payment_used'}
        reference=f'dep.{method}.{txid}'
        credited=min(paid,Decimal(str(saved['amount']))) if method=='binance' else Decimal(str(saved['amount']))
        cents=rdb.to_cents(str(credited));description=f"Verified deposit #{int(depid)} ({method})"
        outcome,ledger_id=rdb.post_wallet_transaction(int(saved['reseller_id']),'topup_credit',cents,reference,description)
        if outcome!='posted':raise DepositError('Deposit credit did not post; retry or review ledger.')
        c.execute('SELECT status FROM resellers WHERE id=?'+(' FOR UPDATE' if pgcompat.use_postgres() else ''),(saved['reseller_id'],));status=c.fetchone()[0]
        c.execute("UPDATE reseller_deposits SET status='paid',actual_amount=?,txid=?,confirmed_at=?,credited_amount_cents=?,ledger_transaction_id=? WHERE id=? AND status='pending'",(float(paid),txid,now_text(),cents,ledger_id,depid))
        if c.rowcount!=1:raise DepositError('Deposit eligibility changed.')
        rdb.log_security_event(saved['reseller_id'],'reseller_deposit_credited',description=json.dumps({'deposit_id':int(depid),'method':method,'txid':txid,'credited_cents':cents,'reference':reference,'suspended':status=='suspended'},separators=(',',':')),commit=False)
        return {'result':'posted','deposit':row(depid)}

def has_receiving_records(method):
    c=db.get_cursor();c.execute("SELECT 1 FROM reseller_deposits WHERE method=? AND (status='pending' OR created_at>=?) LIMIT 1",(method,(now()-timedelta(days=2)).strftime('%Y-%m-%d %H:%M:%S')));return bool(c.fetchone())

async def process_observations(context,method,observations):
    """Provider fetch has completed before this function; no HTTP in DB atomic."""
    with pgcompat.atomic(db.get_conn()):
        c=db.get_cursor();namespace_lock(c);expiry(c)
    clear_expired_navigation()
    c=db.get_cursor();c.execute("SELECT id FROM reseller_deposits WHERE method=? AND (status='pending' OR created_at>=?) ORDER BY id LIMIT 500",(method,(now()-timedelta(days=2)).strftime('%Y-%m-%d %H:%M:%S')))
    deposits=[row(x[0]) for x in c.fetchall()]
    complete = True
    for txid,paid,notes in observations:
        for saved in deposits:
            try:
                if not matches(method,paid,saved['expected_amount'],saved['memo'],notes):continue
                result=credit_verified(saved['id'],method,txid,paid,address=destination(method),notes=notes)
                if result['result']=='late_review':
                    # Dedup review breadcrumbs in existing settings, not a credit.
                    key=f'deposit.review.{method}.{saved["id"]}.{payment_id(method,txid)}'
                    with pgcompat.atomic(db.get_conn()):
                        cur=db.get_cursor();namespace_lock(cur);cur.execute('INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)',(key,now_text()))
                        if cur.rowcount: rdb.log_security_event(saved['reseller_id'],'reseller_deposit_late_review',description=f'deposit={saved["id"]} method={method} txid={txid}',commit=False)
            except Exception:
                complete = False
                import logging
                logging.getLogger(__name__).warning('reseller deposit settlement deferred deposit_id=%s method=%s',saved['id'],method)
    # Defined by 17.2; no financial logic in Telegram handlers.
    await post_credit_actions(context)
    return complete

async def post_credit_actions(context):
    """Post-commit out-of-band sink. Log retryable, notification at-most-once attempt.

    Audit flag+owner log are atomic. Claim notification BEFORE sending: a crash
    after claim can lose an optional notification, but cannot produce duplicates.
    """
    c=db.get_cursor();c.execute("SELECT id FROM reseller_deposits WHERE status='paid' AND (audit_logged=0 OR notification_status IS NULL) ORDER BY id LIMIT 50")
    ids=[x[0] for x in c.fetchall()]
    for depid in ids:
        try:
            with pgcompat.atomic(db.get_conn()):
                c=db.get_cursor();saved=row(depid,lock=True)
                reference=f'dep.{saved["method"]}.{saved["txid"]}'
                if not saved['audit_logged']:
                    db.write_admin_action('reseller_deposit_credit',f'reseller={saved["reseller_id"]} deposit={depid} method={saved["method"]} txid={saved["txid"]} credited_cents={saved["credited_amount_cents"]} reference={reference} result=posted actor=payment-checker')
                    c.execute('UPDATE reseller_deposits SET audit_logged=1 WHERE id=?',(depid,))
                send=saved['notification_status'] is None
                if send:c.execute("UPDATE reseller_deposits SET notification_status='attempted' WHERE id=?",(depid,))
            if send:
                res=rdb.get_reseller(saved['reseller_id']);uid=res.get('telegram_user_id')
                from utils.states import user_state
                if str(user_state.get(uid,'')).startswith('res_topup:'):user_state.pop(uid,None)
                if uid:
                    balance=rdb.from_cents(rdb.get_wallet_balance_cents(saved['reseller_id']))
                    try:
                        await context.bot.send_message(chat_id=uid,text='✅ <b>Reseller wallet credited</b>\n'+f'Credit: <b>{html.escape(rdb.from_cents(saved["credited_amount_cents"]))} USDT</b>\nBalance: <b>{html.escape(balance)} USDT</b>\nTX: <code>{html.escape(saved["txid"])}</code>\nReference: <code>{html.escape(reference)}</code>',parse_mode='HTML')
                    except Exception:pass
        except Exception:
            import logging
            logging.getLogger(__name__).warning('deposit post-credit actions deferred deposit_id=%s',depid)
