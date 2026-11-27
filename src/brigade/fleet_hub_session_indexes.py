"""Additive indexes for live presence counts and exact provider links."""

SESSION_INDEXES = (
    (
        "interactive_sessions_active_expiry",
        "CREATE INDEX IF NOT EXISTS interactive_sessions_active_expiry "
        "ON interactive_sessions (expires_at) WHERE state = 'active'",
    ),
    (
        "interactive_sessions_active_provider",
        "CREATE INDEX IF NOT EXISTS interactive_sessions_active_provider "
        "ON interactive_sessions (node_id, harness, session_id, checkout_path, expires_at) WHERE state = 'active'",
    ),
    (
        "interactive_sessions_active_observer",
        "CREATE INDEX IF NOT EXISTS interactive_sessions_active_observer "
        "ON interactive_sessions (node_id, harness, CAST(json_extract(provider_link_json, '$.harness') AS TEXT), "
        "CAST(json_extract(provider_link_json, '$.session_id') AS TEXT), checkout_path, expires_at) "
        "WHERE state = 'active' AND provider_link_json IS NOT NULL",
    ),
)
