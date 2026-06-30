# tests/test_add.py

from brigade import add as add_mod
from brigade import managed


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
    assert "unknown station" in capsys.readouterr().err.lower()


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
    assert "npx skills add escoffier-labs/skillet" in out
