-- Captured from git show b6fd4a07779dd1f545c86d0c2428d18ea1ee3fa9:src/brigade/fleet_hub.py via init_db.
CREATE TABLE claims (
    target TEXT NOT NULL PRIMARY KEY,
    owner_node TEXT NOT NULL,
    owner_conductor TEXT,
    holder_token TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    renewed_at TEXT NOT NULL,
    ttl_seconds INTEGER NOT NULL,
    expires_at REAL NOT NULL,
    lock_token TEXT,
    lock_acquired_at TEXT,
    lock_run_dir TEXT
);
CREATE TABLE events (
    node_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    digest TEXT NOT NULL,
    repo TEXT,
    seat TEXT,
    harness TEXT,
    state TEXT NOT NULL,
    ts TEXT NOT NULL,
    received_at TEXT NOT NULL,
    PRIMARY KEY (node_id, run_id, sequence, digest)
);
CREATE INDEX events_run_seq ON events (node_id, run_id, sequence);
PRAGMA user_version=3;
