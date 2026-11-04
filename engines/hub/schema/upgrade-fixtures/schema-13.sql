-- Captured from git show 064e651f24c216dfcc197efd881f9ac66e61462e:src/brigade/fleet_hub.py via init_db.
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
CREATE TABLE cloud_leases (
    lease_id TEXT NOT NULL PRIMARY KEY,
    provider TEXT NOT NULL,
    provider_task_id TEXT,
    repo TEXT,
    label TEXT,
    prompt_hash TEXT,
    owner_node TEXT NOT NULL,
    owner_conductor TEXT,
    holder_token TEXT NOT NULL,
    state TEXT NOT NULL,
    admitted_at TEXT NOT NULL,
    renewed_at TEXT NOT NULL,
    ttl_seconds INTEGER NOT NULL,
    expires_at REAL NOT NULL,
    artifact_ref TEXT,
    released_at TEXT
);
CREATE TABLE cloud_provider_state (
    provider TEXT NOT NULL PRIMARY KEY,
    enabled INTEGER NOT NULL,
    limit_count INTEGER NOT NULL,
    hosted INTEGER NOT NULL,
    circuit_state TEXT NOT NULL,
    reason TEXT,
    subscription_pool TEXT,
    reset_at TEXT,
    expires_at TEXT,
    updated_at TEXT NOT NULL
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
CREATE TABLE grokbot_actor_policy (
    node_id TEXT NOT NULL PRIMARY KEY,
    queue_owner_node_id TEXT NOT NULL,
    queue_id TEXT NOT NULL,
    actor_kind TEXT NOT NULL,
    role TEXT,
    enabled INTEGER NOT NULL
);
CREATE TABLE grokbot_jobs (
    job_id TEXT NOT NULL PRIMARY KEY,
    role TEXT NOT NULL,
    repository TEXT NOT NULL,
    label TEXT NOT NULL,
    task_digest TEXT NOT NULL,
    idempotency_key_hash TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL,
    item_revision INTEGER NOT NULL,
    sequence INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    queued_at TEXT NOT NULL,
    timeout_seconds INTEGER NOT NULL,
    artifact_kind TEXT NOT NULL,
    private_snapshot_id TEXT,
    claimed_at TEXT,
    lease_token_digest TEXT,
    lease_generation INTEGER,
    lease_expires_at TEXT,
    cancel_requested_at TEXT,
    artifact_ref TEXT,
    artifact_digest TEXT,
    artifact_size INTEGER,
    owner_node TEXT,
    claimant_node TEXT,
    claimant_worker TEXT,
    queue_id TEXT
);
CREATE TABLE grokbot_operations (
    job_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    action TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    result_revision INTEGER NOT NULL,
    result_state TEXT NOT NULL,
    result_json TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    actor_node_id TEXT,
    queue_id TEXT,
    queue_owner_node_id TEXT,
    PRIMARY KEY (job_id, operation_id)
);
CREATE TABLE model_leases (
    lease_id TEXT NOT NULL PRIMARY KEY,
    seat TEXT NOT NULL,
    owner_node TEXT NOT NULL,
    holder_token TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at REAL NOT NULL,
    released_at TEXT
);
CREATE TABLE model_policy (
    seat TEXT NOT NULL PRIMARY KEY,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    limit_count INTEGER,
    notes TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE nodes (
    node_id TEXT NOT NULL PRIMARY KEY,
    token_sha256 TEXT NOT NULL UNIQUE,
    label TEXT,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE TABLE run_preference (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    impl TEXT,
    review TEXT,
    chef TEXT,
    notes TEXT,
    updated_at TEXT NOT NULL,
    updated_by TEXT
);
CREATE INDEX cloud_leases_active ON cloud_leases (provider, expires_at);
CREATE INDEX events_run_seq ON events (node_id, run_id, sequence);
CREATE INDEX grokbot_jobs_queue ON grokbot_jobs (queue_id, role, state);
CREATE INDEX grokbot_jobs_role ON grokbot_jobs (role, state);
CREATE INDEX grokbot_jobs_state ON grokbot_jobs (state, updated_at);
CREATE INDEX model_leases_active ON model_leases (seat, expires_at);
PRAGMA user_version=13;
