"""Fail-closed helpers for manual, dedicated PostgreSQL TEST load validation.
No application imports or connection attempts occur at import time.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
class SafetyError(Exception):
    """Public messages must contain only fixed, non-sensitive categories."""

def identifier(value):
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,31}', value or ''):
        raise SafetyError('invalid_run_or_namespace_identifier')
    return value

def private_config(path):
    path = Path(path)
    if path.is_symlink() or (os.name != 'nt' and path.stat().st_mode & 0o077):
        raise SafetyError('configuration_must_be_private_mode_0600')
    cfg = json.loads(path.read_text(encoding='utf-8'))
    if cfg.get('environment') != 'test' or cfg.get('dedicated_test_database') is not True:
        raise SafetyError('dedicated_test_confirmation_required')
    if os.environ.get('RESELLER_API_ENVIRONMENT') != 'test':
        raise SafetyError('test_environment_required')
    url = urlsplit(cfg.get('base_url', ''))
    if (url.scheme != 'https' or not url.hostname or url.username or url.password
            or url.query or url.fragment or url.path.rstrip('/') != '/api/v1'
            or url.hostname in {'localhost', '127.0.0.1', '::1'}):
        raise SafetyError('public_https_v1_base_required')
    # Explicit operator allowlist is required; hostname spelling alone never
    # establishes isolation. The read-only API/database canary is also required.
    if url.hostname != cfg.get('confirmed_test_host'):
        raise SafetyError('test_host_confirmation_mismatch')
    if not cfg.get('database_url') or not cfg.get('confirmed_database_name'):
        raise SafetyError('database_confirmation_required')
    return cfg

def guarded_database(cfg):
    import psycopg
    from psycopg.rows import dict_row
    conn = psycopg.connect(cfg['database_url'], autocommit=True,
                           connect_timeout=10, row_factory=dict_row,
                           options='-c statement_timeout=30000 -c lock_timeout=5000')
    try:
        identity = conn.execute('SELECT current_database() AS name').fetchone()['name']
        if identity != cfg['confirmed_database_name']:
            raise SafetyError('database_name_mismatch')
        marker = conn.execute("SELECT value FROM settings WHERE key='deployment_environment'").fetchone()
        if not marker or marker['value'] != 'test':
            raise SafetyError('existing_test_database_marker_required')
        live = conn.execute("SELECT COUNT(*) AS n FROM reseller_api_keys WHERE environment='production'").fetchone()['n']
        if live:
            raise SafetyError('database_contains_production_credentials')
        return conn
    except BaseException:
        conn.close()
        raise

def application_environment(cfg):
    # Only after guarded_database succeeded. Do not load an operator .env file.
    os.environ.update(DATABASE_URL=cfg['database_url'],
                      RESELLER_API_ENVIRONMENT='test', PYTHON_DOTENV_DISABLED='1',
                      BOT_TOKEN='', ADMIN_ID='0')
    if not os.environ.get('DATA_ENCRYPTION_KEY'):
        raise SafetyError('staging_encryption_key_required_in_environment')
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

def validate_report_path(path):
    path = Path(path)
    if path.resolve().is_relative_to(ROOT) or path.is_symlink():
        raise SafetyError('evidence_must_be_outside_repository')
    return path

def write_report(path, report):
    """Never serialize configuration, request headers/bodies or raw responses."""
    path = validate_report_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(report, indent=2, sort_keys=True) + '\n'
    tmp = path.with_suffix(path.suffix + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(data)
    tmp.replace(path)

def database_fingerprint(cfg):
    # Database name fingerprint only, never hash or serialize credentials.
    return hashlib.sha256(cfg['confirmed_database_name'].encode()).hexdigest()[:16]

def run_prefix(run_id):
    return 'load.' + identifier(run_id) + '.'

def safe_main(function):
    try:
        return function() or 0
    except KeyboardInterrupt:
        print('INTERRUPTED: review persisted evidence before retrying.', file=sys.stderr)
        return 130
    except SafetyError as exc:
        print('BLOCKED: ' + str(exc), file=sys.stderr)
        return 2
    except Exception:
        # Network/driver errors can embed passwords, full URLs and response text.
        print('FAILED: operation_error; inspect staging privately, no raw exception logged.', file=sys.stderr)
        return 1
