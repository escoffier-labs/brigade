"""Approved Grok Bot feed: local-only manifest validation and bounded apply."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from brigade import cli, grokbot_feed, grokbot_jobs


SECRET_INSTRUCTIONS = "SECRET_INSTRUCTION_DO_NOT_PRINT"
SECRET_VERIFY = "SECRET_VERIFY_CMD"
SECRET_OWNERSHIP = "SECRET_OWNERSHIP_PATH/file.py"


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
