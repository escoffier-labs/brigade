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
    assert receipt["status"] == "completed"
    assert receipt["bd_version"] == "1.3.1"
    assert receipt["input_digest"] == hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    items = work_cmd.ledger._read_imports(tmp_path)
    assert all(item["status"] == "pending" for item in items)
    assert all(item["metadata"]["provenance"] for item in items)
    assert all(item["metadata"]["bd"]["input_digest"] == receipt["input_digest"] for item in items)
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


@pytest.mark.parametrize("dismiss", [False, True], ids=["pending", "dismissed"])
def test_bd_replay_ignores_operational_row_changes(tmp_path, capsys, dismiss):
    assert run_import(tmp_path, FIXTURE, capsys)[0] == 0
    original = work_cmd.ledger._read_imports(tmp_path)
    if dismiss:
        for item in original:
            assert work_cmd.import_dismiss(target=tmp_path, import_id=item["id"], reason="reviewed") == 0
        capsys.readouterr()
    rows = [json.loads(line) for line in FIXTURE.read_text().splitlines()]
    for row in rows:
        row.update(
            updated_at="2026-10-20T19:00:00Z",
            heartbeat_at="2026-10-20T19:00:00Z",
            lease_expires_at="2026-10-20T19:05:00Z",
            comment_count=3,
            dependency_count=5,
        )
    path = tmp_path / "fresh.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    rc, repeated = run_import(tmp_path, path, capsys)
    assert rc == 0
    assert repeated["created"] == 0
    assert repeated["skipped_dismissed" if dismiss else "skipped_duplicates"] == 2
    assert len(work_cmd.ledger._read_imports(tmp_path)) == 2
    assert repeated["rows"][0]["row_digest"] != original[0]["metadata"]["bd"]["row_digest"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("title", "Revised plan"),
        ("notes", "Additional work"),
        ("priority", 3),
        ("acceptance_criteria", "Parser accepts exports"),
        ("status", "closed"),
        ("issue_type", "bug"),
        ("dependencies", [{"issue_id": "example-2", "depends_on_id": "example-1", "type": "parent-child"}]),
    ],
)
def test_bd_changed_proposal_is_an_additive_candidate(tmp_path, capsys, field, value):
    assert run_import(tmp_path, FIXTURE, capsys)[0] == 0
    rows = [json.loads(line) for line in FIXTURE.read_text().splitlines()]
    rows[1][field] = value
    path = tmp_path / "revised.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    rc, payload = run_import(tmp_path, path, capsys)
    assert rc == 0
    assert payload["created"] == 1
    assert payload["skipped_duplicates"] == 1
    assert len(work_cmd.ledger._read_imports(tmp_path)) == 3


@pytest.mark.parametrize("option", ["--target", "--json"])
def test_bd_parent_options_require_snapshot_mode(tmp_path, monkeypatch, capsys, option):
    current = tmp_path / "current"
    current.mkdir()
    destination = tmp_path / "destination"
    destination.mkdir()
    monkeypatch.chdir(current)
    args = [option, str(destination)] if option == "--target" else [option]
    with pytest.raises(SystemExit) as exc:
        cli.main(["work", "import", *args, "add", "task"])
    assert exc.value.code == 2
    assert "require --from-bd-jsonl" in capsys.readouterr().err
    assert work_cmd.ledger._read_imports(current) == []
    assert work_cmd.ledger._read_imports(destination) == []


@pytest.mark.parametrize("error_type", [work_cmd.ledger._ImportProvenanceError, RuntimeError])
def test_bd_publication_exception_leaves_failed_receipt(tmp_path, monkeypatch, error_type):
    from brigade.work_cmd.import_bd import import_bd_jsonl

    def fail_stamp(**kwargs):
        raise error_type("simulated publication failure")

    monkeypatch.setattr(work_cmd.ledger, "_stamp_import_provenance", fail_stamp)
    with pytest.raises(error_type, match="simulated publication failure"):
        import_bd_jsonl(target=tmp_path, input_path=FIXTURE, bd_version="1.3.1")
    receipts = list((tmp_path / ".brigade/work/import-runs").glob("*/receipt.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text())
    assert receipt["status"] == "failed"
    assert receipt["reason"] == "simulated publication failure"
    assert receipt["bd_version"] == "1.3.1"
    assert receipt["input_digest"] == hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    assert work_cmd.ledger._read_imports(tmp_path) == []


@pytest.mark.parametrize("existing", [False, True], ids=["absent-inbox", "existing-inbox"])
@pytest.mark.parametrize("failure", ["_dirfd_replace", "_dirfd_fsync"])
def test_bd_final_receipt_failure_rolls_back_publication(tmp_path, monkeypatch, capsys, existing, failure):
    from brigade.work_cmd import import_bd

    if existing:
        assert work_cmd.import_add(target=tmp_path, text="Existing reviewed candidate") == 0
        capsys.readouterr()
    inbox = work_cmd.helpers._imports_path(tmp_path)
    previous = inbox.read_bytes() if existing else None
    proofs = tmp_path / ".brigade/work/imports/proofs"
    previous_proofs = {path.name: path.read_bytes() for path in proofs.glob("*.json")}
    write_receipt = import_bd._write_receipt

    def fail_io(*args, **kwargs):
        raise OSError("simulated final receipt failure")

    def fail_final_receipt(directory, payload):
        if payload["status"] == "completed":
            assert len(payload["import_ids"]) == 2
            with monkeypatch.context() as fault:
                fault.setattr(work_cmd.ledger, failure, fail_io)
                write_receipt(directory, payload)
        else:
            write_receipt(directory, payload)

    with monkeypatch.context() as fault:
        fault.setattr(import_bd, "_write_receipt", fail_final_receipt)
        rc, error = run_import(tmp_path, FIXTURE, capsys)
    assert rc == 2
    assert error["reason"] == "bd_import_io_error"
    assert (inbox.read_bytes() if inbox.exists() else None) == previous
    assert {path.name: path.read_bytes() for path in proofs.glob("*.json")} == previous_proofs
    receipts = list((tmp_path / ".brigade/work/import-runs").glob("*/receipt.json"))
    assert len(receipts) == 1
    failed = json.loads(receipts[0].read_text())
    assert failed["status"] == "failed"
    assert failed["created"] == 0
    assert failed["import_ids"] == []
    rc, retried = run_import(tmp_path, FIXTURE, capsys)
    assert rc == 0
    assert retried["created"] == 2
    assert retried["skipped_duplicates"] == 0
    completed = json.loads(Path(retried["receipt_path"]).read_text())
    assert completed["status"] == "completed"
    assert set(completed["import_ids"]) == {
        item["id"] for item in work_cmd.ledger._read_imports(tmp_path) if item["source"] == "bd-jsonl"
    }


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
