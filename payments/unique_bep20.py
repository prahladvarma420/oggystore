"""Permanent reseller addresses: exact accounting and isolated chain scanner.

No production custody adapter is installed. RPC/custody are injected boundaries;
only a future reviewed adapter may supply owned inventory and acceptance evidence.
There are no web/API entry points accepting payment claims.
"""
from dataclasses import dataclass
import hashlib
import json
import os
import re
from database import db, pgcompat
from database import reseller_db as rdb
from payments.deposit_settlement import namespace_lock
from utils.timezone import now_text

TRANSFER = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
MAX_UNITS = 2**256 - 1

class DepositUnavailable(ValueError):
    """Safe public error: never include provider errors or configuration values."""


def address(value):
    if not isinstance(value, str) or not re.fullmatch(r'0x[0-9a-fA-F]{40}', value):
        raise DepositUnavailable('Invalid deposit address.')
    if int(value[2:], 16) == 0:
        raise DepositUnavailable('Invalid deposit address.')
    return value.lower()


def hash32(value):
    if isinstance(value, (bytes, bytearray)):
        value = '0x' + value.hex()
    if not isinstance(value, str) or not re.fullmatch(r'0x[0-9a-fA-F]{64}', value):
        raise DepositUnavailable('Invalid blockchain identifier.')
    return value.lower()


def units(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not re.fullmatch(r'0|[1-9][0-9]{0,77}', str(value)):
        raise DepositUnavailable('Invalid exact token amount.')
    result = int(value)
    if result > MAX_UNITS or (positive and not result):
        raise DepositUnavailable('Invalid exact token amount.')
    return result


def integer(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise DepositUnavailable('Invalid blockchain number.')
    try:
        result = int(value, 16) if isinstance(value, str) and value.startswith('0x') else int(value)
    except (ValueError, TypeError):
        raise DepositUnavailable('Invalid blockchain number.') from None
    if result < 0:
        raise DepositUnavailable('Invalid blockchain number.')
    return result


@dataclass(frozen=True)
class Settings:
    token: str
    provider: str
    wallet_ref: str
    start_block: int
    minimum_units: int
    confirmations: int = 12
    chain_id: int = 56
    decimals: int = 18
    batch_blocks: int = 100

    def __post_init__(self):
        object.__setattr__(self, 'token', address(self.token))
        if (self.chain_id != 56 or self.decimals != 18 or any(isinstance(v,bool) for v in (self.start_block,self.confirmations,self.batch_blocks,self.minimum_units)) or
                not isinstance(self.start_block, int) or self.start_block < 1 or
                not isinstance(self.confirmations, int) or not 5 <= self.confirmations <= 10000 or
                not isinstance(self.batch_blocks, int) or not 1 <= self.batch_blocks <= 500 or
                not re.fullmatch(r'[a-z0-9_-]{1,40}', self.provider) or
                not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', self.wallet_ref)):
            raise DepositUnavailable('Invalid permanent deposit configuration.')
        units(self.minimum_units, positive=True)

    @property
    def key(self):
        # Do not permit changing asset/account/confirmation policy underneath a checkpoint.
        data = (self.chain_id, self.token, self.decimals, self.provider,
                self.wallet_ref, self.start_block, self.minimum_units, self.confirmations)
        return hashlib.sha256(json.dumps(data).encode()).hexdigest()


def requested():
    return os.getenv('RESELLER_BEP20_UNIQUE_ENABLED', '').lower() in ('1', 'true', 'yes')


def load_settings():
    import config
    try:
        if address(os.environ['RESELLER_BEP20_TOKEN']) != address(config.USDT_BEP20_CONTRACT):
            raise ValueError()
        return Settings(token=os.environ['RESELLER_BEP20_TOKEN'],
            provider=os.environ['RESELLER_BEP20_PROVIDER'], wallet_ref=os.environ['RESELLER_BEP20_WALLET_REF'],
            start_block=int(os.environ['RESELLER_BEP20_START_BLOCK']),
            minimum_units=units(os.environ['RESELLER_BEP20_MINIMUM_UNITS'], positive=True),
            confirmations=int(os.getenv('RESELLER_BEP20_CONFIRMATIONS', '12')))
    except (KeyError, ValueError, TypeError):
        raise DepositUnavailable('Permanent deposits are not configured.') from None


def production_adapter(settings):
    # Deliberately no env-selected dynamic import, guessed provider IDs or fake wallets.
    raise DepositUnavailable('Permanent deposits await verified custody integration.')


def runtime():
    if not requested():
        raise DepositUnavailable('Permanent deposits are disabled.')
    cfg = load_settings()
    return cfg, production_adapter(cfg)


def active(uid):
    from handlers.reseller_handlers import get_active_reseller
    res = get_active_reseller(int(uid))
    if not res:
        raise DepositUnavailable('Active reseller access required.')
    return res


def _rows(cur):
    names = [col[0] for col in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def _one(sql, args=()):
    c = db.get_cursor(); c.execute(sql, args)
    rows = _rows(c)
    return rows[0] if rows else None


def import_inventory(cfg, adapter):
    """Adapter must reconcile ALL pages; return owned addresses, never generate keys.

    HTTP finishes before DB transaction. An uncertain/failed inventory read produces
    no partial imports or assignments. No blind retry of provider address creation.
    """
    try:
        inventory = list(adapter.inventory(cfg))
        if len(inventory) > 10000:
            raise ValueError()
        normalized = []
        import config
        legacy = (config.USDT_BEP20_ADDRESS or '').lower()
        for item in inventory:
            a = address(item['address'])
            ref = item['provider_ref']
            if (a == legacy or item['chain_id'] != cfg.chain_id or item['wallet_ref'] != cfg.wallet_ref or
                    not isinstance(ref, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', ref)):
                raise ValueError()
            normalized.append((a, ref))
        if len(set(a for a, _ in normalized)) != len(normalized) or len(set(r for _, r in normalized)) != len(normalized):
            raise ValueError()
    except Exception:
        raise DepositUnavailable('Custody inventory unavailable; retry later.') from None
    with pgcompat.atomic(db.get_conn()):
        c = db.get_cursor(); namespace_lock(c)
        for a, ref in normalized:
            current = _one('SELECT * FROM reseller_bep20_addresses WHERE address=? OR provider_ref=?', (a, ref))
            if current:
                if any(current[k] != v for k, v in {'address': a, 'provider_ref': ref,
                    'chain_id': cfg.chain_id, 'token': cfg.token, 'decimals': cfg.decimals,
                    'provider': cfg.provider, 'wallet_ref': cfg.wallet_ref}.items()):
                    raise DepositUnavailable('Custody inventory ownership conflict.')
                continue
            c.execute('''INSERT INTO reseller_bep20_addresses(address,provider,wallet_ref,provider_ref,
                chain_id,token,decimals,discovered_at) VALUES(?,?,?,?,?,?,?,?)''',
                (a,cfg.provider,cfg.wallet_ref,ref,cfg.chain_id,cfg.token,cfg.decimals,now_text()))
    return len(normalized)


def allocate_address(uid, cfg):
    """Serialized pool assignment; no provider HTTP or private key handling."""
    res = active(uid)
    with pgcompat.atomic(db.get_conn()):
        c = db.get_cursor(); namespace_lock(c)
        res = active(uid)
        c.execute('SELECT status FROM resellers WHERE id=?' + (' FOR UPDATE' if pgcompat.use_postgres() else ''), (res['id'],))
        if c.fetchone()[0] != 'active':
            raise DepositUnavailable('Active reseller access required.')
        saved = _one('SELECT * FROM reseller_bep20_addresses WHERE reseller_id=?', (res['id'],))
        if saved:
            if any(saved[k] != getattr(cfg, k) for k in ('provider','wallet_ref','chain_id','token','decimals')):
                raise DepositUnavailable('Existing address requires operator review.')
            return saved
        saved = _one('''SELECT * FROM reseller_bep20_addresses WHERE reseller_id IS NULL
            AND provider=? AND wallet_ref=? AND chain_id=? AND token=? AND decimals=? ORDER BY address LIMIT 1''',
            (cfg.provider,cfg.wallet_ref,cfg.chain_id,cfg.token,cfg.decimals))
        if not saved:
            raise DepositUnavailable('Address provisioning pending. Please try again later.')
        c.execute('UPDATE reseller_bep20_addresses SET reseller_id=?,assigned_at=? WHERE address=? AND reseller_id IS NULL',
                  (res['id'],now_text(),saved['address']))
        if c.rowcount != 1:
            raise DepositUnavailable('Address allocation changed; retry.')
        return _one('SELECT * FROM reseller_bep20_addresses WHERE reseller_id=?', (res['id'],))


def exact_text(raw, decimals=18):
    value = units(raw); scale = 10**decimals
    whole, fraction = divmod(value, scale)
    return str(whole) + ('.' + str(fraction).zfill(decimals).rstrip('0') if fraction else '')


def account_view(uid, cfg):
    saved = allocate_address(uid, cfg)
    c = db.get_cursor()
    c.execute('SELECT * FROM reseller_bep20_events WHERE address=? ORDER BY block_number DESC,log_index DESC LIMIT 5', (saved['address'],))
    events = _rows(c)
    carry = _one('SELECT raw_units FROM reseller_bep20_carry WHERE reseller_id=?', (saved['reseller_id'],))
    return {'assignment': saved, 'events': events, 'remainder_units': carry['raw_units'] if carry else '0',
            'balance_cents': rdb.get_wallet_balance_cents(saved['reseller_id'])}


@dataclass(frozen=True)
class Transfer:
    chain_id: int
    token: str
    tx_hash: str
    log_index: int
    recipient: str
    raw_units: int
    block_number: int
    block_hash: str

    @property
    def key(self):
        return hashlib.sha256(f'{self.chain_id}:{self.token}:{self.tx_hash}:{self.log_index}'.encode()).hexdigest()


def _decode(log, cfg):
    if log.get('removed') not in (None, False):
        raise DepositUnavailable('Removed transfer.')
    token = address(log['address'])
    topics = [hash32(t) for t in log['topics']]
    if token != cfg.token or len(topics) != 3 or topics[0] != TRANSFER or topics[2][2:26] != '0'*24:
        raise DepositUnavailable('Unsupported transfer.')
    raw = log['data']
    if isinstance(raw, bytes): raw = '0x' + raw.hex()
    if not isinstance(raw,str) or not re.fullmatch(r'0x[0-9a-fA-F]{64}', raw):
        raise DepositUnavailable('Invalid transfer value.')
    value = int(raw,16); units(value,positive=True)
    return Transfer(cfg.chain_id, token, hash32(log['transactionHash']), integer(log['logIndex']),
                    address('0x'+topics[2][-40:]), value, integer(log['blockNumber']), hash32(log['blockHash']))


def verify_transfer(rpc, log, cfg):
    """RPC boundary; receipt and exact receipt-log match, never client proof."""
    try:
        if integer(rpc.eth.chain_id) != cfg.chain_id:
            raise ValueError()
        event = _decode(log, cfg)
        if integer(rpc.eth.block_number) - event.block_number + 1 < cfg.confirmations:
            raise ValueError()
        receipt = rpc.eth.get_transaction_receipt(event.tx_hash)
        if (integer(receipt['status']) != 1 or hash32(receipt['transactionHash']) != event.tx_hash or
                integer(receipt['blockNumber']) != event.block_number or hash32(receipt['blockHash']) != event.block_hash):
            raise ValueError()
        matching = [x for x in receipt['logs'] if integer(x['logIndex']) == event.log_index]
        if len(matching) != 1 or _decode(matching[0], cfg) != event:
            raise ValueError()
        block = rpc.eth.get_block(event.block_number)
        if hash32(block['hash']) != event.block_hash:
            raise ValueError()
        return event
    except Exception:
        raise DepositUnavailable('Transfer verification pending or invalid.') from None


def legacy_used(c, tx):
    for table, col, tail in [('processed_txs','tx_hash',''),('deposits','tx_hash'," AND status='paid'"),
                             ('reseller_deposits','txid'," AND status='paid'")]:
        c.execute('SELECT 1 FROM '+table+' WHERE LOWER('+col+')=?'+tail, (tx,))
        if c.fetchone(): return True
    return False


def settle_transfer(cfg, event, adapter):
    """INTERNAL verified-RPC seam. Custody evidence fetched outside transaction.

    Adapter acceptance MUST corroborate exact network/token/hash/log/address/raw units,
    ownership and final spendable custody acceptance; status strings alone are not proof.
    """
    if (event.chain_id != cfg.chain_id or event.token != cfg.token or
            event.raw_units != units(event.raw_units, positive=True) or event.log_index < 0):
        raise DepositUnavailable('Unsupported transfer.')
    if (hash32(event.tx_hash)!=event.tx_hash or hash32(event.block_hash)!=event.block_hash or
            address(event.recipient)!=event.recipient or isinstance(event.log_index,bool) or
            not isinstance(event.log_index,int) or not isinstance(event.block_number,int) or event.block_number<1):
        raise DepositUnavailable('Invalid canonical transfer identity.')
    saved = _one('SELECT * FROM reseller_bep20_addresses WHERE address=?', (event.recipient,))
    if not saved or saved['reseller_id'] is None:
        raise DepositUnavailable('Transfer ownership unavailable.')
    if any(saved[k] != getattr(cfg,k) for k in ('provider','wallet_ref','chain_id','token','decimals')):
        raise DepositUnavailable('Transfer ownership conflict.')
    try:
        evidence = adapter.acceptance(cfg, event, saved['provider_ref'])
        expected = {'chain_id': event.chain_id, 'token': event.token, 'tx_hash': event.tx_hash,
                    'log_index': event.log_index, 'address': event.recipient, 'raw_units': str(event.raw_units),
                    'wallet_ref': cfg.wallet_ref}
        if any(evidence.get(k) != v for k,v in expected.items()): raise ValueError()
        state = evidence['status']
        if state not in ('accepted','pending','rejected'): raise ValueError()
        ref = evidence.get('reference')
        if not isinstance(ref,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}',ref):raise ValueError()
    except Exception:
        raise DepositUnavailable('Custody verification unavailable; retry later.') from None
    with pgcompat.atomic(db.get_conn()):
        c = db.get_cursor(); namespace_lock(c)
        scan = _one('SELECT paused FROM reseller_bep20_scan WHERE scan_key=?',(cfg.key,))
        if scan and scan['paused']:
            raise DepositUnavailable('Deposit scanner paused for review.')
        old = _one('SELECT * FROM reseller_bep20_events WHERE event_key=?',(event.key,))
        if old and (old['raw_units']!=str(event.raw_units) or old['address']!=event.recipient):
            if old['state']!='accounted':
                c.execute("UPDATE reseller_bep20_events SET state='review',category='identity_conflict' WHERE event_key=?",(event.key,))
                return 'review'
            raise DepositUnavailable('Accounted transfer identity changed; operator review required.')
        if old and old['state'] == 'accounted':
            if any(old[k] != v for k,v in {'raw_units':str(event.raw_units),'address':event.recipient,
                                          'block_hash':event.block_hash}.items()):
                raise DepositUnavailable('Accounted transfer changed; operator review required.')
            return 'duplicate'
        # All financial eligibility is rechecked while holding shared payment lock.
        if legacy_used(c,event.tx_hash):
            state='legacy_conflict'
        if event.raw_units < cfg.minimum_units:
            state='below_minimum'
        if old and old['state']=='review':
            return 'review'  # operator review is deliberately not auto-cleared
        category = 'accepted' if state=='accepted' else state
        if not old:
            c.execute('''INSERT INTO reseller_bep20_events(event_key,chain_id,token,tx_hash,log_index,address,
                raw_units,block_number,block_hash,state,category,provider_ref,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (event.key,event.chain_id,event.token,event.tx_hash,event.log_index,event.recipient,str(event.raw_units),
                 event.block_number,event.block_hash,'observed' if state=='pending' else 'review',category,ref,now_text()))
        else:
            c.execute('''UPDATE reseller_bep20_events SET block_number=?,block_hash=?,category=?,provider_ref=?
                WHERE event_key=?''',(event.block_number,event.block_hash,category,ref,event.key))
        if state != 'accepted':
            c.execute('UPDATE reseller_bep20_events SET state=?,category=? WHERE event_key=?',
                      ('observed' if state=='pending' else 'review',category,event.key))
            return 'pending' if state=='pending' else 'review'
        c.execute('INSERT OR IGNORE INTO reseller_bep20_claims(tx_hash,chain_id,created_at) VALUES(?,?,?)',
                  (event.tx_hash,cfg.chain_id,now_text()))
        rid = saved['reseller_id']
        c.execute('SELECT status FROM resellers WHERE id=?'+(' FOR UPDATE' if pgcompat.use_postgres() else ''),(rid,))
        status = c.fetchone()[0]  # suspended/closed incoming funds remain accountable
        carry = _one('SELECT * FROM reseller_bep20_carry WHERE reseller_id=?',(rid,))
        if carry and carry['token'] != cfg.token: raise DepositUnavailable('Carry token conflict.')
        before = units(carry['raw_units']) if carry else 0
        sequence = (carry['last_seq'] if carry else 0) + 1
        cents, after = divmod(before+event.raw_units,10**(cfg.decimals-2))
        ledger_id = None
        if cents:
            outcome, ledger_id = rdb.post_wallet_transaction(rid,'topup_credit',cents,'bep20.event.'+event.key,
                                                            'Verified permanent-address BEP20 deposit')
            if outcome != 'posted': raise DepositUnavailable('Ledger requires reconciliation.')
        c.execute('INSERT OR IGNORE INTO reseller_bep20_carry(reseller_id,token,raw_units,updated_at) VALUES(?,?,?,?)',
                  (rid,cfg.token,'0',now_text()))
        c.execute('UPDATE reseller_bep20_carry SET raw_units=?,last_seq=?,updated_at=? WHERE reseller_id=?',(str(after),sequence,now_text(),rid))
        c.execute('''UPDATE reseller_bep20_events SET state='accounted',category='accepted',credit_cents=?,
            carry_before=?,carry_after=?,account_seq=?,ledger_id=?,accounted_at=? WHERE event_key=?''',
            (cents,str(before),str(after),sequence,ledger_id,now_text(),event.key))
        rdb.log_security_event(rid,'reseller_bep20_accounted',description=json.dumps({
            'event_key':event.key,'credit_cents':cents,'suspended':status=='suspended'},separators=(',',':')),commit=False)
        return 'credited' if cents else 'retained'


def process_log(cfg, rpc, adapter, log):
    return settle_transfer(cfg,verify_transfer(rpc,log,cfg),adapter)


def reconcile_exact(rid, cfg):
    """Serialize the audit snapshot with all new-flow financial writers."""
    with pgcompat.atomic(db.get_conn()):
        namespace_lock(db.get_cursor())
        return _reconcile_exact_locked(rid, cfg)


def _reconcile_exact_locked(rid, cfg):
    """Exact-unit liability plus per-event ledger/carry chain audit; no float SUM."""
    c = db.get_cursor()
    c.execute('''SELECT e.* FROM reseller_bep20_events e JOIN reseller_bep20_addresses a ON a.address=e.address
        WHERE a.reseller_id=? AND e.state='accounted' ORDER BY e.account_seq''',(rid,))
    rows = _rows(c); raw = credits = 0; problems = []; previous=0
    for sequence,e in enumerate(rows,1):
        if e['account_seq']!=sequence or units(e['carry_before'])!=previous:
            problems.append('carry_chain')
        previous=units(e['carry_after'])
        raw += units(e['raw_units']); credits += int(e['credit_cents'])
        if units(e['carry_before'])+units(e['raw_units']) != e['credit_cents']*10**16+units(e['carry_after']):
            problems.append('event_conservation')
        if e['credit_cents']:
            ledger = _one('SELECT * FROM reseller_wallet_transactions WHERE id=?',(e['ledger_id'],))
            if not ledger or ledger['reseller_id']!=rid or ledger['tx_type']!='topup_credit' or ledger['currency']!='USDT' or ledger['amount_cents']!=e['credit_cents'] or ledger['reference']!='bep20.event.'+e['event_key']:
                problems.append('event_ledger')
        elif e['ledger_id'] is not None: problems.append('zero_ledger')
    carry = _one('SELECT * FROM reseller_bep20_carry WHERE reseller_id=?',(rid,))
    remaining = units(carry['raw_units']) if carry else 0
    if raw != credits*10**16+remaining or not 0<=remaining<10**16 or remaining!=previous:problems.append('account_conservation')
    if carry and (carry['last_seq']!=len(rows) or carry['token']!=cfg.token):problems.append('carry_metadata')
    return {'raw_units':str(raw),'credited_cents':credits,'remainder_units':str(remaining),'problems':problems}


def scan_once(cfg, rpc, adapter):
    """Bounded independent checkpoint; CAS fences concurrent scanner advances.

    Pending custody blocks progress to guarantee re-observation. Review records are
    retained and are never automatically credited. A changed anchor pauses the scan.
    """
    if integer(rpc.eth.chain_id)!=cfg.chain_id:
        raise DepositUnavailable('Wrong deposit network.')
    with pgcompat.atomic(db.get_conn()):
        c=db.get_cursor(); namespace_lock(c)
        c.execute('INSERT OR IGNORE INTO reseller_bep20_scan(scan_key,next_block,updated_at) VALUES(?,?,?)',
                  (cfg.key,cfg.start_block,now_text()))
        checkpoint=_one('SELECT * FROM reseller_bep20_scan WHERE scan_key=?',(cfg.key,))
    if checkpoint['paused']:raise DepositUnavailable('Deposit scanner paused for review.')
    start=checkpoint['next_block']
    if checkpoint['anchor_hash'] and hash32(rpc.eth.get_block(start-1)['hash'])!=checkpoint['anchor_hash']:
        with pgcompat.atomic(db.get_conn()):
            c=db.get_cursor(); namespace_lock(c)
            c.execute('UPDATE reseller_bep20_scan SET paused=1,updated_at=? WHERE scan_key=?',(now_text(),cfg.key))
        raise DepositUnavailable('Canonical checkpoint changed; operator review required.')
    end=min(start+cfg.batch_blocks-1,integer(rpc.eth.block_number)-cfg.confirmations+1)
    if end<start:return 'waiting_confirmations'
    addresses=[]
    c=db.get_cursor();c.execute('SELECT address FROM reseller_bep20_addresses WHERE reseller_id IS NOT NULL AND token=? AND provider=? AND wallet_ref=?',
                               (cfg.token,cfg.provider,cfg.wallet_ref))
    addresses=[r[0] for r in c.fetchall()]
    if not addresses:return 'waiting_addresses'  # never skip pre-assignment history
    batch_anchor=hash32(rpc.eth.get_block(end)['hash'])
    pending=False
    for offset in range(0,len(addresses),100):
        recipients=['0x'+'0'*24+a[2:] for a in addresses[offset:offset+100]]
        logs=list(rpc.eth.get_logs({'fromBlock':start,'toBlock':end,'address':cfg.token,
                                   'topics':[TRANSFER,None,recipients]}))
        if len(logs)>10000:raise DepositUnavailable('Deposit batch limit exceeded.')
        for log in logs:
            if not start<=integer(log['blockNumber'])<=end:raise DepositUnavailable('Out-of-range transfer.')
            # The RPC boundary is not trusted to honor its recipient filters.
            if _decode(log,cfg).recipient not in addresses[offset:offset+100]:raise DepositUnavailable('Unexpected transfer recipient.')
            result=process_log(cfg,rpc,adapter,log)
            pending |= result=='pending'
    anchor=hash32(rpc.eth.get_block(end)['hash'])
    if anchor!=batch_anchor:
        with pgcompat.atomic(db.get_conn()):
            c=db.get_cursor();namespace_lock(c)
            c.execute('UPDATE reseller_bep20_scan SET paused=1,updated_at=? WHERE scan_key=?',(now_text(),cfg.key))
        raise DepositUnavailable('Canonical scan range changed; operator review required.')
    if pending:return 'pending_custody'
    # Revalidate previous anchor after all external work, before checkpoint advance.
    if checkpoint['anchor_hash'] and hash32(rpc.eth.get_block(start-1)['hash'])!=checkpoint['anchor_hash']:
        raise DepositUnavailable('Canonical checkpoint changed during scan.')
    with pgcompat.atomic(db.get_conn()):
        c=db.get_cursor();namespace_lock(c)
        c.execute('''UPDATE reseller_bep20_scan SET next_block=?,anchor_hash=?,version=version+1,updated_at=?
            WHERE scan_key=? AND version=? AND paused=0''',(end+1,anchor,now_text(),cfg.key,checkpoint['version']))
        return 'advanced' if c.rowcount==1 else 'concurrent_retry'


def validate_rpc(cfg, rpc):
    """Worker startup verification: exact chain, deployed code and token decimals."""
    try:
        if integer(rpc.eth.chain_id)!=cfg.chain_id:raise ValueError()
        from web3 import Web3
        token=Web3.to_checksum_address(cfg.token)
        code=rpc.eth.get_code(token)
        if not code or code in ('0x',b''):raise ValueError()
        encoded=rpc.eth.call({'to':token,'data':'0x313ce567'})
        value=int.from_bytes(encoded,'big') if isinstance(encoded,bytes) else int(encoded,16)
        if value!=cfg.decimals:raise ValueError()
    except Exception:
        raise DepositUnavailable('Deposit RPC asset validation failed.') from None
