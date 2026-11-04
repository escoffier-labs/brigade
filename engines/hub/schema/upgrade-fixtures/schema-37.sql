-- Captured from git show 65bb00fb6bc731855ade644efd263f963c6c5a43:src/brigade/fleet_hub.py via init_db.
CREATE TABLE action_broker_grants (node_id TEXT PRIMARY KEY, targets TEXT NOT NULL, subjects TEXT NOT NULL, revoked_at REAL);
CREATE TABLE action_events (action_id TEXT NOT NULL, generation INTEGER NOT NULL, event_id TEXT NOT NULL, document TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(action_id, generation, event_id));
CREATE TABLE action_operator_grants (grant_id TEXT PRIMARY KEY, login TEXT UNIQUE NOT NULL, scopes TEXT NOT NULL, revoked_at REAL);
CREATE TABLE action_sessions (credential_digest TEXT PRIMARY KEY, csrf_digest TEXT NOT NULL, grant_id TEXT NOT NULL, login TEXT NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE action_target_identities (target_id TEXT PRIMARY KEY, node_id TEXT NOT NULL, identity TEXT NOT NULL);
CREATE TABLE action_targets (target_id TEXT PRIMARY KEY, node_id TEXT NOT NULL, document TEXT NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE admin_stats_snapshots (
        snapshot_id TEXT PRIMARY KEY, taken_at TEXT NOT NULL, source_set TEXT NOT NULL,
        schema TEXT NOT NULL, payload_json TEXT NOT NULL, digest TEXT NOT NULL);
CREATE TABLE admin_stats_weeks (
        week TEXT PRIMARY KEY, partial INTEGER NOT NULL, taken_at TEXT NOT NULL,
        payload_json TEXT NOT NULL);
CREATE TABLE agent_actions (action_id TEXT PRIMARY KEY, grant_id TEXT NOT NULL, client_id TEXT NOT NULL, request_digest TEXT NOT NULL, document TEXT NOT NULL, node_id TEXT NOT NULL, target_id TEXT NOT NULL, status TEXT NOT NULL, generation INTEGER NOT NULL, claim_until REAL, started_at REAL, created_at REAL NOT NULL, expires_at REAL NOT NULL, destination_ref TEXT, delivery TEXT NOT NULL DEFAULT 'none', verification TEXT NOT NULL DEFAULT 'pending', destination_thread TEXT, UNIQUE(grant_id, client_id));
CREATE TABLE claim_fences (
    target TEXT NOT NULL PRIMARY KEY,
    generation INTEGER NOT NULL
        CHECK (typeof(generation) = 'integer' AND generation >= 1 AND generation <= 9223372036854775807)
);
CREATE TABLE claim_retired_holders (
    target TEXT NOT NULL,
    owner_node TEXT NOT NULL,
    holder_token TEXT NOT NULL,
    generation INTEGER NOT NULL
        CHECK (typeof(generation) = 'integer' AND generation >= 1 AND generation <= 9223372036854775807),
    retired_at REAL NOT NULL CHECK (typeof(retired_at) IN ('integer', 'real') AND retired_at >= 0 AND retired_at <= 1.7976931348623157e308),
    PRIMARY KEY (target, owner_node, holder_token)
);
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
CREATE TABLE cloud_global_state (
    singleton INTEGER NOT NULL PRIMARY KEY CHECK (singleton = 1),
    limit_count INTEGER NOT NULL CHECK (limit_count BETWEEN 0 AND 64)
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
CREATE TABLE dashboard_enrollment_codes (code_digest TEXT PRIMARY KEY, label TEXT, issued_at REAL NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE dashboard_sessions (session_id TEXT PRIMARY KEY, credential_digest TEXT NOT NULL UNIQUE, label TEXT, scope TEXT NOT NULL CHECK(scope = 'read-only'), created_at REAL NOT NULL, expires_at REAL NOT NULL, revoked_at REAL);
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
    repo_identity TEXT,
    context TEXT,
    received_at TEXT NOT NULL,
    PRIMARY KEY (node_id, run_id, sequence, digest)
);
CREATE TABLE fleet_dispatch_intents (
    decision_id TEXT NOT NULL PRIMARY KEY,
    reservation_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    origin_node TEXT NOT NULL,
    target_node TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE fleet_machine_telemetry (
    node_id TEXT NOT NULL PRIMARY KEY,
    machine TEXT,
    observed_at TEXT NOT NULL,
    ttl_seconds INTEGER NOT NULL,
    status TEXT NOT NULL,
    load REAL,
    running TEXT NOT NULL,
    active_claims TEXT NOT NULL,
    usable_seats TEXT NOT NULL,
    credential_state TEXT NOT NULL,
    observed_id TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE fleet_message_principals (
    principal_id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    enrollment_id TEXT
);
CREATE TABLE fleet_message_send_grants (
        principal_id TEXT NOT NULL, pattern TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(principal_id, pattern));
CREATE TABLE fleet_message_sends (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender TEXT NOT NULL, client_id TEXT NOT NULL, received_at TEXT NOT NULL
        );
CREATE TABLE fleet_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender TEXT NOT NULL,
            recipient TEXT NOT NULL,
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            reference TEXT,
            priority TEXT NOT NULL,
            client_id TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            reply_to INTEGER REFERENCES fleet_messages(id),
            created_at TEXT NOT NULL,
            received_at TEXT NOT NULL,
            acked_at TEXT,
            acked_by TEXT
        , sender_label TEXT, thread TEXT, kind TEXT);
CREATE TABLE fleet_policy_acknowledged (
    receipt_key TEXT NOT NULL PRIMARY KEY,
    external_session_id TEXT NOT NULL,
    consumer TEXT NOT NULL,
    owner_node TEXT NOT NULL,
    repo_identity TEXT,
    origin TEXT NOT NULL,
    revision INTEGER NOT NULL,
    digest TEXT NOT NULL,
    source TEXT NOT NULL,
    effective TEXT NOT NULL,
    sources TEXT NOT NULL,
    selected TEXT,
    overrides TEXT,
    override_reason TEXT,
    context_hash TEXT,
    loaded_at TEXT NOT NULL,
    acknowledged_at TEXT NOT NULL
);
CREATE TABLE fleet_policy_acknowledged_history (
    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_key TEXT NOT NULL,
    external_session_id TEXT NOT NULL,
    consumer TEXT NOT NULL,
    owner_node TEXT NOT NULL,
    repo_identity TEXT,
    origin TEXT NOT NULL,
    revision INTEGER NOT NULL,
    digest TEXT NOT NULL,
    source TEXT NOT NULL,
    effective TEXT NOT NULL,
    sources TEXT NOT NULL,
    selected TEXT,
    overrides TEXT,
    override_reason TEXT,
    context_hash TEXT,
    loaded_at TEXT NOT NULL,
    acknowledged_at TEXT NOT NULL
);
CREATE TABLE fleet_policy_delegations (
            delegation_id TEXT NOT NULL PRIMARY KEY,
            parent_request_id TEXT NOT NULL,
            origin_node TEXT NOT NULL,
            decision_id TEXT NOT NULL,
            reservation_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            consumer TEXT NOT NULL,
            adapter TEXT NOT NULL,
            repo_identity TEXT NOT NULL,
            seat TEXT NOT NULL,
            workload TEXT NOT NULL,
            policy_version INTEGER NOT NULL,
            policy_digest TEXT NOT NULL,
            target_machine TEXT NOT NULL,
            target_node TEXT NOT NULL,
            source_revision TEXT NOT NULL,
            parent_metadata TEXT,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            document TEXT NOT NULL
        );
CREATE TABLE fleet_policy_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    current_revision INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT
);
CREATE TABLE fleet_policy_pending (
    pending_key TEXT NOT NULL PRIMARY KEY,
    session_id TEXT NOT NULL,
    consumer TEXT NOT NULL,
    owner_node TEXT NOT NULL,
    repo_identity TEXT,
    origin TEXT NOT NULL,
    revision INTEGER NOT NULL,
    digest TEXT NOT NULL,
    overrides TEXT,
    override_reason TEXT,
    effective TEXT NOT NULL,
    sources TEXT NOT NULL,
    selected TEXT,
    instructions TEXT,
    context_hash TEXT,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE fleet_policy_revisions (
    revision INTEGER PRIMARY KEY,
    schema TEXT NOT NULL,
    digest TEXT NOT NULL,
    document TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    parent_revision INTEGER
);
CREATE TABLE fleet_policy_sessions (
    session_id TEXT NOT NULL PRIMARY KEY,
    consumer TEXT NOT NULL,
    owner_node TEXT NOT NULL,
    repo_identity TEXT,
    revision INTEGER,
    digest TEXT,
    source TEXT NOT NULL,
    loaded_at TEXT NOT NULL,
    refresh_state TEXT NOT NULL DEFAULT 'none',
    refresh_requested_at TEXT,
    refresh_updated_at TEXT,
    refresh_detail TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE fleet_quota_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    version INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE fleet_quota_observations (
    observation_id TEXT NOT NULL PRIMARY KEY,
    account_id TEXT NOT NULL,
    pool_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    source TEXT NOT NULL,
    source_ref TEXT,
    collected_at TEXT NOT NULL,
    provider_time TEXT,
    expires_at TEXT,
    status TEXT NOT NULL,
    digest TEXT NOT NULL,
    document TEXT NOT NULL,
    raw TEXT NOT NULL,
    overrides_observation_id TEXT,
    reason TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE fleet_reservations (
    reservation_id TEXT NOT NULL PRIMARY KEY,
    decision_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    machine TEXT NOT NULL,
    seat TEXT NOT NULL,
    node_id TEXT NOT NULL,
    origin_node TEXT NOT NULL,
    state TEXT NOT NULL,
    accepted INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE fleet_route_decisions (
    decision_id TEXT NOT NULL PRIMARY KEY,
    request_key TEXT NOT NULL UNIQUE,
    request_digest TEXT NOT NULL,
    admission_generation INTEGER NOT NULL DEFAULT 0,
    policy_version INTEGER NOT NULL,
    policy_digest TEXT NOT NULL,
    origin TEXT,
    origin_node TEXT NOT NULL,
    session_id TEXT NOT NULL,
    consumer TEXT,
    repo_identity TEXT,
    workload TEXT,
    work_id TEXT,
    selected_machine TEXT,
    selected_seat TEXT,
    reservation_id TEXT,
    reason TEXT,
    override_reason TEXT,
    request_document TEXT,
    document TEXT NOT NULL,
    created_at TEXT NOT NULL
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
    queue_ttl_seconds INTEGER,
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
CREATE TABLE interactive_sessions (
    node_id TEXT NOT NULL,
    harness TEXT NOT NULL,
    session_id TEXT NOT NULL,
    repo_identity TEXT NOT NULL,
    identity_scope TEXT NOT NULL,
    repo_label TEXT NOT NULL,
    checkout_path TEXT NOT NULL,
    branch TEXT,
    dirty_paths_json TEXT NOT NULL,
    dirty_truncated INTEGER NOT NULL,
    state TEXT NOT NULL,
    started_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    ended_at TEXT,
    ttl_seconds INTEGER NOT NULL,
    expires_at REAL NOT NULL, cloud_context_json TEXT,
    PRIMARY KEY (node_id, harness, session_id, repo_identity)
);
CREATE TABLE model_admission_audit (
    node_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    consumer TEXT NOT NULL,
    source TEXT NOT NULL,
    roster_revision INTEGER NOT NULL,
    roster_digest TEXT NOT NULL,
    seat TEXT,
    provider TEXT,
    model TEXT,
    reasoning TEXT,
    consumer_binding TEXT,
    request_digest TEXT NOT NULL,
    decision TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (node_id, request_id, phase)
);
CREATE TABLE model_consumer_defaults (
    consumer TEXT NOT NULL PRIMARY KEY,
    seat TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE model_launch_proofs (
    node_id TEXT NOT NULL,
    consumer TEXT NOT NULL,
    policy_session_id TEXT NOT NULL,
    proof_key TEXT NOT NULL,
    request_id TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (node_id, consumer, policy_session_id, proof_key)
);
CREATE TABLE model_leases (
    lease_id TEXT NOT NULL PRIMARY KEY,
    seat TEXT NOT NULL,
    owner_node TEXT NOT NULL,
    holder_token TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at REAL NOT NULL,
    released_at TEXT
, provider TEXT, model TEXT, launch_model TEXT, account_id TEXT, pool TEXT, decision_id TEXT, session_id TEXT, consumer TEXT, context_hash TEXT, request_id TEXT, policy_digest TEXT, policy_version INTEGER);
CREATE TABLE model_policy (
    seat TEXT NOT NULL PRIMARY KEY,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    reasoning TEXT NOT NULL DEFAULT 'none',
    enabled INTEGER NOT NULL,
    limit_count INTEGER,
    brigade_cli TEXT NOT NULL DEFAULT '',
    brigade_model TEXT NOT NULL DEFAULT '',
    t3_instance_id TEXT NOT NULL DEFAULT '',
    t3_service_tier TEXT NOT NULL DEFAULT '',
    t3_model TEXT NOT NULL DEFAULT '',
    notes TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE model_roster_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    revision INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT
);
CREATE TABLE nodes (
    node_id TEXT NOT NULL PRIMARY KEY,
    token_sha256 TEXT NOT NULL UNIQUE,
    label TEXT,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE TABLE retired_models (
    provider TEXT NOT NULL,
    family TEXT NOT NULL,
    match_kind TEXT NOT NULL,
    permanent INTEGER NOT NULL,
    reason_code TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (provider, family)
);
CREATE TABLE run_preference (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    impl TEXT,
    review TEXT,
    chef TEXT,
    research TEXT,
    security TEXT,
    scout TEXT,
    notes TEXT,
    updated_at TEXT NOT NULL,
    updated_by TEXT
);
CREATE TABLE work_create_keys (
        actor_id TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        work_id TEXT NOT NULL,
        received_at TEXT NOT NULL,
        PRIMARY KEY (actor_id, idempotency_key),
        FOREIGN KEY (work_id) REFERENCES work_items(work_id) ON DELETE RESTRICT
    );
CREATE TABLE work_events (
        work_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        from_status TEXT,
        to_status TEXT,
        actor_type TEXT NOT NULL,
        actor_id TEXT,
        node_id TEXT,
        run_id TEXT,
        detail_json TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        received_at TEXT NOT NULL,
        seq INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (work_id, event_id),
        FOREIGN KEY (work_id) REFERENCES work_items(work_id) ON DELETE RESTRICT
    );
CREATE TABLE work_import_keys (
        adapter_id TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        created INTEGER NOT NULL,
        updated INTEGER NOT NULL,
        unchanged INTEGER NOT NULL DEFAULT 0,
        refused INTEGER NOT NULL DEFAULT 0,
        received_at TEXT NOT NULL,
        owner_node TEXT,
        PRIMARY KEY (adapter_id, idempotency_key)
    );
CREATE TABLE work_items (
        work_id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        description TEXT,
        kind TEXT NOT NULL,
        scope TEXT,
        status TEXT NOT NULL,
        priority TEXT NOT NULL,
        burn_eligible INTEGER NOT NULL,
        burn_rank INTEGER NOT NULL,
        token_appetite TEXT NOT NULL,
        execution_mode TEXT NOT NULL,
        acceptance_json TEXT NOT NULL,
        blocker TEXT,
        review_after TEXT,
        spend_by TEXT,
        ready_at TEXT,
        attempt_count INTEGER NOT NULL,
        version INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        archived_at TEXT
    );
CREATE TABLE work_links (
        link_id TEXT PRIMARY KEY,
        work_id TEXT NOT NULL,
        link_type TEXT NOT NULL,
        external_key TEXT NOT NULL,
        display_ref TEXT,
        url TEXT,
        external_state TEXT,
        external_updated_at TEXT,
        source_policy TEXT NOT NULL,
        source_acceptance_json TEXT NOT NULL,
        synced_at TEXT NOT NULL,
        stale_at TEXT,
        adapter_id TEXT,
        owner_node TEXT,
        source_version TEXT,
        FOREIGN KEY (work_id) REFERENCES work_items(work_id) ON DELETE RESTRICT,
        UNIQUE (link_type, external_key)
    );
CREATE TABLE work_schema_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
CREATE TABLE work_sync_cursors (
        adapter_id TEXT PRIMARY KEY,
        source_type TEXT NOT NULL,
        owner_node TEXT NOT NULL,
        cursor TEXT,
        last_success_at TEXT,
        last_error_code TEXT,
        updated_at TEXT NOT NULL
    );
CREATE INDEX admin_stats_taken_at ON admin_stats_snapshots(taken_at);
CREATE INDEX agent_actions_queue ON agent_actions(node_id, status, created_at);
CREATE INDEX claim_retired_holders_age ON claim_retired_holders (retired_at);
CREATE INDEX claim_retired_holders_node_age ON claim_retired_holders (owner_node, retired_at);
CREATE INDEX claims_owner_expiry ON claims (owner_node, expires_at);
CREATE INDEX cloud_leases_active ON cloud_leases (provider, expires_at);
CREATE INDEX events_grokbot_run_seq ON events (run_id, sequence, received_at, digest) WHERE harness = 'grokbot';
CREATE INDEX events_received_at ON events (received_at);
CREATE INDEX events_run_seq ON events (node_id, run_id, sequence);
CREATE INDEX fleet_message_sends_received ON fleet_message_sends (received_at);
CREATE INDEX fleet_message_sends_sender_received ON fleet_message_sends (sender, received_at);
CREATE INDEX fleet_messages_created_at ON fleet_messages (created_at);
CREATE INDEX fleet_messages_inbox ON fleet_messages (recipient, id);
CREATE INDEX fleet_messages_reply_to ON fleet_messages (reply_to);
CREATE INDEX fleet_messages_sender ON fleet_messages (sender, id);
CREATE UNIQUE INDEX fleet_messages_sender_client ON fleet_messages (sender, client_id);
CREATE INDEX fleet_messages_unacked_recipient ON fleet_messages (recipient) WHERE acked_at IS NULL;
CREATE INDEX fleet_policy_ack_ext_session ON fleet_policy_acknowledged (external_session_id, consumer);
CREATE INDEX fleet_policy_ack_history_key ON fleet_policy_acknowledged_history (receipt_key, revision);
CREATE INDEX fleet_policy_pending_session ON fleet_policy_pending (session_id, consumer);
CREATE INDEX fleet_policy_sessions_revision ON fleet_policy_sessions (revision);
CREATE INDEX fleet_quota_observations_pool ON fleet_quota_observations (pool_id, account_id);
CREATE INDEX fleet_reservations_machine ON fleet_reservations (machine, state);
CREATE INDEX fleet_reservations_node ON fleet_reservations (node_id, state);
CREATE INDEX grokbot_jobs_queue ON grokbot_jobs (queue_id, role, state);
CREATE INDEX grokbot_jobs_role ON grokbot_jobs (role, state);
CREATE INDEX grokbot_jobs_state ON grokbot_jobs (state, updated_at);
CREATE UNIQUE INDEX idx_delegation_decision
        ON fleet_policy_delegations (decision_id)
        ;
CREATE UNIQUE INDEX idx_delegation_parent_origin
        ON fleet_policy_delegations (origin_node, parent_request_id)
        ;
CREATE INDEX model_leases_active ON model_leases (seat, expires_at);
CREATE INDEX work_events_adapter_event_work_seq ON work_events (actor_type, event_type, work_id, seq);
CREATE INDEX work_events_adapter_retention ON work_events (work_id, actor_type, event_type, seq);
CREATE INDEX work_events_attempt_identity ON work_events (work_id, event_type, node_id, run_id);
CREATE INDEX work_events_attempt_work_seq ON work_events (work_id, event_type, seq);
CREATE INDEX work_events_recent ON work_events (occurred_at DESC, seq DESC);
CREATE INDEX work_events_seq ON work_events (seq);
CREATE INDEX work_events_work_id ON work_events (work_id);
CREATE INDEX work_events_work_seq ON work_events (work_id, seq);
CREATE INDEX work_import_keys_adapter ON work_import_keys (adapter_id, received_at);
CREATE INDEX work_import_keys_owner ON work_import_keys (owner_node, received_at);
CREATE INDEX work_items_burn ON work_items (burn_eligible);
CREATE INDEX work_items_burn_order ON work_items (burn_rank, (spend_by IS NULL OR spend_by = ''), (COALESCE(spend_by, '')), (COALESCE(ready_at, '')), work_id);
CREATE INDEX work_items_created_order ON work_items (created_at, work_id);
CREATE INDEX work_items_ready_at ON work_items (ready_at);
CREATE INDEX work_items_status ON work_items (status);
CREATE INDEX work_links_adapter ON work_links (adapter_id);
CREATE INDEX work_links_external ON work_links (link_type, external_key);
CREATE INDEX work_links_owner ON work_links (owner_node);
CREATE INDEX work_links_work_id ON work_links (work_id);
CREATE INDEX work_links_work_order ON work_links (work_id, synced_at, link_id);
CREATE INDEX work_links_work_policy_order ON work_links (work_id, source_policy, synced_at, link_id);
CREATE INDEX work_links_work_policy_type_order ON work_links (work_id, source_policy, link_type, synced_at, link_id);
CREATE INDEX work_links_work_policy_type_stale ON work_links (work_id, source_policy, link_type, stale_at);
CREATE INDEX work_links_work_type ON work_links (work_id, link_type, synced_at);
PRAGMA user_version=37;
