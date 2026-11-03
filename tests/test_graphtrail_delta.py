import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from brigade import component_bins, engine_paths, graphtrail_delta


@pytest.mark.parametrize("signal_type", [KeyboardInterrupt, SystemExit])
def test_capture_before_propagates_process_signals(monkeypatch, tmp_path, signal_type):
    def raise_signal():
        raise signal_type()

    monkeypatch.setattr(graphtrail_delta, "_graphtrail_bin", raise_signal)

    with pytest.raises(signal_type):
        graphtrail_delta.capture_before(tmp_path, tmp_path / "run")


@pytest.mark.parametrize("signal_type", [KeyboardInterrupt, SystemExit])
def test_capture_after_and_diff_propagates_process_signals(monkeypatch, tmp_path, signal_type):
    def raise_signal(*args, **kwargs):
        raise signal_type()

    before = {
        "ok": True,
        "binary": "engine",
        "db_path": str(engine_paths.code_graph_db_paths(tmp_path)[1]),
        "before_snapshot_path": str(tmp_path / "run" / graphtrail_delta.SNAPSHOT_NAME),
    }
    monkeypatch.setattr(graphtrail_delta, "_run_graphtrail", raise_signal)

    with pytest.raises(signal_type):
        graphtrail_delta.capture_after_and_diff(tmp_path, tmp_path / "run", before)


@pytest.mark.parametrize("signal_type", [KeyboardInterrupt, SystemExit])
def test_compact_summary_propagates_process_signals(signal_type):
    class SignalMapping(dict):
        def get(self, key, default=None):
            raise signal_type()

    with pytest.raises(signal_type):
        graphtrail_delta._compact_summary(SignalMapping())


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
        graphtrail_delta._backup_sqlite(src, dst, timeout=0.001)
    assert time.monotonic() - started < 0.5
    assert not dst.exists()


def test_capture_before_still_fails_open_for_regular_exceptions(monkeypatch, tmp_path):
    def fail():
        raise OSError("graph unavailable")

    monkeypatch.setattr(graphtrail_delta, "_graphtrail_bin", fail)

    result = graphtrail_delta.capture_before(Path(tmp_path), tmp_path / "run")

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
    monkeypatch.delenv(engine_paths.LEGACY_CODE_GRAPH_DB_ENV, raising=False)
    monkeypatch.chdir(tmp_path)

    result = graphtrail_delta.capture_before(repo, tmp_path / "run")

    canonical, legacy = engine_paths.code_graph_db_paths(repo)
    assert result["status"] == "captured"
    assert result["db_path"] == str(canonical)
    assert not legacy.parent.exists()
    (call,) = [json.loads(line) for line in log.read_text().splitlines()]
    assert call["argv"] == ["--db", str(canonical), "sync"]
    assert Path(call["cwd"]) == repo
