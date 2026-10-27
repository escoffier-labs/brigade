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


@pytest.mark.parametrize("kind", ["membership", "revocation"])
def test_floor_contradiction_evidence_is_independent_of_arrival_order(trust, home, kind):
    entries = ["test-node"] if kind == "membership" else [revoked()]
    raw = signed(kind=kind, seq=5, entries=entries)
    target = signed(kind=kind, seq=5)
    conflicts = []
    for order in ("floor-first", "statement-first"):
        directory = home / order
        directory.mkdir(mode=0o700)
        bootstrap(trust, directory)
        if order == "floor-first":
            floor(trust, directory, target, kind=kind)
        else:
            accept(trust, directory, raw, kind=kind)
        before = trust.load_trust(brigade_home=directory).data
        if order == "floor-first":
            assert accept(trust, directory, raw, kind=kind).evidence_recorded
        else:
            floor(trust, directory, target, kind=kind)
        after = trust.load_trust(brigade_home=directory)
        assert after.data["generation"] == before["generation"] + 1
        for table in ("anchors", "hubs", "objects", "marks", "baselines", "barriers", "hub_revocations"):
            assert after.data[table] == before[table]
        assert len(after.unsatisfied_floors) == 1
        assert len(after.data["conflicts"]) == 1
        evidence = next(iter(after.data["conflicts"].values()))
        assert evidence["reason"] == "trusted_floor_contradiction"
        assert evidence["rejected"]["canonical"] == raw.decode("ascii")
        assert evidence["digest"] == digest(raw)
        assert evidence["floor"]["digest"] == digest(target)
        conflicts.append(after.data["conflicts"])
    assert conflicts[0] == conflicts[1]


@pytest.mark.parametrize("head_hub", [HUB, OTHER])
def test_floor_second_records_contradicted_history_below_a_higher_head(trust, home, head_hub):
    bootstrap(trust, home)
    pin(trust, home, hub=OTHER)
    raw = signed(seq=5, entries=["test-node"])
    accept(trust, home, raw)
    accept(trust, home, hub=head_hub, seq=6)
    before = trust.load_trust(brigade_home=home).data
    target = signed(seq=5)
    after = floor(trust, home, target)
    assert len(after.data["conflicts"]) == 1
    evidence = next(iter(after.data["conflicts"].values()))
    assert evidence["digest"] == digest(raw) and evidence["reason"] == "trusted_floor_contradiction"
    assert len(after.unsatisfied_floors) == (0 if head_hub == HUB else 1)
    for table in ("objects", "marks", "baselines", "barriers"):
        assert before[table] == after.data[table]
    # Repeating either side reuses evidence, even after a later floor is installed.
    for operation in (lambda: floor(trust, home, target), lambda: accept(trust, home, raw)):
        unchanged = saved(home).read_bytes()
        operation()
        assert saved(home).read_bytes() == unchanged
    old_conflicts = after.data["conflicts"]
    later = floor(trust, home, signed(seq=7))
    assert later.data["conflicts"] == old_conflicts
    script = """
from pathlib import Path
import sys
from brigade import fleet_mesh_trust as trust
snapshot = trust.load_trust(brigade_home=Path(sys.argv[1]))
assert len(snapshot.data["conflicts"]) == 1
assert next(iter(snapshot.data["conflicts"].values()))["digest"] == sys.argv[2]
"""
    result = subprocess.run([sys.executable, "-c", script, str(home), digest(raw)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("case", ["same-digest", "lower-seq", "other-hub", "other-kind", "other-signer"])
def test_floor_second_only_records_an_exact_tuple_contradiction(trust, home, case):
    bootstrap(trust, home)
    raw = signed(seq=5)
    accept(trust, home, raw)
    target = raw if case == "same-digest" else signed(seq=6 if case == "lower-seq" else 5, entries=["test-node"])
    kwargs = {"hub": OTHER} if case == "other-hub" else {"seed": SEED2} if case == "other-signer" else {}
    if case == "other-kind":
        target = signed(kind="revocation", seq=5)
        kwargs["kind"] = "revocation"
    before = trust.load_trust(brigade_home=home).data
    after = floor(trust, home, target, **kwargs)
    assert after.data["conflicts"] == {}
    for table in ("objects", "marks", "baselines", "barriers"):
        assert after.data[table] == before[table]


@pytest.mark.parametrize("point", ["capacity", "file-fsync", "replace", "directory-fsync"])
@pytest.mark.parametrize("existing_floor", [False, True])
def test_floor_and_contradiction_publish_atomically_and_recover(trust, home, monkeypatch, point, existing_floor):
    bootstrap(trust, home)
    accept(trust, home, signed(seq=5, entries=["test-node"]))
    if existing_floor:
        floor(trust, home, signed(seq=4))
    before = saved(home).read_bytes()
    old = trust.load_trust(brigade_home=home).data
    target = signed(seq=5)
    with monkeypatch.context() as fault:
        original_sync = os.fsync
        if point == "capacity":
            fault.setattr(trust, "_MAX_CONFLICTS", 0)
        elif point == "replace":

            def replace(*args, **kwargs):
                raise OSError("private injected diagnostic")

            fault.setattr(dirfd, "replace_children", replace)
        else:

            def sync(descriptor):
                directory = stat.S_ISDIR(os.fstat(descriptor).st_mode)
                if directory == (point == "directory-fsync"):
                    raise OSError("private injected diagnostic")
                original_sync(descriptor)

            fault.setattr(os, "fsync", sync)
        code = (
            "mesh_trust_capacity"
            if point == "capacity"
            else "mesh_trust_durability_indeterminate"
            if point == "directory-fsync"
            else "mesh_trust_storage_failure"
        )
        with pytest.raises(crypto.MeshError, match="^" + code + "$"):
            floor(trust, home, target)
        if point == "directory-fsync":
            complete = trust.load_trust(brigade_home=home).data
            assert len(complete["floors"]) == len(complete["conflicts"]) == 1
            assert complete["generation"] == old["generation"] + 1
            with pytest.raises(crypto.MeshError, match="^mesh_trust_durability_indeterminate$"):
                floor(trust, home, target)
            assert saved(home).read_bytes() == crypto.canonical(complete)
        else:
            assert saved(home).read_bytes() == before
    after = floor(trust, home, target)
    assert len(after.data["floors"]) == len(after.data["conflicts"]) == 1
    assert next(iter(after.data["floors"].values()))["digest"] == digest(target)
    assert after.data["generation"] == old["generation"] + 1
    for table in ("objects", "marks", "baselines", "barriers"):
        assert after.data[table] == old[table]
    unchanged = saved(home).read_bytes()
    floor(trust, home, target)
    assert saved(home).read_bytes() == unchanged


@pytest.mark.parametrize("backend", ["missing", "unsupported"])
@pytest.mark.parametrize("operation", ["load", "initialize", "pin", "floor", "import", "hub-revocation"])
def test_saved_signed_snapshot_preserves_backend_diagnostics(trust, home, monkeypatch, backend, operation):
    bootstrap(trust, home)
    raw = signed(kind="revocation", entries=[revoked()])
    accept(trust, home, raw, kind="revocation")
    before = saved(home).read_bytes()
    error = crypto.MeshExtraMissing() if backend == "missing" else crypto.MeshError("mesh_backend_unsupported")

    def unavailable():
        raise error

    operations = {
        "load": lambda: trust.load_trust(brigade_home=home),
        "initialize": lambda: trust.initialize_trust(brigade_home=home),
        "pin": lambda: pin(trust, home, seed=SEED2, previous=signer()),
        "floor": lambda: floor(trust, home, raw, kind="revocation"),
        "import": lambda: accept(trust, home, raw, kind="revocation"),
        "hub-revocation": lambda: trust.record_hub_revocation(
            brigade_home=home, hub=HUB, principal="test-node", kid=KID2, cutoff=TIME
        ),
    }
    with monkeypatch.context() as missing:
        missing.setattr(crypto, "probe_backend", unavailable)
        with pytest.raises(crypto.MeshError) as caught:
            operations[operation]()
        assert caught.value.code == error.code
        assert type(caught.value) is type(error)
        assert caught.value is error
        assert saved(home).read_bytes() == before
    assert trust.load_trust(brigade_home=home).data == json.loads(before)


@pytest.mark.parametrize("backend", ["missing", "unsupported"])
def test_empty_and_anchor_only_snapshots_need_no_crypto_backend(trust, home, monkeypatch, backend):
    def unavailable():
        if backend == "missing":
            raise crypto.MeshExtraMissing()
        raise crypto.MeshError("mesh_backend_unsupported")

    monkeypatch.setattr(crypto, "probe_backend", unavailable)
    trust.initialize_trust(brigade_home=home)
    assert trust.load_trust(brigade_home=home).data["anchors"] == {}
    pin(trust, home)
    before = saved(home).read_bytes()
    assert len(trust.load_trust(brigade_home=home).data["anchors"]) == 1
    trust.initialize_trust(brigade_home=home)
    assert saved(home).read_bytes() == before


@pytest.mark.parametrize("boundary", ["home", "fleet", "mesh-keys"])
def test_initialization_retry_reflushes_each_private_child_parent(trust, home, monkeypatch, boundary):
    child = home if boundary == "home" else home / "fleet" if boundary == "fleet" else home / "fleet" / "mesh-keys"
    if boundary == "home":
        home.rmdir()
    parent = child.parent
    original_sync = dirfd.fsync_directory
    original_open = dirfd.open_child_directory
    opened = []
    flushed = []
    failed = False

    def open_child(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def sync(descriptor):
        nonlocal failed
        info = os.fstat(descriptor)
        identity = (info.st_dev, info.st_ino)
        flushed.append(identity)
        target = parent.stat() if parent.exists() else None
        if target is not None and identity == (target.st_dev, target.st_ino) and not failed:
            assert child.is_dir()
            failed = True
            raise OSError("private parent-flush fault")
        original_sync(descriptor)

    monkeypatch.setattr(dirfd, "open_child_directory", open_child)
    monkeypatch.setattr(dirfd, "fsync_directory", sync)
    with pytest.raises(crypto.MeshError) as caught:
        trust.initialize_trust(brigade_home=home)
    assert child.is_dir() and not saved(home).exists()
    for descriptor in opened:
        with pytest.raises(OSError):
            os.fstat(descriptor)
    # The existing child must trigger the parent barrier again on retry.
    flushed.clear()
    state = trust.initialize_trust(brigade_home=home)
    target = parent.stat()
    assert (target.st_dev, target.st_ino) in flushed
    assert state.data["generation"] == 1
    assert caught.value.code == "mesh_trust_storage_failure"
    assert str(caught.value) == "mesh_trust_storage_failure"


@pytest.mark.parametrize("boundary", ["home", "fleet", "mesh-keys"])
def test_initialization_validates_child_before_flushing_parent_and_closes_fds(trust, home, monkeypatch, boundary):
    bootstrap(trust, home)
    child = home if boundary == "home" else home / "fleet" if boundary == "fleet" else saved(home).parent
    child.chmod(0o750)
    target = child.parent.stat()
    original_sync = dirfd.fsync_directory
    original_open = dirfd.open_child_directory
    opened = []
    flushed = []

    def open_child(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def sync(descriptor):
        info = os.fstat(descriptor)
        flushed.append((info.st_dev, info.st_ino))
        original_sync(descriptor)

    monkeypatch.setattr(dirfd, "open_child_directory", open_child)
    monkeypatch.setattr(dirfd, "fsync_directory", sync)
    with pytest.raises(crypto.MeshError, match="^mesh_trust_unsafe$"):
        trust.initialize_trust(brigade_home=home)
    assert (target.st_dev, target.st_ino) not in flushed
    for descriptor in opened:
        with pytest.raises(OSError):
            os.fstat(descriptor)
    assert stat.S_IMODE(child.stat().st_mode) == 0o750


def test_initialization_post_replace_fault_targets_snapshot_publication(trust, home, monkeypatch):
    original_sync = dirfd.fsync_directory
    original_replace = dirfd.replace_children
    replacements = []

    def replace(*args, **kwargs):
        replacements.append(args)
        original_replace(*args, **kwargs)

    def sync(descriptor):
        info = os.fstat(descriptor)
        if saved(home).exists():
            target = saved(home).parent.stat()
            if (info.st_dev, info.st_ino) == (target.st_dev, target.st_ino):
                raise OSError("private post-replace publication fault")
        original_sync(descriptor)

    monkeypatch.setattr(dirfd, "replace_children", replace)
    with monkeypatch.context() as fault:
        fault.setattr(dirfd, "fsync_directory", sync)
        with pytest.raises(crypto.MeshError, match="^mesh_trust_durability_indeterminate$"):
            trust.initialize_trust(brigade_home=home)
        complete = saved(home).read_bytes()
        assert json.loads(complete)["generation"] == 1
        with pytest.raises(crypto.MeshError, match="^mesh_trust_durability_indeterminate$"):
            trust.initialize_trust(brigade_home=home)
        assert saved(home).read_bytes() == complete
    assert trust.initialize_trust(brigade_home=home).data["generation"] == 1
    assert saved(home).read_bytes() == complete and len(replacements) == 1


def abandoned_trust_temp(home, *, name=None):
    path = saved(home).parent / (name or (".trust-" + "a" * 32 + ".tmp"))
    path.write_bytes(b"abandoned contents are never parsed or promoted")
    path.chmod(0o600)
    return path


def test_next_actual_write_reaps_safe_orphan_without_opening_it(trust, home, monkeypatch):
    bootstrap(trust, home)
    orphan = abandoned_trust_temp(home)
    raw = signed(seq=2)
    events = []
    original_open = dirfd.open_child_file
    original_sync = dirfd.fsync_directory
    original_replace = dirfd.replace_children

    def open_child(parent, name, flags, *args, **kwargs):
        assert name != orphan.name, "orphan contents were opened"
        if flags & os.O_CREAT:
            assert not orphan.exists()
            events.append("create-temp")
        return original_open(parent, name, flags, *args, **kwargs)

    def sync(descriptor):
        events.append("directory-flush")
        original_sync(descriptor)

    def replace(*args, **kwargs):
        events.append("replace")
        original_replace(*args, **kwargs)

    monkeypatch.setattr(dirfd, "open_child_file", open_child)
    monkeypatch.setattr(dirfd, "fsync_directory", sync)
    monkeypatch.setattr(dirfd, "replace_children", replace)
    assert accept(trust, home, raw).status == "accepted"
    assert not orphan.exists()
    assert events == ["directory-flush", "create-temp", "replace", "directory-flush"]
    state = trust.load_trust(brigade_home=home).data
    assert set(state["objects"]) == {digest(raw)}
    assert state["marks"]["membership:" + signer()]["digest"] == digest(raw)


def test_readonly_load_and_replay_leave_safe_orphan_and_snapshot_unchanged(trust, home, monkeypatch):
    bootstrap(trust, home)
    raw = signed()
    accept(trust, home, raw)
    orphan = abandoned_trust_temp(home)
    before = saved(home).read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("read-only load inspected, reaped or flushed directory contents")

    with monkeypatch.context() as readonly:
        for operation in ("child_names", "stat_child", "unlink_child", "fsync_directory"):
            readonly.setattr(dirfd, operation, forbidden)
        trust.load_trust(brigade_home=home)
    assert orphan.read_bytes() == b"abandoned contents are never parsed or promoted"
    assert saved(home).read_bytes() == before
    # Replay still performs its normal durability flush, without a cleanup write.
    assert accept(trust, home, raw).status == "replay"
    assert orphan.exists() and saved(home).read_bytes() == before


@pytest.mark.parametrize(
    "case",
    [
        "symlink",
        "hardlink",
        "fifo",
        "directory",
        "mode",
        "owner",
        "unrelated",
        "custody",
        "uppercase",
        "short",
        "suffix",
    ],
)
def test_orphan_reaper_excludes_unsafe_and_unrelated_entries(trust, home, monkeypatch, case):
    bootstrap(trust, home)
    name = ".trust-" + "a" * 32 + ".tmp"
    if case == "unrelated":
        name = "unrelated.tmp"
    elif case == "custody":
        name = "." + "a" * 32 + ".tmp"
    elif case == "uppercase":
        name = ".trust-" + "A" * 32 + ".tmp"
    elif case == "short":
        name = ".trust-" + "a" * 31 + ".tmp"
    elif case == "suffix":
        name += ".backup"
    candidate = saved(home).parent / name
    target = home / "test-target"
    if case in {"symlink", "hardlink"}:
        target.write_bytes(b"outside the cleanup namespace")
        target.chmod(0o600)
        if case == "symlink":
            candidate.symlink_to(target)
        else:
            os.link(target, candidate)
    elif case == "fifo":
        os.mkfifo(candidate, 0o600)
    elif case == "directory":
        candidate.mkdir(mode=0o700)
    else:
        abandoned_trust_temp(home, name=name)
        if case == "mode":
            candidate.chmod(0o640)
    if case == "owner":
        original_stat = dirfd.stat_child

        def foreign_owner(parent, child, **kwargs):
            info = original_stat(parent, child, **kwargs)
            if child == candidate.name:
                fields = list(info)
                fields[4] = info.st_uid + 1
                return os.stat_result(fields)
            return info

        monkeypatch.setattr(dirfd, "stat_child", foreign_owner)
    original_open = dirfd.open_child_file

    def no_orphan_open(parent, child, flags, *args, **kwargs):
        assert child != candidate.name, "cleanup opened an unsafe/unrelated entry"
        return original_open(parent, child, flags, *args, **kwargs)

    monkeypatch.setattr(dirfd, "open_child_file", no_orphan_open)
    before = candidate.lstat()
    assert accept(trust, home).status == "accepted"
    after = candidate.lstat()
    assert (after.st_ino, after.st_mode, after.st_uid, after.st_nlink) == (
        before.st_ino,
        before.st_mode,
        before.st_uid,
        before.st_nlink,
    )
    if case in {"symlink", "hardlink"}:
        assert target.read_bytes() == b"outside the cleanup namespace"


def test_orphan_deletion_flush_failure_is_sanitized_and_prevents_publication(trust, home, monkeypatch):
    bootstrap(trust, home)
    orphan = abandoned_trust_temp(home)
    before = saved(home).read_bytes()
    original_open = dirfd.open_child_file
    created = []

    def open_child(parent, name, flags, *args, **kwargs):
        if flags & os.O_CREAT:
            created.append(name)
        return original_open(parent, name, flags, *args, **kwargs)

    def fail(descriptor):
        raise OSError("private deletion-flush diagnostic")

    with monkeypatch.context() as fault:
        fault.setattr(dirfd, "open_child_file", open_child)
        fault.setattr(dirfd, "fsync_directory", fail)
        with pytest.raises(crypto.MeshError) as caught:
            accept(trust, home)
        assert caught.value.code == "mesh_trust_storage_failure"
        assert str(caught.value) == "mesh_trust_storage_failure"
        assert not orphan.exists()
        assert saved(home).read_bytes() == before
        assert created == []
    assert accept(trust, home).status == "accepted"
    assert trust.load_trust(brigade_home=home).data["generation"] == json.loads(before)["generation"] + 1


@pytest.mark.parametrize("case", ["capacity", "bad-input", "corrupt-state", "backend"])
def test_validation_and_capacity_refusals_do_not_reap_orphans(trust, home, monkeypatch, case):
    bootstrap(trust, home)
    accept(trust, home)
    orphan = abandoned_trust_temp(home)
    raw = signed(seq=2)
    if case == "capacity":
        monkeypatch.setattr(trust, "_MAX_OBJECTS", 1)
    elif case == "bad-input":
        raw = b"{}"
    elif case == "corrupt-state":
        saved(home).write_bytes(b'{"v":999}')
    else:

        def missing():
            raise crypto.MeshExtraMissing()

        monkeypatch.setattr(crypto, "probe_backend", missing)
    before = saved(home).read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("cleanup ran before validation/capacity refusal")

    monkeypatch.setattr(dirfd, "child_names", forbidden)
    with pytest.raises(crypto.MeshError):
        accept(trust, home, raw)
    assert orphan.exists() and saved(home).read_bytes() == before
