"""Private, handle-first Grok Bot queue contract tests."""

from __future__ import annotations

import hashlib
import json
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from multiprocessing import Pipe, Process
from pathlib import Path

import pytest

from brigade import fleet_client_grokbot, grokbot_jobs
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


def test_grokbot_jobs_expiry_imports_before_grokbot_jobs():
    """Regression for the circular import introduced by PR #1387."""
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import brigade.grokbot_jobs_expiry; import brigade.grokbot_jobs;",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


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


def _leave_orphan_report_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, idempotency_key: str) -> str:
    job_id = _running_report(tmp_path, idempotency_key=idempotency_key)
    original_replace = grokbot_jobs.os.replace

    def fail_job_replace(source, destination, *args, **kwargs):
        dest = destination if isinstance(destination, str) else str(destination)
        if dest.endswith(".json"):
            raise OSError("injected job write failure")
        return original_replace(source, destination, *args, **kwargs)

    with monkeypatch.context() as patched:
        patched.setattr(grokbot_jobs.os, "replace", fail_job_replace)
        with pytest.raises(grokbot_jobs.GrokbotJobError, match="^unsafe-storage$"):
            grokbot_jobs.complete_report(tmp_path, job_id, "bot-a", "lease-a", _report_artifact(), REPORT_TEXT, now=NOW)
    assert _snapshot_path(tmp_path, job_id).is_file()
    assert grokbot_jobs.get_job(tmp_path, job_id, now=NOW)["state"] == "running"
    return job_id


def _job_json(tmp_path: Path, job_id: str) -> str:
    return (tmp_path / ".brigade" / "cloud" / "grokbot" / "jobs" / f"{job_id}.json").read_text()


def _assert_surfaces_omit_report_text(tmp_path: Path, job_id: str, text: str = REPORT_TEXT) -> None:
    dumped_status = json.dumps(grokbot_jobs.status(tmp_path, job_id, now=NOW), sort_keys=True)
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

    job = grokbot_jobs.get_job(tmp_path, handle["job_id"], now=NOW)
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

    result = grokbot_jobs.status(tmp_path, now=NOW)

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


def test_status_expires_a_job_past_its_timeout_without_an_operator_sweep(tmp_path: Path):
    """#1353: a read is a deadline check, on the hub and on local storage alike."""
    job_id = _enqueue(tmp_path)
    late = NOW + timedelta(seconds=901)

    assert grokbot_jobs.status(tmp_path, job_id, now=late)["state"] == "expired"
    assert grokbot_jobs.status(tmp_path)["jobs"][0]["state"] == "expired"


def test_status_reads_each_job_file_once(tmp_path: Path, monkeypatch):
    """All jobs are expired and projected in a single traversal."""
    first = _enqueue(tmp_path)
    second_id = grokbot_jobs.enqueue(tmp_path, _spec(), "request-2", now=NOW)["job_id"]

    original_read = grokbot_jobs._read_json_file
    reads: list[str] = []

    def _counting_read(directory, name, *, missing_ok=False):
        reads.append(name)
        return original_read(directory, name, missing_ok=missing_ok)

    monkeypatch.setattr(grokbot_jobs, "_read_json_file", _counting_read)
    result = grokbot_jobs.status(tmp_path, now=NOW)

    assert {job["job_id"] for job in result["jobs"]} == {first, second_id}
    assert len(reads) == 2
    assert len(set(reads)) == 2


def test_status_leaves_a_job_inside_its_timeout_queued(tmp_path: Path):
    job_id = _enqueue(tmp_path)

    assert grokbot_jobs.status(tmp_path, job_id, now=NOW + timedelta(seconds=899))["state"] == "queued"


def test_status_expires_a_running_job_past_its_own_deadline(tmp_path: Path):
    job_id = _enqueue(tmp_path)
    grokbot_jobs.claim(tmp_path, job_id, "bot-a", "lease-a", 30, now=NOW)
    grokbot_jobs.transition(tmp_path, job_id, "bot-a", "lease-a", "running", now=NOW)

    assert grokbot_jobs.status(tmp_path, job_id, now=NOW + timedelta(seconds=901))["state"] == "expired"


def test_a_read_does_not_end_a_job_whose_lease_alone_lapsed(tmp_path: Path):
    """#1383 composed with #1353: a dead lease is not a dead job."""
    job_id = _enqueue(tmp_path)
    grokbot_jobs.claim(tmp_path, job_id, "bot-a", "lease-a", 30, now=NOW)
    lapsed = NOW + timedelta(seconds=31)

    assert grokbot_jobs.status(tmp_path, job_id, now=lapsed)["state"] == "claimed"
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^lease-expired$"):
        grokbot_jobs.renew(tmp_path, job_id, "bot-a", "lease-a", 60, now=lapsed)
    assert grokbot_jobs.get_job(tmp_path, job_id, now=lapsed)["state"] == "claimed"


def test_a_lease_call_past_the_job_deadline_expires_and_still_says_lease_expired(tmp_path: Path):
    """The row is terminalized, but the holder hears the reason it can act on."""
    job_id = _enqueue(tmp_path)
    grokbot_jobs.claim(tmp_path, job_id, "bot-a", "lease-a", 300, now=NOW)
    late = NOW + timedelta(seconds=901)

    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^lease-expired$"):
        grokbot_jobs.transition(tmp_path, job_id, "bot-a", "lease-a", "failed", now=late)
    assert grokbot_jobs.get_job(tmp_path, job_id, now=late)["state"] == "expired"


def test_expire_still_terminalizes_a_job_whose_lease_alone_lapsed(tmp_path: Path):
    """An explicit expire keeps its own contract: a lost lease ends the job."""
    job_id = _enqueue(tmp_path)
    grokbot_jobs.claim(tmp_path, job_id, "bot-a", "lease-a", 30, now=NOW)

    assert grokbot_jobs.expire(tmp_path, job_id, now=NOW + timedelta(seconds=31))["state"] == "expired"


def test_claim_past_the_deadline_expires_the_job_and_stays_job_expired(tmp_path: Path):
    job_id = _enqueue(tmp_path)
    late = NOW + timedelta(seconds=901)

    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^job-expired$"):
        grokbot_jobs.claim(tmp_path, job_id, "bot-a", "lease-a", 60, now=late)
    assert grokbot_jobs.get_job(tmp_path, job_id, now=late)["state"] == "expired"
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^job-expired$"):
        grokbot_jobs.claim(tmp_path, job_id, "bot-b", "lease-b", 60, now=late)


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


def test_cli_queue_lifecycle_uses_generic_listener_identity_file(tmp_path: Path, monkeypatch, capsys):
    token = "direct-queue-listener-token"
    token_file = tmp_path / "listener.hub-token"
    token_file.write_text(token + "\n", encoding="utf-8")
    token_file.chmod(0o600)
    monkeypatch.setenv("BRIGADE_GROKBOT_HUB_TOKEN_FILE", str(token_file))
    seen: list[str | None] = []
    monkeypatch.setattr(
        grokbot_jobs,
        "status",
        lambda *_args, **_kwargs: seen.append(fleet_client_grokbot.current_listener_token()) or {"jobs": []},
    )

    assert _run_grokbot(tmp_path, "status", "--json") == 0
    assert json.loads(capsys.readouterr().out) == {"jobs": []}
    assert seen == [token]


def test_cli_queue_lifecycle_keeps_host_identity_without_generic_listener_file(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.delenv("BRIGADE_GROKBOT_HUB_TOKEN_FILE", raising=False)
    seen: list[str | None] = []
    monkeypatch.setattr(
        grokbot_jobs,
        "status",
        lambda *_args, **_kwargs: seen.append(fleet_client_grokbot.current_listener_token()) or {"jobs": []},
    )

    assert _run_grokbot(tmp_path, "status", "--json") == 0
    assert json.loads(capsys.readouterr().out) == {"jobs": []}
    assert seen == [None]


@pytest.mark.parametrize("kind", ("relative", "symlink", "directory", "unsafe-mode"))
def test_cli_queue_lifecycle_rejects_unsafe_generic_listener_file_before_hub_call(
    tmp_path: Path, monkeypatch, capsys, kind: str
):
    token = "direct-queue-token-must-not-leak"
    configured_path = tmp_path / "listener.hub-token"
    if kind == "relative":
        configured_text = "listener.hub-token"
    elif kind == "symlink":
        target = tmp_path / "real.hub-token"
        target.write_text(token + "\n", encoding="utf-8")
        target.chmod(0o600)
        configured_path.symlink_to(target)
        configured_text = str(configured_path)
    elif kind == "directory":
        configured_path.mkdir()
        configured_text = str(configured_path)
    else:
        configured_path.write_text(token + "\n", encoding="utf-8")
        configured_path.chmod(0o640)
        configured_text = str(configured_path)
    monkeypatch.setenv("BRIGADE_GROKBOT_HUB_TOKEN_FILE", configured_text)
    hub_calls: list[object] = []
    monkeypatch.setattr(grokbot_jobs, "status", lambda *_args, **_kwargs: hub_calls.append(object()) or {"jobs": []})

    assert _run_grokbot(tmp_path, "status") == 2
    captured = capsys.readouterr()
    rendered = captured.out + captured.err
    assert hub_calls == []
    assert token not in rendered
    assert configured_text not in rendered


def test_cli_generic_listener_identity_does_not_override_hub_role_policy(tmp_path: Path, monkeypatch, capsys):
    token = "role-checked-listener-token"
    token_file = tmp_path / "listener.hub-token"
    token_file.write_text(token + "\n", encoding="utf-8")
    token_file.chmod(0o600)
    monkeypatch.setenv("BRIGADE_GROKBOT_HUB_TOKEN_FILE", str(token_file))
    monkeypatch.setattr(grokbot_jobs, "hub_authority", lambda _target: True)
    seen: list[str | None] = []
    monkeypatch.setattr(
        fleet_client_grokbot,
        "list_jobs",
        lambda **_kwargs: (
            seen.append(fleet_client_grokbot.current_listener_token())
            or fleet_client_grokbot.GrokbotHubDecision(False, "role-forbidden")
        ),
    )

    assert _run_grokbot(tmp_path, "status") == 2
    captured = capsys.readouterr()
    assert seen == [token]
    assert captured.out == ""
    assert captured.err.strip() == "error: role-forbidden"


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


def test_report_snapshot_is_private_and_verified(tmp_path: Path):
    spec = _spec()
    spec["role"] = "repository-scout"
    spec["artifact"] = {"kind": "report"}
    job_id = grokbot_jobs.enqueue(tmp_path, spec, "report-1", now=NOW)["job_id"]
    grokbot_jobs.claim(tmp_path, job_id, "bot-a", "lease-a", 300, now=NOW)
    grokbot_jobs.transition(tmp_path, job_id, "bot-a", "lease-a", "running", now=NOW + timedelta(seconds=1))
    report = "Private scout findings."
    digest = __import__("hashlib").sha256(report.encode()).hexdigest()
    completed = grokbot_jobs.complete_report(
        tmp_path,
        job_id,
        "bot-a",
        "lease-a",
        {"kind": "report", "path": "docs/scout.md", "sha256": digest},
        report,
        now=NOW + timedelta(seconds=2),
    )
    assert completed["state"] == "completed"
    assert report not in json.dumps(completed)
    retrieved = grokbot_jobs.read_report(tmp_path, job_id)
    assert retrieved["text"] == report
    assert retrieved["sha256"] == digest
    artifact = tmp_path / ".brigade" / "cloud" / "grokbot" / "artifacts" / f"{job_id}.md"
    assert artifact.is_file()
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600


def test_hub_snapshot_conflict_preserves_existing_bytes(tmp_path: Path, monkeypatch):
    from brigade import fleet_client_grokbot

    job_id = "grokbot-" + hashlib.sha256(b"shared-key").hexdigest()[:24]
    granted = fleet_client_grokbot.GrokbotHubDecision(
        True,
        "ok",
        job={"job_id": job_id, "state": "queued"},
        idempotent=False,
    )
    repeated_ok = fleet_client_grokbot.GrokbotHubDecision(
        True,
        "ok",
        job={"job_id": job_id, "state": "queued"},
        idempotent=True,
    )
    decisions = [granted, repeated_ok]
    monkeypatch.setattr(fleet_client_grokbot, "hub_configured", lambda: True)
    monkeypatch.setattr(fleet_client_grokbot, "enqueue", lambda **kwargs: decisions.pop(0))

    first = grokbot_jobs.enqueue(tmp_path, _spec(), "shared-key", now=NOW)
    snapshot = tmp_path / ".brigade" / "cloud" / "grokbot" / "snapshots" / f"{first['job_id']}.json"
    original = snapshot.read_bytes()
    changed = _spec()
    changed["label"] = "Different task"
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^idempotency-conflict$"):
        grokbot_jobs.enqueue(tmp_path, changed, "shared-key", now=NOW)
    assert snapshot.read_bytes() == original
    repeated = grokbot_jobs.enqueue(tmp_path, _spec(), "shared-key", now=NOW)
    assert repeated["job_id"] == first["job_id"]
    assert snapshot.read_bytes() == original


def _hub_snapshot_dir(tmp_path: Path) -> Path:
    return tmp_path / ".brigade" / "cloud" / "grokbot" / "snapshots"


def _derived_hub_job_id(key: str) -> str:
    return f"grokbot-{grokbot_jobs._idempotency_key_hash(key).removeprefix('sha256:')[:24]}"


def _hub_listing(*job_ids: str) -> fleet_client_grokbot.GrokbotHubDecision:
    jobs = [{"job_id": job_id, "state": "queued"} for job_id in job_ids]
    return fleet_client_grokbot.GrokbotHubDecision(True, "ok", jobs=jobs)


def _hub_enqueue_probe(
    monkeypatch: pytest.MonkeyPatch,
    *,
    outcome,
    listing,
) -> list[dict[str, object]]:
    """Wire one hub-authority enqueue and record every listing probe it makes."""
    probes: list[dict[str, object]] = []
    monkeypatch.setattr(fleet_client_grokbot, "hub_configured", lambda: True)

    def enqueue(**_fields: object):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def probe(**fields: object):
        probes.append(fields)
        if isinstance(listing, BaseException):
            raise listing
        return listing

    monkeypatch.setattr(fleet_client_grokbot, "enqueue", enqueue)
    monkeypatch.setattr(fleet_client_grokbot, "list_jobs", probe)
    return probes


def test_a_hub_enqueue_that_times_out_after_the_commit_keeps_the_snapshot(tmp_path: Path, monkeypatch):
    job_id = _derived_hub_job_id("in-doubt")
    probes = _hub_enqueue_probe(
        monkeypatch,
        outcome=TimeoutError("client gave up after the hub committed"),
        listing=_hub_listing(job_id),
    )

    with pytest.raises(TimeoutError):
        grokbot_jobs.enqueue(tmp_path, _spec(), "in-doubt", now=NOW)

    # An enqueueing actor holds `list`, not `status`, and the hub scopes the
    # listing to its queue, so the probe passes no role filter.
    assert probes == [{"include_all": True}]
    assert (_hub_snapshot_dir(tmp_path) / f"{job_id}.json").is_file()


def test_a_hub_enqueue_failure_keeps_the_snapshot_when_the_hub_cannot_answer(tmp_path: Path, monkeypatch):
    job_id = _derived_hub_job_id("unavailable")
    probes = _hub_enqueue_probe(
        monkeypatch,
        outcome=fleet_client_grokbot.GrokbotHubDecision(False, "refused"),
        listing=fleet_client_grokbot.GrokbotHubDecision(False, "hub-unavailable"),
    )

    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^refused$"):
        grokbot_jobs.enqueue(tmp_path, _spec(), "unavailable", now=NOW)

    assert probes == [{"include_all": True}]
    assert (_hub_snapshot_dir(tmp_path) / f"{job_id}.json").is_file()


def test_a_hub_enqueue_failure_keeps_the_snapshot_when_the_listing_is_refused(tmp_path: Path, monkeypatch):
    job_id = _derived_hub_job_id("auth-refused")
    probes = _hub_enqueue_probe(
        monkeypatch,
        outcome=fleet_client_grokbot.GrokbotHubDecision(False, "refused"),
        listing=fleet_client_grokbot.GrokbotHubDecision(False, "auth-failed"),
    )

    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^refused$"):
        grokbot_jobs.enqueue(tmp_path, _spec(), "auth-refused", now=NOW)

    assert probes == [{"include_all": True}]
    assert (_hub_snapshot_dir(tmp_path) / f"{job_id}.json").is_file()


def test_a_hub_enqueue_failure_keeps_the_snapshot_when_the_probe_raises(tmp_path: Path, monkeypatch):
    job_id = _derived_hub_job_id("probe-raises")
    probes = _hub_enqueue_probe(
        monkeypatch,
        outcome=TimeoutError("client gave up after the hub committed"),
        listing=RuntimeError("the listing itself failed"),
    )

    with pytest.raises(TimeoutError):
        grokbot_jobs.enqueue(tmp_path, _spec(), "probe-raises", now=NOW)

    assert probes == [{"include_all": True}]
    assert (_hub_snapshot_dir(tmp_path) / f"{job_id}.json").is_file()


@pytest.mark.parametrize("reason", ["idempotency-conflict", "operation-mismatch"])
def test_a_duplicate_key_refusal_keeps_the_snapshot_without_probing_the_hub(tmp_path: Path, monkeypatch, reason: str):
    job_id = _derived_hub_job_id(f"duplicate-{reason}")
    probes = _hub_enqueue_probe(
        monkeypatch,
        outcome=fleet_client_grokbot.GrokbotHubDecision(False, reason),
        listing=_hub_listing(),
    )

    with pytest.raises(grokbot_jobs.GrokbotJobError, match=f"^{reason}$"):
        grokbot_jobs.enqueue(tmp_path, _spec(), f"duplicate-{reason}", now=NOW)

    assert probes == []
    assert (_hub_snapshot_dir(tmp_path) / f"{job_id}.json").is_file()


def test_a_plain_refusal_removes_the_snapshot_when_the_hub_holds_no_such_job(tmp_path: Path, monkeypatch):
    job_id = _derived_hub_job_id("refused")
    probes = _hub_enqueue_probe(
        monkeypatch,
        outcome=fleet_client_grokbot.GrokbotHubDecision(False, "invalid-request"),
        listing=_hub_listing("grokbot-someone-elses-job"),
    )

    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^invalid-request$"):
        grokbot_jobs.enqueue(tmp_path, _spec(), "refused", now=NOW)

    assert probes == [{"include_all": True}]
    assert not (_hub_snapshot_dir(tmp_path) / f"{job_id}.json").exists()


def test_a_refusal_keeps_the_snapshot_when_the_hub_still_holds_the_job(tmp_path: Path, monkeypatch):
    job_id = _derived_hub_job_id("held")
    probes = _hub_enqueue_probe(
        monkeypatch,
        outcome=fleet_client_grokbot.GrokbotHubDecision(False, "refused"),
        listing=_hub_listing("grokbot-someone-elses-job", job_id),
    )

    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^refused$"):
        grokbot_jobs.enqueue(tmp_path, _spec(), "held", now=NOW)

    assert probes == [{"include_all": True}]
    assert (_hub_snapshot_dir(tmp_path) / f"{job_id}.json").is_file()


def test_configured_hub_enqueue_is_fail_closed_and_local_only_without_hub(tmp_path: Path, monkeypatch):
    handle = grokbot_jobs.enqueue(tmp_path, _spec(), "local-only", now=NOW)
    assert handle["state"] == "queued"
    assert (tmp_path / ".brigade" / "cloud" / "grokbot" / "jobs" / f"{handle['job_id']}.json").is_file()

    from brigade import fleet_client_grokbot

    monkeypatch.setattr(fleet_client_grokbot, "hub_configured", lambda: True)
    monkeypatch.setattr(
        fleet_client_grokbot,
        "enqueue",
        lambda **kwargs: fleet_client_grokbot.GrokbotHubDecision(False, "hub-unavailable"),
    )
    before = list((tmp_path / ".brigade" / "cloud" / "grokbot" / "jobs").glob("grokbot-*.json"))
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^legacy-active-needs-reconcile$"):
        grokbot_jobs.enqueue(tmp_path, _spec(), "hub-closed", now=NOW)
    after = list((tmp_path / ".brigade" / "cloud" / "grokbot" / "jobs").glob("grokbot-*.json"))
    assert len(after) == len(before)

    empty = tmp_path / "empty-hub"
    empty.mkdir()
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^hub-unavailable$"):
        grokbot_jobs.enqueue(empty, _spec(), "hub-closed-empty", now=NOW)
    assert not list((empty / ".brigade" / "cloud" / "grokbot" / "jobs").glob("grokbot-*.json"))


def test_authority_marker_rejects_active_legacy_and_fails_closed(tmp_path: Path, monkeypatch):
    handle = grokbot_jobs.enqueue(tmp_path, _spec(), "legacy-active", now=NOW)
    assert handle["state"] == "queued"
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^legacy-active-needs-reconcile$"):
        grokbot_jobs.admit_hub_authority(tmp_path)
    assert grokbot_jobs.authority_marker_present(tmp_path) is False

    grokbot_jobs.cancel(tmp_path, handle["job_id"], now=NOW + timedelta(seconds=1))
    admitted = grokbot_jobs.admit_hub_authority(tmp_path)
    assert admitted["admitted"] is True
    assert grokbot_jobs.authority_marker_present(tmp_path) is True
    again = grokbot_jobs.admit_hub_authority(tmp_path)
    assert again["idempotent"] is True

    from brigade import fleet_client_grokbot

    monkeypatch.setattr(fleet_client_grokbot, "hub_configured", lambda: False)
    assert grokbot_jobs.hub_authority(tmp_path) is True
    monkeypatch.setattr(
        fleet_client_grokbot,
        "status",
        lambda *args, **kwargs: fleet_client_grokbot.GrokbotHubDecision(False, "hub-unavailable"),
    )
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^hub-unavailable$"):
        grokbot_jobs.get_job(tmp_path, handle["job_id"])
    local = tmp_path / ".brigade" / "cloud" / "grokbot" / "jobs" / f"{handle['job_id']}.json"
    record = json.loads(local.read_text())
    assert record["state"] == "canceled"


def test_hub_authority_tracker_rows_do_not_use_local_queue_cards(tmp_path: Path, monkeypatch):
    local = grokbot_jobs.enqueue(tmp_path, _spec(), "local-card", now=NOW)
    from brigade import fleet_client_grokbot

    monkeypatch.setattr(grokbot_jobs, "authority_marker_present", lambda _target: True)
    monkeypatch.setattr(
        fleet_client_grokbot,
        "list_jobs",
        lambda **kwargs: fleet_client_grokbot.GrokbotHubDecision(
            True,
            "ok",
            jobs=[
                {
                    "job_id": "grokbot-" + "c" * 24,
                    "label": "hub job",
                    "role": "implementation-worker",
                    "repository": "example/brigade",
                    "task_digest": "c" * 64,
                    "state": "queued",
                    "created_at": "2026-08-23T12:00:00Z",
                    "updated_at": "2026-08-23T12:00:00Z",
                    "queued_at": "2026-08-23T12:00:00Z",
                    "timeout_seconds": 900,
                    "item_revision": 1,
                    "artifact_kind": "draft-pr",
                }
            ],
        ),
    )
    rows = grokbot_jobs.tracker_rows(tmp_path)
    assert [row["job_id"] for row in rows] == ["grokbot-" + "c" * 24]
    assert local["job_id"] not in {row["job_id"] for row in rows}


def test_cli_grokbot_validates_target_before_queue_access(tmp_path: Path, capsys):
    not_directory = tmp_path / "not-a-directory"
    not_directory.write_text("x")

    assert _run_grokbot(not_directory, "status") == 2
    assert capsys.readouterr().err.strip() == f"error: --target is not a directory: {not_directory.resolve()}"


def test_safe_artifact_metadata_includes_report_size():
    job = {"job_id": "grokbot-" + "a" * 24, "artifact_kind": "report", "private_snapshot_id": "snap"}
    metadata = grokbot_jobs._safe_artifact_metadata(
        {"kind": "report", "sha256": "b" * 64, "size": 12},
        job,
    )
    assert metadata["kind"] == "report"
    assert metadata["digest"] == "b" * 64
    assert metadata["size"] == 12
    assert metadata["private_snapshot_id"] == "snap"


def test_hub_validation_refusal_is_not_hub_unavailable(monkeypatch):
    from brigade import fleet_client_grokbot

    monkeypatch.setattr(fleet_client_grokbot, "load_fleet_config", lambda: {"hub_url": "http://hub", "token": "node"})
    monkeypatch.setattr(fleet_client_grokbot._client, "resolve_node_id", lambda: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    monkeypatch.setattr(fleet_client_grokbot._client, "_node_id_is_claimable", lambda _node: True)
    monkeypatch.setattr(
        fleet_client_grokbot,
        "_post_grokbot_blocking",
        lambda *args, **kwargs: (400, {"error": "grokbot artifact size is invalid"}),
    )
    monkeypatch.setattr(
        fleet_client_grokbot._client,
        "_run_with_deadline",
        lambda operation, timeout: operation(),
    )
    decision = fleet_client_grokbot.complete(
        "grokbot-" + "a" * 24,
        lease_id="lease-a",
        artifact={"kind": "report", "digest": "b" * 64},
        expected_item_revision=1,
        operation_id="op-complete-1",
    )
    assert decision.granted is False
    assert decision.reason != "hub-unavailable"
    assert decision.reason in {"invalid-artifact", "invalid-request", "refused"}


def test_hub_complete_report_accepts_claimed_and_cleans_failed_orphan_only(tmp_path: Path, monkeypatch):
    from brigade import fleet_client_grokbot

    spec = grokbot_jobs._validate_spec(
        {
            **_spec(),
            "role": "repository-scout",
            "artifact": {"kind": "report"},
        }
    )
    job_id = "grokbot-" + hashlib.sha256(b"claim-complete").hexdigest()[:24]
    report = "Private scout findings."
    digest = hashlib.sha256(report.encode()).hexdigest()
    artifact_path = tmp_path / ".brigade" / "cloud" / "grokbot" / "artifacts" / f"{job_id}.md"
    grokbot_jobs._store_task_snapshot(tmp_path, job_id, spec, grokbot_jobs._idempotency_key_hash("claim-complete"))
    monkeypatch.setattr(grokbot_jobs, "hub_authority", lambda _target=None: True)
    claimed = {
        "job_id": job_id,
        "state": "claimed",
        "role": "repository-scout",
        "artifact_kind": "report",
        "item_revision": 2,
        "lease_generation": 1,
        "private_snapshot_id": job_id,
    }
    monkeypatch.setattr(
        fleet_client_grokbot,
        "status",
        lambda *args, **kwargs: fleet_client_grokbot.GrokbotHubDecision(True, "ok", job=claimed),
    )
    monkeypatch.setattr(
        fleet_client_grokbot,
        "complete",
        lambda *args, **kwargs: fleet_client_grokbot.GrokbotHubDecision(False, "invalid-state"),
    )
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^invalid-state$"):
        grokbot_jobs.complete_report(
            tmp_path,
            job_id,
            "grokbot-repository-scout",
            "lease-a",
            {"kind": "report", "path": "docs/scout.md", "sha256": digest},
            report,
        )
    assert not artifact_path.exists()

    preexisting = b"verified report already stored"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_bytes(preexisting)
    artifact_path.chmod(0o600)
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^invalid-state$"):
        grokbot_jobs.complete_report(
            tmp_path,
            job_id,
            "grokbot-repository-scout",
            "lease-a",
            {"kind": "report", "path": "docs/scout.md", "sha256": digest},
            report,
        )
    assert artifact_path.read_bytes() == preexisting

    completed = {**claimed, "state": "completed", "artifact_digest": digest, "artifact_size": len(report.encode())}
    monkeypatch.setattr(
        fleet_client_grokbot,
        "complete",
        lambda *args, **kwargs: fleet_client_grokbot.GrokbotHubDecision(True, "ok", job=completed),
    )
    artifact_path.unlink()
    result = grokbot_jobs.complete_report(
        tmp_path,
        job_id,
        "grokbot-repository-scout",
        "lease-a",
        {"kind": "report", "path": "docs/scout.md", "sha256": digest},
        report,
    )
    assert result["state"] == "completed"
    assert artifact_path.read_bytes() == report.encode()


def test_hub_report_snapshot_requires_digest(tmp_path: Path, monkeypatch):
    from brigade import fleet_client_grokbot

    job_id = "grokbot-" + "d" * 24
    report = b"Private scout findings."
    artifact = tmp_path / ".brigade" / "cloud" / "grokbot" / "artifacts" / f"{job_id}.md"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(report)
    artifact.chmod(0o600)
    monkeypatch.setattr(grokbot_jobs, "hub_authority", lambda _target=None: True)
    monkeypatch.setattr(
        fleet_client_grokbot,
        "status",
        lambda *args, **kwargs: fleet_client_grokbot.GrokbotHubDecision(
            True,
            "ok",
            job={"job_id": job_id, "state": "completed", "artifact_kind": "report"},
        ),
    )
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^missing-digest$"):
        grokbot_jobs.read_report(tmp_path, job_id)
    assert artifact.read_bytes() == report


def test_hub_outage_projects_degraded_needs_you_marker(tmp_path: Path, monkeypatch):
    from brigade import cloud_tracker, fleet_client_grokbot

    monkeypatch.setattr(grokbot_jobs, "hub_authority", lambda _target=None: True)
    monkeypatch.setattr(
        fleet_client_grokbot,
        "list_jobs",
        lambda **kwargs: fleet_client_grokbot.GrokbotHubDecision(False, "hub-unavailable"),
    )
    rows = grokbot_jobs.tracker_rows(tmp_path)
    assert rows
    dumped = json.dumps(rows)
    assert "PRIVATE" not in dumped
    assert any(row.get("classification") == "needs-investigation" or row.get("degraded") for row in rows)
    status = cloud_tracker.status_payload(tmp_path, now=NOW, github={"branches": [], "prs": []})
    assert status["sources"]["grokbot-cloud"].get("degraded") is True or status["counts"]["needs-investigation"] >= 1
    assert "PRIVATE" not in json.dumps(status)
    assert "token" not in json.dumps(status["sources"]["grokbot-cloud"]).lower()


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
    assert grokbot_jobs.get_job(tmp_path, job_id, now=NOW)["state"] == "running"


def test_complete_report_rejects_digest_mismatch_without_writing(tmp_path: Path):
    job_id = _running_report(tmp_path)
    artifact = _report_artifact()
    artifact["sha256"] = "0" * 64

    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^digest-mismatch$") as exc:
        grokbot_jobs.complete_report(tmp_path, job_id, "bot-a", "lease-a", artifact, REPORT_TEXT, now=NOW)

    assert exc.value.reason == "digest-mismatch"
    assert REPORT_TEXT not in str(exc.value)
    assert not _snapshot_path(tmp_path, job_id).exists()
    assert grokbot_jobs.get_job(tmp_path, job_id, now=NOW)["state"] == "running"
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
    assert grokbot_jobs.get_job(tmp_path, job_id, now=NOW)["state"] == "running"


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


def test_metadata_only_complete_does_not_expose_orphan_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    job_id = _leave_orphan_report_snapshot(tmp_path, monkeypatch, idempotency_key="request-orphan-completed")
    completed = grokbot_jobs.transition(
        tmp_path, job_id, "bot-a", "lease-a", "completed", artifact=_report_artifact(), now=NOW
    )

    assert completed["state"] == "completed"
    assert not _snapshot_path(tmp_path, job_id).exists()
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^report-missing$") as exc:
        grokbot_jobs.read_report(tmp_path, job_id)
    assert exc.value.reason == "report-missing"
    assert REPORT_TEXT not in str(exc.value)
    _assert_surfaces_omit_report_text(tmp_path, job_id)


@pytest.mark.parametrize("terminal", ["failed", "expired", "canceled"])
def test_noncompleted_terminal_cleans_orphan_report_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, terminal: str
):
    job_id = _leave_orphan_report_snapshot(tmp_path, monkeypatch, idempotency_key=f"request-orphan-{terminal}")
    snapshot = _snapshot_path(tmp_path, job_id)

    if terminal == "failed":
        result = grokbot_jobs.transition(tmp_path, job_id, "bot-a", "lease-a", "failed", now=NOW)
    elif terminal == "expired":
        result = grokbot_jobs.expire(tmp_path, job_id, now=NOW + timedelta(seconds=60))
    else:
        grokbot_jobs.cancel(tmp_path, job_id, now=NOW)
        result = grokbot_jobs.acknowledge_cancel(tmp_path, job_id, "bot-a", "lease-a", now=NOW)

    assert result["state"] == terminal
    assert not snapshot.exists()
    snapshot.write_text(REPORT_TEXT, encoding="utf-8")
    grokbot_jobs.expire(tmp_path, job_id, now=NOW + timedelta(seconds=60))
    grokbot_jobs.cancel(tmp_path, job_id, now=NOW)
    assert not snapshot.exists()


def test_completed_report_snapshot_survives_later_terminal_attempts(tmp_path: Path):
    job_id = _running_report(tmp_path)
    grokbot_jobs.complete_report(tmp_path, job_id, "bot-a", "lease-a", _report_artifact(), REPORT_TEXT, now=NOW)
    snapshot = _snapshot_path(tmp_path, job_id)
    encoded = snapshot.read_bytes()

    assert grokbot_jobs.expire(tmp_path, job_id, now=NOW + timedelta(seconds=60))["state"] == "completed"
    assert grokbot_jobs.cancel(tmp_path, job_id, now=NOW)["state"] == "completed"
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^terminal-state$"):
        grokbot_jobs.transition(tmp_path, job_id, "bot-a", "lease-a", "failed", now=NOW)

    assert snapshot.read_bytes() == encoded
    assert grokbot_jobs.read_report(tmp_path, job_id)["text"] == REPORT_TEXT


def test_orphan_cleanup_failure_does_not_terminalize_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    job_id = _leave_orphan_report_snapshot(tmp_path, monkeypatch, idempotency_key="request-orphan-unlink-fails")
    real_unlink = grokbot_jobs.os.unlink

    def fail_snapshot_unlink(path, *args, **kwargs):
        if path == f"{job_id}.md":
            raise OSError("injected artifact unlink failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(grokbot_jobs.os, "unlink", fail_snapshot_unlink)
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^unsafe-storage$"):
        grokbot_jobs.transition(tmp_path, job_id, "bot-a", "lease-a", "failed", now=NOW)

    assert grokbot_jobs.get_job(tmp_path, job_id, now=NOW)["state"] == "running"
    assert _snapshot_path(tmp_path, job_id).is_file()


def test_read_report_keeps_reading_after_legal_short_os_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    job_id = _running_report(tmp_path)
    grokbot_jobs.complete_report(tmp_path, job_id, "bot-a", "lease-a", _report_artifact(), REPORT_TEXT, now=NOW)
    real_read = grokbot_jobs.os.read

    def short_read(fd: int, n: int, *args: object, **kwargs: object) -> bytes:
        return real_read(fd, 1 if n > 1 else n, *args, **kwargs)

    monkeypatch.setattr(grokbot_jobs.os, "read", short_read)
    report = grokbot_jobs.read_report(tmp_path, job_id)
    encoded = REPORT_TEXT.encode("utf-8")
    assert report == {
        "job_id": job_id,
        "text": REPORT_TEXT,
        "bytes": len(encoded),
        "sha256": _report_digest(),
    }
