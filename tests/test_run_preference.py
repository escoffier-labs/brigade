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


def test_cache_round_trip(tmp_path) -> None:
    pref = run_preference.RunPreference(impl="cursor_grok", review="claude_standby")
    path = run_preference.write_cached(pref, tmp_path)
    assert path == tmp_path / ".brigade" / "run-preference.toml"
    loaded = run_preference.load_cached(tmp_path)
    assert loaded == pref


def test_hub_preference_get_put_and_rejects_secrets(tmp_path) -> None:
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    empty = fleet_hub.get_run_preference(conn)
    assert empty == {"impl": None, "review": None, "chef": None, "notes": None}
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
