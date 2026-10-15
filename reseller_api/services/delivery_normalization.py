"""Opt-in, bounded empty-delivery cleanup; never invoked at startup/read time."""
import json
from database.pgcompat import atomic
from utils.crypto import _fernet

STATE_KEY = 'reseller_empty_delivery_normalization_v1'


def normalization_ready():
    from database.db import get_setting
    try:
        state = json.loads(get_setting(STATE_KEY) or '{}')
        return isinstance(state, dict) and state.get('ready') is True
    except (TypeError, ValueError):
        return False


def _save(state, cur):
    # Existing settings table; caller owns transaction. Never set_setting(),
    # whose internal commit would split the cleanup batch.
    cur.execute('INSERT INTO settings(key,value) VALUES(?,?) '
                         'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                         (STATE_KEY, json.dumps(state, separators=(',', ':'))))


def normalize_empty_deliveries(*, apply=False, batch_size=100, max_rows=5000, resume=False, connection=None):
    """Dry-run by default; apply changes only authenticated enc: empty strings.

    Limits bound memory/transactions and rows per invocation. Resume state is
    written ONLY by explicit apply. No key/token/decoded content is returned.
    Readiness is promoted only after an entire cutoff is verified, without
    unreadable ciphertext or a concurrent change; otherwise filters stay shut.
    Deploy NULL-writing code and stop legacy writers before this maintenance.
    """
    if not 1 <= batch_size <= 500 or not 1 <= max_rows <= 50000:
        raise ValueError('batch_size must be 1..500 and max_rows 1..50000')
    if connection is None:
        from database.db import get_conn
        connection = get_conn()
    conn = connection
    cur = conn.cursor()
    if resume:
        try:
            cur.execute('SELECT value FROM settings WHERE key=?', (STATE_KEY,))
            saved = cur.fetchone()
            previous = json.loads(saved[0] if saved else '{}')
        except (TypeError, ValueError):
            previous = {}
        if not previous or previous.get('ready'):
            raise ValueError('No unfinished apply run is available to resume')
        state = dict(previous)
    else:
        cur.execute('SELECT COALESCE(MAX(id),0) FROM orders')
        state = {'ready': False, 'cutoff': int(cur.fetchone()[0]), 'cursor': 0,
                 'scanned': 0, 'empty': 0, 'changed': 0, 'unreadable': 0, 'raced': 0}
    if apply:
        with atomic(conn):
            _save(state, cur)  # Explicitly invalidate any earlier certificate.
    consumed = 0
    cipher = _fernet()
    while consumed < max_rows:
        cur.execute("SELECT id,delivery FROM orders WHERE id>? AND id<=? "
                    "AND source='reseller_api' ORDER BY id LIMIT ?",
                    (state['cursor'], state['cutoff'], min(batch_size, max_rows-consumed)))
        rows = cur.fetchall()
        if not rows:
            break
        empty_rows = []
        for oid, value in rows:
            if isinstance(value, str) and value.startswith('enc:'):
                try:
                    if cipher is None:
                        raise ValueError('Encryption configuration unavailable')
                    plain = cipher.decrypt(value[4:].encode('ascii')).decode('utf-8')
                    if plain == '':
                        empty_rows.append((int(oid), value))
                except Exception:
                    state['unreadable'] += 1
            state['cursor'] = int(oid)
            state['scanned'] += 1
            consumed += 1
        state['empty'] += len(empty_rows)
        if apply:
            with atomic(conn):
                for oid, observed in empty_rows:
                    cur.execute("UPDATE orders SET delivery=NULL WHERE id=? "
                                "AND source='reseller_api' AND delivery=?", (oid, observed))
                    if cur.rowcount == 1:
                        state['changed'] += 1
                    else:
                        state['raced'] += 1
                _save(state, cur)
    cur.execute("SELECT id FROM orders WHERE id>? AND id<=? AND source='reseller_api' LIMIT 1",
                (state['cursor'], state['cutoff']))
    finished = cur.fetchone() is None
    # Completion certificate is meaningful only for an applied run. A dry
    # run may have identified empties but has deliberately not removed them.
    state['ready'] = bool(apply and finished and not state['unreadable'] and not state['raced'])
    if apply:
        with atomic(conn):
            _save(state, cur)
    return {**state, 'dry_run': not apply, 'finished': finished,
            'rows_this_invocation': consumed, 'needs_resume': not finished}
