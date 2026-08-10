"""Tests for worker event field classification and fail-closed scrubbing (#592)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brigade import run_audit, worker_events

FIXTURES = Path(__file__).resolve().parents[1] / "src" / "brigade" / "fixtures"
GOLDEN_PATH = FIXTURES / "worker-events-appserver.v1.golden.json"


def _golden():
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


def _case(name: str) -> dict:
    return next(c for c in _golden()["cases"] if c["name"] == name)


def test_media_types_and_artifact_classes_are_distinct():
    media = worker_events.media_types()
    classes = worker_events.artifact_classes()
    assert media["raw"] != media["scrubbed"]
    assert classes["raw"] != classes["scrubbed"]
    assert classes["unclassified"] != classes["scrubbed"]
    assert media["raw"].startswith("application/vnd.brigade.")
    assert media["scrubbed"].startswith("application/vnd.brigade.")


def test_classification_matrix_covers_recorded_app_server_methods():
    matrix = worker_events.classification_matrix()
    assert matrix["transport"] == "codex-app-server"
    assert matrix["version"] == worker_events.CLASSIFICATION_MATRIX_VERSION
    methods = set(matrix["methods"])
    assert methods == {"turn/started", "turn/completed", "item/started", "item/completed"}
    assert "auto_declined_params" in matrix
    assert matrix["auto_declined_methods"] == ["item/commandExecution/requestApproval"]
    for item_type, fields in matrix["item_types"].items():
        assert "id" in fields and fields["id"] == worker_events.FIELD_PUBLIC
        assert "type" in fields and fields["type"] == worker_events.FIELD_PUBLIC
        for classification in fields.values():
            assert classification in worker_events.FIELD_CLASSES
        assert item_type  # non-empty


def test_golden_scrubbed_projections_match_and_omit_sensitive_material():
    for case in _golden()["cases"]:
        if "expect_error_category" in case:
            with pytest.raises(worker_events.WorkerEventError) as excinfo:
                worker_events.scrub_event(case["raw"])
            assert excinfo.value.category == case["expect_error_category"]
            assert len(excinfo.value.diagnostic) <= worker_events.MAX_DIAGNOSTIC_LEN
            continue
        scrubbed = worker_events.scrub_event(case["raw"])
        assert scrubbed == case["scrubbed"]
        assert scrubbed["source_digest"] == case["source_digest"]
        assert scrubbed["schema"] == worker_events.SCHEMA
        assert scrubbed["schema_version"] == worker_events.SCHEMA_VERSION
        assert scrubbed["classification_status"] == worker_events.STATUS_SCRUBBED
        assert not worker_events.scrubbed_projection_omits_sensitive_material(scrubbed)
        assert not worker_events.validate_scrubbed_event(scrubbed)


def test_scrubbed_stream_preserves_order_ids_methods_and_source_digest():
    stream = _golden()["stream"]
    projection = worker_events.scrub_stream_lines(stream["raw_events"])
    assert projection == stream["scrubbed"]
    assert projection["event_count"] == len(stream["raw_events"])
    assert [event["method"] for event in projection["events"]] == [raw["method"] for raw in stream["raw_events"]]
    for raw, scrubbed in zip(stream["raw_events"], projection["events"], strict=True):
        assert scrubbed["source_digest"] == worker_events.source_digest_for_event(raw)
        params = scrubbed["params"]
        raw_params = raw["params"]
        if "threadId" in raw_params:
            assert params["threadId"] == raw_params["threadId"]
        if "turnId" in raw_params:
            assert params["turnId"] == raw_params["turnId"]
        if "item" in raw_params:
            assert params["item"]["id"] == raw_params["item"]["id"]
            assert params["item"]["type"] == raw_params["item"]["type"]
        if "turn" in raw_params:
            assert params["turn"]["id"] == raw_params["turn"]["id"]
            assert params["turn"]["status"] == raw_params["turn"]["status"]


def test_adversarial_secrets_absent_from_scrubbed_auto_declined_case():
    case = _case("auto_declined_with_secrets")
    scrubbed = worker_events.scrub_event(case["raw"])
    blob = json.dumps(scrubbed)
    for needle in (
        "sk-live-EXAMPLESECRETVALUE99",
        "Authorization",
        "Bearer",
        "/home/example-user",
        "OPENAI_API_KEY",
        "raw user prompt",
        "session=abc",
        "cat /home",
    ):
        assert needle not in blob
    assert scrubbed["params"]["threadId"] == "thread-demo-1"
    assert scrubbed["params"]["itemId"] == "cmd-1"
    assert scrubbed["params"]["availableDecisions"] == ["accept", "decline"]
    assert scrubbed["method"].endswith("#auto-declined")


def test_provider_error_bodies_and_agent_text_absent():
    case = _case("turn_completed_with_provider_error")
    scrubbed = worker_events.scrub_event(case["raw"])
    blob = json.dumps(scrubbed)
    assert "upstream 401" not in blob
    assert "Bearer" not in blob
    assert "internal provider dump" not in blob
    assert "error" not in scrubbed["params"]["turn"]
    assert scrubbed["params"]["turn"]["items"][0] == {"id": "msg-1", "type": "agentMessage"}


def test_unknown_method_and_field_diagnostics_are_bounded():
    for name in ("unknown_method_fails_closed", "unknown_field_fails_closed", "unknown_item_type_fails_closed"):
        case = _case(name)
        with pytest.raises(worker_events.WorkerEventError) as excinfo:
            worker_events.scrub_event(case["raw"])
        assert len(str(excinfo.value)) <= worker_events.MAX_DIAGNOSTIC_LEN
        assert case["expect_error_category"] == excinfo.value.category


def test_legacy_raw_stream_is_unclassified_until_scrubbed(tmp_path: Path):
    path = tmp_path / "coder.jsonl"
    path.write_text(json.dumps(_case("turn_started_public")["raw"]) + "\n", encoding="utf-8")
    info = worker_events.inspect_stream_file(path)
    assert info.status == worker_events.STATUS_UNCLASSIFIED
    assert info.artifact_class == worker_events.UNCLASSIFIED_ARTIFACT_CLASS
    assert info.media_type == worker_events.RAW_MEDIA_TYPE
    assert info.diagnostic is not None

    scrubbed_path = tmp_path / "coder.scrubbed.json"
    scrubbed_path.write_text(json.dumps(worker_events.scrub_stream_file(path)), encoding="utf-8")
    scrubbed_info = worker_events.inspect_stream_file(scrubbed_path)
    assert scrubbed_info.status == worker_events.STATUS_SCRUBBED
    assert scrubbed_info.artifact_class == worker_events.SCRUBBED_ARTIFACT_CLASS
    assert scrubbed_info.media_type == worker_events.SCRUBBED_MEDIA_TYPE


def test_audit_and_replay_consumers_reject_raw_unless_local_only(tmp_path: Path):
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    raw_path = events_dir / "coder.jsonl"
    raw_path.write_text(json.dumps(_case("item_started_command")["raw"]) + "\n", encoding="utf-8")

    with pytest.raises(worker_events.WorkerEventPolicyError):
        worker_events.load_stream_for_consumer(raw_path, consumer="run_audit")
    with pytest.raises(worker_events.WorkerEventPolicyError):
        worker_events.assert_audit_rejects_raw_streams(events_dir)
    with pytest.raises(worker_events.WorkerEventPolicyError):
        run_audit.reject_raw_worker_stream_evidence(tmp_path)

    local = worker_events.load_stream_for_consumer(
        raw_path,
        consumer="run_resume",
        policy=worker_events.POLICY_LOCAL_ONLY,
    )
    assert isinstance(local, list)
    assert local[0]["params"]["threadId"] == "thread-demo-1"

    infos = run_audit.reject_raw_worker_stream_evidence(
        tmp_path,
        policy=worker_events.POLICY_LOCAL_ONLY,
    )
    assert len(infos) == 1
    assert infos[0].status == worker_events.STATUS_UNCLASSIFIED


def test_scrubbed_document_is_admissible_for_audit_consumer(tmp_path: Path):
    raw_path = tmp_path / "coder.jsonl"
    raw_path.write_text(json.dumps(_case("turn_started_public")["raw"]) + "\n", encoding="utf-8")
    doc = worker_events.scrub_stream_file(raw_path)
    scrubbed_path = tmp_path / "coder.scrubbed.json"
    scrubbed_path.write_text(json.dumps(doc), encoding="utf-8")
    loaded = worker_events.load_stream_for_consumer(scrubbed_path, consumer="run_audit")
    assert loaded["schema"] == worker_events.STREAM_SCHEMA
    assert loaded["media_type"] == worker_events.SCRUBBED_MEDIA_TYPE
    assert loaded["artifact_class"] == worker_events.SCRUBBED_ARTIFACT_CLASS


def test_reject_raw_worker_stream_evidence_unknown_policy(tmp_path: Path):
    with pytest.raises(run_audit.AuditError):
        run_audit.reject_raw_worker_stream_evidence(tmp_path, policy="export-everything")


def _nested_public_list(depth: int) -> object:
    value: object = "accept"
    for _ in range(depth):
        value = [value]
    return value


def _write_fake_scrubbed_document(tmp_path: Path, *, mutate) -> Path:
    raw_path = tmp_path / "coder.jsonl"
    raw_path.write_text(json.dumps(_case("turn_started_public")["raw"]) + "\n", encoding="utf-8")
    doc = worker_events.scrub_stream_file(raw_path)
    mutate(doc)
    scrubbed_path = tmp_path / "coder.scrubbed.json"
    scrubbed_path.write_text(json.dumps(doc), encoding="utf-8")
    return scrubbed_path


def test_self_declared_scrubbed_document_with_embedded_secrets_is_not_admissible(tmp_path: Path):
    def inject_private_prompt(document: dict) -> None:
        event = document["events"][0]
        event["params"]["prompt"] = "leaked private prompt with sk-live-EXAMPLESECRETVALUE99"

    path = _write_fake_scrubbed_document(tmp_path, mutate=inject_private_prompt)

    info = worker_events.inspect_stream_file(path)
    assert info.status == worker_events.STATUS_UNCLASSIFIED
    assert info.artifact_class == worker_events.UNCLASSIFIED_ARTIFACT_CLASS
    assert info.diagnostic is not None

    with pytest.raises(worker_events.WorkerEventError):
        worker_events.load_stream_for_consumer(path, consumer="run_audit")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("threadId", "sk-live-EXAMPLESECRETVALUE99"),
        ("threadId", "thread\ninject"),
        ("threadId", "t" * 300),
        ("method", "turn/started\n"),
        ("method", "Bearer sk-live-EXAMPLESECRETVALUE99"),
    ],
)
def test_public_identifier_and_method_strings_fail_closed_during_projection(field: str, value: str):
    raw = json.loads(json.dumps(_case("turn_started_public")["raw"]))
    if field == "method":
        raw["method"] = value
    else:
        raw["params"][field] = value
    with pytest.raises(worker_events.WorkerEventError) as excinfo:
        worker_events.scrub_event(raw)
    assert len(str(excinfo.value)) <= worker_events.MAX_DIAGNOSTIC_LEN


def test_auto_declined_method_shape_still_scrubs_after_public_string_validation():
    case = _case("auto_declined_with_secrets")
    scrubbed = worker_events.scrub_event(case["raw"])
    assert scrubbed["method"].endswith("#auto-declined")
    assert not worker_events.validate_scrubbed_event(scrubbed)


def test_single_scrubbed_event_document_is_supported(tmp_path: Path):
    scrubbed = _case("turn_started_public")["scrubbed"]
    path = tmp_path / "turn.scrubbed.json"
    path.write_text(json.dumps(scrubbed), encoding="utf-8")

    info = worker_events.inspect_stream_file(path)
    assert info.status == worker_events.STATUS_SCRUBBED
    assert info.artifact_class == worker_events.SCRUBBED_ARTIFACT_CLASS
    assert info.media_type == worker_events.SCRUBBED_MEDIA_TYPE
    assert info.event_count == 1
    assert info.diagnostic is None

    loaded = worker_events.load_stream_for_consumer(path, consumer="run_audit")
    assert loaded == scrubbed
    assert loaded["schema"] == worker_events.SCHEMA
    assert not worker_events.validate_scrubbed_event(loaded)


def test_arbitrary_auto_declined_method_prefix_fails_closed():
    case = _case("auto_declined_with_secrets")
    raw = json.loads(json.dumps(case["raw"]))
    raw["method"] = "unknown/request#auto-declined"
    with pytest.raises(worker_events.WorkerEventError) as excinfo:
        worker_events.scrub_event(raw)
    assert excinfo.value.category == "unknown-method"
    assert len(str(excinfo.value)) <= worker_events.MAX_DIAGNOSTIC_LEN

    valid = worker_events.scrub_event(case["raw"])
    assert valid["method"] == "item/commandExecution/requestApproval#auto-declined"
    assert not worker_events.validate_scrubbed_event(valid)


def test_deeply_nested_public_list_in_scrubbed_stream_is_bounded(tmp_path: Path):
    raw_path = tmp_path / "coder.jsonl"
    raw_path.write_text(json.dumps(_case("auto_declined_with_secrets")["raw"]) + "\n", encoding="utf-8")
    doc = worker_events.scrub_stream_file(raw_path)
    doc["events"][0]["params"]["availableDecisions"] = _nested_public_list(
        worker_events.MAX_PUBLIC_LIST_DEPTH + 1
    )
    path = tmp_path / "coder.scrubbed.json"
    path.write_text(json.dumps(doc), encoding="utf-8")

    info = worker_events.inspect_stream_file(path)
    assert info.status == worker_events.STATUS_UNCLASSIFIED
    assert info.artifact_class == worker_events.UNCLASSIFIED_ARTIFACT_CLASS
    assert info.diagnostic is not None
    assert len(info.diagnostic) <= worker_events.MAX_DIAGNOSTIC_LEN

    with pytest.raises(worker_events.WorkerEventError) as excinfo:
        worker_events.load_stream_for_consumer(path, consumer="run_audit")
    assert len(str(excinfo.value)) <= worker_events.MAX_DIAGNOSTIC_LEN


def test_scrubbed_event_with_invalid_jsonrpc_is_not_admissible(tmp_path: Path):
    scrubbed = json.loads(json.dumps(_case("turn_started_public")["scrubbed"]))
    scrubbed["jsonrpc"] = "1.0"

    errors = worker_events.validate_scrubbed_event(scrubbed)
    assert errors
    assert "jsonrpc" in errors[0].lower()
    assert len(errors[0]) <= worker_events.MAX_DIAGNOSTIC_LEN

    path = tmp_path / "bad-jsonrpc.scrubbed.json"
    path.write_text(json.dumps(scrubbed), encoding="utf-8")
    info = worker_events.inspect_stream_file(path)
    assert info.status == worker_events.STATUS_UNCLASSIFIED
    assert info.diagnostic is not None

    with pytest.raises(worker_events.WorkerEventError):
        worker_events.load_stream_for_consumer(path, consumer="run_audit")


def test_forged_scrubbed_event_unknown_params_field_is_rejected(tmp_path: Path):
    scrubbed = json.loads(json.dumps(_case("turn_started_public")["scrubbed"]))
    scrubbed["params"]["harmlessExtra"] = "looks-fine"

    errors = worker_events.validate_scrubbed_event(scrubbed)
    assert errors
    assert len(errors[0]) <= worker_events.MAX_DIAGNOSTIC_LEN

    path = tmp_path / "forged.scrubbed.json"
    path.write_text(json.dumps(scrubbed), encoding="utf-8")
    info = worker_events.inspect_stream_file(path)
    assert info.status == worker_events.STATUS_UNCLASSIFIED
    assert info.diagnostic is not None

    with pytest.raises(worker_events.WorkerEventError):
        worker_events.load_stream_for_consumer(path, consumer="run_audit")
