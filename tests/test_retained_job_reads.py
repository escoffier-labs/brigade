"""Retained projection readers preserve path and record validation after workers disappear."""

import json
from datetime import datetime, timezone

import pytest

from brigade import grokbot_jobs
from tests import legacy_job_seed as seed
from tests.test_cloud_tracker import _grokbot_spec
from tests._posix import requires_dirfd

NOW = datetime(2026, 8, 23, 12, tzinfo=timezone.utc)


@requires_dirfd
@pytest.mark.parametrize("component", ["grokbot", "jobs", "idempotency", "artifacts", "snapshots"])
def test_projection_reader_refuses_symlinked_components(tmp_path, monkeypatch, component):
    monkeypatch.setattr(grokbot_jobs, "hub_authority", lambda target: False)
    root = tmp_path / ".brigade/cloud/grokbot"
    root.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    if component == "grokbot":
        root.symlink_to(outside, target_is_directory=True)
    else:
        root.mkdir(mode=0o700)
        (root / component).symlink_to(outside, target_is_directory=True)
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^unsafe-storage$"):
        grokbot_jobs.tracker_rows(tmp_path)
    assert list(outside.iterdir()) == []


@requires_dirfd
def test_projection_reader_refuses_symlinked_job(tmp_path, monkeypatch):
    monkeypatch.setattr(grokbot_jobs, "hub_authority", lambda target: False)
    job_id = seed.enqueue(tmp_path, _grokbot_spec(), "fixture", now=NOW)["job_id"]
    path = tmp_path / ".brigade/cloud/grokbot/jobs" / (job_id + ".json")
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^unsafe-storage$"):
        grokbot_jobs.tracker_rows(tmp_path)
    assert outside.read_text(encoding="utf-8") == "{}"


@pytest.mark.parametrize(
    "corruption",
    ["task_hash", "timestamps", "item_revision", "lease_deadline", "invalid_bot", "invalid_cancel", "completed_cancel"],
)
def test_corrupt_retained_records_fail_closed(tmp_path, monkeypatch, corruption):
    monkeypatch.setattr(grokbot_jobs, "hub_authority", lambda target: False)
    job_id = seed.enqueue(tmp_path, _grokbot_spec(), "fixture", now=NOW)["job_id"]
    if corruption in {"lease_deadline", "invalid_bot", "invalid_cancel", "completed_cancel"}:
        seed.claim(tmp_path, job_id, "bot-a", "lease-a", 60, now=NOW)
    if corruption == "completed_cancel":
        seed.transition(
            tmp_path,
            job_id,
            "bot-a",
            "lease-a",
            "completed",
            artifact={"kind": "draft-pr", "url": "https://github.com/example/brigade/pull/1", "branch": "safe"},
            now=NOW,
        )
    path = tmp_path / ".brigade/cloud/grokbot/jobs" / (job_id + ".json")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if corruption == "task_hash":
        raw["task_hash"] = "sha256:" + "0" * 64
    elif corruption == "timestamps":
        raw["updated_at"] = "not-a-time"
    elif corruption == "item_revision":
        raw["item_revision"] = 0
    elif corruption == "lease_deadline":
        raw["lease_expires_at"] = "2026-08-23T12:15:01Z"
    elif corruption == "invalid_bot":
        raw["bot_id"] = "bot id"
    elif corruption == "invalid_cancel":
        raw["cancel_requested_at"] = "2026-08-23T11:59:59Z"
    else:
        raw["cancel_requested_at"] = raw["updated_at"]
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^corrupt-storage$"):
        grokbot_jobs.tracker_rows(tmp_path)
