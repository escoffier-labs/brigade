import errno
import json
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from brigade import cli
from brigade import dogfood_cmd
from brigade import handoff_cmd
from brigade import localio
from brigade import work_cmd
from brigade.work_cmd import helpers

from tests.work_cmd_test_helpers import (
    _write_json,
    _init_git_repo,
)


def _builtin_scanner(scanner_id: str) -> dict[str, object]:
    from brigade.work_cmd import constants

    return dict(next(scanner for scanner in constants.SCANNER_DEFAULTS if scanner["id"] == scanner_id))


def _verified_builtin_scanner_run(scanner: dict[str, object]) -> dict[str, object]:
    """Construct the verifier-held capability used by direct scanner unit tests."""
    from brigade.work_cmd import scanners as scanners_mod

    run: dict[str, object] = {
        "run_id": f"verified-{scanner['id']}",
        "status": "completed",
        "exit_code": 0,
        "output_after": {"path": "output"},
    }
    scanners_mod._register_scanner_run_proof(scanner, run)
    return run


def _record_scanner_run_authority(tmp_path: Path, run_id: str) -> None:
    directory = helpers._scanner_runs_root(tmp_path) / run_id
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        work_cmd.ledger._record_verifier_owned_directory(
            tmp_path,
            components=(".brigade", "scanners", "runs", run_id),
            directory=descriptor,
        )
    finally:
        os.close(descriptor)


def _bind_planted_scanner_receipt(tmp_path: Path, run_id: str) -> None:
    path = helpers._scanner_runs_root(tmp_path) / run_id / "receipt.json"
    data = path.read_bytes()
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        work_cmd.ledger._record_verifier_owned_file(
            tmp_path,
            components=(".brigade", "scanners", "runs", run_id, "receipt.json"),
            descriptor=descriptor,
            data=data,
        )
    finally:
        os.close(descriptor)


def test_scanner_receipt_producer_rejects_run_directory_swap(tmp_path, monkeypatch):
    from brigade.work_cmd import scanners as scanners_mod

    outside = tmp_path / "outside-run"
    outside.mkdir()
    real_write_json = work_cmd.helpers._write_json
    swapped = False

    def swap_then_write(path: Path, data: object) -> None:
        nonlocal swapped
        if not swapped and path.name == "receipt.json":
            swapped = True
            held = path.parent.with_name(path.parent.name + "-held")
            path.parent.rename(held)
            path.parent.symlink_to(outside, target_is_directory=True)
        real_write_json(path, data)

    monkeypatch.setattr(work_cmd.helpers, "_write_json", swap_then_write)
    scanner = {"id": "gate", "source": "security-scan", "command": "", "timeout": 1}

    scanners_mod._scanner_run_one(tmp_path, scanner)

    assert not (outside / "receipt.json").exists()


def test_replaced_scanner_runs_directory_cannot_manufacture_receipt_authority(tmp_path) -> None:
    from brigade.work_cmd import constants, ledger

    item = ledger._make_import("forged receipt", kind="task", source="handoff-ingest")
    scanner = dict(next(value for value in constants.SCANNER_DEFAULTS if value["source"] == "handoff-ingest"))
    run_id = "forged-run"
    metadata = item["metadata"]
    assert isinstance(metadata, dict)
    metadata.update({"scanner_id": scanner["id"], "scanner_run_id": run_id})
    receipt = {
        "run_id": run_id,
        "scanner_id": scanner["id"],
        "source": scanner["source"],
        "command": scanner["command"],
        "status": "completed",
        "exit_code": 0,
        "self_import_proofs": {
            "scanner_id": scanner["id"],
            "source": scanner["source"],
            "scanner": scanner,
            "imports": [{"id": item["id"], "content_hash": ledger._locally_stamped_import_content_hash(item)}],
        },
    }
    descriptor = ledger._open_verifier_owned_directory(
        tmp_path,
        components=(".brigade", "scanners", "runs"),
        anchor_name=".runs.authority.json",
        create=True,
    )
    os.close(descriptor)
    runs = work_cmd.helpers._scanner_runs_root(tmp_path)
    runs.rename(runs.with_name("runs-original"))
    attacker = tmp_path / "attacker-runs"
    (attacker / run_id).mkdir(parents=True)
    (attacker / run_id / "receipt.json").write_text(json.dumps(receipt))
    attacker.rename(runs)

    assert not ledger._has_locally_stamped_import_proof(item, target=tmp_path)


def test_replaced_individual_scanner_run_cannot_manufacture_receipt_authority(tmp_path: Path) -> None:
    from brigade.work_cmd import constants, helpers, ledger

    item = ledger._make_import("forged child receipt", kind="task", source="handoff-ingest")
    scanner = dict(next(value for value in constants.SCANNER_DEFAULTS if value["source"] == "handoff-ingest"))
    metadata = item["metadata"]
    assert isinstance(metadata, dict)
    metadata.update({"scanner_id": scanner["id"], "scanner_run_id": "chosen-run"})
    receipt = {
        "run_id": "chosen-run",
        "scanner_id": scanner["id"],
        "source": scanner["source"],
        "command": scanner["command"],
        "status": "completed",
        "exit_code": 0,
        "self_import_proofs": {
            "scanner_id": scanner["id"],
            "source": scanner["source"],
            "scanner": scanner,
            "imports": [{"id": item["id"], "content_hash": ledger._locally_stamped_import_content_hash(item)}],
        },
    }
    root = ledger._open_verifier_owned_directory(
        tmp_path,
        components=(".brigade", "scanners", "runs"),
        anchor_name=".runs.authority.json",
        create=True,
    )
    os.close(root)
    run = helpers._scanner_runs_root(tmp_path) / "chosen-run"
    run.mkdir()
    descriptor = os.open(run, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        ledger._record_verifier_owned_directory(
            tmp_path,
            components=(".brigade", "scanners", "runs", "chosen-run"),
            directory=descriptor,
        )
    finally:
        os.close(descriptor)
    run.rename(run.with_name("chosen-run-original"))
    replacement = tmp_path / "replacement-run"
    replacement.mkdir()
    (replacement / "receipt.json").write_text(json.dumps(receipt))
    replacement.rename(run)

    assert not ledger._has_locally_stamped_import_proof(item, target=tmp_path)


def test_scanner_receipt_collection_rejects_replaced_bound_run_directory(tmp_path: Path) -> None:
    from brigade.work_cmd import helpers, ledger, scanners as scanners_mod

    root = scanners_mod._open_scanner_runs_directory(tmp_path, create=True)
    run_id = "bound-run"
    os.mkdir(run_id, dir_fd=root)
    run = os.open(run_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
    try:
        ledger._record_verifier_owned_directory(
            tmp_path,
            components=(".brigade", "scanners", "runs", run_id),
            directory=run,
        )
    finally:
        os.close(run)
        os.close(root)
    run_path = helpers._scanner_runs_root(tmp_path) / run_id
    (run_path / "receipt.json").write_text(json.dumps({"run_id": run_id, "status": "completed"}))
    run_path.rename(run_path.with_name(f"{run_id}-original"))
    replacement = tmp_path / "replacement-run"
    replacement.mkdir()
    (replacement / "receipt.json").write_text(json.dumps({"run_id": run_id, "status": "completed"}))
    replacement.rename(run_path)

    assert scanners_mod._scanner_receipts(tmp_path) == []


def test_scanner_receipt_collection_rejects_renamed_receipt(tmp_path: Path) -> None:
    from brigade.work_cmd import helpers, ledger, scanners as scanners_mod

    root = scanners_mod._open_scanner_runs_directory(tmp_path, create=True)
    run_id = "bound-run"
    os.mkdir(run_id, dir_fd=root)
    run = os.open(run_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
    try:
        ledger._record_verifier_owned_directory(
            tmp_path,
            components=(".brigade", "scanners", "runs", run_id),
            directory=run,
        )
    finally:
        os.close(run)
        os.close(root)
    (helpers._scanner_runs_root(tmp_path) / run_id / "receipt.json").write_text(
        json.dumps({"run_id": "other-run", "status": "completed"})
    )

    assert scanners_mod._scanner_receipts(tmp_path) == []


def test_pre_created_unbound_scanner_runs_directory_is_not_adopted(tmp_path: Path) -> None:
    """#1036: create=True binds a clean released root but does not adopt children."""
    from brigade.work_cmd import helpers, ledger, scanners as scanners_mod

    runs = helpers._scanner_runs_root(tmp_path)
    planted = runs / "planted-child"
    planted.mkdir(parents=True)
    (planted / "receipt.json").write_text(json.dumps({"run_id": "planted-child", "status": "running"}))
    descriptor = scanners_mod._open_scanner_runs_directory(tmp_path, create=True)
    os.close(descriptor)

    _path, payload = ledger._read_external_directory_authority(tmp_path)
    directories = payload.get("directories") if isinstance(payload, dict) else None
    assert isinstance(directories, dict) and ".brigade/scanners/runs" in directories
    assert ".brigade/scanners/runs/planted-child" not in directories
    assert scanners_mod._scanner_receipts(tmp_path) == []
    assert scanners_mod._scanner_running_receipts(tmp_path) == []


def test_plaintext_runs_anchor_does_not_let_replacement_directory_forge_receipt(tmp_path: Path):
    from brigade.work_cmd import constants, helpers, ledger

    item = ledger._make_import("forged receipt", kind="task", source="handoff-ingest")
    scanner = dict(next(value for value in constants.SCANNER_DEFAULTS if value["source"] == "handoff-ingest"))
    run_id = "forged-run"
    metadata = item["metadata"]
    assert isinstance(metadata, dict)
    metadata.update({"scanner_id": scanner["id"], "scanner_run_id": run_id})
    receipt = {
        "run_id": run_id,
        "scanner_id": scanner["id"],
        "source": scanner["source"],
        "command": scanner["command"],
        "status": "completed",
        "exit_code": 0,
        "self_import_proofs": {
            "scanner_id": scanner["id"],
            "source": scanner["source"],
            "scanner": scanner,
            "imports": [{"id": item["id"], "content_hash": ledger._locally_stamped_import_content_hash(item)}],
        },
    }
    descriptor = ledger._open_verifier_owned_directory(
        tmp_path,
        components=(".brigade", "scanners", "runs"),
        anchor_name=".runs.authority.json",
        create=True,
    )
    os.close(descriptor)
    runs = helpers._scanner_runs_root(tmp_path)
    runs.rename(runs.with_name("runs-original"))
    replacement = tmp_path / "replacement-runs"
    (replacement / run_id).mkdir(parents=True)
    (replacement / run_id / "receipt.json").write_text(json.dumps(receipt))
    replacement.rename(runs)
    info = runs.stat()
    (tmp_path / ".brigade" / ".runs.authority.json").write_text(
        json.dumps({"schema_version": 1, "device": info.st_dev, "inode": info.st_ino})
    )

    assert not ledger._has_locally_stamped_import_proof(item, target=tmp_path)


def test_scanner_receipt_temp_substitution_does_not_publish_attacker_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from brigade.work_cmd import scanners as scanners_mod

    attacker = b'{"status":"completed","exit_code":0,"attacker":true}\n'
    original_replace = scanners_mod.os.replace

    def substitute(source, destination, *, src_dir_fd=None, dst_dir_fd=None):
        if destination == "receipt.json" and isinstance(source, str) and source.startswith(".receipt.json."):
            temporary = Path(f"/proc/self/fd/{src_dir_fd}") / source
            temporary.unlink()
            temporary.write_bytes(attacker)
        return original_replace(source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(scanners_mod.os, "replace", substitute)
    scanner = {"id": "gate", "source": "security-scan", "command": "", "timeout": 1}
    with pytest.raises(OSError):
        scanners_mod._scanner_run_one(tmp_path, scanner)

    receipts = list(helpers._scanner_runs_root(tmp_path).glob("*/receipt.json"))
    assert not receipts or receipts[0].read_bytes() != attacker


def test_work_doctor_warns_for_scanner_queue_health(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    monkeypatch.setattr(work_cmd.helpers.shutil, "which", lambda name: "/usr/bin/codex" if name == "codex" else None)
    monkeypatch.setattr(localio, "check_git_ignored", lambda repo, path: "yes")
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc),
    )
    imports = [
        {
            "id": "stale-task",
            "kind": "task",
            "source": "repo-scan",
            "text": "Stale task import",
            "status": "pending",
            "created_at": "2026-05-25T12:00:00+00:00",
            "updated_at": "2026-05-25T12:00:00+00:00",
        }
    ]
    for index in range(work_cmd.DISMISSED_SOURCE_WARN_THRESHOLD):
        imports.append(
            {
                "id": f"dismissed-{index}",
                "kind": "task",
                "source": "noisy-scan",
                "text": f"Noisy import {index}",
                "status": "dismissed",
                "created_at": "2026-05-29T12:00:00+00:00",
                "updated_at": "2026-05-29T12:00:00+00:00",
            }
        )
    work_cmd._write_imports(tmp_path, imports)

    assert work_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[warn] scanner_imports_stale: 1 pending import(s) older than 72h: stale-task" in out
    assert "[warn] scanner_import_acceptance: 1 pending task import(s) missing acceptance criteria: stale-task" in out
    assert "[warn] scanner_import_noise: dismissed import threshold 5: noisy-scan=5" in out


def test_work_scanners_init_list_show_plan_and_json(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(localio, "check_git_ignored", lambda repo, path: "yes")

    assert work_cmd.scanners_init(target=tmp_path) == 0
    out = capsys.readouterr().out
    config = tmp_path / ".brigade" / "scanners.toml"
    assert config.is_file()
    assert not config.is_symlink()
    assert f"scanner_config: {config}" in out
    assert "scanners: 9" in out
    assert ".brigade/scanners.toml" in (tmp_path / ".gitignore").read_text()

    assert work_cmd.scanners_list(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "work scanners:" in out
    assert "- chat-memory-sweep [enabled] daily@02:15 source=chat-memory-sweep" in out
    assert "brigade work import chat-sweep --json" in out
    assert "- friction-scan [enabled] weekly@05:00 source=friction-scan" in out

    assert work_cmd.scanners_list(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is True
    assert payload["scanners"][0]["id"] == "chat-memory-sweep"

    assert work_cmd.scanners_show(target=tmp_path, scanner_id="memory-refresh") == 0
    out = capsys.readouterr().out
    assert "scanner: memory-refresh" in out
    assert "source: memory-refresh" in out

    assert work_cmd.scanners_plan(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "work scanners plan:" in out
    assert "planned:" in out
    assert "conflicts: none" in out
    assert "suggested_schedule:" in out

    assert work_cmd.scanners_plan(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is True
    assert payload["planned"][0]["id"] == "handoff-ingest"
    assert payload["suggestions"]


def test_scanners_init_refuses_dangling_and_force_symlink_scanners_toml(tmp_path, capsys):
    """#1042: init/--force must not follow a planted scanners.toml symlink."""
    outside = tmp_path / "outside" / "planted.toml"
    outside.parent.mkdir()
    brigade = tmp_path / ".brigade"
    brigade.mkdir()
    config = brigade / "scanners.toml"
    config.symlink_to(outside)

    assert work_cmd.scanners_init(target=tmp_path) == 2
    err = capsys.readouterr().err
    assert "scanner config refused" in err
    assert "symlink" in err
    assert not outside.exists()
    assert config.is_symlink()
    assert config.readlink() == outside

    outside.write_text("secret-outside-config\n")
    assert work_cmd.scanners_init(target=tmp_path, force=True) == 2
    err = capsys.readouterr().err
    assert "scanner config refused" in err
    assert "symlink" in err
    assert outside.read_text() == "secret-outside-config\n"
    assert config.is_symlink()

    config.unlink()
    config.write_text("previous-regular\n")
    assert work_cmd.scanners_init(target=tmp_path, force=True) == 0
    assert config.is_file()
    assert not config.is_symlink()
    assert "[[scanner]]" in config.read_text()


def test_work_scanners_plan_detects_conflicts_and_suggests_staggering(tmp_path, capsys):
    _init_git_repo(tmp_path)
    config = tmp_path / ".brigade" / "scanners.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        """
[[scanner]]
id = "chat-memory-sweep"
source = "chat-memory-sweep"
command = "brigade work import chat-sweep --json"
cadence = "daily@02:00"
enabled = true
timeout = 900
output_path = ".brigade/chat-memory-sweeps/latest.json"
conflict_window = "02:00-02:30"

[[scanner]]
id = "memory-refresh"
source = "memory-refresh"
command = "brigade work import memory-refresh --json"
cadence = "daily@02:05"
enabled = true
timeout = 300
output_path = "memory/cards/decay/refresh-queue.json"
conflict_window = "02:10-02:40"
"""
    )

    assert work_cmd.scanners_plan(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "run_overlap: chat-memory-sweep, memory-refresh" in out
    assert "window_overlap: chat-memory-sweep, memory-refresh" in out
    assert "clustered_runs: chat-memory-sweep, memory-refresh" in out
    assert "memory-refresh: daily@02:15" in out

    assert work_cmd.scanners_plan(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert {item["type"] for item in payload["conflicts"]} == {
        "run_overlap",
        "window_overlap",
        "clustered_runs",
    }
    assert payload["suggestions"][1]["suggested_cadence"] == "daily@02:15"


def test_work_scanners_run_writes_receipt_and_reports_import_counts(tmp_path, capsys):
    _init_git_repo(tmp_path)
    script = tmp_path / "scanner.py"
    script.write_text(
        """
import json
from pathlib import Path

root = Path.cwd()
output = root / ".brigade" / "scanner-output.json"
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps({"ok": True}) + "\\n")
inbox = root / ".brigade" / "work" / "imports" / "inbox.jsonl"
inbox.parent.mkdir(parents=True, exist_ok=True)
record = {
    "id": "scan-import-1",
    "kind": "task",
    "source": "repo-scan",
    "text": "Review scanner output",
    "status": "pending",
    "created_at": "2026-05-28T12:00:00+00:00",
    "updated_at": "2026-05-28T12:00:00+00:00",
}
inbox.write_text(json.dumps(record) + "\\n")
print("scanner complete")
"""
    )
    config = tmp_path / ".brigade" / "scanners.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        f"""
[[scanner]]
id = "repo-scan"
source = "repo-scan"
command = "{sys.executable} {script}"
cadence = "daily@02:00"
enabled = true
timeout = 30
output_path = ".brigade/scanner-output.json"
conflict_window = "02:00-02:10"
"""
    )

    assert work_cmd.scanners_run(target=tmp_path, scanner_id="repo-scan") == 0
    out = capsys.readouterr().out
    assert "work scanners run:" in out
    assert "completed: 1" in out
    assert "pending_imports_before: 0" in out
    assert "pending_imports_after: 1" in out

    receipts = list((tmp_path / ".brigade" / "scanners" / "runs").glob("*/receipt.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text())
    assert receipt["scanner_id"] == "repo-scan"
    assert receipt["status"] == "completed"
    assert receipt["exit_code"] == 0
    assert receipt["timed_out"] is False
    assert receipt["stdout_summary"] == "scanner complete"
    assert Path(receipt["stdout_path"]).is_file()
    assert Path(receipt["stderr_path"]).is_file()
    assert receipt["output_before"] == {"path": str(tmp_path / ".brigade" / "scanner-output.json"), "exists": False}
    assert receipt["output_after"]["exists"] is True
    assert receipt["provenance_imports_stamped"] == 1
    imports = json.loads((tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl").read_text().splitlines()[0])
    assert imports["metadata"]["scanner_run_id"] == receipt["run_id"]
    assert imports["metadata"]["scanner_id"] == "repo-scan"
    assert imports["metadata"]["source_fingerprint"]

    assert work_cmd.scanners_run(target=tmp_path, scanner_id="repo-scan", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["completed"] == 1
    assert payload["imports_after"]["by_source"] == {"repo-scan": 1}
    assert payload["runs"][0]["provenance_imports_stamped"] == 0


def _scanner_import_output(tmp_path, import_path: str):
    from brigade.work_cmd import scanners as scanners_mod

    return scanners_mod._scanner_validate_import_output(
        tmp_path,
        {
            "id": "output-import",
            "source": "output-import",
            "import_path": import_path,
            "import_format": "jsonl",
        },
    )


def test_scanner_import_output_rejects_symlinked_leaf_outside_target(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside-import.jsonl"
    outside.write_text('{"text":"Outside scanner import"}\n')
    imports = tmp_path / "imports"
    imports.mkdir()
    (imports / "output.jsonl").symlink_to(outside)

    _path, records, errors = _scanner_import_output(tmp_path, "imports/output.jsonl")

    assert records == []
    assert errors


def test_scanner_import_output_rejects_fifo_without_blocking(tmp_path):
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO creation is unavailable on this platform")
    imports = tmp_path / "imports"
    imports.mkdir()
    os.mkfifo(imports / "output.jsonl")

    _path, records, errors = _scanner_import_output(tmp_path, "imports/output.jsonl")

    assert records == []
    assert errors


def test_scanner_import_output_rejects_hard_linked_leaf(tmp_path):
    imports = tmp_path / "imports"
    imports.mkdir()
    original = tmp_path / "original.jsonl"
    original.write_text('{"text":"Hard linked scanner import"}\n')
    os.link(original, imports / "output.jsonl")

    _path, records, errors = _scanner_import_output(tmp_path, "imports/output.jsonl")

    assert records == []
    assert errors


def test_scanner_import_output_rejects_parent_swapped_while_opening(tmp_path, monkeypatch):
    from brigade.work_cmd import scanners as scanners_mod

    imports = tmp_path / "imports"
    imports.mkdir()
    (imports / "output.jsonl").write_text('{"text":"Original scanner import"}\n')
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "output.jsonl").write_text('{"text":"Outside scanner import"}\n')
    original_open = scanners_mod.os.open
    swapped = False

    def swap_parent_then_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if not swapped and path == "output.jsonl" and dir_fd is not None:
            swapped = True
            held = imports.with_name("imports-held")
            imports.rename(held)
            imports.symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(scanners_mod.os, "open", swap_parent_then_open)
    monkeypatch.setattr(
        scanners_mod.os,
        "supports_dir_fd",
        scanners_mod.os.supports_dir_fd | {swap_parent_then_open},
    )

    _path, records, errors = _scanner_import_output(tmp_path, "imports/output.jsonl")

    assert swapped
    assert records == []
    assert errors


def test_scanner_import_output_rejects_target_root_swapped_while_opening(tmp_path, monkeypatch):
    from brigade.work_cmd import scanners as scanners_mod

    target = tmp_path / "repo"
    imports = target / "imports"
    imports.mkdir(parents=True)
    (imports / "output.jsonl").write_text('{"text":"Original scanner import"}\n')
    original_open = scanners_mod.os.open
    swapped = False

    def swap_target_root_then_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        opening_target_root = (path == target and dir_fd is None) or (path == target.name and dir_fd is not None)
        if not swapped and opening_target_root:
            swapped = True
            target.rename(target.with_name("repo-held"))
            replacement_imports = target / "imports"
            replacement_imports.mkdir(parents=True)
            (replacement_imports / "output.jsonl").write_text('{"text":"Replacement scanner import"}\n')
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(scanners_mod.os, "open", swap_target_root_then_open)
    monkeypatch.setattr(
        scanners_mod.os,
        "supports_dir_fd",
        scanners_mod.os.supports_dir_fd | {swap_target_root_then_open},
    )

    _path, records, errors = _scanner_import_output(target, "imports/output.jsonl")

    assert swapped
    assert records == []
    assert errors


def test_scanner_import_output_loads_ordinary_nested_relative_jsonl(tmp_path):
    imports = tmp_path / "imports" / "nested"
    imports.mkdir(parents=True)
    (imports / "output.jsonl").write_text('{"text":"Nested scanner import","source":"output-import"}\n')

    path, records, errors = _scanner_import_output(tmp_path, "imports/nested/output.jsonl")

    assert path == tmp_path / "imports" / "nested" / "output.jsonl"
    assert errors == []
    assert records == [
        {
            "text": "Nested scanner import",
            "kind": "task",
            "source": "output-import",
            "metadata": {},
        }
    ]


def test_scanner_import_output_rejects_absolute_path_when_config_is_bypassed(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-absolute-import.jsonl"
    outside.write_text('{"text":"Outside scanner import"}\n')

    _path, records, errors = _scanner_import_output(tmp_path, str(outside))

    assert records == []
    assert errors


def test_work_scanners_run_ingest_output_adds_provenance_only_with_flag(tmp_path, capsys):
    _init_git_repo(tmp_path)
    script = tmp_path / "scanner.py"
    script.write_text(
        """
import json
from pathlib import Path

root = Path.cwd()
path = root / ".brigade" / "scanner-imports.jsonl"
path.parent.mkdir(parents=True, exist_ok=True)
record = {
    "kind": "task",
    "source": "repo-scan",
    "text": "Review generated finding",
    "metadata": {"source_item_key": "finding-1"},
    "acceptance": ["Finding is reviewed."],
}
path.write_text(json.dumps(record) + "\\n")
print("wrote imports")
"""
    )
    config = tmp_path / ".brigade" / "scanners.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        f"""
[[scanner]]
id = "repo-scan"
source = "repo-scan"
command = "{sys.executable} {script}"
cadence = "daily@02:00"
enabled = true
timeout = 30
output_path = ".brigade/scanner-imports.jsonl"
import_path = ".brigade/scanner-imports.jsonl"
import_format = "jsonl"
conflict_window = "02:00-02:10"
"""
    )

    assert work_cmd.scanners_run(target=tmp_path, scanner_id="repo-scan") == 0
    out = capsys.readouterr().out
    assert "pending_imports_after: 0" in out
    assert work_cmd.import_list(target=tmp_path, json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["imports"] == []

    assert work_cmd.scanners_run(target=tmp_path, scanner_id="repo-scan", ingest_output=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ingest_output"] is True
    assert payload["runs"][0]["ingest_output"]["created"] == 1
    assert payload["imports_after"]["by_source"] == {"repo-scan": 1}

    item = payload["runs"][0]
    assert work_cmd.import_list(target=tmp_path, json_output=True) == 0
    imports = json.loads(capsys.readouterr().out)["imports"]
    assert len(imports) == 1
    metadata = imports[0]["metadata"]
    assert metadata["scanner_id"] == "repo-scan"
    assert metadata["scanner_source"] == "repo-scan"
    assert metadata["scanner_run_id"] == item["run_id"]
    assert metadata["scanner_receipt_path"].endswith("/receipt.json")
    assert metadata["scanner_import_path"].endswith(".brigade/scanner-imports.jsonl")
    assert metadata["source_fingerprint"]
    assert metadata["scanner_output_path_snapshot"]["exists"] is True


def test_import_path_ingest_rolls_back_when_final_receipt_publication_fails(tmp_path, monkeypatch):
    """#1040: explicit import_path commit + proofs roll back if the final receipt fails."""
    from brigade.work_cmd import scanners as scanners_mod

    _init_git_repo(tmp_path)
    script = tmp_path / "scanner.py"
    script.write_text(
        """
import json
from pathlib import Path

path = Path.cwd() / ".brigade" / "scanner-imports.jsonl"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps({"kind": "task", "source": "repo-scan", "text": "Review generated finding"}) + "\\n")
"""
    )
    config = tmp_path / ".brigade" / "scanners.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        f"""
[[scanner]]
id = "repo-scan"
source = "repo-scan"
command = "{sys.executable} {script}"
cadence = "daily@02:00"
enabled = true
timeout = 30
output_path = ".brigade/scanner-imports.jsonl"
import_path = ".brigade/scanner-imports.jsonl"
import_format = "jsonl"
conflict_window = "02:00-02:10"
"""
    )
    inbox = work_cmd.helpers._imports_path(tmp_path)
    real_write = scanners_mod._write_scanner_run_file

    def fail_final_receipt(authority, name, data, *, after_publish=None):
        if name == "receipt.json" and b'"ingest_output"' in data:
            raise OSError("forced final receipt publication failure")
        return real_write(authority, name, data, after_publish=after_publish)

    monkeypatch.setattr(scanners_mod, "_write_scanner_run_file", fail_final_receipt)

    with pytest.raises(OSError, match="forced final receipt publication failure"):
        scanners_mod._scanners_run_payload(
            target=tmp_path,
            scanner_id="repo-scan",
            ingest_output=True,
        )

    assert not inbox.exists() or inbox.read_bytes() in {b"", b"\n"}
    assert work_cmd.ledger._read_imports(tmp_path) == []
    proofs = tmp_path / ".brigade" / "work" / "imports" / "proofs"
    if proofs.is_dir():
        assert list(proofs.glob("*.json")) == []


def test_scanners_run_releases_directory_authority_when_import_publication_raises(tmp_path, monkeypatch):
    from brigade.work_cmd import scanners as scanners_mod

    _init_git_repo(tmp_path)
    script = tmp_path / "scanner.py"
    script.write_text(
        """
import json
from pathlib import Path

path = Path.cwd() / ".brigade" / "scanner-imports.jsonl"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps({"kind": "task", "source": "repo-scan", "text": "Review generated finding"}) + "\\n")
"""
    )
    config = tmp_path / ".brigade" / "scanners.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        f"""
[[scanner]]
id = "repo-scan"
source = "repo-scan"
command = "{sys.executable} {script}"
cadence = "daily@02:00"
enabled = true
timeout = 30
output_path = ".brigade/scanner-imports.jsonl"
import_path = ".brigade/scanner-imports.jsonl"
import_format = "jsonl"
conflict_window = "02:00-02:10"
"""
    )

    prior_keys = set(scanners_mod._SCANNER_RUN_DIRECTORY_AUTHORITIES)
    captured_keys: list[int] = []
    captured_fds: list[tuple[int, int]] = []

    def fail_publication(*_args: object, **_kwargs: object) -> None:
        authorities = scanners_mod._SCANNER_RUN_DIRECTORY_AUTHORITIES
        retained = {key: authority for key, authority in authorities.items() if key not in prior_keys}
        assert retained, "expected retained scanner-run directory authority"
        for key, authority in retained.items():
            captured_keys.append(key)
            captured_fds.append((authority.root, authority.directory))
            os.fstat(authority.root)
            os.fstat(authority.directory)
        raise RuntimeError("forced import proof publication failure")

    monkeypatch.setattr(scanners_mod.ledger_mod, "_append_import_records", fail_publication)

    with pytest.raises(RuntimeError, match="forced import proof publication failure"):
        scanners_mod._scanners_run_payload(target=tmp_path, scanner_id="repo-scan", ingest_output=True)

    assert captured_keys
    assert captured_fds
    remaining = scanners_mod._SCANNER_RUN_DIRECTORY_AUTHORITIES
    assert set(remaining) == prior_keys
    for key in captured_keys:
        assert key not in remaining
    for root, directory in captured_fds:
        with pytest.raises(OSError) as raised:
            os.fstat(root)
        assert raised.value.errno == errno.EBADF
        with pytest.raises(OSError) as raised:
            os.fstat(directory)
        assert raised.value.errno == errno.EBADF


def _repo_scan_config(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    script = tmp_path / "scanner.py"
    script.write_text("\n")
    config = tmp_path / ".brigade" / "scanners.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        f"""
[[scanner]]
id = "repo-scan"
source = "repo-scan"
command = "{sys.executable} {script}"
cadence = "daily@02:00"
enabled = true
timeout = 30
output_path = ".brigade/scanner-imports.jsonl"
import_path = ".brigade/scanner-imports.jsonl"
import_format = "jsonl"
conflict_window = "02:00-02:10"
"""
    )


def _capture_retained_scanner_run_authorities(
    prior_keys: set[int],
) -> tuple[list[int], list[tuple[int, int]]]:
    from brigade.work_cmd import scanners as scanners_mod

    authorities = scanners_mod._SCANNER_RUN_DIRECTORY_AUTHORITIES
    retained = {key: authority for key, authority in authorities.items() if key not in prior_keys}
    assert retained, "expected retained scanner-run directory authority"
    captured_keys: list[int] = []
    captured_fds: list[tuple[int, int]] = []
    for key, authority in retained.items():
        captured_keys.append(key)
        captured_fds.append((authority.root, authority.directory))
        os.fstat(authority.root)
        os.fstat(authority.directory)
    return captured_keys, captured_fds


def _assert_scanner_run_authorities_released(
    prior_keys: set[int],
    captured_keys: list[int],
    captured_fds: list[tuple[int, int]],
) -> None:
    from brigade.work_cmd import scanners as scanners_mod

    remaining = scanners_mod._SCANNER_RUN_DIRECTORY_AUTHORITIES
    assert set(remaining) == prior_keys
    for key in captured_keys:
        assert key not in remaining
    for root, directory in captured_fds:
        with pytest.raises(OSError) as raised:
            os.fstat(root)
        assert raised.value.errno == errno.EBADF
        with pytest.raises(OSError) as raised:
            os.fstat(directory)
        assert raised.value.errno == errno.EBADF


def test_scanners_run_releases_directory_authority_when_stamp_raises(tmp_path, monkeypatch):
    from brigade.work_cmd import scanners as scanners_mod

    _repo_scan_config(tmp_path)
    prior_keys = set(scanners_mod._SCANNER_RUN_DIRECTORY_AUTHORITIES)
    captured_keys: list[int] = []
    captured_fds: list[tuple[int, int]] = []

    def fail_stamp(*_args: object, **_kwargs: object) -> list[str]:
        keys, fds = _capture_retained_scanner_run_authorities(prior_keys)
        captured_keys.extend(keys)
        captured_fds.extend(fds)
        raise RuntimeError("forced stamp failure")

    monkeypatch.setattr(scanners_mod, "_scanner_stamp_new_imports", fail_stamp)

    with pytest.raises(RuntimeError, match="forced stamp failure"):
        scanners_mod._scanners_run_payload(target=tmp_path, scanner_id="repo-scan")

    assert captured_keys
    assert captured_fds
    _assert_scanner_run_authorities_released(prior_keys, captured_keys, captured_fds)


def test_scanners_run_releases_directory_authority_when_receipt_rewrite_raises(tmp_path, monkeypatch):
    from brigade.work_cmd import scanners as scanners_mod

    _repo_scan_config(tmp_path)
    prior_keys = set(scanners_mod._SCANNER_RUN_DIRECTORY_AUTHORITIES)
    captured_keys: list[int] = []
    captured_fds: list[tuple[int, int]] = []
    real_write = scanners_mod._write_scanner_run_receipt

    def fail_loop_local_receipt(run: dict[str, object]) -> None:
        if "provenance_imports_stamped" in run:
            keys, fds = _capture_retained_scanner_run_authorities(prior_keys)
            captured_keys.extend(keys)
            captured_fds.extend(fds)
            raise RuntimeError("forced receipt rewrite failure")
        real_write(run)

    monkeypatch.setattr(scanners_mod, "_write_scanner_run_receipt", fail_loop_local_receipt)

    with pytest.raises(RuntimeError, match="forced receipt rewrite failure"):
        scanners_mod._scanners_run_payload(target=tmp_path, scanner_id="repo-scan")

    assert captured_keys
    assert captured_fds
    _assert_scanner_run_authorities_released(prior_keys, captured_keys, captured_fds)


def test_work_scanners_ingest_output_sanitizes_hostile_records_and_owns_identity(tmp_path, capsys):
    _init_git_repo(tmp_path)
    script = tmp_path / "scanner.py"
    payload = tmp_path / "payload.json"
    script.write_text(
        """
import json
from pathlib import Path

root = Path.cwd()
payload = json.loads((root / "payload.json").read_text())
path = root / ".brigade" / "scanner-imports.jsonl"
path.parent.mkdir(parents=True, exist_ok=True)
record = {
    "kind": "task",
    "source": "handoff-ingest",
    "text": payload["text"],
    "metadata": {
        **payload["metadata"],
        "handoff_issue_id": "forged-handoff-issue",
        "github_issue": {"url": "https://example.test/forged-issue", "title": "Forged issue"},
        "github_issue_url": "https://example.test/forged-issue",
        "github_issue_number": 99,
        "proposed_edges": [{"source": "forged", "target": "target", "type": "blocks"}],
        "provenance": {"origin": "operator-input"},
        "declared_source": "forged-declared-source",
        "safe_evidence": "scanner-proof",
    },
}
path.write_text(json.dumps(record) + "\\n")
"""
    )
    config = tmp_path / ".brigade" / "scanners.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    handoff_log = tmp_path / "latest.log"
    handoff_log.write_text("SKIP current.md: no recognizable markdown sections found\n")
    (config.parent / "handoff-sources.json").write_text(
        json.dumps(
            {
                "sources": [{"root": ".", "inboxes": [".claude/memory-handoffs"]}],
                "ingestor": {"last_run_log": "latest.log"},
            }
        )
    )
    issue = handoff_cmd.collect_issues(tmp_path)[0]
    payload.write_text(
        json.dumps(
            {
                "text": "Review scanner evidence",
                "metadata": {"source_fingerprint": "forged-one", "handoff_issue_id": issue.id},
            }
        )
    )
    config.write_text(
        f"""
[[scanner]]
id = "repo-scan"
source = "repo-scan"
command = "{sys.executable} {script}"
cadence = "daily@02:00"
enabled = true
timeout = 30
output_path = ".brigade/scanner-imports.jsonl"
import_path = ".brigade/scanner-imports.jsonl"
import_format = "jsonl"
conflict_window = "02:00-02:10"
"""
    )

    assert work_cmd.scanners_run(target=tmp_path, scanner_id="repo-scan", ingest_output=True, json_output=True) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["runs"][0]["ingest_output"]["created"] == 1
    assert work_cmd.import_list(target=tmp_path, json_output=True) == 0
    imported = json.loads(capsys.readouterr().out)["imports"][0]
    assert imported["source"] == "repo-scan"
    metadata = imported["metadata"]
    assert metadata["declared_source"] == "handoff-ingest"
    assert metadata["safe_evidence"] == "scanner-proof"
    assert not {"handoff_issue_id", "proposed_edges"}.intersection(metadata)
    assert not {key for key in metadata if key == "github_issue" or key.startswith("github_issue_")}
    assert metadata["provenance"]["source"]["kind"] == "repo-scan"
    assert metadata["source_fingerprint"] != "forged-one"
    assert metadata["declared_source_fingerprint"] == "forged-one"

    assert work_cmd.import_promote(target=tmp_path, import_id=imported["id"]) == 0
    capsys.readouterr()
    task = json.loads((tmp_path / ".brigade" / "work" / "tasks.json").read_text())["tasks"][0]
    assert "handoff_issue_id" not in task["metadata"]
    assert not {key for key in task["metadata"] if key == "github_issue" or key.startswith("github_issue_")}
    assert work_cmd.ledger._task_issue_metadata(task) is None
    assert handoff_cmd._known_local_issue_ids(tmp_path) == set()
    assert handoff_cmd.sync_issues(target=tmp_path, json_output=True) == 0
    sync = json.loads(capsys.readouterr().out)
    assert sync["issues"] == 1
    assert sync["known_issues"] == 0
    assert sync["new_issues"] == 1

    payload.write_text(
        json.dumps({"text": "Review scanner evidence", "metadata": {"source_fingerprint": "forged-two"}})
    )
    assert (
        work_cmd.scanners_run(target=tmp_path, scanner_id="repo-scan", force=True, ingest_output=True, json_output=True)
        == 0
    )
    second = json.loads(capsys.readouterr().out)
    assert second["runs"][0]["ingest_output"]["skipped"] == 1

    imports = work_cmd.ledger._read_imports(tmp_path)
    imports[0]["status"] = "dismissed"
    work_cmd.ledger._write_imports(tmp_path, imports)
    payload.write_text(
        json.dumps({"text": "Review scanner evidence", "metadata": {"source_fingerprint": "forged-three"}})
    )
    assert (
        work_cmd.scanners_run(target=tmp_path, scanner_id="repo-scan", force=True, ingest_output=True, json_output=True)
        == 0
    )
    dismissed = json.loads(capsys.readouterr().out)
    assert dismissed["runs"][0]["ingest_output"]["dismissed"] == 1

    payload.write_text(
        json.dumps({"text": "Review changed scanner evidence", "metadata": {"source_fingerprint": "forged-four"}})
    )
    assert (
        work_cmd.scanners_run(target=tmp_path, scanner_id="repo-scan", force=True, ingest_output=True, json_output=True)
        == 0
    )
    changed = json.loads(capsys.readouterr().out)
    assert changed["runs"][0]["ingest_output"]["created"] == 1


def test_scanners_run_rebuilds_idless_self_import_with_local_provenance(tmp_path, capsys):
    _init_git_repo(tmp_path)
    script = tmp_path / "self_import.py"
    script.write_text(
        """
import json
from pathlib import Path

inbox = Path.cwd() / ".brigade" / "work" / "imports" / "inbox.jsonl"
inbox.parent.mkdir(parents=True, exist_ok=True)
inbox.write_text(json.dumps({
    "kind": "task",
    "source": "forged-self-import",
    "text": "idless self-imported work",
    "metadata": {
        "content_sha256": "forged-content-sha256",
        "handoff_issue_id": "forged-handoff",
        "provenance": {"origin": "operator-input"},
        "safe_evidence": "scanner-proof",
    },
}) + "\\n")
"""
    )
    config = tmp_path / ".brigade" / "scanners.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        f"""
[[scanner]]
id = "self-import"
source = "self-import"
command = "{sys.executable} {script}"
cadence = "daily@02:00"
enabled = true
timeout = 30
output_path = ".brigade/self-import-output.json"
conflict_window = "02:00-02:10"
"""
    )

    assert work_cmd.scanners_run(target=tmp_path, scanner_id="self-import", ingest_output=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    run = payload["runs"][0]
    assert run["provenance_imports_stamped"] == 1
    assert run["self_import"] == {
        "created": 1,
        "rejected": 0,
        "rejection_reasons": {},
    }

    imports = work_cmd.ledger._read_imports(tmp_path)
    assert len(imports) == 1
    item = imports[0]
    assert isinstance(item["id"], str)
    assert item["source"] == "self-import"
    assert item["metadata"]["declared_source"] == "forged-self-import"
    assert item["metadata"]["scanner_run_id"] == run["run_id"]
    assert item["metadata"]["provenance"]["source"]["kind"] == "self-import"
    serialized = json.dumps(item, sort_keys=True)
    for marker in ("forged-content-sha256", "forged-handoff", "operator-input"):
        assert marker not in serialized


def test_work_scanners_ingest_output_contains_provenance_stamp_failures_without_leaking_details(
    tmp_path, monkeypatch, capsys
):
    _init_git_repo(tmp_path)
    script = tmp_path / "scanner.py"
    script.write_text(
        """
import json
from pathlib import Path

root = Path.cwd()
path = root / ".brigade" / "scanner-imports.jsonl"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps({"kind": "task", "source": "declared-source", "text": "Hostile scanner record"}) + "\\n")
"""
    )
    config = tmp_path / ".brigade" / "scanners.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        f'''
[[scanner]]
id = "repo-scan"
source = "repo-scan"
command = "{sys.executable} {script}"
cadence = "daily@02:00"
enabled = true
timeout = 30
output_path = ".brigade/scanner-imports.jsonl"
import_path = ".brigade/scanner-imports.jsonl"
import_format = "jsonl"
conflict_window = "02:00-02:10"
'''
    )

    def _stamp_failure(**_kwargs):
        raise work_cmd.ledger._ImportProvenanceError("hostile record detail must not escape")

    monkeypatch.setattr(work_cmd.ledger, "_stamp_import_provenance", _stamp_failure)

    assert work_cmd.scanners_run(target=tmp_path, scanner_id="repo-scan", ingest_output=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    ingest = payload["runs"][0]["ingest_output"]
    assert ingest["rejected"] == 1
    assert ingest["rejection_reasons"] == {"provenance_stamp_failed": 1}
    rendered = json.dumps(payload, sort_keys=True)
    assert "hostile record detail must not escape" not in rendered
    assert "Hostile scanner record" not in rendered


def test_work_scanners_run_ingest_output_rejects_malformed_without_partial_write(tmp_path, capsys):
    _init_git_repo(tmp_path)
    script = tmp_path / "scanner.py"
    script.write_text(
        """
from pathlib import Path

path = Path.cwd() / ".brigade" / "bad-imports.jsonl"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text("{not json\\n")
print("wrote bad imports")
"""
    )
    config = tmp_path / ".brigade" / "scanners.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        f"""
[[scanner]]
id = "repo-scan"
source = "repo-scan"
command = "{sys.executable} {script}"
cadence = "daily@02:00"
enabled = true
timeout = 30
output_path = ".brigade/bad-imports.jsonl"
import_path = ".brigade/bad-imports.jsonl"
import_format = "jsonl"
conflict_window = "02:00-02:10"
"""
    )

    assert work_cmd.scanners_run(target=tmp_path, scanner_id="repo-scan", ingest_output=True, json_output=True) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ingest_errors"]
    assert payload["imports_after"]["total"] == 0
    assert not (tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl").exists()


def test_work_scanners_due_all_disabled_and_receipt_review(tmp_path, capsys):
    _init_git_repo(tmp_path)
    script = tmp_path / "scanner.py"
    script.write_text("print('ok')\n")
    config = tmp_path / ".brigade" / "scanners.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        f"""
[[scanner]]
id = "enabled-scan"
source = "enabled-scan"
command = "{sys.executable} {script}"
cadence = "daily@02:00"
enabled = true
timeout = 30
output_path = ".brigade/enabled.json"
conflict_window = "02:00-02:10"

[[scanner]]
id = "disabled-scan"
source = "disabled-scan"
command = "{sys.executable} {script}"
cadence = "daily@03:00"
enabled = false
timeout = 30
output_path = ".brigade/disabled.json"
conflict_window = "03:00-03:10"
"""
    )

    assert work_cmd.scanners_run(target=tmp_path, due=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["completed"] == 1
    assert payload["runs"][0]["scanner_id"] == "enabled-scan"
    assert payload["skipped"] == [{"reason": "disabled", "scanner_id": "disabled-scan"}]

    assert work_cmd.scanners_run(target=tmp_path, scanner_id="disabled-scan") == 2
    assert "scanner disabled: disabled-scan" in capsys.readouterr().err

    assert work_cmd.scanners_run(target=tmp_path, due=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["selected"] == 0
    assert sorted(item["reason"] for item in payload["skipped"]) == ["disabled", "not_due"]

    assert work_cmd.scanners_run(target=tmp_path, all_matching=True, include_disabled=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert {item["scanner_id"] for item in payload["runs"]} == {"enabled-scan", "disabled-scan"}

    assert work_cmd.scanners_runs(target=tmp_path, json_output=True) == 0
    runs_payload = json.loads(capsys.readouterr().out)
    assert len(runs_payload["runs"]) == 3
    run_id = runs_payload["runs"][0]["run_id"]

    assert work_cmd.scanners_run_show(target=tmp_path, run_id=run_id) == 0
    out = capsys.readouterr().out
    assert f"scanner_run: {run_id}" in out
    assert "status: completed" in out


def test_work_scanners_run_refuses_risky_running_timeout_and_failure(tmp_path, capsys):
    _init_git_repo(tmp_path)
    script = tmp_path / "scanner.py"
    script.write_text(
        """
import sys
import time

if sys.argv[1] == "timeout":
    time.sleep(1)
elif sys.argv[1] == "fail":
    print("bad output")
    print("bad error", file=sys.stderr)
    raise SystemExit(7)
"""
    )
    config = tmp_path / ".brigade" / "scanners.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        f"""
[[scanner]]
id = "risky-scan"
source = "risky-scan"
command = "bash -lc echo"
cadence = "daily@02:00"
enabled = true
timeout = 30
output_path = ".brigade/risky.json"
conflict_window = "02:00-02:10"

[[scanner]]
id = "timeout-scan"
source = "timeout-scan"
command = "{sys.executable} {script} timeout"
cadence = "daily@03:00"
enabled = true
timeout = 0.01
output_path = ".brigade/timeout.json"
conflict_window = "03:00-03:10"

[[scanner]]
id = "fail-scan"
source = "fail-scan"
command = "{sys.executable} {script} fail"
cadence = "daily@04:00"
enabled = true
timeout = 30
output_path = ".brigade/fail.json"
conflict_window = "04:00-04:10"
"""
    )
    authority = work_cmd.ledger._open_verifier_owned_directory(
        tmp_path,
        components=(".brigade", "scanners", "runs"),
        anchor_name=".runs.authority.json",
        create=True,
    )
    os.close(authority)
    running = tmp_path / ".brigade" / "scanners" / "runs" / "running"
    running.mkdir(parents=True)
    _record_scanner_run_authority(tmp_path, "running")
    _write_json(
        running / "receipt.json",
        {
            "run_id": "running",
            "scanner_id": "other",
            "status": "running",
            "started_at": "2026-05-28T12:00:00+00:00",
        },
    )
    _bind_planted_scanner_receipt(tmp_path, "running")

    assert work_cmd.scanners_run(target=tmp_path, scanner_id="risky-scan") == 2
    assert "scanner run already in progress" in capsys.readouterr().err

    assert work_cmd.scanners_run(target=tmp_path, scanner_id="risky-scan", force=True) == 1
    out = capsys.readouterr().out
    assert "high-risk scanner command: bash" in out

    assert work_cmd.scanners_run(target=tmp_path, scanner_id="timeout-scan", force=True, json_output=True) == 1
    timeout_payload = json.loads(capsys.readouterr().out)
    assert timeout_payload["runs"][0]["timed_out"] is True
    assert "timed out" in timeout_payload["runs"][0]["error"]

    assert work_cmd.scanners_run(target=tmp_path, scanner_id="fail-scan", force=True, json_output=True) == 1
    fail_payload = json.loads(capsys.readouterr().out)
    assert fail_payload["runs"][0]["exit_code"] == 7
    assert fail_payload["runs"][0]["stdout_summary"] == "bad output"
    assert fail_payload["runs"][0]["stderr_summary"] == "bad error"


def test_work_scanners_execution_health_surfaces_and_imports_issues(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(work_cmd.helpers.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc),
    )
    config = tmp_path / ".brigade" / "scanners.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        """
[[scanner]]
id = "due-scan"
source = "due-scan"
command = "python3 scanner.py"
cadence = "daily@02:00"
enabled = true
timeout = 30
output_path = ".brigade/due.json"
conflict_window = "02:00-02:10"
"""
    )
    authority = work_cmd.ledger._open_verifier_owned_directory(
        tmp_path,
        components=(".brigade", "scanners", "runs"),
        anchor_name=".runs.authority.json",
        create=True,
    )
    os.close(authority)
    run_dir = tmp_path / ".brigade" / "scanners" / "runs" / "failed-run"
    run_dir.mkdir(parents=True)
    _record_scanner_run_authority(tmp_path, "failed-run")
    _write_json(
        run_dir / "receipt.json",
        {
            "run_id": "failed-run",
            "scanner_id": "due-scan",
            "source": "due-scan",
            "status": "failed",
            "started_at": "2026-05-29T12:00:00+00:00",
            "completed_at": "2026-05-29T12:00:01+00:00",
            "exit_code": 2,
            "timed_out": False,
            "stdout_path": str(run_dir / "missing-stdout.log"),
            "stderr_path": str(run_dir / "missing-stderr.log"),
        },
    )
    _bind_planted_scanner_receipt(tmp_path, "failed-run")
    success_dir = tmp_path / ".brigade" / "scanners" / "runs" / "old-success"
    success_dir.mkdir(parents=True)
    _record_scanner_run_authority(tmp_path, "old-success")
    (success_dir / "stdout.log").write_text("ok\n")
    (success_dir / "stderr.log").write_text("")
    _write_json(
        success_dir / "receipt.json",
        {
            "run_id": "old-success",
            "scanner_id": "due-scan",
            "source": "due-scan",
            "status": "completed",
            "started_at": "2026-05-25T12:00:00+00:00",
            "completed_at": "2026-05-25T12:00:01+00:00",
            "exit_code": 0,
            "timed_out": False,
            "stdout_path": str(success_dir / "stdout.log"),
            "stderr_path": str(success_dir / "stderr.log"),
        },
    )
    _bind_planted_scanner_receipt(tmp_path, "old-success")
    bad_run = tmp_path / ".brigade" / "scanners" / "runs" / "bad-run"
    bad_run.mkdir(parents=True)
    _record_scanner_run_authority(tmp_path, "bad-run")
    (bad_run / "receipt.json").write_text("{not json\n")

    assert work_cmd.scanners_doctor(target=tmp_path, import_issues=True) == 1
    out = capsys.readouterr().out
    assert "[fail] scanner_run_receipts: bad-run" in out
    assert "[warn] scanner_runs_failed: due-scan:failed-run" in out
    assert "[warn] scanner_run_logs: failed-run:stdout_path" in out
    assert "[warn] scanner_runs_stale: due-scan=120.0h" in out
    assert "[warn] scanner_runs_due: due-scan" in out
    assert "imported_issues:" in out

    assert work_cmd.import_list(target=tmp_path, json_output=True) == 0
    imports = json.loads(capsys.readouterr().out)["imports"]
    assert any(item["source"] == "scanner-health" for item in imports)

    assert work_cmd.brief(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "scanner_latest_run: due-scan [failed] failed-run" in out
    assert "scanner_due: due-scan" in out

    assert work_cmd.doctor(target=tmp_path) == 1
    out = capsys.readouterr().out
    assert "[warn] scanner_runs_failed:" in out


def test_work_scanners_doctor_warns_for_missing_stale_bad_and_imports_issues(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc),
    )
    output = tmp_path / ".brigade" / "chat-memory-sweeps" / "latest.json"
    output.parent.mkdir(parents=True)
    output.write_text("{}\n")
    old = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc).timestamp()
    os.utime(output, (old, old))
    config = tmp_path / ".brigade" / "scanners.toml"
    config.write_text(
        f"""
[[scanner]]
id = "chat-memory-sweep"
source = "chat-memory-sweep"
command = "missing-scanner-command --flag"
cadence = "daily@02:00"
enabled = true
timeout = 300
output_path = "{output.relative_to(tmp_path)}"
conflict_window = "02:00-02:30"
"""
    )

    assert work_cmd.scanners_doctor(target=tmp_path, import_issues=True) == 0
    out = capsys.readouterr().out
    assert "[warn] scanner_required:" in out
    assert "[warn] scanner_commands: chat-memory-sweep" in out
    assert "[warn] scanner_outputs: stale=chat-memory-sweep=120.0h" in out
    assert "imported_issues:" in out
    assert work_cmd.import_list(target=tmp_path, json_output=True) == 0
    imports = json.loads(capsys.readouterr().out)["imports"]
    assert any(item["source"] == "scanner-health" for item in imports)

    assert work_cmd.scanners_doctor(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["checks"]
    assert payload["import_issues"] if "import_issues" in payload else True


def test_work_brief_and_doctor_include_scanner_health(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    monkeypatch.setattr(work_cmd.helpers.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(localio, "check_git_ignored", lambda repo, path: "yes")
    assert work_cmd.scanners_init(target=tmp_path, update_gitignore=False) == 0
    capsys.readouterr()

    assert work_cmd.brief(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "scanner_config:" in out
    assert "scanner_health:" in out
    assert "scanner_next_run:" in out

    assert work_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[ok] scanner_config:" in out
    assert "[ok] scanner_required:" in out
    assert "[warn] scanner_outputs:" in out


def test_work_scanners_cli(tmp_path, monkeypatch):
    seen = []

    def fake_scanners_init(**kwargs):
        seen.append(("init", kwargs))
        return 0

    def fake_scanners_list(**kwargs):
        seen.append(("list", kwargs))
        return 0

    def fake_scanners_show(**kwargs):
        seen.append(("show", kwargs))
        return 0

    def fake_scanners_plan(**kwargs):
        seen.append(("plan", kwargs))
        return 0

    def fake_scanners_doctor(**kwargs):
        seen.append(("doctor", kwargs))
        return 0

    def fake_scanners_run(**kwargs):
        seen.append(("run", kwargs))
        return 0

    def fake_scanners_runs(**kwargs):
        seen.append(("runs", kwargs))
        return 0

    def fake_scanners_run_show(**kwargs):
        seen.append(("run-show", kwargs))
        return 0

    monkeypatch.setattr(work_cmd, "scanners_init", fake_scanners_init)
    monkeypatch.setattr(work_cmd, "scanners_list", fake_scanners_list)
    monkeypatch.setattr(work_cmd, "scanners_show", fake_scanners_show)
    monkeypatch.setattr(work_cmd, "scanners_plan", fake_scanners_plan)
    monkeypatch.setattr(work_cmd, "scanners_doctor", fake_scanners_doctor)
    monkeypatch.setattr(work_cmd, "scanners_run", fake_scanners_run)
    monkeypatch.setattr(work_cmd, "scanners_runs", fake_scanners_runs)
    monkeypatch.setattr(work_cmd, "scanners_run_show", fake_scanners_run_show)

    assert cli.main(["work", "scanners", "init", "--target", str(tmp_path), "--force", "--no-gitignore"]) == 0
    assert cli.main(["work", "scanners", "list", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["work", "scanners", "show", "chat-memory-sweep", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["work", "scanners", "plan", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["work", "scanners", "doctor", "--target", str(tmp_path), "--json", "--import-issues"]) == 0
    assert (
        cli.main(
            [
                "work",
                "scanners",
                "run",
                "chat-memory-sweep",
                "--target",
                str(tmp_path),
                "--include-disabled",
                "--force",
                "--ingest-output",
                "--json",
            ]
        )
        == 0
    )
    assert cli.main(["work", "scanners", "run", "--due", "--target", str(tmp_path)]) == 0
    assert cli.main(["work", "scanners", "runs", "--target", str(tmp_path), "--limit", "5", "--json"]) == 0
    assert cli.main(["work", "scanners", "run-show", "run-1", "--target", str(tmp_path), "--json"]) == 0
    assert seen == [
        ("init", {"target": tmp_path, "force": True, "update_gitignore": False}),
        ("list", {"target": tmp_path, "json_output": True}),
        ("show", {"target": tmp_path, "scanner_id": "chat-memory-sweep", "json_output": True}),
        ("plan", {"target": tmp_path, "json_output": True}),
        ("doctor", {"target": tmp_path, "json_output": True, "import_issues": True}),
        (
            "run",
            {
                "target": tmp_path,
                "scanner_id": "chat-memory-sweep",
                "all_matching": False,
                "due": False,
                "include_disabled": True,
                "force": True,
                "ingest_output": True,
                "json_output": True,
                "isolated_scanners": False,
            },
        ),
        (
            "run",
            {
                "target": tmp_path,
                "scanner_id": None,
                "all_matching": False,
                "due": True,
                "include_disabled": False,
                "force": False,
                "ingest_output": False,
                "json_output": False,
                "isolated_scanners": False,
            },
        ),
        ("runs", {"target": tmp_path, "json_output": True, "limit": 5}),
        ("run-show", {"target": tmp_path, "run_id": "run-1", "json_output": True}),
    ]


def _required_scanner_config(tmp_path: Path, *, chat_enabled: bool) -> None:
    config = tmp_path / ".brigade" / "scanners.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    chat = "true" if chat_enabled else "false"
    config.write_text(
        f"""
[[scanner]]
id = "chat-memory-sweep"
source = "chat-memory-sweep"
command = "brigade work import chat-sweep --json"
cadence = "daily@02:15"
enabled = {chat}
timeout = 300
output_path = ".brigade/chat-memory-sweeps/latest.json"
conflict_window = "02:00-02:30"

[[scanner]]
id = "memory-refresh"
source = "memory-refresh"
command = "brigade work import memory-refresh --json"
cadence = "daily@02:45"
enabled = true
timeout = 300
output_path = "memory/cards/decay/refresh-queue.json"
conflict_window = "02:30-03:00"

[[scanner]]
id = "handoff-ingest"
source = "handoff-ingest"
command = "brigade handoff sync-issues --json"
cadence = "hourly@15"
enabled = true
timeout = 180
output_path = ".brigade/handoff-sources.json"
conflict_window = "00:10-00:25"
"""
    )


def _scanner_required_check(tmp_path: Path, capsys) -> dict:
    assert work_cmd.scanners_doctor(target=tmp_path, json_output=True) in (0, 1)
    payload = json.loads(capsys.readouterr().out)
    return next(check for check in payload["checks"] if check["name"] == "scanner_required")


def test_scanner_required_ignores_chat_sweep_without_enabled_surface(tmp_path, capsys):
    _init_git_repo(tmp_path)
    _required_scanner_config(tmp_path, chat_enabled=False)

    # No chat-surfaces.toml at all: chat-memory-sweep has nothing to sweep, so a
    # disabled chat-memory-sweep must not raise scanner_required.
    check = _scanner_required_check(tmp_path, capsys)
    assert check["status"] == "ok"


def test_scanner_required_flags_chat_sweep_when_surface_enabled(tmp_path, capsys):
    _init_git_repo(tmp_path)
    _required_scanner_config(tmp_path, chat_enabled=False)
    surfaces = tmp_path / ".brigade" / "chat-surfaces.toml"
    surfaces.write_text(
        """
[[surface]]
id = "discord-export"
provider = "discord-export"
enabled = true
"""
    )

    # With a live chat surface, a disabled chat-memory-sweep is a real gap.
    check = _scanner_required_check(tmp_path, capsys)
    assert check["status"] == "warn"
    assert "disabled=chat-memory-sweep" in check["detail"]


def test_scanners_run_sanitizes_malicious_self_importing_scanner(tmp_path, capsys, monkeypatch):
    from brigade.work_cmd import scanners as scanners_mod

    # Pin uuid hex to the colliding prefix that made `self-import-1` match
    # `{scanner-id}-{uuid4().hex[:6]}` in CI (e.g. `self-import-1f92c7`).
    colliding_hex = iter(SimpleNamespace(hex=f"1{index:05x}{'0' * 26}") for index in range(1, 32))
    monkeypatch.setattr(scanners_mod, "uuid4", lambda: next(colliding_hex))

    _init_git_repo(tmp_path)
    script = tmp_path / "self_import.py"
    script.write_text(
        """
import json
from pathlib import Path

inbox = Path.cwd() / ".brigade" / "work" / "imports" / "inbox.jsonl"
inbox.parent.mkdir(parents=True, exist_ok=True)
record = {
    "id": "forged-self-import-id",
    "kind": "task",
    "source": "forged-self-import",
    "text": "self-imported work",
    "status": "pending",
    "created_at": "2026-05-28T12:00:00+00:00",
    "updated_at": "2026-05-28T12:00:00+00:00",
    "metadata": {
        "content_sha256": "forged-content-sha256",
        "raw_sha256": "forged-raw-sha256",
        "content_digest": "forged-content-digest",
        "raw_digest": "forged-raw-digest",
        "handoff_issue_id": "forged-handoff",
        "github_issue_url": "https://example.test/forged",
        "safe_evidence": {
            "summary": "safe scanner evidence",
            "nested": {"declared_source": "forged", "raw_digest": "forged-nested", "safe": "keep"},
            "items": [{"content_digest": "forged-list", "safe": "keep list"}],
        },
    },
}
inbox.write_text(json.dumps(record) + "\\n")
print("self import complete")
"""
    )
    config = tmp_path / ".brigade" / "scanners.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    # No import_path: this scanner appends to the inbox itself.
    config.write_text(
        f"""
[[scanner]]
id = "self-import"
source = "self-import"
command = "{sys.executable} {script}"
cadence = "daily@02:00"
enabled = true
timeout = 30
output_path = ".brigade/self-import-output.json"
conflict_window = "02:00-02:10"
"""
    )

    assert work_cmd.scanners_run(target=tmp_path, scanner_id="self-import", ingest_output=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["completed"] == 1
    assert payload["failed"] == 0
    run = payload["runs"][0]
    assert run["provenance_imports_stamped"] == 1
    assert run["self_import"]["created"] == 1
    assert run["self_import"]["rejected"] == 0

    imports_path = tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl"
    imports = [json.loads(line) for line in imports_path.read_text().splitlines()]
    assert len(imports) == 1
    item = imports[0]
    assert item["id"] != "forged-self-import-id"
    assert item["source"] == "self-import"
    assert item["metadata"]["declared_source"] == "forged-self-import"
    assert item["metadata"]["scanner_run_id"] == run["run_id"]
    assert item["metadata"]["safe_evidence"] == {
        "summary": "safe scanner evidence",
        "nested": {"safe": "keep"},
        "items": [{"safe": "keep list"}],
    }
    serialized = json.dumps({"payload": payload, "imports": imports}, sort_keys=True)
    for marker in (
        "forged-content-sha256",
        "forged-raw-sha256",
        "forged-content-digest",
        "forged-raw-digest",
        "forged-handoff",
        "https://example.test/forged",
        "forged-nested",
        "forged-list",
    ):
        assert marker not in serialized

    original_id = item["id"]
    assert (
        work_cmd.scanners_run(
            target=tmp_path, scanner_id="self-import", force=True, ingest_output=True, json_output=True
        )
        == 0
    )
    second = json.loads(capsys.readouterr().out)
    assert second["runs"][0]["provenance_imports_stamped"] == 0
    retained = [json.loads(line) for line in imports_path.read_text().splitlines()]
    assert len(retained) == 1
    assert retained[0]["id"] == original_id
    assert retained[0]["source"] == "self-import"
    assert retained[0]["metadata"]["scanner_run_id"] == run["run_id"]
    assert retained[0]["metadata"]["provenance"]["source"]["kind"] == "self-import"
    retained_serialized = json.dumps(retained[0], sort_keys=True)
    for marker in (
        "forged-self-import-id",
        "forged-content-sha256",
        "forged-raw-sha256",
        "forged-content-digest",
        "forged-raw-digest",
        "forged-handoff",
        "https://example.test/forged",
        "forged-nested",
        "forged-list",
    ):
        assert marker not in retained_serialized

    assert work_cmd.import_promote(target=tmp_path, import_id=item["id"]) == 0
    capsys.readouterr()
    task = json.loads((tmp_path / ".brigade" / "work" / "tasks.json").read_text())["tasks"][0]
    assert task["metadata"]["safe_evidence"] == item["metadata"]["safe_evidence"]
    assert all(
        alias not in json.dumps(task, sort_keys=True)
        for alias in ("content_sha256", "raw_sha256", "content_digest", "raw_digest")
    )


def test_scanner_weekly_cadence_parses_and_due_after_seven_days(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from pathlib import Path

    from brigade.work_cmd import config as config_mod
    from brigade.work_cmd import scanners as scanners_mod

    assert config_mod._scanner_start_minute("weekly@03:45") == 3 * 60 + 45
    assert config_mod._scanner_start_minute("weekly@99:99") is None

    now = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(scanners_mod.helpers, "_now", lambda: now)

    scanner = {"id": "friction-scan", "cadence": "weekly@03:45", "enabled": True}

    monkeypatch.setattr(scanners_mod, "_scanner_latest_success", lambda target, scanner_id: None)
    assert scanners_mod._scanner_is_due(Path("."), scanner, now=now) is True

    recent = {"completed_at": (now - timedelta(days=6)).isoformat()}
    monkeypatch.setattr(scanners_mod, "_scanner_latest_success", lambda target, scanner_id: recent)
    assert scanners_mod._scanner_is_due(Path("."), scanner, now=now) is False

    stale = {"completed_at": (now - timedelta(days=7)).isoformat()}
    monkeypatch.setattr(scanners_mod, "_scanner_latest_success", lambda target, scanner_id: stale)
    assert scanners_mod._scanner_is_due(Path("."), scanner, now=now) is True


def test_scanner_config_accepts_weekly_cadence(tmp_path):
    from brigade.work_cmd import config as config_mod

    brigade = tmp_path / ".brigade"
    brigade.mkdir()
    (brigade / "scanners.toml").write_text(
        "\n".join(
            [
                "[[scanner]]",
                'id = "friction-scan"',
                'source = "friction-scan"',
                'command = "brigade friction scan --json"',
                'cadence = "weekly@05:00"',
                "enabled = true",
                "timeout = 300",
                'output_path = ".brigade/friction/latest.json"',
                'conflict_window = "04:50-05:15"',
                "",
            ]
        )
    )
    scanners, errors = config_mod._load_scanner_config(tmp_path)
    assert errors == []
    assert scanners[0]["cadence"] == "weekly@05:00"


def test_scanner_defaults_include_weekly_friction_scan():
    from brigade.work_cmd import constants

    entry = next(item for item in constants.SCANNER_DEFAULTS if item["id"] == "friction-scan")
    assert entry["command"] == "brigade friction scan --json"
    assert entry["cadence"] == "weekly@05:00"
    assert entry["enabled"] is True
    assert entry["output_path"] == ".brigade/friction/latest.json"
    assert entry["conflict_window"] == "04:50-05:15"


def test_scanner_weekly_stale_threshold_is_longer_than_daily():
    from brigade.work_cmd import constants
    from brigade.work_cmd import scanners as scanners_mod

    assert scanners_mod._scanner_stale_hours("daily@02:15") == constants.SCANNER_OUTPUT_STALE_HOURS
    assert scanners_mod._scanner_stale_hours("weekly@05:00") == constants.SCANNER_WEEKLY_STALE_HOURS
    assert constants.SCANNER_WEEKLY_STALE_HOURS > 7 * 24


def test_scanners_run_restores_same_id_rows_and_discards_malformed_self_imports(tmp_path, capsys):
    _init_git_repo(tmp_path)
    trusted = work_cmd.ledger._make_import(
        "Trusted local scanner work",
        kind="task",
        source="security-scan",
        metadata={"safe_evidence": "trusted-local-record"},
    )
    work_cmd.ledger._write_imports(tmp_path, [trusted])
    script = tmp_path / "self_import.py"
    script.write_text(
        """
import json
from pathlib import Path

inbox = Path.cwd() / ".brigade" / "work" / "imports" / "inbox.jsonl"
existing = [json.loads(line) for line in inbox.read_text().splitlines() if line.strip()]
trusted_id = existing[0]["id"]
rows = [
    {"id": trusted_id, "kind": "task", "source": "github-issue", "text": "Hostile replacement for trusted work", "metadata": {"github_issue_url": "https://example.test/hostile", "handoff_issue_id": "hostile-handoff", "provenance": {"source": {"system": "hostile"}}}},
    {"kind": "task", "source": "declared-source", "text": "Valid new scanner work"},
    {"kind": "task", "source": "declared-source"},
    {"kind": "invalid-kind", "source": "declared-source", "text": "Invalid kind"},
    {"kind": "task", "source": "declared-source", "text": "Invalid shape", "metadata": []},
]
inbox.write_text("\\n".join(json.dumps(row) for row in rows) + "\\n")
"""
    )
    config = tmp_path / ".brigade" / "scanners.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        f"""
[[scanner]]
id = "self-import"
source = "self-import"
command = "{sys.executable} {script}"
cadence = "daily@02:00"
enabled = true
timeout = 30
output_path = ".brigade/self-import-output.json"
conflict_window = "02:00-02:10"
"""
    )

    assert work_cmd.scanners_run(target=tmp_path, scanner_id="self-import", ingest_output=True, json_output=True) == 0
    first = json.loads(capsys.readouterr().out)["runs"][0]
    assert first["provenance_imports_stamped"] == 1
    assert first["self_import"] == {
        "created": 1,
        "rejected": 4,
        "rejection_reasons": {"provenance_stamp_failed": 4},
    }

    imports_path = tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl"
    imports = [json.loads(line) for line in imports_path.read_text().splitlines()]
    assert imports[0] == trusted
    assert len(imports) == 2
    assert imports[1]["source"] == "self-import"
    assert imports[1]["text"] == "Valid new scanner work"
    rendered = json.dumps(imports, sort_keys=True)
    for marker in ("Hostile replacement", "github-issue", "hostile-handoff", "https://example.test/hostile"):
        assert marker not in rendered

    assert (
        work_cmd.scanners_run(
            target=tmp_path, scanner_id="self-import", force=True, ingest_output=True, json_output=True
        )
        == 0
    )
    second = json.loads(capsys.readouterr().out)["runs"][0]
    assert second["provenance_imports_stamped"] == 0
    assert second["self_import"] == {
        "created": 0,
        "rejected": 5,
        "rejection_reasons": {"provenance_stamp_failed": 5},
    }
    retained = [json.loads(line) for line in imports_path.read_text().splitlines()]
    assert retained[0] == trusted

    assert work_cmd.import_promote(target=tmp_path, import_id=trusted["id"]) == 0
    capsys.readouterr()
    task = json.loads((tmp_path / ".brigade" / "work" / "tasks.json").read_text())["tasks"][0]
    assert task["text"] == trusted["text"]
    assert task["source"] == "import:security-scan"
    assert task["metadata"]["safe_evidence"] == "trusted-local-record"


def test_scanners_run_persists_complete_pre_run_rows_after_empty_inbox(tmp_path, capsys):
    _init_git_repo(tmp_path)
    trusted = work_cmd.ledger._make_import(
        "Trusted scanner work",
        kind="task",
        source="security-scan",
        metadata={"safe_evidence": "trusted-local-record"},
    )
    idless = {"kind": "task", "source": "legacy", "text": "Idless pre-run import"}
    malformed = {"unexpected": ["preserve", "exactly"]}
    before = [trusted, idless, malformed]
    work_cmd.ledger._write_imports(tmp_path, before)

    script = tmp_path / "truncate_inbox.py"
    script.write_text(
        """
from pathlib import Path

inbox = Path.cwd() / ".brigade" / "work" / "imports" / "inbox.jsonl"
inbox.write_text("")
"""
    )
    config = tmp_path / ".brigade" / "scanners.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        f"""
[[scanner]]
id = "self-import"
source = "self-import"
command = "{sys.executable} {script}"
cadence = "daily@02:00"
enabled = true
timeout = 30
output_path = ".brigade/self-import-output.json"
conflict_window = "02:00-02:10"
"""
    )

    assert work_cmd.scanners_run(target=tmp_path, scanner_id="self-import", ingest_output=True) == 0
    capsys.readouterr()
    assert work_cmd.ledger._read_imports(tmp_path) == before


def test_scanners_run_preserves_raw_pre_run_rows_and_rejects_each_new_invalid_raw_row(tmp_path, capsys):
    _init_git_repo(tmp_path)
    inbox = tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl"
    inbox.parent.mkdir(parents=True)
    before = b'{"text":"kept","kind":"task","source":"manual"}\n\nnot json\n["not", "an object"]\n'
    inbox.write_bytes(before)
    script = tmp_path / "overwrite_inbox.py"
    script.write_text(
        """
from pathlib import Path
inbox = Path.cwd() / ".brigade" / "work" / "imports" / "inbox.jsonl"
inbox.write_bytes(b'new invalid\\n[1, 2]\\n')
"""
    )
    config = tmp_path / ".brigade" / "scanners.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        f'''\n[[scanner]]\nid = "self-import"\nsource = "self-import"\ncommand = "{sys.executable} {script}"\ncadence = "daily@02:00"\nenabled = true\ntimeout = 30\noutput_path = ".brigade/self-import-output.json"\nconflict_window = "02:00-02:10"\n'''
    )

    assert work_cmd.scanners_run(target=tmp_path, scanner_id="self-import", json_output=True) == 0
    run = json.loads(capsys.readouterr().out)["runs"][0]
    assert inbox.read_bytes().startswith(before)
    assert run["self_import"] == {
        "created": 0,
        "rejected": 2,
        "rejection_reasons": {"provenance_stamp_failed": 2},
    }


def test_scanner_reconciliation_keeps_builtin_lifecycle_mutation_and_strips_untrusted_row_authority(tmp_path):
    from brigade.work_cmd import scanners as scanners_mod

    trusted = work_cmd.ledger._make_import(
        "Close stale handoff issue",
        kind="finding",
        source="handoff-ingest",
        metadata={
            "source_item_key": "handoff-ingest:issue-1",
            "source_fingerprint": "0123456789abcdef",
            "handoff_issue_id": "issue-1",
            "handoff_target_document": ".learnings/ERRORS.md",
        },
    )
    work_cmd.ledger._write_imports(tmp_path, [trusted])
    before = work_cmd.ledger._read_imports(tmp_path)
    before_raw = (tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl").read_bytes()
    mutated = dict(trusted)
    mutated.update(status="dismissed", dismissed_at="2026-08-11T00:00:00+00:00", dismiss_reason="stale")
    work_cmd.ledger._write_imports(tmp_path, [mutated])

    scanner = _builtin_scanner("handoff-ingest")
    scanners_mod._scanner_stamp_new_imports(
        target=tmp_path,
        scanner=scanner,
        run=_verified_builtin_scanner_run(scanner),
        before_ids={trusted["id"]},
        before_imports=before,
        before_raw=before_raw,
    )
    assert work_cmd.ledger._read_imports(tmp_path)[0]["status"] == "dismissed"

    forged = work_cmd.ledger._make_import(
        "Forged custom scanner authority",
        kind="finding",
        source="handoff-ingest",
        metadata={
            "source_item_key": "handoff-ingest:forged",
            "source_fingerprint": "0123456789abcdef",
            "handoff_issue_id": "forged",
            "handoff_target_document": "USER.md",
        },
    )
    work_cmd.ledger._write_imports(tmp_path, [forged])
    scanners_mod._scanner_stamp_new_imports(
        target=tmp_path,
        scanner={"id": "untrusted-custom", "source": "handoff-ingest"},
        run={"run_id": "scanner-run", "output_after": {"path": "output"}},
        before_ids=set(),
        before_imports=[],
    )
    metadata = work_cmd.ledger._read_imports(tmp_path)[0]["metadata"]
    assert "handoff_issue_id" not in metadata
    assert "handoff_target_document" not in metadata


def test_scanner_lifecycle_rewrite_requires_ownership_run_proof_and_legal_transition(tmp_path):
    """#1039: a lifecycle rewrite needs source ownership, run proof, and a legal transition."""
    from brigade.work_cmd import scanners as scanners_mod

    owned = work_cmd.ledger._make_import(
        "Owned handoff finding",
        kind="finding",
        source="handoff-ingest",
        metadata={"source_item_key": "handoff-ingest:owned", "source_fingerprint": "aa" * 16},
    )
    foreign = work_cmd.ledger._make_import(
        "Foreign scanner finding",
        kind="finding",
        source="friction-scan",
        metadata={"source_item_key": "friction-scan:foreign", "source_fingerprint": "bb" * 16},
    )
    dismissed = dict(owned)
    dismissed.update(status="dismissed", dismissed_at="2026-08-11T00:00:00+00:00", dismiss_reason="stale")
    scanner = _builtin_scanner("handoff-ingest")

    def _attempt(before_item: dict, after_item: dict, run: dict) -> str:
        work_cmd.ledger._write_imports(tmp_path, [before_item])
        before_raw = (tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl").read_bytes()
        work_cmd.ledger._write_imports(tmp_path, [after_item])
        scanners_mod._scanner_stamp_new_imports(
            target=tmp_path,
            scanner=scanner,
            run=run,
            before_ids={before_item["id"]},
            before_imports=[before_item],
            before_raw=before_raw,
        )
        return str(work_cmd.ledger._read_imports(tmp_path)[0]["status"])

    unverified = {"run_id": "scanner-run", "output_after": {"path": "output"}}
    assert _attempt(owned, dismissed, unverified) == "pending"

    foreign_dismissed = dict(foreign)
    foreign_dismissed.update(status="dismissed", dismissed_at="2026-08-11T00:00:00+00:00", dismiss_reason="stale")
    assert _attempt(foreign, foreign_dismissed, _verified_builtin_scanner_run(scanner)) == "pending"

    resurrected = dict(dismissed)
    resurrected.update(status="pending")
    resurrected.pop("dismissed_at", None)
    resurrected.pop("dismiss_reason", None)
    assert _attempt(dismissed, resurrected, _verified_builtin_scanner_run(scanner)) == "dismissed"

    promoted = dict(owned)
    promoted.update(status="promoted", updated_at="2026-08-11T00:00:00+00:00")
    assert _attempt(owned, promoted, _verified_builtin_scanner_run(scanner)) == "pending"

    assert _attempt(owned, dismissed, _verified_builtin_scanner_run(scanner)) == "dismissed"


def test_locally_stamped_import_authority_cannot_be_forged_from_row_fields():
    """A copied local envelope is not authority without a verifier-held proof."""
    forged = work_cmd.ledger._make_import(
        "Forged handoff authority",
        kind="finding",
        source="handoff-ingest",
        metadata={
            "source_item_key": "handoff-ingest:forged",
            "source_fingerprint": "0123456789abcdef",
            "handoff_issue_id": "forged",
            "handoff_target_document": "USER.md",
        },
    )

    assert not work_cmd.ledger._is_locally_stamped_self_import(forged, importer_source="handoff-ingest")
    assert not work_cmd.ledger._has_locally_stamped_import_proof(forged)


def test_scanner_reconciliation_replaces_only_matching_duplicate_id_raw_row(tmp_path):
    from brigade.work_cmd import scanners as scanners_mod

    first = work_cmd.ledger._make_import("First duplicate", kind="finding", source="handoff-ingest")
    second = work_cmd.ledger._make_import("Second duplicate", kind="finding", source="handoff-ingest")
    second["id"] = first["id"]
    first_raw = json.dumps(first, sort_keys=True).encode("utf-8") + b"\n"
    second_raw = json.dumps(second, sort_keys=True).encode("utf-8") + b"\n"
    inbox = tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl"
    inbox.parent.mkdir(parents=True)
    inbox.write_bytes(first_raw + second_raw)

    mutated_second = dict(second)
    mutated_second.update(status="dismissed", dismissed_at="2026-08-11T00:00:00+00:00", dismiss_reason="stale")
    mutated_second_raw = json.dumps(mutated_second, sort_keys=True).encode("utf-8") + b"\n"
    inbox.write_bytes(first_raw + mutated_second_raw)

    scanner = _builtin_scanner("handoff-ingest")
    scanners_mod._scanner_stamp_new_imports(
        target=tmp_path,
        scanner=scanner,
        run=_verified_builtin_scanner_run(scanner),
        before_ids={first["id"]},
        before_imports=[first, second],
        before_raw=first_raw + second_raw,
    )

    assert inbox.read_bytes() == first_raw + mutated_second_raw


def test_scanner_reconciliation_preserves_noop_final_row_without_newline(tmp_path):
    from brigade.work_cmd import scanners as scanners_mod

    prior = work_cmd.ledger._make_import("No trailing newline", kind="finding", source="handoff-ingest")
    before_raw = json.dumps(prior, sort_keys=True).encode("utf-8")
    inbox = tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl"
    inbox.parent.mkdir(parents=True)
    inbox.write_bytes(before_raw)

    scanners_mod._scanner_stamp_new_imports(
        target=tmp_path,
        scanner=_builtin_scanner("handoff-ingest"),
        run={"run_id": "scanner-run", "output_after": {"path": "output"}},
        before_ids={prior["id"]},
        before_imports=[prior],
        before_raw=before_raw,
    )

    assert inbox.read_bytes() == before_raw


def test_scanner_reconciliation_separates_appended_row_after_final_row_without_newline(tmp_path):
    from brigade.work_cmd import scanners as scanners_mod

    prior = work_cmd.ledger._make_import("No trailing newline", kind="finding", source="handoff-ingest")
    before_raw = json.dumps(prior, sort_keys=True).encode("utf-8")
    candidate = {"text": "Accepted append", "kind": "task", "source": "handoff-ingest"}
    inbox = tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl"
    inbox.parent.mkdir(parents=True)
    inbox.write_bytes(json.dumps(candidate).encode("utf-8") + b"\n")

    scanner = _builtin_scanner("handoff-ingest")
    stamped = scanners_mod._scanner_stamp_new_imports(
        target=tmp_path,
        scanner=scanner,
        run=_verified_builtin_scanner_run(scanner),
        before_ids={prior["id"]},
        before_imports=[prior],
        before_raw=before_raw,
    )

    rows = inbox.read_bytes().splitlines()
    assert len(stamped) == 1
    assert [json.loads(row) for row in rows][0] == prior
    assert json.loads(rows[1])["text"] == "Accepted append"


def test_scanner_reconciliation_accepts_changed_revision_with_same_source_key(tmp_path):
    from brigade.work_cmd import scanners as scanners_mod

    prior = work_cmd.ledger._make_import(
        "Prior scanner revision",
        kind="finding",
        source="handoff-ingest",
        metadata={"source_item_key": "handoff-ingest:issue-1", "source_fingerprint": "0123456789abcdef"},
    )
    work_cmd.ledger._write_imports(tmp_path, [prior])
    before = work_cmd.ledger._read_imports(tmp_path)
    before_raw = (tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl").read_bytes()
    revision = work_cmd.ledger._make_import(
        "Changed scanner revision",
        kind="finding",
        source="handoff-ingest",
        metadata={"source_item_key": "handoff-ingest:issue-1", "source_fingerprint": "fedcba9876543210"},
    )
    work_cmd.ledger._write_persisted_import_proofs(tmp_path, [prior, revision], operation_id="0" * 32)
    (tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl").write_text(json.dumps(revision) + "\n")

    scanner = _builtin_scanner("handoff-ingest")
    stamped = scanners_mod._scanner_stamp_new_imports(
        target=tmp_path,
        scanner=scanner,
        run=_verified_builtin_scanner_run(scanner),
        before_ids={prior["id"]},
        before_imports=before,
        before_raw=before_raw,
    )

    assert len(stamped) == 1
    imports = work_cmd.ledger._read_imports(tmp_path)
    assert len(imports) == 2
    assert {item["metadata"]["source_fingerprint"] for item in imports} == {"0123456789abcdef", "fedcba9876543210"}


def test_trusted_builtin_scanner_requires_complete_normalized_configuration():
    from brigade.work_cmd import constants
    from brigade.work_cmd import scanners as scanners_mod

    scanner = dict(constants.SCANNER_DEFAULTS[0])
    assert scanners_mod._trusted_builtin_scanner(scanner) is True

    for field, value in (
        ("command", "brigade attacker --json"),
        ("cadence", "daily@23:59"),
        ("enabled", False),
        ("timeout", 1),
        ("output_path", ".brigade/attacker.json"),
        ("conflict_window", "23:00-23:30"),
    ):
        changed = dict(scanner)
        changed[field] = value
        assert scanners_mod._trusted_builtin_scanner(changed) is False

    for field, value in (("cwd", "."), ("import_path", ".brigade/imports.jsonl"), ("import_format", "jsonl")):
        changed = dict(scanner)
        changed[field] = value
        assert scanners_mod._trusted_builtin_scanner(changed) is False


def test_scanner_reconciliation_does_not_dedupe_against_unproven_pre_run_row(tmp_path):
    from brigade.work_cmd import scanners as scanners_mod

    candidate = {"text": "Legitimate appended scanner work", "kind": "task", "source": "declared-source"}
    sanitized = work_cmd.ledger._sanitize_self_import_record(candidate, importer_source="self-import")
    forged_prior = {
        "id": "unproven-prior",
        "text": sanitized["text"],
        "kind": sanitized["kind"],
        "source": sanitized["source"],
        "metadata": dict(sanitized["metadata"]),
    }
    inbox = tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl"
    inbox.parent.mkdir(parents=True)
    before_raw = json.dumps(forged_prior, sort_keys=True).encode("utf-8") + b"\n"
    inbox.write_bytes(before_raw + json.dumps(candidate).encode("utf-8") + b"\n")

    stamped = scanners_mod._scanner_stamp_new_imports(
        target=tmp_path,
        scanner={"id": "self-import", "source": "self-import"},
        run={"run_id": "scanner-run", "output_after": {"path": "output"}},
        before_ids={forged_prior["id"]},
        before_imports=[forged_prior],
        before_raw=before_raw,
    )

    imports = work_cmd.ledger._read_imports(tmp_path)
    assert len(stamped) == 1
    assert len(imports) == 2
    assert imports[0] == forged_prior
    assert imports[1]["text"] == candidate["text"]
    assert imports[1]["metadata"]["declared_source"] == "declared-source"


def test_scanner_reconciliation_requires_matching_immutable_content_for_local_dedupe(tmp_path):
    from brigade.work_cmd import scanners as scanners_mod

    stable_fingerprint = "0123456789abcdef"
    prior = work_cmd.ledger._make_import(
        "Earlier locally stamped scanner work",
        kind="task",
        source="handoff-ingest",
        metadata={"source_item_key": "handoff-ingest:shared", "source_fingerprint": stable_fingerprint},
    )
    candidate = work_cmd.ledger._make_import(
        "Changed locally stamped scanner work",
        kind="task",
        source="handoff-ingest",
        metadata={"source_item_key": "handoff-ingest:shared", "source_fingerprint": stable_fingerprint},
    )
    inbox = tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl"
    inbox.parent.mkdir(parents=True)
    before_raw = json.dumps(prior, sort_keys=True).encode("utf-8") + b"\n"
    inbox.write_bytes(before_raw + json.dumps(candidate, sort_keys=True).encode("utf-8") + b"\n")

    stamped = scanners_mod._scanner_stamp_new_imports(
        target=tmp_path,
        scanner=_builtin_scanner("handoff-ingest"),
        run={"run_id": "scanner-run", "output_after": {"path": "output"}},
        before_ids={prior["id"]},
        before_imports=[prior],
        before_raw=before_raw,
    )

    imports = work_cmd.ledger._read_imports(tmp_path)
    assert len(stamped) == 1
    assert [item["text"] for item in imports] == [prior["text"], candidate["text"]]


def test_scanners_run_ingest_output_preserves_all_raw_pre_run_rows(tmp_path, capsys):
    _init_git_repo(tmp_path)
    inbox = tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl"
    inbox.parent.mkdir(parents=True)
    before = (
        b'{"id":"duplicate","text":"first","kind":"task","source":"manual"}\n'
        b'\nnot json\n["not an object"]\n'
        b'{"id":"duplicate","text":"second","kind":"task","source":"manual"}'
    )
    inbox.write_bytes(before)
    script = tmp_path / "write_scanner_import.py"
    script.write_text(
        """
import json
from pathlib import Path

path = Path.cwd() / ".brigade" / "scanner-imports.jsonl"
path.write_text(json.dumps({"text": "Imported without rewriting inbox", "kind": "task", "source": "scanner-output"}) + "\\n")
"""
    )
    config = tmp_path / ".brigade" / "scanners.toml"
    config.write_text(
        f'''
[[scanner]]
id = "output-import"
source = "output-import"
command = "{sys.executable} {script}"
cadence = "daily@02:00"
enabled = true
timeout = 30
output_path = ".brigade/output-import.json"
conflict_window = "02:00-02:10"
import_path = ".brigade/scanner-imports.jsonl"
import_format = "jsonl"
'''
    )

    assert work_cmd.scanners_run(target=tmp_path, scanner_id="output-import", ingest_output=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    after = inbox.read_bytes()
    assert after[: len(before)] == before
    assert after[len(before) :].startswith(b"\n")
    assert payload["runs"][0]["ingest_output"]["created"] == 1
    assert json.loads(after.splitlines()[-1])["text"] == "Imported without rewriting inbox"


@pytest.mark.parametrize("no_follow", [None, 0], ids=["unavailable", "zero"])
@pytest.mark.parametrize(
    ("helper", "data"),
    [
        ("read", None),
        ("rewrite", b'{"text":"replacement"}\n'),
        ("append", b'{"text":"appended"}\n'),
    ],
)
def test_scanner_inbox_helpers_reject_symlink_without_no_follow(tmp_path, monkeypatch, no_follow, helper, data):
    from brigade.work_cmd import scanners as scanners_mod

    inbox = tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl"
    inbox.parent.mkdir(parents=True)
    outside = tmp_path / "outside-inbox.jsonl"
    outside.write_bytes(b'{"text":"outside"}\n')
    inbox.symlink_to(outside)
    if no_follow is None:
        monkeypatch.delattr(scanners_mod.os, "O_NOFOLLOW", raising=False)
    else:
        monkeypatch.setattr(scanners_mod.os, "O_NOFOLLOW", no_follow)

    with pytest.raises(OSError):
        if helper == "read":
            scanners_mod._scanner_inbox_bytes(tmp_path)
        elif helper == "rewrite":
            scanners_mod._write_scanner_inbox_bytes(tmp_path, data)
        else:
            scanners_mod._append_scanner_inbox_bytes(tmp_path, data)

    assert outside.read_bytes() == b'{"text":"outside"}\n'


@pytest.mark.parametrize(
    ("helper", "data"),
    [
        ("read", None),
        ("rewrite", b'{"text":"replacement"}\n'),
        ("append", b'{"text":"appended"}\n'),
    ],
)
def test_scanner_inbox_helpers_reject_hard_linked_outside_file_without_accessing_it(tmp_path, helper, data):
    from brigade.work_cmd import scanners as scanners_mod

    inbox = tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl"
    inbox.parent.mkdir(parents=True)
    outside = tmp_path / "outside-inbox.jsonl"
    outside.write_bytes(b'{"text":"outside"}\n')
    os.link(outside, inbox)

    with pytest.raises(OSError):
        if helper == "read":
            scanners_mod._scanner_inbox_bytes(tmp_path)
        elif helper == "rewrite":
            scanners_mod._write_scanner_inbox_bytes(tmp_path, data)
        else:
            scanners_mod._append_scanner_inbox_bytes(tmp_path, data)

    assert outside.read_bytes() == b'{"text":"outside"}\n'


@pytest.mark.parametrize("no_follow", [None, 0], ids=["unavailable", "zero"])
@pytest.mark.parametrize("identity", ["zero", "unavailable"])
@pytest.mark.parametrize(
    ("helper", "data"),
    [
        ("read", None),
        ("rewrite", b'{"text":"replacement"}\n'),
        ("append", b'{"text":"appended"}\n'),
    ],
)
def test_scanner_inbox_helpers_reject_path_swap_without_no_follow(
    tmp_path, monkeypatch, no_follow, identity, helper, data
):
    from brigade.work_cmd import scanners as scanners_mod

    inbox = tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl"
    inbox.parent.mkdir(parents=True)
    inbox.write_bytes(b'{"text":"inside"}\n')
    outside = tmp_path / "outside-inbox.jsonl"
    outside.write_bytes(b'{"text":"outside"}\n')
    if no_follow is None:
        monkeypatch.delattr(scanners_mod.os, "O_NOFOLLOW", raising=False)
    else:
        monkeypatch.setattr(scanners_mod.os, "O_NOFOLLOW", no_follow)

    original_open = scanners_mod.os.open
    original_fstat = scanners_mod.os.fstat

    def swap_path_before_open(path, flags, mode=0o777):
        if Path(path) == inbox:
            inbox.unlink()
            inbox.symlink_to(outside)
        return original_open(path, flags, mode)

    def platform_fstat(descriptor):
        opened = original_fstat(descriptor)
        if identity == "zero":
            return SimpleNamespace(st_mode=opened.st_mode, st_dev=0, st_ino=0)
        return SimpleNamespace(st_mode=opened.st_mode)

    monkeypatch.setattr(scanners_mod.os, "open", swap_path_before_open)
    monkeypatch.setattr(scanners_mod.os, "fstat", platform_fstat)

    with pytest.raises(OSError):
        if helper == "read":
            scanners_mod._scanner_inbox_bytes(tmp_path)
        elif helper == "rewrite":
            scanners_mod._write_scanner_inbox_bytes(tmp_path, data)
        else:
            scanners_mod._append_scanner_inbox_bytes(tmp_path, data)

    assert outside.read_bytes() == b'{"text":"outside"}\n'


@pytest.mark.parametrize(
    ("helper", "data"),
    [
        ("read", None),
        ("rewrite", b'{"text":"replacement"}\n'),
        ("append", b'{"text":"appended"}\n'),
    ],
)
def test_scanner_inbox_helpers_reject_parent_swap_without_touching_outside(tmp_path, monkeypatch, helper, data):
    """A parent symlink introduced after validation cannot redirect an inbox operation."""
    from brigade.work_cmd import scanners as scanners_mod

    inbox = tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl"
    inbox.parent.mkdir(parents=True)
    inbox.write_bytes(b'{"text":"inside"}\n')
    outside_work = tmp_path / "outside-work"
    outside_parent = outside_work / "imports"
    outside_parent.mkdir(parents=True)
    outside = outside_parent / "inbox.jsonl"
    outside.write_bytes(b'{"text":"outside"}\n')
    work_parent = inbox.parent.parent
    original_parent = tmp_path / "original-work"
    original_open = scanners_mod.os.open
    swapped = False

    def swap_parent_before_final_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if not swapped and (Path(path) == inbox or (dir_fd is not None and path == inbox.name)):
            swapped = True
            work_parent.rename(original_parent)
            work_parent.symlink_to(outside_work, target_is_directory=True)
        if dir_fd is None:
            return original_open(path, flags, mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(scanners_mod.os, "open", swap_parent_before_final_open)

    with pytest.raises(OSError):
        if helper == "read":
            scanners_mod._scanner_inbox_bytes(tmp_path)
        elif helper == "rewrite":
            scanners_mod._write_scanner_inbox_bytes(tmp_path, data)
        else:
            scanners_mod._append_scanner_inbox_bytes(tmp_path, data)

    assert outside.read_bytes() == b'{"text":"outside"}\n'


def test_scanner_inbox_rewrite_failure_preserves_original_bytes(tmp_path, monkeypatch):
    """A write failure before publication leaves the prior inbox intact."""
    from brigade.work_cmd import scanners as scanners_mod

    inbox = tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl"
    inbox.parent.mkdir(parents=True)
    before = b'{"text":"original"}\n'
    inbox.write_bytes(before)

    original_fdopen = scanners_mod.os.fdopen

    class WriteFails:
        def __init__(self, descriptor):
            self.handle = original_fdopen(descriptor, "wb")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.handle.close()

        def write(self, _data):
            raise OSError(errno.ENOSPC, "no space left on device")

    def fail_write_open(descriptor, mode, *args, **kwargs):
        if mode == "wb":
            return WriteFails(descriptor)
        return original_fdopen(descriptor, mode, *args, **kwargs)

    monkeypatch.setattr(scanners_mod.os, "fdopen", fail_write_open)

    with pytest.raises(OSError, match="no space"):
        scanners_mod._write_scanner_inbox_bytes(tmp_path, b'{"text":"replacement"}\n')

    assert inbox.read_bytes() == before


def test_scanner_inbox_rewrite_rejects_post_write_temp_substitution(tmp_path, monkeypatch):
    """A substituted temp name cannot replace the existing inbox."""
    from brigade.work_cmd import scanners as scanners_mod

    inbox = tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl"
    inbox.parent.mkdir(parents=True)
    before = b'{"text":"original"}\n'
    inbox.write_bytes(before)
    outside = tmp_path / "outside-inbox.jsonl"
    outside_bytes = b'{"text":"outside"}\n'
    outside.write_bytes(outside_bytes)
    original_fsync = scanners_mod.os.fsync
    substituted = False

    def substitute_temp_after_write(descriptor):
        nonlocal substituted
        original_fsync(descriptor)
        if not substituted:
            substituted = True
            (temporary,) = inbox.parent.glob(".inbox.*.tmp")
            temporary.unlink()
            os.link(outside, temporary)

    monkeypatch.setattr(scanners_mod.os, "fsync", substitute_temp_after_write)

    with pytest.raises(OSError):
        scanners_mod._write_scanner_inbox_bytes(tmp_path, b'{"text":"replacement"}\n')

    assert inbox.read_bytes() == before
    assert outside.read_bytes() == outside_bytes


def test_scanner_inbox_rewrite_rolls_back_replace_boundary_temp_substitution(tmp_path, monkeypatch):
    """A temp alias substituted inside replace cannot displace the prior inbox."""
    from brigade.work_cmd import scanners as scanners_mod

    inbox = tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl"
    inbox.parent.mkdir(parents=True)
    before = b'{"text":"original"}\n'
    inbox.write_bytes(before)
    outside = tmp_path / "outside-inbox.jsonl"
    outside_bytes = b'{"text":"outside"}\n'
    outside.write_bytes(outside_bytes)
    original_replace = scanners_mod.os.replace
    substituted = False

    def substitute_temp_inside_replace(source, destination, *, src_dir_fd=None, dst_dir_fd=None):
        nonlocal substituted
        if not substituted and source.startswith(".inbox."):
            substituted = True
            temporary = inbox.parent / source
            temporary.unlink()
            os.link(outside, temporary)
        return original_replace(source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(scanners_mod.os, "replace", substitute_temp_inside_replace)

    with pytest.raises(OSError):
        scanners_mod._write_scanner_inbox_bytes(tmp_path, b'{"text":"replacement"}\n')

    assert inbox.read_bytes() == before
    assert outside.read_bytes() == outside_bytes


def test_scanner_inbox_rewrite_preserves_original_when_replace_raises_before_mutation(tmp_path, monkeypatch):
    from brigade.work_cmd import scanners as scanners_mod

    inbox = tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl"
    inbox.parent.mkdir(parents=True)
    before = b'{"text":"original"}\n'
    inbox.write_bytes(before)
    original_replace = scanners_mod.os.replace
    calls = 0

    def fail_before_replacement(source, destination, *, src_dir_fd=None, dst_dir_fd=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("replace failed before mutation")
        return original_replace(source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(scanners_mod.os, "replace", fail_before_replacement)

    with pytest.raises(OSError, match="before mutation"):
        scanners_mod._write_scanner_inbox_bytes(tmp_path, b'{"text":"replacement"}\n')

    assert inbox.read_bytes() == before


def test_scanner_inbox_rewrite_restores_existing_inbox_when_replace_then_raises(tmp_path, monkeypatch):
    from brigade.work_cmd import scanners as scanners_mod

    inbox = tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl"
    inbox.parent.mkdir(parents=True)
    before = b'{"text":"original"}\n'
    inbox.write_bytes(before)
    original_replace = scanners_mod.os.replace
    calls = 0

    def replace_then_interrupt(source, destination, *, src_dir_fd=None, dst_dir_fd=None):
        nonlocal calls
        calls += 1
        result = original_replace(source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
        if calls == 1:
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(scanners_mod.os, "replace", replace_then_interrupt)

    with pytest.raises(KeyboardInterrupt):
        scanners_mod._write_scanner_inbox_bytes(tmp_path, b'{"text":"replacement"}\n')

    assert inbox.read_bytes() == before


def test_scanner_inbox_rewrite_restores_missing_inbox_when_replace_then_raises(tmp_path, monkeypatch):
    from brigade.work_cmd import scanners as scanners_mod

    inbox = tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl"
    inbox.parent.mkdir(parents=True)
    original_replace = scanners_mod.os.replace
    calls = 0

    def replace_then_interrupt(source, destination, *, src_dir_fd=None, dst_dir_fd=None):
        nonlocal calls
        calls += 1
        result = original_replace(source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
        if calls == 1:
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(scanners_mod.os, "replace", replace_then_interrupt)

    with pytest.raises(KeyboardInterrupt):
        scanners_mod._write_scanner_inbox_bytes(tmp_path, b'{"text":"replacement"}\n')

    assert not inbox.exists()


def test_scanner_inbox_rewrite_restores_original_after_rollback_temp_substitution(tmp_path, monkeypatch):
    from brigade.work_cmd import scanners as scanners_mod

    inbox = tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl"
    inbox.parent.mkdir(parents=True)
    before = b'{"text":"original"}\n'
    attacker = b'{"text":"attacker"}\n'
    inbox.write_bytes(before)
    original_replace = scanners_mod.os.replace
    original_fsync_parent = scanners_mod._fsync_scanner_inbox_parent
    replace_calls = 0
    fsync_calls = 0

    def fail_publish_fsync(parent: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 1:
            raise OSError("post-publish fsync failed")
        original_fsync_parent(parent)

    def substitute_rollback(source, destination, *, src_dir_fd=None, dst_dir_fd=None):
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 2:
            rollback = inbox.parent / source
            rollback.unlink()
            rollback.write_bytes(attacker)
        return original_replace(source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(scanners_mod, "_fsync_scanner_inbox_parent", fail_publish_fsync)
    monkeypatch.setattr(scanners_mod.os, "replace", substitute_rollback)

    with pytest.raises(OSError, match="post-publish fsync failed"):
        scanners_mod._write_scanner_inbox_bytes(tmp_path, b'{"text":"replacement"}\n')

    assert inbox.read_bytes() == before


def test_scanner_inbox_rewrite_restores_original_after_repeated_rollback_substitution(tmp_path, monkeypatch):
    from brigade.work_cmd import scanners as scanners_mod

    inbox = tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl"
    inbox.parent.mkdir(parents=True)
    before = b'{"text":"original"}\n'
    attacker = b'{"text":"attacker"}\n'
    inbox.write_bytes(before)
    original_replace = scanners_mod.os.replace
    original_fsync_parent = scanners_mod._fsync_scanner_inbox_parent
    replace_calls = 0
    fsync_calls = 0

    def fail_publish_fsync(parent: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 1:
            raise OSError("post-publish fsync failed")
        original_fsync_parent(parent)

    def substitute_every_rollback(source, destination, *, src_dir_fd=None, dst_dir_fd=None):
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls >= 2:
            rollback = inbox.parent / source
            rollback.unlink()
            rollback.write_bytes(attacker)
        return original_replace(source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(scanners_mod, "_fsync_scanner_inbox_parent", fail_publish_fsync)
    monkeypatch.setattr(scanners_mod.os, "replace", substitute_every_rollback)

    with pytest.raises(OSError, match="rollback could not restore"):
        scanners_mod._write_scanner_inbox_bytes(tmp_path, b'{"text":"replacement"}\n')

    assert not inbox.exists() or inbox.read_bytes() == before


def test_scanner_inbox_append_restores_original_after_partial_write(tmp_path, monkeypatch):
    from brigade.work_cmd import scanners as scanners_mod

    inbox = tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl"
    inbox.parent.mkdir(parents=True)
    before = b'{"text":"original"}\n'
    incoming = b'{"text":"appended"}\n'
    inbox.write_bytes(before)
    original_fdopen = scanners_mod.os.fdopen

    class PartialWriter:
        def __init__(self, descriptor: int) -> None:
            self.handle = original_fdopen(descriptor, "wb")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.handle.close()

        def write(self, data: bytes):
            self.handle.write(data[:5])
            self.handle.flush()
            raise OSError("partial append")

        def flush(self) -> None:
            self.handle.flush()

    def partial_fdopen(descriptor, mode, *args, **kwargs):
        if mode == "wb":
            return PartialWriter(descriptor)
        return original_fdopen(descriptor, mode, *args, **kwargs)

    monkeypatch.setattr(scanners_mod.os, "fdopen", partial_fdopen)

    with pytest.raises(OSError, match="partial append"):
        scanners_mod._append_scanner_inbox_bytes(tmp_path, incoming)

    assert inbox.read_bytes() == before


def test_scanner_inbox_path_uses_validated_resolved_parent(tmp_path):
    from brigade.work_cmd import scanners as scanners_mod

    imports = tmp_path / ".brigade" / "work" / "imports"
    contained = tmp_path / ".brigade" / "work" / "contained-imports"
    contained.mkdir(parents=True)
    imports.symlink_to(contained, target_is_directory=True)

    assert scanners_mod._scanner_inbox_path(tmp_path) == contained.resolve() / "inbox.jsonl"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="platform does not support FIFOs")
@pytest.mark.parametrize(
    ("helper", "data"),
    [
        ("read", None),
        ("rewrite", b'{"text":"replacement"}\n'),
        ("append", b'{"text":"appended"}\n'),
    ],
)
def test_scanner_inbox_helpers_reject_fifo_without_blocking(tmp_path, helper, data):
    from brigade.work_cmd import scanners as scanners_mod

    inbox = tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl"
    inbox.parent.mkdir(parents=True)
    os.mkfifo(inbox)

    with pytest.raises(OSError):
        if helper == "read":
            scanners_mod._scanner_inbox_bytes(tmp_path)
        elif helper == "rewrite":
            scanners_mod._write_scanner_inbox_bytes(tmp_path, data)
        else:
            scanners_mod._append_scanner_inbox_bytes(tmp_path, data)

    assert stat.S_ISFIFO(inbox.stat().st_mode)


def test_scanner_reconciliation_rejects_symlinked_inbox_without_mutating_target(tmp_path, capsys):
    _init_git_repo(tmp_path)
    inbox = tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl"
    inbox.parent.mkdir(parents=True)
    inbox.write_text("")
    outside = tmp_path / "outside-inbox.jsonl"
    outside.write_text('{"text":"outside","kind":"task","source":"attacker"}\n')
    script = tmp_path / "replace_inbox.py"
    script.write_text(
        """
from pathlib import Path

inbox = Path.cwd() / ".brigade" / "work" / "imports" / "inbox.jsonl"
inbox.unlink()
inbox.symlink_to(Path.cwd() / "outside-inbox.jsonl")
"""
    )
    config = tmp_path / ".brigade" / "scanners.toml"
    config.write_text(
        f'''
[[scanner]]
id = "self-import"
source = "self-import"
command = "{sys.executable} {script}"
cadence = "daily@02:00"
enabled = true
timeout = 30
output_path = ".brigade/self-import-output.json"
conflict_window = "02:00-02:10"
'''
    )

    assert work_cmd.scanners_run(target=tmp_path, scanner_id="self-import", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["runs"][0]["provenance_imports_stamped"] == 0
    assert inbox.is_symlink()
    assert outside.read_text() == '{"text":"outside","kind":"task","source":"attacker"}\n'


def test_scanners_run_ingest_output_rejects_symlinked_inbox_without_mutating_target(tmp_path, capsys):
    _init_git_repo(tmp_path)
    inbox = tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl"
    inbox.parent.mkdir(parents=True)
    inbox.write_text("")
    outside = tmp_path / "outside-inbox.jsonl"
    outside.write_text('{"text":"outside","kind":"task","source":"attacker"}\n')
    script = tmp_path / "replace_inbox_and_write_import.py"
    script.write_text(
        """
import json
from pathlib import Path

root = Path.cwd()
inbox = root / ".brigade" / "work" / "imports" / "inbox.jsonl"
inbox.unlink()
inbox.symlink_to(root / "outside-inbox.jsonl")
(root / ".brigade" / "scanner-imports.jsonl").write_text(
    json.dumps({"text": "Valid scanner output", "kind": "task", "source": "scanner-output"}) + "\\n"
)
"""
    )
    config = tmp_path / ".brigade" / "scanners.toml"
    config.write_text(
        f'''
[[scanner]]
id = "output-import"
source = "output-import"
command = "{sys.executable} {script}"
cadence = "daily@02:00"
enabled = true
timeout = 30
output_path = ".brigade/output-import.json"
conflict_window = "02:00-02:10"
import_path = ".brigade/scanner-imports.jsonl"
import_format = "jsonl"
'''
    )

    assert work_cmd.scanners_run(target=tmp_path, scanner_id="output-import", ingest_output=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert inbox.is_symlink()
    assert outside.read_text() == '{"text":"outside","kind":"task","source":"attacker"}\n'
    assert payload["runs"][0]["ingest_output"]["created"] == 0
    assert payload["runs"][0]["ingest_output"]["rejected"] == 1


def test_scanner_self_import_preserves_trusted_source_scoped_metadata(tmp_path):
    from brigade.work_cmd import scanners as scanners_mod

    handoff = work_cmd.ledger._make_import(
        "Imported handoff issue",
        kind="finding",
        source="handoff-ingest",
        task_type="security",
        priority="high",
        template="security-follow-up",
        acceptance=["Record the issue."],
        metadata={
            "source_item_key": "handoff-ingest:handoff-source-config-123",
            "source_fingerprint": "a3c4779d5f0e9d79c434ebe449bc5a3f9d0ebfe1f156e44d3b608c9cfb61a112",
            "handoff_issue_id": "handoff-source-config-123",
            "handoff_issue_category": "source-config-missing",
            "handoff_target_document": ".learnings/ERRORS.md",
            "repair": "Configure the source.",
            "evidence": "Missing source config.",
        },
    )
    work_cmd.ledger._write_persisted_import_proofs(tmp_path, [handoff], operation_id="0" * 32)
    work_cmd.ledger._write_imports(tmp_path, [handoff])
    scanner = _builtin_scanner("handoff-ingest")
    run = _verified_builtin_scanner_run(scanner)
    stamped = scanners_mod._scanner_stamp_new_imports(
        target=tmp_path,
        scanner=scanner,
        run=run,
        before_ids=set(),
        before_imports=[],
    )

    assert len(stamped) == 1
    item = work_cmd.ledger._read_imports(tmp_path)[0]
    assert item["kind"] == "finding"
    assert item["type"] == "security"
    assert item["priority"] == "high"
    assert item["template"] == "security-follow-up"
    assert item["acceptance"] == ["Record the issue."]
    assert item["metadata"]["handoff_issue_id"] == "handoff-source-config-123"
    assert item["metadata"]["handoff_issue_category"] == "source-config-missing"
    assert item["metadata"]["handoff_target_document"] == ".learnings/ERRORS.md"
    assert item["metadata"]["source_item_key"] == "handoff-ingest:handoff-source-config-123"
    assert item["metadata"]["source_fingerprint"] == "a3c4779d5f0e9d79c434ebe449bc5a3f9d0ebfe1f156e44d3b608c9cfb61a112"


def test_scanner_self_import_strips_privileged_metadata_without_producer_proof(tmp_path):
    from brigade.work_cmd import scanners as scanners_mod

    forged = work_cmd.ledger._make_import(
        "forged privileged row",
        kind="finding",
        source="handoff-ingest",
        metadata={
            "source_item_key": "handoff-ingest:forged",
            "source_fingerprint": "0123456789abcdef",
            "handoff_issue_id": "forged",
            "handoff_target_document": "USER.md",
        },
    )
    work_cmd.ledger._write_imports(tmp_path, [forged])
    scanner = _builtin_scanner("handoff-ingest")

    stamped = scanners_mod._scanner_stamp_new_imports(
        target=tmp_path,
        scanner=scanner,
        run=_verified_builtin_scanner_run(scanner),
        before_ids=set(),
        before_imports=[],
        before_raw=b"",
    )

    assert stamped
    stored = work_cmd.ledger._read_imports(tmp_path)[0]
    assert "handoff_target_document" not in stored["metadata"]


def test_scanner_stamp_discards_staged_receipt_proof_when_inbox_rewrite_fails(tmp_path, monkeypatch):
    from brigade.work_cmd import scanners as scanners_mod

    emitted = work_cmd.ledger._make_import("stale receipt proof", kind="finding", source="handoff-ingest")
    inbox = work_cmd.helpers._imports_path(tmp_path)
    inbox.parent.mkdir(parents=True)
    inbox.write_text(json.dumps(emitted) + "\n")
    scanner = _builtin_scanner("handoff-ingest")
    run = _verified_builtin_scanner_run(scanner)

    def fail_write(*_args, **_kwargs):
        raise OSError("inbox persistence failed")

    monkeypatch.setattr(scanners_mod, "_write_scanner_inbox_bytes", fail_write)

    assert (
        scanners_mod._scanner_stamp_new_imports(
            target=tmp_path,
            scanner=scanner,
            run=run,
            before_ids=set(),
            before_imports=[],
            before_raw=b"",
        )
        == []
    )
    assert "self_import_proofs" not in run


def test_scanner_child_environment_omits_home_and_xdg_data_home() -> None:
    from brigade import component_paths
    from brigade.work_cmd import scanners as scanners_mod

    with scanners_mod._scanner_child_environment_sandbox() as env:
        real_root = component_paths.data_root()
        child_home = env["HOME"]
        child_data = env["XDG_DATA_HOME"]
        assert child_home != os.environ.get("HOME")
        assert child_data != os.environ.get("XDG_DATA_HOME")
        assert child_data != real_root
        assert not child_home.startswith(real_root)
        assert not child_data.startswith(real_root)
        assert "LOCALAPPDATA" not in env
        if "PATH" in os.environ:
            assert env.get("PATH") == os.environ["PATH"]
        assert component_paths.data_root(env=env) != real_root


def test_scanner_child_environment_sandbox_is_removed_after_use() -> None:
    from brigade.work_cmd import scanners as scanners_mod

    sandboxes = []
    for _ in range(3):
        with scanners_mod._scanner_child_environment_sandbox() as env:
            sandbox = Path(env["HOME"])
            assert sandbox.is_dir()
            sandboxes.append(sandbox)
        assert not sandbox.exists()

    assert [sandbox for sandbox in sandboxes if sandbox.exists()] == []


def test_scanner_child_environment_sandbox_is_removed_when_the_child_raises() -> None:
    from brigade.work_cmd import scanners as scanners_mod

    captured: list[Path] = []
    with pytest.raises(RuntimeError, match="child blew up"):
        with scanners_mod._scanner_child_environment_sandbox() as env:
            captured.append(Path(env["HOME"]))
            raise RuntimeError("child blew up")

    assert captured and not captured[0].exists()


def test_scanner_child_environment_passes_through_documented_allowlist(monkeypatch) -> None:
    from brigade.work_cmd import scanners as scanners_mod

    monkeypatch.setenv("GH_TOKEN", "token-value")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/run/agent.sock")
    monkeypatch.setenv("USER", "operator")
    monkeypatch.setenv("LOGNAME", "operator")
    monkeypatch.setenv("TMPDIR", "/scratch")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")

    with scanners_mod._scanner_child_environment_sandbox() as env:
        assert env["GH_TOKEN"] == "token-value"
        assert env["SSH_AUTH_SOCK"] == "/run/agent.sock"
        assert env["USER"] == "operator"
        assert env["LOGNAME"] == "operator"
        assert env["TMPDIR"] == "/scratch"
        assert env["HTTPS_PROXY"] == "http://proxy.invalid:3128"
        # The scrub still holds for everything that can reach the authority store.
        assert env["HOME"] != os.environ["HOME"]
        assert env["XDG_DATA_HOME"] != os.environ.get("XDG_DATA_HOME")


def test_scanner_child_environment_points_gh_at_the_operator_config_dir(tmp_path, monkeypatch) -> None:
    from brigade.work_cmd import scanners as scanners_mod

    config_home = tmp_path / "config"
    (config_home / "gh").mkdir(parents=True)
    (config_home / "gh" / "hosts.yml").write_text("github.com:\n  oauth_token: t\n")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))

    with scanners_mod._scanner_child_environment_sandbox() as env:
        assert env["GH_CONFIG_DIR"] == str(config_home / "gh")
        assert Path(env["GH_CONFIG_DIR"], "hosts.yml").is_file()
        # The gh config dir carries no Brigade authority: the store stays sandboxed.
        assert env["XDG_CONFIG_HOME"] != str(config_home)
        assert env["XDG_DATA_HOME"] != os.environ.get("XDG_DATA_HOME")


def test_scanner_child_sandbox_seeds_the_parent_directory_bindings(tmp_path) -> None:
    """A Brigade child under the sandbox validates directories the parent created and bound."""
    from brigade.work_cmd import ledger as ledger_mod
    from brigade.work_cmd import scanners as scanners_mod

    scanners_mod._prebind_child_visible_directories(tmp_path)
    real_path = ledger_mod._directory_authority_store_path(tmp_path)
    _path, parent_payload = ledger_mod._read_external_directory_authority(tmp_path)
    assert isinstance(parent_payload, dict)
    assert ".brigade/work/imports/proofs" in parent_payload["directories"]

    with scanners_mod._scanner_child_environment_sandbox(tmp_path) as env:
        child_path = ledger_mod._directory_authority_store_path(tmp_path, env=env)
        assert child_path != real_path
        assert str(child_path).startswith(env["XDG_DATA_HOME"])
        child_payload = ledger_mod._read_external_directory_authority_path(child_path, env=env)
        assert child_payload is not None
        assert child_payload["directories"] == parent_payload["directories"]
        sandbox = Path(env["HOME"])

    # The seeded copy dies with the sandbox; the operator store is untouched.
    assert not sandbox.exists()
    remaining = ledger_mod._read_external_directory_authority_path(real_path)
    assert remaining is not None
    assert remaining["directories"] == parent_payload["directories"]


def _stub_gh_bin(directory: Path) -> Path:
    """A `gh` that fails the way the real one does when it cannot authenticate."""
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "gh"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        "token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')\n"
        "config = os.environ.get('GH_CONFIG_DIR')\n"
        "hosts = pathlib.Path(config, 'hosts.yml') if config else None\n"
        "if not token and not (hosts and hosts.is_file()):\n"
        "    sys.stderr.write('gh: To get started with GitHub CLI, please run: gh auth login\\n')\n"
        "    sys.exit(4)\n"
        "json.dump({'authenticated': True, 'argv': sys.argv[1:]}, sys.stdout)\n"
    )
    script.chmod(0o755)
    return script


def test_shipped_scanner_child_can_authenticate_gh_under_the_scrubbed_env(tmp_path, monkeypatch) -> None:
    """The env scrub must not break a scanner that shells out to `gh`."""
    from brigade.work_cmd import scanners as scanners_mod

    bin_dir = tmp_path / "bin"
    _stub_gh_bin(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GH_TOKEN", "token-value")
    scanner = {
        "id": "gh-probe",
        "source": "gh-probe",
        "command": "gh issue list --json number",
        "enabled": True,
        "timeout": 60,
    }

    receipt = scanners_mod._scanner_run_one(tmp_path, scanner, force=True)

    assert receipt["exit_code"] == 0, receipt.get("stderr_summary")
    assert receipt["status"] == "completed"
    assert "authenticated" in receipt["stdout_summary"]


def test_shipped_scanner_child_gh_fails_without_credentials(tmp_path, monkeypatch) -> None:
    """Control: the same probe fails when no credential reaches the child."""
    from brigade.work_cmd import scanners as scanners_mod

    bin_dir = tmp_path / "bin"
    _stub_gh_bin(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "absent-gh-config"))
    scanner = {
        "id": "gh-probe",
        "source": "gh-probe",
        "command": "gh issue list --json number",
        "enabled": True,
        "timeout": 60,
    }

    receipt = scanners_mod._scanner_run_one(tmp_path, scanner, force=True)

    assert receipt["exit_code"] == 4
    assert "gh auth login" in receipt["stderr_summary"]


def test_shipped_handoff_ingest_scanner_completes_under_the_scrubbed_env(tmp_path, monkeypatch) -> None:
    """Run the shipped, enabled-by-default scanner end to end with the scrubbed child env."""
    from brigade.work_cmd import scanners as scanners_mod

    bin_dir = tmp_path / "bin"
    _stub_gh_bin(bin_dir)
    monkeypatch.setenv("GH_TOKEN", "token-value")
    venv_bin = Path(sys.executable).parent
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{venv_bin}{os.pathsep}{os.environ['PATH']}")
    scanner = _builtin_scanner("handoff-ingest")
    assert scanner["command"] == "brigade handoff sync-issues --json"
    assert scanner["enabled"] is True

    receipt = scanners_mod._scanner_run_one(tmp_path, scanner, force=True)

    assert receipt["exit_code"] == 0, receipt.get("stderr_summary")
    assert receipt["status"] == "completed"


def test_scanner_stamp_inbox_probe_treats_not_a_directory_as_missing(tmp_path, monkeypatch):
    from brigade.work_cmd import scanners as scanners_mod

    scanner = _builtin_scanner("handoff-ingest")
    run = _verified_builtin_scanner_run(scanner)
    emitted = work_cmd.ledger._make_import("probe inbox", kind="finding", source="handoff-ingest")
    inbox = work_cmd.helpers._imports_path(tmp_path)
    inbox.parent.mkdir(parents=True)
    inbox.write_text(json.dumps(emitted) + "\n")
    original = scanners_mod._open_scanner_inbox
    calls = {"n": 0}

    def raise_on_probe(target, flags, *, create=False):
        calls["n"] += 1
        if calls["n"] == 1:
            return original(target, flags, create=create)
        raise NotADirectoryError(errno.ENOTDIR, "Not a directory", ".brigade")

    monkeypatch.setattr(scanners_mod, "_open_scanner_inbox", raise_on_probe)
    stamped = scanners_mod._scanner_stamp_new_imports(
        target=tmp_path,
        scanner=scanner,
        run=run,
        before_ids=set(),
        before_imports=[],
        before_raw=b"",
    )

    assert calls["n"] >= 2
    assert stamped


def test_scanner_stamp_binding_restore_failure_fails_closed(tmp_path, monkeypatch):
    from brigade.work_cmd import scanners as scanners_mod

    scanner = _builtin_scanner("handoff-ingest")
    run = _verified_builtin_scanner_run(scanner)
    emitted = work_cmd.ledger._make_import("binding restore failure", kind="finding", source="handoff-ingest")
    inbox = work_cmd.helpers._imports_path(tmp_path)
    inbox.parent.mkdir(parents=True)
    raw = json.dumps(emitted).encode() + b"\n"
    inbox.write_bytes(raw)

    def fail_proof(*_args, **_kwargs):
        raise OSError("proof persistence failed")

    def fail_restore(*_args, **_kwargs):
        raise OSError("file authority restore failed")

    monkeypatch.setattr(work_cmd.ledger, "_write_persisted_import_proofs", fail_proof)
    monkeypatch.setattr(work_cmd.ledger, "_restore_external_file_authorities", fail_restore)

    with pytest.raises(OSError, match="scanner import binding rollback could not restore"):
        scanners_mod._scanner_stamp_new_imports(
            target=tmp_path,
            scanner=scanner,
            run=run,
            before_ids=set(),
            before_imports=[],
            before_raw=b"",
        )

    assert inbox.read_bytes() == raw


def test_scanner_stamp_surfaces_failed_proof_rollback_and_restores_inbox(tmp_path, monkeypatch):
    from brigade.work_cmd import scanners as scanners_mod

    scanner = _builtin_scanner("handoff-ingest")
    run = _verified_builtin_scanner_run(scanner)
    emitted = work_cmd.ledger._make_import("proof rollback failure", kind="finding", source="handoff-ingest")
    work_cmd.ledger._write_persisted_import_proofs(tmp_path, [emitted], operation_id="0" * 32)
    inbox = work_cmd.helpers._imports_path(tmp_path)
    inbox.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(emitted).encode() + b"\n"
    inbox.write_bytes(raw)
    original_write = scanners_mod._write_scanner_inbox_bytes
    writes = 0

    def fail_proof(*_args, **_kwargs):
        raise OSError("proof persistence failed")

    def fail_second_write(target, data):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("rollback failed")
        original_write(target, data)

    monkeypatch.setattr(work_cmd.ledger, "_write_persisted_import_proofs", fail_proof)
    monkeypatch.setattr(scanners_mod, "_write_scanner_inbox_bytes", fail_second_write)

    with pytest.raises(OSError, match="rollback failed"):
        scanners_mod._scanner_stamp_new_imports(
            target=tmp_path,
            scanner=scanner,
            run=run,
            before_ids=set(),
            before_imports=[],
            before_raw=b"",
        )

    assert inbox.read_bytes() == raw


def test_scanner_self_import_preserves_trusted_memory_refresh_identity(tmp_path):
    from brigade.work_cmd import scanners as scanners_mod

    refresh = work_cmd.ledger._make_import(
        "Refresh memory card memory/cards/decay/example.md",
        kind="task",
        source="memory-refresh",
        task_type="docs",
        priority="normal",
        template="docs",
        acceptance=["Review the memory card."],
        metadata={
            "source_item_key": "memory-refresh:decay-example",
            "source_fingerprint": "53ff90f17a2d879497cec5c037ff74f2cf1a8fdc8482b66f395b729c32d06f05",
            "card_id": "decay-example",
            "card_file": "memory/cards/decay/example.md",
            "refresh_reason": "missing-freshness",
            "queue_path": "memory/cards/decay/refresh-queue.json",
        },
    )
    work_cmd.ledger._write_persisted_import_proofs(tmp_path, [refresh], operation_id="0" * 32)
    work_cmd.ledger._write_imports(tmp_path, [refresh])
    scanner = _builtin_scanner("memory-refresh")
    stamped = scanners_mod._scanner_stamp_new_imports(
        target=tmp_path,
        scanner=scanner,
        run=_verified_builtin_scanner_run(scanner),
        before_ids=set(),
        before_imports=[],
    )

    assert len(stamped) == 1
    metadata = work_cmd.ledger._read_imports(tmp_path)[0]["metadata"]
    assert metadata["card_id"] == "decay-example"
    assert metadata["card_file"] == "memory/cards/decay/example.md"
    assert metadata["refresh_reason"] == "missing-freshness"
    assert metadata["queue_path"] == "memory/cards/decay/refresh-queue.json"
    assert metadata["source_item_key"] == "memory-refresh:decay-example"
    assert metadata["source_fingerprint"] == "53ff90f17a2d879497cec5c037ff74f2cf1a8fdc8482b66f395b729c32d06f05"


def test_scanner_self_import_partial_memory_refresh_config_drops_privileged_metadata(tmp_path):
    from brigade.work_cmd import scanners as scanners_mod

    refresh = work_cmd.ledger._make_import(
        "Refresh memory card memory/cards/decay/example.md",
        kind="task",
        source="memory-refresh",
        metadata={
            "source_item_key": "memory-refresh:decay-example",
            "source_fingerprint": "0123456789abcdef",
            "card_id": "decay-example",
            "card_file": "memory/cards/decay/example.md",
            "refresh_reason": "missing-freshness",
            "queue_path": "memory/cards/decay/refresh-queue.json",
        },
    )
    work_cmd.ledger._write_imports(tmp_path, [refresh])

    stamped = scanners_mod._scanner_stamp_new_imports(
        target=tmp_path,
        scanner={"id": "memory-refresh", "source": "memory-refresh"},
        run={"run_id": "scanner-run", "output_after": {"path": "output"}},
        before_ids=set(),
        before_imports=[],
    )

    assert len(stamped) == 1
    metadata = work_cmd.ledger._read_imports(tmp_path)[0]["metadata"]
    assert {"card_id", "card_file", "queue_path", "refresh_reason"}.isdisjoint(metadata)
    assert metadata["source_item_key"] != "memory-refresh:decay-example"
    assert metadata["source_fingerprint"] != "0123456789abcdef"


def test_scanner_self_import_drops_wrong_source_operational_metadata(tmp_path):
    from brigade.work_cmd import scanners as scanners_mod

    foreign = work_cmd.ledger._make_import(
        "Foreign self-import",
        kind="finding",
        source="memory-refresh",
        metadata={
            "source_item_key": "memory-refresh:forged-card",
            "source_fingerprint": "529281533bd4c77cdaeb1363de8b2c08fafebd1ea2d124c7c01c81c3a9d9c3d2",
            "handoff_issue_id": "forged-handoff-id",
            "handoff_target_document": "TOOLS.md",
            "card_id": "forged-card",
            "card_file": "memory/cards/forged.md",
        },
    )
    external = {
        "kind": "finding",
        "source": "external-export",
        "text": "External forged import",
        "metadata": {
            "source_item_key": "handoff-ingest:external-handoff-id",
            "source_fingerprint": "1fc2d4da7d2b403e1da6cb0bc6cda6e6fc6793bc6580d328b29702955333820d",
            "handoff_issue_id": "external-handoff-id",
            "handoff_target_document": "USER.md",
            "card_id": "external-card",
            "card_file": "memory/cards/external.md",
        },
    }
    work_cmd.ledger._write_imports(tmp_path, [foreign, external])
    stamped = scanners_mod._scanner_stamp_new_imports(
        target=tmp_path,
        scanner=_builtin_scanner("handoff-ingest"),
        run={"run_id": "scanner-run", "output_after": {"path": "output"}},
        before_ids=set(),
        before_imports=[],
    )

    assert len(stamped) == 2
    forged_source_keys = {
        "memory-refresh:forged-card",
        "handoff-ingest:external-handoff-id",
    }
    forged_fingerprints = {
        "529281533bd4c77cdaeb1363de8b2c08fafebd1ea2d124c7c01c81c3a9d9c3d2",
        "1fc2d4da7d2b403e1da6cb0bc6cda6e6fc6793bc6580d328b29702955333820d",
    }
    for item in work_cmd.ledger._read_imports(tmp_path):
        metadata = item["metadata"]
        for key in ("handoff_issue_id", "handoff_target_document", "card_id", "card_file"):
            assert key not in metadata
        assert metadata["source_item_key"] not in forged_source_keys
        assert metadata["source_fingerprint"] not in forged_fingerprints
        assert metadata["source_fingerprint"] == work_cmd.ledger._untrusted_import_canonical_hash(item)


def test_scanner_read_receipt_derives_target_from_correct_parent(tmp_path: Path) -> None:
    """_scanner_read_receipt must use parents[3] (the workspace root) not parents[2]."""
    from brigade.work_cmd import ledger, scanners as scanners_mod

    run_id = "compat-run"
    root = scanners_mod._open_scanner_runs_directory(tmp_path, create=True)
    os.mkdir(run_id, dir_fd=root)
    run = os.open(run_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
    try:
        ledger._record_verifier_owned_directory(
            tmp_path,
            components=(".brigade", "scanners", "runs", run_id),
            directory=run,
        )
    finally:
        os.close(run)
        os.close(root)
    run_dir = helpers._scanner_runs_root(tmp_path) / run_id
    (run_dir / "receipt.json").write_text(json.dumps({"run_id": run_id, "status": "completed"}))
    _bind_planted_scanner_receipt(tmp_path, run_id)

    receipt = scanners_mod._scanner_read_receipt(run_dir / "receipt.json")
    assert receipt is not None
    assert receipt["run_id"] == run_id


def test_scanner_read_receipt_accepts_run_directory_path(tmp_path: Path) -> None:
    """_scanner_read_receipt should also accept a run directory (not just receipt.json)."""
    from brigade.work_cmd import ledger, scanners as scanners_mod

    run_id = "compat-run-dir"
    root = scanners_mod._open_scanner_runs_directory(tmp_path, create=True)
    os.mkdir(run_id, dir_fd=root)
    run = os.open(run_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
    try:
        ledger._record_verifier_owned_directory(
            tmp_path,
            components=(".brigade", "scanners", "runs", run_id),
            directory=run,
        )
    finally:
        os.close(run)
        os.close(root)
    run_dir = helpers._scanner_runs_root(tmp_path) / run_id
    (run_dir / "receipt.json").write_text(json.dumps({"run_id": run_id, "status": "completed"}))
    _bind_planted_scanner_receipt(tmp_path, run_id)

    receipt = scanners_mod._scanner_read_receipt(run_dir)
    assert receipt is not None
    assert receipt["run_id"] == run_id


def _write_scanner_config(tmp_path: Path, scanner_id: str, command: str, *, timeout: int = 30) -> None:
    config = tmp_path / ".brigade" / "scanners.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        f"""
[[scanner]]
id = "{scanner_id}"
source = "{scanner_id}"
command = "{command}"
cadence = "daily@02:00"
enabled = true
timeout = {timeout}
output_path = ".brigade/scanner-output.json"
conflict_window = "02:00-02:10"
"""
    )


def test_scanner_run_bounded_output_reaps_child_and_records_typed_failure(tmp_path):
    """#1197: unbounded scanner output must trip a typed failure, not exhaust memory."""
    from brigade.work_cmd import scanners as scanners_mod

    script = tmp_path / "loud.py"
    script.write_text("import sys\nsys.stdout.write('x' * 4194304)\n")
    _write_scanner_config(tmp_path, "loud-scan", f"{sys.executable} {script}", timeout=60)

    payload, _rc = scanners_mod._scanners_run_payload(target=tmp_path, scanner_id="loud-scan", force=True)
    receipt = payload["runs"][0]

    assert receipt["status"] == "failed"
    assert receipt.get("output_limit_exceeded") is True
    assert "capture limit" in str(receipt.get("error"))
    assert receipt["exit_code"] != 0


def test_scanner_run_does_not_wait_for_descendant_holding_pipes(tmp_path):
    """#1197: a grandchild holding the pipes is reaped promptly AND fails the run.

    The pre-fix behavior asserted `completed` here; the security review found
    that a terminated group must never stand as success, so this test now
    asserts the typed incomplete-process-group failure.
    """
    from brigade.work_cmd import scanners as scanners_mod

    script = tmp_path / "pipe_holder.py"
    script.write_text(
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        "sys.exit(0)\n"
    )
    _write_scanner_config(tmp_path, "pipe-holder", f"{sys.executable} {script}", timeout=20)

    payload, rc = scanners_mod._scanners_run_payload(target=tmp_path, scanner_id="pipe-holder", force=True)
    receipt = payload["runs"][0]

    assert rc == 1
    assert receipt["status"] == "failed"
    assert receipt.get("incomplete_process_group") is True
    assert receipt["exit_code"] != 0
    assert "process group" in str(receipt.get("error")).lower()
    assert float(receipt["duration_seconds"]) < 15


def test_scanner_cutoff_appended_rows_get_no_proof_and_are_rolled_back(tmp_path):
    """#1197: rows appended before the group cutoff must not become eligible work."""
    from brigade.work_cmd import config as config_mod
    from brigade.work_cmd import scanners as scanners_mod

    script = tmp_path / "late_writer.py"
    script.write_text(
        "import json, subprocess, sys\n"
        "from pathlib import Path\n"
        "inbox = Path.cwd() / '.brigade' / 'work' / 'imports' / 'inbox.jsonl'\n"
        "inbox.parent.mkdir(parents=True, exist_ok=True)\n"
        "record = {'id': 'late-1', 'kind': 'task', 'source': 'late-writer',\n"
        "          'text': 'Late sneaky row', 'status': 'pending',\n"
        "          'created_at': '2026-08-25T00:00:00+00:00',\n"
        "          'updated_at': '2026-08-25T00:00:00+00:00'}\n"
        "with inbox.open('a') as handle:\n"
        "    handle.write(json.dumps(record) + '\\n')\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        "sys.exit(0)\n"
    )
    _write_scanner_config(tmp_path, "late-writer", f"{sys.executable} {script}", timeout=20)

    payload, rc = scanners_mod._scanners_run_payload(target=tmp_path, scanner_id="late-writer", force=True)
    receipt = payload["runs"][0]

    assert rc == 1
    assert receipt["status"] == "failed"
    assert receipt.get("incomplete_process_group") is True
    loaded, _errors = config_mod._load_scanner_config(tmp_path)
    assert scanners_mod._scanner_run_proof(loaded[0], receipt) is None
    inbox = helpers._imports_path(tmp_path)
    if inbox.exists():
        assert "late-1" not in inbox.read_text()
    assert scanners_mod._scanner_import_counts(tmp_path)["total"] == 0


def test_scanner_inbox_read_enforces_total_byte_limit(tmp_path, monkeypatch):
    """#1198: inbox reads must reject oversized files with a typed failure."""
    from brigade.work_cmd import scanners as scanners_mod

    monkeypatch.setattr(scanners_mod, "_SCANNER_INPUT_TOTAL_LIMIT_BYTES", 64)
    inbox = helpers._imports_path(tmp_path)
    inbox.parent.mkdir(parents=True)
    inbox.write_bytes(b"x" * 128)

    with pytest.raises(scanners_mod.ScannerInputLimitExceeded):
        scanners_mod._scanner_inbox_bytes(tmp_path)


def test_scanner_stamp_rejects_oversized_inbox_with_typed_failure(tmp_path, monkeypatch):
    from brigade.work_cmd import scanners as scanners_mod

    monkeypatch.setattr(scanners_mod, "_SCANNER_INPUT_TOTAL_LIMIT_BYTES", 64)
    inbox = helpers._imports_path(tmp_path)
    inbox.parent.mkdir(parents=True)
    inbox.write_bytes(b"x" * 128)
    scanner = {"id": "bounded", "source": "bounded"}
    run = _verified_builtin_scanner_run(scanner)

    stamped = scanners_mod._scanner_stamp_new_imports(
        target=tmp_path,
        scanner=scanner,
        run=run,
        before_ids=set(),
        before_imports=[],
        before_raw=b"",
    )

    assert stamped == []
    assert run["self_import"]["created"] == 0
    assert run["self_import"]["rejection_reasons"] == {"provenance_stamp_failed": 1}


def test_scanner_inbox_imports_enforce_per_record_byte_limit(tmp_path, monkeypatch):
    from brigade.work_cmd import scanners as scanners_mod

    monkeypatch.setattr(scanners_mod, "_SCANNER_RECORD_LIMIT_BYTES", 32)
    inbox = helpers._imports_path(tmp_path)
    inbox.parent.mkdir(parents=True)
    inbox.write_bytes(json.dumps({"id": "r1", "text": "y" * 128}).encode() + b"\n")

    with pytest.raises(scanners_mod.ScannerInputLimitExceeded):
        scanners_mod._scanner_inbox_imports(tmp_path)


def test_scanner_jsonl_ingestion_enforces_total_and_per_record_limits(tmp_path, monkeypatch):
    from brigade.work_cmd import scanners as scanners_mod

    imports = tmp_path / "imports"
    imports.mkdir()
    path = imports / "output.jsonl"

    monkeypatch.setattr(scanners_mod, "_SCANNER_INPUT_TOTAL_LIMIT_BYTES", 64)
    path.write_bytes(b'{"text":"' + b"z" * 256 + b'"}\n')
    with pytest.raises(scanners_mod.ScannerInputLimitExceeded):
        scanners_mod._scanner_read_import_jsonl(
            tmp_path,
            {"id": "ingest", "source": "ingest", "import_path": "imports/output.jsonl"},
        )

    monkeypatch.setattr(scanners_mod, "_SCANNER_RECORD_LIMIT_BYTES", 16)
    path.write_bytes(json.dumps({"id": "r1", "text": "w" * 64}).encode() + b"\n")
    with pytest.raises(scanners_mod.ScannerInputLimitExceeded):
        scanners_mod._scanner_read_import_jsonl(
            tmp_path,
            {"id": "ingest", "source": "ingest", "import_path": "imports/output.jsonl"},
        )


def test_scanner_rollback_snapshot_is_bounded(tmp_path, monkeypatch):
    """#1198: rollback snapshots of run artifacts must be bounded."""
    from brigade.work_cmd import scanners as scanners_mod

    monkeypatch.setattr(scanners_mod, "_SCANNER_SNAPSHOT_LIMIT_BYTES", 16)
    authority = scanners_mod._open_scanner_run_directory(tmp_path, "snapshot-run")
    try:
        run_dir = helpers._scanner_runs_root(tmp_path) / "snapshot-run"
        (run_dir / "stdout.log").write_bytes(b"a" * 64)
        with pytest.raises(scanners_mod.ScannerSnapshotLimitExceeded):
            scanners_mod._write_scanner_run_file(authority, "stdout.log", b"replacement")
    finally:
        os.close(authority.directory)
        os.close(authority.root)


def test_failed_scanner_run_restores_pre_run_inbox_and_rejects_new_rows(tmp_path):
    """#1199: a failed run must not leave eligible rows behind in the inbox."""
    from brigade.work_cmd import ledger, scanners as scanners_mod

    before_row = ledger._make_import("Legit prior finding", kind="finding", source="handoff-ingest")
    sneaky = ledger._make_import("Sneaky pending finding", kind="finding", source="handoff-ingest")
    ledger._write_imports(tmp_path, [before_row])
    inbox = helpers._imports_path(tmp_path)
    before_raw = inbox.read_bytes()
    inbox.write_bytes(before_raw + json.dumps(sneaky, sort_keys=True).encode() + b"\n")

    scanner = {"id": "handoff-ingest", "source": "handoff-ingest"}
    run = {"run_id": "failed-run", "status": "failed", "exit_code": 3}

    stamped = scanners_mod._scanner_stamp_new_imports(
        target=tmp_path,
        scanner=scanner,
        run=run,
        before_ids={before_row["id"]},
        before_imports=[before_row],
        before_raw=before_raw,
    )

    assert stamped == []
    assert inbox.read_bytes() == before_raw
    assert run["self_import"]["created"] == 0
    assert run["self_import"]["rejection_reasons"] == {"run_not_completed": 1}


def test_failed_scanner_run_leaves_no_eligible_work_end_to_end(tmp_path):
    """#1199 end to end: scanner appends a pending row then exits nonzero."""
    from brigade.work_cmd import scanners as scanners_mod

    script = tmp_path / "sneaky.py"
    script.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "root = Path.cwd()\n"
        "inbox = root / '.brigade' / 'work' / 'imports' / 'inbox.jsonl'\n"
        "inbox.parent.mkdir(parents=True, exist_ok=True)\n"
        "record = {'id': 'sneaky-1', 'kind': 'task', 'source': 'sneaky-scan',\n"
        "          'text': 'Eligible sneaky row', 'status': 'pending',\n"
        "          'created_at': '2026-08-25T00:00:00+00:00',\n"
        "          'updated_at': '2026-08-25T00:00:00+00:00'}\n"
        "with inbox.open('a') as handle:\n"
        "    handle.write(json.dumps(record) + '\\n')\n"
        "sys.exit(3)\n"
    )
    _write_scanner_config(tmp_path, "sneaky-scan", f"{sys.executable} {script}", timeout=30)

    payload, rc = scanners_mod._scanners_run_payload(target=tmp_path, scanner_id="sneaky-scan", force=True)
    receipt = payload["runs"][0]

    assert rc == 1
    assert receipt["status"] == "failed"
    inbox = helpers._imports_path(tmp_path)
    if inbox.exists():
        rows = [row for row in inbox.read_text().splitlines() if row.strip()]
        assert all(json.loads(row).get("id") != "sneaky-1" for row in rows)
    counts = scanners_mod._scanner_import_counts(tmp_path)
    assert counts["total"] == 0
    assert receipt["self_import"]["created"] == 0
    assert receipt["self_import"]["rejection_reasons"] == {"run_not_completed": 1}


def test_scanner_run_aborts_before_launch_when_pre_run_snapshot_unavailable(tmp_path, monkeypatch):
    """#1207: an un-snapshotable inbox must abort the run before the scanner launches."""
    from brigade.work_cmd import scanners as scanners_mod

    launched: list[bool] = []
    real_run_one = scanners_mod._scanner_run_one

    def spy_run_one(*args, **kwargs):
        launched.append(True)
        return real_run_one(*args, **kwargs)

    def broken_snapshot(_target):
        raise OSError("inbox snapshot unavailable")

    monkeypatch.setattr(scanners_mod, "_scanner_run_one", spy_run_one)
    monkeypatch.setattr(scanners_mod, "_snapshot_scanner_inbox", broken_snapshot)

    script = tmp_path / "writer.py"
    script.write_text("import sys\nsys.exit(0)\n")
    _write_scanner_config(tmp_path, "snap-fail", f"{sys.executable} {script}", timeout=20)

    payload, rc = scanners_mod._scanners_run_payload(target=tmp_path, scanner_id="snap-fail", force=True)

    assert rc == 2
    assert launched == []
    assert any("snapshot" in str(error).lower() for error in payload["errors"])


def test_oversized_pre_run_inbox_aborts_run_and_preserves_exact_bytes(tmp_path, monkeypatch):
    """#1207: a swallowed snapshot OSError used to let a failed scanner destroy the inbox."""
    from brigade.work_cmd import ledger, scanners as scanners_mod

    before_row = ledger._make_import("Legit prior finding", kind="finding", source="handoff-ingest")
    ledger._write_imports(tmp_path, [before_row])
    inbox = helpers._imports_path(tmp_path)
    original = inbox.read_bytes() + b"x" * 256
    inbox.write_bytes(original)

    monkeypatch.setattr(scanners_mod, "_SCANNER_INPUT_TOTAL_LIMIT_BYTES", 64)

    script = tmp_path / "truncator.py"
    script.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "inbox = Path.cwd() / '.brigade' / 'work' / 'imports' / 'inbox.jsonl'\n"
        "inbox.write_text('')\n"
        "record = {'id': 'sneaky-trunc', 'kind': 'task', 'source': 'truncator',\n"
        "          'text': 'Sneaky', 'status': 'pending',\n"
        "          'created_at': '2026-08-25T00:00:00+00:00',\n"
        "          'updated_at': '2026-08-25T00:00:00+00:00'}\n"
        "with inbox.open('a') as handle:\n"
        "    handle.write(json.dumps(record) + '\\n')\n"
        "sys.exit(3)\n"
    )
    _write_scanner_config(tmp_path, "truncator", f"{sys.executable} {script}", timeout=20)

    payload, rc = scanners_mod._scanners_run_payload(target=tmp_path, scanner_id="truncator", force=True)

    assert rc == 2
    assert payload["runs"] == []
    assert inbox.read_bytes() == original


def test_failed_run_after_truncation_restores_exact_original_bytes(tmp_path):
    """#1207: under-cap snapshot + mid-run truncation still restores the original bytes."""
    from brigade.work_cmd import ledger, scanners as scanners_mod

    before_row = ledger._make_import("Legit prior finding", kind="finding", source="handoff-ingest")
    ledger._write_imports(tmp_path, [before_row])
    inbox = helpers._imports_path(tmp_path)
    original = inbox.read_bytes()

    script = tmp_path / "truncator.py"
    script.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "inbox = Path.cwd() / '.brigade' / 'work' / 'imports' / 'inbox.jsonl'\n"
        "inbox.write_text('')\n"
        "record = {'id': 'sneaky-trunc', 'kind': 'task', 'source': 'truncator',\n"
        "          'text': 'Sneaky', 'status': 'pending',\n"
        "          'created_at': '2026-08-25T00:00:00+00:00',\n"
        "          'updated_at': '2026-08-25T00:00:00+00:00'}\n"
        "with inbox.open('a') as handle:\n"
        "    handle.write(json.dumps(record) + '\\n')\n"
        "sys.exit(3)\n"
    )
    _write_scanner_config(tmp_path, "truncator", f"{sys.executable} {script}", timeout=20)

    payload, rc = scanners_mod._scanners_run_payload(target=tmp_path, scanner_id="truncator", force=True)

    assert rc == 1
    assert payload["runs"][0]["status"] == "failed"
    assert inbox.read_bytes() == original


def test_missing_pre_run_inbox_stays_missing_after_failed_run(tmp_path):
    """#1207: restoring a previously missing inbox must not create an empty file."""
    from brigade.work_cmd import scanners as scanners_mod

    script = tmp_path / "creator.py"
    script.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "inbox = Path.cwd() / '.brigade' / 'work' / 'imports' / 'inbox.jsonl'\n"
        "inbox.parent.mkdir(parents=True, exist_ok=True)\n"
        "record = {'id': 'creator-1', 'kind': 'task', 'source': 'creator',\n"
        "          'text': 'Created row', 'status': 'pending',\n"
        "          'created_at': '2026-08-25T00:00:00+00:00',\n"
        "          'updated_at': '2026-08-25T00:00:00+00:00'}\n"
        "with inbox.open('a') as handle:\n"
        "    handle.write(json.dumps(record) + '\\n')\n"
        "sys.exit(3)\n"
    )
    _write_scanner_config(tmp_path, "creator", f"{sys.executable} {script}", timeout=20)

    payload, rc = scanners_mod._scanners_run_payload(target=tmp_path, scanner_id="creator", force=True)

    assert rc == 1
    assert payload["runs"][0]["status"] == "failed"
    assert not helpers._imports_path(tmp_path).exists()


def test_pre_run_snapshot_detects_torn_same_uid_write(tmp_path, monkeypatch):
    """#1207: a same-UID writer mutating the inode mid-read must be detected."""
    from brigade.work_cmd import scanners as scanners_mod

    inbox = helpers._imports_path(tmp_path)
    inbox.parent.mkdir(parents=True)
    inbox.write_bytes(b'{"id": "r1"}\n')

    real_fdopen = os.fdopen
    mutated = {"done": False}

    def fake_fdopen(fd, mode="rb", *args, **kwargs):
        handle = real_fdopen(fd, mode, *args, **kwargs)
        original_read = handle.read

        def read(size=-1):
            chunk = original_read(size)
            if chunk and not mutated["done"]:
                mutated["done"] = True
                with open(inbox, "r+b") as writer:
                    writer.seek(0)
                    writer.write(b"B" * 32)
                    writer.flush()
            return chunk

        handle.read = read
        return handle

    monkeypatch.setattr(scanners_mod.os, "fdopen", fake_fdopen)

    with pytest.raises(OSError, match="changed while snapshotting"):
        scanners_mod._snapshot_scanner_inbox(tmp_path)


def test_rollback_refuses_inbox_swapped_since_snapshot(tmp_path):
    """#1207: rollback must refuse when the live inbox no longer matches its snapshot."""
    from brigade.work_cmd import ledger, scanners as scanners_mod

    before_row = ledger._make_import("Legit prior finding", kind="finding", source="handoff-ingest")
    ledger._write_imports(tmp_path, [before_row])

    snapshot = scanners_mod._snapshot_scanner_inbox(tmp_path)

    replacement = ledger._make_import("Replacement finding", kind="finding", source="handoff-ingest")
    ledger._write_imports(tmp_path, [replacement])

    with pytest.raises(OSError):
        scanners_mod._restore_scanner_inbox_snapshot_direct(tmp_path, snapshot)


def test_scanner_inbox_descriptor_bytes_enforces_byte_cap(tmp_path, monkeypatch):
    """#1207: rollback descriptor reads enforce the cap while reading, not up front."""
    from brigade.work_cmd import scanners as scanners_mod

    monkeypatch.setattr(scanners_mod, "_SCANNER_INPUT_TOTAL_LIMIT_BYTES", 16)
    inbox = helpers._imports_path(tmp_path)
    inbox.parent.mkdir(parents=True)
    inbox.write_bytes(b"a" * 64)

    descriptor = scanners_mod._open_scanner_inbox(tmp_path, os.O_RDONLY)
    try:
        with pytest.raises(scanners_mod.ScannerInputLimitExceeded):
            scanners_mod._scanner_inbox_descriptor_bytes(descriptor)
    finally:
        os.close(descriptor)


def test_ledger_import_inbox_snapshot_enforces_byte_cap(tmp_path, monkeypatch):
    """#1207: the ordinary import snapshot also bounds its chunk accumulation."""
    from brigade.work_cmd import ledger

    big = [{"id": f"r{index}", "kind": "task", "source": "x", "text": "y" * 32} for index in range(8)]
    ledger._write_imports(tmp_path, big)

    monkeypatch.setattr(ledger, "_IMPORT_INBOX_SNAPSHOT_LIMIT_BYTES", 16)

    with pytest.raises(ledger.ImportInboxSnapshotLimitExceeded):
        ledger._snapshot_import_inbox(tmp_path)


def test_ledger_import_inbox_snapshot_detects_mid_read_append(tmp_path, monkeypatch):
    """#1207 round 2: the before/after compare must cover size and mtime, not only inode fields."""
    from brigade.work_cmd import ledger

    inbox = helpers._imports_path(tmp_path)
    inbox.parent.mkdir(parents=True)
    inbox.write_bytes(b"A" * 64)

    real_read = os.read
    appended = {"done": False}

    def fake_read(fd, size):
        chunk = real_read(fd, size)
        if chunk and not appended["done"]:
            appended["done"] = True
            with open(inbox, "ab") as writer:
                writer.write(b"B" * 16)
        return chunk

    monkeypatch.setattr(os, "read", fake_read)

    with pytest.raises(OSError, match="changed while snapshotting"):
        ledger._snapshot_import_inbox(tmp_path)


def test_inbox_replacement_between_snapshot_and_launch_aborts_run(tmp_path, monkeypatch):
    """#1207 round 2: an inbox swapped after its snapshot aborts before the scanner launches."""
    from brigade.work_cmd import ledger, scanners as scanners_mod

    before_row = ledger._make_import("Legit prior finding", kind="finding", source="handoff-ingest")
    ledger._write_imports(tmp_path, [before_row])

    launched: list[bool] = []
    real_run_one = scanners_mod._scanner_run_one
    real_snapshot = scanners_mod._snapshot_scanner_inbox

    def spy_run_one(*args, **kwargs):
        launched.append(True)
        return real_run_one(*args, **kwargs)

    def replacing_snapshot(target):
        snapshot = real_snapshot(target)
        replacement = ledger._make_import("Rogue finding", kind="finding", source="handoff-ingest")
        ledger._write_imports(target, [replacement])
        return snapshot

    monkeypatch.setattr(scanners_mod, "_scanner_run_one", spy_run_one)
    monkeypatch.setattr(scanners_mod, "_snapshot_scanner_inbox", replacing_snapshot)

    script = tmp_path / "ok.py"
    script.write_text("import sys\nsys.exit(0)\n")
    _write_scanner_config(tmp_path, "swap-race", f"{sys.executable} {script}", timeout=20)

    payload, rc = scanners_mod._scanners_run_payload(target=tmp_path, scanner_id="swap-race", force=True)

    assert rc == 2
    assert launched == []
    assert any("changed" in str(error).lower() for error in payload["errors"])


def test_same_inode_write_between_snapshot_and_launch_aborts_run(tmp_path, monkeypatch):
    """#1207 round 2: a same-inode in-place rewrite after snapshot aborts before launch."""
    from brigade.work_cmd import ledger, scanners as scanners_mod

    before_row = ledger._make_import("Legit prior finding", kind="finding", source="handoff-ingest")
    ledger._write_imports(tmp_path, [before_row])

    launched: list[bool] = []
    real_run_one = scanners_mod._scanner_run_one
    real_snapshot = scanners_mod._snapshot_scanner_inbox

    def spy_run_one(*args, **kwargs):
        launched.append(True)
        return real_run_one(*args, **kwargs)

    def mutating_snapshot(target):
        snapshot = real_snapshot(target)
        with open(helpers._imports_path(target), "ab") as handle:
            handle.write(b'{"id": "concurrent-1", "kind": "task", "source": "x", "text": "y"}\n')
        return snapshot

    monkeypatch.setattr(scanners_mod, "_scanner_run_one", spy_run_one)
    monkeypatch.setattr(scanners_mod, "_snapshot_scanner_inbox", mutating_snapshot)

    script = tmp_path / "ok.py"
    script.write_text("import sys\nsys.exit(0)\n")
    _write_scanner_config(tmp_path, "append-race", f"{sys.executable} {script}", timeout=20)

    payload, rc = scanners_mod._scanners_run_payload(target=tmp_path, scanner_id="append-race", force=True)

    assert rc == 2
    assert launched == []
    assert any("changed" in str(error).lower() for error in payload["errors"])


def test_scanner_inbox_run_lock_is_reentrant_per_process(tmp_path):
    """#1207 round 2: nested writer paths inside one run must not self-deadlock."""
    from brigade.work_cmd import scanners as scanners_mod

    with scanners_mod._scanner_inbox_run_lock(tmp_path):
        with scanners_mod._scanner_inbox_run_lock(tmp_path):
            pass


@pytest.mark.skipif(os.name != "posix" or not hasattr(sys, "executable"), reason="requires POSIX flock")
def test_scanner_inbox_run_lock_blocks_other_process_writers(tmp_path):
    """#1207 round 2: the run-wide inbox lock excludes honest writers in other processes."""
    import subprocess

    from brigade.work_cmd import scanners as scanners_mod

    lock_path = scanners_mod._scanner_inbox_lock_path(tmp_path)
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import errno, fcntl, os, sys\n"
        f"lock = {str(lock_path)!r}\n"
        "fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)\n"
        "try:\n"
        "    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "except OSError as exc:\n"
        "    sys.exit(0 if exc.errno in (errno.EACCES, errno.EAGAIN) else 1)\n"
        "sys.exit(2)\n"
    )

    with scanners_mod._scanner_inbox_run_lock(tmp_path):
        blocked = subprocess.run([sys.executable, str(probe)], capture_output=True, text=True)
    assert blocked.returncode == 0, f"lock did not block the other process: {blocked.stderr}"

    released = subprocess.run([sys.executable, str(probe)], capture_output=True, text=True)
    assert released.returncode == 2, f"lock still held after release: {released.stderr}"


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX symlinks")
def test_scanner_inbox_run_lock_refuses_planted_symlink_lock_file(tmp_path):
    """#1207 round 3: a symlink planted at the lock path must be refused."""
    from brigade.work_cmd import scanners as scanners_mod

    lock_path = scanners_mod._scanner_inbox_lock_path(tmp_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"")
    lock_path.symlink_to(outside)

    with pytest.raises(OSError):
        with scanners_mod._scanner_inbox_run_lock(tmp_path):
            pass

    assert outside.read_bytes() == b"", "the planted link target was locked or written"


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX fifos")
def test_scanner_inbox_run_lock_refuses_planted_fifo_lock_file(tmp_path):
    """#1207 round 3: a FIFO planted at the lock path must be refused."""
    from brigade.work_cmd import scanners as scanners_mod

    lock_path = scanners_mod._scanner_inbox_lock_path(tmp_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(lock_path)

    with pytest.raises(OSError):
        with scanners_mod._scanner_inbox_run_lock(tmp_path):
            pass

    assert lock_path.is_fifo(), "the FIFO was replaced by a regular lock file"


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX hard links")
def test_scanner_inbox_run_lock_refuses_multi_linked_lock_file(tmp_path):
    """#1207 round 3: a multi-linked lock file must be refused."""
    from brigade.work_cmd import scanners as scanners_mod

    lock_path = scanners_mod._scanner_inbox_lock_path(tmp_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    twin = tmp_path / "twin.lock"
    twin.write_bytes(b"")
    os.link(twin, lock_path)

    with pytest.raises(OSError):
        with scanners_mod._scanner_inbox_run_lock(tmp_path):
            pass

    assert twin.read_bytes() == b""


def test_scanner_inbox_run_lock_detects_replaced_lock_file_during_run(tmp_path):
    """#1207 round 3: replacing the locked inode mid-run refuses protected writes."""
    from brigade.work_cmd import scanners as scanners_mod

    lock_path = scanners_mod._scanner_inbox_lock_path(tmp_path)

    with scanners_mod._scanner_inbox_run_lock(tmp_path):
        held_identity = os.stat(lock_path, follow_symlinks=False)
        lock_path.unlink()
        replacement = tmp_path / "replacement.lock"
        replacement.write_bytes(b"")
        os.replace(replacement, lock_path)
        assert os.stat(lock_path, follow_symlinks=False).st_ino != held_identity.st_ino

        with pytest.raises(OSError, match="lock"):
            scanners_mod._write_scanner_inbox_bytes(tmp_path, b'{"text":"swap"}\n')


def test_scanner_inbox_run_lock_verify_requires_held_lock(tmp_path):
    """#1207 round 3: protected-write revalidation fails closed without the lock."""
    from brigade.work_cmd import inbox_lock as inbox_lock_mod
    from brigade.work_cmd import scanners as scanners_mod

    with pytest.raises(OSError, match="not held"):
        inbox_lock_mod.verify_inbox_lock(tmp_path)

    with scanners_mod._scanner_inbox_run_lock(tmp_path):
        inbox_lock_mod.verify_inbox_lock(tmp_path)


def test_cleanup_failure_still_yields_failed_scanner_run_and_unblocks_reruns(tmp_path, monkeypatch):
    """#1207 round 2: a cleanup exception must not bypass the Result or strand a running receipt."""
    from brigade import proc as proc_mod
    from brigade.work_cmd import scanners as scanners_mod

    script = tmp_path / "sleeper.py"
    script.write_text("import time\ntime.sleep(30)\n")
    _write_scanner_config(tmp_path, "cleanup-boom", f"{sys.executable} {script}", timeout=1)

    def exploding_stop(process, registry=None, child_job=None):
        process.kill()
        raise RuntimeError("job terminate exploded")

    monkeypatch.setattr(proc_mod, "_stop_child", exploding_stop)

    payload, rc = scanners_mod._scanners_run_payload(target=tmp_path, scanner_id="cleanup-boom", force=True)

    assert rc == 1
    receipt = payload["runs"][0]
    assert receipt["status"] == "failed"
    persisted = json.loads((helpers._scanner_runs_root(tmp_path) / receipt["run_id"] / "receipt.json").read_text())
    assert persisted["status"] == "failed"

    payload2, rc2 = scanners_mod._scanners_run_payload(target=tmp_path, scanner_id="cleanup-boom", force=True)
    assert rc2 == 1
    assert payload2["runs"][0]["status"] == "failed"


def test_run_one_exception_finalizes_scanner_receipt_as_failed(tmp_path, monkeypatch):
    """#1207 round 2: an exception after the running receipt persists must finalize it failed."""
    from brigade.work_cmd import scanners as scanners_mod

    script = tmp_path / "ok.py"
    script.write_text("import sys\nsys.exit(0)\n")
    scanner = {"id": "supervise-boom", "command": f"{sys.executable} {script}", "timeout": 20}

    def raising_run(*args, **kwargs):
        raise RuntimeError("child supervision exploded")

    monkeypatch.setattr(scanners_mod.proc_mod, "run", raising_run)

    with pytest.raises(RuntimeError, match="child supervision exploded"):
        scanners_mod._scanner_run_one(tmp_path, scanner)

    receipts = scanners_mod._scanner_receipts(tmp_path)
    assert len(receipts) == 1
    assert receipts[0]["status"] == "failed"
    assert scanners_mod._scanner_running_receipts(tmp_path) == []
    assert "child supervision exploded" in str(receipts[0].get("error"))


# --- #1207 round 4: two-lock design (no inherited descriptors) -------------


def test_run_and_writer_lock_files_are_distinct_adjacent_files(tmp_path):
    """Round 4: the run window and each canonical write use separate lock files."""
    from brigade.work_cmd import inbox_lock as inbox_lock_mod
    from brigade.work_cmd import scanners as scanners_mod

    run_path = inbox_lock_mod.inbox_lock_path(tmp_path)
    writer_path = inbox_lock_mod.inbox_writer_lock_path(tmp_path)
    assert run_path.name == "inbox.jsonl.lock"
    assert writer_path.name == "inbox.jsonl.writer.lock"
    assert run_path.parent == writer_path.parent

    with scanners_mod._scanner_inbox_run_lock(tmp_path), inbox_lock_mod.inbox_writer_lock(tmp_path):
        for path in (run_path, writer_path):
            metadata = os.stat(path, follow_symlinks=False)
            assert stat.S_ISREG(metadata.st_mode)
            assert metadata.st_nlink == 1


def test_writer_lock_is_reentrant_per_process_and_refuses_replacement(tmp_path):
    """Round 4: nested writer sections do not self-deadlock; swaps fail closed."""
    from brigade.work_cmd import inbox_lock as inbox_lock_mod

    writer_path = inbox_lock_mod.inbox_writer_lock_path(tmp_path)
    with inbox_lock_mod.inbox_writer_lock(tmp_path):
        with inbox_lock_mod.inbox_writer_lock(tmp_path):
            inbox_lock_mod.verify_inbox_writer_lock(tmp_path)
        held_identity = os.stat(writer_path, follow_symlinks=False)
        replacement = tmp_path / "replacement.writer.lock"
        replacement.write_bytes(b"")
        os.replace(replacement, writer_path)
        assert os.stat(writer_path, follow_symlinks=False).st_ino != held_identity.st_ino
        with pytest.raises(OSError, match="lock"):
            inbox_lock_mod.verify_inbox_writer_lock(tmp_path)


def test_released_inbox_lock_registry_entry_is_removed_and_writers_refuse(tmp_path, monkeypatch):
    """Round 5 (F1): a released lock leaves no registry entry behind."""
    from brigade.work_cmd import inbox_lock as inbox_lock_mod
    from brigade.work_cmd import ledger as ledger_mod

    monkeypatch.setenv(inbox_lock_mod.SCANNER_RUN_ENV, "marked-child")

    with inbox_lock_mod.inbox_writer_lock(tmp_path):
        inbox_lock_mod.verify_inbox_writer_lock(tmp_path)
    with pytest.raises(OSError, match="not held"):
        inbox_lock_mod.verify_inbox_writer_lock(tmp_path)
    with pytest.raises(OSError, match="not held"):
        inbox_lock_mod.verify_canonical_write_locks(tmp_path)

    row = {"text": "unexcluded child row", "kind": "task", "source": "manual"}
    with pytest.raises(OSError, match="not held"):
        ledger_mod._append_import_records_locked(
            target=tmp_path,
            records=[row],
            contain_provenance_errors=True,
        )

    from brigade.work_cmd import scanners as scanners_mod

    with scanners_mod._scanner_inbox_run_lock(tmp_path):
        inbox_lock_mod.verify_inbox_lock(tmp_path)
    with pytest.raises(OSError, match="not held"):
        inbox_lock_mod.verify_inbox_lock(tmp_path)


def test_nested_inbox_lock_entry_persists_until_outermost_exit(tmp_path):
    """Round 5 (F1): reentrant depth keeps the registry entry until outermost exit."""
    from brigade.work_cmd import inbox_lock as inbox_lock_mod

    key = str(inbox_lock_mod.inbox_writer_lock_path(tmp_path))
    with inbox_lock_mod.inbox_writer_lock(tmp_path):
        with inbox_lock_mod.inbox_writer_lock(tmp_path):
            assert key in inbox_lock_mod._ACTIVE_LOCKS
        assert key in inbox_lock_mod._ACTIVE_LOCKS
        inbox_lock_mod.verify_inbox_writer_lock(tmp_path)
    assert key not in inbox_lock_mod._ACTIVE_LOCKS
    with pytest.raises(OSError, match="not held"):
        inbox_lock_mod.verify_inbox_writer_lock(tmp_path)


def test_verify_inbox_lock_fstats_live_descriptor_and_refuses_closed_fd(tmp_path):
    """Round 5 (F1): verify re-stats the live descriptor, not just the path."""
    from brigade.work_cmd import inbox_lock as inbox_lock_mod

    with inbox_lock_mod.inbox_writer_lock(tmp_path) as held:
        restored = os.dup(held.fd)
        os.close(held.fd)
        with pytest.raises(OSError):
            inbox_lock_mod.verify_inbox_writer_lock(tmp_path)
        os.dup2(restored, held.fd)
        os.close(restored)
    assert str(inbox_lock_mod.inbox_writer_lock_path(tmp_path)) not in inbox_lock_mod._ACTIVE_LOCKS


def test_busy_writer_lock_deadline_bounds_launcher_stamping_and_releases_run_lock(tmp_path, monkeypatch):
    """Round 5 (F3): an escaped holder gets a typed failure, never an endless hang."""
    import subprocess
    import time as time_mod

    from brigade.work_cmd import inbox_lock as inbox_lock_mod
    from brigade.work_cmd import scanners as scanners_mod

    monkeypatch.setattr(inbox_lock_mod, "DEFAULT_LOCK_DEADLINE_SECONDS", 0.5)
    src_root = Path(__file__).parents[1] / "src"
    acquired = tmp_path / "holder.acquired"
    script = (
        "import sys, time\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(src_root)!r})\n"
        "from brigade.work_cmd import inbox_lock\n"
        f"target = Path({str(tmp_path)!r})\n"
        f"acquired = Path({str(acquired)!r})\n"
        "with inbox_lock.inbox_writer_lock(target):\n"
        "    acquired.write_text('1')\n"
        "    time.sleep(30)\n"
    )
    child = subprocess.Popen([sys.executable, "-c", script])
    try:
        deadline = time_mod.monotonic() + 15
        while not acquired.exists():
            assert time_mod.monotonic() < deadline, "holder never acquired the writer lock"
            time_mod.sleep(0.02)
        started = time_mod.monotonic()
        with pytest.raises(inbox_lock_mod.InboxLockTimeout):
            with scanners_mod._scanner_inbox_run_lock(tmp_path):
                scanners_mod._write_scanner_inbox_bytes(
                    tmp_path, b'{"id":"stamped","kind":"task","source":"s","text":"t"}\n'
                )
        elapsed = time_mod.monotonic() - started
        assert elapsed < 15, "launcher stamping hung past the lock deadline"

        # Round 6 (H1): the probe below must run while an unrelated holder is
        # still alive; killing the holder first made this probe vacuous.
        released = tmp_path / "runlock.reacquired"
        probe = (
            "import sys\n"
            "from pathlib import Path\n"
            f"sys.path.insert(0, {str(src_root)!r})\n"
            "from brigade.work_cmd import scanners\n"
            f"target = Path({str(tmp_path)!r})\n"
            f"released = Path({str(released)!r})\n"
            "with scanners._scanner_inbox_run_lock(target):\n"
            "    released.write_text('1')\n"
        )
        subprocess.run([sys.executable, "-c", probe], timeout=30, check=True)
        assert released.exists()
        assert child.poll() is None, "holder died before the release probe"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=15)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_scanner_run_reaps_silent_descendant_holding_writer_lock(tmp_path):
    """Round 6 (H1): a descendant that closed the capture pipes and holds the
    writer lock must not outlive a scanner run whose direct child exited 0."""
    import subprocess
    import time as time_mod

    from brigade.work_cmd import scanners as scanners_mod

    src_root = Path(__file__).parents[1] / "src"
    descendant_pid_path = tmp_path / "descendant.pid"
    descendant_code = (
        "import os, sys, time\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(src_root)!r})\n"
        "from brigade.work_cmd import inbox_lock\n"
        f"target = Path({str(tmp_path)!r})\n"
        f"pid_path = Path({str(descendant_pid_path)!r})\n"
        "with inbox_lock.inbox_writer_lock(target):\n"
        "    pid_path.write_text(str(os.getpid()))\n"
        "    time.sleep(120)\n"
    )
    script = tmp_path / "forker.py"
    script.write_text(
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        f"pid_seen = Path({str(descendant_pid_path)!r})\n"
        "subprocess.Popen(\n"
        f"    [sys.executable, '-c', {descendant_code!r}],\n"
        "    stdin=subprocess.DEVNULL,\n"
        "    stdout=subprocess.DEVNULL,\n"
        "    stderr=subprocess.DEVNULL,\n"
        ")\n"
        # Only exit 0 once the silent descendant holds the writer lock.
        "for _ in range(1500):\n"
        "    if pid_seen.exists():\n"
        "        break\n"
        "    time.sleep(0.02)\n"
        "print('child done', flush=True)\n"
    )
    _write_scanner_config(tmp_path, "lock-descendant", f"{sys.executable} {script}", timeout=20)

    payload, rc = scanners_mod._scanners_run_payload(target=tmp_path, scanner_id="lock-descendant", force=True)
    receipt = payload["runs"][0]

    assert rc == 0, receipt
    assert receipt["status"] == "completed", receipt
    assert receipt.get("descendants_reaped") is True, receipt

    deadline = time_mod.monotonic() + 10
    while descendant_pid_path.is_file() and time_mod.monotonic() < deadline:
        try:
            os.kill(int(descendant_pid_path.read_text()), 0)
        except (ProcessLookupError, ValueError):
            break
        time_mod.sleep(0.02)
    descendant_pid = int(descendant_pid_path.read_text())
    try:
        os.kill(descendant_pid, 0)
    except ProcessLookupError:
        pass
    else:
        state = Path(f"/proc/{descendant_pid}/stat").read_text().split()[2]
        assert state == "Z", "escaped descendant survived the scanner run"

    probe = tmp_path / "outside_writer.py"
    outside_acquired = tmp_path / "outside.acquired"
    probe.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(src_root)!r})\n"
        "from brigade.work_cmd import inbox_lock\n"
        f"target = Path({str(tmp_path)!r})\n"
        f"acquired = Path({str(outside_acquired)!r})\n"
        "try:\n"
        "    with inbox_lock.inbox_writer_lock(target, deadline_seconds=5):\n"
        "        acquired.write_text('1')\n"
        "except Exception:\n"
        "    sys.exit(2)\n"
    )
    started = time_mod.monotonic()
    subprocess.run([sys.executable, str(probe)], timeout=30, check=True)
    assert outside_acquired.exists()
    assert time_mod.monotonic() - started < 15, "outside writer could not acquire the writer lock promptly"


def test_ingestion_lock_timeout_marks_run_failed_and_returns_nonzero(tmp_path, monkeypatch):
    """Round 6 (M1): a writer-lock timeout during ingestion must fail the run
    and the command's exit code, not report rc 0 with every import rejected."""
    import contextlib
    import subprocess
    import threading
    import time as time_mod

    from brigade.work_cmd import inbox_lock as inbox_lock_mod
    from brigade.work_cmd import scanners as scanners_mod

    _init_git_repo(tmp_path)
    script = tmp_path / "write_scanner_import.py"
    script.write_text(
        """
import json
from pathlib import Path

path = Path.cwd() / ".brigade" / "scanner-imports.jsonl"
path.write_text(json.dumps({"text": "timeout ingest row", "kind": "task", "source": "scanner-output"}) + "\\n")
"""
    )
    config = tmp_path / ".brigade" / "scanners.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        f'''
[[scanner]]
id = "output-import"
source = "output-import"
command = "{sys.executable} {script}"
cadence = "daily@02:00"
enabled = true
timeout = 30
output_path = ".brigade/output-import.json"
conflict_window = "02:00-02:10"
import_path = ".brigade/scanner-imports.jsonl"
import_format = "jsonl"
'''
    )

    monkeypatch.setattr(inbox_lock_mod, "DEFAULT_LOCK_DEADLINE_SECONDS", 0.5)
    src_root = Path(__file__).parents[1] / "src"
    held_marker = tmp_path / "holder.held"
    holder_script = (
        "import sys, time\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(src_root)!r})\n"
        "from brigade.work_cmd import inbox_lock\n"
        f"target = Path({str(tmp_path)!r})\n"
        f"held = Path({str(held_marker)!r})\n"
        "with inbox_lock.inbox_writer_lock(target):\n"
        "    held.write_text('1')\n"
        "    time.sleep(30)\n"
    )

    real_writer_lock = scanners_mod._scanner_inbox_writer_lock
    holders: list[subprocess.Popen] = []
    run_lock_key = str(inbox_lock_mod.inbox_lock_path(tmp_path))

    @contextlib.contextmanager
    def busy_during_ingestion(target):
        """Grab the writer lock from an outside process, but only for the
        launcher's ingestion acquisition (stamping runs under the run lock and
        must proceed untouched)."""
        if run_lock_key in inbox_lock_mod._ACTIVE_LOCKS:
            with real_writer_lock(target):
                yield
            return
        holder = subprocess.Popen([sys.executable, "-c", holder_script])
        holders.append(holder)
        try:
            deadline = time_mod.monotonic() + 15
            while not held_marker.exists():
                assert time_mod.monotonic() < deadline, "outside holder never took the writer lock"
                time_mod.sleep(0.02)
            with real_writer_lock(target):
                yield
        finally:
            if holder.poll() is None:
                holder.kill()
                holder.wait(timeout=15)

    monkeypatch.setattr(scanners_mod, "_scanner_inbox_writer_lock", busy_during_ingestion)

    outcome: dict[str, object] = {}

    def runner():
        try:
            outcome["rc"] = work_cmd.scanners_run(
                target=tmp_path, scanner_id="output-import", force=True, ingest_output=True, json_output=True
            )
        except BaseException as exc:
            outcome["exc"] = exc

    worker = threading.Thread(target=runner)
    worker.start()
    worker.join(timeout=120)
    assert not worker.is_alive(), "scanner run never finished"
    assert "exc" not in outcome, outcome.get("exc")

    rc = outcome["rc"]
    assert rc != 0, f"ingestion lock timeout returned success: {rc}"
    payload_runs = scanners_mod._scanner_receipts(tmp_path)
    assert payload_runs, "no scanner receipts recorded"
    receipt = [item for item in payload_runs if item.get("scanner_id") == "output-import"][0]
    assert receipt["status"] == "failed", receipt
    assert "writer lock" in str(receipt.get("error")), receipt


def test_overlapping_thread_acquisitions_share_one_owner_close_once_and_stay_exclusive(tmp_path, monkeypatch):
    """Round 6 (L1): depth and ownership live in one registry entry.

    An inner thread context must stay protected when the outer context exits
    first, the fd closes exactly once at outermost exit, and concurrent first
    acquisitions serialize instead of exposing a half-initialized entry.
    """
    import threading

    from brigade.work_cmd import inbox_lock as inbox_lock_mod

    key = str(inbox_lock_mod.inbox_writer_lock_path(tmp_path))
    released_fds: list[int] = []
    original_release = inbox_lock_mod._HeldInboxLock.release

    def spying_release(self):
        released_fds.append(self.fd)
        original_release(self)

    monkeypatch.setattr(inbox_lock_mod._HeldInboxLock, "release", spying_release)

    outer_entered = threading.Event()
    outer_exit = threading.Event()
    inner_entered = threading.Event()
    inner_exit = threading.Event()

    def outer():
        with inbox_lock_mod.inbox_writer_lock(tmp_path):
            outer_entered.set()
            assert outer_exit.wait(timeout=30)

    def inner():
        with inbox_lock_mod.inbox_writer_lock(tmp_path):
            inner_entered.set()
            assert inner_exit.wait(timeout=30)

    thread_outer = threading.Thread(target=outer)
    thread_inner = threading.Thread(target=inner)
    thread_outer.start()
    assert outer_entered.wait(timeout=30)
    held_fd = inbox_lock_mod._ACTIVE_LOCKS[key].owner.fd
    thread_inner.start()

    deadline = __import__("time").monotonic() + 30
    while not inner_entered.is_set() and __import__("time").monotonic() < deadline:
        __import__("time").sleep(0.01)
    assert inner_entered.is_set(), "inner acquisition never entered"

    # The outer context exits FIRST while the inner one is still inside.
    outer_exit.set()
    thread_outer.join(timeout=30)

    assert key in inbox_lock_mod._ACTIVE_LOCKS, "outer exit dropped the entry while an inner context held it"
    inbox_lock_mod.verify_inbox_writer_lock(tmp_path)
    assert inner_entered.is_set()

    # Cross-process exclusion must still hold while the inner context runs.
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import errno, fcntl, os, sys\n"
        f"lock = {str(inbox_lock_mod.inbox_writer_lock_path(tmp_path))!r}\n"
        "fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)\n"
        "try:\n"
        "    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "except OSError as exc:\n"
        "    sys.exit(0 if exc.errno in (errno.EACCES, errno.EAGAIN) else 1)\n"
        "sys.exit(2)\n"
    )
    import subprocess

    blocked = subprocess.run([sys.executable, str(probe)], timeout=30)
    assert blocked.returncode == 0, "inner context lost cross-process exclusion after outer exit"

    inner_exit.set()
    thread_inner.join(timeout=30)
    assert key not in inbox_lock_mod._ACTIVE_LOCKS
    assert released_fds == [held_fd], f"fd closed {len(released_fds)} times: {released_fds}"

    # Concurrent first acquisitions must serialize their initialization.
    errors: list[BaseException] = []
    barrier = threading.Barrier(6)

    def racer():
        barrier.wait(timeout=30)
        try:
            with inbox_lock_mod.inbox_writer_lock(tmp_path):
                inbox_lock_mod.verify_inbox_writer_lock(tmp_path)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=racer) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not errors, errors
    assert key not in inbox_lock_mod._ACTIVE_LOCKS
    # Exactly one additional acquisition/release cycle came out of the race.
    assert len(released_fds) == 2, released_fds


def test_outside_canonical_writer_gets_typed_failure_when_writer_lock_busy(tmp_path, monkeypatch):
    """Round 5 (F3): outside writers fail typed instead of blocking forever."""
    import subprocess
    import time as time_mod

    from brigade.work_cmd import inbox_lock as inbox_lock_mod
    from brigade.work_cmd import ledger as ledger_mod

    monkeypatch.setattr(inbox_lock_mod, "DEFAULT_LOCK_DEADLINE_SECONDS", 0.5)
    src_root = Path(__file__).parents[1] / "src"
    acquired = tmp_path / "holder.acquired"
    script = (
        "import sys, time\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(src_root)!r})\n"
        "from brigade.work_cmd import inbox_lock\n"
        f"target = Path({str(tmp_path)!r})\n"
        f"acquired = Path({str(acquired)!r})\n"
        "with inbox_lock.inbox_writer_lock(target):\n"
        "    acquired.write_text('1')\n"
        "    time.sleep(8)\n"
    )
    child = subprocess.Popen([sys.executable, "-c", script])
    try:
        deadline = time_mod.monotonic() + 15
        while not acquired.exists():
            assert time_mod.monotonic() < deadline, "holder never acquired the writer lock"
            time_mod.sleep(0.02)
        started = time_mod.monotonic()
        with pytest.raises(inbox_lock_mod.InboxLockTimeout):
            ledger_mod._append_import_records(
                target=tmp_path,
                records=[{"text": "blocked outsider row", "kind": "task", "source": "manual"}],
            )
        assert time_mod.monotonic() - started < 15, "outside writer hung past the lock deadline"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=15)


def test_stamping_takes_writer_lock_before_reading_and_keeps_child_rows(tmp_path, monkeypatch):
    """Round 5 (F2): stamping snapshots under the writer lock; late child rows survive."""
    import subprocess
    import threading
    import time as time_mod

    from brigade.work_cmd import inbox_lock as inbox_lock_mod
    from brigade.work_cmd import ledger as ledger_mod
    from brigade.work_cmd import scanners as scanners_mod

    scanner = _builtin_scanner("handoff-ingest")
    run = _verified_builtin_scanner_run(scanner)
    seeded = work_cmd.ledger._make_import("seed row", kind="finding", source="handoff-ingest")
    ledger_mod._write_imports(tmp_path, [seeded])
    inbox = work_cmd.helpers._imports_path(tmp_path)
    before_raw = inbox.read_bytes()

    src_root = Path(__file__).parents[1] / "src"
    ready = tmp_path / "child.ready"
    go = tmp_path / "child.go"
    appended = tmp_path / "child.appended"
    script = (
        "import os, sys, time\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(src_root)!r})\n"
        "from brigade.work_cmd import inbox_lock, ledger\n"
        f"target = Path({str(tmp_path)!r})\n"
        f"ready = Path({str(ready)!r})\n"
        f"go = Path({str(go)!r})\n"
        f"appended = Path({str(appended)!r})\n"
        f"os.environ[{inbox_lock_mod.SCANNER_RUN_ENV!r}] = 'race-child'\n"
        "with inbox_lock.inbox_writer_lock(target):\n"
        "    ready.write_text('1')\n"
        "    for _ in range(750):\n"
        "        if go.exists():\n"
        "            break\n"
        "        time.sleep(0.02)\n"
        "imported, _s, _sd, rejected = ledger._append_import_records(\n"
        "    target=target,\n"
        "    records=[{'text': 'child race row', 'kind': 'task', 'source': 'manual'}],\n"
        "    contain_provenance_errors=True,\n"
        ")\n"
        "appended.write_text('1' if imported and not rejected else '0')\n"
    )
    child = subprocess.Popen([sys.executable, "-c", script])
    try:
        deadline = time_mod.monotonic() + 15
        while not ready.exists():
            assert time_mod.monotonic() < deadline, "child never took the writer lock"
            time_mod.sleep(0.02)

        observed: list[bool] = []
        original_read = scanners_mod._scanner_inbox_bytes
        writer_key = str(inbox_lock_mod.inbox_writer_lock_path(tmp_path))

        def spy_read(target):
            observed.append(writer_key in inbox_lock_mod._ACTIVE_LOCKS)
            return original_read(target)

        monkeypatch.setattr(scanners_mod, "_scanner_inbox_bytes", spy_read)

        outcome: dict[str, object] = {}

        def stamp():
            try:
                outcome["stamped"] = scanners_mod._scanner_stamp_new_imports(
                    target=tmp_path,
                    scanner=scanner,
                    run=run,
                    before_ids={str(seeded["id"])},
                    before_imports=[dict(seeded)],
                    before_raw=before_raw,
                )
            except BaseException as exc:  # pragma: no cover - surfaced below
                outcome["exc"] = exc

        worker = threading.Thread(target=stamp)
        worker.start()
        time_mod.sleep(0.5)
        go.write_text("1")
        child.wait(timeout=30)
        worker.join(timeout=60)
        assert not worker.is_alive(), "stamping never finished"
        assert "exc" not in outcome, outcome.get("exc")
        assert appended.read_text() == "1"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=15)

    assert observed, "stamping never read the inbox"
    assert all(observed), f"canonical snapshot read outside the writer lock: {observed}"
    final_texts = [json.loads(row).get("text") for row in inbox.read_bytes().splitlines() if row.strip()]
    assert "seed row" in final_texts, final_texts
    assert "child race row" in final_texts, f"concurrent child row overwritten: {final_texts!r}"
    assert outcome["stamped"], "stamping produced no rows"


def test_ingestion_pre_reads_under_writer_lock_and_concurrent_commit_survives_rollback(tmp_path, monkeypatch):
    """Round 6 (M3): the writer lock spans snapshot through receipt rollback.

    A trusted writer that commits while receipt publication fails must survive
    the stale-snapshot rollback: either the lock is retained across the whole
    window (fixed) or a commit in the release gap is deleted (the bug).
    """
    import threading
    import time as time_mod

    from brigade.work_cmd import inbox_lock as inbox_lock_mod
    from brigade.work_cmd import scanners as scanners_mod

    _init_git_repo(tmp_path)
    inbox = tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl"
    inbox.parent.mkdir(parents=True)
    inbox.write_bytes(b'{"id":"seed","text":"seed row","kind":"task","source":"manual"}\n')
    script = tmp_path / "write_scanner_import.py"
    script.write_text(
        """
import json
from pathlib import Path

Path.cwd().joinpath(".brigade", "scanner.launched").write_text("1")
path = Path.cwd() / ".brigade" / "scanner-imports.jsonl"
path.write_text(json.dumps({"text": "ingest row", "kind": "task", "source": "scanner-output"}) + "\\n")
"""
    )
    config = tmp_path / ".brigade" / "scanners.toml"
    config.write_text(
        f'''
[[scanner]]
id = "output-import"
source = "output-import"
command = "{sys.executable} {script}"
cadence = "daily@02:00"
enabled = true
timeout = 30
output_path = ".brigade/output-import.json"
conflict_window = "02:00-02:10"
import_path = ".brigade/scanner-imports.jsonl"
import_format = "jsonl"
'''
    )

    observed: list[bool] = []
    writer_key = str(inbox_lock_mod.inbox_writer_lock_path(tmp_path))
    launched = tmp_path / ".brigade" / "scanner.launched"
    original_read = scanners_mod._scanner_inbox_bytes

    def spy_read(target):
        # The pre-run snapshot is legitimately run-lock-only; only reads after
        # the scanner child launched (stamping, ingestion) must hold the writer
        # lock.
        if launched.exists():
            observed.append(writer_key in inbox_lock_mod._ACTIVE_LOCKS)
        return original_read(target)

    monkeypatch.setattr(scanners_mod, "_scanner_inbox_bytes", spy_read)

    state: dict[str, bool] = {"fail_next_receipt": False}
    real_write_file = scanners_mod._write_scanner_run_file

    def maybe_failing_write_file(authority, name, data, **kwargs):
        if name == "receipt.json" and state.get("fail_next_receipt"):
            state["fail_next_receipt"] = False
            raise OSError("receipt boom")
        return real_write_file(authority, name, data, **kwargs)

    monkeypatch.setattr(scanners_mod, "_write_scanner_run_file", maybe_failing_write_file)

    go = threading.Event()
    committed = threading.Event()
    real_receipt = scanners_mod._write_scanner_run_receipt

    def coordinating_receipt(run):
        if isinstance(run.get("ingest_output"), dict) and not go.is_set():
            # The ingestion receipt is being published. Let an outside trusted
            # writer race for the gap between the publication snapshot and this
            # receipt's failure/rollback.
            state["fail_next_receipt"] = True
            go.set()
            go_file.write_text("1")
            committed.wait(timeout=3)
        return real_receipt(run)

    monkeypatch.setattr(scanners_mod, "_write_scanner_run_receipt", coordinating_receipt)

    survivor_row = b'{"id":"survivor-1","text":"gap survivor","kind":"task","source":"trusted-peer"}\n'
    # The racing peer is another process: within this process the writer lock
    # is deliberately reentrant, so only an outside writer models a commit
    # that must survive the launcher's publication window.
    import subprocess

    src_root = Path(__file__).parents[1] / "src"
    go_file = tmp_path / "peer.go"
    done_file = tmp_path / "peer.committed"
    peer_script = (
        "import sys, time\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(src_root)!r})\n"
        "from brigade.work_cmd import inbox_lock\n"
        f"target = Path({str(tmp_path)!r})\n"
        f"go = Path({str(go_file)!r})\n"
        f"done = Path({str(done_file)!r})\n"
        f"row = {survivor_row!r}\n"
        "while not go.exists():\n"
        "    time.sleep(0.02)\n"
        "inbox = target / '.brigade' / 'work' / 'imports' / 'inbox.jsonl'\n"
        "deadline = time.monotonic() + 20\n"
        "while time.monotonic() < deadline:\n"
        "    try:\n"
        "        with inbox_lock.inbox_writer_lock(target, deadline_seconds=0.5):\n"
        "            with inbox.open('ab') as handle:\n"
        "                handle.write(row)\n"
        "            done.write_text('1')\n"
        "            sys.exit(0)\n"
        "    except inbox_lock.InboxLockTimeout:\n"
        "        continue\n"
        "sys.exit(3)\n"
    )

    peer = subprocess.Popen([sys.executable, "-c", peer_script])
    outcome: dict[str, object] = {}

    def runner():
        try:
            outcome["rc"] = work_cmd.scanners_run(
                target=tmp_path, scanner_id="output-import", force=True, ingest_output=True, json_output=True
            )
        except BaseException as exc:
            outcome["exc"] = exc

    def peer_committed(timeout: float) -> bool:
        deadline = time_mod.monotonic() + timeout
        while time_mod.monotonic() < deadline:
            if done_file.exists():
                return True
            time_mod.sleep(0.02)
        return False

    worker = threading.Thread(target=runner)
    worker.start()

    def go_signal():
        assert go.wait(timeout=60)
        assert peer_committed(60)

    signal_thread = threading.Thread(target=go_signal)
    signal_thread.start()
    worker.join(timeout=120)
    signal_thread.join(timeout=120)
    assert not worker.is_alive(), "scanner run never finished"
    try:
        assert peer.wait(timeout=30) == 0, "trusted peer never committed"
        assert done_file.exists()
        assert isinstance(outcome.get("exc"), OSError), outcome
        assert "receipt boom" in str(outcome["exc"])

        assert observed, "ingestion never read the inbox"
        assert all(observed), f"dedup/rollback snapshot read outside the writer lock: {observed}"
        final_texts = [json.loads(row).get("text") for row in inbox.read_bytes().splitlines() if row.strip()]
        assert "seed row" in final_texts, final_texts
        # This transaction was rolled back after its append, so its own row is gone...
        assert "ingest row" not in final_texts, final_texts
        # ...but the peer's commit in the same window must never be deleted.
        assert "gap survivor" in final_texts, f"concurrent commit deleted by rollback: {final_texts!r}"
    finally:
        if peer.poll() is None:
            peer.kill()
            peer.wait(timeout=15)


def test_scanner_children_receive_capability_free_marker_and_no_descriptors(tmp_path, monkeypatch):
    """Round 4 (F1/F2): children get a marker env var, never a lock descriptor."""
    import inspect

    from brigade import proc as proc_mod
    from brigade.work_cmd import inbox_lock as inbox_lock_mod
    from brigade.work_cmd import scanners as scanners_mod

    source = Path(inbox_lock_mod.__file__).read_text()
    assert "BRIGADE_INBOX_LOCK_FD" not in source
    assert not hasattr(inbox_lock_mod, "_adopt_inherited_lock")
    assert not hasattr(inbox_lock_mod, "child_lock_env")
    assert not hasattr(inbox_lock_mod, "child_lock_pass_fds")
    assert "pass_fds" not in inspect.getsource(proc_mod.run)

    script = tmp_path / "ok.py"
    script.write_text("import sys\nsys.exit(0)\n")
    _write_scanner_config(tmp_path, "env-probe", f"{sys.executable} {script}", timeout=20)

    captured: dict[str, object] = {}
    real_run = scanners_mod.proc_mod.run

    def spying_run(argv, *, timeout, env, cwd, **kwargs):
        captured["env"] = dict(env or {})
        captured["kwargs"] = kwargs
        return real_run(argv, timeout=timeout, env=env, cwd=cwd, **kwargs)

    monkeypatch.setattr(scanners_mod.proc_mod, "run", spying_run)
    payload, rc = scanners_mod._scanners_run_payload(target=tmp_path, scanner_id="env-probe", force=True)
    assert rc == 0, payload.get("errors")
    child_env = captured["env"]
    assert isinstance(child_env, dict)
    assert "BRIGADE_INBOX_LOCK_FD" not in child_env
    assert child_env["BRIGADE_SCANNER_RUN_ID"] == payload["runs"][0]["run_id"]
    assert "pass_fds" not in captured["kwargs"]


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX flock and fd passing")
def test_unlocking_any_inherited_descriptor_cannot_release_launcher_run_lock(tmp_path):
    """Round 4 (F1): LOCK_UN on an inherited fd must never drop the run window.

    The launcher's exclusion lives on its own open file description of the run
    lock, which is no longer shared with anyone. A hostile child that unlocks
    whatever it is handed leaves the run lock held.
    """
    import subprocess

    from brigade.work_cmd import scanners as scanners_mod

    lock_path = scanners_mod._scanner_inbox_lock_path(tmp_path)
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import errno, fcntl, os, sys\n"
        f"lock = {str(lock_path)!r}\n"
        "fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)\n"
        "try:\n"
        "    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "except OSError as exc:\n"
        "    sys.exit(0 if exc.errno in (errno.EACCES, errno.EAGAIN) else 1)\n"
        "sys.exit(2)\n"
    )
    unlocker = tmp_path / "unlocker.py"
    unlocker.write_text(
        "import fcntl, sys\n"
        "try:\n"
        "    fcntl.flock(int(sys.argv[1]), fcntl.LOCK_UN)\n"
        "except OSError:\n"
        "    pass\n"
        "sys.exit(0)\n"
    )
    dummy = os.open(os.devnull, os.O_RDWR)
    try:
        child = subprocess.Popen(
            [sys.executable, str(unlocker), str(dummy)],
            pass_fds=(dummy,),
            env={**os.environ, "BRIGADE_INBOX_LOCK_FD": str(dummy)},
        )
        with scanners_mod._scanner_inbox_run_lock(tmp_path):
            assert child.wait(timeout=15) == 0
            blocked = subprocess.run([sys.executable, str(probe)], capture_output=True, text=True)
            assert blocked.returncode == 0, "child unlocked the launcher's run lock"
        released = subprocess.run([sys.executable, str(probe)], capture_output=True, text=True)
        assert released.returncode == 2, "run lock still held after release"
    finally:
        os.close(dummy)


@pytest.mark.skipif(os.name != "posix", reason="uses POSIX flock probes for timing")
def test_launcher_stamping_waits_for_child_writer_lock(tmp_path):
    """Round 4 (F2): launcher writes serialize behind a self-importing child.

    A child holding only the writer lock blocks the launcher's stamping write;
    neither can touch the other's run-window state and siblings exclude each
    other on their own open file descriptions.
    """
    import subprocess
    import time as time_mod

    from brigade.work_cmd import scanners as scanners_mod

    src_root = Path(__file__).parents[1] / "src"
    acquired = tmp_path / "writer.acquired.marker"
    released = tmp_path / "writer.released.marker"
    script = (
        "import sys, time\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(src_root)!r})\n"
        "from brigade.work_cmd import inbox_lock\n"
        f"target = Path({str(tmp_path)!r})\n"
        f"acquired = Path({str(acquired)!r})\n"
        f"released = Path({str(released)!r})\n"
        "with inbox_lock.inbox_writer_lock(target):\n"
        "    acquired.write_text('1')\n"
        "    time.sleep(1.2)\n"
        "released.write_text('1')\n"
    )
    child = subprocess.Popen([sys.executable, "-c", script])
    try:
        deadline = time_mod.monotonic() + 15
        while not acquired.exists():
            assert time_mod.monotonic() < deadline, "child never acquired the writer lock"
            time_mod.sleep(0.02)
        started = time_mod.monotonic()
        with scanners_mod._scanner_inbox_run_lock(tmp_path):
            scanners_mod._write_scanner_inbox_bytes(
                tmp_path, b'{"id":"stamped","kind":"task","source":"s","text":"t"}\n'
            )
        elapsed = time_mod.monotonic() - started
        assert elapsed >= 1.0, "launcher stamping did not wait for the child writer lock"
        child.wait(timeout=15)
        assert released.exists()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=15)


def test_every_canonical_writer_follows_task_then_run_then_writer_order(tmp_path, monkeypatch):
    """Round 4 (F3): every caller takes task-ledger -> run -> writer, in order."""
    import contextlib

    from brigade.work_cmd import inbox_lock as inbox_lock_mod
    from brigade.work_cmd import ledger as ledger_mod
    from brigade.work_cmd import scanners as scanners_mod

    order = {"task": 0, "run": 1, "writer": 2}
    events: list[str] = []
    held: dict[str, int] = {}

    def wrap(name, original):
        @contextlib.contextmanager
        def wrapped(*args, **kwargs):
            # Only acquisitions that could block count: re-entering a lock
            # this process already holds can never wait on another writer.
            if not held.get(name):
                violations = sorted(n for n, depth in held.items() if depth and order[n] > order[name])
                assert not violations, (
                    f"lock order violation: {name} acquired while holding {violations}; recent={events[-8:]}"
                )
            events.append(name)
            held[name] = held.get(name, 0) + 1
            try:
                with original(*args, **kwargs):
                    yield
            finally:
                held[name] -= 1

        return wrapped

    monkeypatch.setattr(inbox_lock_mod, "scanner_inbox_run_lock", wrap("run", inbox_lock_mod.scanner_inbox_run_lock))
    monkeypatch.setattr(inbox_lock_mod, "inbox_writer_lock", wrap("writer", inbox_lock_mod.inbox_writer_lock))
    monkeypatch.setattr(scanners_mod, "_scanner_inbox_run_lock", wrap("run", scanners_mod._scanner_inbox_run_lock))
    monkeypatch.setattr(ledger_mod, "_task_ledger_lock", wrap("task", ledger_mod._task_ledger_lock))

    # Round 6 (M2): a canonical writer's inbox read must happen under the
    # writer lock (or at least the run lock for the launcher's pre-run
    # snapshot), never before its lock window opens.
    real_read_imports = ledger_mod._read_imports

    def checked_read_imports(target):
        if not held.get("writer") and not held.get("run"):
            raise AssertionError(f"inbox read outside canonical locks; recent={events[-8:]}")
        return real_read_imports(target)

    monkeypatch.setattr(ledger_mod, "_read_imports", checked_read_imports)

    clean = work_cmd._make_import("order probe task", kind="task", source="manual")
    imported, _skipped, _dismissed, rejected = ledger_mod._append_import_records(target=tmp_path, records=[clean])
    assert imported and not rejected
    summary = ledger_mod._backfill_import_provenance(tmp_path)
    assert summary.get("stamped", 0) >= 0
    promoted, failed = ledger_mod._promote_matching_imports(tmp_path, kind="task")
    assert promoted and not failed
    with ledger_mod._task_ledger_lock(tmp_path), ledger_mod._canonical_inbox_write(tmp_path):
        kept = ledger_mod._read_imports(tmp_path)
        ledger_mod._write_imports(tmp_path, kept)

    with scanners_mod._scanner_inbox_run_lock(tmp_path):
        scanners_mod._write_scanner_inbox_bytes(
            tmp_path,
            b"".join(json.dumps(item, sort_keys=True).encode("utf-8") + b"\n" for item in kept),
        )

    monkeypatch.setenv(inbox_lock_mod.SCANNER_RUN_ENV, "order-child")
    imported_child, _s, _sd, rejected_child = ledger_mod._append_import_records(
        target=tmp_path,
        records=[{"text": "child row under marker", "kind": "task", "source": "manual"}],
        contain_provenance_errors=True,
    )
    assert imported_child and not rejected_child
    monkeypatch.delenv(inbox_lock_mod.SCANNER_RUN_ENV)

    assert events.count("task") >= 1
    assert events.count("run") >= 2
    assert events.count("writer") >= 5
