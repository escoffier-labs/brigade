"""Compatibility coverage for the split task and import ledger facade."""

from __future__ import annotations

import importlib
from pathlib import Path
from types import ModuleType

import pytest

from brigade.work_cmd import ledger


LEDGER_NAMES_FIXTURE = Path(__file__).with_name("fixtures") / "ledger_facade_names.txt"

PATCHED_FACADE_NAMES = (
    "_write_persisted_import_proofs",
    "_record_verifier_owned_file",
    "_nt_dirfd_available",
    "_posix_dirfd_available",
    "_dirfd_available",
    "_write_import_inbox_bytes_at",
    "_validate_import_proof_descriptor",
    "_validate_import_record",
    "_stamp_import_provenance",
    "_has_persisted_import_proof",
    "_unwrap_authority_envelope",
    "_remove_persisted_import_proofs",
    "_record_external_directory_authority",
    "_read_external_directory_authority",
    "_open_import_inbox_parent_nt",
    "_open_file_nofollow",
    "_downgrade_durability_checkpoint",
    "_acquire_authenticated_authority_snapshot",
    "_IMPORT_INBOX_SNAPSHOT_LIMIT_BYTES",
    "_UNSIGNED_DEDUPE_DOWNGRADE_WARNED",
)


def _main_ledger_names() -> set[str]:
    # Top-level names from src/brigade/work_cmd/ledger.py at commit 08df401d.
    return set(LEDGER_NAMES_FIXTURE.read_text().splitlines())


def _owner_module(name: str) -> ModuleType:
    owner = ledger._OWNERS[name]
    return importlib.import_module(f"{ledger.__name__}.{owner}")


def test_facade_matches_every_top_level_name_from_main_ledger() -> None:
    expected = _main_ledger_names()
    assert set(ledger._OWNERS) == expected
    for name in expected:
        owner = _owner_module(name)
        assert getattr(ledger, name) is getattr(owner, name)


def test_facade_monkeypatches_forward_to_owning_module(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in PATCHED_FACADE_NAMES:
        sentinel = object()
        monkeypatch.setattr(ledger, name, sentinel)
        assert getattr(_owner_module(name), name) is sentinel


@pytest.mark.parametrize("name", ("os", "scan_handoff_injection_heuristics"))
def test_facade_monkeypatches_broadcast_shared_imports(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    sentinel = object()
    monkeypatch.setattr(ledger, name, sentinel)
    for module_name in ledger._SHARED_IMPORTS[name]:
        module = importlib.import_module(f"{ledger.__name__}.{module_name}")
        assert getattr(module, name) is sentinel


def test_cross_module_calls_read_live_owner_attributes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(ledger, "_posix_dirfd_available", lambda: False)
    monkeypatch.setattr(ledger, "_nt_dirfd_available", lambda: False)

    with pytest.raises(OSError, match="descriptor-relative import inbox operations are unavailable"):
        _owner_module("_open_import_inbox_parent")._open_import_inbox_parent(tmp_path, create=False)
