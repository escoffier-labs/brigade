import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from brigade import code_graph_background, code_graph_delta, component_bins, engine_paths, localio, work_cmd

_ENGINE_PATH_ATTR = "_graphtrail_bin"
_ENGINE_RUN_ATTR = "_run_graphtrail"


@pytest.mark.parametrize("signal_type", [KeyboardInterrupt, SystemExit])
def test_capture_before_propagates_process_signals(monkeypatch, tmp_path, signal_type):
    def raise_signal():
        raise signal_type()

    monkeypatch.setattr(code_graph_delta, _ENGINE_PATH_ATTR, raise_signal)

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
    monkeypatch.setattr(code_graph_delta, _ENGINE_RUN_ATTR, raise_signal)

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

    monkeypatch.setattr(code_graph_delta, _ENGINE_PATH_ATTR, fail)

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
    run_subprocess = subprocess.run
    sync_results = iter(())

    def snapshot(db, path, timeout, timings, stage):
        snapshots.append(stage)
        if stage == "after":
            after_backup_timeouts.append(timeout)
        return timed_snapshot(db, path, timeout, timings, stage)

    def engine(argv, *args, **kwargs):
        if not isinstance(argv, list) or argv[:1] != ["fake-engine"]:
            return run_subprocess(argv, *args, **kwargs)
        db, command, *args = argv[2:]
        calls.append(command)
        if command == "digest":
            assert args == ["--json"]
            if calls.count("digest") == 1:
                assert Path(db).name == code_graph_delta.SNAPSHOT_NAME
            else:
                assert Path(db) == live_db
            assert 0 < kwargs["timeout"] <= 0.75
            probe_timeouts.append(kwargs["timeout"])
            code, stdout = next(responses)
            if code == "timeout":
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"], output=_DIGEST_JSON)
            if code == "spawn":
                raise OSError("could not launch engine")
            if code == "decode":
                raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid output")
            if code == "unsupported":
                return subprocess.CompletedProcess(argv, 2, "", "error: unrecognized subcommand 'digest'")
            return subprocess.CompletedProcess(argv, code, stdout, "")
        if command == "diff":
            assert (run_dir / code_graph_delta.SNAPSHOT_AFTER_NAME).is_file()
            if not (run_dir / code_graph_delta.SNAPSHOT_NAME).is_file():
                return subprocess.CompletedProcess(argv, 1, "", "missing snapshot")
            return subprocess.CompletedProcess(argv, 0, '{"summary":{"changed_nodes":1}}', "")
        assert command == "sync"
        code = next(sync_results, 0)
        if code == "timeout":
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return subprocess.CompletedProcess(argv, code, "", "")

    monkeypatch.setattr(code_graph_delta, _ENGINE_PATH_ATTR, lambda: "fake-engine")
    monkeypatch.setattr(code_graph_delta, "_timed_snapshot", snapshot)
    monkeypatch.setattr(code_graph_background, "may_run_killable_foreground_sync", lambda *args: True)
    monkeypatch.setattr(code_graph_delta, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(subprocess, "run", engine)
    monkeypatch.delenv(engine_paths.CODE_GRAPH_DB_ENV, raising=False)

    def respond(*digest_responses):
        nonlocal responses
        responses = iter(digest_responses)

    def capture(
        *digest_responses,
        snapshot_timeout=None,
        remove_baseline=False,
        change_baseline=False,
        before_sync=0,
        after_sync=0,
        sync_timeout=None,
        **before_options,
    ):
        nonlocal sync_results
        respond(*digest_responses)
        sync_results = iter((before_sync, after_sync))
        before = code_graph_delta.capture_before(tmp_path, run_dir, snapshot_timeout=snapshot_timeout, **before_options)
        if remove_baseline:
            (run_dir / code_graph_delta.SNAPSHOT_NAME).unlink()
        if change_baseline:
            with closing(sqlite3.connect(run_dir / code_graph_delta.SNAPSHOT_NAME)) as db, db:
                # Changing an excluded table keeps the content digest equal.
                db.execute("CREATE TABLE meta (value text)")
        delta = code_graph_delta.capture_after_and_diff(tmp_path, run_dir, before, sync_timeout=sync_timeout)
        sidecar = json.loads((run_dir / code_graph_delta.SIDECAR_NAME).read_text(encoding="utf-8"))
        return delta, sidecar, calls, snapshots

    return SimpleNamespace(
        capture=capture,
        respond=respond,
        calls=calls,
        probe_timeouts=probe_timeouts,
        after_backup_timeouts=after_backup_timeouts,
    )


def test_equal_digest_skips_after_backup_and_diff(fake_digest_engine):
    receipt, sidecar, calls, snapshots = fake_digest_engine.capture(
        (0, _DIGEST_JSON), (0, _DIGEST_JSON), digest_shortcut=True
    )

    assert calls == ["sync", "digest", "sync", "digest"]
    assert snapshots == ["before"]
    for delta in (receipt, sidecar):
        assert delta["status"] == "ok"
        assert delta["delta_source"] == "digest_equal"
        assert delta["digest"] == _DIGEST
        assert delta["raw_counts"]["changed_nodes"] == delta["changed_symbol_count"] == delta["edge_churn"] == 0
        assert delta["code_reference_nodes"] == []
        assert delta["commands"]["diff"] is None
        assert delta["digest_probe"].keys() == {"before", "after"}
        for probe in delta["digest_probe"].values():
            assert {key: value for key, value in probe.items() if key != "duration_seconds"} == {
                "status": "ok",
                "format_version": 2,
                "schema_version": 9,
            }
            assert isinstance(probe["duration_seconds"], float)
    assert sidecar["attestations"]["before_snapshot_sha256"]
    assert sidecar["attestations"]["after_snapshot_sha256"] is None
    assert sidecar["attestations"]["diff_stdout_sha256"] is None
    assert sidecar["snapshot_deleted"] is True


@pytest.mark.parametrize(
    "before,after,expected_status",
    [
        pytest.param((0, _DIGEST_JSON), (0, json.dumps({**_DIGEST, "sha256": "b" * 64})), "ok", id="different-content"),
        pytest.param(("unsupported", ""), None, "unsupported", id="unsupported-command"),
        pytest.param((7, _DIGEST_JSON), None, "error", id="nonzero-exit-with-valid-output"),
        pytest.param(("timeout", ""), None, "timeout", id="timeout-with-valid-output"),
        pytest.param(("spawn", ""), None, "error", id="launch-error"),
        pytest.param(("decode", ""), None, "error", id="encoding-error"),
        pytest.param((0, "{"), None, "malformed", id="malformed-json"),
        pytest.param((0, "[]"), None, "malformed", id="non-object"),
        pytest.param((0, '{"schema_version":9,"sha256":"abc"}'), None, "malformed", id="missing-format"),
        pytest.param((0, '{"format_version":2,"sha256":"abc"}'), None, "malformed", id="missing-schema"),
        pytest.param((0, '{"format_version":2,"schema_version":9}'), None, "malformed", id="missing-sha"),
        pytest.param(
            (0, json.dumps({**_DIGEST, "format_version": 3})), None, "format_version_unexpected", id="unknown-format"
        ),
        pytest.param((0, json.dumps({**_DIGEST, "format_version": 2.0})), None, "malformed", id="noninteger-format"),
        pytest.param(
            (0, json.dumps({**_DIGEST, "schema_version": 10})),
            None,
            "schema_version_unexpected",
            id="unexpected-baseline-schema",
        ),
        pytest.param((0, json.dumps({**_DIGEST, "schema_version": "9"})), None, "malformed", id="noninteger-schema"),
        pytest.param((0, json.dumps({**_DIGEST, "sha256": "g" * 64})), None, "malformed", id="nonhex-sha"),
        pytest.param((0, json.dumps({**_DIGEST, "sha256": "a" * 63})), None, "malformed", id="short-sha"),
        pytest.param((0, json.dumps({**_DIGEST, "sha256": 123})), None, "malformed", id="nonstring-sha"),
        pytest.param((0, _DIGEST_JSON[:-1] + ', "schema_version": 9}'), None, "malformed", id="duplicate-field"),
        pytest.param(
            (0, _DIGEST_JSON),
            (0, json.dumps({**_DIGEST, "schema_version": 10})),
            "schema_version_unexpected",
            id="schema-changed",
        ),
        pytest.param((0, _DIGEST_JSON), (7, _DIGEST_JSON), "error", id="after-probe-fails"),
        pytest.param((0, _DIGEST_JSON), ("timeout", ""), "timeout", id="after-probe-times-out"),
        pytest.param((0, json.dumps({**_DIGEST, "format_version": True})), None, "malformed", id="boolean-format"),
        pytest.param((0, json.dumps({**_DIGEST, "schema_version": True})), None, "malformed", id="boolean-schema"),
    ],
)
def test_digest_fallback_uses_after_backup_and_diff(fake_digest_engine, before, after, expected_status):
    receipt, sidecar, calls, snapshots = fake_digest_engine.capture(
        before, *([after] if after is not None else []), digest_shortcut=True
    )

    assert calls == ["sync", "digest", "sync"] + (["digest"] if after is not None else []) + ["diff"]
    assert snapshots == ["before", "after"]
    assert receipt["status"] == "ok"
    assert "delta_source" not in receipt
    assert receipt["raw_counts"] == {"changed_nodes": 1}
    assert sidecar["attestations"]["after_snapshot_sha256"]
    assert sidecar["attestations"]["diff_stdout_sha256"]
    probes = sidecar["digest_probe"]
    assert probes.keys() == ({"before", "after"} if after is not None else {"before"})
    probe = probes["after" if after is not None else "before"]
    assert probe["status"] == expected_status
    assert isinstance(probe["duration_seconds"], float)
    assert probe.keys() <= {"status", "duration_seconds", "format_version", "schema_version"}
    if expected_status.endswith("_unexpected"):
        key = expected_status.removesuffix("_unexpected")
        assert probe[key] == (3 if key == "format_version" else 10)


def test_digest_fallback_preserves_diff_failure_when_baseline_disappears(fake_digest_engine):
    receipt, _, calls, snapshots = fake_digest_engine.capture(
        (0, _DIGEST_JSON), remove_baseline=True, digest_shortcut=True
    )

    assert calls == ["sync", "digest", "sync", "diff"]
    assert snapshots == ["before", "after"]
    assert receipt["status"] == "diff_failed"


@pytest.mark.parametrize(
    "budget,before_elapsed,expected_timeouts",
    [(0.625, 0.375, [0.625, 0.25]), (10.0, 0.875, [0.75, 0.625]), (0.625, 0.625, [0.625])],
)
def test_digest_probes_share_spare_baseline_capture_budget(
    fake_digest_engine, monkeypatch, budget, before_elapsed, expected_timeouts
):
    now = 0.0
    real_run = getattr(code_graph_delta, _ENGINE_RUN_ATTR)

    def run(binary, db, command, *args, **kwargs):
        nonlocal now
        result = real_run(binary, db, command, *args, **kwargs)
        if command == "digest" and db.name == code_graph_delta.SNAPSHOT_NAME:
            now += before_elapsed
        return result

    monkeypatch.setattr(code_graph_delta, "time", SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(code_graph_delta, _ENGINE_RUN_ATTR, run)

    receipt, sidecar, calls, snapshots = fake_digest_engine.capture(
        (0, _DIGEST_JSON), (3, ""), snapshot_timeout=budget, digest_shortcut=True
    )

    assert fake_digest_engine.probe_timeouts == expected_timeouts
    assert sidecar["digest_probe"]["before"]["duration_seconds"] == before_elapsed
    after_timeout = expected_timeouts[1] if len(expected_timeouts) == 2 else 0
    assert before_elapsed + after_timeout <= min(budget, 1.5)
    if not after_timeout:
        assert sidecar["digest_probe"]["after"] == {"status": "skipped_budget"}
    assert calls.count("digest") == len(expected_timeouts)
    assert snapshots == ["before", "after"]
    assert receipt["status"] == "ok"
    assert fake_digest_engine.after_backup_timeouts == [10.0]


def test_background_only_graph_capture_does_not_probe_digest(fake_digest_engine, monkeypatch):
    monkeypatch.setattr(code_graph_background, "may_run_killable_foreground_sync", lambda *args: False)
    monkeypatch.setattr(code_graph_background, "status_or_start", lambda *args, **kwargs: None)
    monkeypatch.setattr(code_graph_background, "index_ready", lambda db: True)
    monkeypatch.setattr(code_graph_background, "last_engine_lock_refusal", lambda *args: None)

    receipt, _, calls, snapshots = fake_digest_engine.capture(digest_shortcut=True)

    assert calls == ["diff"]
    assert snapshots == ["before", "after"]
    assert receipt["stale_graph_used"] is True


def test_digest_versions_match_native_source():
    engine = Path(__file__).resolve().parents[1] / "engines" / "code-graph" / "src"
    for name, source, expected in (
        ("FORMAT_VERSION", engine / "query" / "digest.rs", code_graph_delta._DIGEST_FORMAT_VERSION),
        ("SCHEMA_VERSION", engine / "store" / "schema.rs", code_graph_delta._DIGEST_SCHEMA_VERSION),
    ):
        definition = re.search(rf"\bconst {name}: u32 = (\d+);", source.read_text(encoding="utf-8"))
        assert definition, f"Cannot find {name} in {source.name}"
        assert int(definition[1]) == expected, f"{source.name} {name} changed. Update the Python digest contract."


def test_verify_run_records_equal_digest_sidecar_and_summary(fake_digest_engine, monkeypatch, tmp_path, capsys):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, timeout=10)
    monkeypatch.setattr(code_graph_background, "index_ready", lambda db: True)
    monkeypatch.setattr(code_graph_background, "status_or_start", lambda *args, **kwargs: None)
    fake_digest_engine.respond((0, _DIGEST_JSON), (0, _DIGEST_JSON))

    assert work_cmd.verify_run(target=tmp_path, commands=[[sys.executable, "-V"]], json_output=True, reuse=False) == 0

    receipt = json.loads(capsys.readouterr().out)
    delta = receipt["code_graph_delta"]
    sidecar_path = Path(delta["sidecar_path"])
    stored = json.loads((sidecar_path.parent / "receipt.json").read_text(encoding="utf-8"))
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert stored["code_graph_delta"] == delta
    assert delta["delta_source"] == sidecar["delta_source"] == "digest_equal"
    assert delta["digest"] == sidecar["digest"] == _DIGEST
    assert (
        receipt["digests"]["logs"]["graph-delta.json"] == delta["sidecar_sha256"] == localio.file_sha256(sidecar_path)
    )
    summary = "code graph delta: ok changed_symbols=0 edge_churn=0"
    assert delta["summary"].startswith(summary)
    assert f"- Code graph delta: {delta['summary']}" in (sidecar_path.parent / "summary.md").read_text(encoding="utf-8")
    assert fake_digest_engine.calls == ["sync", "digest", "sync", "digest"]


def test_digest_shortcut_disabled_by_default(fake_digest_engine):
    receipt, sidecar, calls, snapshots = fake_digest_engine.capture()

    assert calls == ["sync", "sync", "diff"]
    assert snapshots == ["before", "after"]
    assert receipt["status"] == "ok"
    assert "digest_probe" not in sidecar


def test_baseline_snapshot_exhausts_digest_budget(fake_digest_engine, monkeypatch):
    take_snapshot = code_graph_delta._timed_snapshot

    def snapshot(db, path, timeout, timings, stage):
        sha256 = take_snapshot(db, path, timeout, timings, stage)
        if stage == "before":
            timings[stage]["duration_seconds"] = timeout
        return sha256

    monkeypatch.setattr(code_graph_delta, "_timed_snapshot", snapshot)
    receipt, sidecar, calls, snapshots = fake_digest_engine.capture(digest_shortcut=True)

    assert calls == ["sync", "sync", "diff"]
    assert snapshots == ["before", "after"]
    assert receipt["status"] == "ok"
    assert "digest_probe" not in sidecar


def test_modified_baseline_forces_diff_even_with_equal_engine_digests(fake_digest_engine):
    receipt, sidecar, calls, snapshots = fake_digest_engine.capture(
        (0, _DIGEST_JSON), (0, _DIGEST_JSON), change_baseline=True, digest_shortcut=True
    )

    assert calls == ["sync", "digest", "sync", "diff"]
    assert snapshots == ["before", "after"]
    assert receipt["raw_counts"] == {"changed_nodes": 1}
    assert sidecar["digest_probe"]["after"] == {"status": "skipped_baseline_changed"}


def test_baseline_modified_during_after_digest_forces_diff(fake_digest_engine, monkeypatch, tmp_path):
    read_digest = code_graph_delta._read_graph_digest
    snapshot = tmp_path / "receipt" / code_graph_delta.SNAPSHOT_NAME

    def digest(binary, db, target, probe, *, timeout):
        if db == snapshot:
            return read_digest(binary, db, target, probe, timeout=timeout)
        with snapshot.open("ab") as file:
            file.write(b"x")
        probe.update(status="ok", format_version=2, schema_version=9, duration_seconds=0.0)
        return _DIGEST

    monkeypatch.setattr(code_graph_delta, "_read_graph_digest", digest)
    receipt, sidecar, calls, snapshots = fake_digest_engine.capture((0, _DIGEST_JSON), digest_shortcut=True)

    assert calls == ["sync", "digest", "sync", "diff"]
    assert snapshots == ["before", "after"]
    assert receipt["raw_counts"] == {"changed_nodes": 1}
    assert sidecar["digest_probe"].keys() == {"before", "after"}
    assert sidecar["digest_probe"]["after"]["status"] == "ok"


@pytest.mark.parametrize("failure", [7, "timeout"])
def test_after_sync_failure_prevents_after_digest(fake_digest_engine, monkeypatch, failure):
    monkeypatch.setattr(code_graph_background, "status_or_start", lambda *args, **kwargs: None)
    monkeypatch.setattr(code_graph_background, "index_ready", lambda db: True)
    receipt, sidecar, calls, snapshots = fake_digest_engine.capture(
        (0, _DIGEST_JSON), after_sync=failure, sync_timeout=0.5, digest_shortcut=True
    )

    assert sidecar["digest_probe"].keys() == {"before"}
    if failure == "timeout":
        assert calls == ["sync", "digest", "sync", "diff"]
        assert snapshots == ["before", "after"]
        assert receipt["stale_after"] is True
    else:
        assert calls == ["sync", "digest", "sync"]
        assert snapshots == ["before"]
        assert receipt["status"] == "sync_failed"


def test_posix_stale_baseline_prevents_digest(fake_digest_engine):
    receipt, sidecar, calls, snapshots = fake_digest_engine.capture(before_sync="timeout", digest_shortcut=True)

    assert calls == ["sync", "sync", "diff"]
    assert snapshots == ["before", "after"]
    assert receipt["stale_graph_used"] is True
    assert "digest_probe" not in sidecar


@pytest.fixture
def native_digest_baseline(monkeypatch, tmp_path):
    if not code_graph_background.may_run_killable_foreground_sync():
        pytest.skip("requires foreground graph capture")
    engine = shutil.which("brigade-code-graph")
    if engine is None:
        pytest.skip("requires the native code graph engine")
    help_result = subprocess.run([engine, "digest", "--help"], capture_output=True, check=False, timeout=10)
    if help_result.returncode != 0:
        pytest.skip("requires a native code graph engine with digest support")
    monkeypatch.setattr(code_graph_delta, _ENGINE_PATH_ATTR, lambda: engine)
    monkeypatch.delenv(engine_paths.CODE_GRAPH_DB_ENV, raising=False)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, timeout=10)
    (tmp_path / "source.py").write_text("def first():\n    pass\n", encoding="utf-8")
    run_dir = tmp_path / ".brigade" / "receipt"

    before = code_graph_delta.capture_before(tmp_path, run_dir, digest_shortcut=True)
    probe = before.get("digest_probe", {}).get("before", {})
    format_version = probe.get("format_version")
    schema_version = probe.get("schema_version")
    if (
        probe.get("status") in {"format_version_unexpected", "schema_version_unexpected"}
        or format_version not in (None, code_graph_delta._DIGEST_FORMAT_VERSION)
        or schema_version not in (None, code_graph_delta._DIGEST_SCHEMA_VERSION)
    ):
        pytest.skip(
            f"native engine digest reports format {format_version} and schema {schema_version}, "
            f"expected format {code_graph_delta._DIGEST_FORMAT_VERSION} and schema "
            f"{code_graph_delta._DIGEST_SCHEMA_VERSION} (probe status {probe.get('status')})"
        )
    assert probe.get("status") == "ok"
    return run_dir, before


def test_equal_digest_with_native_engine(tmp_path, native_digest_baseline):
    run_dir, before = native_digest_baseline
    receipt = code_graph_delta.capture_after_and_diff(tmp_path, run_dir, before)

    assert receipt["delta_source"] == "digest_equal"
    assert receipt["digest"]["schema_version"] == code_graph_delta._DIGEST_SCHEMA_VERSION
    assert set(receipt["snapshots"]) == {"before"}


def test_digest_mismatch_with_native_engine_records_nonempty_delta(tmp_path, native_digest_baseline):
    run_dir, before = native_digest_baseline
    (tmp_path / "source.py").write_text("def first():\n    pass\n\ndef second():\n    pass\n", encoding="utf-8")

    receipt = code_graph_delta.capture_after_and_diff(tmp_path, run_dir, before)

    assert "delta_source" not in receipt
    assert receipt["digest_probe"]["after"]["status"] == "ok"
    assert receipt["raw_counts"]["added_nodes"] >= 1
    assert set(receipt["snapshots"]) == {"before", "after"}
