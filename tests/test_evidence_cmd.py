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
    assert payload["pipeline"][0].startswith("miseledger crawl")


def test_evidence_status_distinguishes_explicit_execution_from_review_only_plans(monkeypatch, tmp_path):
    monkeypatch.setattr(evidence_cmd.evidence_brief, "_miseledger_bin", lambda: None)

    payload = evidence_cmd.status_payload(tmp_path)
    boundaries = payload["boundaries"]

    assert (
        "Explicit user-invoked `brigade evidence crawl` and `brigade evidence search` execute MiseLedger across a process boundary."
        in boundaries
    )
    assert (
        "Review-only `brigade evidence crawl plan` and `brigade evidence export plan` never execute MiseLedger."
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
    assert "explicitly runs local MiseLedger for `brigade evidence crawl|search`" in station_contract
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
    assert "review-only crawl plan never executes MiseLedger" in payload["boundaries"][0]
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
