"""Private, handle-first Grok Bot queue contract tests."""

from __future__ import annotations

import stat
from datetime import datetime, timezone
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
    assert job["revision"] == 1
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
            "revision",
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
