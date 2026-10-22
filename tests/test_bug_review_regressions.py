"""Regression contracts for bug-report identity and safe local collection."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from brigade import bug_doctor, bug_io, bug_last_error, bug_redaction, cli
from tests.test_bug_report import invoke, result
from tests.test_bug_report import report_env as report_env


def test_described_bugs_use_normalized_summary_without_failure_facts(report_env, monkeypatch, capsys):
    mod, target = report_env
    monkeypatch.setattr(mod, "collect_diagnostics", lambda target: {"checks": [], "summary": {}})
    fingerprints = []
    for summary in (
        "WRONG count 123 for /var/one/result id abcd1234",
        "wrong count 999 for /opt/two/result id ffff9876",
        "missing heading in rendered output",
    ):
        assert invoke(target, "--summary", summary, "--dry-run", "--json") == 0
        fingerprints.append(result(capsys)["fingerprint"])
    assert fingerprints[0] == fingerprints[1]
    assert fingerprints[0] != fingerprints[2]


@pytest.mark.parametrize(
    "identity",
    [
        None,
        {"command": "work verify run", "exit_code": 1},
        {"command": "doctor", "exit_code": 1, "exception_type": "ValueError"},
    ],
)
def test_different_summaries_share_fingerprint_when_failure_facts_exist(report_env, monkeypatch, capsys, identity):
    mod, target = report_env
    monkeypatch.setattr(mod, "collect_diagnostics", lambda target: {"checks": [], "summary": {}})
    if identity is not None:
        mod.write_json(bug_last_error.error_path(), identity)
    fingerprints = []
    for summary in ("wrong count", "missing heading"):
        assert invoke(target, "--summary", summary, "--dry-run", "--json") == 0
        fingerprints.append(result(capsys)["fingerprint"])
    assert (fingerprints[0] == fingerprints[1]) is (identity is not None)


def test_usage_error_preserves_real_failure_diagnostic(report_env, capsys):
    bug_last_error.record("doctor", 1, ValueError("profile failed"))
    with pytest.raises(SystemExit) as exc:
        cli.main(["version", "--json"])
    assert exc.value.code == 2
    capsys.readouterr()
    diagnostic = bug_last_error.read()
    assert diagnostic["command"] == "doctor"
    assert diagnostic["exception_type"] == "ValueError"
    assert diagnostic["message_head"] == "profile failed"


@pytest.mark.parametrize("age_days", [0, 2])
@pytest.mark.parametrize("retry", ["search-fails", "fallback-only", "comment", "match"])
def test_pending_submission_retry_after_cooldown(report_env, monkeypatch, capsys, retry, age_days):
    mod, target = report_env
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(mod, "datetime", Clock)
    bug_last_error.record("doctor", 1, ValueError("profile failed"))
    calls = []
    issue = {"number": 321, "title": "same command failure", "state": "open"}

    def fake_gh(*args):
        calls.append(args)
        if args[:2] == ("search", "issues"):
            if now.minute == 0:
                return "[]"
            if retry == "search-fails":
                raise OSError("search unavailable")
            if retry == "fallback-only" and args[2].startswith("bf-"):
                return "[]"
            return json.dumps([issue])
        if now.minute == 0:
            raise OSError("submission outcome unknown")
        return mod.ISSUES_URL + "/322"

    monkeypatch.setattr(mod, "gh", fake_gh)
    assert invoke(target, "--dry-run", "--json") == 0
    preview = result(capsys)
    args = ["bug", "report", "--report-id", preview["report_id"], "--yes", "--not-security", "--via", "gh", "--json"]
    assert cli.main(args) == 2
    capsys.readouterr()
    assert json.loads(Path(preview["receipt_path"]).read_text())["status"] == "pending"
    now += timedelta(days=age_days, minutes=6)
    if retry == "comment":
        args += ["--comment-on", "321"]
    expected = 2 if retry in {"search-fails", "match"} else 0
    assert cli.main(args) == expected
    if expected == 2:
        error = capsys.readouterr().err
        assert ("cannot confirm" if retry == "search-fails" else "matching issue found") in error
        if retry == "match":
            assert "--comment-on" in error
        assert json.loads(Path(preview["receipt_path"]).read_text())["status"] == "pending"
    else:
        submitted = result(capsys)
        assert submitted["submitted"]
        assert submitted["destination_url"] == mod.ISSUES_URL + ("/321" if retry == "comment" else "/322")
    creates = [call for call in calls if call[:2] == ("issue", "create")]
    comments = [call for call in calls if call[:2] == ("issue", "comment")]
    assert len(creates) == (2 if retry == "fallback-only" else 1)
    assert len(comments) == (1 if retry == "comment" else 0)


def test_yes_requires_explicit_comment_when_fingerprint_already_exists(report_env, monkeypatch, capsys):
    mod, target = report_env
    issue = {"number": 321, "title": "same failure", "state": "open"}
    monkeypatch.setattr(mod, "gh", lambda *args: json.dumps([issue]))
    monkeypatch.setattr(mod, "_submit", lambda *args: pytest.fail("duplicate issue submitted"))
    assert invoke(target, "--dry-run", "--json") == 0
    preview = result(capsys)
    assert (
        cli.main(["bug", "report", "--report-id", preview["report_id"], "--yes", "--not-security", "--via", "gh"]) == 2
    )
    assert "--comment-on" in capsys.readouterr().err
    assert json.loads(Path(preview["receipt_path"]).read_text())["status"] == "prepared"


@pytest.mark.parametrize("reuse_report", [False, True])
@pytest.mark.parametrize("comment_on", [321, 322])
def test_pending_comment_retry_requires_confirmation(report_env, monkeypatch, capsys, reuse_report, comment_on):
    mod, target = report_env
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(mod, "datetime", Clock)
    comments = []

    def fake_gh(*args):
        if args[:2] == ("search", "issues"):
            return json.dumps([{"number": number, "title": "same failure", "state": "open"} for number in (321, 322)])
        assert args[:2] == ("issue", "comment")
        comments.append(args)
        if len(comments) == 1:
            raise OSError("comment outcome unknown")
        return ""

    monkeypatch.setattr(mod, "gh", fake_gh)
    assert invoke(target, "--dry-run", "--json") == 0
    preview = result(capsys)
    args = ["bug", "report", "--report-id", preview["report_id"], "--yes", "--not-security", "--via", "gh", "--json"]
    assert cli.main([*args, "--comment-on", "321"]) == 2
    capsys.readouterr()
    now += timedelta(days=2 if reuse_report else 0, minutes=6)
    if not reuse_report:
        assert invoke(target, "--dry-run", "--json") == 0
        args[3] = result(capsys)["report_id"]
    assert cli.main([*args, "--comment-on", str(comment_on)]) == (2 if comment_on == 321 else 0)
    if comment_on == 321:
        assert "cannot confirm" in capsys.readouterr().err
        assert len(comments) == 1
        assert json.loads(Path(preview["receipt_path"]).read_text())["status"] == "pending"
    else:
        assert result(capsys)["destination_url"] == mod.ISSUES_URL + "/322"
        assert len(comments) == 2


@pytest.mark.parametrize("reuse_report", [False, True])
@pytest.mark.parametrize("via", ["url", "email"])
def test_failed_link_handoff_can_retry_without_duplicate_search(report_env, monkeypatch, capsys, reuse_report, via):
    mod, target = report_env
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(mod, "datetime", Clock)
    budget_name = "URL_BUDGET" if via == "url" else "EMAIL_BUDGET"
    budget = getattr(mod, budget_name)
    monkeypatch.setattr(mod, budget_name, 1)
    assert invoke(target, "--dry-run", "--json") == 0
    preview = result(capsys)
    args = ["bug", "report", "--report-id", preview["report_id"], "--yes", "--not-security", "--via", via, "--json"]
    assert cli.main(args) == 2
    assert "destination URL exceeds budget" in capsys.readouterr().err
    assert json.loads(Path(preview["receipt_path"]).read_text())["status"] == "pending"
    now += timedelta(minutes=6)
    monkeypatch.setattr(mod, budget_name, budget)
    if not reuse_report:
        assert invoke(target, "--dry-run", "--json") == 0
        args[3] = result(capsys)["report_id"]
    assert cli.main(args) == 0
    submitted = result(capsys)
    assert submitted["dedup_status"] == "dedup skipped"
    assert submitted["destination_url"].startswith(mod.ISSUES_URL + "/new?" if via == "url" else "mailto:")
    assert json.loads(Path(submitted["receipt_path"]).read_text())["status"] == "handoff"


@pytest.mark.parametrize("hostname", ["main", "brigade", "exampleuser"])
def test_hostname_boundaries_preserve_identifiers(tmp_path, monkeypatch, hostname):
    monkeypatch.setattr(bug_redaction.socket, "gethostname", lambda: hostname)
    monkeypatch.setattr(bug_redaction.getpass, "getuser", lambda: "exampleuser")
    text = f"{hostname.upper()}, brigade.cli:main_handler:269 exampleuser_extra module.exampleuser"
    assert bug_redaction.Redactor(tmp_path).text(text) == (
        "<local>, brigade.cli:main_handler:269 exampleuser_extra module.exampleuser"
    )


def test_unclean_error_replaces_previous_record_with_minimal_current_error(report_env, monkeypatch):
    bug_last_error.record("doctor", 1, ValueError("previous error"))
    monkeypatch.setattr(
        bug_last_error,
        "frames",
        lambda tb: ["brigade.internal:main:12"],  # content-guard: allow private-hostname
    )
    bug_last_error.record("work verify", 2, RuntimeError("current error"))
    record = bug_last_error.read()
    assert record["command"] == "work verify"
    assert record["exception_type"] == "RuntimeError"
    assert record["exit_code"] == 2
    assert record["message_head"] == ""
    assert record["frames"] == []
    bug_redaction.assert_clean(json.dumps(record))


@pytest.mark.parametrize("interruption", [EOFError, KeyboardInterrupt])
@pytest.mark.parametrize("stage", ["security", "duplicate", "consent"])
def test_interrupted_prompt_refuses_without_traceback(report_env, monkeypatch, capsys, interruption, stage):
    mod, target = report_env
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    duplicate = {"number": 42, "url": mod.ISSUES_URL + "/42", "state": "open", "title": "similar failure"}
    monkeypatch.setattr(mod, "gh", lambda *args: json.dumps([duplicate]))
    answers = iter([] if stage == "security" else ["n"] if stage == "duplicate" else ["n", "new"])

    def answer(prompt):
        try:
            return next(answers)
        except StopIteration:
            raise interruption("private prompt context") from None

    monkeypatch.setattr("builtins.input", answer)
    monkeypatch.setattr(mod, "_submit", lambda *args: pytest.fail("interrupted prompt submitted"))
    assert invoke(target, "--via", "gh") == 2
    captured = capsys.readouterr()
    assert "cancelled" in captured.err
    assert "private prompt context" not in captured.err
    assert "Traceback" not in captured.err
    receipt_path = next((Path(mod.data_root()) / "brigade" / "bug-reports").glob("*/receipt.json"))
    assert json.loads(receipt_path.read_text())["submitted"] is False


@pytest.mark.parametrize("malformation", ["missing-key", "wrong-type"])
def test_digest_matching_malformed_bundle_refuses_safely(report_env, capsys, malformation):
    mod, target = report_env
    assert invoke(target, "--dry-run", "--json") == 0
    preview = result(capsys)
    bundle_path = Path(preview["preview_path"]).with_name("bundle.json")
    bundle = json.loads(bundle_path.read_text())
    if malformation == "missing-key":
        del bundle["runtime"]["release"]
    else:
        bundle["runtime"]["release"] = None
    mod.write_json(bundle_path, bundle)
    receipt_path = Path(preview["receipt_path"])
    receipt = json.loads(receipt_path.read_text())
    receipt["bundle_digest"] = mod.canonical_json_digest(bundle)
    receipt["preview_digest"] = hashlib.sha256(Path(preview["preview_path"]).read_bytes()).hexdigest()
    mod.write_json(receipt_path, receipt)
    assert cli.main(["bug", "report", "--report-id", preview["report_id"], "--dry-run"]) == 2
    assert "Bug report blocked: local collection or submission failed" in capsys.readouterr().err


@pytest.mark.parametrize("tail", [False, True])
@pytest.mark.parametrize("fallback", [False, True])
def test_growing_log_tail_is_allowed_but_exact_reads_still_refuse(tmp_path, monkeypatch, tail, fallback):
    path = tmp_path / "output.log"
    path.write_bytes(b"initial output\n")
    if fallback:
        monkeypatch.setattr(bug_io, "_supports_dirfd", lambda: False)
    original = os.read
    grown = False

    def growing_read(fd, size):
        nonlocal grown
        raw = original(fd, size)
        if not grown:
            grown = True
            with path.open("ab") as stream:
                stream.write(b"new output\n")
        return raw

    monkeypatch.setattr(os, "read", growing_read)
    if tail:
        assert bug_io.read_tail(path, limit=100) == b"initial output\nnew output\n"
    else:
        with pytest.raises(OSError, match="file changed"):
            bug_io.read_bytes(path, limit=100)


@pytest.mark.parametrize("config_present", [False, True])
def test_healthy_minimal_workspace_has_no_optional_file_warnings(tmp_path, config_present):
    (tmp_path / "AGENTS.md").write_text("# Definition of Done\nRecord a handoff.\n")
    if config_present:
        (tmp_path / ".brigade").mkdir()
        (tmp_path / ".brigade" / "security.toml").write_text("")
    checks = [
        row for name in ("bootstrap", "agents-quality", "security-config") for row in bug_doctor.check(tmp_path, name)
    ]
    assert checks == []


@pytest.mark.parametrize("error_type", ["incomplete", "bad-status", "url"])
def test_api_protocol_failure_still_previews(report_env, monkeypatch, capsys, error_type):
    from http.client import BadStatusLine, IncompleteRead
    from urllib.error import URLError

    mod, target = report_env
    error = {
        "incomplete": IncompleteRead(b"private bytes"),
        "bad-status": BadStatusLine("private bytes"),
        "url": URLError("private bytes"),
    }[error_type]

    def fetch(*args, **kwargs):
        raise error

    monkeypatch.setattr(mod, "urlopen", fetch)
    assert invoke(target, "--via", "url", "--json") == 0
    preview = result(capsys)
    assert preview["dedup_status"] == "dedup skipped"
    assert "### Full output" in preview["preview_markdown"]
    assert not preview["submitted"]
    assert "private bytes" not in json.dumps(preview)


@pytest.mark.parametrize(
    "channel,coordinate,expected",
    [
        ("stable", None, "stable"),
        ("beta", None, "beta"),
        ("beta", "0.0.0", "unknown"),
        ("invalid", None, "unknown"),
        (None, None, "unknown"),
    ],
)
def test_report_channel_uses_matching_install_state(report_env, capsys, channel, coordinate, expected):
    from brigade import __version__, component_paths

    mod, target = report_env
    if channel is not None:
        mod.write_json(
            Path(component_paths.update_state_path(mod.data_root())),
            {"schema_version": 1, "channel": channel, "cli_coordinate": coordinate or __version__},
        )
    assert invoke(target, "--dry-run", "--json") == 0
    preview = result(capsys)
    bundle = json.loads(Path(preview["preview_path"]).with_name("bundle.json").read_text())
    assert bundle["brigade"]["channel"] == expected


@pytest.mark.parametrize("interactive", [False, True])
@pytest.mark.parametrize("field", ["summary", "last_error", "probe"])
def test_not_security_overrides_classifier_through_saved_submission(
    report_env, monkeypatch, capsys, interactive, field
):
    mod, target = report_env
    narrative = "credential leak in the displayed count"
    flags = ["--summary", narrative] if field == "summary" else []
    if field == "last_error":
        bug_last_error.record("status", 1, ValueError(narrative))
    elif field == "probe":
        monkeypatch.setattr(
            mod,
            "collect_diagnostics",
            lambda target: {"checks": [{"name": "security issue in count display", "status": "FAIL"}], "summary": {}},
        )
    assert invoke(target, *flags, "--not-security", "--dry-run", "--json") == 0
    preview = result(capsys)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: interactive)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: interactive)
    monkeypatch.setattr("builtins.input", lambda prompt: "n" if "vulnerability" in prompt else "y")
    monkeypatch.setattr(mod, "gh", lambda *args: "[]")
    assert (
        cli.main(["bug", "report", "--report-id", preview["report_id"], "--not-security", "--yes", "--via", "url"]) == 0
    )
    capsys.readouterr()
    receipt = json.loads(Path(preview["receipt_path"]).read_text())
    assert receipt["submitted"]
    assert receipt["security_confirmation"] == {"mode": "prompt" if interactive else "flag", "not_security": True}


def test_not_security_never_overrides_explicit_security_kind(report_env, capsys):
    mod, target = report_env
    assert invoke(target, "--kind", "security", "--not-security", "--dry-run") == 2
    assert "SECURITY.md" in capsys.readouterr().err
    assert not list(Path(mod.data_root()).rglob("bundle.json"))
