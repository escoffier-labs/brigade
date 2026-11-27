import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from brigade import code_graph_background, code_graph_delta, component_bins, engine_paths


@pytest.mark.parametrize("signal_type", [KeyboardInterrupt, SystemExit])
def test_capture_before_propagates_process_signals(monkeypatch, tmp_path, signal_type):
    def raise_signal():
        raise signal_type()

    monkeypatch.setattr(code_graph_delta, "_graphtrail_bin", raise_signal)

    with pytest.raises(signal_type):
        code_graph_delta.capture_before(tmp_path, tmp_path / "run")


@pytest.mark.parametrize("signal_type", [KeyboardInterrupt, SystemExit])
def test_capture_after_and_diff_propagates_process_signals(monkeypatch, tmp_path, signal_type):
    def raise_signal(*args, **kwargs):
        raise signal_type()

    before = {
        "ok": True,
        "binary": "engine",
        "db_path": str(engine_paths.code_graph_db_paths(tmp_path)[1]),
        "before_snapshot_path": str(tmp_path / "run" / code_graph_delta.SNAPSHOT_NAME),
    }
    monkeypatch.setattr(code_graph_delta, "_run_graphtrail", raise_signal)

    with pytest.raises(signal_type):
        code_graph_delta.capture_after_and_diff(tmp_path, tmp_path / "run", before)


@pytest.mark.parametrize("signal_type", [KeyboardInterrupt, SystemExit])
def test_compact_summary_propagates_process_signals(signal_type):
    class SignalMapping(dict):
        def get(self, key, default=None):
            raise signal_type()

    with pytest.raises(signal_type):
        code_graph_delta._compact_summary(SignalMapping())


def test_backup_sqlite_honors_timeout(tmp_path):
    src = tmp_path / "src.db"
    dst = tmp_path / "dst.db"
    with sqlite3.connect(src) as con:
        con.execute("create table t (b blob)")
        blob = b"x" * (256 * 1024)
        for _ in range(40):
            con.execute("insert into t values (?)", (blob,))
        con.commit()

    started = time.monotonic()
    with pytest.raises(TimeoutError, match="timeout after"):
        code_graph_delta._backup_sqlite(src, dst, timeout=0.001)
    assert time.monotonic() - started < 0.5
    assert not dst.exists()


def test_capture_before_still_fails_open_for_regular_exceptions(monkeypatch, tmp_path):
    def fail():
        raise OSError("graph unavailable")

    monkeypatch.setattr(code_graph_delta, "_graphtrail_bin", fail)

    result = code_graph_delta.capture_before(Path(tmp_path), tmp_path / "run")

    assert result["status"] == "capture_failed"
    assert "OSError: graph unavailable" in result["summary"]


@pytest.mark.skipif(os.name == "nt", reason="shebang stub engine")
def test_capture_before_indexes_a_fresh_repo_at_the_canonical_path(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    log = tmp_path / "calls.jsonl"
    engine = tmp_path / "engine"
    engine.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sqlite3, sys\n"
        f"with open({str(log)!r}, 'a') as fh:\n"
        "    fh.write(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd()}) + '\\n')\n"
        "db = pathlib.Path(sys.argv[sys.argv.index('--db') + 1])\n"
        "db.parent.mkdir(parents=True, exist_ok=True)\n"
        "sqlite3.connect(db).execute('create table if not exists t (x)').connection.commit()\n"
    )
    engine.chmod(0o755)
    monkeypatch.setattr(component_bins, "resolve", lambda name, **kw: str(engine))
    monkeypatch.delenv(engine_paths.CODE_GRAPH_DB_ENV, raising=False)
    monkeypatch.chdir(tmp_path)

    result = code_graph_delta.capture_before(repo, tmp_path / "run")

    canonical, legacy = engine_paths.code_graph_db_paths(repo)
    assert result["status"] == "captured"
    assert result["db_path"] == str(canonical)
    assert not legacy.parent.exists()
    (call,) = [json.loads(line) for line in log.read_text().splitlines()]
    assert call["argv"] == ["--db", str(canonical), "sync"]
    assert Path(call["cwd"]) == repo


_DIGEST = {"format_version": 2, "schema_version": 9, "sha256": "a" * 64}
_DIGEST_JSON = json.dumps(_DIGEST)


@pytest.fixture
def fake_digest_engine(monkeypatch, tmp_path):
    live_db = tmp_path / ".brigade" / "code-graph" / "code-graph.db"
    live_db.parent.mkdir(parents=True)
    with closing(sqlite3.connect(live_db)) as db:
        db.execute("create table fixture (value text)")
    run_dir = tmp_path / "receipt"
    calls = []
    responses = iter(())
    snapshots = []
    probe_timeouts = []
    after_backup_timeouts = []
    timed_snapshot = code_graph_delta._timed_snapshot

    def snapshot(db, path, timeout, timings, stage):
        snapshots.append(stage)
        if stage == "after":
            after_backup_timeouts.append(timeout)
        return timed_snapshot(db, path, timeout, timings, stage)

    def engine(argv, **kwargs):
        db, command, *args = argv[2:]
        calls.append(command)
        if command == "digest":
            assert args == ["--json"]
            assert Path(db) == (run_dir / code_graph_delta.SNAPSHOT_NAME if calls.count("digest") == 1 else live_db)
            assert 0 < kwargs["timeout"] <= 0.75
            probe_timeouts.append(kwargs["timeout"])
            code, stdout = next(responses)
            if code == "timeout":
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"], output=_DIGEST_JSON)
            if code == "spawn":
                raise OSError("could not launch engine")
            if code == "decode":
                raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid output")
            return subprocess.CompletedProcess(argv, code, stdout, "")
        if command == "diff":
            assert (run_dir / code_graph_delta.SNAPSHOT_AFTER_NAME).is_file()
            if not (run_dir / code_graph_delta.SNAPSHOT_NAME).is_file():
                return subprocess.CompletedProcess(argv, 1, "", "missing snapshot")
            return subprocess.CompletedProcess(argv, 0, '{"summary":{"changed_nodes":1}}', "")
        assert command == "sync"
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(code_graph_delta, "_graphtrail_bin", lambda: "fake-engine")
    monkeypatch.setattr(code_graph_delta, "_timed_snapshot", snapshot)
    monkeypatch.setattr(code_graph_background, "may_run_killable_foreground_sync", lambda: True)
    monkeypatch.setattr(subprocess, "run", engine)
    monkeypatch.delenv(engine_paths.CODE_GRAPH_DB_ENV, raising=False)

    def capture(*digest_responses, snapshot_timeout=None, remove_baseline=False):
        nonlocal responses
        responses = iter(digest_responses)
        before = code_graph_delta.capture_before(
            tmp_path, run_dir, snapshot_timeout=snapshot_timeout, digest_shortcut=True
        )
        if remove_baseline:
            (run_dir / code_graph_delta.SNAPSHOT_NAME).unlink()
        delta = code_graph_delta.capture_after_and_diff(tmp_path, run_dir, before)
        sidecar = json.loads((run_dir / code_graph_delta.SIDECAR_NAME).read_text(encoding="utf-8"))
        return delta, sidecar, calls, snapshots

    return SimpleNamespace(capture=capture, probe_timeouts=probe_timeouts, after_backup_timeouts=after_backup_timeouts)


def test_equal_digest_skips_after_backup_and_diff(fake_digest_engine):
    receipt, sidecar, calls, snapshots = fake_digest_engine.capture((0, _DIGEST_JSON), (0, _DIGEST_JSON))

    assert calls == ["sync", "digest", "sync", "digest"]
    assert snapshots == ["before"]
    for delta in (receipt, sidecar):
        assert delta["status"] == "ok"
        assert delta["delta_source"] == "digest_equal"
        assert delta["digest"] == _DIGEST
        assert delta["raw_counts"]["changed_nodes"] == delta["changed_symbol_count"] == delta["edge_churn"] == 0
        assert delta["code_reference_nodes"] == []
        assert delta["commands"]["diff"] is None
    assert sidecar["attestations"]["before_snapshot_sha256"]
    assert sidecar["attestations"]["after_snapshot_sha256"] is None
    assert sidecar["attestations"]["diff_stdout_sha256"] is None
    assert sidecar["snapshot_deleted"] is True


@pytest.mark.parametrize(
    "before,after",
    [
        pytest.param((0, _DIGEST_JSON), (0, json.dumps({**_DIGEST, "sha256": "b" * 64})), id="different-content"),
        pytest.param((2, ""), None, id="unsupported-command"),
        pytest.param((7, _DIGEST_JSON), None, id="nonzero-exit-with-valid-output"),
        pytest.param(("timeout", ""), None, id="timeout-with-valid-output"),
        pytest.param(("spawn", ""), None, id="launch-error"),
        pytest.param(("decode", ""), None, id="encoding-error"),
        pytest.param((0, "{"), None, id="malformed-json"),
        pytest.param((0, "[]"), None, id="non-object"),
        pytest.param((0, '{"schema_version":9,"sha256":"abc"}'), None, id="missing-format"),
        pytest.param((0, '{"format_version":2,"sha256":"abc"}'), None, id="missing-schema"),
        pytest.param((0, '{"format_version":2,"schema_version":9}'), None, id="missing-sha"),
        pytest.param((0, json.dumps({**_DIGEST, "format_version": 3})), None, id="unknown-format"),
        pytest.param((0, json.dumps({**_DIGEST, "format_version": 2.0})), None, id="noninteger-format"),
        pytest.param((0, json.dumps({**_DIGEST, "schema_version": 10})), None, id="unexpected-baseline-schema"),
        pytest.param((0, json.dumps({**_DIGEST, "schema_version": "9"})), None, id="noninteger-schema"),
        pytest.param((0, json.dumps({**_DIGEST, "sha256": "g" * 64})), None, id="nonhex-sha"),
        pytest.param((0, json.dumps({**_DIGEST, "sha256": "a" * 63})), None, id="short-sha"),
        pytest.param((0, json.dumps({**_DIGEST, "sha256": 123})), None, id="nonstring-sha"),
        pytest.param((0, _DIGEST_JSON[:-1] + ', "schema_version": 9}'), None, id="duplicate-field"),
        pytest.param((0, _DIGEST_JSON), (0, json.dumps({**_DIGEST, "schema_version": 10})), id="schema-changed"),
        pytest.param((0, _DIGEST_JSON), (7, _DIGEST_JSON), id="after-probe-fails"),
        pytest.param((0, _DIGEST_JSON), ("timeout", ""), id="after-probe-times-out"),
    ],
)
def test_digest_fallback_uses_after_backup_and_diff(fake_digest_engine, before, after):
    receipt, sidecar, calls, snapshots = fake_digest_engine.capture(before, *([after] if after is not None else []))

    assert calls == ["sync", "digest", "sync"] + (["digest"] if after is not None else []) + ["diff"]
    assert snapshots == ["before", "after"]
    assert receipt["status"] == "ok"
    assert "delta_source" not in receipt
    assert receipt["raw_counts"] == {"changed_nodes": 1}
    assert sidecar["attestations"]["after_snapshot_sha256"]
    assert sidecar["attestations"]["diff_stdout_sha256"]


def test_digest_fallback_preserves_diff_failure_when_baseline_disappears(fake_digest_engine):
    receipt, _, calls, snapshots = fake_digest_engine.capture((0, _DIGEST_JSON), remove_baseline=True)

    assert calls == ["sync", "digest", "sync", "diff"]
    assert snapshots == ["before", "after"]
    assert receipt["status"] == "diff_failed"


def test_digest_probes_share_spare_baseline_capture_budget(fake_digest_engine, monkeypatch):
    now = 0.0
    real_run = code_graph_delta._run_graphtrail

    def run(binary, db, command, *args, **kwargs):
        nonlocal now
        result = real_run(binary, db, command, *args, **kwargs)
        if command == "digest" and db.name == code_graph_delta.SNAPSHOT_NAME:
            now += 0.375
        return result

    monkeypatch.setattr(code_graph_delta, "time", SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(code_graph_delta, "_run_graphtrail", run)

    fake_digest_engine.capture((0, _DIGEST_JSON), (3, ""), snapshot_timeout=0.625)

    assert fake_digest_engine.probe_timeouts == [0.625, 0.25]
    assert fake_digest_engine.after_backup_timeouts == [10.0]


def test_background_only_graph_capture_does_not_probe_digest(fake_digest_engine, monkeypatch):
    monkeypatch.setattr(code_graph_background, "may_run_killable_foreground_sync", lambda: False)
    monkeypatch.setattr(code_graph_background, "status_or_start", lambda *args, **kwargs: None)
    monkeypatch.setattr(code_graph_background, "index_ready", lambda db: True)
    monkeypatch.setattr(code_graph_background, "last_engine_lock_refusal", lambda *args: None)

    receipt, _, calls, snapshots = fake_digest_engine.capture()

    assert calls == ["diff"]
    assert snapshots == ["before", "after"]
    assert receipt["stale_graph_used"] is True


def test_equal_digest_with_native_engine(monkeypatch, tmp_path):

    if not code_graph_background.may_run_killable_foreground_sync():
        pytest.skip("requires foreground graph capture")
    engine = shutil.which("brigade-code-graph")
    if engine is None:
        pytest.skip("requires the native code graph engine")
    help_result = subprocess.run([engine, "digest", "--help"], capture_output=True, check=False, timeout=10)
    if help_result.returncode != 0:
        pytest.skip("requires a native code graph engine with digest support")
    monkeypatch.setattr(code_graph_delta, "_graphtrail_bin", lambda: engine)
    monkeypatch.delenv(engine_paths.CODE_GRAPH_DB_ENV, raising=False)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, timeout=10)
    (tmp_path / "source.py").write_text("def first():\n    pass\n", encoding="utf-8")
    run_dir = tmp_path / ".brigade" / "receipt"

    before = code_graph_delta.capture_before(tmp_path, run_dir, digest_shortcut=True)
    receipt = code_graph_delta.capture_after_and_diff(tmp_path, run_dir, before)

    assert receipt["delta_source"] == "digest_equal"
    assert receipt["digest"]["schema_version"] == 9
    assert set(receipt["snapshots"]) == {"before"}
