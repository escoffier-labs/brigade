"""Approved Grok Bot feed: local-only manifest validation and bounded apply."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from brigade import cli, fleet_client_grokbot, fleet_hub, fleet_hub_grokbot, grokbot_feed, grokbot_jobs


SECRET_INSTRUCTIONS = "SECRET_INSTRUCTION_DO_NOT_PRINT"
SECRET_VERIFY = "SECRET_VERIFY_CMD"
SECRET_OWNERSHIP = "SECRET_OWNERSHIP_PATH/file.py"
SECRET_WAKE_KEY = "SECRET_WAKE_SENDER_KEY_DO_NOT_PRINT"


def _spec(*, label: str = "Approved worker task") -> dict[str, object]:
    return {
        "label": label,
        "role": "implementation-worker",
        "repository": "example/brigade",
        "base_ref": "main",
        "ownership_paths": [SECRET_OWNERSHIP],
        "instructions": SECRET_INSTRUCTIONS,
        "verification_commands": [SECRET_VERIFY],
        "artifact": {"kind": "draft-pr"},
        "timeout_seconds": 900,
    }


def _manifest(*entries: dict[str, object], approved: bool = True, label: str = "operator-day-1") -> dict[str, object]:
    return {
        "schema": "brigade.grokbot.feed.v1",
        "approved": approved,
        "label": label,
        "entries": list(entries),
    }


def _entry(key: str, spec: dict[str, object] | None = None) -> dict[str, object]:
    return {"idempotency_key": key, "spec": spec if spec is not None else _spec()}


def _write_manifest(path: Path, payload: dict[str, object], *, mode: int = 0o600) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(mode)
    return path


def _queue_root(target: Path) -> Path:
    return target / ".brigade" / "cloud" / "grokbot"


def _job_files(target: Path) -> list[Path]:
    jobs = _queue_root(target) / "jobs"
    if not jobs.exists():
        return []
    return sorted(jobs.glob("grokbot-*.json"))


def test_preflight_accepts_a_valid_manifest_without_writing_queue_state(tmp_path: Path):
    manifest = _write_manifest(
        tmp_path / "feed.json",
        _manifest(_entry("task-a", _spec(label="First")), _entry("task-b", _spec(label="Second"))),
    )

    result = grokbot_feed.preflight(tmp_path, manifest)

    assert result["valid"] == 2
    assert result["known"] == 0
    assert result["limit"] == 1
    assert not _queue_root(tmp_path).exists()


def test_preflight_counts_known_idempotency_records_without_writing(tmp_path: Path):
    grokbot_jobs.enqueue(tmp_path, _spec(label="First"), "task-a")
    before = {path.name for path in _job_files(tmp_path)}
    manifest = _write_manifest(
        tmp_path / "feed.json",
        _manifest(_entry("task-a", _spec(label="First")), _entry("task-b", _spec(label="Second"))),
    )

    result = grokbot_feed.preflight(tmp_path, manifest)

    assert result["valid"] == 2
    assert result["known"] == 1
    assert {path.name for path in _job_files(tmp_path)} == before


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (
            {
                "schema": "brigade.grokbot.feed.v0",
                "approved": True,
                "label": "operator-day-1",
                "entries": [_entry("task-a")],
            },
            "invalid-schema",
        ),
        (
            {
                "schema": "brigade.grokbot.feed.v1",
                "label": "operator-day-1",
                "entries": [_entry("task-a")],
            },
            "not-approved",
        ),
        (
            {
                "schema": "brigade.grokbot.feed.v1",
                "approved": False,
                "label": "operator-day-1",
                "entries": [_entry("task-a")],
            },
            "not-approved",
        ),
        (
            {
                "schema": "brigade.grokbot.feed.v1",
                "approved": True,
                "label": "",
                "entries": [_entry("task-a")],
            },
            "invalid-label",
        ),
        (
            {
                "schema": "brigade.grokbot.feed.v1",
                "approved": True,
                "label": "operator-day-1",
                "entries": [_entry("task-a"), _entry("task-a")],
            },
            "duplicate-key",
        ),
    ],
)
def test_preflight_rejects_malformed_manifests_without_writing_queue_state(
    tmp_path: Path, payload: dict[str, object], reason: str
):
    manifest = _write_manifest(tmp_path / "feed.json", payload)

    with pytest.raises(grokbot_feed.FeedError) as exc:
        grokbot_feed.preflight(tmp_path, manifest)

    assert exc.value.reason == reason
    assert not _queue_root(tmp_path).exists()


def test_preflight_rejects_group_writable_manifest_without_writing(tmp_path: Path):
    manifest = _write_manifest(
        tmp_path / "feed.json",
        _manifest(_entry("task-a")),
        mode=0o660,
    )

    with pytest.raises(grokbot_feed.FeedError) as exc:
        grokbot_feed.preflight(tmp_path, manifest)

    assert exc.value.reason == "unsafe-manifest"
    assert not _queue_root(tmp_path).exists()


def test_preflight_rejects_world_writable_manifest_without_writing(tmp_path: Path):
    manifest = _write_manifest(
        tmp_path / "feed.json",
        _manifest(_entry("task-a")),
        mode=0o602,
    )

    with pytest.raises(grokbot_feed.FeedError) as exc:
        grokbot_feed.preflight(tmp_path, manifest)

    assert exc.value.reason == "unsafe-manifest"
    assert not _queue_root(tmp_path).exists()


def test_preflight_rejects_symlink_manifest_without_writing(tmp_path: Path):
    real = _write_manifest(tmp_path / "real.json", _manifest(_entry("task-a")))
    link = tmp_path / "feed.json"
    link.symlink_to(real)

    with pytest.raises(grokbot_feed.FeedError) as exc:
        grokbot_feed.preflight(tmp_path, link)

    assert exc.value.reason == "unsafe-manifest"
    assert not _queue_root(tmp_path).exists()


@pytest.mark.parametrize("limit", [0, 11, -1, True, 1.5, "1"])
def test_preflight_rejects_invalid_limits_without_writing(tmp_path: Path, limit: object):
    manifest = _write_manifest(tmp_path / "feed.json", _manifest(_entry("task-a")))

    with pytest.raises(grokbot_feed.FeedError) as exc:
        grokbot_feed.preflight(tmp_path, manifest, limit=limit)  # type: ignore[arg-type]

    assert exc.value.reason == "invalid-limit"
    assert not _queue_root(tmp_path).exists()


def test_preflight_rejects_invalid_later_job_without_writing(tmp_path: Path):
    later = _spec(label="Broken later job")
    later.pop("instructions")
    manifest = _write_manifest(
        tmp_path / "feed.json",
        _manifest(_entry("task-a", _spec(label="First")), _entry("task-b", later)),
    )

    with pytest.raises(grokbot_feed.FeedError) as exc:
        grokbot_feed.preflight(tmp_path, manifest)

    assert exc.value.reason == "missing-key"
    assert not _queue_root(tmp_path).exists()


def test_preflight_rejects_idempotency_conflict_without_writing(tmp_path: Path):
    grokbot_jobs.enqueue(tmp_path, _spec(label="First"), "task-a")
    before = {path.name for path in _job_files(tmp_path)}
    manifest = _write_manifest(
        tmp_path / "feed.json",
        _manifest(_entry("task-a", _spec(label="Different task"))),
    )

    with pytest.raises(grokbot_feed.FeedError) as exc:
        grokbot_feed.preflight(tmp_path, manifest)

    assert exc.value.reason == "idempotency-conflict"
    assert {path.name for path in _job_files(tmp_path)} == before


def _assert_redacted(payload: object) -> None:
    dumped = json.dumps(payload)
    assert SECRET_INSTRUCTIONS not in dumped
    assert SECRET_VERIFY not in dumped
    assert SECRET_OWNERSHIP not in dumped
    if isinstance(payload, dict):
        assert "instructions" not in dumped
        assert "verification_commands" not in dumped
        assert "ownership_paths" not in dumped


def test_apply_limit_1_creates_only_the_first_unknown_job(tmp_path: Path):
    manifest = _write_manifest(
        tmp_path / "feed.json",
        _manifest(_entry("task-a", _spec(label="First")), _entry("task-b", _spec(label="Second"))),
    )

    result = grokbot_feed.apply(tmp_path, manifest, limit=1)

    assert result["created"] == 1
    assert result["skipped"] == 0
    assert result["known"] == 0
    assert result["valid"] == 2
    assert len(result["jobs"]) == 1
    assert result["jobs"][0]["state"] == "queued"
    assert result["jobs"][0]["idempotent"] is False
    assert len(_job_files(tmp_path)) == 1
    _assert_redacted(result)


def test_apply_repeated_run_skips_known_and_creates_next_unknown(tmp_path: Path):
    manifest = _write_manifest(
        tmp_path / "feed.json",
        _manifest(_entry("task-a", _spec(label="First")), _entry("task-b", _spec(label="Second"))),
    )
    first = grokbot_feed.apply(tmp_path, manifest, limit=1)
    first_id = first["jobs"][0]["job_id"]

    result = grokbot_feed.apply(tmp_path, manifest, limit=1)

    assert result["created"] == 1
    assert result["skipped"] == 1
    assert result["known"] == 1
    assert result["valid"] == 2
    assert [job["job_id"] for job in result["jobs"]][0] == first_id
    assert result["jobs"][0]["idempotent"] is True
    assert result["jobs"][1]["idempotent"] is False
    assert result["jobs"][1]["job_id"] != first_id
    assert len(_job_files(tmp_path)) == 2
    _assert_redacted(result)


def test_apply_hub_replay_is_skipped_without_duplicate_job_or_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    feed_node = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    connection = fleet_hub.init_db(tmp_path / "fleet.db")
    try:
        status, payload = fleet_hub_grokbot.handle_grokbot(
            connection,
            {
                "action": "enroll-actor",
                "enroll_node_id": feed_node,
                "queue_owner_node_id": feed_node,
                "queue_id": "grokbot-feed-test",
                "actor_kind": "feed",
                "enabled": True,
            },
            caller_node=None,
        )
        assert (status, payload) == (200, {"enrolled": True, "node_id": feed_node})

        def enqueue(**fields: object) -> fleet_client_grokbot.GrokbotHubDecision:
            status, payload = fleet_hub_grokbot.handle_grokbot(
                connection,
                {"action": "enqueue", **fields},
                caller_node=feed_node,
            )
            return fleet_client_grokbot.GrokbotHubDecision(
                granted=status == 200 and payload.get("enqueued") is True,
                reason=payload.get("error", "") if isinstance(payload, dict) else "invalid-response",
                job=payload.get("job") if isinstance(payload, dict) else None,
                idempotent=payload.get("idempotent") is True if isinstance(payload, dict) else False,
            )

        monkeypatch.setattr(grokbot_jobs, "hub_authority", lambda _target: True)
        monkeypatch.setattr(fleet_client_grokbot, "enqueue", enqueue)
        manifest = _write_manifest(tmp_path / "feed.json", _manifest(_entry("task-a")))

        first = grokbot_feed.apply(tmp_path, manifest)
        job_id = first["jobs"][0]["job_id"]
        before = connection.execute(
            "SELECT state, item_revision, sequence FROM grokbot_jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        event_count = connection.execute("SELECT COUNT(*) FROM events WHERE harness='grokbot'").fetchone()[0]

        second = grokbot_feed.apply(tmp_path, manifest)

        assert first["created"] == 1
        assert first["skipped"] == 0
        assert second["known"] == 0
        assert second["created"] == 0
        assert second["skipped"] == 1
        assert second["jobs"] == [{"job_id": job_id, "state": "queued", "idempotent": True}]
        assert (
            connection.execute(
                "SELECT state, item_revision, sequence FROM grokbot_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            == before
        )
        assert connection.execute("SELECT COUNT(*) FROM grokbot_jobs").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM events WHERE harness='grokbot'").fetchone()[0] == event_count
    finally:
        connection.close()


def test_apply_invalid_later_entry_prevents_all_writes(tmp_path: Path):
    later = _spec(label="Broken later job")
    later.pop("instructions")
    manifest = _write_manifest(
        tmp_path / "feed.json",
        _manifest(_entry("task-a", _spec(label="First")), _entry("task-b", later)),
    )

    with pytest.raises(grokbot_feed.FeedError) as exc:
        grokbot_feed.apply(tmp_path, manifest, limit=1)

    assert exc.value.reason == "missing-key"
    assert not _queue_root(tmp_path).exists()


def test_apply_stops_on_unexpected_queue_error_with_generic_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    manifest = _write_manifest(
        tmp_path / "feed.json",
        _manifest(_entry("task-a", _spec(label="First")), _entry("task-b", _spec(label="Second"))),
    )
    calls = {"n": 0}
    original_enqueue = grokbot_jobs.enqueue

    def boom(target: Path, spec: dict, idempotency_key: str, now=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return original_enqueue(target, spec, idempotency_key, now=now)
        raise grokbot_jobs.GrokbotJobError("unsafe-storage")

    monkeypatch.setattr(grokbot_jobs, "enqueue", boom)

    with pytest.raises(grokbot_feed.FeedError) as exc:
        grokbot_feed.apply(tmp_path, manifest, limit=2)

    assert exc.value.reason == "queue-error"
    assert exc.value.index == 1
    assert len(_job_files(tmp_path)) == 1


def test_apply_uses_one_in_memory_snapshot_when_the_manifest_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    manifest = _write_manifest(
        tmp_path / "feed.json",
        _manifest(_entry("task-a", _spec(label="First")), _entry("task-b", _spec(label="Second"))),
    )
    opened = {"n": 0, "flags": 0}
    real_open = grokbot_feed.os.open

    def open_and_replace(path, flags, *args, **kwargs):
        handle = real_open(path, flags, *args, **kwargs)
        if kwargs.get("dir_fd") is not None:
            return handle
        try:
            opened_path = Path(os.fspath(path)).resolve()
        except OSError:
            return handle
        if opened_path != manifest.resolve():
            return handle
        opened["n"] += 1
        opened["flags"] = flags
        replacement = tmp_path / "feed.replaced.json"
        _write_manifest(replacement, _manifest(_entry("task-z", _spec(label="Replaced"))))
        os.replace(replacement, manifest)
        return handle

    monkeypatch.setattr(grokbot_feed.os, "open", open_and_replace)

    result = grokbot_feed.apply(tmp_path, manifest, limit=1)

    assert opened["n"] == 1
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        assert opened["flags"] & nofollow
    assert result["valid"] == 2
    assert result["created"] == 1
    assert result["jobs"][0]["idempotent"] is False
    record = json.loads(_job_files(tmp_path)[0].read_text(encoding="utf-8"))
    assert record["spec"]["label"] == "First"
    _assert_redacted(result)


def test_preflight_rejects_unsafe_idempotency_storage_without_leaking_paths(tmp_path: Path):
    grokbot_jobs.enqueue(tmp_path, _spec(label="First"), "task-a")
    before = {path.name for path in _job_files(tmp_path)}
    record = next((_queue_root(tmp_path) / "idempotency").glob("*.json"))
    destination = tmp_path / "outside.json"
    destination.write_text("{}", encoding="utf-8")
    record.unlink()
    record.symlink_to(destination)
    manifest = _write_manifest(tmp_path / "feed.json", _manifest(_entry("task-a", _spec(label="First"))))

    with pytest.raises(grokbot_feed.FeedError) as exc:
        grokbot_feed.preflight(tmp_path, manifest)

    assert exc.value.reason == "unsafe-storage"
    assert str(record) not in str(exc.value)
    assert str(destination) not in str(exc.value)
    assert str(tmp_path) not in str(exc.value)
    assert {path.name for path in _job_files(tmp_path)} == before


def test_apply_translates_oserror_from_enqueue_to_queue_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    manifest = _write_manifest(
        tmp_path / "feed.json",
        _manifest(_entry("task-a", _spec(label="First")), _entry("task-b", _spec(label="Second"))),
    )

    def boom(target: Path, spec: dict, idempotency_key: str, now=None):
        raise OSError(13, "Permission denied", str(tmp_path / "secret-queue.json"))

    monkeypatch.setattr(grokbot_jobs, "enqueue", boom)

    with pytest.raises(grokbot_feed.FeedError) as exc:
        grokbot_feed.apply(tmp_path, manifest, limit=1)

    assert exc.value.reason == "queue-error"
    assert exc.value.index == 0
    assert "secret-queue.json" not in str(exc.value)
    assert "Permission denied" not in str(exc.value)
    assert not _queue_root(tmp_path).exists()


def _run_feed(target: Path, *command: str) -> int:
    return cli.main(["run", "cloud", "grokbot", "feed", *command, "--target", str(target)])


def test_cli_feed_defaults_to_validation_only(tmp_path: Path, capsys):
    manifest = _write_manifest(
        tmp_path / "feed.json",
        _manifest(_entry("task-a", _spec(label="First")), _entry("task-b", _spec(label="Second"))),
    )

    assert _run_feed(tmp_path, "--manifest", str(manifest)) == 0
    captured = capsys.readouterr()
    assert "valid=2" in captured.out
    assert "known=0" in captured.out
    assert "created=" not in captured.out
    assert captured.err == ""
    assert SECRET_INSTRUCTIONS not in captured.out
    assert SECRET_VERIFY not in captured.out
    assert SECRET_OWNERSHIP not in captured.out
    assert not _queue_root(tmp_path).exists()


def test_cli_feed_preview_needs_no_feed_identity_or_hub_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    manifest = _write_manifest(tmp_path / "feed.json", _manifest(_entry("task-a")))
    monkeypatch.setattr(grokbot_jobs, "hub_authority", lambda _target: True)
    monkeypatch.setattr(
        grokbot_jobs,
        "enqueue",
        lambda *_args, **_kwargs: pytest.fail("preview must not enqueue through the hub"),
    )

    assert _run_feed(tmp_path, "--manifest", str(manifest)) == 0
    assert "valid=1" in capsys.readouterr().out


def test_cli_feed_apply_binds_dedicated_feed_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    token = "dedicated-feed-token"
    token_file = tmp_path / "feed.hub-token"
    token_file.write_text(token + "\n", encoding="utf-8")
    token_file.chmod(0o600)
    monkeypatch.setenv("BRIGADE_GROKBOT_FEED_HUB_TOKEN_FILE", str(token_file))
    monkeypatch.setattr(grokbot_jobs, "hub_authority", lambda _target: True)
    seen: list[str | None] = []
    monkeypatch.setattr(
        grokbot_jobs,
        "enqueue",
        lambda *_args, **_kwargs: (
            seen.append(fleet_client_grokbot.current_listener_token())
            or {"job_id": "grokbot-" + "a" * 24, "state": "queued", "idempotent": False}
        ),
    )
    manifest = _write_manifest(tmp_path / "feed.json", _manifest(_entry("task-a")))

    assert _run_feed(tmp_path, "--manifest", str(manifest), "--apply") == 0
    assert seen == [token]
    assert token not in capsys.readouterr().out


@pytest.mark.parametrize("kind", ("missing", "relative", "symlink", "directory", "unsafe-mode"))
def test_cli_feed_apply_rejects_unsafe_dedicated_identity_before_enqueue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, kind: str
):
    token = "dedicated-feed-token-must-not-leak"
    configured_path = tmp_path / "feed.hub-token"
    if kind == "missing":
        configured_text = str(configured_path)
    elif kind == "relative":
        configured_text = "feed.hub-token"
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
    monkeypatch.setenv("BRIGADE_GROKBOT_FEED_HUB_TOKEN_FILE", configured_text)
    monkeypatch.setattr(grokbot_jobs, "hub_authority", lambda _target: True)
    hub_calls: list[object] = []
    monkeypatch.setattr(
        grokbot_jobs,
        "enqueue",
        lambda *_args, **_kwargs: hub_calls.append(object()) or pytest.fail("unsafe feed token must block enqueue"),
    )
    manifest = _write_manifest(tmp_path / "feed.json", _manifest(_entry("task-a")))

    assert _run_feed(tmp_path, "--manifest", str(manifest), "--apply") == 2
    captured = capsys.readouterr()
    rendered = captured.out + captured.err
    assert hub_calls == []
    assert rendered == "error: Grok Bot feed configuration is invalid\n"
    assert token not in rendered
    assert configured_text not in rendered


def test_cli_feed_apply_limit_1_then_repeats_to_the_next_unknown(tmp_path: Path, capsys):
    manifest = _write_manifest(
        tmp_path / "feed.json",
        _manifest(_entry("task-a", _spec(label="First")), _entry("task-b", _spec(label="Second"))),
    )

    assert _run_feed(tmp_path, "--manifest", str(manifest), "--apply", "--limit", "1") == 0
    first = capsys.readouterr()
    assert "created=1" in first.out
    assert "skipped=0" in first.out
    assert SECRET_INSTRUCTIONS not in first.out
    first_id = [part for part in first.out.split() if part.startswith("grokbot-")][0]
    assert len(_job_files(tmp_path)) == 1

    assert _run_feed(tmp_path, "--manifest", str(manifest), "--apply", "--limit", "1") == 0
    second = capsys.readouterr()
    assert "created=1" in second.out
    assert "skipped=1" in second.out
    assert first_id in second.out
    assert SECRET_VERIFY not in second.out
    assert len(_job_files(tmp_path)) == 2


def test_cli_feed_json_stays_free_of_private_task_context(tmp_path: Path, capsys):
    manifest = _write_manifest(
        tmp_path / "feed.json",
        _manifest(_entry("task-a", _spec(label="First")), _entry("task-b", _spec(label="Second"))),
    )

    assert _run_feed(tmp_path, "--manifest", str(manifest), "--apply", "--limit", "1", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] == 1
    assert payload["jobs"][0]["state"] == "queued"
    _assert_redacted(payload)


def test_cli_feed_rejects_invalid_later_entry_without_writing(tmp_path: Path, capsys):
    later = _spec(label="Broken later job")
    later.pop("instructions")
    manifest = _write_manifest(
        tmp_path / "feed.json",
        _manifest(_entry("task-a", _spec(label="First")), _entry("task-b", later)),
    )

    assert _run_feed(tmp_path, "--manifest", str(manifest), "--apply", "--limit", "1") == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "error: missing-key"
    assert SECRET_INSTRUCTIONS not in captured.err
    assert not _queue_root(tmp_path).exists()


def test_cli_feed_reports_generic_queue_error_with_index(tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch):
    manifest = _write_manifest(
        tmp_path / "feed.json",
        _manifest(_entry("task-a", _spec(label="First")), _entry("task-b", _spec(label="Second"))),
    )
    original_enqueue = grokbot_jobs.enqueue
    calls = {"n": 0}

    def boom(target: Path, spec: dict, idempotency_key: str, now=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return original_enqueue(target, spec, idempotency_key, now=now)
        raise grokbot_jobs.GrokbotJobError("unsafe-storage")

    monkeypatch.setattr(grokbot_jobs, "enqueue", boom)

    assert _run_feed(tmp_path, "--manifest", str(manifest), "--apply", "--limit", "2") == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "error: queue-error index=1"
    assert "unsafe-storage" not in captured.err
    assert SECRET_INSTRUCTIONS not in captured.err


def test_cli_feed_rejects_invalid_limit(tmp_path: Path, capsys):
    manifest = _write_manifest(tmp_path / "feed.json", _manifest(_entry("task-a")))

    assert _run_feed(tmp_path, "--manifest", str(manifest), "--limit", "0") == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "error: invalid-limit"
    assert not _queue_root(tmp_path).exists()


class _WakeRecorder:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []
        self.status = 200
        self.delay = 0.0


def _start_wake_server(recorder: _WakeRecorder) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            if recorder.delay:
                time.sleep(recorder.delay)
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            recorder.requests.append(
                {
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "automation_key": self.headers.get("X-Automation-Key"),
                    "content_type": self.headers.get("Content-Type"),
                    "body": body,
                }
            )
            self.send_response(recorder.status)
            self.end_headers()

        def log_message(self, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _write_wake_key(path: Path, value: str = SECRET_WAKE_KEY) -> Path:
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)
    return path


def _write_wake_config(target: Path, url: str, key_file: Path) -> Path:
    root = _queue_root(target)
    root.mkdir(parents=True, exist_ok=True)
    path = grokbot_feed.wake_config_path(target)
    path.write_text(
        json.dumps(
            {
                "schema": grokbot_feed.WAKE_SCHEMA,
                "webhook_url": url,
                "sender_key_file": str(key_file),
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def _notify_log_text(target: Path) -> str:
    path = grokbot_feed.wake_notify_log_path(target)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _assert_wake_secret_absent(*payloads: object) -> None:
    for payload in payloads:
        text = payload if isinstance(payload, str) else json.dumps(payload)
        assert SECRET_WAKE_KEY not in text
        assert "X-Automation-Key" not in text
        assert "Authorization" not in text


def test_apply_wake_notify_posts_bounded_body_on_success(tmp_path: Path):
    recorder = _WakeRecorder()
    server = _start_wake_server(recorder)
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/wake"
        key_file = _write_wake_key(tmp_path / "wake.key")
        _write_wake_config(tmp_path, url, key_file)
        manifest = _write_manifest(tmp_path / "feed.json", _manifest(_entry("task-a", _spec(label="First"))))

        result = grokbot_feed.apply(tmp_path, manifest, limit=1)

        assert result["created"] == 1
        assert len(recorder.requests) == 1
        request = recorder.requests[0]
        assert request["authorization"] == f"Bearer {SECRET_WAKE_KEY}"
        assert request["automation_key"] == SECRET_WAKE_KEY
        assert request["content_type"] == "application/json"
        body = json.loads(request["body"])
        assert body == {
            "job_id": result["jobs"][0]["job_id"],
            "label": "First",
            "repository": "example/brigade",
            "role": "implementation-worker",
        }
        log = _notify_log_text(tmp_path)
        assert json.loads(log.strip()) == {"status": 200}
        _assert_wake_secret_absent(result, log)
        _assert_redacted(result)
    finally:
        server.shutdown()
        server.server_close()


def test_apply_wake_notify_non_200_does_not_fail_enqueue(tmp_path: Path):
    recorder = _WakeRecorder()
    recorder.status = 503
    server = _start_wake_server(recorder)
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/wake"
        _write_wake_config(tmp_path, url, _write_wake_key(tmp_path / "wake.key"))
        manifest = _write_manifest(tmp_path / "feed.json", _manifest(_entry("task-a")))

        result = grokbot_feed.apply(tmp_path, manifest, limit=1)

        assert result["created"] == 1
        assert result["jobs"][0]["state"] == "queued"
        assert len(_job_files(tmp_path)) == 1
        assert len(recorder.requests) == 1
        log = _notify_log_text(tmp_path)
        assert json.loads(log.strip()) == {"status": 503}
        _assert_wake_secret_absent(result, log)
    finally:
        server.shutdown()
        server.server_close()


def test_apply_wake_notify_timeout_does_not_fail_enqueue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    recorder = _WakeRecorder()
    recorder.delay = 1.0
    server = _start_wake_server(recorder)
    try:
        monkeypatch.setattr(grokbot_feed, "WAKE_TIMEOUT_SECONDS", 0.2)
        url = f"http://127.0.0.1:{server.server_address[1]}/wake"
        _write_wake_config(tmp_path, url, _write_wake_key(tmp_path / "wake.key"))
        manifest = _write_manifest(tmp_path / "feed.json", _manifest(_entry("task-a")))

        result = grokbot_feed.apply(tmp_path, manifest, limit=1)

        assert result["created"] == 1
        assert result["jobs"][0]["state"] == "queued"
        log = _notify_log_text(tmp_path)
        assert json.loads(log.strip()) == {"status": 0}
        _assert_wake_secret_absent(result, log)
    finally:
        server.shutdown()
        server.server_close()


def test_apply_wake_notify_missing_config_skips_post(tmp_path: Path):
    recorder = _WakeRecorder()
    server = _start_wake_server(recorder)
    try:
        manifest = _write_manifest(tmp_path / "feed.json", _manifest(_entry("task-a")))

        result = grokbot_feed.apply(tmp_path, manifest, limit=1)

        assert result["created"] == 1
        assert recorder.requests == []
        assert _notify_log_text(tmp_path) == ""
        assert not grokbot_feed.wake_notify_log_path(tmp_path).exists()
        _assert_redacted(result)
    finally:
        server.shutdown()
        server.server_close()


def test_cli_feed_apply_omits_wake_key_from_output(tmp_path: Path, capsys):
    recorder = _WakeRecorder()
    server = _start_wake_server(recorder)
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/wake"
        _write_wake_config(tmp_path, url, _write_wake_key(tmp_path / "wake.key"))
        manifest = _write_manifest(tmp_path / "feed.json", _manifest(_entry("task-a")))

        assert _run_feed(tmp_path, "--manifest", str(manifest), "--apply") == 0
        captured = capsys.readouterr()
        assert "created=1" in captured.out
        _assert_wake_secret_absent(captured.out, captured.err, _notify_log_text(tmp_path))
    finally:
        server.shutdown()
        server.server_close()


def test_worker_instructions_carry_the_five_swarm_contract_rules():
    """One helper renders the contract, so no feed can drift away from it."""
    instructions = grokbot_feed.worker_instructions(
        header="Repository: example/brigade\nIssue number: 7\n",
        issue_number=7,
        base_ref="main",
        verification_commands=[SECRET_VERIFY],
    )

    assert instructions.startswith("Repository: example/brigade\nIssue number: 7\n\n")
    assert grokbot_feed.UNTRUSTED_CONTEXT_SENTENCE in instructions
    assert grokbot_feed.NO_MERGE_SENTENCE in instructions

    coordinator = instructions.index("Coordinator rule: do not write code in your own context.")
    contract = instructions.index("Job contract:")
    lease = instructions.index("Lease rule: renew the lease at least every 5 minutes.")
    cap = instructions.index("Time cap: stop within 60 minutes of claiming this job")
    proof = instructions.index("Proof rule: run exactly the verification commands named above")
    assert coordinator < contract < lease < cap < proof

    assert "Spawn one cloud agent for this job" in instructions
    assert "Prefer a token-efficient worker model" in instructions
    assert "Keep your own context for planning, lease renewal, reading the worker's proof, and the pull request" in (
        instructions
    )
    assert "Done predicate: the acceptance criteria in the issue's Acceptance or Done section" in instructions
    assert "otherwise: Implement only the approved issue." in instructions
    assert "a branch cut from main named cursor/issue-7-<slug>" in instructions
    assert f"- Verification commands, exactly these:\n  - {SECRET_VERIFY}\n" in instructions
    assert "Final report: exactly one of PASS, ISSUES, or BLOCKED, with the evidence behind it." in instructions
    assert "A lease-expired or job-expired answer is a stop signal" in instructions
    assert "never run the full suite unless it is named there" in instructions
    assert "Open exactly one draft pull request against the base ref whose body carries Fixes #7" in instructions
    assert "each verification command with its exit code, and the receipt id" in instructions
    assert "Complete the job with the pull request URL." in instructions
    assert "push the branch and fail the job with the exact failing command" in instructions


def test_worker_instructions_name_every_verification_command():
    instructions = grokbot_feed.worker_instructions(
        header="Repository: example/brigade\n",
        issue_number=12,
        base_ref="release/2.0",
        verification_commands=["ruff check .", "pytest -q tests/test_one.py"],
    )

    assert "  - ruff check .\n  - pytest -q tests/test_one.py\n" in instructions
    assert "a branch cut from release/2.0 named cursor/issue-12-<slug>" in instructions
    assert "Fixes #12" in instructions


FEED_NODE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
WORKER_NODE = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
OPERATOR_NODE = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
HUB_QUEUE_ID = "grokbot-feed-terminal-replay"


def _enroll_hub_actor(connection, node: str, kind: str, role: str | None = None) -> None:
    body = {
        "action": "enroll-actor",
        "enroll_node_id": node,
        "queue_owner_node_id": FEED_NODE,
        "queue_id": HUB_QUEUE_ID,
        "actor_kind": kind,
        "enabled": True,
    }
    if role is not None:
        body["role"] = role
    status, payload = fleet_hub_grokbot.handle_grokbot(connection, body, caller_node=None)
    assert status == 200, payload


def _bind_hub_enqueue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    connection = fleet_hub.init_db(tmp_path / "fleet.db")
    _enroll_hub_actor(connection, FEED_NODE, "feed")
    _enroll_hub_actor(connection, WORKER_NODE, "implementation-worker", "implementation-worker")
    _enroll_hub_actor(connection, OPERATOR_NODE, "operator")

    def enqueue(**fields: object) -> fleet_client_grokbot.GrokbotHubDecision:
        status, payload = fleet_hub_grokbot.handle_grokbot(
            connection,
            {"action": "enqueue", **fields},
            caller_node=FEED_NODE,
        )
        return fleet_client_grokbot.GrokbotHubDecision(
            granted=status == 200 and payload.get("enqueued") is True,
            reason=payload.get("error", "") if isinstance(payload, dict) else "invalid-response",
            job=payload.get("job") if isinstance(payload, dict) else None,
            idempotent=payload.get("idempotent") is True if isinstance(payload, dict) else False,
        )

    monkeypatch.setattr(grokbot_jobs, "hub_authority", lambda _target: True)
    monkeypatch.setattr(fleet_client_grokbot, "enqueue", enqueue)
    return connection


def _drift_hub_enqueue_digest(connection, job_id: str) -> None:
    connection.execute(
        "UPDATE grokbot_operations SET request_digest=? WHERE job_id=? AND action='enqueue'",
        ("0" * 64, job_id),
    )
    connection.commit()


def _complete_hub_job(connection, job_id: str) -> None:
    claimed = fleet_hub_grokbot.handle_grokbot(
        connection,
        {
            "action": "claim",
            "job_id": job_id,
            "lease_id": "lease-a",
            "expected_item_revision": 1,
            "lease_seconds": 300,
            "operation_id": "op-claim-1",
        },
        caller_node=WORKER_NODE,
    )
    assert claimed[0] == 200, claimed[1]
    generation = claimed[1]["lease_generation"]
    started = fleet_hub_grokbot.handle_grokbot(
        connection,
        {
            "action": "start",
            "job_id": job_id,
            "lease_id": "lease-a",
            "expected_item_revision": claimed[1]["job"]["item_revision"],
            "lease_generation": generation,
            "operation_id": "op-start-1",
        },
        caller_node=WORKER_NODE,
    )
    assert started[0] == 200, started[1]
    completed = fleet_hub_grokbot.handle_grokbot(
        connection,
        {
            "action": "complete",
            "job_id": job_id,
            "lease_id": "lease-a",
            "expected_item_revision": started[1]["job"]["item_revision"],
            "lease_generation": generation,
            "operation_id": "op-complete-1",
            "artifact": {"kind": "draft-pr", "ref": "https://github.com/example/brigade/pull/9"},
        },
        caller_node=WORKER_NODE,
    )
    assert completed[0] == 200, completed[1]


def _cancel_hub_job(connection, job_id: str) -> None:
    status, payload = fleet_hub_grokbot.handle_grokbot(
        connection,
        {"action": "cancel", "job_id": job_id, "expected_item_revision": 1, "operation_id": "op-cancel-1"},
        caller_node=OPERATOR_NODE,
    )
    assert status == 200, payload
    assert payload["job"]["state"] == "canceled"


def _expire_hub_job(connection, job_id: str) -> None:
    queued_at = datetime.now(timezone.utc) - timedelta(seconds=fleet_hub_grokbot.DEFAULT_QUEUE_TTL_SECONDS + 60)
    stamp = queued_at.isoformat().replace("+00:00", "Z")
    connection.execute(
        "UPDATE grokbot_jobs SET queued_at=?, created_at=?, updated_at=? WHERE job_id=?",
        (stamp, stamp, stamp, job_id),
    )
    connection.commit()
    assert fleet_hub_grokbot.sweep_expired_jobs(connection) == [job_id]


@pytest.mark.parametrize("terminalize", ["completed", "expired", "canceled"])
def test_apply_replays_terminal_hub_jobs_and_creates_the_next_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, terminalize: str
):
    connection = _bind_hub_enqueue(tmp_path, monkeypatch)
    try:
        manifest = _write_manifest(
            tmp_path / "feed.json",
            _manifest(_entry("task-a", _spec(label="First")), _entry("task-b", _spec(label="Second"))),
        )
        first = grokbot_feed.apply(tmp_path, manifest, limit=1)
        job_id = first["jobs"][0]["job_id"]
        if terminalize == "completed":
            _complete_hub_job(connection, job_id)
        elif terminalize == "expired":
            _expire_hub_job(connection, job_id)
        else:
            _cancel_hub_job(connection, job_id)
        _drift_hub_enqueue_digest(connection, job_id)

        result = grokbot_feed.apply(tmp_path, manifest, limit=1)

        assert result["created"] == 1
        assert result["skipped"] == 1
        assert result["jobs"][0]["job_id"] == job_id
        assert result["jobs"][0]["idempotent"] is True
        assert result["jobs"][0]["state"] == terminalize
        assert result["jobs"][1]["idempotent"] is False
        assert result["jobs"][1]["state"] == "queued"
        assert result["jobs"][1]["job_id"] != job_id
        assert connection.execute("SELECT COUNT(*) FROM grokbot_jobs").fetchone()[0] == 2
        _assert_redacted(result)
    finally:
        connection.close()


def test_apply_surfaces_bounded_hub_error_detail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    manifest = _write_manifest(
        tmp_path / "feed.json",
        _manifest(_entry("task-a", _spec(label="First")), _entry("task-b", _spec(label="Second"))),
    )

    def boom(target: Path, spec: dict, idempotency_key: str, now=None):
        raise grokbot_jobs.GrokbotJobError("operation-mismatch")

    monkeypatch.setattr(grokbot_jobs, "enqueue", boom)

    with pytest.raises(grokbot_feed.FeedError) as exc:
        grokbot_feed.apply(tmp_path, manifest, limit=1)

    assert exc.value.reason == "queue-error index=0 operation-mismatch"
    assert exc.value.index == 0
    assert exc.value.detail == "operation-mismatch"
    assert not _queue_root(tmp_path).exists()


def test_cli_feed_reports_bounded_hub_error_detail(tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch):
    manifest = _write_manifest(
        tmp_path / "feed.json",
        _manifest(_entry("task-a", _spec(label="First")), _entry("task-b", _spec(label="Second"))),
    )

    def boom(target: Path, spec: dict, idempotency_key: str, now=None):
        raise grokbot_jobs.GrokbotJobError("operation-mismatch")

    monkeypatch.setattr(grokbot_jobs, "enqueue", boom)

    assert _run_feed(tmp_path, "--manifest", str(manifest), "--apply", "--limit", "1") == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "error: queue-error index=0 operation-mismatch"
    assert SECRET_INSTRUCTIONS not in captured.err
