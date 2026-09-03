"""Fleet/local run preference overlay (#1223)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from brigade import fleet_hub, run_preference


@dataclass(frozen=True)
class _FakeRoster:
    orchestrator: str
    agents: dict[str, object]


def test_parse_preference_rejects_secret_material() -> None:
    with pytest.raises(run_preference.RunPreferenceError, match="secret"):
        run_preference.parse_preference({"token": "abc"})
    with pytest.raises(run_preference.RunPreferenceError, match="home paths"):
        run_preference.parse_preference({"notes": "see /home/operator/roster.toml"})
    with pytest.raises(run_preference.RunPreferenceError, match="unknown"):
        run_preference.parse_preference({"impl": "cursor_grok", "hub_url": "http://example"})


def test_apply_to_roster_uses_local_chef_and_ignores_missing() -> None:
    roster = _FakeRoster(orchestrator="chef", agents={"chef": object(), "cursor_grok": object()})
    pinned = run_preference.RunPreference(chef="cursor_grok", impl="cursor_grok")
    assert run_preference.apply_to_roster(roster, pinned).orchestrator == "cursor_grok"
    missing = run_preference.RunPreference(chef="absent")
    assert run_preference.apply_to_roster(roster, missing) is roster


def test_apply_to_task_skips_prefix_when_worker_is_set() -> None:
    pref = run_preference.RunPreference(impl="cursor_grok", review="claude_standby")
    task = "fix the flaky test"
    assert "default impl: cursor_grok" in run_preference.apply_to_task(task, pref, worker=None)
    assert run_preference.apply_to_task(task, pref, worker="coder") == task


def test_resolve_worker_honors_impl_unless_overridden() -> None:
    roster = _FakeRoster(orchestrator="chef", agents={"chef": object(), "cursor_grok": object(), "reviewer": object()})
    pref = run_preference.RunPreference(impl="cursor_grok", review="claude_standby")
    assert run_preference.resolve_worker(pref, roster, worker=None, task="fix the flaky test") == "cursor_grok"
    assert run_preference.resolve_worker(pref, roster, worker="reviewer", task="fix the flaky test") == "reviewer"
    # A spoken roster seat name suppresses the pin: the orchestrator plans and
    # sees the named seat, rather than the whole task being direct-dispatched.
    assert run_preference.resolve_worker(pref, roster, worker=None, task="have reviewer inspect the diff") is None
    assert run_preference.resolve_worker(pref, roster, worker=None, task="ask chef and reviewer") is None
    missing = run_preference.RunPreference(impl="absent")
    assert run_preference.resolve_worker(missing, roster, worker=None, task="fix the flaky test") is None


def test_resolve_worker_without_pin_never_dispatches_on_prose() -> None:
    roster = _FakeRoster(orchestrator="chef", agents={"chef": object(), "cursor_grok": object(), "reviewer": object()})
    unpinned = run_preference.RunPreference()
    assert run_preference.resolve_worker(unpinned, roster, worker=None, task="have reviewer inspect the diff") is None
    assert run_preference.resolve_worker(unpinned, roster, worker=None, task="fix the flaky test") is None


def test_resolve_worker_never_resolves_to_orchestrator() -> None:
    roster = _FakeRoster(orchestrator="chef", agents={"chef": object(), "cursor_grok": object()})
    pinned_chef = run_preference.RunPreference(impl="chef")
    assert run_preference.resolve_worker(pinned_chef, roster, worker=None, task="fix the flaky test") is None


def test_cache_round_trip(tmp_path) -> None:
    pref = run_preference.RunPreference(impl="cursor_grok", review="claude_standby")
    path = run_preference.write_cached(pref, tmp_path)
    assert path == tmp_path / ".brigade" / "run-preference.toml"
    loaded = run_preference.load_cached(tmp_path)
    assert loaded == pref


def test_hub_preference_get_put_and_rejects_secrets(tmp_path) -> None:
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    empty = fleet_hub.get_run_preference(conn)
    assert empty == {
        "impl": None,
        "review": None,
        "chef": None,
        "research": None,
        "security": None,
        "scout": None,
        "notes": None,
    }
    stored = fleet_hub.set_run_preference(
        conn,
        {"impl": "cursor_grok", "review": "claude_standby"},
        updated_by="admin",
    )
    assert stored["impl"] == "cursor_grok"
    assert stored["review"] == "claude_standby"
    assert fleet_hub.get_run_preference(conn)["impl"] == "cursor_grok"
    with pytest.raises(fleet_hub.FleetHubError, match="secret|home paths|unknown"):
        fleet_hub.set_run_preference(conn, {"token": "leak"}, updated_by="admin")
    conn.close()


def test_refresh_cache_keeps_local_copy_when_hub_is_down(tmp_path, monkeypatch) -> None:
    pref = run_preference.RunPreference(impl="cursor_grok")
    run_preference.write_cached(pref, tmp_path)
    monkeypatch.setattr(
        "brigade.fleet_client.fetch_run_preference",
        lambda: (_ for _ in ()).throw(RuntimeError("hub down")),
    )
    assert run_preference.refresh_cache(home=tmp_path) == pref


def test_fleet_preference_cli_get_set_pull(tmp_path, monkeypatch, capsys) -> None:
    from brigade import cli

    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path / "home"))
    stored = {"impl": "cursor_grok", "review": "claude_standby"}

    def fake_fetch():
        return dict(stored)

    def fake_put(preference, *, hub_url=None):
        stored.clear()
        stored.update(preference)
        return dict(stored)

    monkeypatch.setattr("brigade.fleet_client.fetch_run_preference", fake_fetch)
    monkeypatch.setattr("brigade.fleet_client.put_run_preference", fake_put)
    assert cli.main(["fleet", "preference", "set", "--impl", "cursor_grok", "--review", "claude_standby"]) == 0
    assert cli.main(["fleet", "preference", "get", "--json"]) == 0
    assert cli.main(["fleet", "preference", "pull"]) == 0
    out = capsys.readouterr().out
    assert "cursor_grok" in out
    assert "claude_standby" in out


def test_print_preference_sanitizes_control_characters(capsys) -> None:
    from brigade.cli import fleet as fleet_cli

    fleet_cli._print_preference(
        {
            "impl": "cursor_grok",
            "review": None,
            "chef": None,
            "notes": "see \x1b[31mred",
        },
        source="hub",
    )
    out = capsys.readouterr().out
    assert "impl: cursor_grok" in out
    assert "review: -" in out
    assert "chef: -" in out
    assert "\x1b" not in out
    assert "\\x1b[31mred" in out


def test_role_fields_parse_round_trip_and_prefix(tmp_path) -> None:
    raw = {
        "impl": "agy_flash",
        "review": "claude_standby",
        "chef": "chef",
        "research": "researcher",
        "security": "daybreak",
        "scout": "cursor_scout",
        "notes": "cursor via Other Models only",
    }
    pref = run_preference.parse_preference(raw)
    assert pref.research == "researcher"
    assert pref.security == "daybreak"
    assert pref.scout == "cursor_scout"
    assert pref.payload() == raw
    assert run_preference.ROLE_FIELDS == ("impl", "review", "chef", "research", "security", "scout")
    run_preference.write_cached(pref, tmp_path)
    assert run_preference.load_cached(tmp_path) == pref
    prefix = pref.planner_prefix()
    assert "- default research: researcher" in prefix
    assert "- default security: daybreak" in prefix
    assert "- default scout: cursor_scout" in prefix
    assert prefix.index("default chef") < prefix.index("default research")


def test_role_fields_are_seat_names_and_never_dispatch() -> None:
    with pytest.raises(run_preference.RunPreferenceError, match="roster seat name"):
        run_preference.parse_preference({"security": "not a seat"})
    roster = _FakeRoster(orchestrator="chef", agents={"chef": object(), "daybreak": object()})
    pref = run_preference.RunPreference(security="daybreak")
    assert run_preference.resolve_worker(pref, roster, worker=None, task="scan the repo") is None
    # "security" must not trip the secret-key regex.
    assert run_preference.parse_preference({"security": "daybreak"}).security == "daybreak"


def test_hub_preference_stores_roles_and_meta(tmp_path) -> None:
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    assert fleet_hub.get_run_preference_meta(conn) == {"updated_at": None, "updated_by": None}
    stored = fleet_hub.set_run_preference(conn, {"security": "daybreak", "scout": "cursor_scout"}, updated_by="admin")
    assert stored["security"] == "daybreak"
    assert stored["scout"] == "cursor_scout"
    meta = fleet_hub.get_run_preference_meta(conn)
    assert meta["updated_by"] == "admin"
    assert isinstance(meta["updated_at"], str) and meta["updated_at"]
    conn.close()


def test_hub_preference_v18_row_survives_v19_migration(tmp_path) -> None:
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE run_preference (id INTEGER PRIMARY KEY CHECK (id = 1), impl TEXT, review TEXT, "
        "chef TEXT, notes TEXT, updated_at TEXT NOT NULL, updated_by TEXT)"
    )
    old.execute(
        "INSERT INTO run_preference VALUES (1, 'coder', 'claude_standby', 'chef', 'kept', "
        "'2026-01-01T00:00:00+00:00', 'admin')"
    )
    old.execute("PRAGMA user_version=18")
    old.commit()
    old.close()
    conn = fleet_hub.init_db(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == fleet_hub.SCHEMA_VERSION == 19
    pref = fleet_hub.get_run_preference(conn)
    assert pref["impl"] == "coder" and pref["notes"] == "kept"
    assert pref["research"] is None and pref["security"] is None and pref["scout"] is None
    conn.close()
