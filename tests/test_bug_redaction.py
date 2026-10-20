"""Additional public-report leak patterns use fake adversarial values."""

import pytest
from brigade.bug_redaction import Redactor, ReportBlocked, assert_clean
from brigade.guard.engine import scan_text


@pytest.mark.parametrize(
    "value,rule",
    [
        (r"C:\Users\example-user\private file.txt", "windows-home-path"),
        ("/root/private", "root-home-path"),
        ("ghp_" + "a" * 36, "bare-token"),
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
