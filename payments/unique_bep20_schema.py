"""Additive exact-unit reseller funding schema; no customer or ledger changes."""

def ensure_schema(cur):
    statements = [
        """CREATE TABLE IF NOT EXISTS reseller_bep20_addresses(
          address TEXT PRIMARY KEY, reseller_id INTEGER UNIQUE REFERENCES resellers(id) ON DELETE RESTRICT,
          provider TEXT NOT NULL, wallet_ref TEXT NOT NULL, provider_ref TEXT NOT NULL UNIQUE,
          chain_id INTEGER NOT NULL CHECK(chain_id=56), token TEXT NOT NULL,
          decimals INTEGER NOT NULL CHECK(decimals=18), discovered_at TEXT NOT NULL, assigned_at TEXT)""",
        """CREATE TABLE IF NOT EXISTS reseller_bep20_events(
          event_key TEXT PRIMARY KEY, chain_id INTEGER NOT NULL, token TEXT NOT NULL,
          tx_hash TEXT NOT NULL, log_index INTEGER NOT NULL CHECK(log_index>=0),
          address TEXT NOT NULL REFERENCES reseller_bep20_addresses(address) ON DELETE RESTRICT,
          raw_units TEXT NOT NULL, block_number INTEGER NOT NULL, block_hash TEXT NOT NULL,
          state TEXT NOT NULL CHECK(state IN ('observed','review','accounted')),
          category TEXT NOT NULL, provider_ref TEXT, credit_cents INTEGER,
          carry_before TEXT, carry_after TEXT, account_seq INTEGER, ledger_id INTEGER REFERENCES reseller_wallet_transactions(id) ON DELETE RESTRICT,
          created_at TEXT NOT NULL, accounted_at TEXT,
          UNIQUE(chain_id,token,tx_hash,log_index))""",
        """CREATE TABLE IF NOT EXISTS reseller_bep20_carry(
          reseller_id INTEGER PRIMARY KEY REFERENCES resellers(id) ON DELETE RESTRICT,
          token TEXT NOT NULL, raw_units TEXT NOT NULL, last_seq INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS reseller_bep20_claims(
          tx_hash TEXT PRIMARY KEY, chain_id INTEGER NOT NULL CHECK(chain_id=56), created_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS reseller_bep20_scan(
          scan_key TEXT PRIMARY KEY, next_block INTEGER NOT NULL, anchor_hash TEXT,
          version INTEGER NOT NULL DEFAULT 0, paused INTEGER NOT NULL DEFAULT 0 CHECK(paused IN (0,1)),
          updated_at TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS idx_reseller_bep20_events_owner ON reseller_bep20_events(address,block_number)",
        "CREATE INDEX IF NOT EXISTS idx_reseller_bep20_events_state ON reseller_bep20_events(state,block_number)",
    ]
    for sql in statements:
        cur.execute(sql)
