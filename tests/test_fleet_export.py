"""Fleet sync phase 5 tests (issue #1127): deterministic export and the optional Dolt sink."""

from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from brigade import fleet_export, fleet_hub
from tests.support import assert_private_mode

FAKE_DOLT_LOG_ENV = "BRIGADE_TEST_DOLT_LOG"
FAKE_DOLT_CAPTURE_ENV = "BRIGADE_TEST_DOLT_CAPTURE"
FAKE_DOLT_EXIT_ENV = "BRIGADE_TEST_DOLT_EXIT"
FAKE_DOLT_STATUS_ENV = "BRIGADE_TEST_DOLT_STATUS"

NODE_A = "11111111-1111-4111-8111-111111111111"
NODE_B = "22222222-2222-4222-8222-222222222222"


def _event(
    node: str = NODE_A,
    run_id: str = "r1",
    seq: int = 1,
    digest: str | None = None,
    state: str = "run.created",
    ts: str = "2026-08-23T12:00:00+00:00",
) -> dict:
    return {
        "node_id": node,
        "run_id": run_id,
        "repo": "repo-a",
        "seat": "worker",
        "harness": "claude",
        "state": state,
        "ts": ts,
        "sequence": seq,
        "digest": digest or f"d{seq}",
    }


def _seed_events(db: Path, *batches: dict | list[dict]) -> None:
    conn = fleet_hub.init_db(db)
    try:
        for batch in batches:
            fleet_hub.store_events(conn, batch)
    finally:
        conn.close()


def _seed_claim(db: Path, target: str = "repo-a", node: str = "node-a", holder: str = "holder-1") -> None:
    conn = fleet_hub.init_db(db)
    try:
        status, payload = fleet_hub.handle_claim(
            conn,
            {"action": "acquire", "target": target, "node_id": node, "holder": holder, "ttl_seconds": 900},
        )
        assert status == 200 and payload["granted"] is True
    finally:
        conn.close()


def _set_claim_expiry(db: Path, target: str, expires_at: float) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.execute("UPDATE claims SET expires_at = ? WHERE target = ?", (expires_at, target))
        conn.commit()
    finally:
        conn.close()


def _set_received_at(db: Path, run_id: str, received_at: str) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.execute("UPDATE events SET received_at = ? WHERE run_id = ?", (received_at, run_id))
        conn.commit()
    finally:
        conn.close()


def _write_config(home: Path, body: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "fleet.toml").write_text(body, encoding="utf-8")


@pytest.fixture()
def home(tmp_path, monkeypatch):
    home = tmp_path / "brigade-home"
    monkeypatch.setenv("BRIGADE_HOME", str(home))
    return home


@pytest.fixture()
def fake_dolt(tmp_path, monkeypatch):
    """A stub dolt binary that logs its argv and captures any CSV it is handed."""
    bin_dir = tmp_path / "dolt-bin"
    bin_dir.mkdir()
    script = bin_dir / "dolt"
    script.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do\n'
        f'  printf \'%s\\n\' "$a" >> "${FAKE_DOLT_LOG_ENV}"\n'
        '  case "$a" in\n'
        f'    *.csv) cp "$a" "${{{FAKE_DOLT_CAPTURE_ENV}}}/${{a##*/}}" ;;\n'
        "  esac\n"
        "done\n"
        f'if [ "$1" = "status" ]; then printf \'%s\\n\' "${{{FAKE_DOLT_STATUS_ENV}}}"; fi\n'
        f'exit "${{{FAKE_DOLT_EXIT_ENV}:-0}}"\n',
        encoding="utf-8",
    )
    os.chmod(script, 0o755)
    monkeypatch.setenv("PATH", str(bin_dir), prepend=os.pathsep)
    monkeypatch.setenv(FAKE_DOLT_STATUS_ENV, " M fleet_events")
    return bin_dir


class TestExport:
    def test_jsonl_is_deterministic_pk_ordered_with_digest(self, tmp_path, capsys):
        from brigade import cli

        db = tmp_path / "hub.db"
        _seed_events(
            db,
            [_event(run_id="r2", seq=2, digest="d2"), _event(node=NODE_B, run_id="r9")],
            [_event(run_id="r1", seq=1)],
        )
        argv = ["fleet", "export", "--db", str(db)]
        assert cli.main(argv) == 0
        first_capture = capsys.readouterr()
        first = first_capture.out
        assert cli.main(argv) == 0
        second_capture = capsys.readouterr()
        assert second_capture.out == first
        rows = [json.loads(line) for line in first.splitlines()]
        assert [(r["node_id"], r["run_id"], r["sequence"]) for r in rows] == [
            (NODE_A, "r1", 1),
            (NODE_A, "r2", 2),
            (NODE_B, "r9", 1),
        ]
        assert all(row["digest"] for row in rows)
        assert all(list(row) == sorted(row) for row in rows)
        assert all("received_at" in row for row in rows)
        assert "exported 3 fleet event(s)" in first_capture.err
        assert second_capture.err == first_capture.err

    def test_csv_has_stable_header_and_digest_column(self, tmp_path, capsys):
        from brigade import cli

        db = tmp_path / "hub.db"
        _seed_events(db, _event(seq=3, digest="abc123"))
        assert cli.main(["fleet", "export", "--db", str(db), "--format", "csv"]) == 0
        lines = capsys.readouterr().out.splitlines()
        assert lines[0] == ",".join(fleet_export.EVENT_COLUMNS)
        row = lines[1].split(",")
        assert row[fleet_export.EVENT_COLUMNS.index("digest")] == "abc123"
        assert row[fleet_export.EVENT_COLUMNS.index("node_id")] == NODE_A
        assert len(lines) == 2

    def test_csv_neutralizes_formula_like_values(self, tmp_path, capsys):
        from brigade import cli

        db = tmp_path / "hub.db"
        event = _event()
        event["repo"] = "=1+1"
        _seed_events(db, event)

        assert cli.main(["fleet", "export", "--db", str(db), "--format", "csv"]) == 0
        rows = list(csv.reader(io.StringIO(capsys.readouterr().out)))
        assert rows[1][fleet_export.EVENT_COLUMNS.index("repo")] == "'=1+1"

    def test_since_filters_and_skips_unparseable_timestamps(self, tmp_path, capsys):
        from brigade import cli

        db = tmp_path / "hub.db"
        _seed_events(
            db,
            [
                _event(run_id="old", ts="2026-08-01T00:00:00+00:00"),
                _event(run_id="new", ts="2026-08-23T12:00:00+00:00"),
                _event(run_id="bad", ts="not-a-timestamp"),
            ],
        )
        assert cli.main(["fleet", "export", "--db", str(db), "--since", "2026-08-10T00:00:00+00:00"]) == 0
        captured = capsys.readouterr()
        rows = [json.loads(line) for line in captured.out.splitlines()]
        assert [r["run_id"] for r in rows] == ["new"]
        assert "skipped 1 event(s) with unparseable ts" in captured.err
        assert cli.main(["fleet", "export", "--db", str(db)]) == 0
        rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert sorted(r["run_id"] for r in rows) == ["bad", "new", "old"]

    def test_since_accepts_z_suffix_naive_and_rejects_garbage(self, tmp_path, capsys):
        from brigade import cli

        db = tmp_path / "hub.db"
        _seed_events(db, _event(run_id="new", ts="2026-08-23T12:00:00+00:00"))
        for since in ("2026-08-10T00:00:00Z", "2026-08-10T00:00:00", "2026-08-10"):
            assert cli.main(["fleet", "export", "--db", str(db), "--since", since]) == 0
            rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
            assert [r["run_id"] for r in rows] == ["new"]
        assert cli.main(["fleet", "export", "--db", str(db), "--since", "day-before-yesterday"]) == 1
        assert "--since must be an ISO-8601 timestamp" in capsys.readouterr().err

    def test_since_and_since_received_filter_different_event_timestamps(self, tmp_path, capsys):
        from brigade import cli

        db = tmp_path / "hub.db"
        _seed_events(
            db,
            [
                _event(run_id="new-event-old-receipt", ts="2026-08-20T00:00:00+00:00"),
                _event(run_id="old-event-new-receipt", ts="2026-08-01T00:00:00+00:00"),
            ],
        )
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "UPDATE events SET received_at = ? WHERE run_id = ?",
                ("2026-08-01T00:00:00+00:00", "new-event-old-receipt"),
            )
            conn.execute(
                "UPDATE events SET received_at = ? WHERE run_id = ?",
                ("2026-08-20T00:00:00+00:00", "old-event-new-receipt"),
            )
            conn.commit()
        finally:
            conn.close()

        assert cli.main(["fleet", "export", "--db", str(db), "--since", "2026-08-10"]) == 0
        event_rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert [row["run_id"] for row in event_rows] == ["new-event-old-receipt"]

        assert cli.main(["fleet", "export", "--db", str(db), "--since-received", "2026-08-10"]) == 0
        receipt_rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert [row["run_id"] for row in receipt_rows] == ["old-event-new-receipt"]

    def test_idempotent_reexport_after_duplicate_ingest(self, tmp_path, capsys):
        from brigade import cli

        db = tmp_path / "hub.db"
        event = _event(seq=7, digest="dup-7")
        _seed_events(db, event)
        assert cli.main(["fleet", "export", "--db", str(db)]) == 0
        first = capsys.readouterr().out
        _seed_events(db, [event, event])
        assert cli.main(["fleet", "export", "--db", str(db)]) == 0
        assert capsys.readouterr().out == first

    def test_claims_snapshot_excludes_expired_by_default_and_marks_included_rows(self, tmp_path, capsys):
        from brigade import cli

        db = tmp_path / "hub.db"
        _seed_claim(db)
        _seed_claim(db, target="repo-expired", holder="holder-expired")
        _set_claim_expiry(db, "repo-expired", 0)

        assert cli.main(["fleet", "export", "--db", str(db), "--claims"]) == 0
        out = capsys.readouterr().out
        rows = [json.loads(line) for line in out.splitlines()]
        assert [row["target"] for row in rows] == ["repo-a"]
        row = rows[0]
        assert list(row) == sorted(fleet_export.CLAIM_COLUMNS)
        assert row["target"] == "repo-a" and row["owner_node"] == "node-a"
        assert row["expired"] is False
        assert "expires_at" in row and "holder_token" not in out

        assert cli.main(["fleet", "export", "--db", str(db), "--claims", "--include-expired"]) == 0
        included = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert [(row["target"], row["expired"]) for row in included] == [
            ("repo-a", False),
            ("repo-expired", True),
        ]

        assert cli.main(["fleet", "export", "--db", str(db), "--claims", "--format", "csv"]) == 0
        lines = capsys.readouterr().out.splitlines()
        assert lines[0] == ",".join(fleet_export.CLAIM_COLUMNS)
        assert "holder_token" not in lines[0]

    def test_out_file_matches_stdout_stream(self, tmp_path, capsys):
        from brigade import cli

        db = tmp_path / "hub.db"
        _seed_events(db, _event())
        assert cli.main(["fleet", "export", "--db", str(db)]) == 0
        streamed = capsys.readouterr().out
        out_file = tmp_path / "export.jsonl"
        assert cli.main(["fleet", "export", "--db", str(db), "--out", str(out_file)]) == 0
        assert out_file.read_text(encoding="utf-8") == streamed

    def test_out_file_uses_default_creation_mode_when_replacing_existing_file(self, tmp_path, capsys):
        from brigade import cli

        db = tmp_path / "hub.db"
        _seed_events(db, _event())
        out_file = tmp_path / "export.jsonl"
        out_file.write_text("old\n", encoding="utf-8")
        os.chmod(out_file, 0o644)
        previous_umask = os.umask(0o022)
        try:
            assert cli.main(["fleet", "export", "--db", str(db), "--out", str(out_file)]) == 0
            assert_private_mode(out_file, 0o644)
        finally:
            os.umask(previous_umask)

    def test_stdout_bytes_ignore_wrapper_encoding_and_newline_translation(self, tmp_path, monkeypatch):
        from brigade import cli

        db = tmp_path / "hub.db"
        event = _event()
        event["repo"] = "snowman-☃"
        _seed_events(db, event)

        raw_stdout = io.BytesIO()
        translated_stdout = io.TextIOWrapper(raw_stdout, encoding="cp1252", errors="strict", newline="\r\n")
        monkeypatch.setattr(sys, "stdout", translated_stdout)
        assert cli.main(["fleet", "export", "--db", str(db), "--format", "csv"]) == 0
        translated_stdout.flush()
        streamed = raw_stdout.getvalue()

        out_file = tmp_path / "export.csv"
        assert cli.main(["fleet", "export", "--db", str(db), "--format", "csv", "--out", str(out_file)]) == 0
        assert streamed == out_file.read_bytes()
        assert b"\r\n" not in streamed
        assert "☃".encode() in streamed

    def test_broken_stdout_pipe_exits_zero_and_replaces_stdout_with_devnull(self, tmp_path, monkeypatch):
        from brigade import cli

        class BrokenPipeStdout:
            closed = False

            def reconfigure(self, **kwargs):
                return None

            def write(self, value):
                raise BrokenPipeError(32, "Broken pipe")

            def close(self):
                self.closed = True

        db = tmp_path / "hub.db"
        _seed_events(db, _event())
        broken_stdout = BrokenPipeStdout()
        monkeypatch.setattr(sys, "stdout", broken_stdout)

        assert cli.main(["fleet", "export", "--db", str(db)]) == 0
        assert broken_stdout.closed is True
        assert sys.stdout.name == os.devnull
        sys.stdout.close()

    def test_missing_database_is_an_error_not_a_crash(self, tmp_path, capsys):
        from brigade import cli

        assert cli.main(["fleet", "export", "--db", str(tmp_path / "absent.db")]) == 1
        assert "no fleet hub database" in capsys.readouterr().err

    def test_non_hub_database_query_error_is_operator_facing(self, tmp_path, capsys):
        from brigade import cli

        db = tmp_path / "not-a-hub.db"
        sqlite3.connect(db).close()

        assert cli.main(["fleet", "export", "--db", str(db), "--claims"]) == 1
        assert "could not query fleet hub database" in capsys.readouterr().err

    def test_readonly_wal_initialization_error_is_operator_facing(self, tmp_path, monkeypatch, capsys):
        from brigade import cli

        db = tmp_path / "hub.db"
        _seed_events(db, _event())

        def readonly_cantinit(*args, **kwargs):
            raise sqlite3.OperationalError("attempt to write a readonly database")

        monkeypatch.setattr(fleet_export.sqlite3, "connect", readonly_cantinit)
        assert cli.main(["fleet", "export", "--db", str(db)]) == 1
        error = capsys.readouterr().err
        assert "could not open fleet hub database" in error
        assert "WAL" in error and "-shm" in error

    def test_missing_output_directory_is_operator_facing(self, tmp_path, capsys):
        from brigade import cli

        db = tmp_path / "hub.db"
        _seed_events(db, _event())

        assert cli.main(["fleet", "export", "--db", str(db), "--out", str(tmp_path / "missing" / "out.jsonl")]) == 1
        assert "could not write fleet export" in capsys.readouterr().err

    def test_failed_export_leaves_preexisting_output_untouched(self, tmp_path, capsys):
        from brigade import cli

        db = tmp_path / "hub.db"
        _seed_events(db, _event())
        conn = sqlite3.connect(db)
        try:
            conn.execute("DROP TABLE claims")
            conn.commit()
        finally:
            conn.close()
        out_file = tmp_path / "claims.jsonl"
        out_file.write_text("keep this content\n", encoding="utf-8")

        assert cli.main(["fleet", "export", "--db", str(db), "--claims", "--out", str(out_file)]) == 1
        assert "could not query fleet hub database" in capsys.readouterr().err
        assert out_file.read_text(encoding="utf-8") == "keep this content\n"
        assert list(tmp_path.glob(f".{out_file.name}.*.tmp")) == []


class TestSinkConfig:
    def test_defaults_to_disabled_without_config(self, home):
        config = fleet_export.load_sink_config()
        assert config.enabled is False
        assert config.dolt_binary == "dolt"
        assert config.dolt_dir == "~/.brigade/dolt"
        assert config.db is None
        assert config.timeout_seconds == 120
        assert config.events_incremental is True

    def test_parses_nested_fleet_sink_section(self, home):
        _write_config(
            home,
            '[fleet]\nhub_url = "http://127.0.0.1:3774"\n\n[fleet.sink]\nenabled = true\n'
            'dolt_binary = "/usr/local/bin/dolt"\ndolt_dir = "~/dolt-fleet"\ndb = "~/hub.db"\n'
            "timeout_seconds = 37.5\nevents_incremental = false\n",
        )
        config = fleet_export.load_sink_config()
        assert config.enabled is True
        assert config.dolt_binary == "/usr/local/bin/dolt"
        assert config.dolt_dir == "~/dolt-fleet"
        assert config.db == "~/hub.db"
        assert config.timeout_seconds == 37.5
        assert config.events_incremental is False

    def test_non_boolean_enabled_stays_off(self, home):
        _write_config(home, '[fleet.sink]\nenabled = "yes"\n')
        assert fleet_export.load_sink_config().enabled is False

    def test_timeout_is_passed_to_dolt_subprocess(self, tmp_path, monkeypatch):
        observed = {}

        def fake_run(command, **kwargs):
            observed.update(kwargs)
            return fleet_export.subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        monkeypatch.setattr(fleet_export.subprocess, "run", fake_run)
        ok, message, _ = fleet_export._run_dolt(
            ["status", "--porcelain"],
            dolt_binary="dolt",
            cwd=tmp_path,
            timeout_seconds=37.5,
        )

        assert ok is True and message == ""
        assert observed["timeout"] == 37.5


class TestSink:
    def test_disabled_by_default_never_runs_dolt(self, home, fake_dolt, tmp_path, monkeypatch, capsys):
        from brigade import cli

        log = tmp_path / "dolt.log"
        monkeypatch.setenv(FAKE_DOLT_LOG_ENV, str(log))
        assert cli.main(["fleet", "sink", "--db", str(tmp_path / "hub.db")]) == 0
        assert "disabled" in capsys.readouterr().out
        assert not log.exists()

    def test_enabled_without_dolt_binary_is_documented_noop(self, home, tmp_path, monkeypatch, capsys):
        from brigade import cli

        empty_path = tmp_path / "no-bin"
        empty_path.mkdir()
        monkeypatch.setenv("PATH", str(empty_path))
        _write_config(home, "[fleet.sink]\nenabled = true\n")
        assert cli.main(["fleet", "sink", "--db", str(tmp_path / "hub.db")]) == 0
        assert "documented no-op" in capsys.readouterr().out

    @pytest.mark.skipif(os.name != "posix", reason="fake dolt shell stub requires POSIX")
    def test_pass_initializes_identity_upserts_events_and_replaces_claims(
        self, home, fake_dolt, tmp_path, monkeypatch, capsys
    ):
        from brigade import cli

        db = tmp_path / "hub.db"
        _seed_events(
            db,
            [_event(run_id="r2", seq=2, digest="d2"), _event(node=NODE_B, run_id="r9")],
            [_event(run_id="r1", seq=1)],
        )
        _seed_claim(db)
        _seed_claim(db, target="repo-expired", holder="holder-expired")
        _set_claim_expiry(db, "repo-expired", 0)
        dolt_dir = tmp_path / "dolt-home"
        log = tmp_path / "dolt.log"
        capture = tmp_path / "capture"
        capture.mkdir()
        monkeypatch.setenv(FAKE_DOLT_LOG_ENV, str(log))
        monkeypatch.setenv(FAKE_DOLT_CAPTURE_ENV, str(capture))
        monkeypatch.delenv(FAKE_DOLT_EXIT_ENV, raising=False)
        _write_config(home, f'[fleet.sink]\nenabled = true\ndolt_dir = "{dolt_dir}"\n')
        assert cli.main(["fleet", "sink", "--db", str(db)]) == 0
        assert "imported 3 event(s) and 1 claim(s)" in capsys.readouterr().out
        argv_lines = log.read_text(encoding="utf-8").splitlines()
        assert argv_lines[:5] == ["init", "--name", "brigade", "--email", "brigade@localhost"]
        assert argv_lines[5:7] == ["sql", "-q"]
        assert argv_lines[7] == fleet_export._DOLT_SCHEMA_EVENTS
        assert argv_lines[8:12] == ["table", "import", "-u", "fleet_events"]
        assert argv_lines[12].endswith("fleet_events.csv")
        assert argv_lines[13:15] == ["sql", "-q"] and argv_lines[15] == fleet_export._DOLT_SCHEMA_CLAIMS
        assert argv_lines[16:20] == ["table", "import", "-r", "fleet_claims"]
        assert argv_lines[20].endswith("fleet_claims.csv")
        assert argv_lines[21:23] == ["status", "--porcelain"]
        assert argv_lines[23:25] == ["add", "-A"]
        assert argv_lines[25:27] == ["commit", "-m"]
        assert argv_lines[27].endswith("Z: fleet events/claims")
        assert len(argv_lines) == 28
        events_csv = capture / "fleet_events.csv"
        claims_csv = capture / "fleet_claims.csv"
        assert events_csv.is_file() and claims_csv.is_file()
        event_rows = events_csv.read_text(encoding="utf-8").splitlines()
        assert event_rows[0] == ",".join(fleet_export.EVENT_COLUMNS)
        assert [row.split(",")[2] for row in event_rows[1:]] == ["1", "2", "1"]
        claim_rows = claims_csv.read_text(encoding="utf-8").splitlines()
        assert claim_rows[1].startswith("repo-a,node-a")
        parsed_claims = list(csv.DictReader(io.StringIO(claims_csv.read_text(encoding="utf-8"))))
        assert parsed_claims[0]["expired"] == "0"
        assert len(claim_rows) == 2

    @pytest.mark.skipif(os.name != "posix", reason="fake dolt shell stub requires POSIX")
    def test_sink_csv_uses_dolt_null_marker_while_jsonl_keeps_null(
        self, home, fake_dolt, tmp_path, monkeypatch, capsys
    ):
        from brigade import cli

        db = tmp_path / "hub.db"
        event = _event()
        event["repo"] = None
        _seed_events(db, event)
        dolt_dir = tmp_path / "dolt-home"
        capture = tmp_path / "capture"
        capture.mkdir()
        monkeypatch.setenv(FAKE_DOLT_LOG_ENV, str(tmp_path / "dolt.log"))
        monkeypatch.setenv(FAKE_DOLT_CAPTURE_ENV, str(capture))
        _write_config(home, f'[fleet.sink]\nenabled = true\ndolt_dir = "{dolt_dir}"\n')

        assert cli.main(["fleet", "sink", "--db", str(db)]) == 0
        sink_rows = list(csv.DictReader(io.StringIO((capture / "fleet_events.csv").read_text(encoding="utf-8"))))
        assert sink_rows[0]["repo"] == r"\N"

        capsys.readouterr()
        assert cli.main(["fleet", "export", "--db", str(db)]) == 0
        assert json.loads(capsys.readouterr().out)["repo"] is None

    @pytest.mark.skipif(os.name != "posix", reason="fake dolt shell stub requires POSIX")
    def test_sink_keeps_formula_like_values_raw_while_human_csv_guards_them(
        self, home, fake_dolt, tmp_path, monkeypatch, capsys
    ):
        from brigade import cli

        db = tmp_path / "hub.db"
        event = _event()
        event["seat"] = "-worker"
        _seed_events(db, event)
        dolt_dir = tmp_path / "dolt-home"
        log = tmp_path / "dolt.log"
        capture = tmp_path / "capture"
        capture.mkdir()
        monkeypatch.setenv(FAKE_DOLT_LOG_ENV, str(log))
        monkeypatch.setenv(FAKE_DOLT_CAPTURE_ENV, str(capture))
        _write_config(home, f'[fleet.sink]\nenabled = true\ndolt_dir = "{dolt_dir}"\n')

        assert cli.main(["fleet", "sink", "--db", str(db)]) == 0
        sink_rows = list(csv.reader(io.StringIO((capture / "fleet_events.csv").read_text(encoding="utf-8"))))
        assert sink_rows[1][fleet_export.EVENT_COLUMNS.index("seat")] == "-worker"

        capsys.readouterr()
        assert cli.main(["fleet", "export", "--db", str(db), "--format", "csv"]) == 0
        export_rows = list(csv.reader(io.StringIO(capsys.readouterr().out)))
        assert export_rows[1][fleet_export.EVENT_COLUMNS.index("seat")] == "'-worker"

    @pytest.mark.skipif(os.name != "posix", reason="fake dolt shell stub requires POSIX")
    def test_sink_skips_commit_but_advances_watermark_when_working_set_is_unchanged(
        self, home, fake_dolt, tmp_path, monkeypatch
    ):
        from brigade import cli

        db = tmp_path / "hub.db"
        _seed_events(db, _event())
        _set_received_at(db, "r1", "2026-08-23T12:00:00Z")
        dolt_dir = tmp_path / "dolt-home"
        log = tmp_path / "dolt.log"
        capture = tmp_path / "capture"
        capture.mkdir()
        monkeypatch.setenv(FAKE_DOLT_LOG_ENV, str(log))
        monkeypatch.setenv(FAKE_DOLT_CAPTURE_ENV, str(capture))
        monkeypatch.setenv(FAKE_DOLT_STATUS_ENV, "")
        _write_config(home, f'[fleet.sink]\nenabled = true\ndolt_dir = "{dolt_dir}"\n')

        assert cli.main(["fleet", "sink", "--db", str(db)]) == 0
        argv_lines = log.read_text(encoding="utf-8").splitlines()
        assert argv_lines[-2:] == ["status", "--porcelain"]
        assert "add" not in argv_lines
        assert "commit" not in argv_lines
        watermark = dolt_dir / fleet_export.EVENTS_WATERMARK_FILE
        assert watermark.read_text(encoding="utf-8") == "2026-08-23T12:00:00Z\n"

    @pytest.mark.skipif(os.name != "posix", reason="fake dolt shell stub requires POSIX")
    def test_sink_reports_unparseable_incremental_timestamps_on_stderr(
        self, home, fake_dolt, tmp_path, monkeypatch, capsys
    ):
        from brigade import cli

        db = tmp_path / "hub.db"
        _seed_events(db, _event(run_id="good"), _event(run_id="bad", seq=2, digest="d2"))
        _set_received_at(db, "good", "2026-08-23T12:00:00Z")
        _set_received_at(db, "bad", "not-a-timestamp")
        dolt_dir = tmp_path / "dolt-home"
        dolt_dir.mkdir()
        (dolt_dir / fleet_export.EVENTS_WATERMARK_FILE).write_text("2026-08-23T11:00:00Z\n", encoding="utf-8")
        capture = tmp_path / "capture"
        capture.mkdir()
        monkeypatch.setenv(FAKE_DOLT_LOG_ENV, str(tmp_path / "dolt.log"))
        monkeypatch.setenv(FAKE_DOLT_CAPTURE_ENV, str(capture))
        _write_config(home, f'[fleet.sink]\nenabled = true\ndolt_dir = "{dolt_dir}"\n')

        assert cli.main(["fleet", "sink", "--db", str(db)]) == 0
        captured = capsys.readouterr()
        assert "skipped 1 event(s) with unparseable received_at" in captured.err

    def test_dolt_failure_reports_error_and_exits_nonzero(self, home, fake_dolt, tmp_path, monkeypatch, capsys):
        from brigade import cli

        db = tmp_path / "hub.db"
        _seed_events(db, _event())
        log = tmp_path / "dolt.log"
        capture = tmp_path / "capture"
        capture.mkdir()
        monkeypatch.setenv(FAKE_DOLT_LOG_ENV, str(log))
        monkeypatch.setenv(FAKE_DOLT_CAPTURE_ENV, str(capture))
        monkeypatch.setenv(FAKE_DOLT_EXIT_ENV, "7")
        _write_config(home, f'[fleet.sink]\nenabled = true\ndolt_dir = "{tmp_path / "dolt-home"}"\n')
        assert cli.main(["fleet", "sink", "--db", str(db)]) == 1
        assert "failed" in capsys.readouterr().err

    def test_raw_query_error_is_operator_facing(self, home, fake_dolt, tmp_path, monkeypatch, capsys):
        from brigade import cli

        db = tmp_path / "hub.db"
        _seed_events(db, _event())
        _write_config(home, f'[fleet.sink]\nenabled = true\ndolt_dir = "{tmp_path / "dolt-home"}"\n')

        def readonly_cantinit(*args, **kwargs):
            raise sqlite3.OperationalError("attempt to write a readonly database")

        monkeypatch.setattr(fleet_export, "export_into", readonly_cantinit)
        assert cli.main(["fleet", "sink", "--db", str(db)]) == 1
        assert "could not export fleet sink data" in capsys.readouterr().err

    def test_repeat_passes_are_byte_identical_upserts(self, home, fake_dolt, tmp_path, monkeypatch):
        from brigade import cli

        db = tmp_path / "hub.db"
        _seed_events(db, _event(), _event(seq=2, digest="d2"))
        dolt_dir = tmp_path / "dolt-home"
        log = tmp_path / "dolt.log"
        capture = tmp_path / "capture"
        capture.mkdir()
        monkeypatch.setenv(FAKE_DOLT_LOG_ENV, str(log))
        monkeypatch.setenv(FAKE_DOLT_CAPTURE_ENV, str(capture))
        monkeypatch.delenv(FAKE_DOLT_EXIT_ENV, raising=False)
        _write_config(
            home,
            f'[fleet.sink]\nenabled = true\ndolt_dir = "{dolt_dir}"\nevents_incremental = false\n',
        )
        assert cli.main(["fleet", "sink", "--db", str(db)]) == 0
        first = (capture / "fleet_events.csv").read_bytes()
        assert cli.main(["fleet", "sink", "--db", str(db)]) == 0
        second = (capture / "fleet_events.csv").read_bytes()
        assert first == second
        assert "-u" in log.read_text(encoding="utf-8").splitlines()

    @pytest.mark.skipif(os.name != "posix", reason="fake dolt shell stub requires POSIX")
    def test_incremental_event_watermark_round_trip_and_invalid_fallback(self, home, fake_dolt, tmp_path, monkeypatch):
        from brigade import cli

        db = tmp_path / "hub.db"
        _seed_events(db, _event(run_id="first"), _event(run_id="second", seq=2, digest="d2"))
        _set_received_at(db, "first", "2026-08-23T12:00:00Z")
        _set_received_at(db, "second", "2026-08-23T12:01:00Z")
        dolt_dir = tmp_path / "dolt-home"
        capture = tmp_path / "capture"
        capture.mkdir()
        monkeypatch.setenv(FAKE_DOLT_LOG_ENV, str(tmp_path / "dolt.log"))
        monkeypatch.setenv(FAKE_DOLT_CAPTURE_ENV, str(capture))
        _write_config(home, f'[fleet.sink]\nenabled = true\ndolt_dir = "{dolt_dir}"\n')

        assert cli.main(["fleet", "sink", "--db", str(db)]) == 0
        watermark = dolt_dir / fleet_export.EVENTS_WATERMARK_FILE
        assert watermark.read_text(encoding="utf-8") == "2026-08-23T12:01:00Z\n"

        _seed_events(db, _event(run_id="third", seq=3, digest="d3"))
        _set_received_at(db, "third", "2026-08-23T12:02:00Z")
        assert cli.main(["fleet", "sink", "--db", str(db)]) == 0
        rows = list(csv.DictReader(io.StringIO((capture / "fleet_events.csv").read_text(encoding="utf-8"))))
        assert [row["run_id"] for row in rows] == ["third"]
        assert watermark.read_text(encoding="utf-8") == "2026-08-23T12:02:00Z\n"

        watermark.write_text("not-a-timestamp\n", encoding="utf-8")
        assert cli.main(["fleet", "sink", "--db", str(db)]) == 0
        rows = list(csv.DictReader(io.StringIO((capture / "fleet_events.csv").read_text(encoding="utf-8"))))
        assert [row["run_id"] for row in rows] == ["first", "second", "third"]


def test_export_and_sink_parser_callbacks_are_lazy_wrappers():
    from brigade import cli
    from brigade.cli import fleet as fleet_cli

    parser = cli._build_parser()
    assert parser.parse_args(["fleet", "export"]).func is fleet_cli._dispatch_export
    assert parser.parse_args(["fleet", "sink"]).func is fleet_cli._dispatch_sink
