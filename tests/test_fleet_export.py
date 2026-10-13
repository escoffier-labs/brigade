"""Fleet sync phase 5 tests (issue #1127): deterministic export and the optional Dolt sink."""

from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from brigade import fleet_export, fleet_hub
from tests._home import home_env, set_home
from tests.support import assert_private_mode

FAKE_DOLT_LOG_ENV = "BRIGADE_TEST_DOLT_LOG"
FAKE_DOLT_CAPTURE_ENV = "BRIGADE_TEST_DOLT_CAPTURE"
FAKE_DOLT_EXIT_ENV = "BRIGADE_TEST_DOLT_EXIT"
FAKE_DOLT_STATUS_ENV = "BRIGADE_TEST_DOLT_STATUS"
REAL_DOLT_BIN_ENV = "BRIGADE_TEST_DOLT_BIN"

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


def _claim_request(action: str, target: str, holder: str) -> dict:
    return {"action": action, "target": target, "node_id": "node-a", "holder": holder, "ttl_seconds": 900}


def _seed_generations(db: Path) -> None:
    """repo-a: acquire, release, reacquire (fence 3); repo-expired: one expired acquire (fence 1)."""
    conn = fleet_hub.init_db(db)
    try:
        for action, target, holder in (
            ("acquire", "repo-a", "holder-1"),
            ("release", "repo-a", "holder-1"),
            ("acquire", "repo-a", "holder-2"),
            ("acquire", "repo-expired", "holder-expired"),
        ):
            assert fleet_hub.handle_claim(conn, _claim_request(action, target, holder))[0] == 200
        conn.execute("UPDATE claims SET expires_at = 0 WHERE target = 'repo-expired'")
        conn.commit()
    finally:
        conn.close()


def _exec(db: Path, *statements: str) -> None:
    conn = sqlite3.connect(db)
    try:
        for statement in statements:
            conn.execute(statement)
        conn.commit()
    finally:
        conn.close()


def _db_bytes(db: Path) -> list[bytes]:
    # A read-only WAL open may leave an empty sidecar behind; only content counts.
    return [path.read_bytes() for path in (db, Path(f"{db}-wal")) if path.exists() and path.stat().st_size]


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


FENCE_DAMAGE = {
    "visible-fence-missing": (["DELETE FROM claim_fences WHERE target = 'repo-a'"], "claim generation fence"),
    "hidden-expired-fence-missing": (
        ["DELETE FROM claim_fences WHERE target = 'repo-expired'"],
        "claim generation fence",
    ),
    "hidden-expired-bad-floor": (
        ["PRAGMA ignore_check_constraints=ON", "UPDATE claim_fences SET generation = 0 WHERE target = 'repo-expired'"],
        "claim generation fence",
    ),
    "bad-floor-without-claims": (
        ["DELETE FROM claims", "PRAGMA ignore_check_constraints=ON", "UPDATE claim_fences SET generation = -1"],
        "claim generation fence",
    ),
    "lookalike-schema-without-claims": (
        [
            "DELETE FROM claims",
            "DROP TABLE claim_fences",
            "CREATE TABLE claim_fences (target TEXT PRIMARY KEY, generation INTEGER)",
        ],
        "unsupported definition",
    ),
    "fence-table-missing": (["DROP TABLE claim_fences"], "claim generation fence"),
    "legacy-schema-without-fences": (["DROP TABLE claim_fences", "PRAGMA user_version=25"], "claim generation fence"),
    "newer-schema": ([f"PRAGMA user_version={fleet_hub.SCHEMA_VERSION + 1}"], "schema version"),
}


class TestClaimGenerationExport:
    @pytest.mark.parametrize("fmt", ["jsonl", "csv"])
    def test_rows_carry_current_generation_and_legacy_protocol_without_capabilities(self, tmp_path, capsys, fmt):
        from brigade import cli

        db = tmp_path / "hub.db"
        _seed_generations(db)

        def rows(*extra: str) -> list[dict]:
            assert cli.main(["fleet", "export", "--db", str(db), "--claims", "--format", fmt, *extra]) == 0
            out = capsys.readouterr().out
            assert "holder-" not in out
            if fmt == "csv":
                assert out.splitlines()[0] == ",".join(fleet_export.CLAIM_COLUMNS)
                return list(csv.DictReader(io.StringIO(out)))
            return [json.loads(line) for line in out.splitlines()]

        live = rows()
        assert [(r["target"], str(r["generation"]), r["protocol"]) for r in live] == [("repo-a", "3", "legacy")]
        included = rows("--include-expired")
        assert [(r["target"], str(r["generation"]), r["protocol"]) for r in included] == [
            ("repo-a", "3", "legacy"),
            ("repo-expired", "1", "legacy"),
        ]
        assert fleet_export.CLAIM_COLUMNS[-3:] == ("expired", "generation", "protocol")

    @pytest.mark.parametrize("damage", FENCE_DAMAGE)
    def test_damaged_fence_state_refuses_claims_export_without_touching_output_or_database(
        self, tmp_path, capsys, damage
    ):
        from brigade import cli

        db = tmp_path / "hub.db"
        _seed_generations(db)
        statements, message = FENCE_DAMAGE[damage]
        _exec(db, *statements)
        out_file = tmp_path / "claims.jsonl"
        out_file.write_text("keep this content\n", encoding="utf-8")
        before = _db_bytes(db)

        assert cli.main(["fleet", "export", "--db", str(db), "--claims", "--out", str(out_file)]) == 1
        captured = capsys.readouterr()
        assert captured.err.startswith("error: ") and message in captured.err
        assert captured.out == "" and "Traceback" not in captured.err
        assert out_file.read_text(encoding="utf-8") == "keep this content\n"
        assert list(tmp_path.glob(f".{out_file.name}.*.tmp")) == []
        assert _db_bytes(db) == before

    def test_damaged_fence_state_writes_no_csv_header_to_stdout(self, tmp_path, capsys):
        from brigade import cli

        db = tmp_path / "hub.db"
        _seed_generations(db)
        _exec(db, "DELETE FROM claim_fences WHERE target = 'repo-a'")

        assert cli.main(["fleet", "export", "--db", str(db), "--claims", "--format", "csv"]) == 1
        assert capsys.readouterr().out == ""

    def test_legacy_schema_refuses_claims_but_events_still_export(self, tmp_path, capsys):
        from brigade import cli

        db = tmp_path / "hub.db"
        _seed_events(db, _event())
        _seed_generations(db)
        _exec(db, "DROP TABLE claim_fences", "PRAGMA user_version=25")

        assert cli.main(["fleet", "export", "--db", str(db), "--claims"]) == 1
        assert "claim generation fence" in capsys.readouterr().err
        assert cli.main(["fleet", "export", "--db", str(db)]) == 0
        assert [json.loads(line)["run_id"] for line in capsys.readouterr().out.splitlines()] == ["r1"]
        conn = sqlite3.connect(db)
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 25
            assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'claim_fences'").fetchone()
        finally:
            conn.close()


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


class TestDoltWorkingSetStatus:
    """``dolt status --porcelain`` is preferred; only Dolt's explicit unsupported-option error uses dolt_status."""

    PORCELAIN = ["status", "--porcelain"]
    FALLBACK = ["sql", "-r", "json", "-q", "SELECT table_name FROM dolt_status"]
    UNSUPPORTED = "dolt status --porcelain failed: error: unknown option `porcelain'"

    def _status(self, tmp_path, monkeypatch, responses):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return responses[len(calls) - 1]

        monkeypatch.setattr(fleet_export, "_run_dolt", fake_run)
        result = fleet_export._dolt_working_set_status(dolt_binary="dolt", cwd=tmp_path, timeout_seconds=5)
        return result, calls

    def test_supported_porcelain_is_used_without_fallback(self, tmp_path, monkeypatch):
        result, calls = self._status(tmp_path, monkeypatch, [(True, "", " M fleet_claims\n")])
        assert result == (True, "", " M fleet_claims\n")
        assert calls == [self.PORCELAIN]

    @pytest.mark.parametrize(
        ("stdout", "expected"),
        [
            ("{}\n", ""),
            ('{"rows": []}', ""),
            ('{"rows": [{"table_name":"fleet_claims"},{"table_name":"fleet_events"}]}', "fleet_claims\nfleet_events"),
        ],
    )
    def test_unsupported_porcelain_falls_back_to_dolt_status_json(self, tmp_path, monkeypatch, stdout, expected):
        result, calls = self._status(tmp_path, monkeypatch, [(False, self.UNSUPPORTED, ""), (True, "", stdout)])
        assert result == (True, "", expected)
        assert calls == [self.PORCELAIN, self.FALLBACK]

    @pytest.mark.parametrize(
        "message",
        [
            "dolt status --porcelain failed: fatal: Unable to locate a dolt repository",
            "dolt timed out after 5s: dolt status --porcelain",
            "could not run dolt: [Errno 13] Permission denied: 'dolt'",
            "dolt status --porcelain failed: error: unknown option `ignored'",
        ],
    )
    def test_other_porcelain_failures_fail_closed_without_fallback_or_retry(self, tmp_path, monkeypatch, message):
        result, calls = self._status(tmp_path, monkeypatch, [(False, message, "")])
        assert result == (False, message, "")
        assert calls == [self.PORCELAIN]

    @pytest.mark.parametrize(
        "message",
        [
            "dolt sql -r failed: error on line 1 for query SELECT table_name FROM dolt_status: table not found",
            "dolt timed out after 5s: dolt sql -r json -q SELECT table_name FROM dolt_status",
        ],
    )
    def test_fallback_sql_error_or_timeout_fails_closed(self, tmp_path, monkeypatch, message):
        result, calls = self._status(tmp_path, monkeypatch, [(False, self.UNSUPPORTED, ""), (False, message, "")])
        assert result == (False, message, "")
        assert calls == [self.PORCELAIN, self.FALLBACK]

    @pytest.mark.parametrize(
        "stdout",
        [
            "",
            "not json",
            "[]",
            '"rows"',
            '{"rows": "fleet_claims"}',
            '{"rows": [{}]}',
            '{"rows": [{"table_name": 7}]}',
            '{"rows": [{"table_name": ""}]}',
            '{"rows": ["fleet_claims"]}',
            '{"rows": [], "unexpected": 1}',
            '{"unexpected": []}',
        ],
    )
    def test_fallback_malformed_or_unexpected_response_fails_closed(self, tmp_path, monkeypatch, stdout):
        (ok, message, status), calls = self._status(
            tmp_path, monkeypatch, [(False, self.UNSUPPORTED, ""), (True, "", stdout)]
        )
        assert (ok, status) == (False, "")
        assert "dolt_status" in message
        assert calls == [self.PORCELAIN, self.FALLBACK]


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
    def test_sink_claims_carry_generation_and_protocol_columns(self, home, fake_dolt, tmp_path, monkeypatch):
        from brigade import cli

        db = tmp_path / "hub.db"
        _seed_generations(db)
        capture = tmp_path / "capture"
        capture.mkdir()
        log = tmp_path / "dolt.log"
        monkeypatch.setenv(FAKE_DOLT_LOG_ENV, str(log))
        monkeypatch.setenv(FAKE_DOLT_CAPTURE_ENV, str(capture))
        _write_config(home, f'[fleet.sink]\nenabled = true\ndolt_dir = "{tmp_path / "dolt-home"}"\n')

        assert cli.main(["fleet", "sink", "--db", str(db)]) == 0
        claims_csv = (capture / "fleet_claims.csv").read_text(encoding="utf-8")
        assert claims_csv.splitlines()[0] == ",".join(fleet_export.CLAIM_COLUMNS)
        (row,) = csv.DictReader(io.StringIO(claims_csv))
        assert (row["target"], row["generation"], row["protocol"]) == ("repo-a", "3", "legacy")
        assert "holder-" not in claims_csv
        ddl = fleet_export._DOLT_SCHEMA_CLAIMS
        assert "generation bigint NOT NULL" in ddl and "protocol varchar(16) NOT NULL" in ddl
        assert ddl in log.read_text(encoding="utf-8").splitlines()

    @pytest.mark.skipif(os.name != "posix", reason="fake dolt shell stub requires POSIX")
    def test_sink_refuses_damaged_claim_fences_before_dolt_watermark_or_commit(
        self, home, fake_dolt, tmp_path, monkeypatch, capsys
    ):
        from brigade import cli

        db = tmp_path / "hub.db"
        _seed_events(db, _event())
        _seed_generations(db)
        _exec(db, "DELETE FROM claim_fences WHERE target = 'repo-expired'")
        dolt_dir = tmp_path / "dolt-home"
        log = tmp_path / "dolt.log"
        monkeypatch.setenv(FAKE_DOLT_LOG_ENV, str(log))
        monkeypatch.setenv(FAKE_DOLT_CAPTURE_ENV, str(tmp_path))
        _write_config(home, f'[fleet.sink]\nenabled = true\ndolt_dir = "{dolt_dir}"\n')

        assert cli.main(["fleet", "sink", "--db", str(db)]) == 1
        assert "claim generation fence" in capsys.readouterr().err
        assert not log.exists()
        assert not (dolt_dir / fleet_export.EVENTS_WATERMARK_FILE).exists()

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


_LEGACY_DOLT_CLAIMS = (
    "CREATE TABLE fleet_claims ("
    "target varchar(512) NOT NULL PRIMARY KEY, owner_node varchar(128) NOT NULL, "
    "owner_conductor varchar(128), acquired_at varchar(64) NOT NULL, renewed_at varchar(64) NOT NULL, "
    "ttl_seconds bigint NOT NULL, expires_at varchar(64) NOT NULL, expired boolean NOT NULL)"
)


class _RealDolt:
    """Drives an explicit real Dolt binary against temp directories only."""

    def __init__(self, binary: str, home: Path) -> None:
        self.binary = binary
        self.home = home

    def run(self, cwd: Path, *args: str) -> str:
        env = {**os.environ, **home_env(self.home), "DOLT_ROOT_PATH": str(self.home)}
        proc = subprocess.run(
            [self.binary, *args], cwd=cwd, env=env, capture_output=True, text=True, check=False, timeout=120
        )
        assert proc.returncode == 0, f"dolt {' '.join(args[:2])} failed: {proc.stderr or proc.stdout}"
        return proc.stdout

    def rows(self, cwd: Path, query: str) -> list[dict[str, str]]:
        return list(csv.DictReader(io.StringIO(self.run(cwd, "sql", "-r", "csv", "-q", query))))

    def commit_count(self, cwd: Path) -> int:
        return int(self.rows(cwd, "SELECT COUNT(*) AS n FROM dolt_log")[0]["n"])

    def columns(self, cwd: Path) -> dict[str, str]:
        return {r["Field"]: r["Null"] for r in self.rows(cwd, "SHOW COLUMNS FROM fleet_claims")}


@pytest.fixture()
def real_dolt(tmp_path, monkeypatch):
    real = os.environ.get(REAL_DOLT_BIN_ENV)
    if not real:
        pytest.skip(f"{REAL_DOLT_BIN_ENV} names an explicit real dolt binary for the optional migration tests")
    home = tmp_path / "dolt-user-home"
    home.mkdir()
    set_home(monkeypatch, home)
    return _RealDolt(real, home)


def _seed_legacy_dolt(dolt: _RealDolt, dolt_dir: Path) -> None:
    """A pre-generation sink: legacy claims columns, a sentinel row, an index, an unrelated table, history."""
    dolt_dir.mkdir()
    dolt.run(dolt_dir, "init", "--name", "brigade", "--email", "brigade@example.test")
    dolt.run(
        dolt_dir,
        "sql",
        "-q",
        _LEGACY_DOLT_CLAIMS + "; CREATE INDEX idx_claims_owner ON fleet_claims (owner_node); "
        "INSERT INTO fleet_claims VALUES ('sentinel-old', 'node-old', NULL, 'a', 'b', 900, 'c', false); "
        "CREATE TABLE unrelated_notes (id bigint NOT NULL PRIMARY KEY, body varchar(64) NOT NULL); "
        "INSERT INTO unrelated_notes VALUES (1, 'keep me')",
    )
    dolt.run(dolt_dir, "add", "-A")
    dolt.run(dolt_dir, "commit", "-m", "pre-generation sink state")


def _write_real_dolt_config(home: Path, dolt: _RealDolt, dolt_dir: Path) -> None:
    _write_config(home, f'[fleet.sink]\nenabled = true\ndolt_binary = "{dolt.binary}"\ndolt_dir = "{dolt_dir}"\n')


class TestDoltClaimsMigration:
    @pytest.mark.skipif(os.name != "posix", reason="fake dolt shell stub requires POSIX")
    def test_claims_schema_step_is_one_static_idempotent_batch(self, home, fake_dolt, tmp_path, monkeypatch):
        from brigade import cli

        db = tmp_path / "hub.db"
        _seed_generations(db)
        log = tmp_path / "dolt.log"
        capture = tmp_path / "capture"
        capture.mkdir()
        monkeypatch.setenv(FAKE_DOLT_LOG_ENV, str(log))
        monkeypatch.setenv(FAKE_DOLT_CAPTURE_ENV, str(capture))
        _write_config(home, f'[fleet.sink]\nenabled = true\ndolt_dir = "{tmp_path / "dolt-home"}"\n')
        assert cli.main(["fleet", "sink", "--db", str(db)]) == 0

        ddl = fleet_export._DOLT_SCHEMA_CLAIMS
        argv_lines = log.read_text(encoding="utf-8").splitlines()
        assert argv_lines.count(ddl) == 1 and "\n" not in ddl
        assert ddl.startswith("CREATE TABLE IF NOT EXISTS fleet_claims (")
        for column, definition in (("generation", "BIGINT NULL"), ("protocol", "VARCHAR(16) NULL")):
            assert f"'ALTER TABLE fleet_claims ADD COLUMN {column} {definition}'" in ddl
            assert f"column_name = '{column}'" in ddl
        assert ddl.count("; PREPARE brigade_stmt FROM") == 2 and ddl.count("DEALLOCATE PREPARE brigade_stmt") == 2
        assert "DROP" not in ddl.upper() and "ADD COLUMN IF" not in ddl.upper()

    def test_old_dolt_claims_table_is_migrated_idempotently_without_losing_history(
        self, home, real_dolt, tmp_path, capsys
    ):
        from brigade import cli

        db = tmp_path / "hub.db"
        _seed_generations(db)
        dolt_dir = tmp_path / "old-dolt"
        _seed_legacy_dolt(real_dolt, dolt_dir)
        before_commits = real_dolt.commit_count(dolt_dir)
        _write_real_dolt_config(home, real_dolt, dolt_dir)

        assert cli.main(["fleet", "sink", "--db", str(db)]) == 0
        assert "1 claim(s)" in capsys.readouterr().out
        query = "SELECT target, owner_node, generation, protocol FROM fleet_claims"
        claims = real_dolt.rows(dolt_dir, query)
        columns = real_dolt.columns(dolt_dir)
        assert claims == [{"target": "repo-a", "owner_node": "node-a", "generation": "3", "protocol": "legacy"}]
        assert columns["generation"] == "YES" and columns["protocol"] == "YES"
        assert "idx_claims_owner" in {r["Key_name"] for r in real_dolt.rows(dolt_dir, "SHOW INDEX FROM fleet_claims")}
        assert real_dolt.rows(dolt_dir, "SELECT id, body FROM unrelated_notes") == [{"id": "1", "body": "keep me"}]
        messages = {r["message"] for r in real_dolt.rows(dolt_dir, "SELECT message FROM dolt_log")}
        assert "pre-generation sink state" in messages
        assert real_dolt.commit_count(dolt_dir) == before_commits + 1

        assert cli.main(["fleet", "sink", "--db", str(db)]) == 0
        assert "no working-set changes" in capsys.readouterr().out
        assert real_dolt.rows(dolt_dir, query) == claims
        assert real_dolt.columns(dolt_dir) == columns
        assert real_dolt.commit_count(dolt_dir) == before_commits + 1

    def test_fresh_dolt_sink_creates_required_generation_columns(self, home, real_dolt, tmp_path):
        from brigade import cli

        db = tmp_path / "hub.db"
        _seed_generations(db)
        dolt_dir = tmp_path / "fresh-dolt"
        _write_real_dolt_config(home, real_dolt, dolt_dir)

        assert cli.main(["fleet", "sink", "--db", str(db)]) == 0
        assert cli.main(["fleet", "sink", "--db", str(db)]) == 0
        columns = real_dolt.columns(dolt_dir)
        assert columns["generation"] == "NO" and columns["protocol"] == "NO"
        claims = real_dolt.rows(dolt_dir, "SELECT target, generation, protocol FROM fleet_claims")
        assert claims == [{"target": "repo-a", "generation": "3", "protocol": "legacy"}]

    def test_corrupt_source_fails_before_any_dolt_change(self, home, real_dolt, tmp_path, capsys):
        from brigade import cli

        db = tmp_path / "hub.db"
        _seed_generations(db)
        _exec(db, "DELETE FROM claim_fences WHERE target = 'repo-a'")
        dolt_dir = tmp_path / "old-dolt"
        _seed_legacy_dolt(real_dolt, dolt_dir)
        before_commits = real_dolt.commit_count(dolt_dir)
        _write_real_dolt_config(home, real_dolt, dolt_dir)

        assert cli.main(["fleet", "sink", "--db", str(db)]) == 1
        assert "claim generation fence" in capsys.readouterr().err
        assert {"generation", "protocol"}.isdisjoint(real_dolt.columns(dolt_dir))
        assert [r["target"] for r in real_dolt.rows(dolt_dir, "SELECT target FROM fleet_claims")] == ["sentinel-old"]
        assert real_dolt.commit_count(dolt_dir) == before_commits
        assert real_dolt.rows(dolt_dir, "SELECT COUNT(*) AS n FROM dolt_status")[0]["n"] == "0"


def test_export_and_sink_parser_callbacks_are_lazy_wrappers():
    from brigade import cli
    from brigade.cli import fleet as fleet_cli

    parser = cli._build_parser()
    assert parser.parse_args(["fleet", "export"]).func is fleet_cli._dispatch_export
    assert parser.parse_args(["fleet", "sink"]).func is fleet_cli._dispatch_sink
