-- Captured from git show c13b2cc5c24dd3034e613781c6d56e0c59330bf3:src/brigade/fleet_hub.py via init_db.
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
PRAGMA user_version=1;
