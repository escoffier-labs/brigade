"""Focused tests for bounded fleet model inventory component.

Tests cover:
- Schema creation and idempotent migrations
- Time, TTL, clock skew, and exact expiry boundaries
- Newer failure superseding old good
- Older snapshot rejection and same-instant conflict handling
- Idempotency and collision detection
- Complete versus partial inventory semantics
- Account and harness coverage isolation
- Exact operator alias mapping (no string guessing)
- Malformed inputs, oversized payloads, nonfinite floats, and credential-bearing URL sanitization
- Safe error sanitization without raw stderr
- Probe runner dependency injection, executable allowlist, and zero real process execution
- Parsers for agy, opencode, cursor-agent, and refusal of unsupported harnesses (claudecode, codex, jules, grokbot)
- Expiring manual browser observations
- Adaptation to fleet_policy.validate_inventory
"""

import math
import sqlite3
from typing import Any

import pytest

from brigade import fleet_model_inventory as fmi
from brigade import fleet_policy
from brigade import proc


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    fmi.ensure_schema(connection)
    return connection


def test_ensure_schema_is_idempotent(conn: sqlite3.Connection) -> None:
    # Multiple calls must succeed cleanly without duplicate table errors.
    fmi.ensure_schema(conn)
    fmi.ensure_schema(conn)

    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'fleet_model_inventory_%'"
        ).fetchall()
    }
    assert "fleet_model_inventory_observations" in tables
    assert "fleet_model_inventory_projections" in tables
    assert "fleet_model_inventory_aliases" in tables


def test_ingest_and_snapshot_basic_lifecycle(conn: sqlite3.Connection) -> None:
    payload = {
        "schema": fmi.PAYLOAD_SCHEMA,
        "source": "cli:agy",
        "provider": "google",
        "harness": "agy",
        "account_id": "acct-1",
        "scope": "complete",
        "status": "ok",
        "captured_at": "2026-09-05T12:00:00Z",
        "expires_at": "2026-09-05T13:00:00Z",
        "models": [
            {"model_id": "gemini-3.8-flash", "display_name": "Gemini 3.8 Flash"},
            {"model_id": "gemini-3.8-pro", "display_name": "Gemini 3.8 Pro"},
        ],
    }
    res = fmi.ingest(conn, payload, trusted_source="cli:agy")
    assert res["status"] == "ok"
    assert res["projection_action"] == "created"

    snap = fmi.snapshot(conn, now="2026-09-05T12:30:00Z")
    assert "google" in snap["providers"]
    provider_cov = snap["providers"]["google"]
    assert provider_cov["state"] == "fresh"
    assert "gemini-3.8-flash" in provider_cov["available"]
    assert "gemini-3.8-pro" in provider_cov["available"]


def test_expiry_exact_boundary_stale(conn: sqlite3.Connection) -> None:
    payload = {
        "schema": fmi.PAYLOAD_SCHEMA,
        "source": "cli:agy",
        "provider": "google",
        "harness": "agy",
        "scope": "complete",
        "status": "ok",
        "captured_at": "2026-09-05T12:00:00Z",
        "expires_at": "2026-09-05T13:00:00Z",
        "models": ["gemini-3.8-flash"],
    }
    fmi.ingest(conn, payload, trusted_source="cli:agy")

    fresh_snap = fmi.snapshot(conn, now="2026-09-05T12:59:59Z")
    assert fresh_snap["providers"]["google"]["state"] == "fresh"

    # Exact boundary: now == expires_at is evaluated as stale
    boundary_snap = fmi.snapshot(conn, now="2026-09-05T13:00:00Z")
    assert boundary_snap["providers"]["google"]["state"] == "stale"
    assert boundary_snap["providers"]["google"]["reason"] == "inventory-expired"

    # Past boundary is also stale
    past_snap = fmi.snapshot(conn, now="2026-09-05T13:00:01Z")
    assert past_snap["providers"]["google"]["state"] == "stale"


def test_clock_skew_validation() -> None:
    conn = sqlite3.connect(":memory:")
    fmi.ensure_schema(conn)

    # expires_at before captured_at must fail validation
    payload = {
        "schema": fmi.PAYLOAD_SCHEMA,
        "source": "cli:agy",
        "provider": "google",
        "scope": "complete",
        "status": "ok",
        "captured_at": "2026-09-05T14:00:00Z",
        "expires_at": "2026-09-05T13:00:00Z",
        "models": ["gemini-3.8-flash"],
    }
    with pytest.raises(fmi.InventoryValidationError, match="expires_at must be greater than or equal"):
        fmi.ingest(conn, payload, trusted_source="cli:agy")


def test_new_failure_supersedes_old_good(conn: sqlite3.Connection) -> None:
    good_payload = {
        "schema": fmi.PAYLOAD_SCHEMA,
        "source": "cli:opencode",
        "provider": "opencode",
        "harness": "opencode",
        "scope": "complete",
        "status": "ok",
        "captured_at": "2026-09-05T10:00:00Z",
        "expires_at": "2026-09-05T12:00:00Z",
        "models": ["opencode-go/glm-5.2"],
    }
    fmi.ingest(conn, good_payload, trusted_source="cli:opencode")

    snap_good = fmi.snapshot(conn, now="2026-09-05T10:15:00Z")
    assert snap_good["providers"]["opencode"]["state"] == "fresh"

    # Newer error observation for the same coverage
    error_payload = {
        "schema": fmi.PAYLOAD_SCHEMA,
        "source": "cli:opencode",
        "provider": "opencode",
        "harness": "opencode",
        "scope": "complete",
        "status": "error",
        "captured_at": "2026-09-05T10:30:00Z",
        "expires_at": "2026-09-05T12:00:00Z",
        "error_reason": "authentication-revoked",
    }
    fmi.ingest(conn, error_payload, trusted_source="cli:opencode")

    snap_after_error = fmi.snapshot(conn, now="2026-09-05T10:35:00Z")
    assert snap_after_error["providers"]["opencode"]["state"] == "unavailable"
    assert snap_after_error["providers"]["opencode"]["reason"] == "authentication-revoked"


def test_older_snapshot_cannot_win(conn: sqlite3.Connection) -> None:
    newer_payload = {
        "schema": fmi.PAYLOAD_SCHEMA,
        "source": "cli:cursor-agent",
        "provider": "cursor",
        "harness": "cursor",
        "scope": "complete",
        "status": "ok",
        "captured_at": "2026-09-05T12:00:00Z",
        "expires_at": "2026-09-05T14:00:00Z",
        "models": ["composer-2.5"],
    }
    fmi.ingest(conn, newer_payload, trusted_source="cli:cursor-agent")

    older_payload = {
        "schema": fmi.PAYLOAD_SCHEMA,
        "source": "cli:cursor-agent",
        "provider": "cursor",
        "harness": "cursor",
        "scope": "complete",
        "status": "ok",
        "captured_at": "2026-09-05T11:00:00Z",
        "expires_at": "2026-09-05T13:00:00Z",
        "models": ["legacy-model"],
    }
    res_older = fmi.ingest(conn, older_payload, trusted_source="cli:cursor-agent")
    assert res_older["projection_action"] == "ignored_older_snapshot"

    snap = fmi.snapshot(conn, now="2026-09-05T12:30:00Z")
    assert snap["providers"]["cursor"]["available"] == ["composer-2.5"]


def test_same_instant_conflict_marks_unknown(conn: sqlite3.Connection) -> None:
    obs1 = {
        "schema": fmi.PAYLOAD_SCHEMA,
        "source": "cli:cursor-agent",
        "provider": "cursor",
        "harness": "cursor",
        "scope": "complete",
        "status": "ok",
        "captured_at": "2026-09-05T12:00:00Z",
        "expires_at": "2026-09-05T13:00:00Z",
        "models": ["composer-2.5"],
    }
    fmi.ingest(conn, obs1, trusted_source="cli:cursor-agent")

    obs2 = {
        "schema": fmi.PAYLOAD_SCHEMA,
        "source": "cli:cursor-agent",
        "provider": "cursor",
        "harness": "cursor",
        "scope": "complete",
        "status": "error",
        "captured_at": "2026-09-05T12:00:00Z",
        "expires_at": "2026-09-05T13:00:00Z",
        "error_reason": "divergent-read",
    }
    res2 = fmi.ingest(conn, obs2, trusted_source="cli:cursor-agent")
    assert res2["projection_action"] == "conflict_marked_unknown"

    snap = fmi.snapshot(conn, now="2026-09-05T12:15:00Z")
    assert snap["providers"]["cursor"]["state"] == "unknown"
    assert snap["providers"]["cursor"]["reason"] == "same-instant-conflict"


def test_idempotent_equal_observations(conn: sqlite3.Connection) -> None:
    payload = {
        "schema": fmi.PAYLOAD_SCHEMA,
        "source": "cli:agy",
        "provider": "google",
        "harness": "agy",
        "scope": "complete",
        "status": "ok",
        "captured_at": "2026-09-05T12:00:00Z",
        "expires_at": "2026-09-05T13:00:00Z",
        "models": ["gemini-3.8-flash"],
    }
    res1 = fmi.ingest(conn, payload, trusted_source="cli:agy")
    assert res1["projection_action"] == "created"

    res2 = fmi.ingest(conn, payload, trusted_source="cli:agy")
    assert res2["projection_action"] == "idempotent"
    assert res1["observation_id"] == res2["observation_id"]

    # Verify only 1 observation row was inserted
    count = conn.execute("SELECT count(*) FROM fleet_model_inventory_observations").fetchone()[0]
    assert count == 1


def test_complete_vs_partial_inventory_classification(conn: sqlite3.Connection) -> None:
    # 1. Partial inventory: unlisted models are NOT classified as missing
    partial_payload = {
        "schema": fmi.PAYLOAD_SCHEMA,
        "source": "cli:opencode",
        "provider": "opencode",
        "harness": "opencode",
        "scope": "partial",
        "status": "ok",
        "captured_at": "2026-09-05T12:00:00Z",
        "expires_at": "2026-09-05T14:00:00Z",
        "models": [
            {"model_id": "opencode-go/glm-5.2", "state": "available"},
            {"model_id": "opencode-go/retired-model", "state": "retired"},
            {"model_id": "opencode-go/blocked-model", "state": "blocked"},
        ],
    }
    fmi.ingest(conn, partial_payload, trusted_source="cli:opencode")

    # In partial inventory:
    # known available -> available
    cls_avail = fmi.classify_exact_identity(
        conn, provider="opencode", model="opencode-go/glm-5.2", now="2026-09-05T12:30:00Z"
    )
    assert cls_avail["state"] == "available"
    assert cls_avail["scope"] == "partial"

    # known retired -> retired
    cls_ret = fmi.classify_exact_identity(
        conn, provider="opencode", model="opencode-go/retired-model", now="2026-09-05T12:30:00Z"
    )
    assert cls_ret["state"] == "retired"

    # known blocked -> policy-blocked
    cls_blk = fmi.classify_exact_identity(
        conn, provider="opencode", model="opencode-go/blocked-model", now="2026-09-05T12:30:00Z"
    )
    assert cls_blk["state"] == "policy-blocked"

    # unlisted model in partial inventory is UNAVAILABLE, not missing!
    cls_unlisted = fmi.classify_exact_identity(
        conn, provider="opencode", model="opencode-go/unseen-model", now="2026-09-05T12:30:00Z"
    )
    assert cls_unlisted["state"] == "unavailable"
    assert cls_unlisted["reason"] == "uninventoried-in-partial-snapshot"

    # 2. Complete healthy inventory: unlisted models ARE missing
    complete_payload = {
        "schema": fmi.PAYLOAD_SCHEMA,
        "source": "cli:opencode",
        "provider": "opencode",
        "harness": "opencode",
        "scope": "complete",
        "status": "ok",
        "captured_at": "2026-09-05T13:00:00Z",
        "expires_at": "2026-09-05T15:00:00Z",
        "models": ["opencode-go/glm-5.2"],
    }
    fmi.ingest(conn, complete_payload, trusted_source="cli:opencode")

    cls_complete_missing = fmi.classify_exact_identity(
        conn, provider="opencode", model="opencode-go/unseen-model", now="2026-09-05T13:30:00Z"
    )
    assert cls_complete_missing["state"] == "missing"


def test_account_and_harness_distinction(conn: sqlite3.Connection) -> None:
    # Two distinct accounts for the same provider
    payload_acct1 = {
        "schema": fmi.PAYLOAD_SCHEMA,
        "source": "cli:agy",
        "provider": "google",
        "harness": "agy",
        "account_id": "account-pro-1",
        "scope": "complete",
        "status": "ok",
        "captured_at": "2026-09-05T12:00:00Z",
        "expires_at": "2026-09-05T13:00:00Z",
        "models": ["gemini-3.8-pro"],
    }
    payload_acct2 = {
        "schema": fmi.PAYLOAD_SCHEMA,
        "source": "cli:agy",
        "provider": "google",
        "harness": "agy",
        "account_id": "account-flash-2",
        "scope": "complete",
        "status": "ok",
        "captured_at": "2026-09-05T12:00:00Z",
        "expires_at": "2026-09-05T13:00:00Z",
        "models": ["gemini-3.8-flash"],
    }
    fmi.ingest(conn, payload_acct1, trusted_source="cli:agy")
    fmi.ingest(conn, payload_acct2, trusted_source="cli:agy")

    snap = fmi.snapshot(conn, now="2026-09-05T12:15:00Z")
    assert len(snap["coverages"]) == 2

    cov1 = fmi.snapshot(conn, now="2026-09-05T12:15:00Z", account_id="account-pro-1")
    assert cov1["coverages"][0]["available"] == ["gemini-3.8-pro"]

    cov2 = fmi.snapshot(conn, now="2026-09-05T12:15:00Z", account_id="account-flash-2")
    assert cov2["coverages"][0]["available"] == ["gemini-3.8-flash"]


def test_exact_alias_mapping_no_string_guessing(conn: sqlite3.Connection) -> None:
    payload = {
        "schema": fmi.PAYLOAD_SCHEMA,
        "source": "cli:agy",
        "provider": "google",
        "harness": "agy",
        "scope": "complete",
        "status": "ok",
        "captured_at": "2026-09-05T12:00:00Z",
        "expires_at": "2026-09-05T13:00:00Z",
        "models": ["gemini-3.8-flash-low"],
    }
    fmi.ingest(conn, payload, trusted_source="cli:agy")

    # Without alias: "gemini-3.8-flash" does NOT guess or fuzzy match
    no_alias_cls = fmi.classify_exact_identity(
        conn, provider="google", model="gemini-3.8-flash", now="2026-09-05T12:30:00Z"
    )
    assert no_alias_cls["state"] == "missing"

    # With explicit operator alias configured in DB
    fmi.set_alias(
        conn,
        provider="google",
        canonical_model="gemini-3.8-flash",
        native_model="gemini-3.8-flash-low",
        now="2026-09-05T12:00:00Z",
    )
    with_alias_cls = fmi.classify_exact_identity(
        conn, provider="google", model="gemini-3.8-flash", now="2026-09-05T12:30:00Z"
    )
    assert with_alias_cls["state"] == "available"
    assert with_alias_cls["effective_model"] == "gemini-3.8-flash-low"

    # Pass in explicit alias dict
    dict_alias_cls = fmi.classify_exact_identity(
        conn,
        provider="google",
        model="gemini-canonical",
        aliases={"gemini-canonical": "gemini-3.8-flash-low"},
        now="2026-09-05T12:30:00Z",
    )
    assert dict_alias_cls["state"] == "available"
    assert dict_alias_cls["effective_model"] == "gemini-3.8-flash-low"


def test_malformed_and_oversized_payload_rejection(conn: sqlite3.Connection) -> None:
    # 1. Nonfinite floats
    nan_payload = {
        "schema": fmi.PAYLOAD_SCHEMA,
        "source": "cli:agy",
        "provider": "google",
        "scope": "complete",
        "status": "ok",
        "captured_at": "2026-09-05T12:00:00Z",
        "expires_at": "2026-09-05T13:00:00Z",
        "models": ["model-a"],
        "bad_num": float("nan"),
    }
    with pytest.raises(fmi.InventoryValidationError, match="nonfinite float"):
        fmi.ingest(conn, nan_payload, trusted_source="cli:agy")

    inf_payload = {
        "schema": fmi.PAYLOAD_SCHEMA,
        "source": "cli:agy",
        "provider": "google",
        "scope": "complete",
        "status": "ok",
        "captured_at": "2026-09-05T12:00:00Z",
        "expires_at": "2026-09-05T13:00:00Z",
        "models": ["model-a"],
        "bad_num": math.inf,
    }
    with pytest.raises(fmi.InventoryValidationError, match="nonfinite float"):
        fmi.ingest(conn, inf_payload, trusted_source="cli:agy")

    # 2. Credential-bearing URL field
    url_payload = {
        "schema": fmi.PAYLOAD_SCHEMA,
        "source": "cli:agy",
        "provider": "google",
        "scope": "complete",
        "status": "ok",
        "captured_at": "2026-09-05T12:00:00Z",
        "expires_at": "2026-09-05T13:00:00Z",
        "models": ["model-a"],
        "harness": "https://operator:secretpassword@api.example.com",
    }
    with pytest.raises(fmi.InventoryValidationError, match="credential-bearing URL"):
        fmi.ingest(conn, url_payload, trusted_source="cli:agy")

    # 3. Oversized payload
    huge_payload = {
        "schema": fmi.PAYLOAD_SCHEMA,
        "source": "cli:agy",
        "provider": "google",
        "scope": "complete",
        "status": "ok",
        "captured_at": "2026-09-05T12:00:00Z",
        "expires_at": "2026-09-05T13:00:00Z",
        "models": [f"model-{i}" for i in range(1500)],  # exceeds MAX_MODELS_COUNT
    }
    with pytest.raises(fmi.InventoryValidationError, match="models count"):
        fmi.ingest(conn, huge_payload, trusted_source="cli:agy")


def test_trusted_source_mismatch_and_allowlist(conn: sqlite3.Connection) -> None:
    payload = {
        "schema": fmi.PAYLOAD_SCHEMA,
        "source": "cli:agy",
        "provider": "google",
        "scope": "complete",
        "status": "ok",
        "captured_at": "2026-09-05T12:00:00Z",
        "expires_at": "2026-09-05T13:00:00Z",
        "models": ["model-1"],
    }
    # Trusted source differs from payload
    with pytest.raises(fmi.InventorySourceRejectedError, match="does not match caller trusted_source"):
        fmi.ingest(conn, payload, trusted_source="cli:opencode")

    # Trusted source not in allowlist
    with pytest.raises(fmi.InventorySourceRejectedError, match="is not in allowed sources"):
        fmi.ingest(
            conn,
            {**payload, "source": "unauthorized:source"},
            trusted_source="unauthorized:source",
            allowed_sources={"cli:agy"},
        )


def test_safe_error_sanitization_never_leaks_stderr(conn: sqlite3.Connection) -> None:
    payload = {
        "schema": fmi.PAYLOAD_SCHEMA,
        "source": "cli:agy",
        "provider": "google",
        "scope": "complete",
        "status": "error",
        "captured_at": "2026-09-05T12:00:00Z",
        "expires_at": "2026-09-05T13:00:00Z",
        "error_reason": "Fatal: Bearer eyJhbGciOiJIUz... token expired\nTraceback at /internal/path",
    }
    res = fmi.ingest(conn, payload, trusted_source="cli:agy")
    obs = conn.execute(
        "SELECT error_reason FROM fleet_model_inventory_observations WHERE observation_id = ?",
        (res["observation_id"],),
    ).fetchone()
    stored_reason = obs[0]

    # Stored reason must never leak raw stderr, tracebacks, or tokens
    assert "\n" not in stored_reason
    assert "Traceback" not in stored_reason
    assert "Bearer" not in stored_reason
    assert stored_reason == "provider-error"


def test_probe_executable_allowlist_and_mock_runner() -> None:
    executed_commands: list[list[str]] = []

    def mock_runner(argv: list[str], timeout: float) -> tuple[int, str, str]:
        executed_commands.append(argv)
        return (0, "model-1\tModel One\n", "")

    # 1. Non-allowlisted executable is rejected before runner is invoked
    disallowed_result = fmi.run_probe(["evil-tool", "models"], runner=mock_runner)
    assert disallowed_result.returncode == 126
    assert disallowed_result.error == "command-not-allowed"
    assert len(executed_commands) == 0

    # 2. Allowlisted executable runs via runner
    allowed_result = fmi.run_probe(["agy", "models"], runner=mock_runner)
    assert allowed_result.returncode == 0
    assert len(executed_commands) == 1
    assert executed_commands[0] == ["agy", "models"]


def test_parse_agy_models() -> None:
    output = "gemini-3.8-flash-low\tGemini 3.8 Flash (Low)\ngemini-3.8-pro-high\tGemini 3.8 Pro (High)\n"
    records = fmi.parse_agy_models(output)
    assert len(records) == 2
    assert records[0].model_id == "gemini-3.8-flash-low"
    assert records[0].display_name == "Gemini 3.8 Flash (Low)"

    # Empty or unsupported shape raises InventoryValidationError
    with pytest.raises(fmi.InventoryValidationError, match="unsupported output"):
        fmi.parse_agy_models("")

    with pytest.raises(fmi.InventoryValidationError, match="not tab-separated"):
        fmi.parse_agy_models("gemini-flash without tab")


def test_parse_opencode_models() -> None:
    output = "opencode-go/glm-5.2\nopencode-go/kimi-k3\n"
    records = fmi.parse_opencode_models(output)
    assert len(records) == 2
    assert records[0].model_id == "opencode-go/glm-5.2"
    assert records[0].native_id == "opencode-go/glm-5.2"

    with pytest.raises(fmi.InventoryValidationError, match="unsupported output"):
        fmi.parse_opencode_models("")

    with pytest.raises(fmi.InventoryValidationError, match="not a valid provider/model"):
        fmi.parse_opencode_models("not-a-valid-provider-line")


def test_parse_cursor_agent_models() -> None:
    output = (
        "Available models\n\n"
        "composer-2.5 - Cursor Composer 2.5\n"
        "cursor-grok-4.5-high - Grok 4.5 High\n\n"
        "Tip: use --model <id> to switch.\n"
    )
    records = fmi.parse_cursor_agent_models(output)
    assert len(records) == 2
    assert records[0].model_id == "composer-2.5"
    assert records[0].display_name == "Cursor Composer 2.5"

    # Missing tip or header raises InventoryValidationError
    bad_output = "Available models\ncomposer-2.5 - Composer\n"
    with pytest.raises(fmi.InventoryValidationError, match="unsupported output shape"):
        fmi.parse_cursor_agent_models(bad_output)


def test_unsupported_harnesses_report_unsupported_immediately() -> None:
    for harness in ("claudecode", "codex", "jules", "grokbot"):
        payload = fmi.probe_cli_inventory(harness)
        assert payload["status"] == "error"
        assert payload["error_reason"] == "unsupported-harness-no-verified-live-model-list"
        assert payload["models"] == []


def test_probe_cli_inventory_with_synthetic_runner() -> None:
    def mock_runner(argv: list[str], timeout: float) -> tuple[int, str, str]:
        if argv[0] == "agy":
            return (0, "gemini-3.8-flash-low\tGemini 3.8 Flash\n", "")
        if argv[0] == "cursor-agent":
            # Return unsupported shape
            return (0, "unsupported response without header", "")
        return (1, "", "probe failed with stderr")

    # 1. Successful probe
    agy_payload = fmi.probe_cli_inventory("agy", runner=mock_runner, now="2026-09-05T12:00:00Z")
    assert agy_payload["status"] == "ok"
    assert len(agy_payload["models"]) == 1
    assert agy_payload["models"][0]["model_id"] == "gemini-3.8-flash-low"

    # 2. Unsupported output returns unavailable / error, not empty-success
    cursor_payload = fmi.probe_cli_inventory("cursor", runner=mock_runner, now="2026-09-05T12:00:00Z")
    assert cursor_payload["status"] == "error"
    assert cursor_payload["error_reason"] == "unsupported-output-shape"
    assert cursor_payload["models"] == []


def test_manual_browser_payload_and_ingestion(conn: sqlite3.Connection) -> None:
    payload = fmi.build_manual_browser_payload(
        provider="anthropic",
        models=["claude-3-7-sonnet", "claude-opus-5"],
        captured_at="2026-09-05T12:00:00Z",
        expires_at="2026-09-05T14:00:00Z",
        operator="operator-alice",
    )
    assert payload["source"] == "manual:browser"
    assert payload["evidence_type"] == "manual_browser"

    res = fmi.ingest(conn, payload, trusted_source="manual:browser")
    assert res["status"] == "ok"

    snap = fmi.snapshot(conn, now="2026-09-05T12:30:00Z")
    assert snap["providers"]["anthropic"]["evidence_type"] == "manual_browser"
    assert snap["providers"]["anthropic"]["state"] == "fresh"


def test_adaptation_to_fleet_policy_validate_inventory(conn: sqlite3.Connection) -> None:
    # Set up healthy google inventory
    fmi.ingest(
        conn,
        {
            "schema": fmi.PAYLOAD_SCHEMA,
            "source": "cli:agy",
            "provider": "google",
            "harness": "agy",
            "scope": "complete",
            "status": "ok",
            "captured_at": "2026-09-05T12:00:00Z",
            "expires_at": "2026-09-05T14:00:00Z",
            "models": [
                {"model_id": "gemini-3.8-flash-low", "state": "available"},
                {"model_id": "gemini-old", "state": "retired"},
            ],
        },
        trusted_source="cli:agy",
    )

    # Set up stale opencode inventory
    fmi.ingest(
        conn,
        {
            "schema": fmi.PAYLOAD_SCHEMA,
            "source": "cli:opencode",
            "provider": "opencode",
            "harness": "opencode",
            "scope": "complete",
            "status": "ok",
            "captured_at": "2026-09-05T10:00:00Z",
            "expires_at": "2026-09-05T11:00:00Z",
            "models": ["opencode-go/glm-5.2"],
        },
        trusted_source="cli:opencode",
    )

    # Set up partial cursor inventory
    fmi.ingest(
        conn,
        {
            "schema": fmi.PAYLOAD_SCHEMA,
            "source": "cli:cursor-agent",
            "provider": "cursor",
            "harness": "cursor",
            "scope": "partial",
            "status": "ok",
            "captured_at": "2026-09-05T12:00:00Z",
            "expires_at": "2026-09-05T14:00:00Z",
            "models": ["composer-2.5"],
        },
        trusted_source="cli:cursor-agent",
    )

    aliases = {"google": {"gemini-3.8-flash": "gemini-3.8-flash-low"}}
    inv = fmi.to_fleet_policy_inventory(conn, now="2026-09-05T12:30:00Z", aliases=aliases)

    assert inv["google"]["state"] == "fresh"
    assert "gemini-3.8-flash" in inv["google"]["available"]
    assert "gemini-3.8-flash-low" in inv["google"]["available"]
    assert "gemini-old" in inv["google"]["retired"]

    # Opencode is stale -> state is unavailable, cannot claim fresh
    assert inv["opencode"]["state"] == "unavailable"
    assert inv["opencode"]["reason"] == "inventory-expired"

    # Cursor is partial -> state is unavailable, cannot claim fresh
    assert inv["cursor"]["state"] == "unavailable"
    assert inv["cursor"]["reason"] == "partial-inventory-cannot-prove-missing"

    # Now validate against fleet_policy.validate_inventory
    doc: dict[str, Any] = {
        "schema": "brigade.fleet_policy.v1",
        "seats": {
            "seat-google-avail": {
                "provider": "google",
                "model": "gemini-3.8-flash",
            },
            "seat-google-retired": {
                "provider": "google",
                "model": "gemini-old",
            },
            "seat-google-missing": {
                "provider": "google",
                "model": "gemini-nonexistent",
            },
            "seat-opencode-stale": {
                "provider": "opencode",
                "model": "opencode-go/glm-5.2",
            },
            "seat-uninventoried": {
                "provider": "aws",
                "model": "titan",
            },
        },
    }

    report = fleet_policy.validate_inventory(doc, inv)
    seat_states = {row["seat"]: row["state"] for row in report["seats"]}

    assert seat_states["seat-google-avail"] == "available"
    assert seat_states["seat-google-retired"] == "retired"
    assert seat_states["seat-google-missing"] == "missing"
    assert seat_states["seat-opencode-stale"] == "unavailable"
    assert seat_states["seat-uninventoried"] == "unavailable"


def test_exact_coverage_binding_and_no_favorable_ambiguous_projection(conn: sqlite3.Connection) -> None:
    # 1. Ingest two distinct coverages for google under different accounts
    fmi.ingest(
        conn,
        {
            "schema": fmi.PAYLOAD_SCHEMA,
            "source": "cli:agy",
            "provider": "google",
            "harness": "agy",
            "account_id": "acct-1",
            "scope": "complete",
            "status": "ok",
            "captured_at": "2026-09-05T12:00:00Z",
            "expires_at": "2026-09-05T14:00:00Z",
            "models": ["gemini-3.8-flash"],
        },
        trusted_source="cli:agy",
    )
    fmi.ingest(
        conn,
        {
            "schema": fmi.PAYLOAD_SCHEMA,
            "source": "cli:agy",
            "provider": "google",
            "harness": "agy",
            "account_id": "acct-2",
            "scope": "complete",
            "status": "error",
            "captured_at": "2026-09-05T12:00:00Z",
            "expires_at": "2026-09-05T14:00:00Z",
            "error_reason": "probe-failed",
        },
        trusted_source="cli:agy",
    )

    # Without explicit scope, snapshot cannot autoselect favorable data
    snap = fmi.snapshot(conn, now="2026-09-05T12:30:00Z")
    assert snap["providers"]["google"]["state"] == "unavailable"
    assert snap["providers"]["google"]["reason"] == "ambiguous-provider-coverage"
    assert snap["providers"]["google"]["available"] == []

    # to_fleet_policy_inventory without bindings reports ambiguous-provider-coverage
    inv_unbound = fmi.to_fleet_policy_inventory(conn, now="2026-09-05T12:30:00Z")
    assert inv_unbound["google"]["state"] == "unavailable"
    assert inv_unbound["google"]["reason"] == "ambiguous-provider-coverage"

    # With explicit binding map, admission projects the exact configured coverage
    inv_bound_1 = fmi.to_fleet_policy_inventory(
        conn,
        now="2026-09-05T12:30:00Z",
        bindings={"google": {"harness": "agy", "account_id": "acct-1"}},
    )
    assert inv_bound_1["google"]["state"] == "fresh"
    assert "gemini-3.8-flash" in inv_bound_1["google"]["available"]

    inv_bound_2 = fmi.to_fleet_policy_inventory(
        conn,
        now="2026-09-05T12:30:00Z",
        bindings={"google": {"harness": "agy", "account_id": "acct-2"}},
    )
    assert inv_bound_2["google"]["state"] == "unavailable"
    assert inv_bound_2["google"]["reason"] == "probe-failed"

    # Nonexistent binding reports coverage-not-found
    inv_bound_missing = fmi.to_fleet_policy_inventory(
        conn,
        now="2026-09-05T12:30:00Z",
        bindings={"google": {"harness": "agy", "account_id": "nonexistent"}},
    )
    assert inv_bound_missing["google"]["state"] == "unavailable"
    assert inv_bound_missing["google"]["reason"] == "coverage-not-found"

    # classify_exact_identity without filters on DB is ambiguous
    cls_ambiguous = fmi.classify_exact_identity(
        conn, provider="google", model="gemini-3.8-flash", now="2026-09-05T12:30:00Z"
    )
    assert cls_ambiguous["state"] == "unavailable"
    assert cls_ambiguous["reason"] == "ambiguous-provider-coverage"

    # classify_exact_identity with exact filters evaluates matching coverage
    cls_exact = fmi.classify_exact_identity(
        conn,
        provider="google",
        model="gemini-3.8-flash",
        harness="agy",
        account_id="acct-1",
        now="2026-09-05T12:30:00Z",
    )
    assert cls_exact["state"] == "available"

    # classify_exact_identity with non-matching filter
    cls_miss = fmi.classify_exact_identity(
        conn,
        provider="google",
        model="gemini-3.8-flash",
        harness="agy",
        account_id="acct-missing",
        now="2026-09-05T12:30:00Z",
    )
    assert cls_miss["state"] == "unavailable"
    assert cls_miss["reason"] == "coverage-not-found"

    # classify_exact_identity on snapshot mapping directly
    cls_snap_exact = fmi.classify_exact_identity(
        snap,
        provider="google",
        model="gemini-3.8-flash",
        harness="agy",
        account_id="acct-1",
        now="2026-09-05T12:30:00Z",
    )
    assert cls_snap_exact["state"] == "available"


def test_aliases_strictly_scoped_by_provider_and_harness(conn: sqlite3.Connection) -> None:
    fmi.ingest(
        conn,
        {
            "schema": fmi.PAYLOAD_SCHEMA,
            "source": "cli:agy",
            "provider": "google",
            "harness": "agy",
            "scope": "complete",
            "status": "ok",
            "captured_at": "2026-09-05T12:00:00Z",
            "expires_at": "2026-09-05T14:00:00Z",
            "models": ["gemini-3.8-flash-low"],
        },
        trusted_source="cli:agy",
    )
    fmi.ingest(
        conn,
        {
            "schema": fmi.PAYLOAD_SCHEMA,
            "source": "manual:browser",
            "provider": "anthropic",
            "harness": "claude",
            "scope": "complete",
            "status": "ok",
            "captured_at": "2026-09-05T12:00:00Z",
            "expires_at": "2026-09-05T14:00:00Z",
            "models": ["claude-sonnet-4"],
        },
        trusted_source="manual:browser",
    )

    scoped_aliases = {
        "google": {"gemini-seat": "gemini-3.8-flash-low"},
        "anthropic": {"claude-seat": "claude-sonnet-4"},
    }

    inv = fmi.to_fleet_policy_inventory(conn, now="2026-09-05T12:30:00Z", aliases=scoped_aliases)
    # google alias must not leak into anthropic
    assert "gemini-seat" in inv["google"]["available"]
    assert "gemini-seat" not in inv["anthropic"]["available"]
    assert "claude-seat" in inv["anthropic"]["available"]
    assert "claude-seat" not in inv["google"]["available"]

    # classify_exact_identity respects provider scoping
    cls_google = fmi.classify_exact_identity(
        conn, provider="google", model="gemini-seat", aliases=scoped_aliases, now="2026-09-05T12:30:00Z"
    )
    assert cls_google["state"] == "available"
    assert cls_google["effective_model"] == "gemini-3.8-flash-low"

    # Attempting to use the google alias under anthropic should not translate
    cls_anthropic = fmi.classify_exact_identity(
        conn, provider="anthropic", model="gemini-seat", aliases=scoped_aliases, now="2026-09-05T12:30:00Z"
    )
    assert cls_anthropic["state"] == "missing"
    assert cls_anthropic["effective_model"] == "gemini-seat"


def test_bounded_command_output_and_nonfinite_timeout() -> None:
    # 1. Output exceeding 64 KiB returns output-too-large, never truncated complete
    large_output = "model-id\tdisplay\n" * 5000  # > 64 KiB
    oversized_res = fmi.run_probe(["agy", "models"], runner=lambda argv, to: (0, large_output, ""))
    assert oversized_res.returncode == 1
    assert oversized_res.error == "output-too-large"
    assert oversized_res.stdout == ""

    # 2. Nonfinite and negative timeouts return invalid-timeout
    for bad_to in (float("nan"), float("inf"), -5.0, 0.0, True, "15.0"):
        res = fmi.run_probe(["agy", "models"], timeout=bad_to)  # type: ignore[arg-type]
        assert res.returncode == 1
        assert res.error == "invalid-timeout"

    # 3. Valid timeout is bounded between 1.0 and 60.0 passed to runner
    recorded_timeouts: list[float] = []

    def timeout_tracker(argv: list[str], timeout: float) -> tuple[int, str, str]:
        recorded_timeouts.append(timeout)
        return (0, "model-1\tdisplay\n", "")

    fmi.run_probe(["agy", "models"], runner=timeout_tracker, timeout=0.1)
    assert recorded_timeouts[-1] == 1.0

    fmi.run_probe(["agy", "models"], runner=timeout_tracker, timeout=120.0)
    assert recorded_timeouts[-1] == 60.0


def test_naive_and_future_timestamp_rejection_and_exact_expiry(conn: sqlite3.Connection) -> None:
    # 1. Naive timestamps rejected without echoing raw timestamp
    naive_payload = {
        "schema": fmi.PAYLOAD_SCHEMA,
        "source": "cli:agy",
        "provider": "google",
        "scope": "complete",
        "status": "ok",
        "captured_at": "2026-09-05T12:00:00",  # Naive (no offset)
        "expires_at": "2026-09-05T13:00:00Z",
        "models": ["gemini-3.8-flash"],
    }
    with pytest.raises(fmi.InventoryValidationError) as exc_info:
        fmi.ingest(conn, naive_payload, trusted_source="cli:agy")
    assert "naive timestamps rejected" in str(exc_info.value)
    assert "2026-09-05T12:00:00" not in str(exc_info.value)

    # 2. Future timestamp beyond skew rejected
    future_payload = {
        "schema": fmi.PAYLOAD_SCHEMA,
        "source": "cli:agy",
        "provider": "google",
        "scope": "complete",
        "status": "ok",
        "captured_at": "2026-09-05T18:00:00Z",  # 6 hours ahead of now
        "expires_at": "2026-09-05T19:00:00Z",
        "models": ["gemini-3.8-flash"],
    }
    with pytest.raises(fmi.InventoryValidationError, match="captured_at cannot be in the future beyond"):
        fmi.ingest(conn, future_payload, trusted_source="cli:agy", now="2026-09-05T12:00:00Z")

    # 3. Malformed date rejected without echoing raw text
    malformed_payload = {
        "schema": fmi.PAYLOAD_SCHEMA,
        "source": "cli:agy",
        "provider": "google",
        "scope": "complete",
        "status": "ok",
        "captured_at": "secret-token-not-a-dateZ",
        "expires_at": "2026-09-05T13:00:00Z",
        "models": ["gemini-3.8-flash"],
    }
    with pytest.raises(fmi.InventoryValidationError) as mal_exc:
        fmi.ingest(conn, malformed_payload, trusted_source="cli:agy")
    assert "malformed ISO 8601 timestamp" in str(mal_exc.value)
    assert "secret-token-not-a-dateZ" not in str(mal_exc.value)

    # 4. Exact expiry boundary
    fmi.ingest(
        conn,
        {
            "schema": fmi.PAYLOAD_SCHEMA,
            "source": "cli:agy",
            "provider": "google",
            "harness": "agy",
            "account_id": "acct-test",
            "scope": "complete",
            "status": "ok",
            "captured_at": "2026-09-05T12:00:00Z",
            "expires_at": "2026-09-05T13:00:00Z",
            "models": ["gemini-3.8-flash"],
        },
        trusted_source="cli:agy",
        now="2026-09-05T12:00:00Z",
    )
    # now == expires_at: evaluated as stale
    snap_boundary = fmi.snapshot(conn, now="2026-09-05T13:00:00Z")
    assert snap_boundary["providers"]["google"]["state"] == "stale"
    assert snap_boundary["providers"]["google"]["reason"] == "inventory-expired"

    # now < expires_at: evaluated as fresh
    snap_fresh = fmi.snapshot(conn, now="2026-09-05T12:59:59Z")
    assert snap_fresh["providers"]["google"]["state"] == "fresh"

    # 5. Future timestamp in snapshot mapping handled in classify_exact_identity
    future_snap = {
        "schema": fmi.SNAPSHOT_SCHEMA,
        "generated_at": "2026-09-05T18:00:00Z",
        "coverages": [],
        "providers": {},
    }
    cls_future = fmi.classify_exact_identity(
        future_snap,
        provider="google",
        model="gemini-3.8-flash",
        now="2026-09-05T12:00:00Z",
    )
    assert cls_future["state"] == "unavailable"
    assert cls_future["reason"] == "future-timestamp-beyond-skew"


def test_sticky_same_instant_conflict_under_replay(conn: sqlite3.Connection) -> None:
    t = "2026-09-05T12:00:00Z"
    exp = "2026-09-05T14:00:00Z"

    payload_a = {
        "schema": fmi.PAYLOAD_SCHEMA,
        "source": "cli:agy",
        "provider": "google",
        "harness": "agy",
        "account_id": "acct-1",
        "scope": "complete",
        "status": "ok",
        "captured_at": t,
        "expires_at": exp,
        "models": ["gemini-3.8-flash"],
    }
    payload_b = {
        "schema": fmi.PAYLOAD_SCHEMA,
        "source": "cli:agy",
        "provider": "google",
        "harness": "agy",
        "account_id": "acct-1",
        "scope": "complete",
        "status": "ok",
        "captured_at": t,
        "expires_at": exp,
        "models": ["gemini-3.8-pro"],
    }

    res_a = fmi.ingest(conn, payload_a, trusted_source="cli:agy")
    assert res_a["projection_action"] == "created"

    # Conflicting observation at the exact same captured_at timestamp
    res_b = fmi.ingest(conn, payload_b, trusted_source="cli:agy")
    assert res_b["projection_action"] == "conflict_marked_unknown"

    snap = fmi.snapshot(conn, now="2026-09-05T12:30:00Z")
    assert snap["providers"]["google"]["state"] == "unknown"
    assert snap["providers"]["google"]["reason"] == "same-instant-conflict"
    assert snap["providers"]["google"]["available"] == []

    # Replay of payload A must remain sticky in conflict
    res_replay_a = fmi.ingest(conn, payload_a, trusted_source="cli:agy")
    assert res_replay_a["projection_action"] == "conflict_marked_unknown"

    # Replay of payload B must remain sticky in conflict
    res_replay_b = fmi.ingest(conn, payload_b, trusted_source="cli:agy")
    assert res_replay_b["projection_action"] == "conflict_marked_unknown"

    # Older observation must not break out of conflict
    older_payload = {
        "schema": fmi.PAYLOAD_SCHEMA,
        "source": "cli:agy",
        "provider": "google",
        "harness": "agy",
        "account_id": "acct-1",
        "scope": "complete",
        "status": "ok",
        "captured_at": "2026-09-05T11:00:00Z",
        "expires_at": exp,
        "models": ["gemini-3.8-flash"],
    }
    res_older = fmi.ingest(conn, older_payload, trusted_source="cli:agy")
    assert res_older["projection_action"] == "conflict_marked_unknown"

    # Strictly newer observation supersedes conflict
    newer_payload = {
        "schema": fmi.PAYLOAD_SCHEMA,
        "source": "cli:agy",
        "provider": "google",
        "harness": "agy",
        "account_id": "acct-1",
        "scope": "complete",
        "status": "ok",
        "captured_at": "2026-09-05T13:00:00Z",
        "expires_at": "2026-09-05T15:00:00Z",
        "models": ["gemini-3.8-flash", "gemini-3.8-pro"],
    }
    res_newer = fmi.ingest(conn, newer_payload, trusted_source="cli:agy")
    assert res_newer["projection_action"] == "superseded"

    snap_resolved = fmi.snapshot(conn, now="2026-09-05T13:30:00Z")
    assert snap_resolved["providers"]["google"]["state"] == "fresh"
    assert "gemini-3.8-flash" in snap_resolved["providers"]["google"]["available"]


def test_collision_detection_compares_all_stored_fields(conn: sqlite3.Connection) -> None:
    payload = {
        "schema": fmi.PAYLOAD_SCHEMA,
        "source": "cli:agy",
        "provider": "google",
        "harness": "agy",
        "account_id": "acct-1",
        "scope": "complete",
        "status": "ok",
        "captured_at": "2026-09-05T12:00:00Z",
        "expires_at": "2026-09-05T13:00:00Z",
        "models": ["gemini-3.8-flash"],
    }
    res = fmi.ingest(conn, payload, trusted_source="cli:agy")
    obs_id = res["observation_id"]

    # Tamper with a stored field under the same observation_id
    conn.execute(
        "UPDATE fleet_model_inventory_observations SET scope = 'partial' WHERE observation_id = ?",
        (obs_id,),
    )

    # Ingesting original payload again must trigger InventoryCollisionError on stored measurement mismatch
    with pytest.raises(fmi.InventoryCollisionError, match="hash collision detected"):
        fmi.ingest(conn, payload, trusted_source="cli:agy")


def test_outer_transaction_atomic_rollback(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE outer_ledger (entry TEXT)")
    conn.execute("BEGIN")
    conn.execute("INSERT INTO outer_ledger VALUES ('uncommitted-state')")

    # Ingestion failure due to duplicate model ID
    failing_payload = {
        "schema": fmi.PAYLOAD_SCHEMA,
        "source": "cli:agy",
        "provider": "google",
        "scope": "complete",
        "status": "ok",
        "captured_at": "2026-09-05T12:00:00Z",
        "expires_at": "2026-09-05T13:00:00Z",
        "models": ["dup-model", "dup-model"],
    }
    with pytest.raises(fmi.InventoryValidationError, match="duplicate model id in models list"):
        fmi.ingest(conn, failing_payload, trusted_source="cli:agy")

    # Outer transaction must remain intact and uncommitted
    row = conn.execute("SELECT entry FROM outer_ledger").fetchone()
    assert row is not None
    assert row[0] == "uncommitted-state"
    conn.commit()

    committed_row = conn.execute("SELECT entry FROM outer_ledger").fetchone()
    assert committed_row[0] == "uncommitted-state"


def test_strict_json_and_provenance(conn: sqlite3.Connection) -> None:
    base = {
        "schema": fmi.PAYLOAD_SCHEMA,
        "source": "cli:agy",
        "provider": "google",
        "scope": "complete",
        "status": "ok",
        "captured_at": "2026-09-05T12:00:00Z",
        "expires_at": "2026-09-05T13:00:00Z",
        "models": ["gemini-3.8-flash"],
    }

    # 1. Non-list models rejected (e.g. tuple or set)
    bad_tuple_models = dict(base, models=("gemini-3.8-flash",))
    with pytest.raises(fmi.InventoryValidationError):
        fmi.ingest(conn, bad_tuple_models, trusted_source="cli:agy")

    # 2. Unknown keys in payload rejected
    unknown_key_payload = dict(base, extra_field="unexpected")
    with pytest.raises(fmi.InventoryValidationError, match="unknown keys in payload"):
        fmi.ingest(conn, unknown_key_payload, trusted_source="cli:agy")

    # 3. Unknown keys in model record rejected
    unknown_model_key_payload = dict(base, models=[{"model_id": "model-1", "unsupported_prop": 123}])
    with pytest.raises(fmi.InventoryValidationError, match="unknown keys in models"):
        fmi.ingest(conn, unknown_model_key_payload, trusted_source="cli:agy")

    # 4. Credential-bearing URLs rejected without leaking credentials
    cred_payload = dict(base, provider="google http://user:supersecret@example.com/api")
    with pytest.raises(fmi.InventoryValidationError) as cred_exc:
        fmi.ingest(conn, cred_payload, trusted_source="cli:agy")
    assert "supersecret" not in str(cred_exc.value)

    # 5. Evidence type honesty
    # manual:browser cannot claim cli_probe or subscription
    manual_claim_probe = {
        "schema": fmi.PAYLOAD_SCHEMA,
        "source": "manual:browser",
        "provider": "google",
        "scope": "complete",
        "status": "ok",
        "captured_at": "2026-09-05T12:00:00Z",
        "expires_at": "2026-09-05T13:00:00Z",
        "models": ["model-1"],
        "evidence_type": "cli_probe",
    }
    with pytest.raises(fmi.InventoryValidationError, match="evidence_type is not permitted for source"):
        fmi.ingest(conn, manual_claim_probe, trusted_source="manual:browser")


def test_agy_progress_preamble_and_unknown_output_shape() -> None:
    # 1. Output containing preamble lines followed by valid tab-separated rows parses cleanly
    mixed_output = (
        "[info] Loading models...\n"
        "retrieving models\n"
        "gemini-3.8-flash\tGemini 3.8 Flash\n"
        "gemini-3.8-pro\tGemini 3.8 Pro\n"
    )
    records = fmi.parse_agy_models(mixed_output)
    assert len(records) == 2
    assert records[0].model_id == "gemini-3.8-flash"
    assert records[1].model_id == "gemini-3.8-pro"

    # 2. Unexpected unstructured lines raise InventoryValidationError
    bad_output = "[info] Loading models...\nsome unexpected daemon error text\n"
    with pytest.raises(fmi.InventoryValidationError, match="not tab-separated"):
        fmi.parse_agy_models(bad_output)

    # 3. probe_cli_inventory reports unsupported-output-shape, not partial success
    def mock_bad_runner(argv: list[str], timeout: float) -> tuple[int, str, str]:
        return (0, bad_output, "")

    probe_payload = fmi.probe_cli_inventory("agy", runner=mock_bad_runner, now="2026-09-05T12:00:00Z")
    assert probe_payload["status"] == "error"
    assert probe_payload["error_reason"] == "unsupported-output-shape"
    assert probe_payload["models"] == []


def test_grok_models_parser_and_probe() -> None:
    grok_output = "Available models:\n  * grok-2 (latest reasoning)\n  * grok-2-mini\n"
    records = fmi.parse_grok_models(grok_output)
    assert len(records) == 2
    assert records[0].model_id == "grok-2"
    assert records[0].display_name == "grok-2 (latest reasoning)"
    assert records[1].model_id == "grok-2-mini"

    # Missing header raises InventoryValidationError
    with pytest.raises(fmi.InventoryValidationError, match="missing 'Available models:' header"):
        fmi.parse_grok_models("grok-2\ngrok-2-mini\n")

    # probe_cli_inventory parses grok cleanly
    def mock_grok_runner(argv: list[str], timeout: float) -> tuple[int, str, str]:
        assert argv == ["grok", "models"]
        return (0, grok_output, "")

    grok_payload = fmi.probe_cli_inventory("grok", runner=mock_grok_runner, now="2026-09-05T12:00:00Z")
    assert grok_payload["status"] == "ok"
    assert grok_payload["evidence_type"] == "cli_probe"
    assert grok_payload["provider"] == "xai"
    assert len(grok_payload["models"]) == 2


def _mapping_coverage(
    *,
    provider: str,
    harness: str,
    account_id: str,
    models: list[str],
    captured_at: str = "2026-09-05T12:00:00Z",
    expires_at: str = "2026-09-05T13:00:00Z",
    scope: str = "complete",
    status: str = "ok",
    state: str = "fresh",
) -> dict[str, Any]:
    return {
        "provider": provider,
        "harness": harness,
        "account_id": account_id,
        "scope": scope,
        "status": status,
        "state": state,
        "captured_at": captured_at,
        "expires_at": expires_at,
        "available": list(models),
        "retired": [],
        "blocked": [],
        "models": [{"model_id": mid, "native_id": mid, "state": "available"} for mid in models],
    }


def test_policy_inventory_reevaluates_mapping_snapshot_freshness_at_now() -> None:
    # Mapping snapshots can carry a stale precomputed state="fresh". Query-time
    # now must reevaluate captured_at/expires_at, including exact expiry.
    bindings = {"google": {"harness": "agy", "account_id": "acct-1"}}
    coverage = _mapping_coverage(
        provider="google",
        harness="agy",
        account_id="acct-1",
        models=["gemini-3.8-flash"],
        captured_at="2026-09-05T12:00:00Z",
        expires_at="2026-09-05T13:00:00Z",
        state="fresh",
    )
    snap: dict[str, Any] = {
        "schema": fmi.SNAPSHOT_SCHEMA,
        "generated_at": "2026-09-05T12:30:00Z",
        "coverages": [coverage],
        "providers": {"google": coverage},
    }

    inv_fresh = fmi.to_fleet_policy_inventory(snap, now="2026-09-05T12:59:59Z", bindings=bindings)
    assert inv_fresh["google"]["state"] == "fresh"
    assert "gemini-3.8-flash" in inv_fresh["google"]["available"]

    inv_boundary = fmi.to_fleet_policy_inventory(snap, now="2026-09-05T13:00:00Z", bindings=bindings)
    assert inv_boundary["google"]["state"] == "unavailable"
    assert inv_boundary["google"]["reason"] == "inventory-expired"
    assert inv_boundary["google"]["available"] == []

    inv_expired = fmi.to_fleet_policy_inventory(snap, now="2026-09-05T13:00:01Z", bindings=bindings)
    assert inv_expired["google"]["state"] == "unavailable"
    assert inv_expired["google"]["reason"] == "inventory-expired"
    assert inv_expired["google"]["available"] == []

    cls_boundary = fmi.classify_exact_identity(
        snap,
        provider="google",
        model="gemini-3.8-flash",
        harness="agy",
        account_id="acct-1",
        now="2026-09-05T13:00:00Z",
    )
    assert cls_boundary["state"] == "unavailable"
    assert cls_boundary["reason"] == "inventory-expired"

    future_cov = dict(coverage, captured_at="2026-09-05T18:00:00Z", expires_at="2026-09-05T19:00:00Z")
    future_snap = {
        "schema": fmi.SNAPSHOT_SCHEMA,
        "generated_at": "2026-09-05T12:00:00Z",
        "coverages": [future_cov],
        "providers": {"google": future_cov},
    }
    inv_future = fmi.to_fleet_policy_inventory(future_snap, now="2026-09-05T12:00:00Z", bindings=bindings)
    assert inv_future["google"]["state"] == "unavailable"
    assert inv_future["google"]["reason"] == "future-timestamp-beyond-skew"
    assert inv_future["google"]["available"] == []

    for bad_ts, label in (
        (None, "missing"),
        ("2026-09-05T12:00:00", "naive"),
        ("not-a-timestampZ", "malformed"),
    ):
        bad_cov = dict(coverage)
        if bad_ts is None:
            bad_cov.pop("captured_at")
        else:
            bad_cov["captured_at"] = bad_ts
        bad_snap = {
            "schema": fmi.SNAPSHOT_SCHEMA,
            "generated_at": "2026-09-05T12:30:00Z",
            "coverages": [bad_cov],
            "providers": {"google": bad_cov},
        }
        inv_bad = fmi.to_fleet_policy_inventory(bad_snap, now="2026-09-05T12:30:00Z", bindings=bindings)
        assert inv_bad["google"]["state"] == "unavailable", label
        assert inv_bad["google"]["available"] == [], label
        assert str(bad_ts) not in str(inv_bad["google"].get("reason", "")), label


def test_flat_aliases_do_not_cross_providers_in_policy_inventory(conn: sqlite3.Connection) -> None:
    # Both providers list the same native id. A google-scoped alias must not
    # bless anthropic, and a flat {canonical: native} map must not apply to
    # every provider in a multi-provider projection.
    fmi.ingest(
        conn,
        {
            "schema": fmi.PAYLOAD_SCHEMA,
            "source": "cli:agy",
            "provider": "google",
            "harness": "agy",
            "account_id": "acct-google",
            "scope": "complete",
            "status": "ok",
            "captured_at": "2026-09-05T12:00:00Z",
            "expires_at": "2026-09-05T14:00:00Z",
            "models": ["shared-native"],
        },
        trusted_source="cli:agy",
    )
    fmi.ingest(
        conn,
        {
            "schema": fmi.PAYLOAD_SCHEMA,
            "source": "manual:browser",
            "provider": "anthropic",
            "harness": "claude",
            "account_id": "acct-claude",
            "scope": "complete",
            "status": "ok",
            "captured_at": "2026-09-05T12:00:00Z",
            "expires_at": "2026-09-05T14:00:00Z",
            "models": ["shared-native"],
        },
        trusted_source="manual:browser",
    )

    scoped = {"google": {"gemini-seat": "shared-native"}}
    inv_scoped = fmi.to_fleet_policy_inventory(conn, now="2026-09-05T12:30:00Z", aliases=scoped)
    assert "gemini-seat" in inv_scoped["google"]["available"]
    assert "gemini-seat" not in inv_scoped["anthropic"]["available"]
    assert "shared-native" in inv_scoped["anthropic"]["available"]

    flat = {"gemini-seat": "shared-native"}
    inv_flat = fmi.to_fleet_policy_inventory(conn, now="2026-09-05T12:30:00Z", aliases=flat)
    assert "gemini-seat" not in inv_flat["google"]["available"]
    assert "gemini-seat" not in inv_flat["anthropic"]["available"]

    # classify_exact_identity may accept a flat alias only because the provider
    # is already explicitly selected. That does not project across providers.
    cls_google = fmi.classify_exact_identity(
        conn,
        provider="google",
        model="gemini-seat",
        aliases=flat,
        now="2026-09-05T12:30:00Z",
        harness="agy",
        account_id="acct-google",
    )
    assert cls_google["state"] == "available"
    assert cls_google["effective_model"] == "shared-native"

    cls_anthropic_scoped = fmi.classify_exact_identity(
        conn,
        provider="anthropic",
        model="gemini-seat",
        aliases=scoped,
        now="2026-09-05T12:30:00Z",
        harness="claude",
        account_id="acct-claude",
    )
    assert cls_anthropic_scoped["state"] == "missing"
    assert cls_anthropic_scoped["effective_model"] == "gemini-seat"


def test_aliases_do_not_bless_mismatched_coverage(conn: sqlite3.Connection) -> None:
    fmi.ingest(
        conn,
        {
            "schema": fmi.PAYLOAD_SCHEMA,
            "source": "cli:agy",
            "provider": "google",
            "harness": "agy",
            "account_id": "acct-1",
            "scope": "complete",
            "status": "ok",
            "captured_at": "2026-09-05T12:00:00Z",
            "expires_at": "2026-09-05T14:00:00Z",
            "models": ["gemini-3.8-flash-low"],
        },
        trusted_source="cli:agy",
    )
    fmi.ingest(
        conn,
        {
            "schema": fmi.PAYLOAD_SCHEMA,
            "source": "cli:agy",
            "provider": "google",
            "harness": "agy",
            "account_id": "acct-2",
            "scope": "complete",
            "status": "ok",
            "captured_at": "2026-09-05T12:00:00Z",
            "expires_at": "2026-09-05T14:00:00Z",
            "models": ["other-google-model"],
        },
        trusted_source="cli:agy",
    )
    aliases = {"google": {"gemini-seat": "gemini-3.8-flash-low"}}
    inv_acct1 = fmi.to_fleet_policy_inventory(
        conn,
        now="2026-09-05T12:30:00Z",
        aliases=aliases,
        bindings={"google": {"harness": "agy", "account_id": "acct-1"}},
    )
    assert "gemini-seat" in inv_acct1["google"]["available"]
    assert "gemini-3.8-flash-low" in inv_acct1["google"]["available"]

    inv_acct2 = fmi.to_fleet_policy_inventory(
        conn,
        now="2026-09-05T12:30:00Z",
        aliases=aliases,
        bindings={"google": {"harness": "agy", "account_id": "acct-2"}},
    )
    assert "gemini-seat" not in inv_acct2["google"]["available"]
    assert "gemini-3.8-flash-low" not in inv_acct2["google"]["available"]
    assert "other-google-model" in inv_acct2["google"]["available"]

    fmi.set_alias(conn, "google", "stored-seat", "gemini-3.8-flash-low")
    inv_stored_acct2 = fmi.to_fleet_policy_inventory(
        conn,
        now="2026-09-05T12:30:00Z",
        bindings={"google": {"harness": "agy", "account_id": "acct-2"}},
    )
    assert "stored-seat" not in inv_stored_acct2["google"]["available"]


def test_run_probe_proc_result_failure_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def install(result: proc.Result) -> None:
        def fake_run(args: list[str], timeout: float = 30.0, **kwargs: Any) -> proc.Result:
            captured["args"] = list(args)
            captured["timeout"] = timeout
            captured["kwargs"] = kwargs
            return result

        monkeypatch.setattr(proc, "run", fake_run)

    leak = "SECRET_STDERR token=abc Traceback"

    install(
        proc.Result(
            code=0,
            stdout="model-1\tdisplay\n",
            stderr=leak,
            stdout_bytes=len("model-1\tdisplay\n"),
        )
    )
    ok = fmi.run_probe(["agy", "models"], timeout=15.0)
    assert ok.returncode == 0
    assert ok.stdout == "model-1\tdisplay\n"
    assert ok.error is None
    assert captured["args"] == ["agy", "models"]
    assert captured["kwargs"].get("supervise_group") is True
    assert 1.0 <= float(captured["timeout"]) <= 60.0

    flag_cases = (
        (
            proc.Result(code=0, stdout=leak, stderr=leak, output_limit_exceeded=True, stdout_bytes=2_000_000),
            "output-too-large",
        ),
        (
            proc.Result(code=0, stdout=leak, stderr=leak, stream_limit_exceeded=True, stdout_bytes=100),
            "output-too-large",
        ),
        (
            proc.Result(code=0, stdout=leak, stderr=leak, incomplete_process_group=True, stdout_bytes=16),
            "probe-failed",
        ),
        (
            proc.Result(
                code=0,
                stdout=leak,
                stderr=leak,
                stdout_decode_error="child stdout is not valid UTF-8",
                stdout_bytes=16,
            ),
            "probe-failed",
        ),
        (
            proc.Result(code=0, stdout="m\t" + ("x" * (70 * 1024)), stderr=leak, stdout_bytes=70 * 1024),
            "output-too-large",
        ),
        (
            proc.Result(code=124, stdout=leak, stderr=leak, stdout_bytes=8),
            "probe-timed-out",
        ),
        (
            proc.Result(code=127, stdout="", stderr="command not found: agy", stdout_bytes=0),
            "executable-not-found",
        ),
        (
            proc.Result(code=126, stdout="", stderr="command permission denied: agy", stdout_bytes=0),
            "permission-denied",
        ),
    )
    for result, expected_error in flag_cases:
        install(result)
        probe = fmi.run_probe(["agy", "models"])
        assert probe.stdout == ""
        assert probe.error == expected_error
        assert leak not in (probe.error or "")
        assert "Traceback" not in (probe.error or "")
        assert "token=abc" not in (probe.error or "")
        assert "SECRET" not in (probe.error or "")


def test_strict_json_tree_depth_and_node_bounds_are_generic(conn: sqlite3.Connection) -> None:
    nested: Any = {"model_id": "gemini-3.8-flash"}
    for index in range(20):
        nested = {f"LEAK-ME-{index}": nested}
    deep_payload = {
        "schema": fmi.PAYLOAD_SCHEMA,
        "source": "cli:agy",
        "provider": "google",
        "scope": "complete",
        "status": "ok",
        "captured_at": "2026-09-05T12:00:00Z",
        "expires_at": "2026-09-05T13:00:00Z",
        "models": ["gemini-3.8-flash"],
        "operator": nested,
    }
    with pytest.raises(fmi.InventoryValidationError) as deep_exc:
        fmi.ingest(conn, deep_payload, trusted_source="cli:agy")
    deep_msg = str(deep_exc.value)
    assert "maximum depth" in deep_msg
    assert "LEAK-ME-" not in deep_msg
    assert "gemini-3.8-flash" not in deep_msg

    wide: dict[str, Any] = {f"LEAK-NODE-{i}": i for i in range(9000)}
    wide_payload = {
        "schema": fmi.PAYLOAD_SCHEMA,
        "source": "cli:agy",
        "provider": "google",
        "scope": "complete",
        "status": "ok",
        "captured_at": "2026-09-05T12:00:00Z",
        "expires_at": "2026-09-05T13:00:00Z",
        "models": ["gemini-3.8-flash"],
        "operator": wide,
    }
    with pytest.raises(fmi.InventoryValidationError) as wide_exc:
        fmi.ingest(conn, wide_payload, trusted_source="cli:agy")
    wide_msg = str(wide_exc.value)
    assert "maximum node" in wide_msg
    assert "LEAK-NODE-" not in wide_msg
