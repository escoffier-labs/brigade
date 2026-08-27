"""Private, handle-first Grok Bot queue contract tests."""

from __future__ import annotations

import hashlib
import json
import stat
from datetime import datetime, timedelta, timezone
from multiprocessing import Pipe, Process
from pathlib import Path

import pytest

from brigade import grokbot_jobs
from brigade import cli


NOW = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
REPORT_TEXT = "PRIVATE_SCOUT_FINDING_TOKEN_alpha\nScout findings for example/brigade.\n"


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


def _enqueue_scout_in_process(target: str, issue_number: int, connection) -> None:
    """Exercise the Scout admission guard from two independent producers."""
    spec = _spec()
    spec.update({"label": "Repository Scout", "role": "repository-scout", "artifact": {"kind": "report"}})
    try:
        result = grokbot_jobs.enqueue_repository_scout(
            Path(target), spec, f"scout-{issue_number}", daily_limit=3, now=NOW
        )
    except grokbot_jobs.GrokbotJobError as exc:
        connection.send({"error": exc.reason})
    else:
        connection.send(result)
    finally:
        connection.close()


def _enqueue(tmp_path: Path, *, artifact: str = "draft-pr", idempotency_key: str | None = None) -> str:
    spec = _spec()
    spec["artifact"] = {"kind": artifact}
    if artifact == "report":
        spec["role"] = "repository-scout"
    return grokbot_jobs.enqueue(tmp_path, spec, idempotency_key or f"request-{artifact}", now=NOW)["job_id"]


def _report_digest(text: str = REPORT_TEXT) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _report_artifact(text: str = REPORT_TEXT, path: str = "artifacts/report.md") -> dict[str, str]:
    return {"kind": "report", "path": path, "sha256": _report_digest(text)}


def _running_report(tmp_path: Path, *, idempotency_key: str = "request-report") -> str:
    job_id = _enqueue(tmp_path, artifact="report", idempotency_key=idempotency_key)
    grokbot_jobs.claim(tmp_path, job_id, "bot-a", "lease-a", 60, now=NOW)
    grokbot_jobs.transition(tmp_path, job_id, "bot-a", "lease-a", "running", now=NOW)
    return job_id


def _snapshot_path(tmp_path: Path, job_id: str) -> Path:
    return tmp_path / ".brigade" / "cloud" / "grokbot" / "artifacts" / f"{job_id}.md"


def _job_json(tmp_path: Path, job_id: str) -> str:
    return (tmp_path / ".brigade" / "cloud" / "grokbot" / "jobs" / f"{job_id}.json").read_text()


def _assert_surfaces_omit_report_text(tmp_path: Path, job_id: str, text: str = REPORT_TEXT) -> None:
    dumped_status = json.dumps(grokbot_jobs.status(tmp_path, job_id), sort_keys=True)
    dumped_tracker = json.dumps(grokbot_jobs.tracker_rows(tmp_path), sort_keys=True)
    raw_job = _job_json(tmp_path, job_id)
    assert text not in dumped_status
    assert text not in dumped_tracker
    assert text not in raw_job
    assert "PRIVATE_SCOUT_FINDING_TOKEN_alpha" not in dumped_status
    assert "PRIVATE_SCOUT_FINDING_TOKEN_alpha" not in dumped_tracker
    assert "PRIVATE_SCOUT_FINDING_TOKEN_alpha" not in raw_job


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
    assert stat.S_IMODE((root / "artifacts").stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "queue.lock").stat().st_mode) == 0o600
    assert stat.S_IMODE(job_path.stat().st_mode) == 0o600
    assert len(idempotency_files) == 1
    assert stat.S_IMODE(idempotency_files[0].stat().st_mode) == 0o600
    raw = job_path.read_text()
    assert "Build the private queue module." in raw
    assert "pytest -q tests/test_grokbot_jobs.py" in raw


def test_concurrent_scout_admission_creates_only_one_active_job(tmp_path: Path):
    first_parent, first_child = Pipe(duplex=False)
    second_parent, second_child = Pipe(duplex=False)
    first = Process(target=_enqueue_scout_in_process, args=(str(tmp_path), 1, first_child))
    second = Process(target=_enqueue_scout_in_process, args=(str(tmp_path), 2, second_child))
    first.start()
    second.start()
    first_child.close()
    second_child.close()

    first_result = first_parent.recv()
    second_result = second_parent.recv()
    first.join(timeout=10)
    second.join(timeout=10)

    assert first.exitcode == 0
    assert second.exitcode == 0
    assert {result["reason"] for result in (first_result, second_result)} == {"created", "active-scout"}
    assert len(list((tmp_path / ".brigade" / "cloud" / "grokbot" / "jobs").glob("*.json"))) == 1


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


def test_enqueue_recovers_a_job_when_the_idempotency_write_failed(tmp_path: Path, monkeypatch):
    original_replace = grokbot_jobs.os.replace
    calls = 0

    def fail_index_replace(source, destination, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected idempotency write failure")
        return original_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(grokbot_jobs.os, "replace", fail_index_replace)
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^unsafe-storage$"):
        grokbot_jobs.enqueue(tmp_path, _spec(), "request-recovery", now=NOW)

    monkeypatch.setattr(grokbot_jobs.os, "replace", original_replace)
    recovered = grokbot_jobs.enqueue(tmp_path, _spec(), "request-recovery", now=NOW)
    changed = _spec()
    changed["label"] = "Different task"

    assert recovered["idempotent"] is True
    assert len(list((tmp_path / ".brigade" / "cloud" / "grokbot" / "jobs").glob("*.json"))) == 1
    raw = json.loads((tmp_path / ".brigade" / "cloud" / "grokbot" / "jobs" / f"{recovered['job_id']}.json").read_text())
    assert raw["idempotency_key_hash"] == "sha256:" + __import__("hashlib").sha256(b"request-recovery").hexdigest()
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^idempotency-conflict$"):
        grokbot_jobs.enqueue(tmp_path, changed, "request-recovery", now=NOW)


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


@pytest.mark.parametrize("component", ["grokbot", "jobs", "idempotency", "artifacts", "queue.lock"])
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


def test_claim_execution_context_is_exact_and_safe_claim_stays_redacted(tmp_path: Path):
    context_job = _enqueue(tmp_path, idempotency_key="request-context")
    claimed = grokbot_jobs.claim_execution_context(tmp_path, context_job, "bot-a", "lease-a", 60, now=NOW)

    assert claimed["execution_context"] == {
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
    assert grokbot_jobs.claim_execution_context(tmp_path, context_job, "bot-a", "lease-a", 60, now=NOW) == claimed

    safe_job = _enqueue(tmp_path, idempotency_key="request-safe")
    safe = grokbot_jobs.claim(tmp_path, safe_job, "bot-a", "lease-safe", 60, now=NOW)
    assert "execution_context" not in safe


def test_claim_execution_context_rejects_conflicting_and_expired_leases(tmp_path: Path):
    job_id = _enqueue(tmp_path, idempotency_key="request-context-boundary")
    grokbot_jobs.claim_execution_context(tmp_path, job_id, "bot-a", "lease-a", 60, now=NOW)

    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^lease-conflict$"):
        grokbot_jobs.claim_execution_context(tmp_path, job_id, "bot-a", "lease-b", 60, now=NOW)
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^lease-expired$"):
        grokbot_jobs.claim_execution_context(tmp_path, job_id, "bot-a", "lease-a", 60, now=NOW + timedelta(seconds=60))


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
        (
            "draft-pr",
            {"kind": "draft-pr", "url": "http://github.com/example/brigade/pull/1", "branch": "safe"},
            "invalid-artifact",
        ),
        (
            "draft-pr",
            {"kind": "draft-pr", "url": "https://github.com/example/brigade/issues/1", "branch": "safe"},
            "invalid-artifact",
        ),
        (
            "draft-pr",
            {"kind": "draft-pr", "url": "https://github.com/example/brigade/pull/1", "branch": "bad@{ref"},
            "invalid-artifact",
        ),
        ("branch", {"kind": "branch", "branch": "../bad", "commit": "a" * 40}, "invalid-artifact"),
        ("branch", {"kind": "branch", "branch": "safe", "commit": "A" * 40}, "invalid-artifact"),
        ("report", {"kind": "report", "path": "/report.md", "sha256": "a" * 64}, "invalid-artifact"),
        ("report", {"kind": "report", "path": "report.md", "sha256": "A" * 64}, "invalid-artifact"),
        (
            "branch",
            {"kind": "draft-pr", "url": "https://github.com/example/brigade/pull/1", "branch": "safe"},
            "artifact-mismatch",
        ),
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
        assert (
            grokbot_jobs.transition(tmp_path, job_id, "bot-a", "lease-a", "completed", artifact=artifact, now=NOW)[
                "state"
            ]
            == "completed"
        )


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


def test_cancel_requested_claim_cannot_start(tmp_path: Path):
    job_id = _enqueue(tmp_path)
    grokbot_jobs.claim(tmp_path, job_id, "bot-a", "lease-a", 60, now=NOW)
    grokbot_jobs.cancel(tmp_path, job_id, now=NOW)

    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^cancel-requested$"):
        grokbot_jobs.transition(tmp_path, job_id, "bot-a", "lease-a", "running", now=NOW)


def test_mutation_rejects_backward_clock(tmp_path: Path):
    job_id = _enqueue(tmp_path)
    grokbot_jobs.claim(tmp_path, job_id, "bot-a", "lease-a", 60, now=NOW)

    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^clock-regression$"):
        grokbot_jobs.renew(tmp_path, job_id, "bot-a", "lease-a", 60, now=NOW - timedelta(seconds=1))


def test_storage_os_errors_use_the_unsafe_storage_reason(tmp_path: Path, monkeypatch, capsys):
    job_id = _enqueue(tmp_path)
    original_open = grokbot_jobs.os.open

    def fail_job_open(path, *args, **kwargs):
        if path == f"{job_id}.json" and kwargs.get("dir_fd") is not None:
            raise OSError("injected read failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(grokbot_jobs.os, "open", fail_job_open)
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^unsafe-storage$"):
        grokbot_jobs.get_job(tmp_path, job_id)

    assert _run_grokbot(tmp_path, "status", "--job-id", job_id) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "error: unsafe-storage"


@pytest.mark.skipif(grokbot_jobs.os.name != "posix", reason="POSIX directory descriptor contract")
def test_posix_storage_write_stays_anchored_when_visible_directory_is_replaced(tmp_path: Path):
    root = tmp_path / ".brigade" / "cloud" / "grokbot"
    outside = tmp_path / "outside"
    outside.mkdir()

    with grokbot_jobs._storage_paths(tmp_path) as storage:
        moved = root / "jobs-pinned"
        (root / "jobs").rename(moved)
        (root / "jobs").symlink_to(outside, target_is_directory=True)
        grokbot_jobs._write_json_file(storage.jobs, "probe.json", {"anchored": True})

    assert json.loads((moved / "probe.json").read_text()) == {"anchored": True}
    assert not (outside / "probe.json").exists()


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
    ["task_hash", "timestamps", "item_revision", "lease_deadline", "invalid_bot", "invalid_cancel", "completed_cancel"],
)
def test_corrupted_lifecycle_records_fail_closed(tmp_path: Path, corruption: str):
    job_id = _enqueue(tmp_path)
    if corruption in {"lease_deadline", "invalid_bot", "invalid_cancel", "completed_cancel"}:
        grokbot_jobs.claim(tmp_path, job_id, "bot-a", "lease-a", 60, now=NOW)
    if corruption == "completed_cancel":
        grokbot_jobs.transition(tmp_path, job_id, "bot-a", "lease-a", "running", now=NOW)
        grokbot_jobs.transition(
            tmp_path,
            job_id,
            "bot-a",
            "lease-a",
            "completed",
            artifact={"kind": "draft-pr", "url": "https://github.com/example/brigade/pull/1", "branch": "safe"},
            now=NOW,
        )
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
    elif corruption == "invalid_cancel":
        raw["cancel_requested_at"] = "2026-08-23T11:59:59Z"
    else:
        raw["cancel_requested_at"] = raw["updated_at"]
    path.write_text(__import__("json").dumps(raw))

    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^corrupt-storage$"):
        grokbot_jobs.claim(tmp_path, job_id, "bot-a", "lease-a", 60, now=NOW)


def test_corrupted_idempotency_record_fails_closed(tmp_path: Path):
    _enqueue(tmp_path)
    path = next((tmp_path / ".brigade" / "cloud" / "grokbot" / "idempotency").glob("*.json"))
    path.write_text('{"schema":"brigade.grokbot.idempotency.v1","job_id":"bad","task_hash":"wrong"}')

    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^corrupt-storage$"):
        grokbot_jobs.enqueue(tmp_path, _spec(), "request-draft-pr", now=NOW)


def _run_grokbot(target: Path, *command: str) -> int:
    return cli.main(["run", "cloud", "grokbot", *command, "--target", str(target)])


def test_cli_grokbot_lifecycle_commands_keep_private_content_out_of_output(tmp_path: Path, capsys):
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(_spec()))
    artifact_path = tmp_path / "artifact.json"
    artifact_path.write_text(
        json.dumps(
            {
                "kind": "draft-pr",
                "url": "https://github.com/example/brigade/pull/123",
                "branch": "grokbot/cli-complete",
            }
        )
    )

    assert _run_grokbot(tmp_path, "enqueue", "--spec", str(spec_path), "--idempotency-key", "cli-complete") == 0
    queued = capsys.readouterr()
    assert "grokbot-" in queued.out and "queued" in queued.out
    assert "Build the private queue module." not in queued.out
    assert "pytest -q tests/test_grokbot_jobs.py" not in queued.out
    job_id = queued.out.split()[1]

    assert (
        _run_grokbot(
            tmp_path,
            "claim",
            "--job-id",
            job_id,
            "--bot-id",
            "bot-a",
            "--lease-id",
            "lease-a",
            "--lease-seconds",
            "60",
            "--json",
        )
        == 0
    )
    claimed = capsys.readouterr().out
    assert json.loads(claimed)["state"] == "claimed"
    assert "instructions" not in claimed and "verification_commands" not in claimed
    assert "execution_context" not in claimed

    assert (
        _run_grokbot(
            tmp_path,
            "renew",
            "--job-id",
            job_id,
            "--bot-id",
            "bot-a",
            "--lease-id",
            "lease-a",
            "--lease-seconds",
            "60",
        )
        == 0
    )
    assert f"job {job_id} state=claimed" in capsys.readouterr().out

    assert _run_grokbot(tmp_path, "start", "--job-id", job_id, "--bot-id", "bot-a", "--lease-id", "lease-a") == 0
    assert f"job {job_id} state=running" in capsys.readouterr().out

    assert (
        _run_grokbot(
            tmp_path,
            "complete",
            "--job-id",
            job_id,
            "--bot-id",
            "bot-a",
            "--lease-id",
            "lease-a",
            "--artifact",
            str(artifact_path),
            "--json",
        )
        == 0
    )
    completed = capsys.readouterr().out
    assert json.loads(completed)["state"] == "completed"
    assert "instructions" not in completed and "verification_commands" not in completed

    for suffix, state in (("fail", "failed"), ("cancel", "canceled"), ("expire", "queued")):
        assert _run_grokbot(tmp_path, "enqueue", "--spec", str(spec_path), "--idempotency-key", f"cli-{suffix}") == 0
        next_job = capsys.readouterr().out.split()[1]
        if suffix == "fail":
            assert (
                _run_grokbot(
                    tmp_path,
                    "claim",
                    "--job-id",
                    next_job,
                    "--bot-id",
                    "bot-a",
                    "--lease-id",
                    "lease-a",
                    "--lease-seconds",
                    "60",
                )
                == 0
            )
            capsys.readouterr()
            command = ["fail", "--job-id", next_job, "--bot-id", "bot-a", "--lease-id", "lease-a"]
        else:
            command = [suffix, "--job-id", next_job]
        assert _run_grokbot(tmp_path, *command) == 0
        assert f"job {next_job} state={state}" in capsys.readouterr().out

    assert _run_grokbot(tmp_path, "enqueue", "--spec", str(spec_path), "--idempotency-key", "cli-ack") == 0
    cancel_job = capsys.readouterr().out.split()[1]
    assert (
        _run_grokbot(
            tmp_path,
            "claim",
            "--job-id",
            cancel_job,
            "--bot-id",
            "bot-a",
            "--lease-id",
            "lease-a",
            "--lease-seconds",
            "60",
        )
        == 0
    )
    capsys.readouterr()
    assert _run_grokbot(tmp_path, "cancel", "--job-id", cancel_job) == 0
    capsys.readouterr()
    assert (
        _run_grokbot(tmp_path, "ack-cancel", "--job-id", cancel_job, "--bot-id", "bot-a", "--lease-id", "lease-a") == 0
    )
    assert f"job {cancel_job} state=canceled" in capsys.readouterr().out

    assert _run_grokbot(tmp_path, "status", "--job-id", job_id, "--json") == 0
    status = capsys.readouterr().out
    assert json.loads(status)["job_id"] == job_id
    assert "instructions" not in status and "verification_commands" not in status
    assert _run_grokbot(tmp_path, "status") == 0
    status_text = capsys.readouterr().out
    assert "grokbot jobs:" in status_text
    assert "Build the private queue module." not in status_text
    assert "pytest -q tests/test_grokbot_jobs.py" not in status_text


@pytest.mark.parametrize(
    ("command", "expected_error"),
    [
        (("enqueue", "--spec", "missing.json", "--idempotency-key", "missing"), "--spec is not a file"),
        (
            ("claim", "--job-id", "not-a-job", "--bot-id", "bot-a", "--lease-id", "lease-a", "--lease-seconds", "60"),
            "invalid-job-id",
        ),
    ],
)
def test_cli_grokbot_rejects_invalid_inputs_with_exit_2(tmp_path: Path, capsys, command, expected_error: str):
    assert _run_grokbot(tmp_path, *command) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip().startswith("error: ")
    assert expected_error in captured.err


@pytest.mark.parametrize("payload", ["not json", "[]"])
def test_cli_grokbot_rejects_invalid_json_files(tmp_path: Path, capsys, payload: str):
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(payload)

    assert _run_grokbot(tmp_path, "enqueue", "--spec", str(spec_path), "--idempotency-key", "invalid-json") == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "error: invalid JSON object in --spec"


@pytest.mark.parametrize(
    ("filename", "payload", "expected_error"),
    [
        ("missing-artifact.json", None, "--artifact is not a file"),
        ("artifact.json", "not json", "invalid JSON object in --artifact"),
        ("artifact.json", "[]", "invalid JSON object in --artifact"),
    ],
)
def test_cli_grokbot_complete_rejects_invalid_artifact_files(
    tmp_path: Path, capsys, filename: str, payload: str | None, expected_error: str
):
    job_id = _enqueue(tmp_path)
    grokbot_jobs.claim(tmp_path, job_id, "bot-a", "lease-a", 60, now=NOW)
    grokbot_jobs.transition(tmp_path, job_id, "bot-a", "lease-a", "running", now=NOW)
    artifact_path = tmp_path / filename
    if payload is not None:
        artifact_path.write_text(payload)

    assert (
        _run_grokbot(
            tmp_path,
            "complete",
            "--job-id",
            job_id,
            "--bot-id",
            "bot-a",
            "--lease-id",
            "lease-a",
            "--artifact",
            str(artifact_path),
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip().startswith("error: ")
    assert expected_error in captured.err


def test_cli_grokbot_validates_target_before_queue_access(tmp_path: Path, capsys):
    not_directory = tmp_path / "not-a-directory"
    not_directory.write_text("x")

    assert _run_grokbot(not_directory, "status") == 2
    assert capsys.readouterr().err.strip() == f"error: --target is not a directory: {not_directory.resolve()}"


def test_complete_report_roundtrip_keeps_snapshot_private(tmp_path: Path, caplog):
    job_id = _running_report(tmp_path)
    completed = grokbot_jobs.complete_report(
        tmp_path, job_id, "bot-a", "lease-a", _report_artifact(), REPORT_TEXT, now=NOW
    )
    snapshot = _snapshot_path(tmp_path, job_id)
    encoded = REPORT_TEXT.encode("utf-8")

    assert grokbot_jobs.MAX_REPORT_BYTES == 12000
    assert completed["state"] == "completed"
    assert REPORT_TEXT not in json.dumps(completed)
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600
    assert snapshot.read_bytes() == encoded
    assert grokbot_jobs.read_report(tmp_path, job_id) == {
        "job_id": job_id,
        "text": REPORT_TEXT,
        "bytes": len(encoded),
        "sha256": _report_digest(),
    }
    _assert_surfaces_omit_report_text(tmp_path, job_id)
    assert REPORT_TEXT not in caplog.text


def test_complete_report_writes_snapshot_before_the_terminal_record(tmp_path: Path, monkeypatch):
    job_id = _running_report(tmp_path)
    original_replace = grokbot_jobs.os.replace

    def fail_job_replace(source, destination, *args, **kwargs):
        dest = destination if isinstance(destination, str) else str(destination)
        if dest.endswith(".json"):
            raise OSError("injected job write failure")
        return original_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(grokbot_jobs.os, "replace", fail_job_replace)
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^unsafe-storage$") as exc:
        grokbot_jobs.complete_report(tmp_path, job_id, "bot-a", "lease-a", _report_artifact(), REPORT_TEXT, now=NOW)

    assert REPORT_TEXT not in str(exc.value)
    assert _snapshot_path(tmp_path, job_id).read_text(encoding="utf-8") == REPORT_TEXT
    assert grokbot_jobs.get_job(tmp_path, job_id)["state"] == "running"


def test_complete_report_rejects_digest_mismatch_without_writing(tmp_path: Path):
    job_id = _running_report(tmp_path)
    artifact = _report_artifact()
    artifact["sha256"] = "0" * 64

    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^digest-mismatch$") as exc:
        grokbot_jobs.complete_report(tmp_path, job_id, "bot-a", "lease-a", artifact, REPORT_TEXT, now=NOW)

    assert exc.value.reason == "digest-mismatch"
    assert REPORT_TEXT not in str(exc.value)
    assert not _snapshot_path(tmp_path, job_id).exists()
    assert grokbot_jobs.get_job(tmp_path, job_id)["state"] == "running"
    _assert_surfaces_omit_report_text(tmp_path, job_id)


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("", "invalid-report"),
        ("x" * 12001, "report-too-large"),
        ("é" * 6000 + "!", "report-too-large"),
    ],
)
def test_complete_report_rejects_empty_and_oversize_utf8(tmp_path: Path, text: str, reason: str):
    job_id = _running_report(tmp_path)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest() if text else "a" * 64
    artifact = {"kind": "report", "path": "artifacts/report.md", "sha256": digest}

    with pytest.raises(grokbot_jobs.GrokbotJobError, match=rf"^{reason}$") as exc:
        grokbot_jobs.complete_report(tmp_path, job_id, "bot-a", "lease-a", artifact, text, now=NOW)

    assert exc.value.reason == reason
    if text:
        assert text not in str(exc.value)
    assert not _snapshot_path(tmp_path, job_id).exists()
    assert grokbot_jobs.get_job(tmp_path, job_id)["state"] == "running"


def test_complete_report_accepts_max_utf8_bytes(tmp_path: Path):
    job_id = _running_report(tmp_path)
    text = "é" * 6000
    completed = grokbot_jobs.complete_report(
        tmp_path, job_id, "bot-a", "lease-a", _report_artifact(text), text, now=NOW
    )

    assert completed["state"] == "completed"
    report = grokbot_jobs.read_report(tmp_path, job_id)
    assert report == {"job_id": job_id, "text": text, "bytes": 12000, "sha256": _report_digest(text)}
    assert text not in json.dumps(completed)


@pytest.mark.parametrize("artifact_kind", ["draft-pr", "branch"])
def test_complete_report_rejects_wrong_role_or_kind(tmp_path: Path, artifact_kind: str):
    job_id = _enqueue(tmp_path, artifact=artifact_kind, idempotency_key=f"request-{artifact_kind}-report")
    grokbot_jobs.claim(tmp_path, job_id, "bot-a", "lease-a", 60, now=NOW)
    grokbot_jobs.transition(tmp_path, job_id, "bot-a", "lease-a", "running", now=NOW)

    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^invalid-role$") as exc:
        grokbot_jobs.complete_report(tmp_path, job_id, "bot-a", "lease-a", _report_artifact(), REPORT_TEXT, now=NOW)

    assert exc.value.reason == "invalid-role"
    assert REPORT_TEXT not in str(exc.value)
    assert not _snapshot_path(tmp_path, job_id).exists()


def test_complete_report_shares_lease_state_cancel_and_artifact_rules(tmp_path: Path):
    job_id = _enqueue(tmp_path, artifact="report")
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^invalid-state$"):
        grokbot_jobs.complete_report(tmp_path, job_id, "bot-a", "lease-a", _report_artifact(), REPORT_TEXT, now=NOW)
    grokbot_jobs.claim(tmp_path, job_id, "bot-a", "lease-a", 30, now=NOW)
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^invalid-transition$"):
        grokbot_jobs.complete_report(tmp_path, job_id, "bot-a", "lease-a", _report_artifact(), REPORT_TEXT, now=NOW)
    grokbot_jobs.transition(tmp_path, job_id, "bot-a", "lease-a", "running", now=NOW)
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^invalid-artifact$"):
        grokbot_jobs.complete_report(
            tmp_path,
            job_id,
            "bot-a",
            "lease-a",
            {"kind": "report", "path": "/report.md", "sha256": _report_digest()},
            REPORT_TEXT,
            now=NOW,
        )
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^lease-conflict$"):
        grokbot_jobs.complete_report(tmp_path, job_id, "bot-b", "lease-a", _report_artifact(), REPORT_TEXT, now=NOW)
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^lease-expired$"):
        grokbot_jobs.complete_report(
            tmp_path,
            job_id,
            "bot-a",
            "lease-a",
            _report_artifact(),
            REPORT_TEXT,
            now=NOW + timedelta(seconds=30),
        )
    grokbot_jobs.cancel(tmp_path, job_id, now=NOW)
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^cancel-requested$"):
        grokbot_jobs.complete_report(tmp_path, job_id, "bot-a", "lease-a", _report_artifact(), REPORT_TEXT, now=NOW)
    assert not _snapshot_path(tmp_path, job_id).exists()


def test_read_report_rejects_missing_legacy_snapshot(tmp_path: Path):
    job_id = _running_report(tmp_path)
    grokbot_jobs.transition(tmp_path, job_id, "bot-a", "lease-a", "completed", artifact=_report_artifact(), now=NOW)

    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^report-missing$") as exc:
        grokbot_jobs.read_report(tmp_path, job_id)

    assert exc.value.reason == "report-missing"
    assert REPORT_TEXT not in str(exc.value)
    assert not _snapshot_path(tmp_path, job_id).exists()
    _assert_surfaces_omit_report_text(tmp_path, job_id)


def test_read_report_rejects_symlink_snapshot(tmp_path: Path):
    job_id = _running_report(tmp_path)
    grokbot_jobs.complete_report(tmp_path, job_id, "bot-a", "lease-a", _report_artifact(), REPORT_TEXT, now=NOW)
    snapshot = _snapshot_path(tmp_path, job_id)
    destination = tmp_path / "outside-report.md"
    destination.write_text(REPORT_TEXT)
    snapshot.unlink()
    snapshot.symlink_to(destination)

    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^unsafe-storage$"):
        grokbot_jobs.read_report(tmp_path, job_id)


def test_read_report_rejects_oversize_invalid_utf8_and_digest_mismatch(tmp_path: Path):
    oversize_job = _running_report(tmp_path, idempotency_key="request-oversize-read")
    grokbot_jobs.complete_report(tmp_path, oversize_job, "bot-a", "lease-a", _report_artifact(), REPORT_TEXT, now=NOW)
    _snapshot_path(tmp_path, oversize_job).write_bytes(b"x" * 12001)
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^report-too-large$"):
        grokbot_jobs.read_report(tmp_path, oversize_job)

    utf8_job = _running_report(tmp_path, idempotency_key="request-utf8-read")
    grokbot_jobs.complete_report(tmp_path, utf8_job, "bot-a", "lease-a", _report_artifact(), REPORT_TEXT, now=NOW)
    _snapshot_path(tmp_path, utf8_job).write_bytes(b"\xff")
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^invalid-report$") as exc:
        grokbot_jobs.read_report(tmp_path, utf8_job)
    assert "PRIVATE_SCOUT_FINDING_TOKEN_alpha" not in str(exc.value)

    digest_job = _running_report(tmp_path, idempotency_key="request-digest-read")
    grokbot_jobs.complete_report(tmp_path, digest_job, "bot-a", "lease-a", _report_artifact(), REPORT_TEXT, now=NOW)
    _snapshot_path(tmp_path, digest_job).write_text("tampered snapshot", encoding="utf-8")
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^digest-mismatch$"):
        grokbot_jobs.read_report(tmp_path, digest_job)


def test_read_report_rejects_wrong_role_kind_and_incomplete_jobs(tmp_path: Path):
    draft_job = _enqueue(tmp_path, artifact="draft-pr", idempotency_key="request-draft-read")
    grokbot_jobs.claim(tmp_path, draft_job, "bot-a", "lease-a", 60, now=NOW)
    grokbot_jobs.transition(tmp_path, draft_job, "bot-a", "lease-a", "running", now=NOW)
    grokbot_jobs.transition(
        tmp_path,
        draft_job,
        "bot-a",
        "lease-a",
        "completed",
        artifact={
            "kind": "draft-pr",
            "url": "https://github.com/example/brigade/pull/9",
            "branch": "grokbot/read",
        },
        now=NOW,
    )
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^invalid-role$"):
        grokbot_jobs.read_report(tmp_path, draft_job)

    queued = _enqueue(tmp_path, artifact="report", idempotency_key="request-queued-read")
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^invalid-state$"):
        grokbot_jobs.read_report(tmp_path, queued)


def test_read_report_rejects_corrupted_completed_record(tmp_path: Path):
    job_id = _running_report(tmp_path)
    grokbot_jobs.complete_report(tmp_path, job_id, "bot-a", "lease-a", _report_artifact(), REPORT_TEXT, now=NOW)
    path = tmp_path / ".brigade" / "cloud" / "grokbot" / "jobs" / f"{job_id}.json"
    raw = json.loads(path.read_text())
    raw["item_revision"] = 0
    path.write_text(json.dumps(raw))

    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^corrupt-storage$"):
        grokbot_jobs.read_report(tmp_path, job_id)


def test_metadata_only_report_completion_stays_compatible(tmp_path: Path):
    job_id = _running_report(tmp_path)
    completed = grokbot_jobs.transition(
        tmp_path, job_id, "bot-a", "lease-a", "completed", artifact=_report_artifact(), now=NOW
    )

    assert completed["state"] == "completed"
    assert completed["artifact"] == {"kind": "report"}
    assert not _snapshot_path(tmp_path, job_id).exists()
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^report-missing$"):
        grokbot_jobs.read_report(tmp_path, job_id)
    _assert_surfaces_omit_report_text(tmp_path, job_id)
