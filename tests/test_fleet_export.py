"""Fleet sync phase 5 tests (issue #1127): deterministic export and the optional Dolt sink."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from brigade import fleet_export, fleet_hub

FAKE_DOLT_LOG_ENV = "BRIGADE_TEST_DOLT_LOG"
FAKE_DOLT_CAPTURE_ENV = "BRIGADE_TEST_DOLT_CAPTURE"
FAKE_DOLT_EXIT_ENV = "BRIGADE_TEST_DOLT_EXIT"

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


def _write_config(home: Path, body: str) -> None:
    config_dir = home / ".brigade"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "fleet.toml").write_text(body, encoding="utf-8")


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
        f'exit "${{{FAKE_DOLT_EXIT_ENV}:-0}}"\n',
        encoding="utf-8",
    )
    os.chmod(script, 0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
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
        first = capsys.readouterr().out
        assert cli.main(argv) == 0
        assert capsys.readouterr().out == first
        rows = [json.loads(line) for line in first.splitlines()]
        assert [(r["node_id"], r["run_id"], r["sequence"]) for r in rows] == [
            (NODE_A, "r1", 1),
            (NODE_A, "r2", 2),
            (NODE_B, "r9", 1),
        ]
        assert all(row["digest"] for row in rows)
        assert all(list(row) == sorted(row) for row in rows)
        assert all("received_at" in row for row in rows)
        assert "exported 3 fleet event(s)" in capsys.readouterr().err

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
        rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert [r["run_id"] for r in rows] == ["new"]
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

    def test_claims_snapshot_exports_without_holder_token(self, tmp_path, capsys):
        from brigade import cli

        db = tmp_path / "hub.db"
        _seed_claim(db)
        assert cli.main(["fleet", "export", "--db", str(db), "--claims"]) == 0
        out = capsys.readouterr().out
        row = json.loads(out.splitlines()[0])
        assert list(row) == sorted(fleet_export.CLAIM_COLUMNS)
        assert row["target"] == "repo-a" and row["owner_node"] == "node-a"
        assert "expires_at" in row and "holder_token" not in out
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

    def test_missing_database_is_an_error_not_a_crash(self, tmp_path, capsys):
        from brigade import cli

        assert cli.main(["fleet", "export", "--db", str(tmp_path / "absent.db")]) == 1
        assert "no fleet hub database" in capsys.readouterr().err


class TestSinkConfig:
    def test_defaults_to_disabled_without_config(self, home):
        config = fleet_export.load_sink_config()
        assert config.enabled is False
        assert config.dolt_binary == "dolt"
        assert config.dolt_dir == "~/.brigade/dolt"
        assert config.db is None

    def test_parses_nested_fleet_sink_section(self, home):
        _write_config(
            home,
            '[fleet]\nhub_url = "http://127.0.0.1:3774"\n\n[fleet.sink]\nenabled = true\n'
            'dolt_binary = "/usr/local/bin/dolt"\ndolt_dir = "~/dolt-fleet"\ndb = "~/hub.db"\n',
        )
        config = fleet_export.load_sink_config()
        assert config.enabled is True
        assert config.dolt_binary == "/usr/local/bin/dolt"
        assert config.dolt_dir == "~/dolt-fleet"
        assert config.db == "~/hub.db"

    def test_non_boolean_enabled_stays_off(self, home):
        _write_config(home, "[fleet.sink]\nenabled = \"yes\"\n")
        assert fleet_export.load_sink_config().enabled is False


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
    def test_pass_feeds_expected_argv_and_upsert_flag(self, home, fake_dolt, tmp_path, monkeypatch, capsys):
        from brigade import cli

        db = tmp_path / "hub.db"
        _seed_events(
            db,
            [_event(run_id="r2", seq=2, digest="d2"), _event(node=NODE_B, run_id="r9")],
            [_event(run_id="r1", seq=1)],
        )
        _seed_claim(db)
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
        assert argv_lines[0] == "init"
        assert argv_lines[1:3] == ["sql", "-q"]
        assert argv_lines[3] == fleet_export._DOLT_SCHEMA_EVENTS
        assert argv_lines[4:8] == ["table", "import", "-u", "fleet_events"]
        assert argv_lines[8].endswith("fleet_events.csv")
        assert argv_lines[9:12] == ["sql", "-q"] and argv_lines[11] == fleet_export._DOLT_SCHEMA_CLAIMS
        assert argv_lines[12:16] == ["table", "import", "-u", "fleet_claims"]
        assert argv_lines[16].endswith("fleet_claims.csv")
        assert len(argv_lines) == 17
        events_csv = capture / "fleet_events.csv"
        claims_csv = capture / "fleet_claims.csv"
        assert events_csv.is_file() and claims_csv.is_file()
        event_rows = events_csv.read_text(encoding="utf-8").splitlines()
        assert event_rows[0] == ",".join(fleet_export.EVENT_COLUMNS)
        assert [row.split(",")[2] for row in event_rows[1:]] == ["1", "2", "1"]
        claim_rows = claims_csv.read_text(encoding="utf-8").splitlines()
        assert claim_rows[1].startswith("repo-a,node-a")

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
        _write_config(home, f'[fleet.sink]\nenabled = true\ndolt_dir = "{dolt_dir}"\n')
        assert cli.main(["fleet", "sink", "--db", str(db)]) == 0
        first = (capture / "fleet_events.csv").read_bytes()
        assert cli.main(["fleet", "sink", "--db", str(db)]) == 0
        second = (capture / "fleet_events.csv").read_bytes()
        assert first == second
        assert "-u" in log.read_text(encoding="utf-8").splitlines()
