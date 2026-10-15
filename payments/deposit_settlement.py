"""Shared payment ownership, allocation and atomic customer settlement.

No network here. Both wallet namespaces claim the EXISTING processed_txs row
inside their settlement transaction. A settings row serializes allocation /
settlement on PostgreSQL; SQLite's outer BEGIN IMMEDIATE does that already.
No extra claim table, customer table or ledger type is introduced.
"""
from decimal import Decimal
from datetime import timedelta
import random,secrets,string,re
from database import db,pgcompat
from utils.timezone import now,now_text
from config import DEPOSIT_EXPIRE_HOURS,PAYMENT_AMOUNT_TOLERANCE

METHODS=('bep20','trc20','binance')
TOL=Decimal(str(PAYMENT_AMOUNT_TOLERANCE))

def namespace_lock(cur):
    cur.execute("SELECT value FROM settings WHERE key='payment.namespace.lock'" + (' FOR UPDATE' if pgcompat.use_postgres() else ''))
    if not cur.fetchone():raise RuntimeError('Payment namespace is not initialized.')

def payment_id(method,value,*,fallback=False):
    if method not in METHODS:raise ValueError('Unsupported payment method.')
    v=str(value or '')
    if fallback and method=='bep20' and re.fullmatch(r'balance-delta-[1-9][0-9]*',v):return v
    if method=='bep20' and re.fullmatch(r'0x[0-9a-fA-F]{64}',v):return v.lower()
    if method=='trc20' and re.fullmatch(r'[0-9a-fA-F]{64}',v):return v.lower()
    if method=='binance' and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9:_.-]{0,99}',v):return v
    raise ValueError('Invalid verified payment identifier.')

def allocate(cur,amount,method):
    """Same suffix/memo alphabet as customers; shared lock caller, no network."""
    for _ in range(25):
        value=round(float(amount)+random.randint(1000,9999)/1000000,6)
        # Disjoint tolerance windows prevent cross-invoice ambiguous matches.
        cutoff=(now()-timedelta(days=2)).strftime('%Y-%m-%d %H:%M:%S')
        # Keep identifiers reserved over the trusted feeds' existing 2-day
        # lookback: a late cancelled invoice must not fund a newer wallet.
        cur.execute("SELECT unique_amount FROM deposits WHERE status='pending' OR created>=?",(cutoff,))
        values=[Decimal(str(x[0])) for x in cur.fetchall() if x[0] is not None]
        cur.execute("SELECT expected_amount FROM reseller_deposits WHERE status='pending' OR created_at>=?",(cutoff,))
        values += [Decimal(str(x[0])) for x in cur.fetchall()]
        if all(abs(Decimal(str(value))-x)>2*TOL for x in values):break
    else:raise ValueError('No unique invoice amount is available; retry later.')
    memo=None
    if method=='binance':
        for _ in range(25):
            memo=''.join(secrets.choice(string.ascii_uppercase+string.digits) for _ in range(8))
            cur.execute('SELECT 1 FROM deposits WHERE note_code=?',(memo,));a=cur.fetchone()
            cur.execute('SELECT 1 FROM reseller_deposits WHERE memo=?',(memo,));b=cur.fetchone()
            if not a and not b:break
        else:raise ValueError('No unique invoice memo is available; retry later.')
    return value,memo

def matches(method,paid,expected,memo=None,notes=''):
    paid=Decimal(str(paid));expected=Decimal(str(expected))
    if not paid.is_finite() or paid<=0:return False
    if method=='binance':return bool(memo and str(memo).upper() in str(notes).upper()) and paid>=expected-TOL
    return abs(paid-expected)<=TOL

def claim(cur,method,txid,deposit_id,user_id,paid):
    """Global raw-ID semantics retained; legacy rows without marker fail closed."""
    lookup='LOWER(tx_hash)=?' if method!='binance' else 'tx_hash=?'
    cur.execute('SELECT 1 FROM processed_txs WHERE '+lookup,(txid,))
    if cur.fetchone():return False
    cur.execute('SELECT 1 FROM deposits WHERE '+lookup+' AND status=\'paid\'',(txid,))
    if cur.fetchone():return False
    lookup2='LOWER(txid)=?' if method!='binance' else 'txid=?'
    cur.execute('SELECT 1 FROM reseller_deposits WHERE '+lookup2+' AND status=\'paid\'',(txid,))
    if cur.fetchone():return False
    cur.execute('INSERT OR IGNORE INTO processed_txs(tx_hash,deposit_id,user_id,amount) VALUES(?,?,?,?)',(txid,deposit_id,user_id,float(paid)))
    return cur.rowcount==1

def customer_invoice(uid,amount,method):
    with pgcompat.atomic(db.get_conn()):
        c=db.get_cursor();namespace_lock(c)
        c.execute("UPDATE deposits SET status='cancelled' WHERE user_id=? AND status='pending' AND checkout_product_id IS NULL",(uid,))
        value,memo=allocate(c,amount,method)
        c.execute("INSERT INTO deposits(user_id,amount,unique_amount,status,created,method,note_code) VALUES(?,?,?,'pending',?,?,?)",(uid,amount,value,now_text(),method,memo))
        return int(c.lastrowid),value,memo

def settle_customer(depid,method,txid,paid,*,notes='',fallback=False):
    """Verify saved matching/eligibility again; credit OR deliver in one commit.

    Existing reserve_stock_items is reused without its standalone commit.
    Screens/notifications are still sent by the original checkers AFTER commit.
    """
    txid=payment_id(method,txid,fallback=fallback)
    with pgcompat.atomic(db.get_conn()):
        c=db.get_cursor();namespace_lock(c)
        c.execute('SELECT user_id,amount,unique_amount,status,created,method,note_code,checkout_product_id,checkout_quantity,checkout_done FROM deposits WHERE id=?'+(' FOR UPDATE' if pgcompat.use_postgres() else ''),(depid,))
        row=c.fetchone()
        if not row:return {'posted':False}
        uid,base,expected,st,created,saved_method,memo,pid,qty,done=row
        if st!='pending' or (saved_method or 'bep20')!=method or str(created)<(now()-timedelta(hours=DEPOSIT_EXPIRE_HOURS)).strftime('%Y-%m-%d %H:%M:%S'):
            return {'posted':False}
        if not matches(method,paid,base if method=='binance' else expected,memo,notes):return {'posted':False}
        if fallback:
            # Without transaction evidence, pooled reseller funds cannot safely
            # be separated from customer funds. Hold for logs/owner review while
            # any reseller invoice/evidence exists on this receiving address.
            c.execute("SELECT 1 FROM reseller_deposits WHERE method='bep20' LIMIT 1")
            if c.fetchone():return {'posted':False,'review':True}
        if not claim(c,method,txid,depid,uid,paid):return {'posted':False}
        result={'posted':True,'deliveries':None,'product_name':None,'product_exists':False,'order_id':None}
        if pid and int(qty or 0)>0 and not done and not fallback:
            c.execute('SELECT name FROM products WHERE id=? AND active=1',(pid,));product=c.fetchone()
            result['product_exists']=bool(product)
            if product:
                result['product_name']=str(product[0]);result['deliveries']=db.reserve_stock_items(pid,uid,int(qty),commit=False)
                if result['deliveries']:
                    text='\n\n'.join(map(str,result['deliveries']))
                    if method=='binance':
                        try:
                            from products.product_handlers import format_delivery_items
                            text=format_delivery_items(result['deliveries'])
                        except Exception:pass
                    from utils.crypto import encrypt_text
                    c.execute('UPDATE products SET stock=? WHERE id=?',(db.get_available_stock_count(pid),pid))
                    c.execute('INSERT INTO orders(user_id,product_name,price,delivery,payment_method) VALUES(?,?,?,?,?)',(uid,f"{result['product_name']} x{int(qty)}",float(base),encrypt_text(text),{'bep20':'USDT BEP20','trc20':'USDT TRC20','binance':'Binance Pay'}[method]))
                    result.update(order_id=int(c.lastrowid),delivery_text=text)
        if not result['deliveries']:
            credit=min(Decimal(str(paid)),Decimal(str(base))) if method=='binance' and not pid else Decimal(str(base))
            c.execute('UPDATE users SET balance=balance+? WHERE user_id=?',(float(credit),uid))
            if c.rowcount!=1:raise ValueError('Customer identity not found.')
        c.execute("UPDATE deposits SET status='paid',tx_hash=?,paid_at=?,checkout_done=? WHERE id=? AND status='pending'",(txid,now_text(),1 if pid or fallback else int(done or 0),depid))
        if c.rowcount!=1:raise RuntimeError('Deposit eligibility changed.')
        return result


def settle_customer_safely(*args, **kwargs):
    """Checker-only isolation: one invoice failure must not kill the whole scan."""
    try:
        return settle_customer(*args, **kwargs)
    except Exception:
        import logging
        logging.getLogger(__name__).warning("customer deposit settlement deferred deposit_id=%s", args[0])
        return {"posted": False, "deferred": True}
