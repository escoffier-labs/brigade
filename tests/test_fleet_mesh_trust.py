"""Local trust transactions with public deterministic keys and fake identities."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from brigade import dirfd
from brigade import fleet_mesh_crypto as crypto
from brigade import fleet_mesh_statements as statements

HUB = "h_" + crypto.encode_binary(bytes(16))
OTHER = "h_" + crypto.encode_binary(bytes([1]) * 16)
KID = "k1_" + crypto.encode_binary(bytes(16))
KID2 = "k1_" + crypto.encode_binary(bytes([1]) * 16)
SEED = bytes(range(32))
SEED2 = bytes(reversed(range(32)))
TIME = "2026-01-02T00:00:00Z"


def key(seed=SEED):
    return Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes_raw()


def signer(seed=SEED):
    return hashlib.sha256(key(seed)).hexdigest()


def signed(*, hub=HUB, kind="membership", seq=1, seed=SEED, entries=None):
    field = "members" if kind == "membership" else "revocations"
    value = {"v": 1, "hub": hub, "seq": seq, "created_at": TIME, field: entries or []}
    return crypto.canonical(statements.sign_statement(value, seed, expected_kind=kind))


def revoked(*, principal="test-node", kid=KID, cutoff=TIME):
    return {"principal": principal, "kid": kid, "compromised_from": cutoff}


def digest(raw):
    return hashlib.sha256(crypto.canonical(json.loads(raw))).hexdigest()


@pytest.fixture
def trust():
    return importlib.import_module("brigade.fleet_mesh_trust")


@pytest.fixture
def home(tmp_path):
    if not dirfd.posix_available():
        pytest.skip("owner-only trust transactions require POSIX dirfd and flock")
    result = tmp_path / "private"
    result.mkdir(mode=0o700)
    return result


def saved(home):
    return home / "fleet" / "mesh-keys" / "trust-state.json"


def pin(trust, home, *, hub=HUB, seed=SEED, previous=None):
    return trust.pin_manifest_anchor(
        brigade_home=home,
        hub=hub,
        public_key_file=crypto.encode_binary(key(seed)).encode("ascii"),
        expected_fingerprint=crypto.fingerprint(key(seed)),
        expected_current_signer=previous,
    )


def bootstrap(trust, home):
    trust.initialize_trust(brigade_home=home)
    return pin(trust, home)


def accept(trust, home, raw=None, *, hub=HUB, kind="membership", **kwargs):
    return trust.import_statement(
        brigade_home=home, hub=hub, kind=kind, raw=raw or signed(hub=hub, kind=kind, **kwargs)
    )


def floor(trust, home, raw, *, hub=HUB, kind="membership", seed=SEED):
    return trust.require_floor(
        brigade_home=home, hub=hub, kind=kind, signer=signer(seed), seq=json.loads(raw)["seq"], digest=digest(raw)
    )


def test_explicit_initialization_and_detached_load(trust, home):
    with pytest.raises(crypto.MeshError, match="^mesh_trust_missing$"):
        trust.load_trust(brigade_home=home)
    state = trust.initialize_trust(brigade_home=home)
    assert state.data["v"] == 1
    assert state.data["generation"] == 1
    assert stat.S_IMODE(saved(home).stat().st_mode) == 0o600
    before = saved(home).read_bytes()
    state.data["anchors"]["bad"] = {}
    assert trust.load_trust(brigade_home=home).data["anchors"] == {}
    assert trust.initialize_trust(brigade_home=home).data["generation"] == 1
    assert saved(home).read_bytes() == before


@pytest.mark.parametrize("mutation", ["newline", "padding", "wrong-fingerprint", "wrong-cas"])
def test_bootstrap_key_is_exact_independently_confirmed_and_cas(trust, home, mutation):
    trust.initialize_trust(brigade_home=home)
    before = saved(home).read_bytes()
    material = crypto.encode_binary(key()).encode()
    if mutation == "newline":
        material += b"\n"
    if mutation == "padding":
        material += b"="
    with pytest.raises(crypto.MeshError):
        trust.pin_manifest_anchor(
            brigade_home=home,
            hub=HUB,
            public_key_file=material,
            expected_fingerprint=crypto.fingerprint(key(SEED2) if mutation == "wrong-fingerprint" else key()),
            expected_current_signer=signer() if mutation == "wrong-cas" else None,
        )
    assert saved(home).read_bytes() == before
    pin(trust, home)
    before = saved(home).read_bytes()
    pin(trust, home)
    assert saved(home).read_bytes() == before


def test_global_cross_hub_marks_kind_separation_and_canonical_replay(trust, home):
    bootstrap(trust, home)
    pin(trust, home, hub=OTHER)
    raw = signed(seq=7)
    assert accept(trust, home, raw).status == "accepted"
    before = saved(home).read_bytes()
    reformatted = json.dumps(dict(reversed(list(json.loads(raw).items()))), indent=2).encode()
    assert accept(trust, home, reformatted).status == "replay"
    assert saved(home).read_bytes() == before
    assert accept(trust, home, kind="revocation", seq=0).status == "accepted"
    accept(trust, home, hub=OTHER, seq=8)
    with pytest.raises(crypto.MeshError, match="^signed_statement_rollback$"):
        accept(trust, home, raw)
    snapshot = trust.load_trust(brigade_home=home)
    mark = snapshot.data["marks"]["membership:" + signer()]
    assert mark["seq"] == 8
    assert snapshot.data["objects"][mark["digest"]]["hub"] == OTHER
    assert digest(raw) in snapshot.data["objects"]


def test_authenticated_conflict_persists_only_evidence_and_is_idempotent(trust, home):
    bootstrap(trust, home)
    accept(trust, home, seq=4)
    before = trust.load_trust(brigade_home=home).data
    conflict = signed(seq=4, entries=["test-node"])
    result = accept(trust, home, conflict)
    assert result.status == "signed_statement_conflict"
    assert result.evidence_recorded and result.storage_error is None
    after = trust.load_trust(brigade_home=home).data
    for name in ("objects", "marks", "floors", "barriers", "baselines"):
        assert before[name] == after[name]
    assert len(after["conflicts"]) == 1
    evidence = next(iter(after["conflicts"].values()))
    assert evidence["reason"] == "signer_equivocation"
    assert evidence["rejected"]["canonical"] == conflict.decode()
    previous = saved(home).read_bytes()
    assert accept(trust, home, conflict).evidence_recorded
    assert saved(home).read_bytes() == previous


def test_floor_scope_tightening_and_contradiction_evidence(trust, home):
    bootstrap(trust, home)
    pin(trust, home, hub=OTHER)
    target = signed(seq=4)
    snap = floor(trust, home, target)
    assert len(snap.unsatisfied_floors) == 1
    accept(trust, home, hub=OTHER, seq=7)
    assert len(trust.load_trust(brigade_home=home).unsatisfied_floors) == 1
    later = signed(seq=8)
    accept(trust, home, later)
    assert not trust.load_trust(brigade_home=home).unsatisfied_floors
    floor(trust, home, signed(seq=9))
    assert digest(later) in trust.load_trust(brigade_home=home).data["objects"]
    previous = saved(home).read_bytes()
    for raw in (target, signed(seq=9, entries=["test-node"])):
        with pytest.raises(crypto.MeshError, match="^mesh_trust_floor_not_tightened$"):
            floor(trust, home, raw)
        assert saved(home).read_bytes() == previous
    result = accept(trust, home, signed(seq=9, entries=["test-node"]))
    assert result.status == "signed_statement_conflict"
    evidence = next(iter(trust.load_trust(brigade_home=home).data["conflicts"].values()))
    assert evidence["reason"] == "trusted_floor_contradiction"
    assert evidence["floor"]["digest"] == digest(signed(seq=9))


def test_anchor_replacement_retains_history_marks_and_barriers(trust, home):
    bootstrap(trust, home)
    old = signed(seq=10, kind="revocation", entries=[revoked()])
    accept(trust, home, old, kind="revocation")
    membership = signed(seq=10, entries=["test-node"])
    accept(trust, home, membership)
    floor(trust, home, signed(seq=11))
    pin(trust, home, seed=SEED2, previous=signer())
    accept(trust, home, seed=SEED2, kind="revocation", seq=0)
    state = trust.load_trust(brigade_home=home)
    assert set(state.data["anchors"]) == {signer(), signer(SEED2)}
    assert state.data["hubs"][HUB] == [signer(), signer(SEED2)]
    assert digest(old) in state.data["objects"] and digest(membership) in state.data["objects"]
    assert len(state.data["barriers"]) == 1 and len(state.unsatisfied_floors) == 1
    with pytest.raises(crypto.MeshError, match="^statement_signature_invalid$"):
        accept(trust, home, seq=12)
    pin(trust, home, previous=signer(SEED2))
    with pytest.raises(crypto.MeshError, match="^signed_statement_rollback$"):
        accept(trust, home, kind="revocation", seq=9)


def test_cumulative_signed_baseline_is_separate_from_hub_earlier_wins(trust, home):
    bootstrap(trust, home)
    trust.record_hub_revocation(brigade_home=home, hub=HUB, principal="test-node", kid=KID2, cutoff=TIME)
    first = signed(kind="revocation", seq=1, entries=[revoked()])
    accept(trust, home, first, kind="revocation")
    for entries in ([], [revoked(cutoff="2026-01-03T00:00:00Z")]):
        before = saved(home).read_bytes()
        with pytest.raises(crypto.MeshError, match="^mesh_trust_revocation_not_cumulative$"):
            accept(trust, home, kind="revocation", seq=2, entries=entries)
        assert saved(home).read_bytes() == before
    accept(trust, home, kind="revocation", seq=2, entries=[revoked(cutoff="2026-01-02T01:00:00+01:00")])
    trust.record_hub_revocation(
        brigade_home=home, hub=HUB, principal="test-node", kid=KID, cutoff="2026-01-01T23:00:00Z"
    )
    previous = saved(home).read_bytes()
    trust.record_hub_revocation(brigade_home=home, hub=HUB, principal="test-node", kid=KID, cutoff=TIME)
    assert saved(home).read_bytes() == previous
    state = trust.load_trust(brigade_home=home).data
    barrier = next(b for b in state["barriers"].values() if b["kid"] == KID)
    assert barrier["cutoff"] == "2026-01-01T23:00:00Z"
    assert {s["source"] for s in barrier["sources"]} == {
        "hub",
        digest(first),
        digest(signed(kind="revocation", seq=2, entries=[revoked(cutoff="2026-01-02T01:00:00+01:00")])),
    }


def test_extreme_timestamps_compare_without_utc_datetime_overflow(trust, home):
    bootstrap(trust, home)
    for cutoff in ("9999-12-31T23:59:59-23:59", "0001-01-01T00:00:00+23:59"):
        trust.record_hub_revocation(brigade_home=home, hub=HUB, principal="test-node", kid=KID, cutoff=cutoff)
    assert next(iter(trust.load_trust(brigade_home=home).data["barriers"].values()))["cutoff"].startswith("0001-")


def test_kid_binding_rejects_cross_principal_but_not_cross_hub(trust, home):
    bootstrap(trust, home)
    trust.record_hub_revocation(brigade_home=home, hub=HUB, principal="test-node", kid=KID, cutoff=TIME)
    before = saved(home).read_bytes()
    with pytest.raises(crypto.MeshError, match="^mesh_trust_kid_binding_conflict$"):
        accept(trust, home, kind="revocation", entries=[revoked(principal="test-other")])
    assert saved(home).read_bytes() == before
    trust.record_hub_revocation(brigade_home=home, hub=OTHER, principal="test-other", kid=KID, cutoff=TIME)
    assert len(trust.load_trust(brigade_home=home).data["barriers"]) == 2


@pytest.mark.parametrize("case", ["bad-signature", "wrong-scope", "duplicate", "unknown", "too-deep"])
def test_rejected_untrusted_statement_never_changes_bytes(trust, home, case):
    bootstrap(trust, home)
    raw = signed()
    if case == "bad-signature":
        raw = raw.replace(b'"seq":1', b'"seq":2')
    elif case == "wrong-scope":
        raw = signed(hub=OTHER)
    elif case == "duplicate":
        raw = raw.replace(b'"v":1', b'"v":1,"v":1')
    elif case == "unknown":
        raw = raw.replace(b'"v":1', b'"extra":1,"v":1')
    else:
        raw = b"[[[[[[]]]]]]"
    before = saved(home).read_bytes()
    with pytest.raises(crypto.MeshError):
        accept(trust, home, raw)
    assert saved(home).read_bytes() == before


@pytest.mark.parametrize("kind", ["manifest", "hub-identity", "hub-migration", "mesh", "", None, []])
def test_unsupported_kinds_never_create_or_mutate(trust, home, kind):
    with pytest.raises(crypto.MeshError, match="^statement_kind_unsupported$"):
        trust.import_statement(brigade_home=home, hub=HUB, kind=kind, raw=b"{}")
    assert not saved(home).exists()


@pytest.mark.parametrize(
    "case", ["version", "duplicate", "depth", "huge-int", "hash", "mark", "barrier", "key", "signature"]
)
def test_corruption_refuses_load_and_initialize_without_reset(trust, home, case):
    bootstrap(trust, home)
    accept(trust, home, kind="revocation", entries=[revoked()])
    state = json.loads(saved(home).read_bytes())
    if case == "version":
        state["v"] = 2
    elif case == "hash":
        value = state["objects"].pop(next(iter(state["objects"])))
        state["objects"]["0" * 64] = value
    elif case == "mark":
        next(iter(state["marks"].values()))["seq"] += 1
    elif case == "barrier":
        state["barriers"] = {}
    elif case == "key":
        state["anchors"][signer()]["public_key"] = crypto.encode_binary(key(SEED2))
    elif case == "signature":
        entry = next(iter(state["objects"].values()))
        entry["canonical"] = entry["canonical"].replace('"seq":1', '"seq":2')
    raw = crypto.canonical(state)
    if case == "duplicate":
        raw = raw.replace(b'"v":1', b'"v":1,"v":1')
    if case == "depth":
        raw = b"[" * 20 + b"]" * 20
    if case == "huge-int":
        raw = raw.replace(b'"v":1', b'"v":123456789012345678901234567890')
    saved(home).write_bytes(raw)
    for op in (trust.load_trust, trust.initialize_trust):
        with pytest.raises(crypto.MeshError, match="^mesh_trust_invalid$"):
            op(brigade_home=home)
        assert saved(home).read_bytes() == raw


@pytest.mark.parametrize("case", ["ordinary", "object-count", "conflict-count", "total"])
def test_capacity_fails_closed_without_forgetting(trust, home, monkeypatch, case):
    bootstrap(trust, home)
    accept(trust, home)
    before = saved(home).read_bytes()
    if case == "ordinary":
        monkeypatch.setattr(trust, "_ORDINARY_BYTES", len(before) + 50)
    elif case == "object-count":
        monkeypatch.setattr(trust, "_MAX_OBJECTS", 1)
    elif case == "conflict-count":
        monkeypatch.setattr(trust, "_MAX_CONFLICTS", 0)
    else:
        monkeypatch.setattr(trust, "_MAX_BYTES", len(before) + 50)
    if case in {"conflict-count", "total"}:
        result = accept(trust, home, entries=["test-node"])
        assert result.status == "signed_statement_conflict"
        assert not result.evidence_recorded and result.storage_error == "mesh_trust_capacity"
    else:
        with pytest.raises(crypto.MeshError, match="^mesh_trust_capacity$"):
            accept(trust, home, seq=2)
    assert saved(home).read_bytes() == before


def test_conflict_reserve_remains_available_when_ordinary_budget_is_full(trust, home, monkeypatch):
    bootstrap(trust, home)
    accept(trust, home)
    monkeypatch.setattr(trust, "_ORDINARY_BYTES", len(saved(home).read_bytes()))
    result = accept(trust, home, entries=["test-node"])
    assert result.evidence_recorded


@pytest.mark.parametrize(
    "case", ["file-symlink", "hardlink", "fifo", "file-mode", "directory-mode", "ancestor-symlink", "owner"]
)
def test_unsafe_paths_and_files_refuse_without_repair(trust, home, monkeypatch, case):
    bootstrap(trust, home)
    path = saved(home)
    if case == "file-symlink":
        other = home / "copy"
        path.rename(other)
        path.symlink_to(other)
    elif case == "hardlink":
        os.link(path, home / "copy")
    elif case == "fifo":
        path.unlink()
        os.mkfifo(path, 0o600)
    elif case == "file-mode":
        path.chmod(0o640)
    elif case == "directory-mode":
        path.parent.chmod(0o750)
    elif case == "ancestor-symlink":
        alias = home.parent / "alias"
        alias.symlink_to(home, target_is_directory=True)
        home = alias
    else:
        monkeypatch.setattr(os, "geteuid", lambda: path.stat().st_uid + 1)
    with pytest.raises(crypto.MeshError, match="^mesh_trust_unsafe$"):
        trust.load_trust(brigade_home=home)


def test_raw_bound_precedes_json_allocation(trust, home, monkeypatch):
    bootstrap(trust, home)
    bound = len(saved(home).read_bytes()) - 1
    monkeypatch.setattr(trust, "_MAX_BYTES", bound)

    def forbidden(*args, **kwargs):
        pytest.fail("JSON allocated before raw bound")

    monkeypatch.setattr(json, "loads", forbidden)
    with pytest.raises(crypto.MeshError, match="^mesh_trust_invalid$"):
        trust.load_trust(brigade_home=home)


@pytest.mark.parametrize("point", ["file-fsync", "replace", "directory-fsync"])
def test_transaction_faults_and_durable_idempotent_recovery(trust, home, monkeypatch, point):
    bootstrap(trust, home)
    old = saved(home).read_bytes()
    raw = signed(seq=2)
    original_sync = os.fsync
    original_replace = dirfd.replace_children

    def sync(fd):
        directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        if (point == "directory-fsync" and directory) or (point == "file-fsync" and not directory):
            raise OSError("sensitive injected text")
        original_sync(fd)

    def replace(*args, **kwargs):
        raise OSError("sensitive injected text")

    monkeypatch.setattr(os, "fsync", sync)
    if point == "replace":
        monkeypatch.setattr(dirfd, "replace_children", replace)
    code = "mesh_trust_durability_indeterminate" if point == "directory-fsync" else "mesh_trust_storage_failure"
    with pytest.raises(crypto.MeshError, match="^" + code + "$"):
        accept(trust, home, raw)
    if point != "directory-fsync":
        assert saved(home).read_bytes() == old
    else:
        assert saved(home).read_bytes() != old
        # A matching retry must flush, even though the full generation exists.
        with pytest.raises(crypto.MeshError, match="^mesh_trust_durability_indeterminate$"):
            accept(trust, home, raw)
    monkeypatch.setattr(os, "fsync", original_sync)
    monkeypatch.setattr(dirfd, "replace_children", original_replace)
    assert accept(trust, home, raw).status == ("replay" if point == "directory-fsync" else "accepted")
    assert digest(raw) in trust.load_trust(brigade_home=home).data["objects"]
    assert not list(saved(home).parent.glob(".trust-*.tmp"))


def test_failed_conflict_evidence_keeps_diagnosis_without_claiming_recorded(trust, home, monkeypatch):
    bootstrap(trust, home)
    accept(trust, home)
    before = saved(home).read_bytes()

    def fail(*args, **kwargs):
        raise OSError("private diagnostic")

    monkeypatch.setattr(dirfd, "replace_children", fail)
    result = accept(trust, home, entries=["test-node"])
    assert result.status == "signed_statement_conflict"
    assert not result.evidence_recorded and result.storage_error == "mesh_trust_storage_failure"
    assert saved(home).read_bytes() == before


def test_lost_updates_lock_contention_and_custody_inode(trust, home, monkeypatch):
    bootstrap(trust, home)

    def update(index):
        trust.record_hub_revocation(
            brigade_home=home,
            hub=HUB,
            principal="test-node",
            kid="k1_" + crypto.encode_binary(index.to_bytes(16, "big")),
            cutoff=TIME,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(update, range(12)))
    assert len(trust.load_trust(brigade_home=home).data["barriers"]) == 12
    import fcntl

    descriptor = dirfd.open_directory_chain(saved(home).parent)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        monkeypatch.setattr(trust, "_LOCK_SECONDS", 0.01)
        with pytest.raises(crypto.MeshError, match="^mesh_trust_busy$"):
            trust.load_trust(brigade_home=home)
    finally:
        os.close(descriptor)


def test_readonly_load_does_not_flush_or_adopt_orphans(trust, home, monkeypatch):
    bootstrap(trust, home)
    orphan = saved(home).parent / (".trust-" + "a" * 32 + ".tmp")
    custody = saved(home).parent / ("." + "b" * 32 + ".tmp")
    orphan.write_bytes(b"uncommitted")
    custody.write_bytes(b"custody")

    def fail(*args):
        pytest.fail("read-only load attempted a durability write")

    monkeypatch.setattr(os, "fsync", fail)
    trust.load_trust(brigade_home=home)
    assert orphan.read_bytes() == b"uncommitted" and custody.read_bytes() == b"custody"


def test_native_platform_refusal_before_filesystem_without_skipping(tmp_path):
    # Runs on Windows too. Refusal must precede Path conversion or any open.
    script = """
from brigade import fleet_mesh_trust as trust
from brigade import fleet_mesh_crypto as crypto
trust.os.name = "nt"
class NoPath:
    def __fspath__(self): raise AssertionError("filesystem accessed")
operations = (
    lambda: trust.load_trust(brigade_home=NoPath()),
    lambda: trust.initialize_trust(brigade_home=NoPath()),
    lambda: trust.pin_manifest_anchor(brigade_home=NoPath(), hub="", public_key_file=b"", expected_fingerprint="", expected_current_signer=None),
    lambda: trust.require_floor(brigade_home=NoPath(), hub="", kind="", signer="", seq=0, digest=""),
    lambda: trust.import_statement(brigade_home=NoPath(), hub="", kind="", raw=b""),
    lambda: trust.record_hub_revocation(brigade_home=NoPath(), hub="", principal="", kid="", cutoff=""),
)
for operation in operations:
    try: operation()
    except crypto.MeshError as error: assert error.code == "mesh_trust_platform_unsupported"
    else: raise AssertionError("native platform accepted")
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_recorded_conflict_retries_after_head_and_floor_advance(trust, home):
    bootstrap(trust, home)
    accept(trust, home, seq=1)
    conflict = signed(seq=1, entries=["test-node"])
    assert accept(trust, home, conflict).evidence_recorded
    accept(trust, home, seq=2)
    before = saved(home).read_bytes()
    result = accept(trust, home, conflict)
    assert result.status == "signed_statement_conflict" and result.evidence_recorded
    assert saved(home).read_bytes() == before
    floor(trust, home, signed(seq=3))
    conflict = signed(seq=3, entries=["test-other"])
    assert accept(trust, home, conflict).evidence_recorded
    floor(trust, home, signed(seq=4))
    before = saved(home).read_bytes()
    assert accept(trust, home, conflict).evidence_recorded
    assert saved(home).read_bytes() == before


def test_floor_blocks_lower_statement_without_a_global_mark(trust, home):
    bootstrap(trust, home)
    floor(trust, home, signed(seq=10))
    before = saved(home).read_bytes()
    with pytest.raises(crypto.MeshError, match="^signed_statement_rollback$"):
        accept(trust, home, seq=9)
    assert saved(home).read_bytes() == before


def test_non_object_table_capacity_has_dedicated_refusal(trust, home, monkeypatch):
    bootstrap(trust, home)
    trust.record_hub_revocation(brigade_home=home, hub=HUB, principal="test-node", kid=KID, cutoff=TIME)
    before = saved(home).read_bytes()
    monkeypatch.setattr(trust, "_MAX_ENTRIES", 1)
    with pytest.raises(crypto.MeshError, match="^mesh_trust_capacity$"):
        trust.record_hub_revocation(brigade_home=home, hub=HUB, principal="test-node", kid=KID2, cutoff=TIME)
    assert saved(home).read_bytes() == before


def test_conflict_post_replace_failure_is_recoverable_and_not_reported_recorded(trust, home, monkeypatch):
    bootstrap(trust, home)
    accept(trust, home)
    original = dirfd.fsync_directory

    def fail(descriptor):
        raise OSError("private fault")

    monkeypatch.setattr(dirfd, "fsync_directory", fail)
    raw = signed(entries=["test-node"])
    for _ in range(2):
        result = accept(trust, home, raw)
        assert result.status == "signed_statement_conflict" and not result.evidence_recorded
        assert result.storage_error == "mesh_trust_durability_indeterminate"
    monkeypatch.setattr(dirfd, "fsync_directory", original)
    assert accept(trust, home, raw).evidence_recorded
    assert len(trust.load_trust(brigade_home=home).data["conflicts"]) == 1


def test_restart_uses_persisted_marks(trust, home):
    bootstrap(trust, home)
    accept(trust, home, seq=8)
    script = """
from pathlib import Path
import sys
from brigade import fleet_mesh_trust as trust, fleet_mesh_crypto as crypto
try: trust.import_statement(brigade_home=Path(sys.argv[1]), hub=sys.argv[2], kind="membership", raw=bytes.fromhex(sys.argv[3]))
except crypto.MeshError as error: assert error.code == "signed_statement_rollback"
else: raise AssertionError("restart forgot mark")
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(home), HUB, signed(seq=7).hex()], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("value", [1, [], {}])
def test_corrupt_canonical_field_is_a_fixed_refusal(trust, home, value):
    bootstrap(trust, home)
    accept(trust, home)
    state = json.loads(saved(home).read_bytes())
    next(iter(state["objects"].values()))["canonical"] = value
    raw = crypto.canonical(state)
    saved(home).write_bytes(raw)
    with pytest.raises(crypto.MeshError, match="^mesh_trust_invalid$"):
        trust.load_trust(brigade_home=home)
    assert saved(home).read_bytes() == raw


def test_load_refuses_ordinary_state_in_conflict_reserve(trust, home, monkeypatch):
    bootstrap(trust, home)
    before = saved(home).read_bytes()
    monkeypatch.setattr(trust, "_ORDINARY_BYTES", len(before) - 1)
    with pytest.raises(crypto.MeshError, match="^mesh_trust_invalid$"):
        trust.load_trust(brigade_home=home)
    assert saved(home).read_bytes() == before


def test_bad_signature_at_conflicting_sequence_does_not_record_evidence(trust, home):
    bootstrap(trust, home)
    accept(trust, home)
    raw = signed(seed=SEED2, entries=["test-node"])
    before = saved(home).read_bytes()
    with pytest.raises(crypto.MeshError, match="^statement_signature_invalid$"):
        accept(trust, home, raw)
    assert saved(home).read_bytes() == before


def test_anchor_replacement_retry_flushes_with_original_cas(trust, home, monkeypatch):
    bootstrap(trust, home)
    original = dirfd.fsync_directory

    def fail(descriptor):
        raise OSError("private diagnostic")

    monkeypatch.setattr(dirfd, "fsync_directory", fail)
    for _ in range(2):
        with pytest.raises(crypto.MeshError, match="^mesh_trust_durability_indeterminate$"):
            pin(trust, home, seed=SEED2, previous=signer())
    monkeypatch.setattr(dirfd, "fsync_directory", original)
    before = saved(home).read_bytes()
    state = pin(trust, home, seed=SEED2, previous=signer())
    assert state.data["hubs"][HUB] == [signer(), signer(SEED2)]
    assert saved(home).read_bytes() == before


def test_all_mutators_refuse_missing_state_without_creating_it(trust, home):
    operations = (
        lambda: pin(trust, home),
        lambda: floor(trust, home, signed()),
        lambda: accept(trust, home),
        lambda: trust.record_hub_revocation(brigade_home=home, hub=HUB, principal="test-node", kid=KID, cutoff=TIME),
    )
    for operation in operations:
        with pytest.raises(crypto.MeshError, match="^mesh_trust_missing$"):
            operation()
        assert not saved(home).exists()
