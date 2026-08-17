"""Tests for the evidence station CLI (MiseLedger process-boundary sidecar)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from brigade import code_cmd, doctor as doctor_mod, evidence_cmd, search_cmd
from brigade.station import DoctorContext


def test_evidence_status_reports_uninstalled(monkeypatch, tmp_path):
    monkeypatch.setattr(evidence_cmd.evidence_brief, "_miseledger_bin", lambda: None)

    payload = evidence_cmd.status_payload(tmp_path)

    assert payload["installed"] is False
    assert payload["health"] == "missing"
    assert "brigade setup" in payload["summary"]
    assert "brigade evidence crawl plan" in payload["next_commands"]
    assert payload["pipeline"][0].startswith("evidence crawl")


def test_evidence_status_distinguishes_explicit_execution_from_review_only_plans(monkeypatch, tmp_path):
    monkeypatch.setattr(evidence_cmd.evidence_brief, "_miseledger_bin", lambda: None)

    payload = evidence_cmd.status_payload(tmp_path)
    boundaries = payload["boundaries"]

    assert (
        "Explicit user-invoked `brigade evidence crawl` and `brigade evidence search` execute the evidence engine across a process boundary."
        in boundaries
    )
    assert (
        "Review-only `brigade evidence crawl plan` and `brigade evidence export plan` never execute the engine."
        in boundaries
    )
    assert "Brigade does not start daemons or upload data; receipt export remains local." in boundaries
    assert "Brigade does not crawl sessions or import adapter JSONL from these commands." not in boundaries
    assert "only: they do not crawl sessions" not in (evidence_cmd.__doc__ or "")
    repo_root = Path(__file__).parents[1]
    quickstart = (repo_root / "QUICKSTART.md").read_text()
    station_contract = (repo_root / "docs" / "station-contract.md").read_text()
    search_docstring = search_cmd.__doc__ or ""
    evidence_docstring = evidence_cmd.__doc__ or ""

    assert "brigade evidence crawl sessions" in quickstart
    assert "brigade evidence search" in quickstart
    assert "brigade evidence crawl plan     # review-only" in quickstart
    assert "brigade evidence export plan    # review-only" in quickstart
    assert "brigade code sync .             # preferred GraphTrail facade" in quickstart
    assert "brigade search sync|context|impact` remain compatibility aliases" in quickstart
    assert "runs the local evidence engine for `brigade evidence crawl|search`" in station_contract
    assert "crawl/export plans are review-only" in station_contract
    assert "Explicit user-invoked" in evidence_docstring
    assert "brigade code sync|context|impact" in search_docstring
    assert "compatibility aliases" in search_docstring
    assert "sync plan`` stays\nreview-only" in search_docstring
    for text in (quickstart, station_contract, search_docstring, evidence_docstring):
        assert "does not crawl for you" not in text


def test_code_run_relays_child_stderr_unchanged(monkeypatch, capsys):
    monkeypatch.setattr(code_cmd.context_cmd, "_graphtrail_bin", lambda: "/x/graphtrail")
    monkeypatch.setattr(code_cmd.proc, "run", lambda *_, **__: code_cmd.proc.Result(7, "", "graph warning\\n"))

    assert code_cmd.run("impact", ["brigade.cli.main"]) == 7
    assert capsys.readouterr().err == "graph warning\\n"


def test_evidence_run_engine_relays_child_stderr_unchanged(monkeypatch, capsys):
    monkeypatch.setattr(evidence_cmd.evidence_brief, "_miseledger_bin", lambda: "/x/miseledger")
    monkeypatch.setattr(evidence_cmd.proc, "run", lambda *_, **__: evidence_cmd.proc.Result(6, "", "ledger warning\\n"))

    assert evidence_cmd.run_engine("search", ["needle"]) == 6
    assert capsys.readouterr().err == "ledger warning\\n"


def test_evidence_status_ok_with_status_json(monkeypatch, tmp_path):
    monkeypatch.setattr(evidence_cmd.evidence_brief, "_miseledger_bin", lambda: "/x/miseledger")

    def fake_run(args, **kw):
        if args[:2] == ["/x/miseledger", "status"]:
            return evidence_cmd.proc.Result(0, json.dumps({"items": 12, "sources": ["sessions"]}), "")
        if args[:2] == ["/x/miseledger", "doctor"]:
            return evidence_cmd.proc.Result(0, json.dumps({"fail_count": 0, "warn_count": 0, "checks": []}), "")
        raise AssertionError(args)

    monkeypatch.setattr(evidence_cmd.proc, "run", fake_run)

    payload = evidence_cmd.status_payload(tmp_path)

    assert payload["installed"] is True
    assert payload["health"] == "ok"
    assert "items=12" in payload["summary"]
    assert any("receipts export miseledger" in cmd for cmd in payload["next_commands"])


def test_evidence_summary_status_skips_doctor(monkeypatch, tmp_path):
    monkeypatch.setattr(evidence_cmd.evidence_brief, "_miseledger_bin", lambda: "/x/miseledger")
    calls = []

    def fake_run_json(args, *, timeout):
        calls.append((args, timeout))
        return {
            "command": args,
            "exit_code": 0,
            "stdout_json": {"items": 12},
            "stdout_unparsed": None,
            "stderr": "",
        }

    monkeypatch.setattr(evidence_cmd, "_run_json", fake_run_json)

    payload = evidence_cmd.status_payload(tmp_path, include_doctor=False, timeout=5.0)

    assert payload["health"] == "ok"
    assert calls == [(["/x/miseledger", "status", "--json"], 5.0)]
    assert payload["doctor"] is None


def test_evidence_doctor_exits_nonzero_on_fail(monkeypatch, tmp_path):
    monkeypatch.setattr(evidence_cmd.evidence_brief, "_miseledger_bin", lambda: "/x/miseledger")

    def fake_run(args, **kw):
        if args[:2] == ["/x/miseledger", "status"]:
            return evidence_cmd.proc.Result(0, json.dumps({"items": 1}), "")
        if args[:2] == ["/x/miseledger", "doctor"]:
            return evidence_cmd.proc.Result(
                1,
                json.dumps(
                    {
                        "fail_count": 2,
                        "warn_count": 0,
                        "checks": [{"status": "FAIL", "name": "fts", "detail": "missing"}],
                    }
                ),
                "",
            )
        raise AssertionError(args)

    monkeypatch.setattr(evidence_cmd.proc, "run", fake_run)
    assert evidence_cmd.doctor(target=tmp_path) == 1


def test_crawl_plan_is_review_only(tmp_path):
    payload = evidence_cmd.crawl_plan_payload(target=tmp_path)
    rendered = evidence_cmd._render_plan_md(payload)

    assert ["miseledger", "crawl", "sessions"] in payload["commands"]
    assert "review-only crawl plan never executes the evidence engine" in payload["boundaries"][0]
    assert "miseledger crawl sessions" in rendered


def test_crawl_plan_write_creates_files(tmp_path):
    rc = evidence_cmd.crawl_plan(target=tmp_path, write=True, json_output=True)
    assert rc == 0
    plans = list((tmp_path / ".brigade" / "evidence" / "plans").glob("*/plan.json"))
    assert len(plans) == 1
    assert (plans[0].parent / "PLAN.md").exists()


def test_export_plan_points_at_receipts_export(tmp_path):
    payload = evidence_cmd.export_plan_payload(target=tmp_path)
    commands = [" ".join(cmd) for cmd in payload["commands"]]
    assert any("receipts export miseledger" in cmd for cmd in commands)
    assert payload["export_cursor_present"] is False


def test_memory_crawl_rebuild_delegates_only_after_preflight(monkeypatch, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    marker = tmp_path / "called"
    binary = tmp_path / "miseledger"
    binary.write_text(
        """#!/bin/sh
if [ \"$1\" = \"version\" ]; then echo '0.6.0'; exit 0; fi
if [ \"$1\" = \"doctor\" ]; then echo '{\"checks\":[{\"name\":\"schema\",\"ok\":true}],\"capability\":{\"memory\":\"memory-projection.v1\",\"engine_version\":\"0.6.0\"}}'; exit 0; fi
if [ \"$1\" = \"crawl\" ]; then touch '"""
        + str(marker)
        + """'; echo '{\"scan_id\":\"scan-rebuild\",\"capability\":\"memory-projection.v1\",\"engine_version\":\"0.6.0\",\"status\":\"completed\",\"stale\":false,\"partial\":false,\"created\":0,\"updated\":0,\"unchanged\":1,\"removed\":0,\"skipped\":0,\"failed\":0}'; exit 0; fi
exit 1
"""
    )
    binary.chmod(0o755)
    monkeypatch.setattr(evidence_cmd.evidence_brief, "_miseledger_bin", lambda: str(binary))

    assert evidence_cmd.run_engine("crawl", ["memory", str(workspace), "--rebuild"]) == 0

    assert marker.exists()


# ---- crawler runtime and health propagation tests --------------------------


def _discrawl_script(version: str = "0.8.0", database: str = "ok", capabilities: str = "version doctor export") -> str:
    return f'''if [ "$1" = "version" ]; then echo "{version}"; exit 0; fi
if [ "$1" = "--help" ]; then echo "Commands: {capabilities}"; exit 0; fi
if [ "$1" = "doctor" ] && [ "$2" = "--json" ]; then echo '{{"config":"ok","config_path":"/tmp/.discrawl/config.toml","database":"{database}","default_guild_id":"ok","discord_token":"ok","embeddings":"ok","fts":"ok","vector":"ok"}}'; exit 0; fi
exit 1
'''


def _write_fake_bin(tmp_path: Path, name: str, body: str) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    path = bin_dir / name
    path.write_text(f"#!/bin/sh\n{body}")
    path.chmod(0o755)
    return path


def _path_with_bin(tmp_path: Path) -> str:
    return f"{tmp_path / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}"


def _miseledger_ok_script() -> str:
    return (
        'if [ "$1" = "status" ] && [ "$2" = "--json" ]; then echo \'{"items":0}\'; exit 0; fi\n'
        'if [ "$1" = "doctor" ] && [ "$2" = "--json" ]; then echo \'{"fail_count":0,"warn_count":0,"checks":[]}\'; exit 0; fi\n'
        "exit 1\n"
    )


def test_crawl_refuses_unreadable_archive(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _write_fake_bin(tmp_path, "discrawl", _discrawl_script(database="schema-too-new"))
    marker = tmp_path / "miseledger_invoked.txt"
    miseledger = _write_fake_bin(
        tmp_path,
        "miseledger",
        f'echo "invoked" > "{marker}"\nexit 0\n',
    )
    monkeypatch.setenv("PATH", _path_with_bin(tmp_path))
    monkeypatch.setattr(evidence_cmd.evidence_brief, "_miseledger_bin", lambda: str(miseledger))

    rc = evidence_cmd.run_engine("crawl", ["discord"])

    assert rc == 1
    assert not marker.exists()
    last_run = evidence_cmd._read_last_run(tmp_path, "discord")
    assert last_run is not None
    assert last_run["status"] == "fail"
    assert "schema-too-new" in last_run["detail"]


def test_crawl_gate_is_case_insensitive(monkeypatch, tmp_path):
    # A differently-cased source (e.g. "Discord") must not bypass the
    # compatibility gate; it is normalized to the "discord" contract and refused.
    monkeypatch.chdir(tmp_path)
    _write_fake_bin(tmp_path, "discrawl", _discrawl_script(database="schema-too-new"))
    marker = tmp_path / "miseledger_invoked.txt"
    miseledger = _write_fake_bin(
        tmp_path,
        "miseledger",
        f'echo "invoked" > "{marker}"\nexit 0\n',
    )
    monkeypatch.setenv("PATH", _path_with_bin(tmp_path))
    monkeypatch.setattr(evidence_cmd.evidence_brief, "_miseledger_bin", lambda: str(miseledger))

    rc = evidence_cmd.run_engine("crawl", ["Discord"])

    assert rc == 1
    assert not marker.exists()
    last_run = evidence_cmd._read_last_run(tmp_path, "discord")
    assert last_run is not None
    assert last_run["status"] == "fail"


def test_crawl_delegates_with_crawler_dir_on_path(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _write_fake_bin(tmp_path, "discrawl", _discrawl_script())
    args_file = tmp_path / "miseledger_args.txt"
    path_file = tmp_path / "miseledger_path.txt"
    miseledger = _write_fake_bin(
        tmp_path,
        "miseledger",
        (
            f'echo "$*" > "{args_file}"\n'
            f'echo "$PATH" > "{path_file}"\n'
            'if [ "$1" = "crawl" ]; then echo "crawled"; exit 0; fi\n'
            "exit 1\n"
        ),
    )
    monkeypatch.setenv("PATH", _path_with_bin(tmp_path))
    monkeypatch.setattr(evidence_cmd.evidence_brief, "_miseledger_bin", lambda: str(miseledger))

    rc = evidence_cmd.run_engine("crawl", ["discord"])

    assert rc == 0
    assert args_file.read_text().strip() == "crawl discord"
    recorded_path = path_file.read_text().strip()
    assert recorded_path.startswith(str(tmp_path / "bin"))
    last_run = evidence_cmd._read_last_run(tmp_path, "discord")
    assert last_run is not None
    assert last_run["status"] == "ok"
    assert last_run["exit_code"] == 0


def test_crawl_propagates_nonzero_exit(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _write_fake_bin(tmp_path, "discrawl", _discrawl_script())
    miseledger = _write_fake_bin(
        tmp_path,
        "miseledger",
        'if [ "$1" = "crawl" ]; then echo "boom" >&2; exit 5; fi\nexit 1\n',
    )
    monkeypatch.setenv("PATH", _path_with_bin(tmp_path))
    monkeypatch.setattr(evidence_cmd.evidence_brief, "_miseledger_bin", lambda: str(miseledger))

    rc = evidence_cmd.run_engine("crawl", ["discord"])

    assert rc == 5
    last_run = evidence_cmd._read_last_run(tmp_path, "discord")
    assert last_run is not None
    assert last_run["status"] == "fail"
    assert last_run["exit_code"] == 5


def test_status_reports_unhealthy_after_failed_last_run(monkeypatch, tmp_path):
    evidence_cmd._write_last_run(
        target=tmp_path,
        source="discord",
        status="fail",
        exit_code=5,
        crawler_version="0.8.0",
        database="ok",
        started_at="2026-01-01T00:00:00Z",
        finished_at="2026-01-01T00:01:00Z",
        detail="producer failed",
    )
    _write_fake_bin(tmp_path, "discrawl", _discrawl_script())
    miseledger = _write_fake_bin(tmp_path, "miseledger", _miseledger_ok_script())
    monkeypatch.setenv("PATH", _path_with_bin(tmp_path))
    monkeypatch.setattr(evidence_cmd.evidence_brief, "_miseledger_bin", lambda: str(miseledger))

    payload = evidence_cmd.status_payload(tmp_path)

    assert payload["health"] == "fail"
    assert payload["crawlers"]["discord"]["latest_run"]["status"] == "fail"
    assert "producer failed" in payload["crawlers"]["discord"]["latest_run"]["detail"]


def test_status_crawler_block_reflects_override(monkeypatch, tmp_path):
    _write_fake_bin(tmp_path, "discrawl", _discrawl_script())
    override_dir = tmp_path / "override"
    override_dir.mkdir()
    override = override_dir / "discrawl"
    override.write_text(f"#!/bin/sh\n{_discrawl_script()}")
    override.chmod(0o755)
    monkeypatch.setenv("PATH", _path_with_bin(tmp_path))
    monkeypatch.setenv("DISCORD_CRAWLER_BIN", str(override))
    miseledger = _write_fake_bin(tmp_path, "miseledger", _miseledger_ok_script())
    monkeypatch.setattr(evidence_cmd.evidence_brief, "_miseledger_bin", lambda: str(miseledger))

    payload = evidence_cmd.status_payload(tmp_path, include_doctor=False)

    assert "crawlers" in payload
    assert payload["crawlers"]["discord"]["override"] == str(override)
    assert payload["crawlers"]["discord"]["compatibility"]["state"] == "warn"


def test_doctor_exits_one_on_crawler_fail(monkeypatch, tmp_path):
    evidence_cmd._write_last_run(
        target=tmp_path,
        source="discord",
        status="fail",
        exit_code=5,
        crawler_version="0.8.0",
        database="ok",
        started_at="2026-01-01T00:00:00Z",
        finished_at="2026-01-01T00:01:00Z",
        detail="producer failed",
    )
    _write_fake_bin(tmp_path, "discrawl", _discrawl_script())
    miseledger = _write_fake_bin(tmp_path, "miseledger", _miseledger_ok_script())
    monkeypatch.setenv("PATH", _path_with_bin(tmp_path))
    monkeypatch.setattr(evidence_cmd.evidence_brief, "_miseledger_bin", lambda: str(miseledger))

    rc = evidence_cmd.doctor(target=tmp_path, json_output=True)

    assert rc == 1


def test_workspace_doctor_evidence_station_is_advisory():
    ctx = DoctorContext(target=Path("/tmp"), selection=None, harnesses=["claude"])
    assert doctor_mod.evidence_station_checks(ctx) == []


def test_status_doctor_json_backward_compat(monkeypatch, tmp_path):
    _write_fake_bin(tmp_path, "discrawl", _discrawl_script())
    body = (
        'if [ "$1" = "status" ] && [ "$2" = "--json" ]; then echo \'{"items":12}\'; exit 0; fi\n'
        'if [ "$1" = "doctor" ] && [ "$2" = "--json" ]; then echo \'{"fail_count":0,"warn_count":0,"checks":[]}\'; exit 0; fi\n'
        "exit 1\n"
    )
    miseledger = _write_fake_bin(tmp_path, "miseledger", body)
    monkeypatch.setenv("PATH", _path_with_bin(tmp_path))
    monkeypatch.setattr(evidence_cmd.evidence_brief, "_miseledger_bin", lambda: str(miseledger))

    payload = evidence_cmd.status_payload(tmp_path)

    assert payload["health"] == "ok"
    assert payload.get("crawlers") is None
    assert payload["health"] in {"ok", "warn", "fail", "incomplete", "unwired", "timeout", "missing"}


def test_newer_than_supported_archive_schema_refuses_crawl(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _write_fake_bin(tmp_path, "discrawl", _discrawl_script(database="schema-too-new"))
    marker = tmp_path / "miseledger_invoked.txt"
    miseledger = _write_fake_bin(
        tmp_path,
        "miseledger",
        f'echo "invoked" > "{marker}"\nexit 0\n',
    )
    monkeypatch.setenv("PATH", _path_with_bin(tmp_path))
    monkeypatch.setattr(evidence_cmd.evidence_brief, "_miseledger_bin", lambda: str(miseledger))

    rc = evidence_cmd.run_engine("crawl", ["discord"])

    assert rc == 1
    assert not marker.exists()
    assert evidence_cmd._read_last_run(tmp_path, "discord")["status"] == "fail"


def test_stale_cleanup_queue_after_producer_failure(monkeypatch, tmp_path):
    # NO_PENDING queue after a failed ingest must not read as healthy.
    evidence_cmd._write_last_run(
        target=tmp_path,
        source="discord",
        status="fail",
        exit_code=5,
        crawler_version="0.8.0",
        database="ok",
        started_at="2026-01-01T00:00:00Z",
        finished_at="2026-01-01T00:01:00Z",
        detail="ingest failed",
    )
    _write_fake_bin(tmp_path, "discrawl", _discrawl_script())
    miseledger = _write_fake_bin(tmp_path, "miseledger", _miseledger_ok_script())
    monkeypatch.setenv("PATH", _path_with_bin(tmp_path))
    monkeypatch.setattr(evidence_cmd.evidence_brief, "_miseledger_bin", lambda: str(miseledger))

    payload = evidence_cmd.status_payload(tmp_path)

    assert payload["health"] == "fail"
    assert payload["crawlers"]["discord"]["latest_run"]["status"] == "fail"


# ---- memory projection crawl (#844 F1) -------------------------------------


def _miseledger_memory_capable_script(
    *,
    version: str = "0.6.0",
    capability: str | None = "memory-projection.v1",
    crawl_receipt: str | None = None,
    crawl_exit: int = 0,
    marker: Path | None = None,
    database_ok: bool = True,
) -> str:
    cap_json = "null" if capability is None else f'"{capability}"'
    db_ok = "true" if database_ok else "false"
    receipt = crawl_receipt or (
        '{"scan_id":"scan-abc","capability":"memory-projection.v1","engine_version":"%s",'
        '"status":"completed","stale":false,"partial":false,'
        '"created":1,"updated":2,"unchanged":3,"removed":4,"skipped":5,"failed":0,'
        '"canonical_count":10,"live_count":9,"hash_divergence":0,'
        '"unresolved_relations":1,"malformed_skipped":5,'
        '"memory_health":{"capability":"memory-projection.v1","engine_version":"%s",'
        '"status":"completed","stale":false,"partial":false,'
        '"last_completed_scan_id":"scan-abc","canonical_count":10,"live_count":9,'
        '"hash_divergence":0,"unresolved_relations":1,"malformed_skipped":5}}' % (version, version)
    )
    mark = f'echo invoked > "{marker}"\n' if marker is not None else ""
    return f'''
if [ "$1" = "version" ]; then echo "miseledger {version}"; exit 0; fi
if [ "$1" = "doctor" ] && [ "$2" = "--json" ]; then
  echo '{{"ok":{db_ok},"engine_version":"{version}","capability":{{"engine_version":"{version}","memory":{cap_json}}},"checks":[{{"name":"database","ok":{db_ok},"detail":"db"}}],"fail_count":0,"warn_count":0}}'
  exit 0
fi
if [ "$1" = "status" ] && [ "$2" = "--json" ]; then
  echo '{{"items":0,"capability":{{"engine_version":"{version}","memory":{cap_json}}},"engine_version":"{version}"}}'
  exit 0
fi
if [ "$1" = "crawl" ] && [ "$2" = "memory" ]; then
  {mark}echo '{receipt}'
  exit {crawl_exit}
fi
if [ "$1" = "show" ]; then
  echo '{{"id":"item-1","source":{{"kind":"brigade-memory"}},"collection":{{"external_id":"memory-11111111-1111-4111-8111-111111111111"}},"content_hash":"sha256:abc","raw_hash":"sha256:def","trust_label":"local","injection":"none","live":true,"tombstoned":false,"relations":[{{"type":"derived_from","target_source_kind":"brigade","target_collection_external_id":"brigade:receipts","target_external_id":"receipt:1","target_item_id":null}}]}}'
  exit 0
fi
exit 1
'''


def test_memory_crawl_delegates_when_capable(monkeypatch, tmp_path, capsys):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    marker = tmp_path / "invoked.txt"
    miseledger = _write_fake_bin(
        tmp_path,
        "miseledger",
        _miseledger_memory_capable_script(marker=marker),
    )
    monkeypatch.setenv("PATH", _path_with_bin(tmp_path))
    monkeypatch.setattr(evidence_cmd.evidence_brief, "_miseledger_bin", lambda: str(miseledger))

    rc = evidence_cmd.run_engine("crawl", ["memory", str(workspace)])

    assert rc == 0
    assert marker.exists()
    out = capsys.readouterr().out
    assert "scan=scan-abc" in out
    assert "created=1" in out
    assert "updated=2" in out
    assert "unchanged=3" in out
    assert "removed=4" in out
    assert "skipped=5" in out
    assert "failed=0" in out
    last_run = evidence_cmd._read_last_run(workspace, "memory")
    assert last_run is not None
    assert last_run["status"] == "ok"
    assert last_run["scan_id"] == "scan-abc"
    assert last_run["created"] == 1
    assert last_run["updated"] == 2
    assert last_run["unchanged"] == 3
    assert last_run["removed"] == 4
    assert last_run["skipped"] == 5
    assert last_run["failed"] == 0
    assert last_run["capability"] == "memory-projection.v1"


def test_memory_crawl_refuses_missing_binary(monkeypatch, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setattr(evidence_cmd.evidence_brief, "_miseledger_bin", lambda: None)

    rc = evidence_cmd.run_engine("crawl", ["memory", str(workspace)])

    assert rc == 1
    last_run = evidence_cmd._read_last_run(workspace, "memory")
    assert last_run is not None
    assert last_run["status"] == "fail"
    assert last_run.get("preflight") == "refused"


def test_memory_crawl_refuses_absent_capability(monkeypatch, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    marker = tmp_path / "invoked.txt"
    miseledger = _write_fake_bin(
        tmp_path,
        "miseledger",
        _miseledger_memory_capable_script(capability=None, marker=marker),
    )
    monkeypatch.setattr(evidence_cmd.evidence_brief, "_miseledger_bin", lambda: str(miseledger))

    rc = evidence_cmd.run_engine("crawl", ["memory", str(workspace)])

    assert rc == 1
    assert not marker.exists()
    assert evidence_cmd._read_last_run(workspace, "memory")["status"] == "fail"


def test_memory_crawl_refuses_future_capability(monkeypatch, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    marker = tmp_path / "invoked.txt"
    miseledger = _write_fake_bin(
        tmp_path,
        "miseledger",
        _miseledger_memory_capable_script(capability="memory-projection.v99", marker=marker),
    )
    monkeypatch.setattr(evidence_cmd.evidence_brief, "_miseledger_bin", lambda: str(miseledger))

    rc = evidence_cmd.run_engine("crawl", ["memory", str(workspace)])

    assert rc == 1
    assert not marker.exists()


def test_memory_crawl_refuses_old_version(monkeypatch, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    marker = tmp_path / "invoked.txt"
    miseledger = _write_fake_bin(
        tmp_path,
        "miseledger",
        _miseledger_memory_capable_script(version="0.5.9", marker=marker),
    )
    monkeypatch.setattr(evidence_cmd.evidence_brief, "_miseledger_bin", lambda: str(miseledger))

    rc = evidence_cmd.run_engine("crawl", ["memory", str(workspace)])

    assert rc == 1
    assert not marker.exists()


def test_memory_crawl_refuses_unreadable_archive(monkeypatch, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    marker = tmp_path / "invoked.txt"
    miseledger = _write_fake_bin(
        tmp_path,
        "miseledger",
        _miseledger_memory_capable_script(database_ok=False, marker=marker),
    )
    monkeypatch.setattr(evidence_cmd.evidence_brief, "_miseledger_bin", lambda: str(miseledger))

    rc = evidence_cmd.run_engine("crawl", ["memory", str(workspace)])

    assert rc == 1
    assert not marker.exists()


def test_memory_crawl_refuses_exit1_doctor_without_checks(monkeypatch, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    marker = tmp_path / "invoked.txt"
    miseledger = _write_fake_bin(
        tmp_path,
        "miseledger",
        (
            'if [ "$1" = "version" ]; then echo "miseledger 0.6.0"; exit 0; fi\n'
            'if [ "$1" = "doctor" ] && [ "$2" = "--json" ]; then '
            'echo \'{"ok":false,"capability":{"engine_version":"0.6.0","memory":"memory-projection.v1"}}\'; '
            "exit 1; fi\n"
            f'if [ "$1" = "crawl" ]; then echo invoked > "{marker}"; exit 0; fi\n'
            "exit 1\n"
        ),
    )
    monkeypatch.setattr(evidence_cmd.evidence_brief, "_miseledger_bin", lambda: str(miseledger))

    rc = evidence_cmd.run_engine("crawl", ["memory", str(workspace)])

    assert rc == 1
    assert not marker.exists()
    last_run = evidence_cmd._read_last_run(workspace, "memory")
    assert last_run is not None
    assert last_run["status"] == "fail"
    assert last_run.get("preflight") == "refused"


def test_memory_crawl_partial_failed_not_healthy(monkeypatch, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    receipt = (
        '{"scan_id":"scan-partial","capability":"memory-projection.v1","engine_version":"0.6.0",'
        '"status":"completed","stale":false,"partial":false,'
        '"created":0,"updated":0,"unchanged":0,"removed":0,"skipped":1,"failed":2,'
        '"canonical_count":1,"live_count":0,"hash_divergence":0,'
        '"unresolved_relations":0,"malformed_skipped":3,'
        '"memory_health":{"capability":"memory-projection.v1","engine_version":"0.6.0",'
        '"status":"completed","stale":false,"partial":false,"failed":2,'
        '"last_completed_scan_id":"scan-partial","canonical_count":1,"live_count":0,'
        '"hash_divergence":0,"unresolved_relations":0,"malformed_skipped":3}}'
    )
    miseledger = _write_fake_bin(
        tmp_path,
        "miseledger",
        _miseledger_memory_capable_script(crawl_receipt=receipt),
    )
    monkeypatch.setattr(evidence_cmd.evidence_brief, "_miseledger_bin", lambda: str(miseledger))

    rc = evidence_cmd.run_engine("crawl", ["memory", str(workspace)])

    assert rc == 1
    last_run = evidence_cmd._read_last_run(workspace, "memory")
    assert last_run["status"] == "partial"
    assert last_run["failed"] == 2

    payload = evidence_cmd.status_payload(workspace)
    assert payload["memory_projection"]["healthy"] is False
    assert payload["health"] in {"fail", "incomplete"}
    assert payload["memory_projection"]["state"] == "partial"


def test_memory_projection_state_distinguishes_timeout_stale_missing_and_failed(monkeypatch, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    miseledger = _write_fake_bin(tmp_path, "miseledger", _miseledger_memory_capable_script())
    monkeypatch.setattr(evidence_cmd.evidence_brief, "_miseledger_bin", lambda: str(miseledger))

    evidence_cmd._write_last_run(
        target=workspace,
        source="memory",
        status="ok",
        exit_code=0,
        crawler_version="0.6.0",
        database="ok",
        started_at="2026-01-01T00:00:00Z",
        finished_at="2026-01-01T00:01:00Z",
        detail="stale",
        extra={
            "capability": "memory-projection.v1",
            "status": "completed",
            "stale": True,
            "partial": False,
            "failed": 0,
        },
    )
    assert evidence_cmd.status_payload(workspace)["memory_projection"]["state"] == "stale"

    evidence_cmd._write_last_run(
        target=workspace,
        source="memory",
        status="fail",
        exit_code=124,
        crawler_version="0.6.0",
        database="ok",
        started_at="2026-01-01T00:00:00Z",
        finished_at="2026-01-01T00:01:00Z",
        detail="timeout",
        extra={
            "capability": "memory-projection.v1",
            "status": "completed",
            "stale": False,
            "partial": False,
            "failed": 0,
        },
    )
    assert evidence_cmd.status_payload(workspace)["memory_projection"]["state"] == "timed_out"

    monkeypatch.setattr(evidence_cmd.evidence_brief, "_miseledger_bin", lambda: None)
    payload = evidence_cmd.status_payload(tmp_path / "never-crawled")
    assert payload["memory_projection"]["state"] == "missing"


def test_memory_status_body_free(monkeypatch, tmp_path, capsys):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    evidence_cmd._write_last_run(
        target=workspace,
        source="memory",
        status="ok",
        exit_code=0,
        crawler_version="0.6.0",
        database="ok",
        started_at="2026-01-01T00:00:00Z",
        finished_at="2026-01-01T00:01:00Z",
        detail="ok",
        extra={
            "capability": "memory-projection.v1",
            "engine_version": "0.6.0",
            "status": "completed",
            "stale": False,
            "partial": False,
            "failed": 0,
            "scan_id": "scan-abc",
            "last_completed_scan_id": "scan-abc",
            "canonical_count": 4,
            "live_count": 4,
            "hash_divergence": 0,
            "unresolved_relations": 0,
            "malformed_skipped": 0,
            "workspace": str(workspace),
            "text": "SHOULD_NOT_PRINT_CARD_BODY",
            "raw": {"body": "nope"},
            "resolved_path": "/abs/secret/miseledger",
        },
    )
    miseledger = _write_fake_bin(tmp_path, "miseledger", _miseledger_memory_capable_script())
    monkeypatch.setattr(evidence_cmd.evidence_brief, "_miseledger_bin", lambda: str(miseledger))

    rc = evidence_cmd.status(target=workspace, json_output=False)
    out = capsys.readouterr().out

    assert rc == 0
    assert "memory_projection:" in out
    assert "state=healthy" in out
    assert "capability=memory-projection.v1" in out
    assert "SHOULD_NOT_PRINT_CARD_BODY" not in out
    assert "nope" not in out
    payload = evidence_cmd.status_payload(workspace)
    assert "text" not in (payload.get("memory_projection") or {})
    assert payload["memory_projection"]["healthy"] is True
    latest = payload["memory_projection"]["latest_run"]
    assert isinstance(latest, dict)
    assert "workspace" not in latest
    assert "detail" not in latest
    assert "text" not in latest
    assert "raw" not in latest
    assert "resolved_path" not in latest
    assert latest.get("scan_id") == "scan-abc"


def test_memory_status_json_whitelists_latest_run(monkeypatch, tmp_path, capsys):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    evidence_cmd._write_last_run(
        target=workspace,
        source="memory",
        status="ok",
        exit_code=0,
        crawler_version="0.6.0",
        database="ok",
        started_at="2026-01-01T00:00:00Z",
        finished_at="2026-01-01T00:01:00Z",
        detail="stderr should stay private",
        extra={
            "capability": "memory-projection.v1",
            "engine_version": "0.6.0",
            "scan_status": "completed",
            "stale": False,
            "partial": False,
            "failed": 0,
            "created": 1,
            "updated": 0,
            "unchanged": 0,
            "removed": 0,
            "skipped": 0,
            "scan_id": "scan-json",
            "workspace": str(workspace / "secret"),
            "text": "BODY",
            "raw": {"body": "RAW"},
        },
    )
    miseledger = _write_fake_bin(tmp_path, "miseledger", _miseledger_memory_capable_script())
    monkeypatch.setattr(evidence_cmd.evidence_brief, "_miseledger_bin", lambda: str(miseledger))

    rc = evidence_cmd.status(target=workspace, json_output=True)
    printed = json.loads(capsys.readouterr().out)

    assert rc == 0
    latest = printed["memory_projection"]["latest_run"]
    assert latest["scan_id"] == "scan-json"
    assert "workspace" not in latest
    assert "detail" not in latest
    assert "text" not in latest
    assert "raw" not in latest
    dumped = json.dumps(printed)
    assert "BODY" not in dumped
    assert "stderr should stay private" not in dumped
    assert str(workspace / "secret") not in dumped


def test_memory_status_drops_malformed_optional_health_fields(monkeypatch, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    receipt = (
        '{"scan_id":"scan-safe","capability":"memory-projection.v1","engine_version":"0.6.0",'
        '"status":"completed","stale":false,"partial":false,'
        '"created":0,"updated":0,"unchanged":1,"removed":0,"skipped":0,"failed":0,'
        '"canonical_count":"CARD BODY MUST NOT APPEAR","live_count":1,'
        '"hash_divergence":0,"unresolved_relations":0,"malformed_skipped":0,'
        '"memory_namespace":"CARD BODY MUST NOT APPEAR"}'
    )
    miseledger = _write_fake_bin(
        tmp_path,
        "miseledger",
        _miseledger_memory_capable_script(crawl_receipt=receipt),
    )
    monkeypatch.setattr(evidence_cmd.evidence_brief, "_miseledger_bin", lambda: str(miseledger))

    assert evidence_cmd.run_engine("crawl", ["memory", str(workspace)]) == 0

    payload = evidence_cmd.status_payload(workspace)
    dumped = json.dumps(payload)
    assert "CARD BODY MUST NOT APPEAR" not in dumped
    assert payload["memory_projection"]["canonical_count"] is None
    assert payload["memory_projection"].get("memory_namespace") is None


def test_memory_crawl_rejects_rebuild_and_full(monkeypatch, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    marker = tmp_path / "invoked.txt"
    miseledger = _write_fake_bin(
        tmp_path,
        "miseledger",
        _miseledger_memory_capable_script(marker=marker),
    )
    monkeypatch.setattr(evidence_cmd.evidence_brief, "_miseledger_bin", lambda: str(miseledger))

    assert evidence_cmd.run_engine("crawl", ["memory", str(workspace), "--full"]) == 2
    assert evidence_cmd.run_engine("crawl", ["memory", str(workspace), "--full"]) == 2
    assert not marker.exists()


def test_memory_crawl_dry_run_is_non_current(monkeypatch, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    dry_receipt = (
        '{"source_kind":"brigade-memory","capability":"memory-projection.v1",'
        '"engine_version":"0.6.0","dry_run":true,"skipped":0,"failed":0,"canonical_count":2}'
    )
    miseledger = _write_fake_bin(
        tmp_path,
        "miseledger",
        _miseledger_memory_capable_script(crawl_receipt=dry_receipt),
    )
    monkeypatch.setattr(evidence_cmd.evidence_brief, "_miseledger_bin", lambda: str(miseledger))

    rc = evidence_cmd.run_engine("crawl", ["memory", str(workspace), "--dry-run"])

    assert rc == 1
    last_run = evidence_cmd._read_last_run(workspace, "memory")
    assert last_run["status"] == "dry_run"
    assert last_run.get("dry_run") is True
    assert "created" not in last_run
    assert "failed" not in last_run or last_run.get("scan_id") is None
    payload = evidence_cmd.status_payload(workspace)
    assert payload["memory_projection"]["healthy"] is False


def test_memory_crawl_malformed_receipt_fail_closed_no_fabricated_zeros(monkeypatch, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    # Successful exit but missing counts / scan_id — must not invent zeros or report ok.
    malformed = (
        '{"capability":"memory-projection.v1","engine_version":"0.6.0",'
        '"status":"completed","stale":false,"partial":false}'
    )
    miseledger = _write_fake_bin(
        tmp_path,
        "miseledger",
        _miseledger_memory_capable_script(crawl_receipt=malformed),
    )
    monkeypatch.setattr(evidence_cmd.evidence_brief, "_miseledger_bin", lambda: str(miseledger))

    rc = evidence_cmd.run_engine("crawl", ["memory", str(workspace)])

    assert rc == 1
    last_run = evidence_cmd._read_last_run(workspace, "memory")
    assert last_run["status"] == "fail"
    assert "created" not in last_run
    assert "updated" not in last_run
    assert "unchanged" not in last_run
    assert "removed" not in last_run
    assert "skipped" not in last_run
    assert "failed" not in last_run
    assert last_run.get("scan_id") is None


def test_memory_crawl_uses_preflight_resolved_path(monkeypatch, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    good_marker = tmp_path / "good.txt"
    bad_marker = tmp_path / "bad.txt"
    good = _write_fake_bin(
        tmp_path,
        "miseledger-good",
        _miseledger_memory_capable_script(marker=good_marker),
    )
    bad = _write_fake_bin(
        tmp_path,
        "miseledger-bad",
        (f'if [ "$1" = "crawl" ]; then echo bad > "{bad_marker}"; echo \'{{"scan_id":"bad"}}\'; exit 0; fi\nexit 1\n'),
    )
    state = {"n": 0}

    def resolver() -> str:
        state["n"] += 1
        return str(good) if state["n"] == 1 else str(bad)

    monkeypatch.setattr(evidence_cmd.evidence_brief, "_miseledger_bin", resolver)

    rc = evidence_cmd.run_engine("crawl", ["memory", str(workspace)])

    assert rc == 0
    assert good_marker.exists()
    assert not bad_marker.exists()
    # Preflight resolved once; crawl must reuse compat.resolved_path (no second resolve).
    assert state["n"] == 1


def test_memory_file_backed_search_works_without_engine(monkeypatch, tmp_path):
    from brigade import memory_cmd

    cards = tmp_path / "memory" / "cards"
    cards.mkdir(parents=True)
    (cards / "alpha.md").write_text(
        "---\ntopic: alpha-topic\nconfidence: high\nevidence: [README.md]\n---\n\n# Alpha\n\nalpha body keyword zebra\n"
    )
    (tmp_path / "MEMORY.md").write_text("- [alpha-topic](memory/cards/alpha.md)\n")
    monkeypatch.setattr(evidence_cmd.evidence_brief, "_miseledger_bin", lambda: None)

    payload = memory_cmd.search_cards_payload(tmp_path, "zebra", limit=5)

    assert payload["matches"]
    assert any("alpha" in str(row).lower() for row in payload["matches"])


def test_generic_show_has_no_memory_renderer(monkeypatch, tmp_path, capsys):
    miseledger = _write_fake_bin(tmp_path, "miseledger", _miseledger_memory_capable_script())
    monkeypatch.setattr(evidence_cmd.evidence_brief, "_miseledger_bin", lambda: str(miseledger))

    rc = evidence_cmd.run_engine("show", ["item-1", "--json"])
    out = capsys.readouterr().out

    assert rc == 0
    data = json.loads(out)
    assert data["id"] == "item-1"
    assert data["live"] is True
    assert data["relations"][0]["target_source_kind"] == "brigade"
    assert not hasattr(evidence_cmd, "render_memory_show")
    assert not hasattr(evidence_cmd, "explain_memory")


def test_discord_crawl_still_works_alongside_memory(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _write_fake_bin(tmp_path, "discrawl", _discrawl_script())
    args_file = tmp_path / "args.txt"
    miseledger = _write_fake_bin(
        tmp_path,
        "miseledger",
        (f'echo "$*" > "{args_file}"\nif [ "$1" = "crawl" ]; then echo crawled; exit 0; fi\nexit 1\n'),
    )
    monkeypatch.setenv("PATH", _path_with_bin(tmp_path))
    monkeypatch.setattr(evidence_cmd.evidence_brief, "_miseledger_bin", lambda: str(miseledger))

    rc = evidence_cmd.run_engine("crawl", ["discord"])

    assert rc == 0
    assert args_file.read_text().strip() == "crawl discord"


def test_evidence_trust_review_requires_exact_hash_and_appends_one_event(tmp_path, capsys):
    from brigade import cli, provenance, trust_gate, work_cmd

    item = work_cmd._make_import("review this untrusted import", kind="task", source="manual")
    work_cmd._write_imports(tmp_path, [item])
    digest = item["metadata"]["provenance"]["hashes"]["content"]
    assert digest == provenance.content_sha256(item["text"])
    item_ref = f"work-import:{item['id']}"

    assert (
        cli.main(
            [
                "evidence",
                "trust",
                "review",
                item_ref,
                "--content-hash",
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "--target",
                str(tmp_path),
            ]
        )
        == 2
    )
    assert "content hash" in capsys.readouterr().err
    assert trust_gate.read_events(trust_gate.work_events_path(tmp_path)) == []

    assert (
        cli.main(
            [
                "evidence",
                "trust",
                "review",
                item_ref,
                "--content-hash",
                digest,
                "--target",
                str(tmp_path),
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "reviewed"
    events = trust_gate.read_events(trust_gate.work_events_path(tmp_path))
    assert len(events) == 1
    assert events[0]["to_label"] == "reviewed"
    assert events[0]["envelope_content_hash"] == digest
    reloaded, _imports = work_cmd._find_import(tmp_path, item["id"])
    assert reloaded["metadata"]["provenance"]["trust"]["label"] == "reviewed"

    assert (
        cli.main(
            [
                "evidence",
                "trust",
                "review",
                item_ref,
                "--content-hash",
                digest,
                "--target",
                str(tmp_path),
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "noop"
    assert len(trust_gate.read_events(trust_gate.work_events_path(tmp_path))) == 1


def test_evidence_trust_review_rejects_unknown(tmp_path, capsys):
    from brigade import cli, trust_gate, work_cmd

    item = work_cmd._make_import("legacy unknown import", kind="task", source="manual")
    item["metadata"]["provenance"] = trust_gate.apply_trust_label(
        item["metadata"]["provenance"],
        to_label="unknown",
        assigned_by="ingest:test",
        assigned_at="2026-08-17T00:00:00+00:00",
    )
    work_cmd._write_imports(tmp_path, [item])
    digest = item["metadata"]["provenance"]["hashes"]["content"]
    assert (
        cli.main(
            [
                "evidence",
                "trust",
                "review",
                f"work-import:{item['id']}",
                "--content-hash",
                digest,
                "--target",
                str(tmp_path),
            ]
        )
        == 2
    )
    assert "unknown" in capsys.readouterr().err
    assert trust_gate.read_events(trust_gate.work_events_path(tmp_path)) == []
