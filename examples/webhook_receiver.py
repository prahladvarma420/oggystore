"""Local Flask receiver: exact wire HMAC + durable SQLite inbox, no app imports.

See examples/README.md before deployment. Set WEBHOOK_SECRET and INBOX_DB.
The default processor is a metadata-only demonstration, not order fulfillment.
"""
from __future__ import annotations
import hashlib
import hmac
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

HEADER_SIGNATURE = 'X-Oggystore-Signature-256'
HEADER_TIMESTAMP = 'X-Oggystore-Timestamp'
HEADER_EVENT = 'X-Oggystore-Event'
HEADER_DELIVERY = 'X-Oggystore-Delivery'
FRESHNESS_WINDOW_SECONDS = 300
EVENTS = frozenset({'order.created', 'order.failed', 'order.cancelled',
    'delivery.pending', 'delivery.completed', 'delivery.failed', 'refund.created',
    'product.updated', 'product.disabled', 'product.enabled', 'product.deleted',
    'stock.updated', 'test.ping'})
log = logging.getLogger('webhook-reference')


def verify(secret, raw, headers, now):
    ts = headers.get(HEADER_TIMESTAMP, '')
    sig = headers.get(HEADER_SIGNATURE, '')
    if not re.fullmatch(r'[0-9]{1,12}', ts) or not re.fullmatch(r'sha256=[a-f0-9]{64}', sig):
        return False
    if abs(math.floor(now) - int(ts)) > FRESHNESS_WINDOW_SECONDS:
        return False
    expected = 'sha256=' + hmac.new(secret.encode('utf-8'), ts.encode('ascii') + b'.' + raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode('ascii'), sig.encode('ascii'))


def parse_event(raw, headers):
    def invalid_number(_):
        raise ValueError('non-JSON number')
    body = json.loads(raw.decode('utf-8'), parse_constant=invalid_number)
    if not isinstance(body, dict):
        raise ValueError('invalid event')
    event_id = body.get('event_id')
    if type(event_id) in (int, float):
        # JSON has one number type in JavaScript: 42, 42.0 and 4.2e1 are the
        # same integral ID. Normalize here so both examples dedupe identically.
        if (not 1 <= event_id <= 9007199254740991 or not math.isfinite(event_id)
                or event_id != int(event_id)):
            raise ValueError('invalid event id')
        event_id = body['event_id'] = int(event_id)
    elif not isinstance(event_id, str) or not re.fullmatch(r'[A-Za-z0-9._:-]{1,128}', event_id):
        raise ValueError('invalid event id')
    event_type = body.get('event_type', body.get('event'))
    if (not isinstance(event_type, str) or event_type not in EVENTS
            or body.get('event', event_type) != event_type
            or headers.get(HEADER_EVENT) != event_type):
        raise ValueError('unsupported event')
    # delivery_id is changed by the sender on EVERY attempt; never dedupe on it.
    stable = {k: v for k, v in body.items() if k != 'delivery_id'}
    canonical = json.dumps(stable, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    return str(event_id), event_type, body, hashlib.sha256(canonical.encode()).hexdigest()


class Inbox:
    def __init__(self, path):
        if str(path) == ':memory:':
            raise ValueError('Use a persistent SQLite file, not :memory:')
        self.path = str(path)
        with self.connect() as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('''CREATE TABLE IF NOT EXISTS inbox (
                event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL,
                payload TEXT NOT NULL, content_hash TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
                next_at REAL NOT NULL DEFAULT 0, lease_token TEXT, lease_until REAL,
                last_error TEXT, processed_at REAL)''')
            conn.execute('CREATE INDEX IF NOT EXISTS inbox_due ON inbox(status, next_at)')

    def connect(self):
        # Each operation owns/closes a connection; no shared cross-thread handle.
        conn = sqlite3.connect(self.path, timeout=1)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA synchronous=FULL')
        return _ClosingConnection(conn)

    def accept(self, event_id, event_type, body, fingerprint):
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT content_hash FROM inbox WHERE event_id=?', (event_id,)).fetchone()
            if row:
                return 'duplicate' if row['content_hash'] == fingerprint else 'conflict'
            conn.execute('INSERT INTO inbox(event_id,event_type,payload,content_hash) VALUES(?,?,?,?)',
                         (event_id, event_type, json.dumps(body, ensure_ascii=False, allow_nan=False), fingerprint))
        return 'accepted'

    def claim(self, now, lease_seconds=60):
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('''SELECT * FROM inbox WHERE (status='queued' AND next_at<=?)
                OR (status='processing' AND lease_until<=?) ORDER BY next_at,event_id LIMIT 1''', (now, now)).fetchone()
            if not row:
                return None
            token = uuid.uuid4().hex
            conn.execute("UPDATE inbox SET status='processing', attempts=attempts+1, lease_token=?, lease_until=? WHERE event_id=?",
                         (token, now + lease_seconds, row['event_id']))
            result = dict(row)
            result.update(lease_token=token, attempts=row['attempts'] + 1)
            return result

    def finish(self, job, now, failed=False):
        with self.connect() as conn:
            status = 'failed' if failed and job['attempts'] >= 5 else 'queued' if failed else 'done'
            conn.execute('''UPDATE inbox SET status=?, next_at=?, last_error=?, processed_at=?,
                lease_token=NULL, lease_until=NULL WHERE event_id=? AND lease_token=? AND status='processing' ''',
                (status, now + min(60, 2 ** min(job['attempts'], 6)) if failed else 0,
                 'processing_failed' if failed else None, None if failed else now,
                 job['event_id'], job['lease_token']))

    def retry_failed(self):
        with self.connect() as conn:
            return conn.execute("UPDATE inbox SET status='queued', attempts=0, next_at=0, last_error=NULL WHERE status='failed'").rowcount


class _ClosingConnection:
    def __init__(self, connection): self.connection = connection
    def __enter__(self): return self.connection.__enter__()
    def __exit__(self, *args):
        try: return self.connection.__exit__(*args)
        finally: self.connection.close()


def process_event(body):
    if body.get('event_type', body.get('event')) == 'test.ping':
        log.info('test.ping processed')
    else:
        # Replace with idempotent business logic; never log body/signature/secret.
        log.info('business event processed type=%s', body.get('event_type', body.get('event')))


def create_app(secret, db_path, *, processor=process_event, clock=time.time, start_worker=True):
    if not isinstance(secret, str) or len(secret) < 16:
        raise ValueError('Configure a signing secret of at least 16 characters')
    store = Inbox(db_path)
    app = Flask(__name__)
    app.config['MAX_CONTENT_LENGTH'] = 256 * 1024
    stop = threading.Event()

    def process_one():
        job = store.claim(clock())
        if not job:
            return False
        try:
            processor(json.loads(job['payload']))
        except Exception:
            # No exception text/traceback: application errors may contain PII.
            log.warning('event processing failed')
            store.finish(job, clock(), failed=True)
        else:
            store.finish(job, clock())
        return True

    def work():
        while not stop.is_set():
            try:
                busy = process_one()
            except Exception:
                log.warning('inbox worker unavailable')
                busy = False
            stop.wait(0.02 if busy else 0.25)

    thread = threading.Thread(target=work, daemon=True, name='reference-inbox') if start_worker else None
    if thread: thread.start()
    def close():
        stop.set()
        if thread: thread.join(timeout=2)
    app.extensions['receiver'] = {'store': store, 'process_one': process_one, 'close': close}

    @app.post('/webhook')
    def receive():
        if request.headers.get('Content-Encoding', 'identity').lower() != 'identity':
            return jsonify(ok=False, error='unsupported_encoding'), 415
        raw = request.get_data(cache=False, as_text=False)  # BEFORE JSON parsing
        if not verify(secret, raw, request.headers, clock()):
            return jsonify(ok=False, error='invalid_signature'), 401
        try:
            event_id, event_type, body, fingerprint = parse_event(raw, request.headers)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return jsonify(ok=False, error='invalid_event'), 400
        try:
            outcome = store.accept(event_id, event_type, body, fingerprint)
        except Exception:
            log.warning('inbox unavailable')
            return jsonify(ok=False, error='inbox_unavailable'), 503
        if outcome == 'conflict':
            return jsonify(ok=False, error='event_conflict'), 409
        # Commit precedes ACK; processing happens only on the background worker.
        return jsonify(ok=True, duplicate=outcome == 'duplicate'), 200

    @app.errorhandler(HTTPException)
    def http_error(exc):
        return jsonify(ok=False, error='payload_too_large' if exc.code == 413 else 'invalid_request'), exc.code
    return app


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--retry-failed', action='store_true', help='manually requeue receiver DLQ; then exit')
    args = parser.parse_args()
    path = os.environ.get('INBOX_DB', 'receiver-python.sqlite3')
    if args.retry_failed:
        print('Requeued failed inbox rows:', Inbox(path).retry_failed())
    else:
        app = create_app(os.environ.get('WEBHOOK_SECRET', ''), path)
        try: app.run(host='127.0.0.1', port=int(os.environ.get('PORT', '8080')), debug=False, use_reloader=False)
        finally: app.extensions['receiver']['close']()
