"""Framework-neutral atomic reseller wallet service."""
import re
from dataclasses import dataclass
import database.reseller_db as rdb
from reseller_api.services.authorization import BALANCE_READ, ORDERS_CREATE, authorize_operation

_REF=re.compile(r'^[A-Za-z0-9][A-Za-z0-9:_.-]{0,127}$')
class WalletError(ValueError):
    def __init__(self, code, message, balance_cents=None, transaction_id=None):
        super().__init__(code); self.code=code; self.message=message; self.balance_cents=balance_cents; self.transaction_id=transaction_id
@dataclass(frozen=True)
class WalletResult:
    status:str; transaction_id:int; balance_cents:int; currency:str='USDT'
    @property
    def balance(self): return rdb.from_cents(self.balance_cents)
def _rid(v):
    if isinstance(v,bool): raise WalletError('invalid_reseller','A valid reseller is required.')
    try: v=int(v)
    except Exception: raise WalletError('invalid_reseller','A valid reseller is required.') from None
    if v<=0 or not rdb.get_reseller(v): raise WalletError('invalid_reseller','A valid reseller is required.')
    return v
def _cents(v):
    if isinstance(v,(bool,float)): raise WalletError('invalid_amount','Amount must be a positive decimal value.')
    try: c=rdb.to_cents(v)
    except Exception: raise WalletError('invalid_amount','Amount must be a positive decimal value.') from None
    if c<=0: raise WalletError('invalid_amount','Amount must be greater than zero.')
    if c > rdb.MAX_WALLET_BALANCE_CENTS: raise WalletError('invalid_amount','Amount exceeds the supported wallet limit.')
    return c
def _ref(v):
    v=str(v or '').strip()
    if not _REF.fullmatch(v): raise WalletError('invalid_reference','A safe idempotency reference is required.')
    return v
def get_balance(reseller_id):
    c=rdb.get_wallet_balance_cents(_rid(reseller_id)); return {'balance_cents':c,'balance':rdb.from_cents(c),'currency':'USDT'}
def _post(rid,typ,cents,ref,description='',related=None):
    rid=_rid(rid); ref=_ref(ref)
    if related is not None: related=_ref(related)
    if len(str(description or ''))>500: raise WalletError('invalid_description','Description is too long.')
    status,data=rdb.post_wallet_transaction(rid,typ,cents,ref,str(description or ''),related_order_reference=related)
    if status=='insufficient_funds': raise WalletError('insufficient_funds','The reseller wallet has insufficient funds.',int(data))
    if status=='balance_limit_exceeded': raise WalletError('balance_limit_exceeded','The wallet balance limit would be exceeded.',int(data))
    if status=='idempotency_conflict':
        raise WalletError('idempotency_conflict','This reference was already used for a different wallet request.',transaction_id=int(data) if data else None)
    row=rdb.get_wallet_transaction_by_reference(rid,typ,ref)
    return WalletResult(status,int(data or row['id']),int(row['balance_after_cents']))
def credit_wallet(reseller_id,amount,reference,description='',adjustment=False): return _post(reseller_id,'adjustment_credit' if adjustment else 'topup_credit',_cents(amount),reference,description)
def debit_wallet(reseller_id,amount,reference,description='',adjustment=False,related_order_reference=None): return _post(reseller_id,'adjustment_debit' if adjustment else 'purchase_debit',-_cents(amount),reference,description,related_order_reference)
def refund_wallet(reseller_id,amount,reference,description='',related_order_reference=None):
    rid=_rid(reseller_id); ref=_ref(reference); cents=_cents(amount)
    original=rdb.get_wallet_transaction_by_reference(rid,'purchase_debit',ref)
    if not original: raise WalletError('refund_not_eligible','No matching wallet debit exists.')
    if int(original['amount_cents']) != -cents: raise WalletError('invalid_refund_amount','Refund must match the original debit.')
    return _post(rid,'refund_credit',cents,ref,description,related_order_reference or ref)
def get_authenticated_balance(context,environment):
    d=authorize_operation(context,BALANCE_READ,environment)
    if not d.allowed: raise WalletError('authorization_denied','This operation is not permitted.')
    return get_balance(context.reseller_id)
def debit_authenticated_wallet(context,amount,reference,environment,description='',related_order_reference=None):
    d=authorize_operation(context,ORDERS_CREATE,environment)
    if not d.allowed: raise WalletError('authorization_denied','This operation is not permitted.')
    return debit_wallet(context.reseller_id,amount,reference,description,related_order_reference=related_order_reference)
