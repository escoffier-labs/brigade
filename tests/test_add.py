# tests/test_add.py

import json

from brigade import add as add_mod
from brigade import managed
from brigade import station_manifest


def test_add_installs_and_wires_station_tools(monkeypatch, tmp_target, capsys):
    calls = []
    monkeypatch.setattr(managed.proc, "which", lambda c: None)  # not yet installed

    def fake_run(args, **kw):
        calls.append(args)
        return managed.proc.Result(0, "", "")

    monkeypatch.setattr(managed.proc, "run", fake_run)
    rc = add_mod.run(target=tmp_target, station="guard")
    out = capsys.readouterr().out
    assert rc == 0
    # content-guard install args were invoked
    assert any("content-guard" in " ".join(a) for a in calls)
    assert "content-guard" in out


def test_add_unknown_station_errors(tmp_target, capsys):
    rc = add_mod.run(target=tmp_target, station="nope")
    assert rc == 2
    err = capsys.readouterr().err.lower()
    assert "unknown station" in err or "unknown station or tool" in err


def test_add_accepts_managed_tool_name(monkeypatch, tmp_target, capsys):
    calls = []
    monkeypatch.setattr(managed.proc, "which", lambda c: None)

    def fake_run(args, **kw):
        calls.append(args)
        return managed.proc.Result(0, "", "")

    monkeypatch.setattr(managed.proc, "run", fake_run)
    rc = add_mod.run(target=tmp_target, station="graphtrail")
    out = capsys.readouterr().out
    assert rc == 0
    assert "graphtrail" in out
    assert any("cargo" in a for a in calls)


def test_add_skips_install_when_already_present(monkeypatch, tmp_target):
    calls = []
    monkeypatch.setattr(managed.proc, "which", lambda c: "/x/" + c)  # already installed

    def fake_run(args, **kw):
        calls.append(args)
        return managed.proc.Result(0, "", "")

    monkeypatch.setattr(managed.proc, "run", fake_run)
    add_mod.run(target=tmp_target, station="guard")
    # no install argv (pipx/npm) should have run, only wire
    assert not any(a[:1] in (["pipx"], ["npm"], ["pip"]) for a in calls)


def test_add_skills_explains_builtin_and_skillet_paths(tmp_target, capsys):
    rc = add_mod.run(target=tmp_target, station="skills")
    out = capsys.readouterr().out
    assert rc == 0
    assert "ultra-work-scout" in out
    assert "brigade-work" in out
    assert "skills add escoffier-labs/skillet" in out


def _write_station_manifest(path):
    path.mkdir()
    (path / "station.json").write_text(
        json.dumps(
            {
                "schema": station_manifest.SCHEMA,
                "name": "agentpantry",
                "station": "pantry",
                "summary": "agent session auth sync",
                "tools": [
                    {
                        "name": "agentpantry",
                        "command": "agentpantry",
                        "summary": "browser session inventory",
                        "install": ["go", "install", "example.invalid/agentpantry@latest"],
                        "surfaces": [
                            {
                                "kind": "doctor-json",
                                "command": ["agentpantry", "doctor", "--json"],
                                "timeout_seconds": 10,
                            },
                            {
                                "kind": "brief-markdown",
                                "command": ["agentpantry", "inventory", "--markdown"],
                                "max_chars": 4000,
                            },
                        ],
                    }
                ],
            }
        )
    )


def test_add_discovers_local_station_manifest_without_installing(monkeypatch, tmp_target, tmp_path, capsys):
    manifest_dir = tmp_path / "agentpantry"
    _write_station_manifest(manifest_dir)
    calls = []
    monkeypatch.setattr(managed.proc, "which", lambda c: None)
    monkeypatch.setattr(managed.proc, "run", lambda args, **kw: calls.append(args))

    rc = add_mod.run(target=tmp_target, station=str(manifest_dir))

    out = capsys.readouterr().out
    assert rc == 0
    assert calls == []
    assert "station manifest: agentpantry (pantry)" in out
    assert "surface[doctor-json]: agentpantry doctor --json" in out
    assert "surface[brief-markdown]: agentpantry inventory --markdown" in out
    assert "Re-run with `--install`" in out


def test_add_manifest_install_runs_only_when_requested(monkeypatch, tmp_target, tmp_path, capsys):
    manifest_dir = tmp_path / "agentpantry"
    _write_station_manifest(manifest_dir)
    calls = []
    monkeypatch.setattr(managed.proc, "which", lambda c: None)

    def fake_run(args, **kw):
        calls.append(args)
        return managed.proc.Result(0, "", "")

    monkeypatch.setattr(managed.proc, "run", fake_run)

    rc = add_mod.run(target=tmp_target, station=str(manifest_dir / "station.json"), install_manifest=True)

    out = capsys.readouterr().out
    assert rc == 0
    assert calls == [["go", "install", "example.invalid/agentpantry@latest"]]
    assert "install: go install example.invalid/agentpantry@latest" in out


def test_station_manifest_rejects_invalid_schema(tmp_path):
    manifest_dir = tmp_path / "bad"
    manifest_dir.mkdir()
    (manifest_dir / "station.json").write_text(json.dumps({"schema": "bad"}))

    try:
        station_manifest.load(str(manifest_dir))
    except ValueError as exc:
        assert "schema" in str(exc)
    else:
        raise AssertionError("expected ValueError")
