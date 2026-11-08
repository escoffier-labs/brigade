-- Production fleet hub at schema v50 (copy taken 2026-11-08): DDL of the two tables
-- whose layouts the Rust gate refused. DDL only, no rows. user_version is left at 0
-- so init_db creates the remaining startup objects around them.
CREATE TABLE claims (
    target TEXT NOT NULL PRIMARY KEY,
    owner_node TEXT NOT NULL,
    owner_conductor TEXT,
    holder_token TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    renewed_at TEXT NOT NULL,
    ttl_seconds INTEGER NOT NULL,
    expires_at REAL NOT NULL
, lock_token TEXT, lock_acquired_at TEXT, lock_run_dir TEXT, harness TEXT, role TEXT, job TEXT, session TEXT);
CREATE INDEX claims_owner_expiry ON claims (owner_node, expires_at);
CREATE TABLE model_policy (
    seat TEXT NOT NULL PRIMARY KEY,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    limit_count INTEGER,
    notes TEXT,
    updated_at TEXT NOT NULL
, reasoning TEXT NOT NULL DEFAULT 'none', brigade_cli TEXT NOT NULL DEFAULT '', t3_instance_id TEXT NOT NULL DEFAULT '', t3_service_tier TEXT NOT NULL DEFAULT '', brigade_model TEXT NOT NULL DEFAULT '', t3_model TEXT NOT NULL DEFAULT '');
