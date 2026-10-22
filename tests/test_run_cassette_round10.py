"""Cassette defaults, export staging, and run-identity regressions."""

import json
import os
import time

import pytest

from brigade import config, run_cassette, work_run_archive
from brigade import run_cassette_store as store
from brigade.selection import Selection
from tests.test_run_cassette import _documents, _record
from tests.test_work_run_archive import RUN_ID, _seed_run


@pytest.mark.parametrize("configuration", [None, {}, {"record": "metadata-only"}])
def test_default_recording_omits_private_bodies(tmp_path, configuration):
    if configuration is not None:
        path = tmp_path / ".brigade" / "config.json"
        path.parent.mkdir()
        path.write_text(json.dumps({"version": 1, "depth": "repo", "harnesses": ["codex"], "run": configuration}))
    run = tmp_path / ".brigade" / "runs" / RUN_ID
    assert _record(run) is not None
    docs = _documents(run)
    assert {d["privacy_class"] for d in docs} == {"scrubbed", "public-metadata"}
    assert "private-worker-answer" not in json.dumps(docs)


def test_configuration_preserves_explicit_private_body_opt_in(tmp_path):
    cfg = config.Config(version=1, selection=Selection(depth="repo", harnesses=["codex"]))
    assert cfg.run_record == "metadata-only"
    config.write_config(tmp_path, cfg)
    assert config.load_config(tmp_path).run_record == "metadata-only"
    cfg.run_record = "auto"
    config.write_config(tmp_path, cfg)
    assert json.loads(config.config_path(tmp_path).read_text())["run"]["record"] == "auto"
    assert config.load_config(tmp_path).run_record == "auto"
    run = tmp_path / ".brigade" / "runs" / RUN_ID
    _record(run)
    assert any(d["privacy_class"] == "private" for d in _documents(run))


def test_export_never_materializes_cassettes_before_stripping(tmp_path, monkeypatch):
    run = _seed_run(tmp_path / "runs" / RUN_ID)
    directory = run / "events" / "cassettes"
    directory.mkdir(parents=True)
    private = directory / "private.json"
    needle = b"adversarial-private-staging-needle"
    private.write_bytes(needle)
    strip = run_cassette.strip_for_export
    observed = []

    def inspect_staging(payload):
        observed.append(payload)
        assert not (payload / "events" / "cassettes").exists()
        assert needle not in b"".join(p.read_bytes() for p in payload.rglob("*") if p.is_file())
        strip(payload)

    monkeypatch.setattr(run_cassette, "strip_for_export", inspect_staging)
    work_run_archive.export_run(run, tmp_path / "export")
    assert len(observed) == 1
    assert private.read_bytes() == needle


@pytest.mark.parametrize("kind", ["missing-identity", "copied-receipt", "staging"])
def test_invalid_run_identity_cannot_displace_retention_or_latest(tmp_path, monkeypatch, kind):
    config.write_config(
        tmp_path,
        config.Config(version=1, selection=Selection(depth="repo", harnesses=["codex"]), run_record="auto"),
    )
    root = tmp_path / ".brigade" / "runs"
    old, current = (root / name for name in ("old", "current"))
    for day, run in enumerate((old, current), start=1):
        _record(run)
        (run / "run.json").write_text(json.dumps({"run_id": run.name, "started_at": f"2026-10-{day:02d}T00:00:00Z"}))
    debris = root / (".current.import-120000-1" if kind == "staging" else "copied")
    debris.mkdir()
    identity = {} if kind == "missing-identity" else {"run_id": debris.name if kind == "staging" else current.name}
    (debris / "run.json").write_text(json.dumps({**identity, "started_at": "2099-01-01T00:00:00Z"}))
    now = time.time()
    for index, run in enumerate((old, current, debris)):
        os.utime(run, (now + index, now + index))
    monkeypatch.setattr(store, "KEEP_RUNS", 2)
    assert store.retain_private(tmp_path, root, current=current) == 0
    assert any(d["privacy_class"] == "private" for d in _documents(old))
    assert run_cassette.purge_cli("latest", cwd=tmp_path) == 0
    assert not _documents(current)
    assert _documents(old)
    assert (debris / "run.json").exists()
