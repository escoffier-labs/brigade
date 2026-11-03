"""Keep the verify-brigade Fleet Hub storage parity drive honest.

`control-brigade.py storage-parity` runs one scripted request sequence against
a hub on a stepped clock, dumps every table of its SQLite file, and compares
the rows exactly with a golden the in-repo Python hub produced. These tests
run it against the in-repo hub (alone, mixed across restarts, with the events
outbox on, and on databases migrated from release fixtures), then change one
stored value at a time, without changing its format, and require the
comparison to catch each change.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import shlex
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_verify_brigade_skill import REPO_ROOT, SKILL_DIR, control

DEFAULT_TEMPLATE = (
    "{python} {launcher} --clock-file {clock_file} -- "
    "fleet serve --host {host} --port {port} --db {db} --token-file {token_file}"
)


def _load():
    if str(SKILL_DIR) not in sys.path:
        sys.path.insert(0, str(SKILL_DIR))
    return importlib.import_module("storage_parity")


@pytest.fixture(scope="module")
def sp():
    return _load()


@pytest.fixture(scope="module")
def defaults_run(tmp_path_factory):
    code, payload = control("storage-parity", root=tmp_path_factory.mktemp("parity") / "state")
    assert code == 0, payload
    return payload


def _assert_clean(payload: dict, profile: str) -> None:
    assert payload["ok"] is True, payload
    assert payload["profile"] == profile
    assert payload["step_failures"] == []
    assert payload["relation_failures"] == []
    assert payload["rule_problems"] == []
    assert payload["differences"] == []
    assert payload["dump_digest"] == payload["golden_digest"]


def test_storage_parity_golden_holds_on_two_runs(defaults_run, tmp_path):
    """The golden is deterministic: a second run, this time with the hub swapped at each restart, matches it."""
    _assert_clean(defaults_run, "defaults")
    assert defaults_run["engines"] == ["a", "a", "a"]
    assert defaults_run["tables"] >= 76
    code, mixed = control(
        "storage-parity",
        "--engines",
        "a,b,a",
        "--other-hub-cmd",
        DEFAULT_TEMPLATE,
        root=tmp_path / "state",
    )
    assert code == 0, mixed
    _assert_clean(mixed, "defaults")
    assert mixed["engines"] == ["a", "b", "a"]
    assert [Path(hub["log"]).name for hub in mixed["hubs"]] == ["hub-1-a.log", "hub-2-b.log", "hub-3-a.log"]
    assert mixed["dump_digest"] == defaults_run["dump_digest"]


@pytest.mark.parametrize("profile", ["outbox", "migrated-v34", "migrated-v22"])
def test_storage_parity_profiles_match_their_goldens(profile, tmp_path):
    code, payload = control("storage-parity", "--profile", profile, root=tmp_path / "state")
    assert code == 0, payload
    _assert_clean(payload, profile)


def test_storage_parity_outbox_run_stores_events(sp):
    """The outbox profile writes the MCP Events tables; the defaults profile leaves them empty."""
    profiles = sp.load_golden()["profiles"]
    for table in ("fleet_events", "fleet_event_deliveries", "fleet_event_subscriptions", "fleet_event_attempts"):
        assert profiles["outbox"]["tables"][table]["rows"], table
        assert profiles["defaults"]["tables"][table]["rows"] == [], table
    url = profiles["outbox"]["tables"]["fleet_event_subscriptions"]
    assert ["t:<sink>/hook"] == [row[url["columns"].index("url")] for row in url["rows"]]


def test_storage_parity_migration_changes_only_install_stamps(sp):
    """A database upgraded from a release fixture holds the same rows as a fresh one.

    The fixtures were installed 30 days before the run, so the rows the hub
    seeds at install carry that time. Nothing else may differ.
    """
    profiles = sp.load_golden()["profiles"]
    for profile in ("migrated-v34", "migrated-v22"):
        differences = sp.compare(profiles["defaults"]["tables"], profiles[profile]["tables"], limit=1000)
        assert differences, profile
        for item in differences:
            assert item["check"] == "value", item
            assert item["expected"] == "t:<T+0|YYYY-MM-DDTHH:MM:SS+00:00>", item
            assert item["observed"] == "t:<T-2592000|YYYY-MM-DDTHH:MM:SS+00:00>", item


def test_storage_parity_dumps_every_table_and_trigger_output(sp, tmp_path):
    """The golden names every table a fresh hub creates, and the trigger-maintained ones hold rows."""
    from brigade import fleet_hub

    db = tmp_path / "fresh.db"
    fleet_hub.init_db(db).close()
    conn = sqlite3.connect(db)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
        triggers = conn.execute("SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger'").fetchone()[0]
    finally:
        conn.close()
    golden = sp.load_golden()["profiles"]["defaults"]["tables"]
    assert set(golden) == tables
    assert triggers == 15
    questions = golden["fleet_questions"]
    status = questions["columns"].index("status")
    # One question answered through the reply trigger, one answered after its deadline.
    assert sorted(row[status] for row in questions["rows"]) == ["t:answered", "t:timeout"]
    groups = golden["fleet_read_snapshot_groups"]
    total, count = groups["columns"].index("total"), groups["columns"].index("row_count")
    # Revoking a node flips its snapshot row invisible, and the total trigger follows.
    assert any(row[total] != row[count] for row in groups["rows"])
    messages = golden["fleet_messages"]
    state = messages["columns"].index("content_state")
    assert "t:content_expired" in [row[state] for row in messages["rows"]]


def test_storage_parity_rules_name_real_columns(sp, defaults_run):
    raw = sp.dump(Path(defaults_run["db"]))
    assert sp.rule_problems(raw, sp.load_rules()) == []
    assert sp.rule_problems(raw, {"random": {"nodes": {"nope": "x"}}, "json": {"missing": {"a": "compact"}}}) == [
        "random: no column nodes.nope",
        "json: no table missing",
    ]


# ----------------------------------------------------------- mutations


def _sealed(text: str, **changes) -> str:
    value = json.loads(text)
    value.update(changes)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _compact_keys(conn: sqlite3.Connection) -> None:
    kid, record = conn.execute("SELECT kid, record FROM fleet_message_keys ORDER BY kid LIMIT 1").fetchone()
    compact = json.dumps(json.loads(record), separators=(",", ":"))
    conn.execute("UPDATE fleet_message_keys SET record=? WHERE kid=?", (compact, kid))


def _keep_recipients(conn: sqlite3.Connection) -> None:
    row_id, text = conn.execute(
        "SELECT id, envelope FROM fleet_messages WHERE content_state='content_expired'"
    ).fetchone()
    kept = _sealed(text, recipients=[{"kid": "k1_kept", "wrap": "AAAA"}])
    conn.execute("UPDATE fleet_messages SET envelope=?, envelope_bytes=? WHERE id=?", (kept, len(kept), row_id))


MUTATIONS = {
    "a timestamp format": (
        "UPDATE nodes SET created_at=replace(created_at, '+00:00', 'Z') WHERE node_id='parity-a'",
        ("nodes", "created_at"),
        None,
    ),
    "a JSON separator": (_compact_keys, ("fleet_message_keys", "record"), "json-encoding"),
    "a generation off by one": (
        "UPDATE claim_fences SET generation=generation+1 WHERE target='repo:example-org/parity-two'",
        ("claim_fences", "generation"),
        None,
    ),
    "an expiry one second late": (
        "UPDATE claims SET expires_at=expires_at+1",
        ("claims", "expires_at"),
        "claim-expiry",
    ),
    "a wrong digest": (
        "UPDATE fleet_messages SET payload_hash='" + hashlib.sha256(b"parity").hexdigest() + "' "
        "WHERE content_state='sealed'",
        ("fleet_messages", "payload_hash"),
        "sealed-digest",
    ),
    "a broken reply link": (
        "UPDATE fleet_messages SET sealed_reply_to=id, reply_to=id WHERE client_id='s2'",
        ("fleet_messages", "reply_to"),
        "reply-link",
    ),
    "a question bound to the wrong token": (
        "UPDATE fleet_questions SET generation=(SELECT token_sha256 FROM nodes WHERE node_id='parity-b')",
        ("fleet_questions", "generation"),
        None,
    ),
    "a tombstone that keeps recipients": (_keep_recipients, ("fleet_messages", "envelope"), "tombstone"),
}


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_storage_parity_catches_each_mutation(name, sp, defaults_run, tmp_path):
    change, (table, column), relation = MUTATIONS[name]
    db = tmp_path / "hub.db"
    shutil.copyfile(defaults_run["db"], db)
    rules, golden = sp.load_rules(), sp.load_golden()["profiles"]["defaults"]["tables"]
    untouched = sp.dump(db)
    assert sp.compare(golden, sp.normalize(untouched, rules)) == []
    assert sp.relations(untouched, rules, None, None) == []

    conn = sqlite3.connect(db)
    try:
        if isinstance(change, str):
            conn.execute(change)
        else:
            change(conn)
        conn.commit()
    finally:
        conn.close()
    raw = sp.dump(db)
    differences = sp.compare(golden, sp.normalize(raw, rules))
    assert (table, column) in {(item["table"], item.get("column")) for item in differences}, differences
    if relation is not None:
        assert relation in {item["check"] for item in sp.relations(raw, rules, None, None)}


# ----------------------------------------------------------- normalizer units


def test_storage_parity_timestamps_keep_their_shape(sp):
    assert sp.timestamp_form("2026-03-02T00:00:01.250000+00:00") == "<T+1.25|YYYY-MM-DDTHH:MM:SS.ffffff+00:00>"
    assert sp.timestamp_form("2026-03-02T00:00:01+00:00") == "<T+1|YYYY-MM-DDTHH:MM:SS+00:00>"
    assert sp.timestamp_form("2026-03-02T00:00:01Z") == "<T+1|YYYY-MM-DDTHH:MM:SSZ>"
    assert sp.timestamp_form("2026-03-01T23:00:00-01:00") == "<T+0|YYYY-MM-DDTHH:MM:SS-01:00>"
    assert sp.timestamp_form("2026-03-01T23:59:59.5+00:00") == "<T-0.5|YYYY-MM-DDTHH:MM:SS.f+00:00>"
    assert sp.timestamp_form("parity") is None
    assert sp.epoch_form(float(sp.T0) + 2.25) == "<E+2.25>"
    assert sp.epoch_form(5.0) is None


def test_storage_parity_maps_random_ids_inside_other_columns(sp):
    digest = "ab" * 32
    tables = {
        "nodes": {
            "columns": ["node_id", "token_sha256"],
            "rowid": True,
            "rows": [{"node_id": "n", "token_sha256": digest}],
        },
        "notes": {"columns": ["body"], "rowid": True, "rows": [{"body": f'{{"by":"{digest}","n":5}}'}, {"body": 5.0}]},
    }
    normalized = sp.normalize(tables, {"random": {"nodes": {"token_sha256": "token-digest"}}})
    assert normalized["nodes"]["rows"] == [["t:n", "t:<token-digest#1>"]]
    assert normalized["notes"]["rows"] == [['t:{"by":"<token-digest#1>","n":5}'], ["r:5.0"]]


def test_storage_parity_pattern_rules_map_only_generated_values(sp):
    tables = {
        "fleet_messages": {
            "columns": ["client_id"],
            "rowid": True,
            "rows": [{"client_id": "p-old-1"}, {"client_id": "0f" * 16}],
        }
    }
    rules = {"random": {"fleet_messages": {"client_id": {"space": "client-id", "match": "[0-9a-f]{32}"}}}}
    assert sp.normalize(tables, rules)["fleet_messages"]["rows"] == [["t:p-old-1"], ["t:<client-id#1>"]]


# ----------------------------------------------------------- arguments


def test_storage_parity_dry_run_starts_nothing(tmp_path):
    root = tmp_path / "state"
    code, payload = control("storage-parity", "--dry-run", "--profile", "outbox", root=root)
    assert code == 0, payload
    assert payload["dry_run"] is True
    assert payload["profile"] == "outbox"
    assert [engine["name"] for engine in payload["engines"]] == ["a", "a", "a"]
    assert not (root / "targets").exists() or not any((root / "targets").iterdir())


@pytest.mark.parametrize(
    ("args", "error"),
    [
        (["--engines", "a,b,a"], "engine 'b' needs --other-hub-cmd"),
        (["--engines", "a,a"], "--engines needs 3 entries, one per phase"),
        (["--engines", "a,c,a"], "--engines names engines a and b only, not 'c'"),
        (["--hub-cmd", "hub --port {port}"], "must contain {clock_file}"),
        (["--write-golden", "--hub-cmd", DEFAULT_TEMPLATE], "--write-golden records the in-repo Python hub alone"),
    ],
)
def test_storage_parity_refuses_bad_arguments(args, error, tmp_path):
    code, payload = control("storage-parity", *args, root=tmp_path / "state")
    assert code == 1, payload
    assert payload["ok"] is False
    assert error in payload["error"]


def test_storage_parity_reports_a_hub_that_never_starts(tmp_path):
    code, payload = control(
        "storage-parity",
        "--hub-cmd",
        " ".join([shlex.quote(sys.executable), "-c", shlex.quote("raise SystemExit(4)"), "{port}", "{clock_file}"]),
        "--startup-timeout",
        "5",
        root=tmp_path / "state",
    )
    assert code == 1, payload
    assert "exited with 4" in payload["error"]


def test_storage_parity_clock_launcher_needs_its_file(tmp_path):
    launcher = _load().LAUNCHER
    proc = subprocess.run(
        [sys.executable, str(launcher), "--clock-file", str(tmp_path / "missing"), "--", "--version"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=REPO_ROOT,
    )
    assert proc.returncode == 2
    assert "does not exist" in proc.stderr
