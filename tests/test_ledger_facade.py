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
        owner = _owner_module(name)
        original = getattr(owner, name)
        sentinel = object()
        with monkeypatch.context() as patch:
            patch.setattr(ledger, name, sentinel)
            assert getattr(owner, name) is sentinel
        assert getattr(owner, name) is original
        assert getattr(ledger, name) is original


@pytest.mark.parametrize("name", ("os", "scan_handoff_injection_heuristics"))
def test_facade_monkeypatches_broadcast_shared_imports(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    original = getattr(ledger, name)
    sentinel = object()
    with monkeypatch.context() as patch:
        patch.setattr(ledger, name, sentinel)
        for module_name in ledger._SHARED_IMPORTS[name]:
            module = importlib.import_module(f"{ledger.__name__}.{module_name}")
            assert getattr(module, name) is sentinel
    for module_name in ledger._SHARED_IMPORTS[name]:
        module = importlib.import_module(f"{ledger.__name__}.{module_name}")
        assert getattr(module, name) is original
    assert getattr(ledger, name) is original


def test_nested_facade_patches_restore_live_owner_values() -> None:
    name = "_posix_dirfd_available"
    owner = _owner_module(name)
    original = getattr(owner, name)
    with pytest.MonkeyPatch.context() as outer:
        outer.setattr(ledger, name, lambda: False)
        with pytest.MonkeyPatch.context() as direct:
            direct.setattr(owner, name, lambda: True)
            assert getattr(ledger, name)() is True
            with pytest.MonkeyPatch.context() as inner:
                inner.setattr(ledger, name, lambda: False)
                assert getattr(owner, name)() is False
            assert getattr(ledger, name)() is True
        assert getattr(ledger, name)() is False
    assert getattr(owner, name) is original
    assert getattr(ledger, name) is original
    assert getattr(ledger, name)() == original()


def test_shared_imports_read_live_first_binding_and_restore_by_broadcast() -> None:
    name = "scan_handoff_injection_heuristics"
    modules = [importlib.import_module(f"{ledger.__name__}.{part}") for part in ledger._SHARED_IMPORTS[name]]
    original = getattr(ledger, name)
    first, second, broadcast = object(), object(), object()
    with pytest.MonkeyPatch.context() as direct:
        for module in modules:
            direct.setattr(module, name, getattr(module, name))
        direct.setattr(modules[0], name, first)
        direct.setattr(modules[1], name, second)
        assert getattr(ledger, name) is first
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(ledger, name, broadcast)
            assert all(getattr(module, name) is broadcast for module in modules)
            assert name not in vars(ledger)
        # A facade assignment, including rollback, broadcasts its single value.
        assert all(getattr(module, name) is first for module in modules)
    assert getattr(ledger, name) is original


@pytest.mark.parametrize("name", ("_posix_dirfd_available", "scan_handoff_injection_heuristics"))
def test_facade_deletion_reaches_owners_and_missing_names_raise(name: str) -> None:
    modules = (
        [_owner_module(name)]
        if name in ledger._OWNERS
        else [importlib.import_module(f"{ledger.__name__}.{part}") for part in ledger._SHARED_IMPORTS[name]]
    )
    with pytest.MonkeyPatch.context() as patch:
        for module in modules:
            # Register restoration on the real owner before deleting via facade.
            patch.setattr(module, name, getattr(module, name))
        delattr(ledger, name)
        assert all(not hasattr(module, name) for module in modules)
        with pytest.raises(AttributeError, match=name):
            getattr(ledger, name)
        with pytest.raises(AttributeError, match=name):
            delattr(ledger, name)
    assert getattr(ledger, name) is getattr(modules[0], name)
    missing = "missing_facade_attribute"
    with pytest.raises(AttributeError, match=missing):
        getattr(ledger, missing)
    with pytest.raises(AttributeError, match=missing):
        delattr(ledger, missing)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ledger, missing, object(), raising=False)
        assert missing in vars(ledger)
    assert not hasattr(ledger, missing)


def test_cross_module_calls_read_live_owner_attributes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(ledger, "_posix_dirfd_available", lambda: False)
    monkeypatch.setattr(ledger, "_nt_dirfd_available", lambda: False)

    with pytest.raises(OSError, match="descriptor-relative import inbox operations are unavailable"):
        _owner_module("_open_import_inbox_parent")._open_import_inbox_parent(tmp_path, create=False)


def test_facade_shared_import_assignment_reads_back(monkeypatch: pytest.MonkeyPatch) -> None:
    # The facade must not shadow a shared import with its own binding: after
    # assignment, the facade and every seam that binds the name agree.
    for name in ("sys", "Any", "os"):
        sentinel = object()
        monkeypatch.setattr(ledger, name, sentinel)
        assert getattr(ledger, name) is sentinel
        for module_name in ledger._SHARED_IMPORTS[name]:
            assert getattr(importlib.import_module(f"{ledger.__name__}.{module_name}"), name) is sentinel
    monkeypatch.undo()
    import sys as real_sys

    assert ledger.sys is real_sys
