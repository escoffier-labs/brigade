"""One-release role alias, canonical storage and frozen v1 preference output."""

import pytest

from brigade import fleet_hub, fleet_hub_preference, fleet_policy, run_preference


@pytest.mark.parametrize("key", ["chef", "orchestrator"])
def test_preference_alias_cache_and_overlay(tmp_path, key):
    pref = run_preference.parse_preference({key: "planner"})
    assert pref.orchestrator == "planner"
    assert pref.payload() == {"orchestrator": "planner"}
    path = run_preference.write_cached(pref, tmp_path)
    assert "chef" not in path.read_text(encoding="utf-8")
    assert run_preference.load_cached(tmp_path) == pref


def test_policy_alias_normalization_preserves_historical_digest():
    old = fleet_policy.empty_document()
    old["defaults"] = {"roles": {"chef": "planner"}}
    digest = fleet_policy.document_digest(old)
    canonical = fleet_policy.canonical_role_document(old)
    assert canonical["defaults"]["roles"] == {"orchestrator": "planner"}
    assert fleet_policy.document_digest(old) == digest
    assert fleet_policy.resolve_policy(old, None, None)["effective"]["roles"] == {"orchestrator": "planner"}


def test_v47_preference_column_upgrade_and_v1_response(tmp_path):
    path = tmp_path / "hub.db"
    conn = fleet_hub.init_db(path)
    conn.execute("ALTER TABLE run_preference RENAME COLUMN orchestrator TO chef")
    conn.execute("INSERT INTO run_preference (id, chef, updated_at) VALUES (1, 'planner', 'before')")
    conn.execute("PRAGMA user_version=47")
    conn.commit()
    conn.close()
    conn = fleet_hub.init_db(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == fleet_hub.SCHEMA_VERSION
    assert fleet_hub_preference.raw_run_preference(conn)["orchestrator"] == "planner"
    assert "chef" not in {row[1] for row in conn.execute("PRAGMA table_info(run_preference)")}
    assert fleet_hub.get_run_preference(conn)["chef"] == "planner"
    conn.close()
    # The old-code version fence refuses this DB before touching its columns.
    from unittest.mock import patch

    current_version = fleet_hub.SCHEMA_VERSION
    with patch.object(fleet_hub, "SCHEMA_VERSION", 47):
        with pytest.raises(fleet_hub.FleetHubError, match=f"schema version {current_version}"):
            fleet_hub.init_db(path)


def test_legacy_cache_and_canonical_precedence(tmp_path):
    path = run_preference.cache_path(tmp_path)
    path.parent.mkdir()
    path.write_text("chef = 'planner'\n")
    assert run_preference.load_cached(tmp_path).orchestrator == "planner"
    pref = run_preference.parse_preference({"chef": "old", "orchestrator": "new"})
    assert pref.payload() == {"orchestrator": "new"}


def test_new_policy_write_and_activated_v1_projection(tmp_path):
    from brigade import fleet_hub_policy, fleet_policy_migration

    conn = fleet_hub.init_db(tmp_path / "hub.db")
    document = fleet_policy.empty_document()
    document["defaults"] = {"roles": {"chef": "planner"}}
    saved = fleet_hub_policy.save_policy(conn, document, expected_version=1, actor="test", reason="alias")
    assert saved["document"]["defaults"]["roles"] == {"orchestrator": "planner"}
    assert fleet_policy_migration.projected_run_preference(conn)["orchestrator"] == "planner"
    from unittest.mock import patch

    with patch.object(fleet_policy_migration, "is_activated", return_value=True):
        assert fleet_hub.get_run_preference(conn)["chef"] == "planner"
    conn.close()


def test_brief_uses_canonical_role_without_changing_seat(capsys):
    from brigade.work_cmd.session.fleet_routing import print_fleet_routing

    pref = run_preference.parse_preference({"chef": "planner"})
    print_fleet_routing({"roles": pref.payload()})
    output = capsys.readouterr().out
    assert "orchestrator=planner" in output
    assert "chef" not in output
