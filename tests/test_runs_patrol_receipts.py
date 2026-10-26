"""Patrol receipts publish atomically and retain action fences."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from brigade import cli, localio, runs_patrol, runs_patrol_actions, runs_patrol_receipts
from tests.test_run_reap import NOW


@pytest.mark.parametrize("field", ["status", "scope", "idempotency_key"])
@pytest.mark.parametrize("value", [None, "", " ", 1, [], {}, "missing"])
def test_receipt_rejects_invalid_fence_fields(tmp_path, field, value):
    action = {"status": "pending", "scope": "local", "idempotency_key": "fence"}
    if value == "missing":
        action.pop(field)
    else:
        action[field] = value
    path = tmp_path / "receipt/patrol.json"
    localio.write_json(path, {"schema": "brigade.runs_patrol.v1", "actions": [action]})
    with pytest.raises(ValueError, match="invalid patrol action history"):
        runs_patrol_receipts.read_receipt(path.parent)


@pytest.mark.parametrize("status", sorted(runs_patrol_receipts.FENCE_STATUSES))
def test_receipt_accepts_all_fence_statuses_without_optional_legacy_fields(tmp_path, status):
    action = {"status": status, "scope": "local", "idempotency_key": "fence"}
    path = tmp_path / "receipt/patrol.json"
    localio.write_json(path, {"schema": "brigade.runs_patrol.v1", "actions": [action]})
    assert runs_patrol_receipts.read_receipt(path.parent)["actions"] == [action]


@pytest.mark.parametrize("location", ["receipt", "legacy", "shard"])
def test_apply_refuses_corrupt_history_before_releasing_claim(tmp_path, capsys, location):
    from tests.test_run_reap import _repo, _write_run
    from tests.test_runs_patrol import _ledger

    repo = _repo(tmp_path)
    run = _write_run(repo, "20260820-120000-patrol01", status="ok", extra={"claim_id": "claim-1"})
    ledger = _ledger(repo)
    before = ledger.read_bytes()
    root = repo / ".brigade/patrol"
    paths = {
        "receipt": root / "20260820T120000-old/patrol.json",
        "legacy": root / runs_patrol_receipts.FENCE_FILE,
        "shard": runs_patrol_receipts._fence_path(root / runs_patrol_receipts.FENCE_DIRECTORY, run.name),
    }
    path = paths[location]
    localio.write_json(
        path,
        {
            "schema": "brigade.runs_patrol.v1",
            "observed_at": (NOW - timedelta(days=8)).isoformat(),
            "actions": [{"status": "unrecognized", "scope": "ledger", "idempotency_key": "fence"}],
        },
    )
    original = path.read_bytes()
    assert runs_patrol.patrol(target=repo, apply="release-claim", now=NOW) == 2
    assert "invalid patrol action history" in capsys.readouterr().err
    assert ledger.read_bytes() == before
    assert path.read_bytes() == original


def test_report_retention_bounds_count_and_age_but_keeps_action_fences(tmp_path, capsys):
    root = tmp_path / ".brigade/patrol"

    def seed(stamp, suffix, actions):
        path = root / f"{stamp:%Y%m%dT%H%M%S}-{suffix}/patrol.json"
        localio.write_json(
            path,
            {
                "schema": "brigade.runs_patrol.v1",
                "patrol_id": path.parent.name,
                "observed_at": stamp.isoformat(),
                "actions": actions,
            },
        )
        return path

    old = seed(NOW - timedelta(days=8), "old", [])
    action = {"status": "pending", "idempotency_key": "fence", "scope": "local", "action": "reap"}
    fence = seed(NOW - timedelta(days=8), "fence", [action])
    recent = [seed(NOW - timedelta(minutes=i + 1), "recent", []) for i in range(146)]
    assert runs_patrol.patrol(target=tmp_path, receipt=True, now=NOW, json_output=True) == 0
    report = json.loads(capsys.readouterr().out)
    assert not old.exists()
    assert not fence.exists()
    assert action in runs_patrol_actions._history(root)
    assert recent[0].exists() and not recent[-1].exists()
    assert len(list(root.glob("*/patrol.json"))) == 144
    assert (tmp_path / report["receipt"]).exists()
    assert runs_patrol.patrol(target=tmp_path, receipt=True, now=NOW + timedelta(days=8)) == 0
    capsys.readouterr()
    assert action in runs_patrol_actions._history(root)
    assert len(list(root.glob("*/patrol.json"))) == 1


def test_apply_retention_prunes_receipts_with_only_refused_or_skipped_actions(tmp_path, capsys):
    root = tmp_path / ".brigade/patrol"
    old = root / "20260801T120000-refused/patrol.json"
    localio.write_json(
        old,
        {
            "schema": "brigade.runs_patrol.v1",
            "observed_at": (NOW - timedelta(days=8)).isoformat(),
            "actions": [{"status": "refused"}, {"status": "skipped"}],
        },
    )
    assert runs_patrol.patrol(target=tmp_path, apply="reap", now=NOW) == 0
    capsys.readouterr()
    assert not old.exists()
    assert runs_patrol_actions._history(root) == []


def test_history_reads_only_newest_receipts(tmp_path, monkeypatch):
    root = tmp_path / ".brigade/patrol"
    for index in range(6):
        localio.write_json(
            root / f"20260828T12000{index}-report/patrol.json",
            {"schema": "brigade.runs_patrol.v1", "actions": []},
        )
    monkeypatch.setattr(runs_patrol_receipts, "REPORT_RECEIPT_LIMIT", 2)
    read = runs_patrol_receipts.read_receipt
    reads = []

    def checked(directory):
        reads.append(directory.name)
        return read(directory)

    monkeypatch.setattr(runs_patrol_receipts, "read_receipt", checked)
    assert runs_patrol_actions._history(root) == []
    assert reads == ["20260828T120005-report", "20260828T120004-report"]


def test_archive_failure_preserves_the_original_action_receipt(tmp_path, monkeypatch, capsys):
    root = tmp_path / ".brigade/patrol"
    old = root / "20260801T120000-fence/patrol.json"
    action = {"status": "pending", "idempotency_key": "fence", "scope": "local", "action": "reap"}
    localio.write_json(
        old,
        {"schema": "brigade.runs_patrol.v1", "observed_at": (NOW - timedelta(days=8)).isoformat(), "actions": [action]},
    )
    write = localio.write_text_atomic

    def unavailable(path, content):
        if path.name == runs_patrol_receipts.FENCE_FILE:
            raise OSError("archive write refused")
        return write(path, content)

    monkeypatch.setattr(localio, "write_text_atomic", unavailable)
    assert runs_patrol.patrol(target=tmp_path, apply="reap", now=NOW) == 2
    assert "archive write refused" in capsys.readouterr().err
    assert json.loads(old.read_text())["actions"] == [action]
    monkeypatch.setattr(localio, "write_text_atomic", write)
    assert runs_patrol.patrol(target=tmp_path, apply="reap", now=NOW) == 0
    capsys.readouterr()
    assert not old.exists() and action in runs_patrol_actions._history(root)


def test_receipt_is_complete_before_publication(tmp_path, monkeypatch, capsys):
    replace = localio.os.replace
    published = []

    def checked(source, destination):
        if destination.name == "patrol.json":
            assert not destination.exists()
            published.append(json.loads(source.read_text()))
        return replace(source, destination)

    monkeypatch.setattr(localio.os, "replace", checked)
    assert runs_patrol.patrol(target=tmp_path, receipt=True, now=NOW, json_output=True) == 0
    report = json.loads(capsys.readouterr().out)
    assert published == [report]


def test_report_receipt_survives_busy_apply_lock(tmp_path, monkeypatch, capsys):
    from contextlib import contextmanager
    from brigade import inbox_lock

    @contextmanager
    def busy(*args, **kwargs):
        raise inbox_lock.InboxLockTimeout("another patrol is sending")
        yield

    monkeypatch.setattr(inbox_lock, "held_file_lock", busy)
    assert runs_patrol.patrol(target=tmp_path, receipt=True, now=NOW, json_output=True) == 0
    report = json.loads(capsys.readouterr().out)
    assert json.loads((tmp_path / report["receipt"]).read_text()) == report
    assert runs_patrol.patrol(target=tmp_path, apply="reap", now=NOW) == 2
    assert "another patrol is sending" in capsys.readouterr().err


def test_fences_for_many_lanes_do_not_hit_one_global_archive_cap(tmp_path, monkeypatch, capsys):
    root = tmp_path / ".brigade/patrol"
    actions = [
        {
            "lane_id": f"old-run-{i}",
            "action": "escalate",
            "scope": "local",
            "idempotency_key": f"{i:064x}",
            "attempted_at": (NOW - timedelta(days=8)).isoformat(),
            "status": "applied",
        }
        for i in range(12)
    ]
    monkeypatch.setattr(runs_patrol_receipts, "RECEIPT_BYTE_LIMIT", 2000)
    localio.write_json(
        root / runs_patrol_receipts.FENCE_FILE, {"schema": "brigade.runs_patrol.v1", "actions": actions[:3]}
    )
    for i in range(1, 4):
        localio.write_json(
            root / f"20260801T12000{i}-old/patrol.json",
            {
                "schema": "brigade.runs_patrol.v1",
                "observed_at": (NOW - timedelta(days=8)).isoformat(),
                "actions": actions[i * 3 : i * 3 + 3],
            },
        )
    assert runs_patrol.patrol(target=tmp_path, apply="reap", now=NOW) == 0
    capsys.readouterr()
    assert {item["idempotency_key"] for item in runs_patrol_actions._history(root)} == {
        item["idempotency_key"] for item in actions
    }
    assert runs_patrol.patrol(target=tmp_path, apply="reap", now=NOW) == 0
    capsys.readouterr()
    assert json.loads((root / runs_patrol_receipts.FENCE_FILE).read_text())["actions"] == []
    read = runs_patrol_receipts._read_payload
    reads = []

    def tracked(path):
        reads.append(path)
        return read(path)

    monkeypatch.setattr(runs_patrol_receipts, "_read_payload", tracked)
    assert runs_patrol_receipts.read_fences(root, lane_ids={"old-run-0"}) == [actions[0]]
    assert len(reads) == 2  # Legacy archive plus this scan's single lane shard.


def test_oversized_receipt_is_bounded_and_stays_readable(tmp_path, monkeypatch):
    monkeypatch.setattr(runs_patrol_receipts, "RECEIPT_BYTE_LIMIT", 20000)
    lanes = [
        {
            "lane_id": f"lane-{i}",
            "run_id": f"run-{i}",
            "state": "complete",
            "reason": "r",
            "classification_digest": "d" * 64,
            "evidence": [{"source": "s", "detail": "x" * 100} for _ in range(20)],
        }
        for i in range(60)
    ]
    action = {"status": "applied", "idempotency_key": "fence", "scope": "local", "action": "reap"}
    payload = {"schema": "brigade.runs_patrol.v1", "lanes": lanes, "actions": [action]}
    path = tmp_path / "20260820T120000-big/patrol.json"
    runs_patrol_receipts.write_receipt(path, payload)
    assert path.stat().st_size <= 20000
    read = runs_patrol_receipts.read_receipt(path.parent)
    assert read["actions"] == [action]
    assert read["receipt_truncated"] is True
    small = {"schema": "brigade.runs_patrol.v1", "lanes": lanes[:1], "actions": []}
    runs_patrol_receipts.write_receipt(path, small)
    assert "receipt_truncated" not in runs_patrol_receipts.read_receipt(path.parent)


def test_action_history_over_the_limit_is_refused_not_published(tmp_path, monkeypatch):
    import pytest

    monkeypatch.setattr(runs_patrol_receipts, "RECEIPT_BYTE_LIMIT", 2000)
    actions = [{"lane_id": "x" * 100, "status": "applied"} for _ in range(50)]
    payload = {"schema": "brigade.runs_patrol.v1", "lanes": [], "actions": actions}
    path = tmp_path / "20260820T120000-big/patrol.json"
    with pytest.raises(ValueError, match="action history"):
        runs_patrol_receipts.write_receipt(path, payload)
    assert not path.exists()


def test_oversized_action_history_returns_patrol_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(runs_patrol_receipts, "RECEIPT_BYTE_LIMIT", 2000)
    for i in range(8):
        path = tmp_path / ".brigade/runs" / f"20260820-120000-patrol{i:02d}" / "run.json"
        path.parent.mkdir(parents=True)
        path.write_text("{")
    write = runs_patrol_receipts.write_receipt
    published = []

    def tracked(path, payload):
        write(path, payload)
        published.append(path.read_bytes())

    monkeypatch.setattr(runs_patrol_receipts, "write_receipt", tracked)
    assert cli.main(["runs", "patrol", "--target", str(tmp_path), "--apply", "escalate", "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: patrol: patrol action history exceeds receipt byte limit\n"
    receipts = list((tmp_path / ".brigade/patrol").glob("*/patrol.json"))
    assert len(receipts) == 1
    assert receipts[0].read_bytes() == published[-1]
    assert receipts[0].stat().st_size <= 2000
    receipt = runs_patrol_receipts.read_receipt(receipts[0].parent)
    assert receipt["actions"]
    assert all(action["status"] == "recorded" for action in receipt["actions"])


def test_care_status_reads_a_receipt_between_one_and_four_mib(tmp_path):
    from brigade import care_patrol

    path = tmp_path / ".brigade/patrol/20260820T120000-big/patrol.json"
    lanes = [{"lane_id": f"l{i}", "evidence": [{"detail": "x" * 200}] * 20} for i in range(300)]
    localio.write_json(
        path,
        {
            "schema": "brigade.runs_patrol.v1",
            "patrol_id": "20260820T120000-big",
            "observed_at": NOW.isoformat(),
            "exit_code": 0,
            "lanes": lanes,
            "actions": [],
        },
    )
    assert 1024 * 1024 < path.stat().st_size < runs_patrol_receipts.RECEIPT_BYTE_LIMIT
    entry = type("Entry", (), {"entry_id": "run-patrol"})()
    fields = care_patrol.receipt_fields(tmp_path, entry, None)
    assert fields["patrol_receipt"]["patrol_id"] == "20260820T120000-big"


def test_unpublished_directories_do_not_crowd_out_older_fences(tmp_path):
    root = tmp_path / ".brigade/patrol"
    action = {"status": "pending", "idempotency_key": "fence", "scope": "local", "action": "escalate"}
    localio.write_json(
        root / "20260101T000000-old/patrol.json",
        {"schema": "brigade.runs_patrol.v1", "observed_at": NOW.isoformat(), "actions": [action]},
    )
    for index in range(runs_patrol_receipts.REPORT_RECEIPT_LIMIT + 5):
        (root / f"20260801T{index:06d}-unpublished").mkdir(parents=True)
    assert action in runs_patrol_actions._history(root)
