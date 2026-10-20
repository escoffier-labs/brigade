"""Bug-report privacy and consent contracts. All transports are denied by default."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from brigade import cli


@pytest.fixture
def report_env(tmp_path, monkeypatch):
    from brigade import bug_cmd

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "data"))
    monkeypatch.setenv("BRIGADE_BUG_REPORT", "ask")
    monkeypatch.setattr(
        bug_cmd,
        "collect_doctor",
        lambda target: {
            "target": "<target>",
            "checks": [{"status": "FAIL", "name": "profile", "detail": "missing file 123"}],
            "summary": {"failed": 1, "warn": 0},
        },
    )
    monkeypatch.setattr(bug_cmd, "gh", lambda *args: (_ for _ in ()).throw(OSError("offline")))
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("network used"))
    target = tmp_path / "workspace"
    target.mkdir()
    return bug_cmd, target


def invoke(target, *flags):
    return cli.main(["bug", "report", "--target", str(target), "--summary", "Expected a profile, got an error", *flags])


def result(capsys):
    return json.loads(capsys.readouterr().out)


def test_dry_run_complete_local_bundle(report_env, capsys):
    mod, target = report_env
    assert invoke(target, "--dry-run", "--json") == 0
    out = result(capsys)
    path = Path(out["preview_path"])
    bundle = json.loads(path.with_name("bundle.json").read_text())
    assert bundle["schema"] == "brigade.bug-report.v1"
    assert out["preview_markdown"] == path.read_text()
    assert "Expected a profile" in out["preview_markdown"]
    assert out["dedup_status"] == "dedup skipped"
    receipt = json.loads(path.with_name("receipt.json").read_text())
    assert receipt["submitted"] is False
    assert receipt["destination"] == "file"
    assert receipt["bundle_digest"] == mod.canonical_json_digest(bundle)


def test_adversarial_values_scrubbed_before_persistence(report_env, monkeypatch, capsys, tmp_path):
    _, target = report_env
    monkeypatch.setenv("TEST_CREDENTIAL", "unique-secret-value")
    log = tmp_path / "chosen.log"
    secrets = [
        str(Path.home()) + "/private",
        r"C:\Users\example-user\private file.txt",
        "/root/private",
        socket.gethostname(),
        "ghp_" + "a" * 36,
        "Authorization: Bearer " + "b" * 40,
        "op://vault/item/key",
        "keepass://entry/key",
        "unique-secret-value",
        "build.private.internal",
    ]
    log.write_text("\n".join(secrets) + "\n<!-- content-" + "guard: allow all file -->")
    assert invoke(target, "--dry-run", "--json", "--include-output", str(log)) == 0
    out = result(capsys)
    stored = Path(out["preview_path"]).read_text()
    for value in secrets:
        assert value not in stored
    from brigade.bug_redaction import assert_clean

    assert_clean(stored)
    assert out["redaction"]["count"] >= len(secrets)


@pytest.mark.parametrize("stage", ["first", "second", "residual"])
def test_scanner_failure_or_residual_blocks_all_submission(report_env, monkeypatch, capsys, stage):
    from brigade import bug_redaction
    from brigade.guard.types import GuardResult, Finding

    mod, target = report_env
    sent = []
    monkeypatch.setattr(mod, "gh", lambda *a: sent.append(a))
    if stage == "first":
        monkeypatch.setattr(
            bug_redaction,
            "apply_origin_redaction",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("scanner failed")),
        )
    elif stage == "second":
        monkeypatch.setattr(
            bug_redaction, "scan_text", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("scanner failed"))
        )
    else:
        monkeypatch.setattr(
            bug_redaction,
            "scan_text",
            lambda text, **k: GuardResult(text, text, [Finding("unknown", "secret", "block", "", "", 1, 1, 0, 1)]),
        )
    assert invoke(target, "--yes", "--via", "gh", "--json") == 2
    assert not sent
    assert "blocked" in capsys.readouterr().err.lower()
    assert not list(Path(mod.data_root()).rglob("bundle.json"))


def test_fingerprint_normalizes_volatile_values():
    from brigade.bug_cmd import fingerprint

    first = {
        "command": "doctor",
        "exception_type": "ValueError",
        "message_head": "failed /tmp/run-12/a.json id abcd1234 123",
    }
    second = {**first, "message_head": "failed /tmp/run-98/b.json id ffff9876 999"}
    assert fingerprint(first, ["profile"]) == fingerprint(second, ["profile"])
    assert fingerprint(first, ["profile"]) != fingerprint(first, ["memory"])


def test_noninteractive_requires_yes_and_flag_receipt(report_env, monkeypatch, capsys):
    mod, target = report_env
    calls = []

    def fake(*args):
        calls.append(args)
        if args[:2] == ("search", "issues"):
            return "[]"
        if args[:2] == ("issue", "create"):
            return "https://github.com/escoffier-labs/brigade/issues/42"
        return ""

    monkeypatch.setattr(mod, "gh", fake)
    assert invoke(target, "--json", "--via", "gh") == 0
    out = result(capsys)
    assert out["submitted"] is False
    assert not any(c[:2] == ("issue", "create") for c in calls)
    assert invoke(target, "--json", "--via", "gh", "--yes") == 0
    out = result(capsys)
    receipt = json.loads(Path(out["receipt_path"]).read_text())
    assert receipt["consent"]["mode"] == "flag"
    assert receipt["submitted"] is True
    assert not any("--label" in c for c in calls)
    assert invoke(target, "--json", "--via", "gh", "--yes") == 2
    assert "cooldown" in capsys.readouterr().err


@pytest.mark.parametrize("flags", [["--kind", "security"], ["--summary", "Found a vulnerability"], ["--summary", " "]])
def test_security_and_empty_summary_refused(report_env, capsys, flags):
    mod, target = report_env
    assert invoke(target, "--json", *flags) == 2
    assert not list(Path(mod.data_root()).rglob("bundle.json"))
    assert capsys.readouterr().err


def test_security_doctor_failure_refused(report_env, monkeypatch, capsys):
    mod, target = report_env
    monkeypatch.setattr(
        mod,
        "collect_doctor",
        lambda target: {
            "checks": [{"status": "FAIL", "name": "security: exposed credential", "detail": "unsafe"}],
            "summary": {},
        },
    )
    assert invoke(target, "--dry-run", "--json") == 2
    assert "SECURITY.md" in capsys.readouterr().err


def test_never_disables_submission_but_allows_local_preview(report_env, monkeypatch, capsys):
    _, target = report_env
    monkeypatch.setenv("BRIGADE_BUG_REPORT", "never")
    assert invoke(target, "--yes", "--via", "gh", "--json") == 2
    capsys.readouterr()
    assert invoke(target, "--dry-run", "--json") == 0
    assert result(capsys)["submitted"] is False


@pytest.mark.parametrize("via,budget", [("url", 6000), ("email", 1800)])
def test_encoded_destinations_bounded_and_bundle_available(report_env, capsys, via, budget):
    from urllib.parse import parse_qs, urlsplit
    from brigade.bug_cmd import BUG_EMAIL

    _, target = report_env
    assert invoke(target, "--summary", "界 ? & # " * 1500, "--via", via, "--yes", "--json") == 0
    out = result(capsys)
    assert len(out["destination_url"].encode()) <= budget
    assert Path(out["preview_path"]).is_file()
    if via == "email":
        assert urlsplit(out["destination_url"]).path == BUG_EMAIL
    else:
        fields = parse_qs(urlsplit(out["destination_url"]).query)
        assert fields["template"] == ["bug.yml"]
        assert fields["fingerprint"] == [out["fingerprint"]]


def test_last_error_closed_schema_and_only_brigade_frames(report_env, monkeypatch):
    from brigade import bug_last_error
    from brigade.cli import version

    mod, _ = report_env
    monkeypatch.setattr(version, "dispatch", lambda args: (_ for _ in ()).throw(ValueError("bad " + "ghp_" + "x" * 36)))
    with pytest.raises(ValueError):
        cli.main(["version"])
    record = json.loads(bug_last_error.error_path().read_text())
    assert record["command"] == "version"
    assert record["exception_type"] == "ValueError"
    assert "ghp_" not in record["message_head"]
    assert all(frame.startswith("brigade.") for frame in record["frames"])
    assert set(record) == {"command", "exit_code", "exception_type", "message_head", "frames"}
    assert not list(Path(mod.data_root()).rglob("bundle.json"))


def test_duplicate_comment_requires_shown_number(report_env, monkeypatch, capsys):
    mod, target = report_env
    calls = []

    def fake(*args):
        calls.append(args)
        if args[:2] == ("search", "issues"):
            return json.dumps(
                [
                    {
                        "number": 42,
                        "title": "Same failure",
                        "state": "open",
                        "url": "untrusted-url",
                        "body": "private data",
                    }
                ]
            )
        return ""

    monkeypatch.setattr(mod, "gh", fake)
    assert invoke(target, "--yes", "--via", "gh", "--comment-on", "41", "--json") == 2
    capsys.readouterr()
    assert not any(c[:2] == ("issue", "comment") for c in calls)
    assert invoke(target, "--yes", "--via", "gh", "--comment-on", "42", "--json") == 0
    out = result(capsys)
    assert out["duplicates"] == [
        {"number": 42, "title": "Same failure", "state": "open", "url": mod.ISSUES_URL + "/42"}
    ]
    assert any(c[:3] == ("issue", "comment", "42") for c in calls)
    receipt = json.loads(Path(out["receipt_path"]).read_text())
    assert receipt["duplicate_choice"] == "comment"


def test_daily_cap_refuses_sixth_attempt(report_env, capsys):
    from datetime import datetime, timezone

    mod, target = report_env
    root = Path(mod.data_root()) / "brigade" / "bug-reports"
    for i in range(5):
        directory = root / str(i)
        directory.mkdir(parents=True)
        (directory / "receipt.json").write_text(
            json.dumps(
                {"submitted": True, "created_at": datetime.now(timezone.utc).isoformat(), "fingerprint": "bf-" + str(i)}
            )
        )
    assert invoke(target, "--yes", "--via", "url", "--json") == 2
    assert "daily cap" in capsys.readouterr().err


def test_oversize_bundle_refused_before_network_or_save(report_env, capsys):
    mod, target = report_env
    assert invoke(target, "--summary", "failure " * 10000, "--yes", "--via", "gh", "--json") == 2
    assert "60 KB" in capsys.readouterr().err
    assert not list(Path(mod.data_root()).rglob("bundle.json"))


def test_failed_transport_leaves_pending_receipt_for_cooldown(report_env, monkeypatch, capsys):
    mod, target = report_env

    def fake(*args):
        if args[:2] == ("search", "issues"):
            return "[]"
        raise OSError("response lost after submission")

    monkeypatch.setattr(mod, "gh", fake)
    assert invoke(target, "--yes", "--via", "gh", "--json") == 2
    capsys.readouterr()
    receipts = list((Path(mod.data_root()) / "brigade" / "bug-reports").glob("*/receipt.json"))
    assert json.loads(receipts[0].read_text())["status"] == "pending"
    assert invoke(target, "--yes", "--via", "gh", "--json") == 2
    assert "cooldown" in capsys.readouterr().err


def test_closed_projection_drops_unlisted_doctor_and_last_error_fields(report_env, monkeypatch, capsys):
    from brigade import bug_last_error

    mod, target = report_env
    bug_last_error.error_path().parent.mkdir(parents=True, exist_ok=True)
    bug_last_error.error_path().write_text(
        json.dumps(
            {
                "command": "doctor",
                "exit_code": 1,
                "raw": "private-raw-data",
                "frames": [],
                "environment": "private-env-data",
            }
        )
    )
    monkeypatch.setattr(
        mod,
        "collect_doctor",
        lambda target: {
            "private": "private-top-field",
            "checks": [{"status": "WARN", "name": "profile", "detail": "warning", "raw": "private-check-field"}],
            "summary": {"failed": 0, "warn": 1, "private": "private-summary-field"},
        },
    )
    assert invoke(target, "--dry-run", "--json") == 0
    out = result(capsys)
    assert "private-" not in out["preview_markdown"]


def test_ordinary_nonzero_records_command_without_arguments(report_env, monkeypatch):
    from brigade import bug_last_error
    from brigade.cli import version

    monkeypatch.setattr(version, "dispatch", lambda args: 7)
    assert cli.main(["version"]) == 7
    record = json.loads(bug_last_error.error_path().read_text())
    assert record["command"] == "version"
    assert record["exit_code"] == 7
    assert record["exception_type"] is None
    assert record["message_head"] == ""


def test_process_main_keeps_exception_and_records_error(report_env, monkeypatch):
    from brigade import bug_last_error
    from brigade.cli import version

    monkeypatch.setattr("sys.argv", ["brigade", "version"])
    monkeypatch.setattr(version, "dispatch", lambda args: (_ for _ in ()).throw(RuntimeError("failed process")))
    with pytest.raises(RuntimeError, match="failed process"):
        cli.process_main()
    assert json.loads(bug_last_error.error_path().read_text())["exception_type"] == "RuntimeError"


def test_text_preview_includes_every_field_and_never_prompts_without_tty(report_env, monkeypatch, capsys):
    import builtins

    _, target = report_env
    monkeypatch.setattr(builtins, "input", lambda *args: pytest.fail("unsolicited prompt"))
    assert invoke(target, "--via", "file", "--dry-run") == 0
    out = capsys.readouterr().out
    assert '"schema": "brigade.bug-report.v1"' in out
    assert '"recent_failures"' in out
    assert "Full redacted bundle:" in out


def test_concurrent_submissions_share_daily_limit(report_env, monkeypatch, capsys):
    """A competing process cannot reserve capacity during another reservation."""
    from brigade.inbox_lock import held_file_lock

    mod, target = report_env
    root = Path(mod.data_root()) / "brigade" / "bug-reports"
    root.mkdir(parents=True)
    # Exercise the same lock primitive independently of the reporting process.
    # The bounded acquisition must fail before the fake transport can run.
    with held_file_lock(root / "submission.lock", deadline_seconds=1):
        assert invoke(target, "--yes", "--via", "url", "--json") == 2
    assert "blocked" in capsys.readouterr().err.lower()


def test_real_doctor_temp_workspace_dry_run_is_network_free(report_env, monkeypatch, capsys):
    from brigade.bug_collect import collect_doctor

    mod, target = report_env
    monkeypatch.setattr(mod, "collect_doctor", collect_doctor)
    assert invoke(target, "--dry-run", "--json") == 0
    out = result(capsys)
    bundle = json.loads(Path(out["preview_path"]).with_name("bundle.json").read_text())
    assert bundle["doctor"]["target"] == "<target>"
    assert bundle["doctor"]["checks"]
    assert all(c["status"] in {"FAIL", "WARN"} for c in bundle["doctor"]["checks"])


def test_parser_failure_records_only_resolved_command(report_env):
    from brigade import bug_last_error

    with pytest.raises(SystemExit):
        cli.main(["bug", "report", "--unknown-private-value"])
    record = json.loads(bug_last_error.error_path().read_text())
    assert record["command"] == "bug report"
    assert record["exit_code"] == 2
    assert "private-value" not in json.dumps(record)


def test_recent_failures_uses_projected_ids_and_verify_digest(report_env, capsys):
    from brigade.bug_collect import recent_failures
    from brigade.localio import write_json, canonical_json_digest

    _, target = report_env
    for i in range(7):
        run = target / ".brigade" / "runs" / f"run-{i}"
        write_json(
            run / "run.json",
            {
                "started_at": f"2026-01-0{i + 1}T00:00:00Z",
                "finished_at": f"2026-01-0{i + 1}T00:00:01Z",
                "task": "MUST NOT COPY TASK",
            },
        )
        write_json(run / "roster.json", {"agents": {"worker": {"cli": "codex"}}})
        write_json(
            run / "worker-results.json",
            {
                "results": [
                    {
                        "worker": "worker",
                        "ok": False,
                        "exit_code": 1,
                        "timed_out": i == 6,
                        "output": "MUST NOT COPY OUTPUT",
                    }
                ]
            },
        )
    receipt = {
        "run_id": "verify-1",
        "status": "failed",
        "started_at": "2026-01-01T00:00:00Z",
        "completed_at": "2026-01-01T00:00:01Z",
        "commands": [{"command": "pytest -q", "exit_code": 1, "stdout": "MUST NOT COPY LOG"}],
    }
    write_json(target / ".brigade" / "work" / "verify-runs" / "verify-1" / "receipt.json", receipt)
    value = recent_failures(target)
    assert [row["id"] for row in value["runs"]] == ["run-6", "run-5", "run-4", "run-3", "run-2"]
    assert value["runs"][0]["status"] == "timed_out"
    assert value["verify"] == {"id": "verify-1", "digest": canonical_json_digest(receipt)}
    assert "MUST NOT COPY" not in json.dumps(value)


def test_chosen_output_tail_and_secret_header_are_redacted(report_env, tmp_path, capsys):
    _, target = report_env
    output = tmp_path / "chosen.txt"
    output.write_text("private initial line\n" + "line\n" * 205 + "Authorization: Bearer tiny\n")
    assert invoke(target, "--dry-run", "--json", "--include-output", str(output)) == 0
    out = result(capsys)
    bundle = json.loads(Path(out["preview_path"]).with_name("bundle.json").read_text())
    assert "private initial line" not in bundle["user"]["output_tail"]
    assert "tiny" not in out["preview_markdown"]
    assert len(bundle["user"]["output_tail"].splitlines()) == 200


def test_dry_run_does_not_trigger_background_update_transport(report_env, monkeypatch, capsys):
    from brigade import update_notify

    _, target = report_env
    monkeypatch.setattr(update_notify, "maybe_notify", lambda *args: pytest.fail("background transport invoked"))
    assert invoke(target, "--dry-run", "--json") == 0
    assert result(capsys)["submitted"] is False


def test_mixed_single_line_leaks_and_escaped_windows_path(report_env, tmp_path, capsys):
    _, target = report_env
    output = tmp_path / "chosen.txt"
    path = "C:" + "\\\\" + "Users" + "\\\\" + "example-user" + "\\\\" + "file.txt"
    output.write_text("C:\\Users\\example-user\\private file.txt Authorization: Bearer " + "z" * 35 + " " + path)
    assert invoke(target, "--dry-run", "--json", "--include-output", str(output)) == 0
    out = result(capsys)
    assert "example-user" not in out["preview_markdown"]
    assert "z" * 35 not in out["preview_markdown"]


def test_redirected_preview_requires_yes_even_with_terminal_stdin(report_env, monkeypatch, capsys):
    import builtins
    import sys

    _, target = report_env
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(builtins, "input", lambda *args: pytest.fail("preview was redirected"))
    assert invoke(target, "--via", "url") == 0
    assert "Full redacted bundle:" in capsys.readouterr().out


def test_duplicate_search_falls_back_to_exception_and_command(report_env, monkeypatch, capsys):
    from brigade import bug_last_error

    mod, target = report_env
    bug_last_error.error_path().parent.mkdir(parents=True, exist_ok=True)
    bug_last_error.error_path().write_text(
        json.dumps({"command": "doctor", "exception_type": "ValueError", "message_head": "failure", "frames": []})
    )
    queries = []

    def fake(*args):
        queries.append(args[2])
        if args[2].startswith("bf-"):
            return "[]"
        return json.dumps([{"number": 99, "title": "doctor fails", "state": "open"}])

    monkeypatch.setattr(mod, "gh", fake)
    assert invoke(target, "--json", "--via", "gh") == 0
    out = result(capsys)
    assert out["duplicates"][0]["number"] == 99
    assert "ValueError" in queries[1] and "doctor" in queries[1]
