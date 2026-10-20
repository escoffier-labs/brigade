"""The bd boundary owns snapshot validation, provenance and additive replay."""

import hashlib
import json
from pathlib import Path

import pytest

from brigade import cli, work_cmd


FIXTURE = Path(__file__).parent / "fixtures" / "bd" / "export.jsonl"


def run_import(target, path, capsys, *extra):
    rc = cli.main(
        [
            "work",
            "import",
            "--from-bd-jsonl",
            str(path),
            "--bd-version",
            "1.3.1",
            "--target",
            str(target),
            "--json",
            *extra,
        ]
    )
    return rc, json.loads(capsys.readouterr().out)


def test_bd_snapshot_receipts_replay_and_omissions_are_additive(tmp_path, capsys):
    rc, payload = run_import(tmp_path, FIXTURE, capsys)
    assert rc == 0
    assert payload["created"] == 2
    receipt = json.loads(Path(payload["receipt_path"]).read_text())
    assert receipt["bd_version"] == "1.3.1"
    assert receipt["input_digest"] == hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    items = work_cmd.ledger._read_imports(tmp_path)
    assert all(item["status"] == "pending" for item in items)
    assert all(item["metadata"]["provenance"] for item in items)
    assert items[0]["metadata"]["bd"]["row_digest"] == hashlib.sha256(FIXTURE.read_bytes().splitlines()[0]).hexdigest()
    assert items[1]["metadata"]["proposed_edges"] == [{"type": "blocks", "source": "example-1", "target": "example-2"}]
    rc, repeated = run_import(tmp_path, FIXTURE, capsys)
    assert rc == 0
    assert repeated["created"] == 0
    assert repeated["skipped_duplicates"] == 2
    assert repeated["receipt_path"] != payload["receipt_path"]
    smaller = tmp_path / "smaller.jsonl"
    smaller.write_bytes(FIXTURE.read_bytes().splitlines(keepends=True)[0])
    assert run_import(tmp_path, smaller, capsys)[0] == 0
    assert len(work_cmd.ledger._read_imports(tmp_path)) == 2
    assert not (tmp_path / ".brigade/work/tasks.json").exists()


@pytest.mark.parametrize(
    "change,reason",
    [
        ("unknown_row", "invalid_bd_row"),
        ("unknown_field", "invalid_bd_row"),
        ("malformed", "invalid_bd_jsonl"),
        ("unknown_edge", "unknown_edge_type"),
        ("cycle", "dependency_cycle"),
        ("orphan", "unknown_node_ref"),
    ],
)
def test_bd_rejects_entire_file_before_inbox_write(tmp_path, capsys, change, reason):
    assert work_cmd.import_add(target=tmp_path, text="Existing reviewed candidate") == 0
    capsys.readouterr()
    inbox = work_cmd.helpers._imports_path(tmp_path)
    previous = inbox.read_bytes()
    rows = [json.loads(line) for line in FIXTURE.read_text().splitlines()]
    if change == "unknown_row":
        rows.append({"_type": "memory", "key": "unhandled", "value": "data"})
    elif change == "unknown_field":
        rows[1]["surprise"] = True
    elif change == "unknown_edge":
        rows[1]["dependencies"][0]["type"] = "waits-for"
    elif change == "cycle":
        rows[0]["dependencies"] = [{"issue_id": "example-1", "depends_on_id": "example-2", "type": "blocks"}]
    elif change == "orphan":
        rows[1]["dependencies"][0]["depends_on_id"] = "missing"
    source = tmp_path / "input.jsonl"
    source.write_text("".join(json.dumps(row) + "\n" for row in rows) + ("{bad\n" if change == "malformed" else ""))
    rc, payload = run_import(tmp_path, source, capsys)
    assert rc == 2
    assert payload["reason"] == reason
    assert inbox.read_bytes() == previous


@pytest.mark.parametrize(
    "edge_type,source,target",
    [
        ("parent-child", "example-1", "example-2"),
        ("discovered-from", "example-2", "example-1"),
    ],
)
def test_bd_native_edge_directions(tmp_path, capsys, edge_type, source, target):
    rows = [json.loads(line) for line in FIXTURE.read_text().splitlines()]
    rows[1]["dependencies"][0]["type"] = edge_type
    path = tmp_path / "edges.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert run_import(tmp_path, path, capsys)[0] == 0
    items = work_cmd.ledger._read_imports(tmp_path)
    assert items[1]["metadata"]["proposed_edges"] == [{"type": edge_type, "source": source, "target": target}]
