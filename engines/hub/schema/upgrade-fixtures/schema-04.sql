-- Captured from git show 74e3c86c3a2fa6f39f332c4cd21f4f374ad4a040:src/brigade/fleet_hub.py via init_db.
CREATE TABLE claims (
    target TEXT NOT NULL PRIMARY KEY,
    owner_node TEXT NOT NULL,
    owner_conductor TEXT,
    harness TEXT,
    role TEXT,
    job TEXT,
    session TEXT,
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
    exit_status INTEGER,
    capability_fingerprint TEXT,
    received_at TEXT NOT NULL,
    PRIMARY KEY (node_id, run_id, sequence, digest)
);
CREATE TABLE nodes (
    node_id TEXT NOT NULL PRIMARY KEY,
    token_sha256 TEXT NOT NULL UNIQUE,
    label TEXT,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE INDEX events_run_seq ON events (node_id, run_id, sequence);
PRAGMA user_version=4;
