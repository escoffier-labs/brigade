# content-guard: allow bearer-token file
"""Durable Grok Bot findings relay: opaque outbox and unchanged report_event."""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from brigade import cli, fleet_client, grokbot_findings, grokbot_findings_relay
from brigade.grokbot_findings_relay import relay_apply, relay_preview

RELAY_HARNESS = "grokbot"
RELAY_SEAT = "findings-relay"

PRIVATE_TITLE = "PRIVATE_RELAY_TITLE_TOKEN"
PRIVATE_BODY = (
    "Bearer ghp_exampleNotARealToken0000000000000001\n"
    "ssh root@192.0.2.10\n"
    "command: curl http://example.invalid/secret\n"
    "credential: hunter2-example\n"
)
PATHLIKE_PRODUCER = "192.0.2.88"
PATHLIKE_FINDING_ID = "etc:shadow.wazuh"
PATHLIKE_SOURCE_REF = "/var/ossec/logs/alerts/alerts.json"
FORBIDDEN_RESULT_KEYS = {
    "title",
    "body",
    "address",
    "command",
    "credential",
    "payload",
    "token",
    "password",
    "secret",
    "producer",
    "finding_id",
    "source_ref",
    "source_digest",
    "events",
    "findings",
    "revision",
}
EVENT_KEYS = {"run_id", "seat", "harness", "state", "ts", "sequence", "digest"}
APPLY_RESULT_KEYS = {"eligible", "known", "created", "skipped", "limit", "pending", "reported", "relays"}
PREVIEW_RESULT_KEYS = {"eligible", "known", "created", "limit", "relays"}
NOW = datetime(2026, 8, 28, 16, 0, tzinfo=timezone.utc)
LEAK_TOKENS = (
    PRIVATE_TITLE,
    "ghp_exampleNotARealToken0000000000000001",
    "192.0.2.10",
    "curl http://example.invalid/secret",
    "hunter2-example",
    PATHLIKE_PRODUCER,
    PATHLIKE_FINDING_ID,
    PATHLIKE_SOURCE_REF,
)


def _entry(
    *,
    producer: str = "wazuh",
    finding_id: str = "agent-disconnected",
    revision: str = "1",
    title: str = PRIVATE_TITLE,
    body: str = PRIVATE_BODY,
    source_ref: str = "wazuh:agent-disconnected",
    severity: str = "high",
) -> dict[str, str]:
    return {
        "producer": producer,
        "finding_id": finding_id,
        "revision": revision,
        "observed_at": "2026-08-28T16:00:00Z",
        "severity": severity,
        "title": title,
        "body": body,
        "source_ref": source_ref,
        "source_digest": grokbot_findings.content_digest("source", body),
        "content_digest": grokbot_findings.content_digest(title, body),
    }


def _pathlike_entry() -> dict[str, str]:
    return _entry(
        producer=PATHLIKE_PRODUCER,
        finding_id=PATHLIKE_FINDING_ID,
        source_ref=PATHLIKE_SOURCE_REF,
    )


def _relay_id(entry: dict[str, str]) -> str:
    payload = f"{entry['producer']}\0{entry['finding_id']}\0{entry['revision']}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _result_keys(value: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        keys.update(value)
        for item in value.values():
            keys.update(_result_keys(item))
    elif isinstance(value, list):
        for item in value:
            keys.update(_result_keys(item))
    return keys


def _review_inbox(owner: Path) -> Path:
    return owner / "memory" / "handoff-inbox"


def _outbox_dir(queue: Path) -> Path:
    return queue / ".brigade" / "cloud" / "grokbot" / "outbox"


def _findings_dir(queue: Path) -> Path:
    return queue / ".brigade" / "cloud" / "grokbot" / "findings"


def _outbox_path(queue: Path, entry: dict[str, str]) -> Path:
    return _outbox_dir(queue) / f"{_relay_id(entry)}.json"


def _marker_path(queue: Path, entry: dict[str, str]) -> Path:
    return _findings_dir(queue) / grokbot_findings.marker_filename(
        entry["producer"], entry["finding_id"], entry["revision"]
    )


def _isolate_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("BRIGADE_HOME", str(home))
    monkeypatch.delenv("BRIGADE_FLEET_HUB_URL", raising=False)
    monkeypatch.delenv("BRIGADE_FLEET_TOKEN", raising=False)
    return home


def _capture_events(monkeypatch) -> list[dict[str, object]]:
    captured: list[dict[str, object]] = []

    def _capture(event: dict[str, object], **_kwargs: object) -> bool:
        captured.append(copy.deepcopy(event))
        return True

    monkeypatch.setattr(fleet_client, "report_event", _capture)
    return captured


def _queue_and_owner(tmp_path: Path) -> tuple[Path, Path]:
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    return queue, owner


def _assert_opaque(text: str) -> None:
    for token in LEAK_TOKENS:
        assert token not in text


def _assert_opaque_result(result: dict[str, object]) -> None:
    assert FORBIDDEN_RESULT_KEYS.isdisjoint(_result_keys(result))
    _assert_opaque(json.dumps(result, sort_keys=True))
    for item in result.get("relays", []):
        assert isinstance(item, str)
        assert grokbot_findings.grokbot_jobs.LOWER_HEX_64_RE.fullmatch(item)


def _expected_event(entry: dict[str, str], *, ts: str = "2026-08-28T16:00:00Z", sequence: int = 1) -> dict[str, object]:
    relay_id = _relay_id(entry)
    canonical = {
        "harness": RELAY_HARNESS,
        "run_id": relay_id,
        "seat": RELAY_SEAT,
        "sequence": sequence,
        "state": f"finding.{entry['severity']}",
        "ts": ts,
    }
    digest = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {**canonical, "digest": digest}


def test_report_event_signature_stays_unchanged():
    parameters = inspect.signature(fleet_client.report_event).parameters
    assert list(parameters) == ["event", "base_path", "node_id"]
    assert parameters["base_path"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["node_id"].kind is inspect.Parameter.KEYWORD_ONLY


def test_preview_projects_only_relay_ids_and_writes_nothing(tmp_path: Path, monkeypatch):
    home = _isolate_home(tmp_path, monkeypatch)
    queue, owner = _queue_and_owner(tmp_path)
    record = _pathlike_entry()

    result = relay_preview([record], queue, owner)

    assert set(result) == PREVIEW_RESULT_KEYS
    assert result["eligible"] == 1
    assert result["known"] == 0
    assert result["created"] == 0
    assert result["relays"] == [_relay_id(record)]
    _assert_opaque_result(result)
    assert not _review_inbox(owner).exists()
    assert not _findings_dir(queue).exists()
    assert not _outbox_dir(queue).exists()
    assert not (home / "fleet-spool").exists()


def test_apply_writes_full_untrusted_body_and_calls_unchanged_report_event(tmp_path: Path, monkeypatch):
    home = _isolate_home(tmp_path, monkeypatch)
    external_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        fleet_client,
        "report_external_event",
        lambda **kwargs: external_calls.append(dict(kwargs)) or True,
    )
    captured = _capture_events(monkeypatch)
    queue, owner = _queue_and_owner(tmp_path)
    record = _entry()

    result = relay_apply([record], queue, owner, now=NOW)

    assert set(result) == APPLY_RESULT_KEYS
    assert result["created"] == 1
    assert result["reported"] == 1
    assert result["pending"] == 0
    assert result["relays"] == [_relay_id(record)]
    _assert_opaque_result(result)
    drafts = list(_review_inbox(owner).glob("*.md"))
    assert len(drafts) == 1
    text = drafts[0].read_text(encoding="utf-8")
    assert PRIVATE_TITLE in text
    assert "ghp_exampleNotARealToken0000000000000001" in text
    assert external_calls == []
    assert captured == [_expected_event(record)]
    event = captured[0]
    assert set(event) == EVENT_KEYS
    assert event["run_id"] == _relay_id(record)
    assert event["seat"] == RELAY_SEAT == "findings-relay"
    assert event["harness"] == RELAY_HARNESS == "grokbot"
    assert event["state"] == "finding.high"
    _assert_opaque(json.dumps(event, sort_keys=True))
    outbox = json.loads(_outbox_path(queue, record).read_text(encoding="utf-8"))
    assert outbox["status"] == "reported"
    assert outbox["event"] == event
    assert _outbox_path(queue, record).stat().st_mode & 0o777 == 0o600
    assert _outbox_dir(queue).stat().st_mode & 0o777 == 0o700
    assert not (home / "fleet-spool").exists()


def test_report_false_keeps_pending_and_replay_retries_identical_event(tmp_path: Path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    captured: list[dict[str, object]] = []
    answers = [False, True]

    def _report(event: dict[str, object], **_kwargs: object) -> bool:
        captured.append(copy.deepcopy(event))
        return answers.pop(0)

    monkeypatch.setattr(fleet_client, "report_event", _report)
    queue, owner = _queue_and_owner(tmp_path)
    record = _entry()

    first = relay_apply([record], queue, owner, now=NOW)
    second = relay_apply([record], queue, owner, now=datetime(2026, 8, 28, 16, 1, tzinfo=timezone.utc))

    assert first["created"] == 1
    assert first["reported"] == 0
    assert first["pending"] == 1
    assert second["created"] == 0
    assert second["known"] == 1
    assert second["reported"] == 1
    assert second["pending"] == 0
    assert captured[0] == captured[1] == _expected_event(record)
    outbox = json.loads(_outbox_path(queue, record).read_text(encoding="utf-8"))
    assert outbox["status"] == "reported"
    assert outbox["event"] == captured[0]


def test_ready_outbox_retries_without_original_finding_in_next_batch(tmp_path: Path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    captured: list[dict[str, object]] = []
    answers = [False, True]

    def _report(event: dict[str, object], **_kwargs: object) -> bool:
        captured.append(copy.deepcopy(event))
        return answers.pop(0)

    monkeypatch.setattr(fleet_client, "report_event", _report)
    queue, owner = _queue_and_owner(tmp_path)
    record = _entry()

    first = relay_apply([record], queue, owner, now=NOW)
    second = relay_apply([], queue, owner, now=datetime(2026, 8, 28, 16, 1, tzinfo=timezone.utc))

    assert first["pending"] == 1
    assert second["eligible"] == 0
    assert second["created"] == 0
    assert second["reported"] == 1
    assert captured[0] == captured[1] == _expected_event(record)
    assert json.loads(_outbox_path(queue, record).read_text(encoding="utf-8"))["status"] == "reported"


def test_pending_outbox_rejects_changed_entry_with_same_identity(tmp_path: Path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    queue, owner = _queue_and_owner(tmp_path)
    record = _entry()
    monkeypatch.setattr(
        grokbot_findings,
        "_deliver_one",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("interrupted after outbox")),
    )
    with pytest.raises(RuntimeError, match="interrupted after outbox"):
        relay_apply([record], queue, owner, now=NOW)
    changed = _entry(title="changed", body="changed body")

    with pytest.raises(grokbot_findings.FindingsError) as exc:
        relay_apply([changed], queue, owner, now=NOW)

    assert exc.value.reason == "outbox-conflict"


def test_concurrent_bounded_relays_spool_the_entries_they_deliver(tmp_path: Path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    captured = _capture_events(monkeypatch)
    queue, owner = _queue_and_owner(tmp_path)
    records = [_entry(finding_id="first"), _entry(finding_id="second")]
    entered = threading.Event()
    release = threading.Event()
    real_deliver = grokbot_findings._deliver_one
    calls = 0
    calls_lock = threading.Lock()

    def _blocked_first(*args: object, **kwargs: object):
        nonlocal calls
        with calls_lock:
            calls += 1
            current = calls
        if current == 1:
            entered.set()
            assert release.wait(timeout=5)
        return real_deliver(*args, **kwargs)

    monkeypatch.setattr(grokbot_findings, "_deliver_one", _blocked_first)
    results: list[dict[str, object]] = []
    errors: list[BaseException] = []

    def _run() -> None:
        try:
            results.append(relay_apply(records, queue, owner, limit=1, now=NOW))
        except BaseException as exc:  # pragma: no cover - assertion aid.
            errors.append(exc)

    first = threading.Thread(target=_run)
    second = threading.Thread(target=_run)
    first.start()
    assert entered.wait(timeout=5)
    second.start()
    time.sleep(0.05)
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not errors
    assert not first.is_alive() and not second.is_alive()
    assert sum(result["created"] for result in results) == 2
    assert {_relay_id(record) for record in records} == {event["run_id"] for event in captured}
    assert all(_outbox_path(queue, record).is_file() for record in records)


def test_existing_legacy_marker_remains_readable_after_upgrade(tmp_path: Path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    captured = _capture_events(monkeypatch)
    queue, owner = _queue_and_owner(tmp_path)
    record = _entry()
    relay_apply([record], queue, owner, now=NOW)
    marker_path = _marker_path(queue, record)
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["identity"] = {"producer": record["producer"], "finding_id": record["finding_id"]}
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    marker_path.chmod(0o600)

    result = relay_apply([record], queue, owner, now=NOW)

    assert result["known"] == 1
    assert result["created"] == 0
    assert result["reported"] == 0
    assert len(captured) == 1


def test_crash_after_outbox_before_delivery_does_not_lose_the_event(tmp_path: Path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    captured = _capture_events(monkeypatch)
    queue, owner = _queue_and_owner(tmp_path)
    record = _entry()
    real_deliver = grokbot_findings._deliver_one
    calls = {"n": 0}

    def _flaky(*args: object, **kwargs: object):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("interrupted after outbox")
        return real_deliver(*args, **kwargs)

    monkeypatch.setattr(grokbot_findings, "_deliver_one", _flaky)

    with pytest.raises(RuntimeError, match="interrupted after outbox"):
        relay_apply([record], queue, owner, now=NOW)

    assert captured == []
    assert not _review_inbox(owner).exists()
    outbox = json.loads(_outbox_path(queue, record).read_text(encoding="utf-8"))
    assert outbox["status"] == "pending"
    assert outbox["event"] == _expected_event(record)

    result = relay_apply([record], queue, owner, now=datetime(2026, 8, 28, 16, 1, tzinfo=timezone.utc))

    assert result["created"] == 1
    assert result["reported"] == 1
    assert captured == [_expected_event(record)]
    assert json.loads(_outbox_path(queue, record).read_text(encoding="utf-8"))["status"] == "reported"


def test_crash_after_delivery_before_ready_recovers_on_replay(tmp_path: Path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    captured = _capture_events(monkeypatch)
    queue, owner = _queue_and_owner(tmp_path)
    record = _entry()
    real_mark = grokbot_findings_relay._mark_outbox_status
    calls = 0

    def _crash_first(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("interrupted before ready")
        real_mark(*args, **kwargs)

    monkeypatch.setattr(grokbot_findings_relay, "_mark_outbox_status", _crash_first)

    with pytest.raises(RuntimeError, match="interrupted before ready"):
        relay_apply([record], queue, owner, now=NOW)

    assert _marker_path(queue, record).is_file()
    assert json.loads(_outbox_path(queue, record).read_text(encoding="utf-8"))["status"] == "pending"
    assert captured == []

    result = relay_apply([record], queue, owner, now=NOW)

    assert result["known"] == 1
    assert result["reported"] == 1
    assert captured == [_expected_event(record)]


def test_report_event_raise_keeps_pending_and_replay_is_duplicate_safe(tmp_path: Path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    captured: list[dict[str, object]] = []

    def _raise(event: dict[str, object], **_kwargs: object) -> bool:
        captured.append(copy.deepcopy(event))
        raise RuntimeError("spooled then raised")

    monkeypatch.setattr(fleet_client, "report_event", _raise)
    queue, owner = _queue_and_owner(tmp_path)
    record = _entry()

    with pytest.raises(RuntimeError, match="spooled then raised"):
        relay_apply([record], queue, owner, now=NOW)

    assert _review_inbox(owner).is_dir()
    assert json.loads(_outbox_path(queue, record).read_text(encoding="utf-8"))["status"] == "ready"

    monkeypatch.setattr(
        fleet_client,
        "report_event",
        lambda event, **_kwargs: captured.append(copy.deepcopy(event)) or True,
    )
    result = relay_apply([record], queue, owner, now=datetime(2026, 8, 28, 16, 1, tzinfo=timezone.utc))

    assert result["created"] == 0
    assert result["reported"] == 1
    assert captured[0] == captured[1] == _expected_event(record)


def test_duplicate_delivery_does_not_write_a_second_draft_or_event(tmp_path: Path, monkeypatch):
    home = _isolate_home(tmp_path, monkeypatch)
    captured = _capture_events(monkeypatch)
    queue, owner = _queue_and_owner(tmp_path)
    record = _entry()

    first = relay_apply([record], queue, owner, now=NOW)
    second = relay_apply([record], queue, owner, now=datetime(2026, 8, 28, 16, 1, tzinfo=timezone.utc))

    assert first["created"] == 1
    assert first["reported"] == 1
    assert second["created"] == 0
    assert second["known"] == 1
    assert second["reported"] == 0
    assert second["pending"] == 0
    assert len(captured) == 1
    assert len(list(_review_inbox(owner).glob("*.md"))) == 1
    assert not (home / "fleet-spool").exists()


def test_interrupted_draft_recovery_rewrites_the_marker_without_a_second_event(tmp_path: Path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    captured = _capture_events(monkeypatch)
    queue, owner = _queue_and_owner(tmp_path)
    record = _entry()
    relay_apply([record], queue, owner, now=NOW)
    marker = _marker_path(queue, record)
    marker.unlink()
    captured.clear()

    result = relay_apply([record], queue, owner, now=datetime(2026, 8, 28, 16, 1, tzinfo=timezone.utc))

    assert result["created"] == 0
    assert result["skipped"] == 1
    assert result["reported"] == 0
    assert captured == []
    assert marker.is_file()
    assert [path.name for path in _review_inbox(owner).glob("*.md")] == [
        grokbot_findings.draft_filename(record["producer"], record["finding_id"], record["revision"])
    ]


def test_invalid_schema_fails_closed_before_any_write(tmp_path: Path, monkeypatch):
    home = _isolate_home(tmp_path, monkeypatch)
    captured = _capture_events(monkeypatch)
    queue, owner = _queue_and_owner(tmp_path)
    invalid = {**_entry(), "schema": "brigade.grokbot.findings.v1"}

    with pytest.raises(grokbot_findings.FindingsError) as exc:
        relay_apply([invalid], queue, owner)

    assert exc.value.reason == "invalid-entry"
    assert captured == []
    assert not _review_inbox(owner).exists()
    assert not _findings_dir(queue).exists()
    assert not _outbox_dir(queue).exists()
    assert not (home / "fleet-spool").exists()


def test_bounded_batch_rejects_oversized_records_without_writing(tmp_path: Path, monkeypatch):
    home = _isolate_home(tmp_path, monkeypatch)
    captured = _capture_events(monkeypatch)
    queue, owner = _queue_and_owner(tmp_path)
    records = [_entry(finding_id=f"finding-{index}") for index in range(grokbot_findings.MAX_ENTRIES + 1)]

    with pytest.raises(grokbot_findings.FindingsError) as exc:
        relay_preview(records, queue, owner)

    assert exc.value.reason == "malformed-manifest"
    assert captured == []
    assert not _review_inbox(owner).exists()
    assert not _outbox_dir(queue).exists()
    assert not (home / "fleet-spool").exists()


def test_pathlike_handles_never_leave_owner_review_draft(tmp_path: Path, capsys, monkeypatch):
    home = _isolate_home(tmp_path, monkeypatch)
    captured = _capture_events(monkeypatch)
    queue, owner = _queue_and_owner(tmp_path)
    record = _pathlike_entry()
    manifest = _write_manifest(tmp_path / "findings.json", [record])

    result = relay_apply([record], queue, owner, now=NOW)
    assert _run_relay(queue, owner, manifest, "--apply", "--json") == 0
    stdout = capsys.readouterr().out
    payload = json.loads(stdout)
    outbox_text = _outbox_path(queue, record).read_text(encoding="utf-8")
    marker_text = _marker_path(queue, record).read_text(encoding="utf-8")
    surfaces = [
        json.dumps(captured[0], sort_keys=True),
        json.dumps(result, sort_keys=True),
        stdout,
        outbox_text,
        marker_text,
        json.dumps(payload, sort_keys=True),
    ]
    for surface in surfaces:
        _assert_opaque(surface)
        assert record["producer"] not in surface
        assert record["finding_id"] not in surface
        assert record["source_ref"] not in surface
    draft = (
        _review_inbox(owner)
        / grokbot_findings.draft_filename(record["producer"], record["finding_id"], record["revision"])
    ).read_text(encoding="utf-8")
    assert PATHLIKE_PRODUCER in draft
    assert PATHLIKE_FINDING_ID in draft
    assert PATHLIKE_SOURCE_REF in draft
    assert not (home / "fleet-spool").exists()


@pytest.mark.parametrize("status", [401, 503], ids=["http-401", "http-5xx"])
def test_hub_auth_and_server_errors_spool_without_finding_text(tmp_path: Path, monkeypatch, status: int):
    home = tmp_path / "home"
    monkeypatch.setenv("BRIGADE_HOME", str(home))
    monkeypatch.setenv("BRIGADE_FLEET_TOKEN", "test-token")

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.send_response(status)
            self.end_headers()
            self.wfile.write(b'{"error":"denied"}')

        def log_message(self, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", f"http://127.0.0.1:{server.server_address[1]}")
        queue, owner = _queue_and_owner(tmp_path)
        record = _entry()

        result = relay_apply([record], queue, owner, now=NOW)

        assert result["created"] == 1
        assert result["reported"] == 0
        assert result["pending"] == 1
        _assert_opaque_result(result)
        spool_files = list((home / "fleet-spool").glob("*.jsonl"))
        assert len(spool_files) == 1
        spool_text = spool_files[0].read_text(encoding="utf-8")
        _assert_opaque(spool_text)
        outbox = json.loads(_outbox_path(queue, record).read_text(encoding="utf-8"))
        assert outbox["status"] == "ready"
        assert outbox["event"] == _expected_event(record)
    finally:
        server.shutdown()
        server.server_close()


def _run_relay(queue: Path, owner: Path, manifest: Path, *command: str) -> int:
    return cli.main(
        [
            "run",
            "cloud",
            "grokbot",
            "relay-findings",
            "--target",
            str(queue),
            "--owner",
            str(owner),
            "--manifest",
            str(manifest),
            *command,
        ]
    )


def _write_manifest(path: Path, entries: list[dict[str, str]]) -> Path:
    path.write_text(json.dumps({"schema": grokbot_findings.FINDINGS_SCHEMA, "entries": entries}), encoding="utf-8")
    path.chmod(0o600)
    return path


def test_cli_preview_defaults_to_write_nothing_and_omits_finding_text(tmp_path: Path, capsys, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    queue, owner = _queue_and_owner(tmp_path)
    record = _pathlike_entry()
    manifest = _write_manifest(tmp_path / "findings.json", [record])

    assert _run_relay(queue, owner, manifest) == 0
    captured = capsys.readouterr()
    assert "eligible=1" in captured.out
    assert "created=0" in captured.out
    assert _relay_id(record) in captured.out
    assert record["producer"] not in captured.out
    assert record["finding_id"] not in captured.out
    assert record["source_ref"] not in captured.out
    assert PRIVATE_TITLE not in captured.out
    assert PRIVATE_BODY not in captured.out
    assert captured.err == ""
    assert not _review_inbox(owner).exists()
    assert not _outbox_dir(queue).exists()


def test_cli_apply_json_stays_free_of_finding_text(tmp_path: Path, capsys, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    _capture_events(monkeypatch)
    queue, owner = _queue_and_owner(tmp_path)
    record = _pathlike_entry()
    manifest = _write_manifest(tmp_path / "findings.json", [record])

    assert _run_relay(queue, owner, manifest, "--apply", "--json") == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert set(payload) == APPLY_RESULT_KEYS
    assert payload["created"] == 1
    assert payload["relays"] == [_relay_id(record)]
    _assert_opaque_result(payload)
    assert record["producer"] not in captured.out
    assert record["finding_id"] not in captured.out
    assert (
        _review_inbox(owner)
        / grokbot_findings.draft_filename(record["producer"], record["finding_id"], record["revision"])
    ).is_file()


def test_relay_findings_help_is_preview_first_without_credential_options(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["run", "cloud", "grokbot", "relay-findings", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out.casefold()
    assert "--apply" in out
    assert "preview" in out
    for option in ("--token", "--bearer", "--password", "--api-key", "--credential"):
        assert option not in out


def test_existing_findings_commands_remain_in_parser_inventory():
    from brigade.roadmap_cmd import _cli_command_paths

    paths = _cli_command_paths()
    assert "brigade run-cloud grokbot relay-findings" in paths
    assert "brigade run-cloud grokbot reconcile-findings" in paths
    assert "brigade run-cloud grokbot convert-findings" in paths
