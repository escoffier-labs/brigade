"""care install must never delete or rewrite operator runbooks."""

from __future__ import annotations

import json

from brigade import care_cmd, memory_cmd


def _seed(target):
    assert memory_cmd._write_memory_care_runbooks(target) == 0
    runbooks = memory_cmd._memory_care_runbooks_dir(target)
    operator = runbooks / "evidence-crawl.json"
    operator.write_text(json.dumps({"id": "evidence-crawl", "steps": [{"id": "x", "run": "true"}]}), encoding="utf-8")
    customized = runbooks / "ingest-sweep.json"
    customized.write_text(json.dumps({"id": "ingest-sweep", "approved": True, "steps": []}), encoding="utf-8")
    # A template added by a later release is missing from this older workspace.
    (runbooks / memory_cmd.MEMORY_CARE_RUNBOOK_NAMES[0]).unlink()
    return runbooks, {p.name: p.read_bytes() for p in runbooks.iterdir()}


def test_dry_run_writes_no_runbooks(tmp_path, monkeypatch):
    monkeypatch.setattr(care_cmd, "_is_windows", lambda: False)
    target = tmp_path / "ws"
    target.mkdir()
    runbooks, before = _seed(target)

    assert care_cmd.install(target=target, backend="systemd", home=tmp_path / "home", dry_run=True) == 0

    assert {p.name: p.read_bytes() for p in runbooks.iterdir()} == before


def test_install_adds_missing_templates_and_keeps_every_other_runbook(tmp_path, monkeypatch):
    monkeypatch.setattr(care_cmd, "_is_windows", lambda: False)
    target = tmp_path / "ws"
    target.mkdir()
    runbooks, before = _seed(target)

    assert care_cmd._ensure_memory_care_runbooks(target) == 0

    after = {p.name: p.read_bytes() for p in runbooks.iterdir()}
    for name, data in before.items():
        assert after[name] == data, name
    assert (runbooks / memory_cmd.MEMORY_CARE_RUNBOOK_NAMES[0]).is_file()


def _seed_edited_shipped_runbook(target):
    runbooks = memory_cmd._memory_care_runbooks_dir(target)
    runbooks.mkdir(parents=True, exist_ok=True)
    edited = runbooks / "ingest-sweep.json"
    edited.write_text(json.dumps({"id": "ingest-sweep", "approved": True, "steps": []}), encoding="utf-8")
    return runbooks, edited, edited.read_bytes()


def _assert_missing_shipped_runbooks_added(runbooks, edited_name):
    for name in memory_cmd.MEMORY_CARE_RUNBOOK_NAMES:
        if name != edited_name:
            assert (runbooks / name).is_file(), name


def test_memory_care_init_keeps_edited_runbook_without_force(tmp_path):
    target = tmp_path / "ws"
    target.mkdir()
    runbooks, edited, original = _seed_edited_shipped_runbook(target)

    assert memory_cmd.init(target=target, with_runbooks=True) == 0

    assert edited.read_bytes() == original
    _assert_missing_shipped_runbooks_added(runbooks, edited.name)


def test_memory_care_force_init_replaces_edited_shipped_runbook_only_after_a_backup(tmp_path):
    target = tmp_path / "ws"
    target.mkdir()
    assert memory_cmd.init(target=target) == 0
    runbooks, edited, original = _seed_edited_shipped_runbook(target)

    assert memory_cmd.init(target=target, force=True, with_runbooks=True) == 0

    assert edited.read_bytes() != original
    assert [path.read_bytes() for path in runbooks.glob(f"{edited.name}.bak-*")] == [original]
    _assert_missing_shipped_runbooks_added(runbooks, edited.name)


def test_memory_care_init_directory_at_runbook_name_fails_cleanly(tmp_path, capsys):
    target = tmp_path / "ws"
    target.mkdir()
    (memory_cmd._memory_care_runbooks_dir(target) / "ingest-sweep.json").mkdir(parents=True)

    assert memory_cmd.init(target=target, with_runbooks=True) == 2
    assert "ingest-sweep.json" in capsys.readouterr().err
