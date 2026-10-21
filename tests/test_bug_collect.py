"""Bug diagnostic budgets do not constrain public receipt exports."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from brigade import bug_collect
from brigade.localio import write_json


def _run(target, *, run_id="run-1"):
    directory = target / ".brigade" / "runs" / run_id
    write_json(directory / "run.json", {"started_at": "2026-01-01T00:00:00Z"})
    write_json(directory / "roster.json", {"agents": {"worker": {}}})
    write_json(directory / "worker-results.json", {"results": [{"worker": "worker", "ok": True}]})
    return directory


def _verify_receipt(target, run_id):
    path = target / ".brigade" / "work" / "verify-runs" / run_id / "receipt.json"
    write_json(
        path,
        {
            "run_id": run_id,
            "status": "completed",
            "started_at": "2026-01-01T00:00:00Z",
            "completed_at": "2026-01-01T00:00:01Z",
            "commands": [{"command": "pytest -q"}],
        },
    )
    return path


@pytest.mark.parametrize("name", ["run.json", "roster.json", "worker-results.json", "receipt.json"])
@pytest.mark.parametrize("kind", ["fifo", "symlink"])
@pytest.mark.parametrize("fallback", [False, True])
def test_diagnostics_skip_nonregular_files(tmp_path, monkeypatch, name, kind, fallback):
    from pathlib import Path

    if kind == "fifo" and not hasattr(os, "mkfifo"):
        pytest.skip("FIFO creation requires POSIX")
    if name == "receipt.json":
        path = _verify_receipt(tmp_path, "verify-1")
        payload = json.loads(path.read_text())
        payload["status"] = "failed"
    else:
        directory = _run(tmp_path)
        write_json(directory / "worker-results.json", {"results": [{"worker": "worker", "ok": False}]})
        path = directory / name
        payload = json.loads(path.read_text())
    path.unlink()
    if kind == "symlink":
        victim = tmp_path / "victim.json"
        write_json(victim, payload)
        path.symlink_to(victim)
    else:
        os.mkfifo(path)
        original = Path.open

        def deny_blocking_open(p, *args, **kwargs):
            if p == path:
                pytest.fail("diagnostics used a blocking FIFO open")
            return original(p, *args, **kwargs)

        monkeypatch.setattr(Path, "open", deny_blocking_open)
    if fallback:
        from brigade import run_journal

        monkeypatch.setattr(os, "supports_dir_fd", set())
        monkeypatch.setattr(run_journal, "_HAS_O_NOFOLLOW", False)
        monkeypatch.delattr(os, "O_NONBLOCK", raising=False)
    assert bug_collect.recent_failures(tmp_path) == {"runs": [], "verify": None}


def test_diagnostics_skip_file_swapped_to_symlink_during_fallback_open(tmp_path, monkeypatch):
    from brigade import run_journal

    path = tmp_path / "receipt.json"
    victim = tmp_path / "victim.json"
    path.write_text("{}")
    victim.write_text('{"private":"outside receipt"}')
    original = os.open

    def swap(p, *args, **kwargs):
        if p == path:
            path.unlink()
            path.symlink_to(victim)
        return original(p, *args, **kwargs)

    monkeypatch.setattr(os, "supports_dir_fd", set())
    monkeypatch.setattr(run_journal, "_HAS_O_NOFOLLOW", False)
    monkeypatch.setattr(os, "open", swap)
    assert bug_collect._object(path) is None


@pytest.mark.parametrize("kind", ["oversize", "invalid-utf8", "deep-json"])
@pytest.mark.parametrize("fallback", [False, True])
def test_bug_receipt_reader_bounds_bytes_before_decoding(tmp_path, monkeypatch, kind, fallback):
    if fallback:
        from brigade import run_journal

        monkeypatch.setattr(os, "supports_dir_fd", set())
        monkeypatch.setattr(run_journal, "_HAS_O_NOFOLLOW", False)
    payload = {
        "oversize": b'{"log":"' + b"x" * (256 * 1024) + b'"}',
        "invalid-utf8": b"\xff",
        "deep-json": b"[" * 2000 + b"]" * 2000,
    }[kind]
    path = tmp_path / "receipt.json"
    path.write_bytes(payload)
    original = os.read
    reads = []

    def bounded_read(fd, size):
        assert 0 < size <= 256 * 1024 + 1
        raw = original(fd, size)
        reads.append(len(raw))
        return raw

    monkeypatch.setattr(os, "read", bounded_read)
    assert bug_collect._object(path) is None
    if kind == "oversize":
        assert not reads, "oversized files must be rejected before reading"
    else:
        assert sum(reads) == len(payload)


@pytest.mark.parametrize("source", ["runs", "verify"])
def test_bug_diagnostics_caps_files_and_reads_newest_first(tmp_path, monkeypatch, source):
    import os

    directories = []
    for i in range(4):
        if source == "runs":
            directory = _run(tmp_path, run_id=f"run-{i}")
        else:
            directory = _verify_receipt(tmp_path, f"verify-{i}").parent
        os.utime(directory, (100 + i, 100 + i))
        directories.append(directory)
    monkeypatch.setattr(bug_collect, "_RECEIPT_DIR_LIMIT", 2, raising=False)
    original = bug_collect._object
    reads = []

    def read(path):
        reads.append(path.parent)
        return original(path)

    monkeypatch.setattr(bug_collect, "_object", read)
    value = bug_collect.recent_failures(tmp_path)
    assert value == {"runs": [], "verify": None}
    assert list(dict.fromkeys(reads)) == directories[:1:-1]
    assert len(reads) == (6 if source == "runs" else 2)


def test_bug_receipt_directory_enumeration_has_work_budget(tmp_path, monkeypatch):
    import os

    root = tmp_path / ".brigade" / "runs"
    root.mkdir(parents=True)
    for i in range(5):
        (root / f"run-{i}").mkdir()
    original = os.scandir
    visited = []

    class Entries:
        def __enter__(self):
            self.entries = original(root)
            return self

        def __exit__(self, *args):
            self.entries.close()

        def __iter__(self):
            for entry in self.entries:
                visited.append(entry.name)
                assert len(visited) <= 2, "directory scan exceeded the entry budget"
                yield entry

    monkeypatch.setattr(bug_collect, "_DIRECTORY_ENTRY_LIMIT", 2, raising=False)
    monkeypatch.setattr(os, "scandir", lambda path: Entries() if path == root else original(path))
    bug_collect.recent_failures(tmp_path)
    assert len(visited) == 2


@pytest.mark.parametrize("source", ["runs", "verify"])
def test_latest_failures_beyond_1024_entries_use_timestamp_names(tmp_path, monkeypatch, source):
    root = tmp_path / ".brigade" / ("runs" if source == "runs" else "work/verify-runs")
    root.mkdir(parents=True)
    directories = []
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(1030):
        stamp = (start + timedelta(seconds=i)).strftime("%Y%m%d-%H%M%S")
        # Run ids may carry a node namespace that must not affect ordering.
        name = f"{stamp}-00000000" if source == "verify" else f"{1029 - i:08x}.{stamp}-00000000"
        directory = root / name
        directory.mkdir()
        os.utime(directory, (2000 - i, 2000 - i))
        directories.append(directory)
    latest = directories[-1]
    if source == "runs":
        _run(tmp_path, run_id=latest.name)
        write_json(latest / "worker-results.json", {"results": [{"worker": "worker", "ok": False}]})
    else:
        receipt_path = _verify_receipt(tmp_path, latest.name)
        receipt = json.loads(receipt_path.read_text())
        receipt["status"] = "failed"
        write_json(receipt_path, receipt)
    os.utime(latest, (1, 1))

    # Pin hostile filesystem order so the latest entry is outside the old cap.
    original = os.scandir

    class Entries:
        def __enter__(self):
            with original(root) as entries:
                by_name = {entry.name: entry for entry in entries}
            return iter(by_name[d.name] for d in directories)

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(os, "scandir", lambda path: Entries() if path == root else original(path))
    failures = bug_collect.recent_failures(tmp_path)
    found = failures["runs"][0] if source == "runs" and failures["runs"] else failures["verify"]
    assert found is not None
    assert found["id"] == latest.name


@pytest.mark.parametrize("source", ["runs", "verify"])
def test_bug_diagnostics_caps_row_work(tmp_path, monkeypatch, source):
    monkeypatch.setattr(bug_collect, "_PROJECTION_ROW_LIMIT", 2)
    if source == "runs":
        path = _run(tmp_path) / "worker-results.json"
        key = "results"
        failure = {"worker": "worker", "ok": False}
    else:
        path = _verify_receipt(tmp_path, "verify-1")
        key = "commands"
        failure = {"command": "pytest -q", "status": "failed"}
    receipt = json.loads(path.read_text())
    receipt[key] *= 2
    receipt[key].append(failure)
    path.write_text(json.dumps(receipt))
    assert bug_collect.recent_failures(tmp_path) == {"runs": [], "verify": None}


@pytest.mark.parametrize("fallback", [False, True])
def test_latest_failed_verify_uses_latest_command_time(tmp_path, monkeypatch, fallback):
    first = _verify_receipt(tmp_path, "verify-first")
    receipt = json.loads(first.read_text())
    receipt["commands"] = [
        {"command": "pytest -q", "status": "failed", "started_at": f"2026-01-0{i}T00:00:00Z"} for i in (1, 4)
    ]
    write_json(first, receipt)
    second = _verify_receipt(tmp_path, "verify-second")
    other = json.loads(second.read_text())
    other.update(status="failed", started_at="2026-01-03T00:00:00Z")
    write_json(second, other)
    if fallback:
        from brigade import run_journal

        monkeypatch.setattr(os, "supports_dir_fd", set())
        monkeypatch.setattr(run_journal, "_HAS_O_NOFOLLOW", False)
    assert bug_collect.recent_failures(tmp_path)["verify"]["id"] == "verify-first"


def test_failed_runs_have_only_ids_and_failure_kinds(tmp_path):
    directory = _run(tmp_path)
    seat = "example-client-production"
    write_json(directory / "roster.json", {"agents": {seat: {"model": "private-model"}}})
    write_json(directory / "worker-results.json", {"results": [{"worker": seat, "ok": False}]})
    assert bug_collect.recent_failures(tmp_path)["runs"] == [{"id": "run-1", "status": "failed"}]


@pytest.mark.parametrize("large", [False, True])
def test_bug_doctor_never_reads_configured_external_logs(tmp_path, monkeypatch, large):
    from pathlib import Path

    target = tmp_path / "workspace"
    target.mkdir()
    log = tmp_path / "external.log"
    log.write_text("timeout for customer Example project Sample\n")
    if large:
        with log.open("ab") as handle:
            handle.truncate(1 << 30)
    write_json(
        target / ".brigade" / "handoff-sources.json",
        {
            "sources": [{"root": ".", "inboxes": [".claude/memory-handoffs"]}],
            "ingestor": {"last_run_log": str(log)},
        },
    )
    original = Path.open

    def deny_external_read(path, *args, **kwargs):
        if path == log:
            pytest.fail("bug collection read an external ingestor log")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny_external_read)
    payload = bug_collect.collect_diagnostics(target)
    assert payload["checks"]
    assert all(set(check) == {"name", "status"} for check in payload["checks"])
    assert "Example" not in json.dumps(payload)
    assert str(log) not in json.dumps(payload)


def test_doctor_bundle_projection_drops_all_detail_text(tmp_path):
    bundle = bug_collect.collect(
        tmp_path,
        summary="Unexpected result",
        title="Report",
        kind="bug",
        output="",
        diagnostics={
            "checks": [{"name": "profile", "status": "WARN", "detail": "customer Example project Sample"}],
            "summary": {"failed": 0, "warn": 1},
        },
    )
    assert bundle["bug_diagnostics"]["checks"] == [{"name": "profile", "status": "WARN"}]


@pytest.mark.parametrize("hazard", ["runs", "agents"])
def test_report_doctor_avoids_unbounded_general_checks(tmp_path, monkeypatch, hazard):
    from pathlib import Path
    from brigade import doctor

    (tmp_path / "AGENTS.md").write_text("# Definition of Done\nMemory handoff\n")
    if hazard == "runs":
        (tmp_path / ".brigade" / "runs").mkdir(parents=True)
        monkeypatch.setattr(doctor, "_immediate_run_dirs", lambda *a: pytest.fail("unbounded run enumeration"))
    else:
        with (tmp_path / "AGENTS.md").open("ab") as handle:
            handle.truncate(1 << 30)
        original = Path.read_text

        def bounded(path, *args, **kwargs):
            if path == tmp_path / "AGENTS.md":
                pytest.fail("unbounded AGENTS read")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", bounded)
    report = bug_collect.collect_diagnostics(tmp_path)
    if hazard == "agents":
        assert report["checks"] == [{"status": "WARN", "name": "agents-quality: AGENTS.md"}]
    else:
        assert report["checks"] == []


def test_report_doctor_kills_checks_at_deadline(tmp_path, monkeypatch):
    import subprocess
    import sys
    import time

    original = subprocess.run
    deadlines = []

    def stalled_check(argv, **kwargs):
        deadlines.append(kwargs.get("timeout"))
        # Keep the real subprocess timeout and cleanup, but simulate stalled I/O.
        kwargs["timeout"] = 0.05
        return original([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)

    monkeypatch.setattr(subprocess, "run", stalled_check)
    started = time.monotonic()
    report = bug_collect.collect_diagnostics(tmp_path)
    assert deadlines and all(0 < value <= 5 for value in deadlines)
    assert time.monotonic() - started < 3
    assert report["checks"] and all(check["status"] == "WARN" for check in report["checks"])
    assert all("budget" in check["name"] for check in report["checks"])


def test_report_doctor_file_probe_has_entry_budget(tmp_path, monkeypatch):
    from pathlib import Path
    from brigade import bug_doctor

    original = Path.lstat
    visited = []

    def probe(path, *args, **kwargs):
        visited.append(path)
        assert len(visited) <= 2, "diagnostic exceeded its entry budget"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", probe)
    checks = bug_doctor.check(tmp_path, "bootstrap", entry_limit=2)
    assert len(visited) == 2
    assert {"status": "WARN", "name": "bug diagnostics: entry budget"} in checks


def test_report_doctor_agents_read_is_byte_bounded(tmp_path, monkeypatch):
    from brigade import bug_doctor

    path = tmp_path / "AGENTS.md"
    path.write_bytes(b"x" * (64 * 1024 + 1))
    original = os.read

    def bounded(fd, size):
        assert size <= 64 * 1024 + 1
        assert os.fstat(fd).st_size <= 64 * 1024, "oversized diagnostics must be rejected before reading"
        return original(fd, size)

    monkeypatch.setattr(os, "read", bounded)
    assert bug_doctor.check(tmp_path, "agents-quality") == [{"status": "WARN", "name": "agents-quality: AGENTS.md"}]


@pytest.mark.parametrize("source", ["cwd", "pythonpath"])
def test_diagnostics_do_not_import_workspace_package(tmp_path, monkeypatch, source):
    import subprocess
    from pathlib import Path

    target = tmp_path / "workspace"
    package = target / "brigade"
    package.mkdir(parents=True)
    marker = tmp_path / "executed"
    (package / "__init__.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    if source == "cwd":
        monkeypatch.chdir(target)
    else:
        monkeypatch.setenv("PYTHONPATH", str(target))
    original = subprocess.run
    workdirs = []

    def run(argv, **kwargs):
        cwd = kwargs.get("cwd")
        if cwd is not None:
            directory = Path(cwd)
            assert directory != target and target not in directory.parents
            assert not list(directory.iterdir())
            workdirs.append(directory)
        return original(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    diagnostics = bug_collect.collect_diagnostics(target)
    assert not marker.exists()
    assert {"name": "bootstrap: AGENTS.md", "status": "FAIL"} in diagnostics["checks"]
    assert workdirs and all(not directory.exists() for directory in workdirs)
