from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from brigade import aboyeur, cli, doctor as doctor_mod, node, run_id, runguard, runs_cmd
from brigade.station import DoctorContext


def test_node_identity_persists_across_two_inits(tmp_path, monkeypatch):
    monkeypatch.setattr(node.socket, "gethostname", lambda: "testhost.example")
    first = node.ensure_identity(tmp_path)
    second = node.ensure_identity(tmp_path)
    assert first.node_id == second.node_id
    assert first.short_id == second.short_id
    assert first.hostname == "testhost"
    assert first.roles == ()
    assert first.platform
    payload = (tmp_path / ".brigade" / "node.toml").read_text()
    assert first.node_id in payload
    third = node.ensure_identity(tmp_path)
    assert third.node_id == first.node_id


def test_node_cli_inits_and_prints_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(node.socket, "gethostname", lambda: "testhost.example")
    assert cli.main(["node", "--target", str(tmp_path), "--json"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["node_id"]
    assert first["short_id"] == node.short_node_id(first["node_id"])
    assert first["hostname"] == "testhost"
    assert first["roles"] == []
    assert cli.main(["node", "--target", str(tmp_path), "--json"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["node_id"] == first["node_id"]


def test_node_cli_text_form(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(node.socket, "gethostname", lambda: "testhost.example")
    assert cli.main(["node", "--target", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "node_id:" in out
    assert "short_id:" in out
    assert "hostname: testhost" in out
    assert "roles: (none)" in out


def test_run_id_namespacing_round_trip():
    local = "20260823-135400-abcd1234"
    namespaced = run_id.format_run_id("a1b2c3d4", local)
    assert namespaced == "a1b2c3d4.20260823-135400-abcd1234"
    node_short, parsed_local = run_id.parse_run_id(namespaced)
    assert node_short == "a1b2c3d4"
    assert parsed_local == local
    assert run_id.parse_run_id(local) == (None, local)
    assert run_id.lookup_names(local, local_short="a1b2c3d4") == [local, namespaced]
    assert run_id.lookup_names(namespaced, local_short="a1b2c3d4") == [namespaced, local]
    assert run_id.run_id_matches(namespaced, local, local_short="a1b2c3d4")
    assert run_id.run_id_matches(local, local, local_short="a1b2c3d4")
    assert not run_id.run_id_matches(namespaced, local, local_short="ffffffff")


def test_unnamespaced_legacy_run_id_still_resolves(tmp_path, monkeypatch):
    monkeypatch.setattr(node.socket, "gethostname", lambda: "testhost.example")
    workspace = tmp_path / "workspace"
    runs_root = workspace / ".brigade" / "runs"
    legacy_id = "20260823-135400-abcd1234"
    legacy_dir = runs_root / legacy_id
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "run.json").write_text(json.dumps({"schema": "brigade.run.v1", "status": "ok"}))
    resolved, error = runs_cmd._resolve_run_dir(legacy_id, cwd=workspace)
    assert error is None
    assert resolved == legacy_dir.resolve()

    identity = node.ensure_identity(workspace)
    namespaced = run_id.format_run_id(identity.short_id, legacy_id)
    namespaced_dir = runs_root / namespaced
    namespaced_dir.mkdir()
    (namespaced_dir / "run.json").write_text(json.dumps({"schema": "brigade.run.v1", "status": "ok"}))
    # Exact legacy directory still wins when both exist.
    resolved_legacy, error = runs_cmd._resolve_run_dir(legacy_id, cwd=workspace)
    assert error is None
    assert resolved_legacy == legacy_dir.resolve()


def test_make_run_dir_prefixes_local_short_id(tmp_path, monkeypatch):
    monkeypatch.setattr(node.socket, "gethostname", lambda: "testhost.example")
    workspace = tmp_path / "workspace"
    runs = workspace / ".brigade" / "runs"
    runs.mkdir(parents=True)
    now = datetime(2026, 8, 23, 13, 54, 0, tzinfo=timezone.utc)
    minted = aboyeur.make_run_dir(runs, now)
    identity = node.ensure_identity(workspace)
    assert minted.parent == runs
    node_short, local = run_id.parse_run_id(minted.name)
    assert node_short == identity.short_id
    assert local.startswith("20260823-135400-")


def _write_lock(workspace: Path, run_dir: Path, *, pid: int, status: str) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "schema": "brigade.run.v1",
                "status": status,
                "finished_at": "2026-08-23T12:00:00+00:00" if status == "ok" else None,
            }
        )
    )
    lock_path = runguard.lock_path(workspace)
    lock_path.mkdir(parents=True)
    (lock_path / "pid").write_text(f"{pid}\n")
    (lock_path / "owner.json").write_text(
        json.dumps(
            {
                "schema": "brigade.run_lock.v1",
                "owner_token": "owner",
                "pid": pid,
                "run_dir": str(run_dir.resolve()),
                "acquired_at": "2026-08-23T11:00:00+00:00",
            }
        )
    )
    return lock_path


def test_reconcile_releases_dead_owner_terminal_lock(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    run_dir = tmp_path / "done-run"
    lock_path = _write_lock(repo, run_dir, pid=99999999, status="ok")
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)
    assert runguard.inspect_run_lock_reconcile(repo) == "unreconciled"
    assert runguard.reconcile_run_lock(repo) == "released"
    assert not lock_path.exists()
    assert json.loads((run_dir / "run.json").read_text())["status"] == "ok"


def test_reconcile_leaves_active_owner_lock_alone(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    run_dir = tmp_path / "live-run"
    lock_path = _write_lock(repo, run_dir, pid=777777, status="ok")
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: True)
    assert runguard.inspect_run_lock_reconcile(repo) == "live"
    assert runguard.reconcile_run_lock(repo) == "live"
    assert lock_path.is_dir()
    assert json.loads((lock_path / "owner.json").read_text())["pid"] == 777777


def test_doctor_reports_unreconciled_stale_lock(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("# agents\n")
    run_dir = workspace / ".brigade" / "runs" / "20260823-120000-abcd1234"
    _write_lock(workspace, run_dir, pid=99999999, status="ok")
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)
    check = doctor_mod._check_unreconciled_run_lock(workspace)
    assert check[0] == doctor_mod.WARN
    assert check[1] == "run lock: unreconciled"
    ctx = DoctorContext(target=workspace, selection=None, harnesses=[])
    names = [item[1] for item in doctor_mod.core_station_checks(ctx)]
    assert "run lock: unreconciled" in names


def test_next_claim_releases_dead_owner_terminal_lock(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    run_dir = tmp_path / "done-run"
    lock_path = _write_lock(repo, run_dir, pid=99999999, status="ok")
    (run_dir / "events").mkdir()
    (run_dir / "events" / "lifecycle.jsonl").write_text("")
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)
    with runguard.run_lock(repo, run_dir=tmp_path / "new-run"):
        owner = json.loads((lock_path / "owner.json").read_text())
        assert Path(str(owner["run_dir"])).resolve() == (tmp_path / "new-run").resolve()
    assert json.loads((run_dir / "run.json").read_text())["status"] == "ok"
