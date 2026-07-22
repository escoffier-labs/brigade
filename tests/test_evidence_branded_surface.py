"""Branded evidence surface: show/explain/stats passthroughs, status timeout
override, doctor check labels, and no engine-name leaks in user guidance.

The evidence engine binary stays miseledger behind the process boundary; the
`brigade evidence` surface is what users see and type.
"""

from __future__ import annotations

import json

import pytest

from brigade import cli, evidence_brief, evidence_cmd, proc


def _patch_engine(monkeypatch, *, run_calls: list | None = None):
    def fake_run(args, timeout=30.0, **kwargs):
        if run_calls is not None:
            run_calls.append({"args": list(args), "timeout": timeout, "kwargs": kwargs})
        exit_code = 0
        if args and args[-1].isdigit() and len(args) >= 2 and args[-2] == "--test-exit":
            exit_code = int(args[-1])
        stdout = '{"ok": true}\n' if "--json" in args else "engine-stdout\n"
        return proc.Result(exit_code, stdout, "")

    monkeypatch.setattr(evidence_brief, "_miseledger_bin", lambda: "/fake/miseledger")
    monkeypatch.setattr(proc, "run", fake_run)


# --- show / explain / stats passthroughs ------------------------------------


@pytest.mark.parametrize("verb", ("show", "explain", "stats"))
def test_evidence_verb_forwards_exact_argv_and_stdout(monkeypatch, capsys, verb):
    calls: list[dict] = []
    _patch_engine(monkeypatch, run_calls=calls)

    rc = cli.main(["evidence", verb, "18ae49710e71", "--json"])

    assert rc == 0
    assert calls == [{"args": ["/fake/miseledger", verb, "18ae49710e71", "--json"], "timeout": 30.0, "kwargs": {}}]
    assert json.loads(capsys.readouterr().out) == {"ok": True}


@pytest.mark.parametrize("verb", ("show", "explain", "stats"))
def test_evidence_verb_propagates_child_exit_code(monkeypatch, verb):
    calls: list[dict] = []
    _patch_engine(monkeypatch, run_calls=calls)

    rc = cli.main(["evidence", verb, "item", "--test-exit", "5"])

    assert rc == 5
    assert calls[0]["args"][:2] == ["/fake/miseledger", verb]


@pytest.mark.parametrize("verb", ("show", "explain", "stats"))
def test_evidence_verb_missing_engine_exits_127(monkeypatch, capsys, verb):
    monkeypatch.setattr(evidence_brief, "_miseledger_bin", lambda: None)

    rc = cli.main(["evidence", verb, "item"])

    assert rc == 127
    err = capsys.readouterr().err
    assert "evidence engine" in err
    assert "brigade setup" in err


# --- query verb timeout override ---------------------------------------------


@pytest.mark.parametrize("verb", ("search", "show", "explain", "stats"))
def test_evidence_query_verbs_use_configured_timeout(monkeypatch, verb):
    calls: list[dict] = []
    _patch_engine(monkeypatch, run_calls=calls)
    monkeypatch.setenv("BRIGADE_EVIDENCE_TIMEOUT_SECONDS", "300")

    assert cli.main(["evidence", verb, "item"]) == 0
    assert calls[0]["timeout"] == 300.0


def test_evidence_query_verb_rejects_invalid_timeout_before_starting_engine(monkeypatch, capsys):
    calls: list[dict] = []
    _patch_engine(monkeypatch, run_calls=calls)
    monkeypatch.setenv("BRIGADE_EVIDENCE_TIMEOUT_SECONDS", "nan")

    assert cli.main(["evidence", "stats"]) == 2
    assert calls == []
    assert "BRIGADE_EVIDENCE_TIMEOUT_SECONDS" in capsys.readouterr().err


def test_evidence_query_verb_timeout_prints_branded_retry_hint(monkeypatch, capsys):
    monkeypatch.setattr(evidence_brief, "_miseledger_bin", lambda: "/fake/miseledger")
    monkeypatch.setattr(proc, "run", lambda *a, **k: proc.Result(124, "", "timeout after 30.0s\n"))

    rc = cli.main(["evidence", "stats"])
    err = capsys.readouterr().err

    assert rc == 124
    assert "BRIGADE_EVIDENCE_TIMEOUT_SECONDS" in err
    assert "brigade evidence stats" in err


# --- status timeout override -------------------------------------------------


@pytest.mark.parametrize("value", ("", "zero", "0", "-1", "nan", "inf"))
def test_evidence_status_rejects_invalid_timeout_before_starting_engine(monkeypatch, capsys, value):
    calls: list[dict] = []
    _patch_engine(monkeypatch, run_calls=calls)
    monkeypatch.setenv("BRIGADE_EVIDENCE_STATUS_TIMEOUT_SECONDS", value)

    assert cli.main(["evidence", "status", "--target", "."]) == 2
    assert calls == []
    assert "BRIGADE_EVIDENCE_STATUS_TIMEOUT_SECONDS" in capsys.readouterr().err


def test_evidence_status_uses_configured_timeout(monkeypatch, tmp_path):
    calls: list[dict] = []
    _patch_engine(monkeypatch, run_calls=calls)
    monkeypatch.setenv("BRIGADE_EVIDENCE_STATUS_TIMEOUT_SECONDS", "300")

    assert cli.main(["evidence", "status", "--target", str(tmp_path)]) == 0
    status_calls = [c for c in calls if c["args"][:2] == ["/fake/miseledger", "status"]]
    assert status_calls
    assert all(c["timeout"] == 300.0 for c in status_calls)


def test_evidence_status_timeout_summary_offers_branded_retry(monkeypatch, tmp_path):
    monkeypatch.setattr(evidence_brief, "_miseledger_bin", lambda: "/fake/miseledger")
    monkeypatch.setattr(proc, "run", lambda *a, **k: proc.Result(124, "", "timeout\n"))

    payload = evidence_cmd.status_payload(tmp_path)

    assert payload["health"] == "timeout"
    assert "BRIGADE_EVIDENCE_STATUS_TIMEOUT_SECONDS" in payload["summary"]
    for command in payload["next_commands"]:
        assert not command.startswith("miseledger")


def test_evidence_status_next_commands_never_name_the_engine_binary(monkeypatch, tmp_path):
    """No health state may tell the user to run the engine binary directly."""
    monkeypatch.setattr(evidence_brief, "_miseledger_bin", lambda: "/fake/miseledger")

    for exit_code in (0, 2, 3, 124):
        monkeypatch.setattr(proc, "run", lambda *a, _code=exit_code, **k: proc.Result(_code, "", ""))
        payload = evidence_cmd.status_payload(tmp_path)
        for command in payload["next_commands"]:
            assert not command.startswith("miseledger"), (exit_code, command)


# --- doctor check labels ------------------------------------------------------


def test_check_label_supports_ok_bool_rows():
    assert evidence_cmd._check_label({"name": "paths", "ok": True}) == "OK"
    assert evidence_cmd._check_label({"name": "paths", "ok": False}) == "FAIL"
    assert evidence_cmd._check_label({"name": "paths", "status": "WARN"}) == "WARN"
    assert evidence_cmd._check_label({"name": "paths"}) == "?"


def test_evidence_status_renders_ok_bool_check_rows_without_none(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(evidence_brief, "_miseledger_bin", lambda: "/fake/miseledger")

    def fake_run(args, timeout=30.0, **kwargs):
        if "doctor" in args:
            body = {"ok": True, "checks": [{"name": "paths", "detail": "/tmp/db", "ok": True}]}
            return proc.Result(0, json.dumps(body) + "\n", "")
        return proc.Result(0, '{"items": 3}\n', "")

    monkeypatch.setattr(proc, "run", fake_run)

    rc = evidence_cmd.status(target=tmp_path, json_output=False)
    out = capsys.readouterr().out

    assert rc == 0
    assert "- OK: paths - /tmp/db" in out
    assert "None:" not in out


# --- branded help surface -----------------------------------------------------


def test_evidence_group_help_does_not_name_the_engine_binary(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["evidence", "--help"])

    assert exc.value.code == 0
    assert "miseledger" not in capsys.readouterr().out.lower()
