"""Additional public-report leak patterns use fake adversarial values."""

import json
import os
import re

import pytest
from brigade.bug_redaction import Redactor, ReportBlocked, assert_clean
from brigade.guard.engine import scan_text


@pytest.fixture(autouse=True)
def isolated_secret_environment(monkeypatch):
    # Only credentials seeded by these tests may influence their second scan.
    for name in os.environ:
        if re.search(r"TOKEN|KEY|SECRET|PASSWORD|PASS|CREDENTIAL", name, re.I):
            monkeypatch.delenv(name)


@pytest.mark.parametrize(
    "value,rule",
    [
        (r"C:\Users\example-user\private file.txt", "windows-home-path"),
        ("/root/private", "root-home-path"),
        ("ghp_" + "a" * 36, "bare-token"),
        ("gho_" + "a" * 36, "bare-token"),
        ("ghu_" + "a" * 36, "bare-token"),
        ("ghs_" + "a" * 36, "bare-token"),
        ("ghr_" + "a" * 36, "bare-token"),
        ("github_pat_" + "b" * 70, "bare-token"),
        ("sk-" + "c" * 32, "bare-token"),
        ("AKIA" + "A" * 16, "bare-token"),
        ("xoxb-" + "1234567890-" * 3, "bare-token"),
        ("build.private.internal", "private-hostname"),
        ("hostname: build-node", "private-hostname"),
    ],
)
def test_report_leak_patterns(value, rule, tmp_path):
    with pytest.raises(ReportBlocked, match="remaining findings"):
        assert_clean(value)
    redactor = Redactor(tmp_path)
    redacted = redactor.text(value)
    assert value not in redacted
    assert redactor.record()["detectors"][rule] == 1
    assert_clean(redacted)


@pytest.mark.parametrize(
    "value",
    [r"C:\Users\example-user\private.txt", "/root/private", "ghp_" + "a" * 36, "build.private.internal"],
)
def test_bug_detectors_do_not_change_repository_guard(value):
    assert not scan_text(value).findings


def test_python_home_call_is_not_a_hostname():
    assert_clean("Path.home() and self.local()")


def test_python_host_arguments_and_type_annotations_are_not_hostnames():
    assert_clean("host: str, host: object, host=host, host=config.host, hostname = parts.hostname")


@pytest.mark.parametrize("value", ["BuildNode", "BUILDNODE", "buildnode", "bUiLdNoDe"])
def test_local_hostname_is_case_insensitive(tmp_path, monkeypatch, value):
    monkeypatch.setattr("brigade.bug_redaction.socket.gethostname", lambda: "BuildNode")
    redactor = Redactor(tmp_path)
    redacted = redactor.text("failure on " + value)
    assert value not in redacted
    assert redacted == "failure on <local>"
    assert_clean(redacted)


def test_hostname_matching_does_not_fold_other_local_literals(tmp_path, monkeypatch):
    monkeypatch.setattr("brigade.bug_redaction.socket.gethostname", lambda: "BuildNode")
    monkeypatch.setenv("TEST_CREDENTIAL", "CaseSensitiveValue")
    redactor = Redactor(tmp_path)
    assert redactor.text("BUILDNODE casesensitivevalue") == "<local> casesensitivevalue"


@pytest.mark.parametrize("secret", ["q", "qx", "qxy", "x-y", "q\nx", 'q"x', "q\u2603x"])
def test_short_secret_blocks_second_scan_without_corrupting_text(tmp_path, monkeypatch, secret):
    monkeypatch.setenv("TEST_CREDENTIAL", secret)
    text = f"prefix{secret}postfix"
    redacted = Redactor(tmp_path).text(text)
    assert redacted == text
    for candidate in (redacted, json.dumps({"detail": redacted}), json.dumps({"detail": redacted}, ensure_ascii=False)):
        with pytest.raises(ReportBlocked, match="TEST_CREDENTIAL") as blocked:
            assert_clean(candidate)
        assert secret not in str(blocked.value)


def test_long_secret_is_scrubbed_inside_words(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_CREDENTIAL", "qxyz")
    redacted = Redactor(tmp_path).text("prefixqxyzpostfix")
    assert redacted == "prefix<local>postfix"
    assert_clean(redacted)
