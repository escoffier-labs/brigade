CREATE TABLE IF NOT EXISTS events (
    cursor INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id TEXT NOT NULL UNIQUE,
    repository TEXT NOT NULL,
    event TEXT NOT NULL,
    action TEXT,
    head_sha TEXT,
    status TEXT,
    conclusion TEXT,
    run_id TEXT,
    run_attempt INTEGER,
    pr_number INTEGER,
    provider_created_at TEXT,
    provider_updated_at TEXT,
    received_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS events_received_at_idx ON events(received_at);
