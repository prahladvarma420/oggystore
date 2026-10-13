"""Encrypted, versioned reference metadata in the ONE new nullable column."""
import json
import os
from database.db import get_conn, get_cursor
from database.pgcompat import atomic
from utils.crypto import encrypt_text, _fernet
from reseller_api.services.order_status import persisted_terminal_status
from reseller_api.services.order_delivery import has_stored_delivery
from fulfillment.external_clients.supplier_api_client import validate_reference


class ReferenceUnavailable(ValueError):
    def __init__(self):
        super().__init__('Fulfillment reference is unavailable.')


def encode_reference(order_id, reference=None):
    if not (os.getenv('DATA_ENCRYPTION_KEY') or '').strip():
        raise ReferenceUnavailable()
    kind = 'supplier' if reference is not None else 'order'
    value = validate_reference(reference) if reference is not None else f'ORD-{int(order_id):08d}'
    return encrypt_text(json.dumps({'v':1,'kind':kind,'value':value},separators=(',',':')),force=True)


def decode_reference(blob):
    try:
        if not isinstance(blob,str) or not blob.startswith('enc:'):
            raise ValueError()
        cipher = _fernet()
        data = json.loads(cipher.decrypt(blob[4:].encode('ascii')).decode('utf-8'))
        if data.get('v')!=1 or data.get('kind') not in ('order','supplier'):
            raise ValueError()
        validate_reference(data['value'])
        return data
    except Exception:
        raise ReferenceUnavailable() from None


def persist_reference(order_id, reference=None):
    """CAS upgrade correlation -> real reference; never overwrite another ref.

    Terminal/delivered orders are not rewritten. Delivery content never enters
    this function or column. All reference values are encrypted before storage.
    """
    encoded = encode_reference(order_id,reference)
    with atomic(get_conn()):
        cur = get_cursor()
        cur.execute('SELECT fulfillment_ref,source,master_status,delivery FROM orders WHERE id=?',(int(order_id),))
        row = cur.fetchone()
        if not row or row[1]!='reseller_api' or persisted_terminal_status(row[2]) or has_stored_delivery(row[3]):
            return False
        old = row[0]
        if old:
            existing = decode_reference(old)
            if reference is None:
                return False
            if existing['kind']=='supplier':
                if existing['value']!=reference:
                    raise ReferenceUnavailable()
                return False
        cur.execute("UPDATE orders SET fulfillment_ref=? WHERE id=? AND COALESCE(fulfillment_ref,'')=?",(encoded,int(order_id),old or ''))
        return cur.rowcount==1
