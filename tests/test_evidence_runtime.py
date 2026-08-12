"""Tests for the evidence crawler runtime resolver and compatibility checks."""

from __future__ import annotations

import os
from pathlib import Path

from brigade import evidence_runtime


def _make_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    return bin_dir


def _write_script(path: Path, body: str) -> Path:
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return path


def _path_with(tmp_path: Path, bin_dir: Path) -> str:
    return f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"


def _discrawl_script(version: str = "0.8.0", database: str = "ok", capabilities: str = "version doctor export") -> str:
    return (
        f'if [ "$1" = "version" ]; then echo "{version}"; exit 0; fi\n'
        f'if [ "$1" = "--help" ]; then echo "Commands: {capabilities}"; exit 0; fi\n'
        f'if [ "$1" = "doctor" ] && [ "$2" = "--json" ]; then '
        f'echo \'{{"config":"ok","config_path":"/tmp/.discrawl/config.toml","database":"{database}",'
        f'"default_guild_id":"ok","discord_token":"ok","embeddings":"ok","fts":"ok","vector":"ok"}}\'; '
        f"exit 0; fi\n"
        "exit 1\n"
    )


def test_resolve_crawler_prefers_source_override(monkeypatch, tmp_path):
    bin_dir = _make_bin(tmp_path)
    _write_script(bin_dir / "discrawl", _discrawl_script())
    override_dir = tmp_path / "override"
    override_dir.mkdir()
    override = _write_script(override_dir / "discrawl", _discrawl_script(version="0.9.0"))
    monkeypatch.setenv("PATH", _path_with(tmp_path, bin_dir))
    monkeypatch.setenv("DISCORD_CRAWLER_BIN", str(override))

    runtime = evidence_runtime.resolve_crawler("discord")

    assert runtime is not None
    assert runtime.resolved_path == str(override)
    assert runtime.override == str(override)
    assert runtime.version == "0.9.0"


def test_resolve_crawler_falls_back_to_path(monkeypatch, tmp_path):
    bin_dir = _make_bin(tmp_path)
    default = _write_script(bin_dir / "discrawl", _discrawl_script())
    monkeypatch.setenv("PATH", _path_with(tmp_path, bin_dir))

    runtime = evidence_runtime.resolve_crawler("discord")

    assert runtime is not None
    assert runtime.resolved_path == str(default)
    assert runtime.override is None
    assert runtime.version == "0.8.0"
    assert "export" in runtime.capabilities


def test_resolve_crawler_generic_discrawl_bin_override(monkeypatch, tmp_path):
    bin_dir = _make_bin(tmp_path)
    _write_script(bin_dir / "discrawl", _discrawl_script())
    override_dir = tmp_path / "override"
    override_dir.mkdir()
    override = _write_script(override_dir / "mydiscrawl", _discrawl_script(version="0.9.0"))
    monkeypatch.setenv("PATH", _path_with(tmp_path, bin_dir))
    monkeypatch.setenv("DISCRAWL_BIN", str(override))

    runtime = evidence_runtime.resolve_crawler("discord")

    assert runtime is not None
    assert runtime.resolved_path == str(override)
    assert runtime.override == str(override)


def test_resolve_crawler_reports_missing_binary(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))

    runtime = evidence_runtime.resolve_crawler("discord")

    assert runtime is not None
    assert runtime.resolved_path is None
    assert runtime.error is not None
    assert "no executable" in runtime.error


def test_resolve_crawler_unknown_source_returns_none(tmp_path):
    env = {"PATH": str(tmp_path / "empty")}
    assert evidence_runtime.resolve_crawler("unknown_source", env=env) is None


def test_resolve_crawler_probes_capabilities_from_help(monkeypatch, tmp_path):
    bin_dir = _make_bin(tmp_path)
    _write_script(
        bin_dir / "discrawl",
        'if [ "$1" = "--help" ]; then echo "Commands: version doctor export"; exit 0; fi\n'
        'if [ "$1" = "version" ]; then echo "0.8.0"; exit 0; fi\n'
        "exit 1\n",
    )
    monkeypatch.setenv("PATH", _path_with(tmp_path, bin_dir))

    runtime = evidence_runtime.resolve_crawler("discord")

    assert runtime is not None
    assert set(runtime.capabilities) >= {"version", "doctor", "export"}


def test_check_compatibility_ok(monkeypatch, tmp_path):
    bin_dir = _make_bin(tmp_path)
    _write_script(bin_dir / "discrawl", _discrawl_script())
    monkeypatch.setenv("PATH", _path_with(tmp_path, bin_dir))
    runtime = evidence_runtime.resolve_crawler("discord")

    assert runtime is not None
    compat = evidence_runtime.check_compatibility(runtime)

    assert compat.state == "ok"
    assert compat.database == "ok"
    assert compat.config_path == "/tmp/.discrawl/config.toml"


def test_check_compatibility_fails_on_unreadable_archive(monkeypatch, tmp_path):
    bin_dir = _make_bin(tmp_path)
    _write_script(bin_dir / "discrawl", _discrawl_script(database="schema-too-new"))
    monkeypatch.setenv("PATH", _path_with(tmp_path, bin_dir))
    runtime = evidence_runtime.resolve_crawler("discord")

    assert runtime is not None
    compat = evidence_runtime.check_compatibility(runtime)

    assert compat.state == "fail"
    assert compat.database == "schema-too-new"
    assert "archive unreadable" in compat.detail
    assert "expected database='ok'" in compat.detail


def test_check_compatibility_fails_on_missing_capability(monkeypatch, tmp_path):
    bin_dir = _make_bin(tmp_path)
    _write_script(bin_dir / "discrawl", _discrawl_script(capabilities="version doctor"))
    monkeypatch.setenv("PATH", _path_with(tmp_path, bin_dir))
    runtime = evidence_runtime.resolve_crawler("discord")

    assert runtime is not None
    compat = evidence_runtime.check_compatibility(runtime)

    assert compat.state == "fail"
    assert "export" in compat.missing_capabilities
    assert "missing required capabilities" in compat.detail


def test_check_compatibility_fails_on_version_below_floor(monkeypatch, tmp_path):
    bin_dir = _make_bin(tmp_path)
    _write_script(bin_dir / "discrawl", _discrawl_script(version="0.7.0"))
    monkeypatch.setenv("PATH", _path_with(tmp_path, bin_dir))
    runtime = evidence_runtime.resolve_crawler("discord")

    assert runtime is not None
    compat = evidence_runtime.check_compatibility(runtime)

    assert compat.state == "fail"
    assert "version below floor" in compat.detail
    assert "expected >= 0.8.0" in compat.detail
    assert "observed 0.7.0" in compat.detail


def test_check_compatibility_warns_on_override_different_path(monkeypatch, tmp_path):
    bin_dir = _make_bin(tmp_path)
    _write_script(bin_dir / "discrawl", _discrawl_script())
    override_dir = tmp_path / "override"
    override_dir.mkdir()
    override = _write_script(override_dir / "discrawl", _discrawl_script(version="0.8.0"))
    monkeypatch.setenv("PATH", _path_with(tmp_path, bin_dir))
    monkeypatch.setenv("DISCORD_CRAWLER_BIN", str(override))
    runtime = evidence_runtime.resolve_crawler("discord")

    assert runtime is not None
    compat = evidence_runtime.check_compatibility(runtime)

    assert compat.state == "warn"
    assert "override binary" in compat.detail


def test_check_compatibility_override_drift_does_not_downgrade_version_fail(monkeypatch, tmp_path):
    # An override binary below the version floor must stay FAIL even though it
    # also drifts from the default path - override drift must never mask an
    # incompatible runtime, or _run_crawl would stop refusing it.
    bin_dir = _make_bin(tmp_path)
    _write_script(bin_dir / "discrawl", _discrawl_script(version="0.8.0"))
    override_dir = tmp_path / "override"
    override_dir.mkdir()
    override = _write_script(override_dir / "discrawl", _discrawl_script(version="0.7.0"))
    monkeypatch.setenv("PATH", _path_with(tmp_path, bin_dir))
    monkeypatch.setenv("DISCORD_CRAWLER_BIN", str(override))
    runtime = evidence_runtime.resolve_crawler("discord")

    assert runtime is not None
    compat = evidence_runtime.check_compatibility(runtime)

    assert compat.state == "fail"
    assert "version below floor" in compat.detail


# ---- memory projection (#844 F1) -------------------------------------------


def _miseledger_memory_script(
    *,
    version: str = "0.6.0",
    capability: str | None = "memory-projection.v1",
    doctor_exit: int = 0,
    database_ok: bool = True,
    malformed: bool = False,
    memory_health: str | None = None,
) -> str:
    cap_json = "null" if capability is None else f'"{capability}"'
    db_ok = "true" if database_ok else "false"
    health = memory_health or (
        '{"capability":"memory-projection.v1","engine_version":"%s","status":"absent",'
        '"last_completed_scan_id":"","canonical_count":0,"live_count":0,'
        '"hash_divergence":0,"unresolved_relations":0,"malformed_skipped":0,'
        '"stale":false,"partial":false}' % version
    )
    if malformed:
        doctor_body = 'echo "not-json"; exit 0'
    else:
        doctor_body = (
            f'echo \'{{"ok":{db_ok},"engine_version":"{version}",'
            f'"capability":{{"engine_version":"{version}","memory":{cap_json}}},'
            f'"memory_health":{health},'
            f'"checks":[{{"name":"database","ok":{db_ok},"detail":"db"}}]}}\'; '
            f"exit {doctor_exit}"
        )
    return (
        f'if [ "$1" = "version" ]; then echo "miseledger {version}"; exit 0; fi\n'
        f'if [ "$1" = "doctor" ] && [ "$2" = "--json" ]; then {doctor_body}; fi\n'
        "exit 1\n"
    )


def test_memory_probe_ok(monkeypatch, tmp_path):
    bin_dir = _make_bin(tmp_path)
    binary = _write_script(bin_dir / "miseledger", _miseledger_memory_script())
    monkeypatch.setenv("PATH", _path_with(tmp_path, bin_dir))

    probe = evidence_runtime.probe_memory_projection(str(binary))
    compat = evidence_runtime.check_memory_projection_preflight(probe)

    assert probe.capability == evidence_runtime.MEMORY_PROJECTION_CAPABILITY
    assert probe.archive_ok is True
    assert compat.state in {"ok", "warn"}


def test_memory_probe_missing_binary():
    probe = evidence_runtime.probe_memory_projection(None)
    compat = evidence_runtime.check_memory_projection_preflight(probe)

    assert compat.state == "fail"
    assert "not found" in compat.detail.lower() or "setup" in compat.detail.lower()


def test_memory_probe_absent_capability(monkeypatch, tmp_path):
    bin_dir = _make_bin(tmp_path)
    binary = _write_script(bin_dir / "miseledger", _miseledger_memory_script(capability=None))
    monkeypatch.setenv("PATH", _path_with(tmp_path, bin_dir))

    probe = evidence_runtime.probe_memory_projection(str(binary))
    compat = evidence_runtime.check_memory_projection_preflight(probe)

    assert compat.state == "fail"
    assert "absent capability" in compat.detail or evidence_runtime.MEMORY_PROJECTION_CAPABILITY in compat.detail


def test_memory_probe_future_capability(monkeypatch, tmp_path):
    bin_dir = _make_bin(tmp_path)
    binary = _write_script(
        bin_dir / "miseledger",
        _miseledger_memory_script(capability="memory-projection.v2"),
    )
    monkeypatch.setenv("PATH", _path_with(tmp_path, bin_dir))

    probe = evidence_runtime.probe_memory_projection(str(binary))
    compat = evidence_runtime.check_memory_projection_preflight(probe)

    assert compat.state == "fail"
    assert "incompatible capability" in compat.detail


def test_memory_probe_version_below_floor(monkeypatch, tmp_path):
    bin_dir = _make_bin(tmp_path)
    binary = _write_script(bin_dir / "miseledger", _miseledger_memory_script(version="0.5.0"))
    monkeypatch.setenv("PATH", _path_with(tmp_path, bin_dir))

    probe = evidence_runtime.probe_memory_projection(str(binary))
    compat = evidence_runtime.check_memory_projection_preflight(probe)

    assert compat.state == "fail"
    assert "version below floor" in compat.detail


def test_memory_probe_unreadable_archive(monkeypatch, tmp_path):
    bin_dir = _make_bin(tmp_path)
    binary = _write_script(
        bin_dir / "miseledger",
        _miseledger_memory_script(database_ok=False, doctor_exit=1),
    )
    monkeypatch.setenv("PATH", _path_with(tmp_path, bin_dir))

    probe = evidence_runtime.probe_memory_projection(str(binary))
    compat = evidence_runtime.check_memory_projection_preflight(probe)

    assert compat.state == "fail"
    assert "archive unreadable" in compat.detail


def test_archive_readable_rejects_exit1_absent_checks():
    # Fail-open blocker: exit 1 + capability without checks must not pass.
    ok, detail = evidence_runtime._archive_readable_from_doctor(
        {"ok": False, "capability": {"memory": "memory-projection.v1"}},
        1,
    )
    assert ok is False
    assert detail is not None
    assert "absent checks" in detail


def test_archive_readable_rejects_exit1_malformed_checks():
    ok, detail = evidence_runtime._archive_readable_from_doctor(
        {"ok": False, "checks": "not-a-list", "capability": {"memory": "memory-projection.v1"}},
        1,
    )
    assert ok is False
    assert detail is not None
    assert "malformed checks" in detail

    ok, detail = evidence_runtime._archive_readable_from_doctor(
        {"ok": False, "checks": [None, "x", {"name": "database"}], "capability": {"memory": "memory-projection.v1"}},
        1,
    )
    assert ok is False
    assert detail is not None
    assert "malformed checks" in detail or "absent archive-readability" in detail


def test_archive_readable_rejects_explicit_failing_archive_check():
    ok, detail = evidence_runtime._archive_readable_from_doctor(
        {
            "ok": False,
            "checks": [
                {"name": "paths", "ok": True, "detail": "/tmp/db"},
                {"name": "database", "ok": False, "detail": "open failed"},
            ],
        },
        1,
    )
    assert ok is False
    assert detail is not None
    assert "open failed" in detail or "database" in detail


def test_archive_readable_accepts_degraded_but_readable_exit1():
    # Legitimate degraded: archive opened (schema/fts ok) but memory_projection fails.
    ok, detail = evidence_runtime._archive_readable_from_doctor(
        {
            "ok": False,
            "capability": {"engine_version": "0.6.0", "memory": "memory-projection.v1"},
            "checks": [
                {"name": "paths", "ok": True, "detail": "/tmp/db"},
                {"name": "schema", "ok": True, "detail": "version 4"},
                {"name": "fts", "ok": True, "detail": "sqlite fts5"},
                {
                    "name": "memory_projection",
                    "ok": False,
                    "detail": "status=interrupted stale=true partial=true",
                },
            ],
        },
        1,
    )
    assert ok is True
    assert detail is None


def test_memory_probe_exit1_absent_checks_refuses_preflight(monkeypatch, tmp_path):
    bin_dir = _make_bin(tmp_path)
    binary = _write_script(
        bin_dir / "miseledger",
        (
            'if [ "$1" = "version" ]; then echo "miseledger 0.6.0"; exit 0; fi\n'
            'if [ "$1" = "doctor" ] && [ "$2" = "--json" ]; then '
            'echo \'{"ok":false,"capability":{"engine_version":"0.6.0","memory":"memory-projection.v1"}}\'; '
            "exit 1; fi\n"
            "exit 1\n"
        ),
    )
    monkeypatch.setenv("PATH", _path_with(tmp_path, bin_dir))

    probe = evidence_runtime.probe_memory_projection(str(binary))
    compat = evidence_runtime.check_memory_projection_preflight(probe)

    assert probe.archive_ok is False
    assert compat.state == "fail"
    assert "absent checks" in compat.detail or "archive unreadable" in compat.detail


def test_memory_probe_degraded_memory_health_still_preflight_ok(monkeypatch, tmp_path):
    bin_dir = _make_bin(tmp_path)
    binary = _write_script(
        bin_dir / "miseledger",
        (
            'if [ "$1" = "version" ]; then echo "miseledger 0.6.0"; exit 0; fi\n'
            'if [ "$1" = "doctor" ] && [ "$2" = "--json" ]; then '
            'echo \'{"ok":false,"engine_version":"0.6.0",'
            '"capability":{"engine_version":"0.6.0","memory":"memory-projection.v1"},'
            '"checks":['
            '{"name":"paths","ok":true,"detail":"/tmp/db"},'
            '{"name":"schema","ok":true,"detail":"version 4"},'
            '{"name":"fts","ok":true,"detail":"sqlite fts5"},'
            '{"name":"memory_projection","ok":false,"detail":"stale=true"}'
            "]}'; exit 1; fi\n"
            "exit 1\n"
        ),
    )
    monkeypatch.setenv("PATH", _path_with(tmp_path, bin_dir))

    probe = evidence_runtime.probe_memory_projection(str(binary))
    compat = evidence_runtime.check_memory_projection_preflight(probe)

    assert probe.archive_ok is True
    assert compat.state in {"ok", "warn"}


def test_memory_probe_malformed(monkeypatch, tmp_path):
    bin_dir = _make_bin(tmp_path)
    binary = _write_script(bin_dir / "miseledger", _miseledger_memory_script(malformed=True))
    monkeypatch.setenv("PATH", _path_with(tmp_path, bin_dir))

    probe = evidence_runtime.probe_memory_projection(str(binary))
    compat = evidence_runtime.check_memory_projection_preflight(probe)

    assert compat.state == "fail"
    assert "malformed" in compat.detail


def test_memory_projection_healthy_requires_failed_zero():
    assert evidence_runtime.memory_projection_is_healthy(
        capability="memory-projection.v1",
        status="completed",
        stale=False,
        partial=False,
        failed=0,
        engine_ok=True,
    )
    assert not evidence_runtime.memory_projection_is_healthy(
        capability="memory-projection.v1",
        status="completed",
        stale=False,
        partial=False,
        failed=1,
        engine_ok=True,
    )
    assert not evidence_runtime.memory_projection_is_healthy(
        capability="memory-projection.v1",
        status="completed",
        stale=True,
        partial=False,
        failed=0,
        engine_ok=True,
    )


def test_classify_memory_crawl_status_partial_and_counts():
    receipt = {
        "capability": "memory-projection.v1",
        "status": "completed",
        "stale": False,
        "partial": False,
        "failed": 2,
        "scan_id": "scan-1",
        "created": 1,
        "updated": 2,
        "unchanged": 3,
        "removed": 4,
        "skipped": 5,
    }
    assert evidence_runtime.classify_memory_crawl_status(exit_code=0, receipt=receipt) == "partial"
    counts = evidence_runtime.extract_memory_crawl_counts(receipt)
    assert counts == {
        "scan_id": "scan-1",
        "created": 1,
        "updated": 2,
        "unchanged": 3,
        "removed": 4,
        "skipped": 5,
        "failed": 2,
    }


def test_parse_memory_receipt_fail_closed_missing_fields():
    incomplete = {
        "capability": "memory-projection.v1",
        "status": "completed",
        "stale": False,
        "partial": False,
        "scan_id": "scan-1",
        "created": 1,
        # missing updated/unchanged/removed/skipped/failed
    }
    status, counts, detail = evidence_runtime.parse_memory_crawl_receipt(incomplete)
    assert status == "fail"
    assert counts is None
    assert detail is not None
    assert "updated" in detail
    assert evidence_runtime.extract_memory_crawl_counts(incomplete) is None


def test_parse_memory_receipt_rejects_bool_and_string_counts():
    receipt = {
        "capability": "memory-projection.v1",
        "status": "completed",
        "stale": False,
        "partial": False,
        "scan_id": "scan-1",
        "created": True,  # bool must not coerce to 1
        "updated": 0,
        "unchanged": 0,
        "removed": 0,
        "skipped": 0,
        "failed": 0,
    }
    status, counts, detail = evidence_runtime.parse_memory_crawl_receipt(receipt)
    assert status == "fail"
    assert counts is None
    assert detail is not None

    receipt["created"] = "0"
    status, counts, _detail = evidence_runtime.parse_memory_crawl_receipt(receipt)
    assert status == "fail"
    assert counts is None


def test_parse_memory_receipt_dry_run_is_non_current():
    receipt = {
        "capability": "memory-projection.v1",
        "dry_run": True,
        "skipped": 1,
        "failed": 0,
    }
    status, counts, detail = evidence_runtime.parse_memory_crawl_receipt(receipt, dry_run=True)
    assert status == "dry_run"
    assert counts is None
    assert detail is not None
    assert evidence_runtime.classify_memory_crawl_status(exit_code=0, receipt=receipt, dry_run=True) == "dry_run"


def test_parse_memory_options_rejects_rebuild_and_full():
    err, _want_json, _dry_run, passthrough = evidence_runtime.parse_memory_crawl_options(["--rebuild"])
    assert err is None
    assert passthrough == ["--rebuild"]
    err, *_rest = evidence_runtime.parse_memory_crawl_options(["--full"])
    assert err is not None
    assert "--full" in err


def test_public_memory_latest_run_whitelist():
    raw = {
        "status": "ok",
        "exit_code": 0,
        "capability": "memory-projection.v1",
        "scan_id": "scan-1",
        "created": 1,
        "workspace": "/abs/secret/workspace",
        "detail": "stderr leak",
        "text": "CARD BODY",
        "raw": {"body": "nope"},
        "resolved_path": "/abs/bin/miseledger",
        "database": "ok",
    }
    public = evidence_runtime.public_memory_latest_run(raw)
    assert public is not None
    assert public["status"] == "ok"
    assert public["scan_id"] == "scan-1"
    assert public["database"] == "ok"
    assert "workspace" not in public
    assert "detail" not in public
    assert "text" not in public
    assert "raw" not in public
    assert "resolved_path" not in public


def test_body_free_memory_health_strips_bodies():
    raw = {
        "capability": "memory-projection.v1",
        "engine_version": "0.6.0",
        "status": "completed",
        "text": "SECRET CARD BODY",
        "raw": {"path": "memory/cards/x.md", "body": "nope"},
        "canonical_count": 3,
    }
    cleaned = evidence_runtime.body_free_memory_health(raw)
    assert cleaned is not None
    assert "text" not in cleaned
    assert "raw" not in cleaned
    assert cleaned["canonical_count"] == 3


def test_register_memory_facade_extensions_is_inert():
    assert evidence_runtime.register_memory_facade_extensions(None) is None


def test_known_sources_excludes_memory():
    assert "memory" not in evidence_runtime.known_sources()
    assert "discord" in evidence_runtime.known_sources()
