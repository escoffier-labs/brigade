"""Idempotent canonical-owner Grok Bot report reconciliation."""

from __future__ import annotations

import hashlib
import json
import shutil
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

from brigade import cli, grokbot_jobs, grokbot_reconcile


NOW = datetime(2026, 8, 27, 15, 0, tzinfo=timezone.utc)
REPORT_TEXT = "PRIVATE_SCOUT_FINDING_TOKEN_alpha\n## Injected Heading\nScout findings for example/brigade.\n"


def _spec() -> dict[str, object]:
    return {
        "label": "Repository Scout",
        "role": "repository-scout",
        "repository": "example/brigade",
        "base_ref": "main",
        "ownership_paths": ["src/brigade", "tests"],
        "instructions": "PRIVATE_SCOUT_INSTRUCTIONS",
        "verification_commands": ["SECRET_VERIFY_CMD"],
        "artifact": {"kind": "report"},
        "timeout_seconds": 900,
    }


def _report_digest(text: str = REPORT_TEXT) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _complete_report(queue: Path, *, key: str = "scout-report-1", text: str = REPORT_TEXT) -> str:
    job_id = grokbot_jobs.enqueue(queue, _spec(), key, now=NOW)["job_id"]
    grokbot_jobs.claim(queue, job_id, "bot-a", "lease-a", 60, now=NOW)
    grokbot_jobs.transition(queue, job_id, "bot-a", "lease-a", "running", now=NOW)
    grokbot_jobs.complete_report(
        queue,
        job_id,
        "bot-a",
        "lease-a",
        {"kind": "report", "path": "artifacts/report.md", "sha256": _report_digest(text)},
        text,
        now=NOW,
    )
    return job_id


def _queue_root(queue: Path) -> Path:
    return queue / ".brigade" / "cloud" / "grokbot"


def _review_inbox(owner: Path) -> Path:
    return owner / "memory" / "handoff-inbox"


def _writer_inbox(owner: Path) -> Path:
    return owner / ".grok" / "memory-handoffs"


def test_preview_counts_completed_scout_reports_and_writes_nothing(tmp_path: Path):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    job_id = _complete_report(queue)

    result = grokbot_reconcile.preview(queue, owner)

    assert result == {
        "eligible": 1,
        "known": 0,
        "unavailable": 0,
        "created": 0,
        "limit": 1,
        "jobs": [{"job_id": job_id, "state": "completed"}],
    }
    dumped = json.dumps(result, sort_keys=True)
    assert REPORT_TEXT not in dumped
    assert "PRIVATE_SCOUT_FINDING_TOKEN_alpha" not in dumped
    assert "Injected Heading" not in dumped
    assert not _review_inbox(owner).exists()
    reconcile_dir = _queue_root(queue) / "reconcile"
    assert not reconcile_dir.exists() or list(reconcile_dir.glob("*.json")) == []
    assert REPORT_TEXT not in _queue_root(queue).joinpath("jobs", f"{job_id}.json").read_text()


def test_apply_writes_one_quoted_handoff_and_a_private_marker(tmp_path: Path):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    job_id = _complete_report(queue)
    job = grokbot_jobs.get_job(queue, job_id)

    result = grokbot_reconcile.apply(queue, owner)

    assert result == {
        "eligible": 1,
        "known": 0,
        "unavailable": 0,
        "created": 1,
        "skipped": 0,
        "limit": 1,
        "jobs": [{"job_id": job_id, "state": "completed"}],
    }
    dumped = json.dumps(result, sort_keys=True)
    assert REPORT_TEXT not in dumped
    assert "PRIVATE_SCOUT_FINDING_TOKEN_alpha" not in dumped

    inbox = _review_inbox(owner)
    drafts = list(inbox.glob("*.md"))
    assert [path.name for path in drafts] == [f"{job_id}-scout-report.md"]
    text = drafts[0].read_text(encoding="utf-8")
    assert "## Recommended memory action\n\nno-card" in text
    assert "untrusted" in text.casefold()
    assert "canonical-owner" in text.casefold()
    assert job_id in text
    assert "example/brigade" in text
    assert job["task_hash"] in text
    assert _report_digest() in text
    assert job["updated_at"] in text
    assert "> PRIVATE_SCOUT_FINDING_TOKEN_alpha" in text
    assert "> ## Injected Heading" in text
    assert "\n## Injected Heading\n" not in text
    assert "do not auto-edit canonical memory" in text.casefold() or "rather than auto-edit" in text.casefold()

    marker = _queue_root(queue) / "reconcile" / f"{job_id}.json"
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["schema"] == "brigade.grokbot.reconcile.v1"
    assert payload["job_id"] == job_id
    assert payload["sha256"] == _report_digest()
    assert REPORT_TEXT not in json.dumps(payload)
    assert "PRIVATE_SCOUT_FINDING_TOKEN_alpha" not in json.dumps(payload)
    assert "text" not in payload
    assert "report" not in payload
    assert "report_text" not in payload


def test_default_apply_delivers_directly_to_owner_review_inbox(tmp_path: Path):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    job_id = _complete_report(queue)

    result = grokbot_reconcile.apply(queue, owner)

    assert result["created"] == 1
    assert (_review_inbox(owner) / f"{job_id}-scout-report.md").is_file()
    assert not _writer_inbox(owner).exists()


@pytest.mark.parametrize("inbox", ["/tmp/outside-owner", "../../outside-owner"])
def test_custom_inbox_cannot_escape_owner(tmp_path: Path, inbox: str):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    _complete_report(queue)

    with pytest.raises(grokbot_reconcile.ReconcileError, match="^invalid-inbox$"):
        grokbot_reconcile.apply(queue, owner, inbox=inbox)

    assert not _review_inbox(owner).exists()


def test_existing_handoff_symlink_fails_closed(tmp_path: Path):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    job_id = _complete_report(queue)
    review = _review_inbox(owner)
    review.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("do not replace\n", encoding="utf-8")
    (review / f"{job_id}-scout-report.md").symlink_to(outside)

    with pytest.raises(grokbot_reconcile.ReconcileError, match="^unsafe-storage$"):
        grokbot_reconcile.apply(queue, owner)

    assert outside.read_text(encoding="utf-8") == "do not replace\n"
    assert not (_queue_root(queue) / "reconcile" / f"{job_id}.json").exists()


@pytest.mark.skipif(not hasattr(__import__("os"), "O_NOFOLLOW"), reason="requires descriptor-safe POSIX traversal")
def test_parent_symlink_swap_after_resolution_fails_closed(tmp_path: Path, monkeypatch):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    (owner / "memory").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    job_id = _complete_report(queue)
    original = grokbot_reconcile._resolve_inbox

    def swap_after_resolution(owner_path: Path, inbox: object) -> Path:
        result = original(owner_path, inbox)
        (owner / "memory").rename(owner / "memory-real")
        (owner / "memory").symlink_to(outside, target_is_directory=True)
        return result

    monkeypatch.setattr(grokbot_reconcile, "_resolve_inbox", swap_after_resolution)

    with pytest.raises(grokbot_reconcile.ReconcileError, match="^unsafe-storage$"):
        grokbot_reconcile.apply(queue, owner)

    assert not list(outside.rglob("*.md"))
    assert not (_queue_root(queue) / "reconcile" / f"{job_id}.json").exists()


def test_non_posix_apply_fails_closed_before_path_write(tmp_path: Path, monkeypatch):
    owner = tmp_path / "owner"
    owner.mkdir()
    monkeypatch.setattr(grokbot_reconcile, "SECURE_OWNER_WRITE_AVAILABLE", False)

    with pytest.raises(grokbot_reconcile.ReconcileError, match="^secure-owner-write-unavailable$"):
        grokbot_reconcile._write_handoff(
            owner,
            Path("memory/handoff-inbox"),
            "grokbot-canary-scout-report.md",
            "not published",
        )

    assert list(owner.iterdir()) == []


def test_repeated_apply_does_not_duplicate_drafts(tmp_path: Path):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    job_id = _complete_report(queue)
    first = grokbot_reconcile.apply(queue, owner)
    second = grokbot_reconcile.apply(queue, owner)
    preview = grokbot_reconcile.preview(queue, owner)

    assert first["created"] == 1
    assert second == {
        "eligible": 0,
        "known": 1,
        "unavailable": 0,
        "created": 0,
        "skipped": 1,
        "limit": 1,
        "jobs": [],
    }
    assert preview["eligible"] == 0
    assert preview["known"] == 1
    assert preview["unavailable"] == 0
    assert preview["created"] == 0
    drafts = list(_review_inbox(owner).glob("*.md"))
    assert [path.name for path in drafts] == [f"{job_id}-scout-report.md"]


def test_apply_respects_limit_and_continues_on_the_next_run(tmp_path: Path):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    first = _complete_report(queue, key="scout-a")
    second = _complete_report(queue, key="scout-b")

    limited = grokbot_reconcile.apply(queue, owner, limit=1)
    rest = grokbot_reconcile.apply(queue, owner, limit=1)

    assert limited["created"] == 1
    assert limited["eligible"] == 2
    assert rest["created"] == 1
    created = {limited["jobs"][0]["job_id"], rest["jobs"][0]["job_id"]}
    assert created == {first, second}
    assert {path.name for path in _review_inbox(owner).glob("*.md")} == {
        f"{first}-scout-report.md",
        f"{second}-scout-report.md",
    }


def test_preview_and_apply_skip_non_report_jobs(tmp_path: Path):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    worker = _spec()
    worker.update(
        {
            "label": "Worker",
            "role": "implementation-worker",
            "artifact": {"kind": "draft-pr"},
        }
    )
    worker_id = grokbot_jobs.enqueue(queue, worker, "worker-1", now=NOW)["job_id"]
    grokbot_jobs.claim(queue, worker_id, "bot-a", "lease-a", 60, now=NOW)
    grokbot_jobs.transition(queue, worker_id, "bot-a", "lease-a", "running", now=NOW)
    grokbot_jobs.transition(
        queue,
        worker_id,
        "bot-a",
        "lease-a",
        "completed",
        artifact={
            "kind": "draft-pr",
            "url": "https://github.com/example/brigade/pull/1",
            "branch": "grokbot/worker",
        },
        now=NOW,
    )
    grokbot_jobs.enqueue(queue, _spec(), "queued-scout", now=NOW)

    result = grokbot_reconcile.preview(queue, owner)
    applied = grokbot_reconcile.apply(queue, owner)

    assert result["eligible"] == 0
    assert applied["created"] == 0
    assert not _review_inbox(owner).exists()


def test_missing_legacy_snapshot_does_not_block_a_retrievable_report(tmp_path: Path):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    legacy = _complete_report(queue, key="legacy-report")
    (_queue_root(queue) / "artifacts" / f"{legacy}.md").unlink()
    retrievable = _complete_report(queue, key="retrievable-report")

    preview = grokbot_reconcile.preview(queue, owner)
    applied = grokbot_reconcile.apply(queue, owner)

    assert preview["unavailable"] == 1
    assert preview["unavailable_reasons"] == {"report-missing": 1}
    assert preview["eligible"] == 1
    assert applied["unavailable"] == 1
    assert applied["unavailable_reasons"] == {"report-missing": 1}
    assert applied["created"] == 1
    assert applied["jobs"] == [{"job_id": retrievable, "state": "completed"}]
    assert not (_queue_root(queue) / "reconcile" / f"{legacy}.json").exists()


def test_malformed_marker_beside_missing_snapshot_still_fails_closed(tmp_path: Path):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    job_id = _complete_report(queue, key="legacy-with-bad-marker")
    (_queue_root(queue) / "artifacts" / f"{job_id}.md").unlink()
    marker_dir = _queue_root(queue) / "reconcile"
    marker_dir.mkdir(parents=True)
    (marker_dir / f"{job_id}.json").write_text("{not-json", encoding="utf-8")

    with pytest.raises(grokbot_reconcile.ReconcileError, match="^corrupt-storage$"):
        grokbot_reconcile.apply(queue, owner)

    assert not _review_inbox(owner).exists()


@pytest.mark.skipif(not hasattr(__import__("os"), "O_NOFOLLOW"), reason="requires descriptor-safe POSIX publication")
def test_failed_atomic_publication_leaves_no_partial_final_or_marker(tmp_path: Path, monkeypatch):
    import os

    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    job_id = _complete_report(queue)
    original_link = os.link

    def fail_before_publish(src, dst, **kwargs):
        destination_dir = kwargs["dst_dir_fd"]
        with pytest.raises(FileNotFoundError):
            os.stat(dst, dir_fd=destination_dir, follow_symlinks=False)
        raise OSError("simulated publication failure")

    monkeypatch.setattr(grokbot_reconcile.os, "link", fail_before_publish)

    with pytest.raises(grokbot_reconcile.ReconcileError, match="^unsafe-storage$"):
        grokbot_reconcile.apply(queue, owner)

    monkeypatch.setattr(grokbot_reconcile.os, "link", original_link)
    review = _review_inbox(owner)
    assert not (review / f"{job_id}-scout-report.md").exists()
    assert not (review / f".{job_id}-scout-report.md.tmp").exists()
    assert not (_queue_root(queue) / "reconcile" / f"{job_id}.json").exists()


def test_corrupt_snapshot_fails_closed_without_writing(tmp_path: Path):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    job_id = _complete_report(queue)
    snapshot = _queue_root(queue) / "artifacts" / f"{job_id}.md"
    snapshot.write_bytes(b"\xff")

    with pytest.raises(grokbot_reconcile.ReconcileError, match="^(invalid-report|digest-mismatch)$") as exc:
        grokbot_reconcile.apply(queue, owner)

    assert REPORT_TEXT not in str(exc.value)
    assert not _review_inbox(owner).exists()
    assert not (_queue_root(queue) / "reconcile" / f"{job_id}.json").exists()


def test_conflicting_marker_fails_closed(tmp_path: Path):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    job_id = _complete_report(queue)
    marker_dir = _queue_root(queue) / "reconcile"
    marker_dir.mkdir(parents=True)
    marker = marker_dir / f"{job_id}.json"
    marker.write_text(
        json.dumps(
            {
                "schema": "brigade.grokbot.reconcile.v1",
                "job_id": job_id,
                "sha256": "0" * 64,
                "task_hash": grokbot_jobs.get_job(queue, job_id)["task_hash"],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(grokbot_reconcile.ReconcileError, match="^marker-conflict$"):
        grokbot_reconcile.apply(queue, owner)

    assert not _review_inbox(owner).exists()


def test_marker_with_wrong_task_hash_fails_closed(tmp_path: Path):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    job_id = _complete_report(queue)
    marker_dir = _queue_root(queue) / "reconcile"
    marker_dir.mkdir(parents=True)
    (marker_dir / f"{job_id}.json").write_text(
        json.dumps(
            {
                "schema": "brigade.grokbot.reconcile.v1",
                "job_id": job_id,
                "sha256": _report_digest(),
                "task_hash": "sha256:" + "0" * 64,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(grokbot_reconcile.ReconcileError, match="^marker-conflict$"):
        grokbot_reconcile.apply(queue, owner)

    assert not _review_inbox(owner).exists()


def test_corrupt_marker_fails_closed(tmp_path: Path):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    job_id = _complete_report(queue)
    marker_dir = _queue_root(queue) / "reconcile"
    marker_dir.mkdir(parents=True)
    (marker_dir / f"{job_id}.json").write_text("{not-json", encoding="utf-8")

    with pytest.raises(grokbot_reconcile.ReconcileError, match="^corrupt-storage$"):
        grokbot_reconcile.apply(queue, owner)

    assert not _review_inbox(owner).exists()


@pytest.mark.parametrize("extra", [{"notes": "PRIVATE_SCOUT_FINDING_TOKEN_alpha"}, {"payload": REPORT_TEXT}])
def test_marker_with_unexpected_field_fails_closed(tmp_path: Path, extra: dict[str, str]):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    job_id = _complete_report(queue)
    marker_dir = _queue_root(queue) / "reconcile"
    marker_dir.mkdir(parents=True)
    payload = {
        "schema": "brigade.grokbot.reconcile.v1",
        "job_id": job_id,
        "sha256": _report_digest(),
        "task_hash": grokbot_jobs.get_job(queue, job_id)["task_hash"],
        **extra,
    }
    (marker_dir / f"{job_id}.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(grokbot_reconcile.ReconcileError, match="^corrupt-storage$") as exc:
        grokbot_reconcile.apply(queue, owner)

    assert REPORT_TEXT not in str(exc.value)
    assert "PRIVATE_SCOUT_FINDING_TOKEN_alpha" not in str(exc.value)
    assert not _review_inbox(owner).exists()


@pytest.mark.skipif(not hasattr(__import__("os"), "O_NOFOLLOW"), reason="requires descriptor-safe POSIX traversal")
def test_marker_read_stays_anchored_when_queue_root_is_swapped(tmp_path: Path, monkeypatch):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    job_id = _complete_report(queue)
    original_status = grokbot_jobs._status_from_storage
    swapped = False
    substituted_text = "SUBSTITUTED_QUEUE_REPORT_TOKEN\n"

    def swap_root_after_status(storage):
        nonlocal swapped
        result = original_status(storage)
        if not swapped:
            root = _queue_root(queue)
            original_root = root.with_name("grokbot-original")
            root.rename(original_root)
            shutil.copytree(original_root, root)
            (root / "artifacts" / f"{job_id}.md").write_text(substituted_text, encoding="utf-8")
            job_path = root / "jobs" / f"{job_id}.json"
            record = json.loads(job_path.read_text(encoding="utf-8"))
            record["result_artifact"]["sha256"] = _report_digest(substituted_text)
            job_path.write_text(json.dumps(record), encoding="utf-8")
            marker_dir = root / "reconcile"
            marker_dir.mkdir()
            (marker_dir / f"{job_id}.json").write_text(
                json.dumps(
                    {
                        "schema": "brigade.grokbot.reconcile.v1",
                        "job_id": job_id,
                        "sha256": _report_digest(substituted_text),
                        "task_hash": record["task_hash"],
                    }
                ),
                encoding="utf-8",
            )
            swapped = True
        return result

    monkeypatch.setattr(grokbot_jobs, "_status_from_storage", swap_root_after_status)

    result = grokbot_reconcile.apply(queue, owner)

    assert result["created"] == 1
    handoff = (_review_inbox(owner) / f"{job_id}-scout-report.md").read_text(encoding="utf-8")
    assert "PRIVATE_SCOUT_FINDING_TOKEN_alpha" in handoff
    assert "SUBSTITUTED_QUEUE_REPORT_TOKEN" not in handoff
    original_marker = _queue_root(queue).with_name("grokbot-original") / "reconcile" / f"{job_id}.json"
    assert original_marker.is_file()
    substituted_marker = _queue_root(queue) / "reconcile" / f"{job_id}.json"
    assert substituted_marker.is_file()


def test_preview_on_empty_target_does_not_create_queue_state(tmp_path: Path):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()

    result = grokbot_reconcile.preview(queue, owner)

    assert result["eligible"] == 0
    assert result["created"] == 0
    assert not (queue / ".brigade").exists()
    assert not _review_inbox(owner).exists()


def test_preview_does_not_repair_or_create_an_incomplete_queue(tmp_path: Path):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    root = _queue_root(queue)
    root.mkdir(parents=True, mode=0o755)
    mode_before = root.stat().st_mode

    with pytest.raises(grokbot_reconcile.ReconcileError, match="^unsafe-storage$"):
        grokbot_reconcile.preview(queue, owner)

    assert list(root.iterdir()) == []
    assert root.stat().st_mode == mode_before


@pytest.mark.skipif(not hasattr(__import__("os"), "O_NOFOLLOW"), reason="requires POSIX mode semantics")
def test_preview_does_not_chmod_existing_queue_directories(tmp_path: Path):
    import os

    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    _complete_report(queue)
    directories = [
        _queue_root(queue),
        _queue_root(queue) / "jobs",
        _queue_root(queue) / "idempotency",
        _queue_root(queue) / "artifacts",
    ]
    for path in directories:
        os.chmod(path, 0o750)

    result = grokbot_reconcile.preview(queue, owner)

    assert result["eligible"] == 1
    assert [path.stat().st_mode & 0o777 for path in directories] == [0o750] * len(directories)


def _run_reconcile(queue: Path, owner: Path, *command: str) -> int:
    return cli.main(
        ["run", "cloud", "grokbot", "reconcile-reports", "--target", str(queue), "--owner", str(owner), *command]
    )


def test_cli_preview_defaults_to_write_nothing_and_omits_report_text(tmp_path: Path, capsys):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    job_id = _complete_report(queue)

    assert _run_reconcile(queue, owner) == 0
    captured = capsys.readouterr()
    assert "eligible=1" in captured.out
    assert "known=0" in captured.out
    assert job_id in captured.out
    assert REPORT_TEXT not in captured.out
    assert "PRIVATE_SCOUT_FINDING_TOKEN_alpha" not in captured.out
    assert "Injected Heading" not in captured.out
    assert captured.err == ""
    assert not _review_inbox(owner).exists()


def test_cli_apply_json_stays_free_of_report_text(tmp_path: Path, capsys):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    job_id = _complete_report(queue)

    assert _run_reconcile(queue, owner, "--apply", "--json") == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["created"] == 1
    assert payload["jobs"][0]["job_id"] == job_id
    assert REPORT_TEXT not in captured.out
    assert "PRIVATE_SCOUT_FINDING_TOKEN_alpha" not in captured.out
    assert (_review_inbox(owner) / f"{job_id}-scout-report.md").is_file()


def test_cli_rejects_invalid_limit_without_writing(tmp_path: Path, capsys):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    _complete_report(queue)

    assert _run_reconcile(queue, owner, "--apply", "--limit", "0") == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "error: invalid-limit"
    assert not _review_inbox(owner).exists()


def _apply_in_process(queue: str, owner: str, connection) -> None:
    try:
        result = grokbot_reconcile.apply(Path(queue), Path(owner))
    except grokbot_reconcile.ReconcileError as exc:
        connection.send({"error": exc.reason})
    else:
        connection.send(result)
    finally:
        connection.close()


def test_concurrent_apply_creates_only_one_draft(tmp_path: Path):
    from multiprocessing import Pipe, Process

    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    job_id = _complete_report(queue)
    first_parent, first_child = Pipe(duplex=False)
    second_parent, second_child = Pipe(duplex=False)
    first = Process(target=_apply_in_process, args=(str(queue), str(owner), first_child))
    second = Process(target=_apply_in_process, args=(str(queue), str(owner), second_child))
    first.start()
    second.start()
    first.join()
    second.join()
    results = [first_parent.recv(), second_parent.recv()]
    created = sum(item.get("created", 0) for item in results if "created" in item)
    assert created == 1
    drafts = list(_review_inbox(owner).glob("*.md"))
    assert [path.name for path in drafts] == [f"{job_id}-scout-report.md"]


def test_restart_after_draft_without_marker_does_not_duplicate(tmp_path: Path):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    job_id = _complete_report(queue)
    grokbot_reconcile.apply(queue, owner)
    marker = _queue_root(queue) / "reconcile" / f"{job_id}.json"
    marker.unlink()

    result = grokbot_reconcile.apply(queue, owner)

    assert result["created"] == 0
    assert result["skipped"] == 1
    drafts = list(_review_inbox(owner).glob("*.md"))
    assert [path.name for path in drafts] == [f"{job_id}-scout-report.md"]
    assert marker.is_file()


def _hub_job_id(key: str) -> str:
    return f"grokbot-{grokbot_jobs._idempotency_key_hash(key).removeprefix('sha256:')[:24]}"


def _store_hub_scout(
    queue: Path, *, key: str = "hub-scout-1", text: str = REPORT_TEXT
) -> tuple[str, dict[str, object]]:
    """Write the snapshot and artifact a hub-authority scout completion leaves locally."""
    spec = grokbot_jobs._validate_spec(_spec())
    job_id = _hub_job_id(key)
    grokbot_jobs._store_task_snapshot(queue, job_id, spec, grokbot_jobs._idempotency_key_hash(key))
    with grokbot_jobs._storage_paths(queue) as storage:
        grokbot_jobs._write_bytes_file(storage.artifacts, f"{job_id}.md", text.encode("utf-8"))
    digest = grokbot_jobs._task_hash(spec).removeprefix("sha256:")
    return job_id, {
        "job_id": job_id,
        "label": spec["label"],
        "role": spec["role"],
        "repository": spec["repository"],
        "task_digest": digest,
        "state": "completed",
        "created_at": "2026-09-01T12:00:00Z",
        "updated_at": "2026-09-01T12:05:00Z",
        "queued_at": "2026-09-01T12:00:00Z",
        "timeout_seconds": spec["timeout_seconds"],
        "item_revision": 4,
        "artifact_kind": "report",
        "artifact_digest": _report_digest(text),
        "artifact_size": len(text.encode("utf-8")),
        "private_snapshot_id": job_id,
    }


def _fake_hub_listing(monkeypatch: pytest.MonkeyPatch, jobs: list[dict[str, object]]) -> list[dict[str, object]]:
    from brigade import fleet_client_grokbot

    calls: list[dict[str, object]] = []

    def list_jobs(**kwargs: object) -> fleet_client_grokbot.GrokbotHubDecision:
        calls.append(kwargs)
        return fleet_client_grokbot.GrokbotHubDecision(True, "ok", jobs=list(jobs))

    monkeypatch.setattr(grokbot_jobs, "hub_authority", lambda _target=None: True)
    monkeypatch.setattr(fleet_client_grokbot, "list_jobs", list_jobs)
    return calls


def test_hub_authority_preview_lists_completed_scout_without_local_job_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    job_id, hub_job = _store_hub_scout(queue)
    leftover = _complete_report(queue, key="leftover-local")
    (_queue_root(queue) / "artifacts" / f"{leftover}.md").unlink()
    assert not (_queue_root(queue) / "jobs" / f"{job_id}.json").exists()
    calls = _fake_hub_listing(monkeypatch, [hub_job])

    result = grokbot_reconcile.preview(queue, owner)

    assert calls == [{"role": "repository-scout", "include_all": True}]
    assert result["eligible"] == 1
    assert result["known"] == 0
    assert result["unavailable"] == 0
    assert result["jobs"] == [{"job_id": job_id, "state": "completed"}]
    dumped = json.dumps(result, sort_keys=True)
    assert REPORT_TEXT not in dumped
    assert "PRIVATE_SCOUT_FINDING_TOKEN_alpha" not in dumped
    assert not _review_inbox(owner).exists()


def test_hub_authority_unavailable_is_missing_artifact_not_leftover_local_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    leftover = _complete_report(queue, key="leftover-local")
    (_queue_root(queue) / "artifacts" / f"{leftover}.md").unlink()
    job_id, hub_job = _store_hub_scout(queue, key="hub-orphan")
    (_queue_root(queue) / "artifacts" / f"{job_id}.md").unlink()
    _fake_hub_listing(monkeypatch, [hub_job])

    preview = grokbot_reconcile.preview(queue, owner)
    applied = grokbot_reconcile.apply(queue, owner)

    assert preview["eligible"] == 0
    assert preview["unavailable"] == 1
    assert preview["unavailable_reasons"] == {"artifact-missing": 1}
    assert applied["created"] == 0
    assert applied["unavailable"] == 1
    assert applied["unavailable_reasons"] == {"artifact-missing": 1}
    assert not _review_inbox(owner).exists()
    assert not (_queue_root(queue) / "reconcile" / f"{job_id}.json").exists()
    assert not (_queue_root(queue) / "reconcile" / f"{leftover}.json").exists()


def test_hub_authority_apply_drafts_unmarked_hub_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    job_id, hub_job = _store_hub_scout(queue)
    _fake_hub_listing(monkeypatch, [hub_job])
    task_hash = grokbot_jobs._task_hash(grokbot_jobs._validate_spec(_spec()))

    result = grokbot_reconcile.apply(queue, owner)

    assert result == {
        "eligible": 1,
        "known": 0,
        "unavailable": 0,
        "created": 1,
        "skipped": 0,
        "limit": 1,
        "jobs": [{"job_id": job_id, "state": "completed"}],
    }
    assert REPORT_TEXT not in json.dumps(result)
    inbox = _review_inbox(owner)
    drafts = list(inbox.glob("*.md"))
    assert [path.name for path in drafts] == [f"{job_id}-scout-report.md"]
    text = drafts[0].read_text(encoding="utf-8")
    assert job_id in text
    assert "example/brigade" in text
    assert task_hash in text
    assert _report_digest() in text
    assert "> PRIVATE_SCOUT_FINDING_TOKEN_alpha" in text
    marker = json.loads((_queue_root(queue) / "reconcile" / f"{job_id}.json").read_text(encoding="utf-8"))
    assert marker["schema"] == "brigade.grokbot.reconcile.v1"
    assert marker["job_id"] == job_id
    assert marker["sha256"] == _report_digest()
    assert marker["task_hash"] == task_hash
    assert REPORT_TEXT not in json.dumps(marker)


def test_hub_authority_known_marker_skips_hub_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    job_id, hub_job = _store_hub_scout(queue)
    _fake_hub_listing(monkeypatch, [hub_job])
    grokbot_reconcile.apply(queue, owner)

    preview = grokbot_reconcile.preview(queue, owner)
    second = grokbot_reconcile.apply(queue, owner)

    assert preview == {
        "eligible": 0,
        "known": 1,
        "unavailable": 0,
        "created": 0,
        "limit": 1,
        "jobs": [],
    }
    assert second["created"] == 0
    assert second["skipped"] == 1
    assert second["known"] == 1
    assert list(_review_inbox(owner).glob("*.md")) == [_review_inbox(owner) / f"{job_id}-scout-report.md"]


def test_cli_reconcile_preview_binds_operator_identity_for_hub_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    from brigade import fleet_client_grokbot

    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    job_id, hub_job = _store_hub_scout(queue)
    token = "operator-reconcile-token"  # content-guard: allow api-key-assignment
    token_file = tmp_path / "operator.hub-token"
    token_file.write_text(token + "\n", encoding="utf-8")
    token_file.chmod(0o600)
    monkeypatch.setenv("BRIGADE_GROKBOT_OPERATOR_HUB_TOKEN_FILE", str(token_file))
    seen: list[str | None] = []

    def list_jobs(**kwargs: object) -> fleet_client_grokbot.GrokbotHubDecision:
        seen.append(fleet_client_grokbot.current_listener_token())
        assert kwargs == {"role": "repository-scout", "include_all": True}
        return fleet_client_grokbot.GrokbotHubDecision(True, "ok", jobs=[hub_job])

    monkeypatch.setattr(grokbot_jobs, "hub_authority", lambda _target=None: True)
    monkeypatch.setattr(fleet_client_grokbot, "list_jobs", list_jobs)

    assert _run_reconcile(queue, owner) == 0
    assert seen == [token]
    captured = capsys.readouterr()
    assert f"job {job_id}" in captured.out
    assert token not in captured.out
    assert REPORT_TEXT not in captured.out


def test_cli_reconcile_preview_binds_feed_identity_when_operator_token_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    from brigade import fleet_client_grokbot

    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    job_id, hub_job = _store_hub_scout(queue)
    token = "feed-reconcile-token"  # content-guard: allow api-key-assignment
    token_file = tmp_path / "feed.hub-token"
    token_file.write_text(token + "\n", encoding="utf-8")
    token_file.chmod(0o600)
    monkeypatch.setenv("BRIGADE_GROKBOT_FEED_HUB_TOKEN_FILE", str(token_file))
    seen: list[str | None] = []

    def list_jobs(**kwargs: object) -> fleet_client_grokbot.GrokbotHubDecision:
        seen.append(fleet_client_grokbot.current_listener_token())
        return fleet_client_grokbot.GrokbotHubDecision(True, "ok", jobs=[hub_job])

    monkeypatch.setattr(grokbot_jobs, "hub_authority", lambda _target=None: True)
    monkeypatch.setattr(fleet_client_grokbot, "list_jobs", list_jobs)

    assert _run_reconcile(queue, owner) == 0
    assert seen == [token]
    assert token not in capsys.readouterr().out
    assert (_queue_root(queue) / "jobs" / f"{job_id}.json").exists() is False


def test_hub_authority_apply_lists_hub_before_exclusive_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from brigade import fleet_client_grokbot

    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    _job_id, hub_job = _store_hub_scout(queue)
    order: list[str] = []
    original_lock = grokbot_jobs._queue_lock

    @contextmanager
    def recording_lock(storage: object):
        order.append("lock")
        with original_lock(storage):
            yield

    def list_jobs(**kwargs: object) -> fleet_client_grokbot.GrokbotHubDecision:
        order.append("list")
        assert kwargs == {"role": "repository-scout", "include_all": True}
        return fleet_client_grokbot.GrokbotHubDecision(True, "ok", jobs=[hub_job])

    monkeypatch.setattr(grokbot_jobs, "hub_authority", lambda _target=None: True)
    monkeypatch.setattr(fleet_client_grokbot, "list_jobs", list_jobs)
    monkeypatch.setattr(grokbot_jobs, "_queue_lock", recording_lock)

    result = grokbot_reconcile.apply(queue, owner)

    assert result["created"] == 1
    assert order == ["list", "lock"]
