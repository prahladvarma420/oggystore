"""Dedicated permanent-address worker. No production custody adapter is installed.

Run manually with --once for a bounded cycle. Disabled invocation exits without DB
imports; requested-but-unqualified invocation fails closed, never falls back to legacy.
"""
import argparse
import logging
import signal
import threading

log = logging.getLogger(__name__)


def cycle(cfg, adapter, rpc):
    from payments.unique_bep20 import import_inventory, scan_once, validate_rpc
    validate_rpc(cfg, rpc)
    import_inventory(cfg, adapter)
    return scan_once(cfg, rpc, adapter)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args(argv)
    import os
    if os.getenv('RESELLER_BEP20_UNIQUE_ENABLED','').lower() not in ('1','true','yes'):
        print('reseller-deposit-worker: disabled; no receiving enabled.')
        return 0
    from payments.unique_bep20 import runtime
    try:
        cfg, adapter = runtime()
    except Exception:
        print('reseller-deposit-worker: custody qualification unavailable; startup refused.')
        return 1
    # Future reviewed adapter is the sole gate to this path. No guessed API routes.
    from web3 import Web3
    import config
    from database import db
    rpc = Web3(Web3.HTTPProvider(config.RPC_URL, request_kwargs={'timeout':15}))
    stop=threading.Event()
    for signum in (signal.SIGINT,signal.SIGTERM):
        signal.signal(signum,lambda *_:stop.set())
    delay=5
    try:
        while not stop.is_set():
            try:
                outcome=cycle(cfg,adapter,rpc)
                log.info('reseller_deposit_cycle category=%s',outcome)
                delay=5
            except Exception:
                # Deliberately no raw exception/URL/key/provider payload in logs.
                log.warning('reseller_deposit_cycle category=verification_or_dependency')
                if args.once:return 1
                delay=min(delay*2,120)
            if args.once:return 0
            stop.wait(delay)
    finally:
        db.close_thread_connection()
    return 0


if __name__=='__main__':
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
