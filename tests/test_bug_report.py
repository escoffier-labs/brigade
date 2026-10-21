"""Bug-report privacy and consent contracts. All transports are denied by default."""

from __future__ import annotations

import json
import os
import re
import socket
from pathlib import Path

import pytest

from brigade import cli


@pytest.fixture
def report_env(tmp_path, monkeypatch):
    from brigade import bug_cmd

    # Host credentials must not influence unrelated fixture reports.
    for name in os.environ:
        if re.search(r"TOKEN|KEY|SECRET|PASSWORD|PASS|CREDENTIAL", name, re.I):
            monkeypatch.delenv(name)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "data"))
    monkeypatch.setenv("BRIGADE_BUG_REPORT", "ask")
    monkeypatch.setattr(
        bug_cmd,
        "collect_diagnostics",
        lambda target: {
            "target": "<target>",
            "checks": [{"status": "FAIL", "name": "profile", "detail": "missing file 123"}],
            "summary": {"failed": 1, "warn": 0},
        },
    )
    monkeypatch.setattr(bug_cmd, "gh", lambda *args: (_ for _ in ()).throw(OSError("offline")))
    monkeypatch.setattr("brigade.bug_cmd.urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("network used"))
    target = tmp_path / "workspace"
    target.mkdir()
    return bug_cmd, target


def invoke(target, *flags):
    return cli.main(["bug", "report", "--target", str(target), "--summary", "Expected a profile, got an error", *flags])


def submit_prepared(target, capsys, *flags, summary="Expected a profile, got an error"):
    assert invoke(target, "--summary", summary, "--dry-run", "--json") == 0
    preview = result(capsys)
    return cli.main(["bug", "report", "--report-id", preview["report_id"], "--yes", "--not-security", *flags])


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


@pytest.mark.parametrize("via", ["gh", "url", "email"])
def test_windows_submission_records_attempt_and_enforces_cooldown(report_env, monkeypatch, capsys, via):
    from brigade import bug_last_error

    mod, target = report_env
    _without_posix_io(monkeypatch)
    mod.write_json(bug_last_error.error_path(), {"command": "doctor", "message_head": "profile missing"})
    calls = []

    def fake_gh(*args):
        calls.append(args)
        return "[]" if args[:2] == ("search", "issues") else mod.ISSUES_URL + "/42"

    monkeypatch.setattr(mod, "gh", fake_gh)
    assert submit_prepared(target, capsys, "--via", via, "--json") == 0
    submitted = result(capsys)
    assert submitted["submitted"] is True
    assert submitted["destination"] == via
    bundle = json.loads(Path(submitted["preview_path"]).with_name("bundle.json").read_text())
    assert bundle["last_error"]["command"] == "doctor"
    assert bundle["last_error"]["message_head"] == "profile missing"
    logs = Path(submitted["receipt_path"]).parent.parent / "attempts"
    records = [json.loads(line) for path in logs.glob("*.jsonl") for line in path.read_text().splitlines()]
    assert len(records) == 1
    assert records[0]["report_id"] == submitted["report_id"]
    assert records[0]["fingerprint"] == submitted["fingerprint"]
    assert any(call[:2] == ("issue", "create") for call in calls) == (via == "gh")
    assert (
        cli.main(
            ["bug", "report", "--report-id", submitted["report_id"], "--yes", "--not-security", "--via", via, "--json"]
        )
        == 2
    )
    assert ("already submitted" if via == "gh" else "cooldown") in capsys.readouterr().err
    assert sum(len(path.read_text().splitlines()) for path in logs.glob("*.jsonl")) == 1


def _without_posix_io(monkeypatch):
    import shutil
    from brigade import localio, run_journal

    monkeypatch.setattr(os, "supports_dir_fd", set())
    monkeypatch.setattr(run_journal, "_HAS_O_NOFOLLOW", False)
    monkeypatch.setattr(localio, "_supports_directory_fsync", lambda: False)
    # shutil caches this decision at import time. Windows uses the path branch.
    monkeypatch.setattr(shutil, "_use_fd_functions", False)
    for name in ("O_NOFOLLOW", "O_NONBLOCK", "fchmod"):
        monkeypatch.delattr(os, name, raising=False)


def test_untrusted_markdown_stays_in_code_blocks_through_submission(report_env, monkeypatch, capsys, tmp_path):
    mod, target = report_env
    payload = "``````\n![image](https://example.com/image)\n<b>html</b> [link](https://example.com) @example-user"
    summary = tmp_path / "summary.txt"
    output = tmp_path / "output.txt"
    summary.write_text(payload)
    output.write_text(payload)
    assert (
        cli.main(
            [
                "bug",
                "report",
                "--target",
                str(target),
                "--summary-file",
                str(summary),
                "--title",
                "Problem @example-user",
                "--include-output",
                str(output),
                "--dry-run",
                "--json",
            ]
        )
        == 0
    )
    preview = result(capsys)
    bodies, titles = [], []

    def fake_gh(*args):
        if args[:2] == ("search", "issues"):
            return "[]"
        bodies.append(Path(args[args.index("--body-file") + 1]).read_text())
        titles.append(args[args.index("--title") + 1])
        return mod.ISSUES_URL + "/42"

    monkeypatch.setattr(mod, "gh", fake_gh)
    assert (
        cli.main(
            [
                "bug",
                "report",
                "--report-id",
                preview["report_id"],
                "--yes",
                "--not-security",
                "--via",
                "gh",
                "--json",
            ]
        )
        == 0
    )
    result(capsys)
    assert bodies == [preview["preview_markdown"]]
    fence = None
    literal = []
    for line in bodies[0].splitlines():
        if fence is None and re.fullmatch(r"`{3,}(?:json|text)?", line):
            fence = re.match(r"`+", line)[0]
        elif fence is not None and line == fence:
            fence = None
        elif fence is not None:
            literal.append(line)
        else:
            assert not any(value in line for value in ("![", "[link]", "<b>", "@example-user"))
    assert fence is None
    assert "<b>html</b> [link](https://example.com) @example-user" in literal
    assert "@example-user" not in titles[0]


def test_legacy_unfenced_preview_requires_reprepare_before_network(report_env, monkeypatch, capsys):
    import hashlib

    mod, target = report_env
    assert invoke(target, "--summary", "[link](https://example.com) @example-user", "--dry-run", "--json") == 0
    preview = result(capsys)
    path = Path(preview["preview_path"])
    bundle = json.loads(path.with_name("bundle.json").read_text())
    # Emulate a digest-valid preview saved before literal Markdown rendering.
    old_markdown = f"### What happened\n\n{bundle['user']['summary']}\n\n### Full output\n\n{json.dumps(bundle)}\n"
    path.write_text(old_markdown)
    receipt_path = Path(preview["receipt_path"])
    receipt = json.loads(receipt_path.read_text())
    receipt["preview_digest"] = hashlib.sha256(old_markdown.encode()).hexdigest()
    receipt_path.write_text(json.dumps(receipt))
    monkeypatch.setattr(mod, "gh", lambda *a: pytest.fail("unfenced preview reached network"))
    assert (
        cli.main(
            [
                "bug",
                "report",
                "--report-id",
                preview["report_id"],
                "--yes",
                "--not-security",
                "--via",
                "gh",
                "--json",
            ]
        )
        == 2
    )
    assert "prepare a new preview" in capsys.readouterr().err


def test_adversarial_values_scrubbed_before_persistence(report_env, monkeypatch, capsys, tmp_path):
    _, target = report_env
    monkeypatch.setenv("TEST_CREDENTIAL", "unique-secret-value")
    log = tmp_path / "chosen.log"
    secrets = [
        str(Path.home()) + "/private",
        r"C:\Users\example-user\private file.txt",  # content-guard: allow windows-home-path
        "/root/private",  # content-guard: allow root-path
        socket.gethostname(),
        "ghp_" + "a" * 36,
        *[prefix + "a" * 36 for prefix in ("gho_", "ghu_", "ghs_", "ghr_", "github_pat_", "sk-", "xoxb-")],
        "AKIA" + "A" * 16,
        "Authorization: Bearer " + "b" * 40,
        "op://vault/item/key",
        "keepass://entry/key",
        "unique-secret-value",
        "build.private.internal",  # content-guard: allow private-hostname
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


@pytest.mark.parametrize("stage", ["preview", "prepared"])
def test_short_environment_value_allows_report(report_env, monkeypatch, capsys, stage):
    _, target = report_env
    flags = ["--summary", "prefixqxypostfix", "--dry-run", "--json"]
    if stage == "prepared":
        assert invoke(target, *flags) == 0
        preview = result(capsys)
        flags = ["--report-id", preview["report_id"], "--dry-run", "--json"]
    monkeypatch.setenv("TEST_CREDENTIAL", "qxy")
    status = cli.main(["bug", "report", *flags]) if stage == "prepared" else invoke(target, *flags)
    assert status == 0
    assert "prefixqxypostfix" in result(capsys)["preview_markdown"]


@pytest.mark.parametrize("secret", ["qxyp-secret", 'qx"yp-secret', "qx\nyp-secret", "qx\u2603yp-secret"])
def test_delayed_submission_blocks_new_long_environment_secret(report_env, monkeypatch, capsys, secret):
    mod, target = report_env
    assert invoke(target, "--summary", f"Unexpected marker {secret}", "--dry-run", "--json") == 0
    preview = result(capsys)
    before = Path(preview["receipt_path"]).read_bytes()
    monkeypatch.setenv("DEPLOY_SECRET", secret)
    monkeypatch.setattr(mod, "gh", lambda *a: pytest.fail("blocked report reached search or submission"))
    assert (
        cli.main(["bug", "report", "--report-id", preview["report_id"], "--yes", "--not-security", "--via", "gh"]) == 2
    )
    captured = capsys.readouterr()
    assert "DEPLOY_SECRET" in captured.err
    assert secret not in captured.out + captured.err
    assert Path(preview["receipt_path"]).read_bytes() == before


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
    assert invoke(target, "--dry-run", "--json") == 2
    assert not sent
    assert "blocked" in capsys.readouterr().err.lower()
    assert not list(Path(mod.data_root()).rglob("bundle.json"))


@pytest.mark.parametrize(
    "first_path,second_path",
    [
        ("/tmp/run-12/a.json", "/tmp/run-98/b.json"),
        ("/var/lib/Alpha One/result", "/opt/Beta Two/result"),
        (r"C:\Alpha One\result", r"D:\Beta Two\result"),
        ('"/var/lib/Alpha One/result file"', '"/opt/Beta Two/other file"'),
        (r'"C:\Alpha One\result file"', r'"D:\Beta Two\other file"'),
    ],
)
def test_fingerprint_normalizes_volatile_values(first_path, second_path):
    from brigade.bug_cmd import fingerprint

    first = {
        "command": "doctor",
        "exception_type": "ValueError",
        "message_head": f"failed {first_path} id abcd1234 123",
    }
    second = {**first, "message_head": f"failed {second_path} id ffff9876 999"}
    assert fingerprint(first, ["profile"]) == fingerprint(second, ["profile"])
    assert fingerprint(first, ["profile"]) != fingerprint(first, ["memory"])


@pytest.mark.parametrize(
    "first,second",
    [
        ("memory-card: memory/cards/client-123.md", "memory-card: memory/cards/client-999.md"),
        ("memory-card: memory/cards/alpha.md", "memory-card: memory/cards/beta.md"),
        ("profile: run-ab123 12", "profile: run-cd999 99"),
        (r"memory-card: C:\Alpha One\card.md", r"memory-card: D:\Beta Two\other.md"),
    ],
)
def test_fingerprint_normalizes_check_name_paths_and_ids(first, second):
    from brigade.bug_cmd import fingerprint

    assert fingerprint(None, [first]) == fingerprint(None, [second])
    assert fingerprint(None, [first]) != fingerprint(None, ["other-check"])


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
    assert submit_prepared(target, capsys, "--json", "--via", "gh") == 0
    out = result(capsys)
    receipt = json.loads(Path(out["receipt_path"]).read_text())
    assert receipt["consent"]["mode"] == "flag"
    assert receipt["security_confirmation"] == {"mode": "flag", "not_security": True}
    assert receipt["submitted"] is True
    assert not any("--label" in c for c in calls)
    assert submit_prepared(target, capsys, "--json", "--via", "gh") == 2
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
        "collect_diagnostics",
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
    from urllib.parse import parse_qs, unquote, urlsplit
    from brigade.bug_cmd import BUG_EMAIL

    _, target = report_env
    assert submit_prepared(target, capsys, "--via", via, "--json", summary="界 ? & # " * 1500) == 0
    out = result(capsys)
    assert len(out["destination_url"].encode()) <= budget
    assert Path(out["preview_path"]).is_file()
    if via == "email":
        assert urlsplit(out["destination_url"]).path == BUG_EMAIL
        decoded = dict(item.split("=", 1) for item in urlsplit(out["destination_url"]).query.split("&"))
        assert unquote(decoded["subject"]) == "Brigade bug report"
        assert "界 ? & # " in unquote(decoded["body"])
        assert "+" not in out["destination_url"]
    else:
        fields = parse_qs(urlsplit(out["destination_url"]).query)
        assert fields["template"] == ["bug.yml"]
        assert fields["fingerprint"] == [out["fingerprint"]]
        assert not fields["logs"][0].startswith("```")
        assert not fields["logs"][0].endswith("```")
        for key in ("what-happened",):
            lines = fields[key][0].splitlines()
            fence = lines[0].removesuffix("text")
            assert re.fullmatch(r"`{3,}", fence)
            assert lines[-1] == fence


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
    assert set(record) == {"command", "exit_code", "exception_type", "message_head", "frames", "recorded_at"}
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
    assert submit_prepared(target, capsys, "--via", "gh", "--comment-on", "41", "--json") == 2
    capsys.readouterr()
    assert not any(c[:2] == ("issue", "comment") for c in calls)
    assert submit_prepared(target, capsys, "--via", "gh", "--comment-on", "42", "--json") == 0
    out = result(capsys)
    assert out["duplicates"] == [
        {"number": 42, "title": "Same failure", "state": "open", "url": mod.ISSUES_URL + "/42"}
    ]
    assert any(c[:3] == ("issue", "comment", "42") for c in calls)
    receipt = json.loads(Path(out["receipt_path"]).read_text())
    assert receipt["duplicate_choice"] == "comment"


@pytest.mark.parametrize("transport_fails", [False, True])
def test_daily_cap_counts_repeated_report_attempts(report_env, monkeypatch, capsys, transport_fails):
    from datetime import datetime, timedelta, timezone

    mod, target = report_env
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(mod, "datetime", Clock)
    calls = []

    def fake(*args):
        if args[:2] == ("search", "issues"):
            return "[]"
        calls.append(args)
        if transport_fails:
            raise OSError("response lost after submission")
        return mod.ISSUES_URL + "/42"

    monkeypatch.setattr(mod, "gh", fake)
    assert invoke(target, "--dry-run", "--json") == 0
    preview = result(capsys)
    args = ["bug", "report", "--report-id", preview["report_id"], "--yes", "--not-security", "--via", "gh", "--json"]
    for _ in range(5):
        assert cli.main(args) == (2 if transport_fails else 0)
        capsys.readouterr()
        now += timedelta(minutes=6)
        if not transport_fails:
            # Successful report ids are terminal. Prepare another report for
            # the next attempt, including the sixth attempt denied by the cap.
            assert invoke(target, "--dry-run", "--json") == 0
            args[3] = result(capsys)["report_id"]
    log = Path(mod.data_root()) / "brigade" / "bug-reports" / "attempts" / "2026-01-01.jsonl"
    before = log.read_bytes()
    attempts = [json.loads(line) for line in before.splitlines()]
    assert len(attempts) == 5
    assert log.stat().st_mode & 0o777 == 0o600
    assert log.parent.stat().st_mode & 0o777 == 0o700
    if transport_fails:
        assert {row["report_id"] for row in attempts} == {preview["report_id"]}
    else:
        assert len({row["report_id"] for row in attempts}) == 5
    assert len({row["attempted_at"] for row in attempts}) == 5
    assert cli.main(args) == 2
    assert "daily cap" in capsys.readouterr().err
    assert len(calls) == 5
    assert log.read_bytes() == before


def test_oversize_bundle_refused_before_network_or_save(report_env, capsys):
    mod, target = report_env
    assert invoke(target, "--summary", "failure " * 10000, "--dry-run", "--json") == 2
    assert "60 KB" in capsys.readouterr().err
    assert not list(Path(mod.data_root()).rglob("bundle.json"))


def test_failed_transport_leaves_pending_receipt_for_cooldown(report_env, monkeypatch, capsys):
    mod, target = report_env

    def fake(*args):
        if args[:2] == ("search", "issues"):
            return "[]"
        raise OSError("response lost after submission")

    monkeypatch.setattr(mod, "gh", fake)
    assert submit_prepared(target, capsys, "--via", "gh", "--json") == 2
    capsys.readouterr()
    receipts = list((Path(mod.data_root()) / "brigade" / "bug-reports").glob("*/receipt.json"))
    assert json.loads(receipts[0].read_text())["status"] == "pending"
    assert submit_prepared(target, capsys, "--via", "gh", "--json") == 2
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
        "collect_diagnostics",
        lambda target: {
            "private": "private-top-field",
            "checks": [{"status": "WARN", "name": "profile", "detail": "warning", "raw": "private-check-field"}],
            "summary": {"failed": 0, "warn": 1, "private": "private-summary-field"},
        },
    )
    assert invoke(target, "--dry-run", "--json") == 0
    out = result(capsys)
    assert "private-" not in out["preview_markdown"]


@pytest.mark.parametrize("field", ["doctor-detail", "run-seat"])
def test_older_prepared_diagnostic_fields_require_new_preview(report_env, monkeypatch, capsys, field):
    import hashlib

    mod, target = report_env
    assert invoke(target, "--dry-run", "--json") == 0
    preview = result(capsys)
    markdown_path = Path(preview["preview_path"])
    bundle_path = markdown_path.with_name("bundle.json")
    receipt_path = Path(preview["receipt_path"])
    bundle = json.loads(bundle_path.read_text())
    if field == "doctor-detail":
        bundle["bug_diagnostics"]["checks"][0]["detail"] = "customer Example project Sample"
    else:
        bundle["recent_failures"]["runs"] = [{"id": "run-1", "status": "failed", "seat": "example-production"}]
    markdown = mod.render(bundle)
    mod.write_json(bundle_path, bundle)
    mod.write_text_atomic(markdown_path, markdown)
    receipt = json.loads(receipt_path.read_text())
    receipt.update(
        bundle_digest=mod.canonical_json_digest(bundle), preview_digest=hashlib.sha256(markdown.encode()).hexdigest()
    )
    mod.write_json(receipt_path, receipt)
    monkeypatch.setattr(mod, "gh", lambda *a: pytest.fail("old diagnostic projection reached transport"))
    assert (
        cli.main(["bug", "report", "--report-id", preview["report_id"], "--yes", "--not-security", "--via", "gh"]) == 2
    )
    assert "prepare a new preview" in capsys.readouterr().err


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
        assert submit_prepared(target, capsys, "--via", "url", "--json") == 2
    assert "blocked" in capsys.readouterr().err.lower()


@pytest.mark.parametrize("source", ["sidecar", "registry", "host"])
def test_real_diagnostics_temp_workspace_dry_run_is_network_free(report_env, monkeypatch, capsys, source):
    from brigade import doctor, managed, registry
    from brigade.bug_collect import collect_diagnostics
    from brigade.station import DoctorContext, Station

    mod, target = report_env
    # Installed sidecars and even target-labelled registry callbacks are not
    # safe to invoke merely to discard their results after collection.
    monkeypatch.setattr(managed.ManagedTool, "detect", lambda self: True)
    sidecar = next(tool for tool in managed.for_station("search") if tool.name == "code-search-api")
    monkeypatch.setattr(
        managed, "for_station", lambda station: (sidecar,) if source == "sidecar" and station == "search" else ()
    )
    monkeypatch.setattr(managed.urllib.request, "urlopen", lambda *a, **k: pytest.fail("sidecar network used"))
    monkeypatch.setattr(
        doctor, "build_context", lambda t: DoctorContext(t, None, ["openclaw"] if source == "host" else [])
    )
    monkeypatch.setattr(doctor, "_check_openclaw", lambda: pytest.fail("host check used"))
    network_station = Station("network", "fake network check", lambda ctx: pytest.fail("registry network used"))
    original = registry.all_stations()
    monkeypatch.setattr(
        registry, "all_stations", lambda: (*original, network_station) if source == "registry" else original
    )
    monkeypatch.setattr(mod, "collect_diagnostics", collect_diagnostics)
    assert invoke(target, "--dry-run", "--json") == 0
    out = result(capsys)
    bundle = json.loads(Path(out["preview_path"]).with_name("bundle.json").read_text())
    assert bundle["bug_diagnostics"]["target"] == "<target>"
    assert bundle["bug_diagnostics"]["checks"]
    assert all(c["status"] in {"FAIL", "WARN"} for c in bundle["bug_diagnostics"]["checks"])


def test_real_diagnostics_prepared_report_allows_security_config_warning(report_env, monkeypatch, capsys):
    from brigade.bug_collect import collect_diagnostics

    mod, target = report_env
    (target / ".brigade" / "security.toml").mkdir(parents=True)
    monkeypatch.setattr(mod, "collect_diagnostics", collect_diagnostics)
    assert invoke(target, "--dry-run", "--json") == 0
    preview = result(capsys)
    bundle = json.loads(Path(preview["preview_path"]).with_name("bundle.json").read_text())
    assert any(c["status"] == "WARN" and c["name"].startswith("security:") for c in bundle["bug_diagnostics"]["checks"])
    assert (
        cli.main(
            ["bug", "report", "--report-id", preview["report_id"], "--yes", "--not-security", "--via", "url", "--json"]
        )
        == 0
    )
    submitted = result(capsys)
    assert submitted["submitted"] is True
    assert submitted["preview_markdown"] == preview["preview_markdown"]


@pytest.mark.parametrize("process", [False, True])
@pytest.mark.parametrize("exception", [RuntimeError, SystemExit])
@pytest.mark.parametrize("quoted", [False, True])
def test_last_error_scrubs_all_parsed_paths_and_unknown_absolute_paths(
    report_env, monkeypatch, capsys, tmp_path, process, exception, quoted
):
    from brigade import bug_last_error
    from brigade.cli import evidence

    _, target = report_env
    monkeypatch.chdir(tmp_path)
    lexical_target = Path("private client/../workspace")
    lexical_file = Path("private report/output file.md")
    private_paths = [str(p) for p in (lexical_target, lexical_target.resolve(), lexical_file, lexical_file.resolve())]
    private_paths += [
        "/srv/private tenant/repo/module.py",
        "/home/example tenant/repo/module.py",  # content-guard: allow home-path
        r"D:\Private Tenant\repo\module.py",
        r"\\private-share\tenant\repo\module.py",
    ]
    message = "failed " + " | ".join(repr(p) if quoted else p for p in private_paths)
    monkeypatch.setattr(evidence, "dispatch", lambda args: (_ for _ in ()).throw(exception(message)))
    argv = ["evidence", "controls", "--target", str(lexical_target), "--render-doc", str(lexical_file)]
    with pytest.raises(exception):
        if process:
            monkeypatch.setattr("sys.argv", ["brigade", *argv])
            cli.process_main()
        else:
            cli.main(argv)
    record = json.loads(bug_last_error.error_path().read_text())
    assert record["command"] == "evidence controls"
    assert record["message_head"].startswith("failed ")
    assert all(p not in record["message_head"] for p in private_paths)
    assert "private" not in record["message_head"].lower()
    assert "tenant" not in record["message_head"].lower()
    assert invoke(target, "--dry-run", "--json") == 0
    assert "private" not in result(capsys)["preview_markdown"].lower()


def test_parser_failure_records_only_resolved_command(report_env):
    from brigade import bug_last_error

    with pytest.raises(SystemExit):
        cli.main(["version", "--unknown-private-value"])
    record = json.loads(bug_last_error.error_path().read_text())
    assert record["command"] == "version"
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


def test_yes_requires_prepared_report(report_env, capsys):
    _, target = report_env
    assert invoke(target, "--yes", "--via", "gh", "--json") == 2
    assert "--report-id" in capsys.readouterr().err


def test_submit_uses_approved_bundle_without_recollecting(report_env, monkeypatch, capsys, tmp_path):
    from brigade import bug_last_error

    mod, target = report_env
    log = tmp_path / "chosen.log"
    log.write_text("reviewed output")
    assert invoke(target, "--include-output", str(log), "--dry-run", "--json") == 0
    preview = result(capsys)
    log.write_text("unreviewed output")
    bug_last_error.error_path().write_text('{"command": "unreviewed"}')
    monkeypatch.setattr(mod, "collect_diagnostics", lambda *a: pytest.fail("doctor recollected"))
    monkeypatch.setattr(mod, "collect", lambda *a, **k: pytest.fail("bundle recollected"))
    bodies = []

    def fake(*args):
        if args[:2] == ("search", "issues"):
            return "[]"
        if args[:2] == ("issue", "create"):
            bodies.append(Path(args[-1]).read_text())
            return mod.ISSUES_URL + "/42"
        return ""

    monkeypatch.setattr(mod, "gh", fake)
    assert (
        cli.main(
            ["bug", "report", "--report-id", preview["report_id"], "--yes", "--not-security", "--via", "gh", "--json"]
        )
        == 0
    )
    submitted = result(capsys)
    assert submitted["report_id"] == preview["report_id"]
    assert bodies == [preview["preview_markdown"]]
    assert "reviewed output" in bodies[0]
    assert "unreviewed" not in bodies[0]


@pytest.mark.parametrize("filename", ["bundle.json", "bundle.md", "receipt.json"])
def test_prepared_tampering_blocks_before_transport(report_env, monkeypatch, capsys, filename):
    mod, target = report_env
    assert invoke(target, "--dry-run", "--json") == 0
    preview = result(capsys)
    path = Path(preview["preview_path"]).with_name(filename)
    if filename == "bundle.md":
        path.write_text(path.read_text() + "changed")
    else:
        value = json.loads(path.read_text())
        if filename == "bundle.json":
            value["user"]["summary"] = "unapproved summary"
        else:
            value["bundle_digest"] = "changed"
        path.write_text(json.dumps(value))
    monkeypatch.setattr(mod, "gh", lambda *a: pytest.fail("transport before integrity check"))
    assert (
        cli.main(["bug", "report", "--report-id", preview["report_id"], "--yes", "--not-security", "--via", "gh"]) == 2
    )
    assert "digest" in capsys.readouterr().err


@pytest.mark.parametrize("failure", ["parser", "scanner", "transport", "exception"])
def test_report_failure_preserves_previous_error(report_env, monkeypatch, capsys, failure):
    from brigade import bug_last_error, bug_redaction

    mod, target = report_env
    bug_last_error.record("version", 1, RuntimeError("original diagnostic"))
    original = bug_last_error.error_path().read_bytes()
    if failure == "parser":
        with pytest.raises(SystemExit):
            cli.main(["bug", "report", "--unknown-private-value"])
    elif failure == "scanner":
        monkeypatch.setattr(bug_redaction, "scan_text", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("scanner")))
        assert invoke(target, "--dry-run", "--json") == 2
    elif failure == "exception":
        from brigade.cli import bug

        monkeypatch.setattr(bug, "dispatch", lambda *a: (_ for _ in ()).throw(RuntimeError("unexpected")))
        with pytest.raises(RuntimeError, match="unexpected"):
            invoke(target, "--dry-run", "--json")
    else:
        assert invoke(target, "--dry-run", "--json") == 0
        preview = result(capsys)
        assert (
            cli.main(["bug", "report", "--report-id", preview["report_id"], "--yes", "--not-security", "--via", "gh"])
            == 2
        )
    assert bug_last_error.error_path().read_bytes() == original


@pytest.mark.parametrize("field", ["summary", "title", "output", "last_error"])
@pytest.mark.parametrize(
    "narrative",
    [
        "SQL injection",
        "security vulnerability",
        "security issue",
        "code execution in the ingester",
        "personal data leaks into a workspace",
        "CVE-2026-12345",
        "authentication bypass",
        "cross-site scripting",
        "privilege escalation",
        "SSRF",
        "command injection",
        "symlink race permits arbitrary file overwrite",
        "sandbox escape exposes private keys",
        "sandbox escape",
        "symlink race",
        "arbitrary file overwrite",
        "TOCTOU permits arbitrary file access",
        "leak of keys or tokens",
        "profile writes outside --target",
        "guard bypass publishes personal data",
        "template exposes credentials",
        "insecure deserialization",
        "path traversal",
        "RCE",
        "XSS",
    ],
)
def test_vulnerability_narratives_refused(report_env, capsys, tmp_path, field, narrative):
    mod, target = report_env
    flags = ["--" + field, narrative]
    if field == "output":
        path = tmp_path / "output.txt"
        path.write_text(narrative)
        flags = ["--include-output", str(path)]
    elif field == "last_error":
        from brigade import bug_last_error

        bug_last_error.record("version", 1, RuntimeError(narrative))
        flags = []
    assert invoke(target, "--dry-run", "--json", *flags) == 2
    assert "SECURITY.md" in capsys.readouterr().err
    assert not list(Path(mod.data_root()).rglob("bundle.json"))


def test_summary_file_read_is_bounded_before_decoding(report_env, monkeypatch, capsys, tmp_path):
    mod, target = report_env
    summary = tmp_path / "huge.txt"
    summary.write_bytes(b"x" * (mod.BUNDLE_CAP + 1) + b"\xff")
    original_read = os.read
    reads = []

    def bounded_read(fd, size):
        reads.append(size)
        assert 0 < size <= mod.BUNDLE_CAP + 1
        return original_read(fd, size)

    monkeypatch.setattr(os, "read", bounded_read)
    assert cli.main(["bug", "report", "--target", str(target), "--summary-file", str(summary), "--dry-run"]) == 2
    assert not reads, "oversized summary must be rejected before reading or decoding"
    assert "60 KB" in capsys.readouterr().err


@pytest.mark.parametrize("flag", ["--summary-file", "--include-output"])
@pytest.mark.parametrize("kind", ["symlink", "fifo", "swap"])
@pytest.mark.parametrize("fallback", [False, True])
def test_selected_inputs_reject_links_fifos_and_swaps(report_env, monkeypatch, capsys, tmp_path, flag, kind, fallback):
    mod, target = report_env
    if kind == "fifo" and not hasattr(os, "mkfifo"):
        pytest.skip("FIFO creation requires POSIX")
    if fallback:
        _without_posix_io(monkeypatch)
    selected = tmp_path / "selected.txt"
    victim = tmp_path / "victim.txt"
    victim.write_text("unrelated private text")
    if kind == "symlink":
        selected.symlink_to(victim)
    elif kind == "fifo":
        os.mkfifo(selected)
    else:
        selected.write_text("expected log")
        original_read = os.read

        def swap_after_read(fd, size):
            raw = original_read(fd, size)
            if raw and selected.is_file():
                os.replace(victim, selected)
            return raw

        monkeypatch.setattr(os, "read", swap_after_read)
    original_open = Path.open

    def deny_ordinary_input_open(path, *args, **kwargs):
        if path == selected:
            pytest.fail("selected input used an ordinary blocking path open")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny_ordinary_input_open)
    flags = ["--target", str(target), flag, str(selected), "--dry-run", "--json"]
    if flag == "--include-output":
        flags += ["--summary", "Unexpected result"]
    assert cli.main(["bug", "report", *flags]) == 2
    captured = capsys.readouterr()
    assert "blocked" in captured.err
    assert "private text" not in captured.out + captured.err
    assert not list(Path(mod.data_root()).rglob("bundle.json"))


@pytest.mark.parametrize("fallback", [False, True])
def test_selected_output_reads_only_bounded_tail(report_env, monkeypatch, capsys, tmp_path, fallback):
    mod, target = report_env
    if fallback:
        _without_posix_io(monkeypatch)
    selected = tmp_path / "large.log"
    original_read = os.read
    sizes = []

    def bounded_read(fd, size):
        assert 0 < size <= mod.BUNDLE_CAP
        raw = original_read(fd, size)
        sizes.append(len(raw))
        return raw

    monkeypatch.setattr(os, "read", bounded_read)
    # Use many short lines so the resulting bundle remains below its cap.
    selected.write_text("private initial line\n" + "line\n" * mod.BUNDLE_CAP + "final line\n")
    assert invoke(target, "--include-output", str(selected), "--dry-run", "--json") == 0
    preview = result(capsys)
    assert "private initial line" not in preview["preview_markdown"]
    assert "final line" in preview["preview_markdown"]
    assert sum(sizes) <= mod.BUNDLE_CAP


def test_delayed_prepared_submission_still_starts_cooldown(report_env, capsys):
    from datetime import datetime, timedelta, timezone

    _, target = report_env
    assert invoke(target, "--dry-run", "--json") == 0
    preview = result(capsys)
    receipt_path = Path(preview["receipt_path"])
    receipt = json.loads(receipt_path.read_text())
    receipt["created_at"] = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    receipt_path.write_text(json.dumps(receipt))
    args = ["bug", "report", "--report-id", preview["report_id"], "--yes", "--not-security", "--via", "url", "--json"]
    assert cli.main(args) == 0
    result(capsys)
    assert cli.main(args) == 2
    assert "cooldown" in capsys.readouterr().err


@pytest.mark.parametrize(
    "field,value", [("--summary", "changed"), ("--title", "changed"), ("--include-output", "missing.log")]
)
def test_prepared_report_rejects_new_content(report_env, capsys, field, value):
    _, target = report_env
    assert invoke(target, "--dry-run", "--json") == 0
    preview = result(capsys)
    assert cli.main(["bug", "report", "--report-id", preview["report_id"], field, value, "--yes", "--via", "gh"]) == 2
    assert "new report content" in capsys.readouterr().err


def test_prepared_report_rescans_before_search_or_submit(report_env, monkeypatch, capsys):
    from brigade import bug_redaction

    mod, target = report_env
    assert invoke(target, "--dry-run", "--json") == 0
    preview = result(capsys)
    monkeypatch.setattr(bug_redaction, "scan_text", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("scanner")))
    monkeypatch.setattr(mod, "gh", lambda *a: pytest.fail("transport before rescan"))
    assert (
        cli.main(["bug", "report", "--report-id", preview["report_id"], "--yes", "--not-security", "--via", "gh"]) == 2
    )
    assert "second scan failed" in capsys.readouterr().err


def test_prepared_report_id_cannot_escape_store(report_env, monkeypatch, capsys):
    mod, _ = report_env
    monkeypatch.setattr(mod, "gh", lambda *a: pytest.fail("transport with invalid id"))
    assert cli.main(["bug", "report", "--report-id", "../outside", "--yes", "--not-security", "--via", "gh"]) == 2
    assert "invalid --report-id" in capsys.readouterr().err


def test_prepared_unicode_bundle_roundtrips_within_byte_cap(report_env, tmp_path, capsys):
    mod, target = report_env
    output = tmp_path / "unicode.log"
    output.write_text("界" * 12000, encoding="utf-8")
    assert invoke(target, "--include-output", str(output), "--dry-run", "--json") == 0
    preview = result(capsys)
    assert len(preview["preview_markdown"].encode()) < mod.BUNDLE_CAP
    assert (
        cli.main(
            ["bug", "report", "--report-id", preview["report_id"], "--yes", "--not-security", "--via", "url", "--json"]
        )
        == 0
    )
    assert result(capsys)["preview_markdown"] == preview["preview_markdown"]


@pytest.mark.parametrize("via", ["gh", "url", "email"])
def test_yes_requires_not_security_assertion(report_env, monkeypatch, capsys, via):
    mod, target = report_env
    assert invoke(target, "--dry-run", "--json") == 0
    preview = result(capsys)
    monkeypatch.setattr(mod, "gh", lambda *a: pytest.fail("network before security assertion"))
    assert cli.main(["bug", "report", "--report-id", preview["report_id"], "--yes", "--via", via, "--json"]) == 2
    assert "--not-security" in capsys.readouterr().err
    receipt = json.loads(Path(preview["receipt_path"]).read_text())
    assert receipt["submitted"] is False


@pytest.mark.parametrize("security_answer", ["y", "yes", "n", ""])
@pytest.mark.parametrize("yes_flag", [False, True])
def test_interactive_security_confirmation(report_env, monkeypatch, capsys, security_answer, yes_flag):
    import sys

    mod, target = report_env
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    flags = []
    if yes_flag:
        assert invoke(target, "--dry-run", "--json") == 0
        preview = result(capsys)
        flags = ["--report-id", preview["report_id"], "--yes", "--not-security"]
    questions = []
    answers = iter([security_answer, "y"])

    def answer(question):
        questions.append(question)
        return next(answers)

    monkeypatch.setattr("builtins.input", answer)
    security = security_answer in {"y", "yes"}
    code = cli.main(["bug", "report", "--via", "url", *flags]) if yes_flag else invoke(target, "--via", "url")
    assert code == (2 if security else 0)
    captured = capsys.readouterr()
    assert questions[0] == "Is this a security vulnerability? [y/N] "
    receipt_path = next((Path(mod.data_root()) / "brigade" / "bug-reports").glob("*/receipt.json"))
    receipt = json.loads(receipt_path.read_text())
    assert receipt["security_confirmation"] == {"mode": "prompt", "not_security": not security}
    assert receipt["submitted"] is not security
    if security:
        assert len(questions) == 1
        assert "SECURITY.md" in captured.err
    else:
        assert len(questions) == (1 if yes_flag else 2)


@pytest.mark.parametrize("code", [None, 0, 2, 7, "", "dispatch failed"])
def test_dispatch_system_exit_records_only_failure(report_env, monkeypatch, code):
    from brigade import bug_last_error
    from brigade.cli import version

    bug_last_error.record("doctor", 1, RuntimeError("previous"))
    previous = bug_last_error.error_path().read_bytes()
    monkeypatch.setattr(version, "dispatch", lambda args: (_ for _ in ()).throw(SystemExit(code)))
    with pytest.raises(SystemExit) as caught:
        cli.main(["version"])
    assert caught.value.code == code
    if code in (None, 0):
        assert bug_last_error.error_path().read_bytes() == previous
    else:
        record = json.loads(bug_last_error.error_path().read_text())
        assert record["command"] == "version"
        assert record["exit_code"] == (code if isinstance(code, int) else 1)
        assert record["exception_type"] == "SystemExit"


def test_dispatch_parser_error_replaces_stale_diagnostic(report_env):
    from brigade import bug_last_error

    bug_last_error.record("doctor", 1, RuntimeError("previous"))
    with pytest.raises(SystemExit) as caught:
        cli.main(["version", "--json"])
    assert caught.value.code == 2
    record = json.loads(bug_last_error.error_path().read_text())
    assert record["command"] == "version"
    assert record["exit_code"] == 2


def test_report_dispatch_system_exit_preserves_previous_error(report_env, monkeypatch):
    from brigade import bug_last_error
    from brigade.cli import bug

    _, target = report_env
    bug_last_error.record("doctor", 1, RuntimeError("previous"))
    previous = bug_last_error.error_path().read_bytes()
    monkeypatch.setattr(bug, "dispatch", lambda args: (_ for _ in ()).throw(SystemExit(2)))
    with pytest.raises(SystemExit):
        invoke(target, "--dry-run")
    assert bug_last_error.error_path().read_bytes() == previous


def test_older_prepared_vulnerability_is_blocked_before_network(report_env, monkeypatch, capsys):
    import hashlib

    mod, target = report_env
    assert invoke(target, "--dry-run", "--json") == 0
    preview = result(capsys)
    directory = Path(preview["preview_path"]).parent
    bundle = json.loads((directory / "bundle.json").read_text())
    bundle["user"]["summary"] = "sandbox escape exposes private keys"
    markdown = mod.render(bundle)
    (directory / "bundle.json").write_text(json.dumps(bundle))
    (directory / "bundle.md").write_text(markdown)
    receipt = json.loads((directory / "receipt.json").read_text())
    receipt["bundle_digest"] = mod.canonical_json_digest(bundle)
    receipt["preview_digest"] = hashlib.sha256(markdown.encode()).hexdigest()
    (directory / "receipt.json").write_text(json.dumps(receipt))
    monkeypatch.setattr(mod, "gh", lambda *args: pytest.fail("network for a vulnerability"))
    assert cli.main(["bug", "report", "--report-id", preview["report_id"], "--via", "gh"]) == 2
    assert "SECURITY.md" in capsys.readouterr().err


@pytest.mark.parametrize(
    "summary",
    [
        "Pressing Escape closes the prompt",
        "The overwrite confirmation never appears",
        "A race between progress messages makes the display flicker",
        "The privilege column is missing from the table",
        "Escaped quotes render incorrectly",
        "The traversal order changes between runs",
        "Deserialization fails on an empty receipt",
        "brigade security enrich crashes",
        "security.toml fails to parse",
        "memory leak when listing skill keys",
        "status exposes the wrong token count",
    ],
)
def test_benign_ui_language_allows_report(report_env, capsys, summary):
    _, target = report_env
    assert invoke(target, "--summary", summary, "--dry-run", "--json") == 0
    assert summary in result(capsys)["preview_markdown"]


@pytest.mark.parametrize(
    "payload",
    [
        "before\x1b]52;c;Y2xpcGJvYXJk\x07after\rconcealed\x9b2J\x00",
        "before" + "".join(chr(c) for c in [0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A)]) + "after",
    ],
)
def test_terminal_controls_escaped_in_text_preview_and_submission(report_env, monkeypatch, capsys, tmp_path, payload):
    mod, target = report_env
    summary = tmp_path / "summary.txt"
    summary.write_text(payload, encoding="utf-8")
    output = tmp_path / "output.txt"
    output.write_text(payload, encoding="utf-8")
    assert (
        cli.main(
            [
                "bug",
                "report",
                "--target",
                str(target),
                "--summary-file",
                str(summary),
                "--title",
                payload,
                "--include-output",
                str(output),
                "--dry-run",
            ]
        )
        == 0
    )
    text = capsys.readouterr().out
    assert "before" in text and "after" in text
    assert not re.search(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u200e\u200f\u202a-\u202e\u2066-\u2069]", text)
    directory = next((Path(mod.data_root()) / "brigade" / "bug-reports").glob("*/bundle.json")).parent
    bundle = json.loads((directory / "bundle.json").read_text())
    for value in bundle["user"].values():
        assert not re.search(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u200e\u200f\u202a-\u202e\u2066-\u2069]", value)
    assert not re.search(
        r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u200e\u200f\u202a-\u202e\u2066-\u2069]", (directory / "bundle.md").read_text()
    )
    sent = []

    def fake(*args):
        if args[:2] == ("search", "issues"):
            return "[]"
        sent.extend([args[args.index("--title") + 1], Path(args[-1]).read_text()])
        return mod.ISSUES_URL + "/42"

    monkeypatch.setattr(mod, "gh", fake)
    assert (
        cli.main(
            [
                "bug",
                "report",
                "--report-id",
                directory.name,
                "--yes",
                "--not-security",
                "--via",
                "gh",
            ]
        )
        == 0
    )
    assert len(sent) == 2
    assert all(
        not re.search(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u200e\u200f\u202a-\u202e\u2066-\u2069]", value) for value in sent
    )


@pytest.mark.parametrize(
    "control",
    [
        "\x1b]52;c;Y2xpcGJvYXJk\x07\r",
        "\u202e",
        "\u2066",
        "\u2067",
        "\u2068",
        "\u2069",
        "\u200e",
        "\u200f",
        "\u202a",
        "\u202b",
        "\u202c",
        "\u202d",
    ],
)
def test_older_prepared_control_title_blocked_before_network(report_env, monkeypatch, capsys, control):
    import hashlib

    mod, target = report_env
    assert invoke(target, "--dry-run", "--json") == 0
    preview = result(capsys)
    directory = Path(preview["preview_path"]).parent
    bundle = json.loads((directory / "bundle.json").read_text())
    bundle["user"]["title"] = "title" + control + "hidden"
    markdown = mod.render(bundle)
    receipt = json.loads((directory / "receipt.json").read_text())
    receipt["bundle_digest"] = mod.canonical_json_digest(bundle)
    receipt["preview_digest"] = hashlib.sha256(markdown.encode()).hexdigest()
    (directory / "bundle.json").write_text(json.dumps(bundle))
    (directory / "bundle.md").write_text(markdown)
    (directory / "receipt.json").write_text(json.dumps(receipt))
    monkeypatch.setattr(mod, "gh", lambda *a: pytest.fail("unsafe title reached transport"))
    assert (
        cli.main(
            [
                "bug",
                "report",
                "--report-id",
                preview["report_id"],
                "--yes",
                "--not-security",
                "--via",
                "gh",
            ]
        )
        == 2
    )
    assert "control" in capsys.readouterr().err


@pytest.mark.parametrize("transport_fails", [False, True])
def test_attempt_cooldown_crosses_utc_midnight(report_env, monkeypatch, capsys, transport_fails):
    from datetime import datetime, timedelta, timezone

    mod, target = report_env
    now = datetime(2026, 1, 1, 23, 59, tzinfo=timezone.utc)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(mod, "datetime", Clock)
    monkeypatch.setattr(mod, "gh", lambda *a: (_ for _ in ()).throw(OSError("offline")))
    assert invoke(target, "--dry-run", "--json") == 0
    preview = result(capsys)
    args = [
        "bug",
        "report",
        "--report-id",
        preview["report_id"],
        "--yes",
        "--not-security",
        "--via",
        "gh" if transport_fails else "url",
        "--json",
    ]
    assert cli.main(args) == (2 if transport_fails else 0)
    capsys.readouterr()
    now += timedelta(minutes=2)
    assert cli.main(args) == 2
    assert "cooldown" in capsys.readouterr().err
    now += timedelta(minutes=4)
    assert cli.main(args) == (2 if transport_fails else 0)
    capsys.readouterr()
    logs = Path(mod.data_root()) / "brigade" / "bug-reports" / "attempts"
    assert len((logs / "2026-01-01.jsonl").read_text().splitlines()) == 1
    assert len((logs / "2026-01-02.jsonl").read_text().splitlines()) == 1


@pytest.mark.parametrize("fallback", [False, True])
def test_attempt_log_read_is_bounded_and_fails_closed(report_env, monkeypatch, capsys, fallback):
    from datetime import datetime, timezone

    mod, target = report_env
    if fallback:
        _without_posix_io(monkeypatch)
    assert invoke(target, "--dry-run", "--json") == 0
    preview = result(capsys)
    logs = Path(mod.data_root()) / "brigade" / "bug-reports" / "attempts"
    logs.mkdir()
    log = logs / f"{datetime.now(timezone.utc).date().isoformat()}.jsonl"
    log.write_bytes(b"x" * (mod.BUNDLE_CAP + 100))
    original_read = os.read
    inode = log.stat().st_ino

    def bounded_read(fd, size):
        if os.fstat(fd).st_ino == inode:
            pytest.fail("oversized attempt log was read before its size was rejected")
        return original_read(fd, size)

    monkeypatch.setattr(os, "read", bounded_read)
    assert (
        cli.main(
            [
                "bug",
                "report",
                "--report-id",
                preview["report_id"],
                "--yes",
                "--not-security",
                "--via",
                "url",
                "--json",
            ]
        )
        == 2
    )
    assert "60 KB" in capsys.readouterr().err


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "public", "directory-symlink"])
@pytest.mark.parametrize("fallback", [False, True])
def test_submission_refuses_unsafe_attempt_log(report_env, monkeypatch, capsys, tmp_path, kind, fallback):
    from datetime import datetime, timezone

    mod, target = report_env
    if fallback:
        _without_posix_io(monkeypatch)
    assert invoke(target, "--dry-run", "--json") == 0
    preview = result(capsys)
    root = Path(mod.data_root()) / "brigade" / "bug-reports"
    logs = root / "attempts"
    victim = tmp_path / "victim"
    limit = mod._limit_submissions

    def plant_after_limit_check(*args):
        limit(*args)
        if kind == "directory-symlink":
            victim.mkdir()
            logs.symlink_to(victim, target_is_directory=True)
        else:
            logs.mkdir()
            log = logs / f"{datetime.now(timezone.utc).date().isoformat()}.jsonl"
            victim.touch(mode=0o600)
            if kind == "symlink":
                log.symlink_to(victim)
            elif kind == "hardlink":
                log.hardlink_to(victim)
            else:
                log.touch(mode=0o644)

    monkeypatch.setattr(mod, "_limit_submissions", plant_after_limit_check)
    monkeypatch.setattr(mod, "_submit", lambda *a: pytest.fail("unsafe log reached transport"))
    assert (
        cli.main(
            [
                "bug",
                "report",
                "--report-id",
                preview["report_id"],
                "--yes",
                "--not-security",
                "--via",
                "url",
                "--json",
            ]
        )
        == 2
    )
    assert "blocked" in capsys.readouterr().err
    if kind == "directory-symlink":
        assert list(victim.iterdir()) == []
    else:
        assert victim.read_bytes() == b""


@pytest.mark.parametrize("kind", ["oversize", "symlink", "fifo", "directory", "deep-json"])
def test_last_error_reader_rejects_unsafe_or_oversized_files(report_env, monkeypatch, tmp_path, kind):
    from brigade import bug_last_error

    path = tmp_path / "last-error.json"
    monkeypatch.setattr(bug_last_error, "error_path", lambda: path)
    if kind == "oversize":
        # Valid JSON must be rejected before decoding rather than truncated.
        path.write_text(json.dumps({"message_head": "x" * (64 * 1024)}))
    elif kind == "symlink":
        victim = tmp_path / "victim.json"
        victim.write_text('{"command":"untrusted"}')
        path.symlink_to(victim)
    elif kind == "fifo":
        os.mkfifo(path)
        # A no-writer FIFO would hang the vulnerable reader, so fail on an
        # ordinary Path.open before that operation can block.
        original = Path.open

        def deny_fifo(p, *a, **k):
            if p == path:
                pytest.fail("last-error reader used a blocking FIFO open")
            return original(p, *a, **k)

        monkeypatch.setattr(Path, "open", deny_fifo)
    elif kind == "directory":
        path.mkdir()
    else:
        path.write_text("[" * 2000 + "]" * 2000)
    assert bug_last_error.read() is None


def test_dry_run_with_ordinary_developer_environment(report_env, monkeypatch, capsys):
    from brigade.bug_collect import collect_diagnostics

    mod, target = report_env
    monkeypatch.setattr(mod, "collect_diagnostics", collect_diagnostics)
    for name, value in {
        "TOKENIZERS_PARALLELISM": "false",
        "KEYTIMEOUT": "1",
        "TEST_KEY": "1",
        "TEST_TOKEN": "false",
        "TEST_PASSWORD": "short",
        "TEST_CREDENTIAL": "12345678",
        "MONKEY_MODE": "ordinary-value",
        "SHELL": "/bin/bash",
        "TERM": "xterm-256color",
        "SHLVL": "1",
    }.items():
        monkeypatch.setenv(name, value)
    assert invoke(target, "--summary", "false 1 short 12345678 ordinary-value", "--dry-run", "--json") == 0
    preview = result(capsys)
    bundle = json.loads(Path(preview["preview_path"]).with_name("bundle.json").read_text())
    assert bundle["user"]["summary"] == "false 1 short 12345678 ordinary-value"
    assert bundle["bug_diagnostics"]["checks"]
    assert "doctor" not in bundle
    assert preview["submitted"] is False


def test_security_config_last_error_allows_ordinary_report(report_env, capsys):
    from brigade import bug_last_error

    mod, target = report_env
    mod.write_json(bug_last_error.error_path(), {"message_head": "invalid .brigade/security.toml: syntax"})
    assert invoke(target, "--dry-run", "--json") == 0
    assert result(capsys)["report_id"]


def test_same_failure_with_normalized_summary_shares_fingerprint_and_cooldown(report_env, monkeypatch, capsys):
    from brigade import bug_last_error

    mod, target = report_env
    bug_last_error.record("doctor", 1, ValueError("profile missing"))
    monkeypatch.setattr(mod, "gh", lambda *a: "[]")
    assert submit_prepared(target, capsys, "--via", "url", "--json", summary="Profile output stops at row 123") == 0
    first = result(capsys)
    assert invoke(target, "--summary", "PROFILE output stops at row 999", "--dry-run", "--json") == 0
    second = result(capsys)
    assert first["fingerprint"] == second["fingerprint"]
    assert (
        cli.main(["bug", "report", "--report-id", second["report_id"], "--yes", "--not-security", "--via", "url"]) == 2
    )
    assert "cooldown" in capsys.readouterr().err


@pytest.mark.parametrize("fallback", [False, True])
def test_report_allows_symlinked_input_and_data_ancestors(report_env, monkeypatch, capsys, tmp_path, fallback):
    from brigade import bug_last_error

    mod, target = report_env
    if fallback:
        _without_posix_io(monkeypatch)
    actual = tmp_path / "actual"
    actual.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    monkeypatch.setenv("XDG_DATA_HOME", str(alias / "data"))
    monkeypatch.setenv("LOCALAPPDATA", str(alias / "data"))
    (actual / "summary.txt").write_text("Unexpected result")
    (actual / "output.log").write_text("newest output")
    bug_last_error.record("doctor", 1, ValueError("missing profile"))
    assert (
        cli.main(
            [
                "bug",
                "report",
                "--target",
                str(target),
                "--summary-file",
                str(alias / "summary.txt"),
                "--include-output",
                str(alias / "output.log"),
                "--dry-run",
                "--json",
            ]
        )
        == 0
    )
    preview = result(capsys)
    bundle = json.loads(Path(preview["preview_path"]).with_name("bundle.json").read_text())
    assert bundle["last_error"]["command"] == "doctor"
    assert bundle["user"]["output_tail"] == "newest output"
    monkeypatch.setattr(mod, "gh", lambda *a: "[]")
    assert (
        cli.main(
            ["bug", "report", "--report-id", preview["report_id"], "--yes", "--not-security", "--via", "url", "--json"]
        )
        == 0
    )
    assert result(capsys)["submitted"] is True


def test_submitted_report_cannot_be_retried_after_cooldown(report_env, monkeypatch, capsys):
    from datetime import datetime, timedelta, timezone

    mod, target = report_env
    calls = []

    def transport(*args):
        calls.append(args)
        return "[]" if args[:2] == ("search", "issues") else mod.ISSUES_URL + "/42"

    monkeypatch.setattr(mod, "gh", transport)
    assert submit_prepared(target, capsys, "--via", "gh", "--json") == 0
    submitted = result(capsys)
    receipt_path = Path(submitted["receipt_path"])
    original = receipt_path.read_bytes()
    future = datetime.now(timezone.utc) + timedelta(minutes=6)

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return future

    monkeypatch.setattr(mod, "datetime", Later)
    calls.clear()
    assert (
        cli.main(
            ["bug", "report", "--report-id", submitted["report_id"], "--yes", "--not-security", "--via", "gh", "--json"]
        )
        == 2
    )
    assert f"already submitted: {mod.ISSUES_URL}/42" in capsys.readouterr().err
    assert not calls
    assert receipt_path.read_bytes() == original


@pytest.mark.parametrize("unit", ['"\\' * 200, "漢" * 140], ids=["escaped", "unicode"])
def test_large_output_keeps_budgeted_newest_redacted_bytes(report_env, capsys, tmp_path, unit):
    mod, target = report_env
    log = tmp_path / "run.log"
    log.write_text((unit + "\n") * 200 + "newest line\n", encoding="utf-8")
    assert invoke(target, "--include-output", str(log), "--dry-run", "--json") == 0
    preview = result(capsys)
    path = Path(preview["preview_path"])
    bundle = json.loads(path.with_name("bundle.json").read_text())
    tail = bundle["user"]["output_tail"]
    assert tail.endswith("newest line")
    assert len(tail.encode()) <= 16 * 1024
    assert bundle["included_output"]["bytes_kept"] == len(tail.encode())
    assert bundle["included_output"]["truncated"] is True
    assert max(path.stat().st_size, path.with_name("bundle.json").stat().st_size) <= mod.BUNDLE_CAP


def test_diagnostics_child_can_import_parent_package_without_workspace_imports(report_env, monkeypatch, tmp_path):
    import sys
    from brigade import bug_collect

    _, target = report_env
    # The base interpreter cannot see the virtualenv's editable installation.
    monkeypatch.setattr(sys, "executable", sys._base_executable)
    poisoned = tmp_path / "poisoned"
    poisoned.mkdir()
    (poisoned / "brigade.py").write_text("raise RuntimeError('untrusted module imported')")
    monkeypatch.setenv("PYTHONPATH", str(poisoned))
    monkeypatch.chdir(target)
    checks = bug_collect.collect_diagnostics(target)["checks"]
    assert {"status": "FAIL", "name": "bootstrap: AGENTS.md"} in checks
    assert not any("unavailable" in check["name"] for check in checks)


@pytest.mark.parametrize("age_hours", [0, 25])
def test_last_error_timestamp_expires_stale_failure(report_env, capsys, age_hours):
    from datetime import datetime, timedelta, timezone
    from brigade import bug_last_error

    mod, target = report_env
    bug_last_error.record("doctor", 1, ValueError("previous failure"))
    path = bug_last_error.error_path()
    record = json.loads(path.read_text())
    assert datetime.fromisoformat(record["recorded_at"]).tzinfo is not None
    record["recorded_at"] = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat()
    mod.write_json(path, record)
    assert invoke(target, "--dry-run", "--json") == 0
    preview = result(capsys)
    bundle = json.loads(Path(preview["preview_path"]).with_name("bundle.json").read_text())
    if age_hours:
        assert bundle["last_error"] is None
    else:
        assert bundle["last_error"]["recorded_at"] == record["recorded_at"]


@pytest.mark.parametrize("valid", [True, False])
def test_dedup_uses_bounded_unauthenticated_api_when_gh_fails(report_env, monkeypatch, capsys, valid):
    from io import BytesIO
    from urllib.parse import parse_qs, urlsplit

    mod, target = report_env
    requested = []

    class Response(BytesIO):
        def read(self, size=-1):
            assert 0 < size <= mod.BUNDLE_CAP + 1
            return super().read(size)

    def fetch(request, **kwargs):
        requested.append(request)
        assert kwargs["timeout"] <= 10
        value = {"items": [{"number": 51, "title": "profile fails", "state": "open"}]}
        return Response(json.dumps(value).encode() if valid else b"x" * (mod.BUNDLE_CAP + 1))

    monkeypatch.setattr(mod, "urlopen", fetch)
    assert invoke(target, "--via", "gh", "--json") == 0
    preview = result(capsys)
    assert len(requested) == 1
    request = requested[0]
    assert not request.has_header("Authorization")
    url = urlsplit(request.full_url)
    assert (url.scheme, url.netloc, url.path) == ("https", "api.github.com", "/search/issues")
    assert parse_qs(url.query)["q"] == [f"repo:{mod.REPOSITORY} is:issue {preview['fingerprint']}"]
    assert preview["dedup_status"] == ("searched" if valid else "dedup skipped")
    assert [row["number"] for row in preview["duplicates"]] == ([51] if valid else [])
    assert preview["submitted"] is False
