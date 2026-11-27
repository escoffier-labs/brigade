"""Retained projection readers preserve hub response and authority checks."""

import json

import pytest

from brigade import grokbot_jobs


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
