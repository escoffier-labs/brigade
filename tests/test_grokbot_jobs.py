"""Private, handle-first Grok Bot queue contract tests."""

from __future__ import annotations

import stat
from datetime import datetime, timedelta, timezone
from multiprocessing import Pipe, Process
from pathlib import Path

import pytest

from brigade import grokbot_jobs


NOW = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)


def _spec() -> dict[str, object]:
    return {
        "label": "Add queue foundation",
        "role": "implementation-worker",
        "repository": "example/brigade",
        "base_ref": "main",
        "ownership_paths": ["src/brigade/grokbot_jobs.py", "tests/test_grokbot_jobs.py"],
        "instructions": "Build the private queue module.",
        "verification_commands": ["pytest -q tests/test_grokbot_jobs.py"],
        "artifact": {"kind": "draft-pr"},
        "timeout_seconds": 900,
    }


def _claim_in_process(target: str, job_id: str, lease_id: str, connection) -> None:
    """Attempt a claim in a separate process for the lock contract test."""
    try:
        result = grokbot_jobs.claim(
            Path(target),
            job_id,
            "bot-a",
            lease_id,
            60,
            now=NOW,
        )
    except grokbot_jobs.GrokbotJobError as exc:
        connection.send(exc.reason)
    else:
        connection.send(result["state"])
    finally:
        connection.close()


def _enqueue(tmp_path: Path, *, artifact: str = "draft-pr", idempotency_key: str | None = None) -> str:
    spec = _spec()
    spec["artifact"] = {"kind": artifact}
    if artifact == "report":
        spec["role"] = "repository-scout"
    return grokbot_jobs.enqueue(tmp_path, spec, idempotency_key or f"request-{artifact}", now=NOW)["job_id"]


def test_enqueue_returns_opaque_handle_and_private_projection(tmp_path: Path):
    handle = grokbot_jobs.enqueue(tmp_path, _spec(), "request-1", now=NOW)

    assert handle == {"job_id": handle["job_id"], "state": "queued", "idempotent": False}
    assert handle["job_id"].startswith("grokbot-")
    assert len(handle["job_id"]) == len("grokbot-") + 24

    job = grokbot_jobs.get_job(tmp_path, handle["job_id"])
    assert job["job_id"] == handle["job_id"]
    assert job["label"] == "Add queue foundation"
    assert job["role"] == "implementation-worker"
    assert job["repository"] == "example/brigade"
    assert job["state"] == "queued"
    assert job["task_hash"].startswith("sha256:")
    assert job["timeout_seconds"] == 900
    assert job["item_revision"] == 1
    assert job["artifact"] == {"kind": "draft-pr"}
    assert job["created_at"] == "2026-08-23T12:00:00Z"
    assert "instructions" not in job
    assert "verification_commands" not in job


def test_storage_is_private_and_keeps_the_full_envelope(tmp_path: Path):
    handle = grokbot_jobs.enqueue(tmp_path, _spec(), "request-1", now=NOW)
    root = tmp_path / ".brigade" / "cloud" / "grokbot"
    job_path = root / "jobs" / f"{handle['job_id']}.json"
    idempotency_files = list((root / "idempotency").glob("*.json"))

    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "jobs").stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "idempotency").stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "queue.lock").stat().st_mode) == 0o600
    assert stat.S_IMODE(job_path.stat().st_mode) == 0o600
    assert len(idempotency_files) == 1
    assert stat.S_IMODE(idempotency_files[0].stat().st_mode) == 0o600
    raw = job_path.read_text()
    assert "Build the private queue module." in raw
    assert "pytest -q tests/test_grokbot_jobs.py" in raw


def test_status_returns_only_safe_projections(tmp_path: Path):
    first = grokbot_jobs.enqueue(tmp_path, _spec(), "request-1", now=NOW)
    scout = _spec()
    scout.update({"label": "Scout", "role": "repository-scout", "artifact": {"kind": "report"}})
    second = grokbot_jobs.enqueue(tmp_path, scout, "request-2", now=NOW)

    result = grokbot_jobs.status(tmp_path)

    assert [job["job_id"] for job in result["jobs"]] == sorted([first["job_id"], second["job_id"]])
    for job in result["jobs"]:
        assert set(job) == {
            "job_id",
            "label",
            "role",
            "repository",
            "task_hash",
            "state",
            "created_at",
            "updated_at",
            "queued_at",
            "timeout_seconds",
            "item_revision",
            "artifact",
        }
        assert "instructions" not in job
        assert "verification_commands" not in job


def test_enqueue_is_idempotent_only_for_identical_canonical_task_content(tmp_path: Path):
    first = grokbot_jobs.enqueue(tmp_path, _spec(), "request-1", now=NOW)
    repeated = grokbot_jobs.enqueue(tmp_path, _spec(), "request-1", now=NOW)
    changed = _spec()
    changed["label"] = "Different task"

    assert repeated == {"job_id": first["job_id"], "state": "queued", "idempotent": True}
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^idempotency-conflict$") as exc:
        grokbot_jobs.enqueue(tmp_path, changed, "request-1", now=NOW)
    assert exc.value.reason == "idempotency-conflict"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda spec: spec.update({"unexpected": "value"}), "unknown-key"),
        (lambda spec: spec.pop("label"), "missing-key"),
        (lambda spec: spec.update({"role": "writer"}), "invalid-role"),
        (lambda spec: spec.update({"repository": "not/a/repo"}), "invalid-repository"),
        (lambda spec: spec.update({"base_ref": "main..bad"}), "invalid-base-ref"),
        (lambda spec: spec.update({"ownership_paths": ["/absolute.py"]}), "invalid-ownership-paths"),
        (lambda spec: spec.update({"ownership_paths": ["../escape.py"]}), "invalid-ownership-paths"),
        (lambda spec: spec.update({"ownership_paths": ["src\\escape.py"]}), "invalid-ownership-paths"),
        (lambda spec: spec.update({"ownership_paths": ["same.py", "same.py"]}), "invalid-ownership-paths"),
        (lambda spec: spec.update({"instructions": ""}), "invalid-instructions"),
        (lambda spec: spec.update({"verification_commands": []}), "invalid-verification-commands"),
        (lambda spec: spec.update({"verification_commands": ["x" * 1001]}), "invalid-verification-commands"),
        (lambda spec: spec.update({"artifact": {"kind": "report"}}), "invalid-artifact"),
        (lambda spec: spec.update({"timeout_seconds": 59}), "invalid-timeout"),
        (lambda spec: spec.update({"timeout_seconds": 14401}), "invalid-timeout"),
    ],
)
def test_enqueue_rejects_invalid_envelope_classes(tmp_path: Path, change, reason: str):
    spec = _spec()
    change(spec)

    with pytest.raises(grokbot_jobs.GrokbotJobError, match=rf"^{reason}$") as exc:
        grokbot_jobs.enqueue(tmp_path, spec, "request-1", now=NOW)
    assert exc.value.reason == reason


@pytest.mark.parametrize(
    "path",
    [
        ("artifact", "accessToken"),
        ("artifact", "nested", "deployment_secret"),
        ("unknown_header",),
    ],
)
def test_enqueue_recursively_rejects_sensitive_field_names(tmp_path: Path, path: tuple[str, ...]):
    spec = _spec()
    cursor = spec
    for key in path[:-1]:
        next_value = cursor.get(key)
        if not isinstance(next_value, dict):
            next_value = {}
            cursor[key] = next_value
        cursor = next_value
    cursor[path[-1]] = "value"

    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^forbidden-key$") as exc:
        grokbot_jobs.enqueue(tmp_path, spec, "request-1", now=NOW)
    assert exc.value.reason == "forbidden-key"


@pytest.mark.parametrize("key", ["", "space key", "x" * 129])
def test_enqueue_rejects_invalid_idempotency_keys(tmp_path: Path, key: str):
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^invalid-idempotency-key$"):
        grokbot_jobs.enqueue(tmp_path, _spec(), key, now=NOW)


def test_enqueue_rejects_scout_artifact_mismatch(tmp_path: Path):
    spec = _spec()
    spec.update({"role": "repository-scout", "artifact": {"kind": "branch"}})

    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^invalid-artifact$"):
        grokbot_jobs.enqueue(tmp_path, spec, "request-1", now=NOW)


@pytest.mark.parametrize("name", ["grokbot-not-hex", "other-0123456789abcdef01234567"])
def test_get_job_rejects_malformed_handles(tmp_path: Path, name: str):
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^invalid-job-id$"):
        grokbot_jobs.get_job(tmp_path, name)


@pytest.mark.parametrize("component", ["grokbot", "jobs", "idempotency", "queue.lock"])
def test_storage_refuses_symlinked_queue_components(tmp_path: Path, component: str):
    root = tmp_path / ".brigade" / "cloud" / "grokbot"
    root.parent.mkdir(parents=True)
    destination = tmp_path / "outside"
    destination.mkdir()
    if component == "grokbot":
        root.symlink_to(destination, target_is_directory=True)
    else:
        root.mkdir(mode=0o700)
        (root / component).symlink_to(destination, target_is_directory=component != "queue.lock")

    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^unsafe-storage$"):
        grokbot_jobs.enqueue(tmp_path, _spec(), "request-1", now=NOW)


@pytest.mark.parametrize("stored_file", ["job", "idempotency"])
def test_storage_refuses_symlinked_queue_files(tmp_path: Path, stored_file: str):
    handle = grokbot_jobs.enqueue(tmp_path, _spec(), "request-1", now=NOW)
    root = tmp_path / ".brigade" / "cloud" / "grokbot"
    if stored_file == "job":
        path = root / "jobs" / f"{handle['job_id']}.json"
    else:
        path = next((root / "idempotency").glob("*.json"))
    destination = tmp_path / "outside.json"
    destination.write_text("{}")
    path.unlink()
    path.symlink_to(destination)

    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^unsafe-storage$"):
        if stored_file == "job":
            grokbot_jobs.get_job(tmp_path, handle["job_id"])
        else:
            grokbot_jobs.enqueue(tmp_path, _spec(), "request-1", now=NOW)


def test_concurrent_claims_have_one_winner(tmp_path: Path):
    job_id = _enqueue(tmp_path)
    first_parent, first_child = Pipe(duplex=False)
    second_parent, second_child = Pipe(duplex=False)
    first = Process(target=_claim_in_process, args=(str(tmp_path), job_id, "lease-a", first_child))
    second = Process(target=_claim_in_process, args=(str(tmp_path), job_id, "lease-b", second_child))

    first.start()
    second.start()
    first_child.close()
    second_child.close()
    outcomes = {first_parent.recv(), second_parent.recv()}
    first.join()
    second.join()

    assert first.exitcode == 0
    assert second.exitcode == 0
    assert outcomes == {"claimed", "lease-conflict"}


def test_claim_is_idempotent_for_same_lease_and_rejects_another(tmp_path: Path):
    job_id = _enqueue(tmp_path)
    claimed = grokbot_jobs.claim(tmp_path, job_id, "bot-a", "lease-a", 60, now=NOW)
    retried = grokbot_jobs.claim(tmp_path, job_id, "bot-a", "lease-a", 60, now=NOW)

    assert claimed["state"] == "claimed"
    assert claimed["item_revision"] == 2
    assert retried == claimed
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^lease-conflict$"):
        grokbot_jobs.claim(tmp_path, job_id, "bot-a", "lease-b", 60, now=NOW)
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^lease-conflict$"):
        grokbot_jobs.claim(tmp_path, job_id, "bot-b", "lease-a", 60, now=NOW)


def test_renew_clamps_to_deadline_and_increments_revision(tmp_path: Path):
    job_id = _enqueue(tmp_path)
    grokbot_jobs.claim(tmp_path, job_id, "bot-a", "lease-a", 3600, now=NOW)
    renewed = grokbot_jobs.renew(
        tmp_path,
        job_id,
        "bot-a",
        "lease-a",
        3600,
        now=NOW + timedelta(seconds=100),
    )

    assert renewed["item_revision"] == 3
    assert renewed["lease_expires_at"] == "2026-08-23T12:15:00Z"
    assert renewed["updated_at"] == "2026-08-23T12:01:40Z"


@pytest.mark.parametrize("lease_seconds", [29, 3601])
def test_claim_rejects_out_of_range_leases(tmp_path: Path, lease_seconds: int):
    job_id = _enqueue(tmp_path)
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^invalid-lease-seconds$"):
        grokbot_jobs.claim(tmp_path, job_id, "bot-a", "lease-a", lease_seconds, now=NOW)


def test_valid_lifecycle_transition_and_draft_pr_artifact(tmp_path: Path):
    job_id = _enqueue(tmp_path)
    grokbot_jobs.claim(tmp_path, job_id, "bot-a", "lease-a", 60, now=NOW)
    running = grokbot_jobs.transition(tmp_path, job_id, "bot-a", "lease-a", "running", now=NOW)
    completed = grokbot_jobs.transition(
        tmp_path,
        job_id,
        "bot-a",
        "lease-a",
        "completed",
        artifact={"kind": "draft-pr", "url": "https://github.com/example/brigade/pull/123", "branch": "grokbot/job-1"},
        now=NOW,
    )

    assert running["state"] == "running"
    assert completed["state"] == "completed"
    assert completed["item_revision"] == 4
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^terminal-state$"):
        grokbot_jobs.transition(tmp_path, job_id, "bot-a", "lease-a", "failed", now=NOW)


@pytest.mark.parametrize(
    ("artifact_kind", "artifact", "reason"),
    [
        ("draft-pr", {"kind": "draft-pr", "url": "http://github.com/example/brigade/pull/1", "branch": "safe"}, "invalid-artifact"),
        ("draft-pr", {"kind": "draft-pr", "url": "https://github.com/example/brigade/issues/1", "branch": "safe"}, "invalid-artifact"),
        ("draft-pr", {"kind": "draft-pr", "url": "https://github.com/example/brigade/pull/1", "branch": "bad@{ref"}, "invalid-artifact"),
        ("branch", {"kind": "branch", "branch": "../bad", "commit": "a" * 40}, "invalid-artifact"),
        ("branch", {"kind": "branch", "branch": "safe", "commit": "A" * 40}, "invalid-artifact"),
        ("report", {"kind": "report", "path": "/report.md", "sha256": "a" * 64}, "invalid-artifact"),
        ("report", {"kind": "report", "path": "report.md", "sha256": "A" * 64}, "invalid-artifact"),
        ("branch", {"kind": "draft-pr", "url": "https://github.com/example/brigade/pull/1", "branch": "safe"}, "artifact-mismatch"),
    ],
)
def test_completed_artifacts_are_exact_and_match_specification(
    tmp_path: Path, artifact_kind: str, artifact: dict[str, str], reason: str
):
    job_id = _enqueue(tmp_path, artifact=artifact_kind)
    grokbot_jobs.claim(tmp_path, job_id, "bot-a", "lease-a", 60, now=NOW)
    grokbot_jobs.transition(tmp_path, job_id, "bot-a", "lease-a", "running", now=NOW)

    with pytest.raises(grokbot_jobs.GrokbotJobError, match=rf"^{reason}$"):
        grokbot_jobs.transition(tmp_path, job_id, "bot-a", "lease-a", "completed", artifact=artifact, now=NOW)


def test_branch_and_report_artifacts_complete_their_allowed_roles(tmp_path: Path):
    branch_job = _enqueue(tmp_path, artifact="branch")
    report_job = _enqueue(tmp_path, artifact="report")
    for job_id, artifact in (
        (branch_job, {"kind": "branch", "branch": "grokbot/branch", "commit": "a" * 40}),
        (report_job, {"kind": "report", "path": "artifacts/report.md", "sha256": "b" * 64}),
    ):
        grokbot_jobs.claim(tmp_path, job_id, "bot-a", "lease-a", 60, now=NOW)
        grokbot_jobs.transition(tmp_path, job_id, "bot-a", "lease-a", "running", now=NOW)
        assert grokbot_jobs.transition(
            tmp_path, job_id, "bot-a", "lease-a", "completed", artifact=artifact, now=NOW
        )["state"] == "completed"


def test_failure_terminalizes_claimed_or_running_job(tmp_path: Path):
    job_id = _enqueue(tmp_path)
    grokbot_jobs.claim(tmp_path, job_id, "bot-a", "lease-a", 60, now=NOW)
    failed = grokbot_jobs.transition(tmp_path, job_id, "bot-a", "lease-a", "failed", now=NOW)

    assert failed["state"] == "failed"
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^terminal-state$"):
        grokbot_jobs.renew(tmp_path, job_id, "bot-a", "lease-a", 60, now=NOW)


def test_transitions_require_the_current_unexpired_lease(tmp_path: Path):
    job_id = _enqueue(tmp_path)
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^invalid-state$"):
        grokbot_jobs.transition(tmp_path, job_id, "bot-a", "lease-a", "running", now=NOW)
    grokbot_jobs.claim(tmp_path, job_id, "bot-a", "lease-a", 30, now=NOW)
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^lease-conflict$"):
        grokbot_jobs.transition(tmp_path, job_id, "bot-b", "lease-a", "running", now=NOW)
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^lease-expired$"):
        grokbot_jobs.transition(tmp_path, job_id, "bot-a", "lease-a", "running", now=NOW + timedelta(seconds=30))


def test_cancel_queued_and_cooperative_live_job(tmp_path: Path):
    queued_job = _enqueue(tmp_path)
    assert grokbot_jobs.cancel(tmp_path, queued_job, now=NOW)["state"] == "canceled"
    assert grokbot_jobs.cancel(tmp_path, queued_job, now=NOW)["state"] == "canceled"

    live_job = _enqueue(tmp_path, idempotency_key="request-live")
    grokbot_jobs.claim(tmp_path, live_job, "bot-a", "lease-a", 60, now=NOW)
    requested = grokbot_jobs.cancel(tmp_path, live_job, now=NOW)
    repeated = grokbot_jobs.cancel(tmp_path, live_job, now=NOW + timedelta(seconds=1))
    assert requested["state"] == "claimed"
    assert requested["cancel_requested_at"] == repeated["cancel_requested_at"]
    assert grokbot_jobs.acknowledge_cancel(tmp_path, live_job, "bot-a", "lease-a", now=NOW)["state"] == "canceled"


def test_expiry_never_requeues_and_late_completion_is_rejected(tmp_path: Path):
    queued_job = _enqueue(tmp_path)
    assert grokbot_jobs.expire(tmp_path, queued_job, now=NOW + timedelta(seconds=900))["state"] == "expired"

    live_job = _enqueue(tmp_path, idempotency_key="request-live")
    grokbot_jobs.claim(tmp_path, live_job, "bot-a", "lease-a", 30, now=NOW)
    grokbot_jobs.transition(tmp_path, live_job, "bot-a", "lease-a", "running", now=NOW)
    assert grokbot_jobs.expire(tmp_path, live_job, now=NOW + timedelta(seconds=30))["state"] == "expired"
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^terminal-state$"):
        grokbot_jobs.transition(
            tmp_path,
            live_job,
            "bot-a",
            "lease-a",
            "completed",
            artifact={"kind": "draft-pr", "url": "https://github.com/example/brigade/pull/1", "branch": "safe"},
            now=NOW + timedelta(seconds=30),
        )


@pytest.mark.parametrize(
    "corruption",
    ["task_hash", "timestamps", "item_revision", "lease_deadline", "invalid_bot", "invalid_cancel"],
)
def test_corrupted_lifecycle_records_fail_closed(tmp_path: Path, corruption: str):
    job_id = _enqueue(tmp_path)
    if corruption in {"lease_deadline", "invalid_bot", "invalid_cancel"}:
        grokbot_jobs.claim(tmp_path, job_id, "bot-a", "lease-a", 60, now=NOW)
    path = tmp_path / ".brigade" / "cloud" / "grokbot" / "jobs" / f"{job_id}.json"
    raw = __import__("json").loads(path.read_text())
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
    else:
        raw["cancel_requested_at"] = "2026-08-23T11:59:59Z"
    path.write_text(__import__("json").dumps(raw))

    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^corrupt-storage$"):
        grokbot_jobs.claim(tmp_path, job_id, "bot-a", "lease-a", 60, now=NOW)


def test_corrupted_idempotency_record_fails_closed(tmp_path: Path):
    _enqueue(tmp_path)
    path = next((tmp_path / ".brigade" / "cloud" / "grokbot" / "idempotency").glob("*.json"))
    path.write_text('{"schema":"brigade.grokbot.idempotency.v1","job_id":"bad","task_hash":"wrong"}')

    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^corrupt-storage$"):
        grokbot_jobs.enqueue(tmp_path, _spec(), "request-draft-pr", now=NOW)
