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


@pytest.mark.parametrize("label", ["builder-2\njob forged state=completed", "Builder-2", "a" * 33, "builder 2", 7])
def test_hub_projection_drops_unsafe_worker_labels(label):
    from brigade.grokbot_jobs_hub import _hub_projection_job

    row = {"job_id": "grokbot-" + "d" * 24, "state": "claimed", "worker_label": label}
    assert "worker_label" not in _hub_projection_job(row)
    assert _hub_projection_job({**row, "worker_label": "builder-2"})["worker_label"] == "builder-2"


@pytest.mark.parametrize("marker", ["valid", "corrupt", "unreadable"])
def test_authority_marker_fails_closed(tmp_path, monkeypatch, marker):
    from brigade import fleet_client_grokbot

    monkeypatch.setattr(fleet_client_grokbot, "hub_configured", lambda: False)
    assert grokbot_jobs.hub_authority(tmp_path) is False
    path = grokbot_jobs.authority_marker_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"schema": grokbot_jobs.AUTHORITY_SCHEMA}) if marker == "valid" else "{", encoding="utf-8"
    )
    if marker == "unreadable":
        original = type(path).read_text

        def read_text(self, *args, **kwargs):
            if self == path:
                raise PermissionError("fixture unreadable marker")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(type(path), "read_text", read_text)
    assert grokbot_jobs.authority_marker_present(tmp_path) is True
    assert grokbot_jobs.hub_authority(tmp_path) is True


@pytest.mark.parametrize(
    "claimed,value", [(True, ""), (True, "x" * 513), (True, "acme/repo\n"), (True, 5), (False, "acme/repo")]
)
def test_stored_claim_target_is_validated(tmp_path, monkeypatch, claimed, value):
    monkeypatch.setattr(grokbot_jobs, "hub_authority", lambda target: False)
    job_id = seed.enqueue(tmp_path, _grokbot_spec(), "fixture", now=NOW)["job_id"]
    if claimed:
        seed.claim(tmp_path, job_id, "bot-a", "lease-a", 60, now=NOW)
    path = tmp_path / ".brigade/cloud/grokbot/jobs" / (job_id + ".json")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["claim_target"] = value
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^corrupt-storage$"):
        grokbot_jobs.tracker_rows(tmp_path)


@pytest.mark.parametrize(
    "path",
    [
        ("artifact", "accessToken"),
        ("artifact", "nested", "deployment_secret"),
        ("unknown_header",),
        ("artifact", "nested", 0, "api_token"),
    ],
)
def test_stored_spec_recursively_rejects_sensitive_keys(tmp_path, monkeypatch, path):
    spec = _grokbot_spec()
    cursor = spec
    for index, key in enumerate(path[:-1]):
        if isinstance(key, int):
            cursor.append({})
            cursor = cursor[key]
        else:
            cursor = cursor.setdefault(key, [] if isinstance(path[index + 1], int) else {})
    cursor[path[-1]] = "value"
    monkeypatch.setattr(grokbot_jobs, "hub_authority", lambda target: False)
    seed.enqueue(tmp_path, spec, "fixture", now=NOW)
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^forbidden-key$"):
        grokbot_jobs.tracker_rows(tmp_path)


@pytest.mark.parametrize(
    "value", [grokbot_jobs.QUEUE_TTL_SECONDS_MIN - 1, grokbot_jobs.QUEUE_TTL_SECONDS_MAX + 1, "3600"]
)
def test_stored_spec_refuses_invalid_queue_lifetime(tmp_path, monkeypatch, value):
    monkeypatch.setattr(grokbot_jobs, "hub_authority", lambda target: False)
    seed.enqueue(tmp_path, {**_grokbot_spec(), "queue_ttl_seconds": value}, "fixture", now=NOW)
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^invalid-queue-ttl$"):
        grokbot_jobs.tracker_rows(tmp_path)


@pytest.mark.parametrize("value", [grokbot_jobs.QUEUE_TTL_SECONDS_MIN, grokbot_jobs.QUEUE_TTL_SECONDS_MAX])
def test_stored_spec_accepts_queue_lifetime_bounds(tmp_path, monkeypatch, value):
    monkeypatch.setattr(grokbot_jobs, "hub_authority", lambda target: False)
    seed.enqueue(tmp_path, {**_grokbot_spec(), "queue_ttl_seconds": value}, "fixture", now=NOW)
    assert len(grokbot_jobs.tracker_rows(tmp_path)) == 1


def test_stored_spec_refuses_unknown_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(grokbot_jobs, "hub_authority", lambda target: False)
    seed.enqueue(tmp_path, {**_grokbot_spec(), "unexpected": True}, "fixture", now=NOW)
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^unknown-key$"):
        grokbot_jobs.tracker_rows(tmp_path)


@pytest.mark.parametrize("case", ["oversized", "extra-fields", "refusal", "known-refusal", "artifact-refusal"])
def test_retained_list_bounds_hub_responses(monkeypatch, case):
    import io
    from brigade import fleet_client_grokbot as client

    requested = []

    class Response(io.BytesIO):
        status = 409 if case in {"refusal", "known-refusal"} else 400 if case == "artifact-refusal" else 200

        def read(self, size=-1):
            requested.append(size)
            return super().read(size)

    payload = {"jobs": [{"job_id": "fixture", "state": "queued", "unexpected": "private"}]}
    if case == "refusal":
        payload = {"error": "private detail\n" * 1000}
    elif case == "known-refusal":
        payload = {"error": "lease-conflict"}
    elif case == "artifact-refusal":
        payload = {"error": "private artifact detail"}
    raw = b" " * (client.MAX_GROKBOT_RESPONSE_BYTES + 1) if case == "oversized" else json.dumps(payload).encode("utf-8")
    monkeypatch.setattr(
        client, "load_fleet_config", lambda: {"hub_url": "https://hub.example.test", "token": "fixture"}
    )
    monkeypatch.setattr(client._client, "_hub_open", lambda *args, **kwargs: Response(raw))
    with client.listener_identity("fixture"):
        decision = client.list_jobs()
    assert requested == [client.MAX_GROKBOT_RESPONSE_BYTES + 1]
    if case == "extra-fields":
        assert decision.granted
        assert decision.jobs == [{"job_id": "fixture", "state": "queued"}]
    else:
        assert not decision.granted
        assert (
            decision.reason
            == {
                "oversized": "hub-unavailable",
                "refusal": "refused",
                "known-refusal": "lease-conflict",
                "artifact-refusal": "invalid-artifact",
            }[case]
        )
