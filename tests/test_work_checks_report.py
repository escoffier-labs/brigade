"""Report counts use real writer records, including bounded bulk history."""

import json
import subprocess
import socket
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from brigade import agent_identity, cli, observed_checks as oc, observed_report as report
from brigade.work_checks_cmd import render_first_pass
from tests._home import set_home


@pytest.fixture
def store(tmp_path, monkeypatch):
    set_home(monkeypatch, tmp_path / "home")
    target = tmp_path / "repo"
    target.mkdir()

    def record(
        session="one",
        command="pytest -q",
        status="ok",
        harness="claude-code",
        model="claude-test",
        ts="2026-01-02T00:00:00+00:00",
    ):
        check = oc.build_check(
            target=target,
            command=command,
            status=status,
            identity=agent_identity.AgentIdentity(harness, "hook-event", model, "hook-event"),
            session_fingerprint=session,
            fingerprint_tree=False,
        )
        return replace(check, ts=ts)

    return target, record


def test_first_attempt_retries_and_unknowns(store):
    target, record = store
    checks = [
        record(status="failed"),
        record(status="ok"),
        record(session="two"),
        record(session="three", status="unknown"),
        record(command="ruff check ."),
    ]
    for check in checks:
        assert oc.append_check(target, check)
    rows, notes = report.first_pass_rows(target)
    assert [(r.samples, r.passed, r.failed, r.unknown, r.rate()) for r in rows] == [(4, 2, 1, 1, None)]
    assert notes.retries == 1 and notes.observed_read == 5
    assert all(c.tree_fingerprint is None for c in checks)


def test_unavailable_keys_never_group(store):
    target, record = store
    for field in ("session_fingerprint", "command_digest"):
        for _ in range(2):
            assert oc.append_check(target, replace(record(), **{field: oc.DIGEST_UNAVAILABLE}))
    rows, notes = report.first_pass_rows(target)
    assert rows == [] and notes.ungrouped == 4 and notes.retries == 0


def test_bulk_counts_threshold_unknown_model_and_codex(store):
    target, record = store
    base = record()
    assert oc.append_check(target, base)
    path = oc.digest_key_path(target).with_name(oc.CHECKS_FILE)
    # Bulk fixture uses precisely append_jsonl's encoding, with real appends at both edges.
    checks = []
    for i in range(3000):
        harness, model = [("claude-code", "claude-test"), ("codex", "gpt-test"), ("cursor", "unknown")][i % 3]
        checks.append(
            replace(
                base,
                harness=harness,
                model=model,
                session_fingerprint=f"{i + 1:016x}",
                status="failed" if i % 2 else "ok",
            )
        )
    with path.open("ab") as stream:
        for check in checks:
            stream.write((json.dumps(check.to_dict(), ensure_ascii=True, allow_nan=False) + "\n").encode())
    assert oc.append_check(target, record(session="last", status="unknown"))
    rows, notes = report.first_pass_rows(target)
    assert notes.observed_read == 3002 and notes.skipped_lines == 0
    by_harness = {r.harness: r for r in rows}
    assert by_harness["claude-code"].rate() == 501 / 1001
    assert by_harness["claude-code"].unknown == 1
    assert by_harness["codex"].unknown == 1000
    assert by_harness["codex"].passed == by_harness["codex"].failed == 0
    assert by_harness["codex"].rate() is None
    assert by_harness["cursor"].model == "unknown"
    text = render_first_pass(rows, notes)
    assert "no result reported" in text and "50.0%" in text
    assert "unknown" in text and f"Minimum reported results for a rate: {report.MIN_RESULTS}" in text


def test_group_by_harness_and_unknown_model_row(store):
    target, record = store
    for i, model in enumerate(("claude-test", "unknown")):
        assert oc.append_check(target, record(session=str(i), model=model))
    rows, _ = report.first_pass_rows(target)
    assert [r.model for r in rows] == ["claude-test", "unknown"]
    rows, _ = report.first_pass_rows(target, group_by="harness")
    assert len(rows) == 1 and rows[0].samples == 2 and rows[0].model is None


def test_since_does_not_promote_retry(store):
    target, record = store
    assert oc.append_check(target, record(ts="2026-01-01T00:00:00+00:00"))
    assert oc.append_check(target, record(ts="2026-01-03T00:00:00+00:00"))
    rows, notes = report.first_pass_rows(target, since=datetime(2026, 1, 2, tzinfo=timezone.utc))
    assert rows == [] and notes.retries == 1


def test_threshold_boundary_and_unknown_denominator():
    row = report.FirstPassRow("claude-code", "unknown", 12, 3, 2, 7)
    assert row.rate() == 0.6
    assert report.FirstPassRow("claude-code", "unknown", 10, 4, 0, 6).rate() is None


def test_cli_json_relative_target_and_no_side_effects(store, monkeypatch, capsys):
    target, record = store
    assert oc.append_check(target, record())
    path = oc.digest_key_path(target).parent
    path.joinpath("checks.jsonl.lock").unlink()
    with path.joinpath("checks.jsonl").open("ab") as stream:
        stream.write(b"{broken\n")
    before = {p: p.read_bytes() for p in path.iterdir()}

    def forbidden(*args, **kwargs):
        raise AssertionError("report must not start a process or write")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(oc, "append_check", forbidden)
    monkeypatch.chdir(target.parent)
    assert cli.main(["work", "checks", "--target", target.name, "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert set(result) == {
        "schema",
        "label",
        "group_by",
        "min_results",
        "rows",
        "observed_read",
        "skipped_lines",
        "ungrouped",
        "retries",
    }
    assert result["schema"] == "brigade.work_checks.v1" and result["skipped_lines"] == 1
    assert result["rows"][0]["rate"] is None
    assert {p: p.read_bytes() for p in path.iterdir()} == before
    assert cli.main(["work", "checks", "--target", str(target)]) == 0
    output = capsys.readouterr().out
    assert "Skipped lines: 1" in output and "\x1b" not in output


def test_empty_cli_writes_nothing(store, capsys):
    target, _ = store
    assert cli.main(["work", "checks", "--target", str(target)]) == 0
    text = capsys.readouterr().out
    assert len(text.splitlines()) == 1 and "mode set observe" in text
    assert not oc.digest_key_path(target).parent.exists()


@pytest.mark.parametrize("days", ["-1", "oops", "999999999999999999"])
def test_invalid_since(store, days):
    target, _ = store
    with pytest.raises(SystemExit) as exc:
        cli.main(["work", "checks", "--target", str(target), "--since", days])
    assert exc.value.code == 2


def test_observe_brief_names_report():
    from brigade.brief_sections import render_brief_session_close_lines

    assert any("brigade work checks" in line for line in render_brief_session_close_lines(mode="observe"))
