"""Explicit maintenance only: default dry-run; never called by app startup."""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db-path', help='Existing SQLite database path; not used with DATABASE_URL')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--confirm', choices=['NORMALIZE_EMPTY_RESELLER_DELIVERY'])
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--batch-size', type=int, default=100)
    parser.add_argument('--max-rows', type=int, default=5000)
    args = parser.parse_args(argv)
    if args.apply and args.confirm != 'NORMALIZE_EMPTY_RESELLER_DELIVERY':
        parser.error('--apply requires --confirm NORMALIZE_EMPTY_RESELLER_DELIVERY')
    from dotenv import load_dotenv
    load_dotenv(ROOT/'.env')
    if args.db_path and os.getenv('DATABASE_URL'):
        parser.error('Choose SQLite --db-path OR DATABASE_URL, not both')
    database_url = os.getenv('DATABASE_URL', '')
    if not database_url:
        path = Path(args.db_path or ROOT/'shopbot.db').resolve()
        if not path.is_file():
            parser.error('SQLite database must already exist; no database is created')
    elif not database_url.lower().startswith(('postgres://','postgresql://')):
        parser.error('DATABASE_URL must be a PostgreSQL URL')
    connection = None
    try:
        if not database_url:
            # No database.db import: historical imports can run migrations.
            import sqlite3
            connection = sqlite3.connect(f"{path.as_uri()}?mode={'rw' if args.apply else 'ro'}", uri=True)
        else:
            from database.pgcompat import connect
            connection = connect('unused')
            if not args.apply:
                connection.cursor().execute('SET default_transaction_read_only = on')
        from reseller_api.services.delivery_normalization import normalize_empty_deliveries
        result = normalize_empty_deliveries(apply=args.apply, batch_size=args.batch_size,
                                           max_rows=args.max_rows, resume=args.resume, connection=connection)
        print(json.dumps(result, sort_keys=True))
        return 2 if result['unreadable'] or result['raced'] else 3 if result['needs_resume'] else 0
    except Exception:
        print('Cleanup stopped safely; verify database/configuration. No secret or row content is printed.', file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


if __name__ == '__main__':
    sys.exit(main())
