"""Local trust transactions with public deterministic keys and fake identities."""

from __future__ import annotations

import errno
import hashlib
import importlib
import json
import os
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

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


def test_kid_binding_disagreement_accepts_complete_list_and_keeps_hubs_separate(trust, home):
    # Policy A accepts complete disputed lists, including unrelated keys,
    # subject to ordinary capacity and the existing authenticity checks.
    trust.initialize_trust(brigade_home=home)
    earlier = "2026-01-01T00:00:00Z"
    trust.record_hub_revocation(brigade_home=home, hub=HUB, principal="bogus-node", kid=KID, cutoff=earlier)
    pin(trust, home)
    raw = signed(kind="revocation", entries=[revoked(), revoked(kid=KID2)])
    result = accept(trust, home, raw, kind="revocation")
    assert result.status == "accepted" and result.evidence_recorded
    state = trust.load_trust(brigade_home=home).data
    disputed = state["barriers"][HUB + ":" + KID]
    assert disputed["principals"] == ["bogus-node", "test-node"]
    assert disputed["cutoff"] == earlier
    assert state["barriers"][HUB + ":" + KID2]["principals"] == ["test-node"]
    assert state["marks"]["revocation:" + signer()]["digest"] == digest(raw)
    assert len(state["baselines"]) == len(state["objects"]) == len(state["binding_conflicts"]) == 1
    evidence = next(iter(state["binding_conflicts"].values()))
    assert evidence["reason"] == "kid_binding_conflict" and evidence["hub"] == HUB and evidence["kid"] == KID
    assert {(c["principal"], c["source"]) for c in evidence["claims"]} == {
        ("bogus-node", "hub"),
        ("test-node", digest(raw)),
    }
    before = saved(home).read_bytes()
    replay = accept(trust, home, raw, kind="revocation")
    assert replay.status == "replay" and replay.evidence_recorded
    assert saved(home).read_bytes() == before
    trust.record_hub_revocation(brigade_home=home, hub=OTHER, principal="test-other", kid=KID, cutoff=TIME)
    state = trust.load_trust(brigade_home=home).data
    assert len(state["barriers"]) == 3
    assert state["barriers"][OTHER + ":" + KID]["principals"] == ["test-other"]
    assert len(state["binding_conflicts"]) == 1


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


@pytest.mark.parametrize("point", ["initial-open", "snapshot-open", "snapshot-read", "lock"])
@pytest.mark.parametrize(
    "number, expected",
    [
        (number, "mesh_trust_storage_failure")
        for number in (errno.ENOSPC, errno.EDQUOT, errno.EIO, errno.EROFS, errno.EMFILE, errno.ENFILE)
    ]
    + [(number, "mesh_trust_unsafe") for number in (errno.EACCES, errno.ELOOP)],
    ids=lambda value: errno.errorcode[value] if isinstance(value, int) else None,
)
def test_load_classifies_operational_errors_without_changing_state(trust, home, monkeypatch, point, number, expected):
    import fcntl

    bootstrap(trust, home)
    accept(trust, home)
    before = saved(home).read_bytes()
    reached = []
    original_open = dirfd.open_child_file
    original_fdopen = os.fdopen

    def fail(*args, **kwargs):
        reached.append(point)
        raise OSError(number, "private storage/path diagnostic")

    def open_child(parent, name, flags, *args, **kwargs):
        if name == saved(home).name:
            fail()
        return original_open(parent, name, flags, *args, **kwargs)

    def fdopen(descriptor, mode, *args, **kwargs):
        stream = original_fdopen(descriptor, mode, *args, **kwargs)
        assert mode == "rb"
        monkeypatch.setattr(stream, "read", fail)
        return stream

    with monkeypatch.context() as fault:
        if point == "initial-open":
            fault.setattr(dirfd, "open_directory_nofollow", fail)
        elif point == "snapshot-open":
            fault.setattr(dirfd, "open_child_file", open_child)
        elif point == "snapshot-read":
            fault.setattr(os, "fdopen", fdopen)
        else:
            fault.setattr(fcntl, "flock", fail)
        with pytest.raises(crypto.MeshError) as caught:
            trust.load_trust(brigade_home=home)
        assert caught.value.code == expected
        assert str(caught.value) == expected
        assert reached == [point]
    assert saved(home).read_bytes() == before
    assert trust.load_trust(brigade_home=home).data == json.loads(before)


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


@pytest.mark.parametrize("point", ["close-before", "close-after", "write-and-close", "file-fsync-and-close"])
def test_writable_close_failure_never_publishes_or_masks_storage_error(trust, home, monkeypatch, point):
    bootstrap(trust, home)
    before = saved(home).read_bytes()
    raw = signed(seq=2)
    original_open = dirfd.open_child_file
    original_close = os.close
    original_fdopen = os.fdopen
    original_sync = os.fsync
    writable = None
    retained = None
    closes = []
    replacements = []

    def open_child(parent, name, flags, *args, **kwargs):
        nonlocal writable
        descriptor = original_open(parent, name, flags, *args, **kwargs)
        if flags & os.O_WRONLY:
            writable = descriptor
        return descriptor

    def fail(*args, **kwargs):
        raise OSError("private write/close diagnostic")

    def fdopen(descriptor, mode, *args, **kwargs):
        stream = original_fdopen(descriptor, mode, *args, **kwargs)
        if descriptor == writable and point == "write-and-close":
            monkeypatch.setattr(stream, "write", fail)
        return stream

    def sync(descriptor):
        if descriptor == writable and point == "file-fsync-and-close":
            fail()
        original_sync(descriptor)

    def close(descriptor):
        nonlocal retained
        if descriptor == writable:
            closes.append(descriptor)
            if len(closes) == 1:
                if point == "close-before":
                    retained = descriptor  # Model the bounded case where close did not consume the fd.
                else:
                    original_close(descriptor)
                    retained = os.open(os.devnull, os.O_RDONLY)
                    if retained != descriptor:
                        os.dup2(retained, descriptor)
                        original_close(retained)
                        retained = descriptor
                fail()  # Ownership is ambiguous, so the transaction must never retry close.
        original_close(descriptor)

    def replace(*args, **kwargs):
        replacements.append(args)
        return original_replace(*args, **kwargs)

    original_replace = dirfd.replace_children
    try:
        with monkeypatch.context() as fault:
            fault.setattr(dirfd, "open_child_file", open_child)
            fault.setattr(dirfd, "replace_children", replace)
            fault.setattr(os, "close", close)
            fault.setattr(os, "fdopen", fdopen)
            fault.setattr(os, "fsync", sync)
            with pytest.raises(crypto.MeshError) as caught:
                accept(trust, home, raw)
            assert caught.value.code == "mesh_trust_storage_failure"
            assert str(caught.value) == "mesh_trust_storage_failure"
            assert replacements == []
            assert writable is not None and closes == [writable]
            assert retained is not None
            os.fstat(retained)  # A descriptor reused after kernel-close must remain owned by its new caller.
        assert saved(home).read_bytes() == before
        assert not list(saved(home).parent.glob(".trust-*.tmp"))
        state = trust.load_trust(brigade_home=home).data
        assert state["objects"] == state["marks"] == state["conflicts"] == {}
    finally:
        if retained is not None:
            original_close(retained)
    assert accept(trust, home, raw).status == "accepted"
    state = trust.load_trust(brigade_home=home).data
    assert state["generation"] == json.loads(before)["generation"] + 1
    assert set(state["objects"]) == {digest(raw)}
    assert accept(trust, home, raw).status == "replay"


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

    def repeat_accepted_raw():
        unchanged = saved(home).read_bytes()
        result = accept(trust, home, raw)
        assert result.status == "signed_statement_conflict"
        assert result.digest == digest(raw)
        assert result.evidence_recorded and result.storage_error is None
        assert saved(home).read_bytes() == unchanged

    # Repeating either side reuses evidence, even after a later floor is installed.
    for operation in (lambda: floor(trust, home, target), repeat_accepted_raw):
        unchanged = saved(home).read_bytes()
        operation()
        assert saved(home).read_bytes() == unchanged
    old_conflicts = after.data["conflicts"]
    later = floor(trust, home, signed(seq=7))
    assert later.data["conflicts"] == old_conflicts
    repeat_accepted_raw()
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


@pytest.mark.parametrize("extra", [0, 1, 3], ids=["exact-cap", "cap-plus-one", "bounded-scan"])
def test_directory_entry_capacity_precedes_cleanup_and_publication(trust, home, monkeypatch, extra):
    bootstrap(trust, home)
    orphan = abandoned_trust_temp(home)
    directory = saved(home).parent
    for name, mode in [("unrelated", 0o600), ("." + "b" * 32 + ".tmp", 0o600), (".trust-" + "c" * 32 + ".tmp", 0o640)]:
        path = directory / name
        path.write_bytes(b"not a safe trust orphan")
        path.chmod(mode)
    for index in range(extra):
        (directory / f"extra-{index}").write_bytes(b"unrelated entry")
    before = {path.name: (path.stat().st_ino, path.read_bytes()) for path in directory.iterdir()}
    cap = 5  # Snapshot, safe orphan, unrelated file, custody temporary and unsafe trust temporary.
    monkeypatch.setattr(trust, "_MAX_DIRECTORY_ENTRIES", cap, raising=False)
    original_names = dirfd.child_names
    original_open = dirfd.open_child_file
    original_unlink = dirfd.unlink_child
    original_replace = dirfd.replace_children
    scans, mutations = [], []

    def names(parent, limit):
        result = original_names(parent, limit)
        scans.append(len(result))
        return result

    def open_child(parent, name, flags, *args, **kwargs):
        if flags & os.O_CREAT:
            mutations.append("create")
        assert name != orphan.name, "cleanup opened orphan contents"
        return original_open(parent, name, flags, *args, **kwargs)

    def unlink(*args, **kwargs):
        mutations.append("unlink")
        return original_unlink(*args, **kwargs)

    def replace(*args, **kwargs):
        mutations.append("replace")
        return original_replace(*args, **kwargs)

    monkeypatch.setattr(dirfd, "child_names", names)
    monkeypatch.setattr(dirfd, "open_child_file", open_child)
    monkeypatch.setattr(dirfd, "unlink_child", unlink)
    monkeypatch.setattr(dirfd, "replace_children", replace)
    raw = signed(seq=2)
    if extra:
        with pytest.raises(crypto.MeshError, match="^mesh_trust_capacity$"):
            accept(trust, home, raw)
        assert mutations == []
        assert {path.name: (path.stat().st_ino, path.read_bytes()) for path in directory.iterdir()} == before
        state = trust.load_trust(brigade_home=home).data
        assert state["objects"] == state["marks"] == state["conflicts"] == {}
    else:
        assert accept(trust, home, raw).status == "accepted"
        assert not orphan.exists()
        for name, contents in before.items():
            if name not in {orphan.name, saved(home).name}:
                path = directory / name
                assert (path.stat().st_ino, path.read_bytes()) == contents
        state = trust.load_trust(brigade_home=home).data
        assert state["generation"] == json.loads(before[saved(home).name][1])["generation"] + 1
        assert set(state["objects"]) == {digest(raw)}
    assert scans and max(scans) <= cap + 1


def test_overfull_directory_load_and_replay_do_not_enumerate(trust, home, monkeypatch):
    bootstrap(trust, home)
    raw = signed()
    accept(trust, home, raw)
    orphan = abandoned_trust_temp(home)
    before = saved(home).read_bytes()
    monkeypatch.setattr(trust, "_MAX_DIRECTORY_ENTRIES", 1, raising=False)

    def forbidden(*args, **kwargs):
        pytest.fail("load or replay enumerated an overfull directory")

    monkeypatch.setattr(dirfd, "child_names", forbidden)
    assert digest(raw) in trust.load_trust(brigade_home=home).data["objects"]
    assert accept(trust, home, raw).status == "replay"
    assert orphan.exists() and saved(home).read_bytes() == before


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


@pytest.mark.parametrize("reverse_objects", [False, True])
@pytest.mark.parametrize("reverse_floors", [False, True])
def test_snapshot_floor_predicate_across_scopes_ties_and_insertion_order(trust, reverse_objects, reverse_floors):
    """View computation only; stored-state authentication remains independently tested.

    The two same-scope/sequence digests exercise the complete existential view
    predicate, even though the store separately refuses accepting that conflict.
    """
    third = "h_" + crypto.encode_binary(bytes([2]) * 16)
    fourth = "h_" + crypto.encode_binary(bytes([3]) * 16)
    objects = {}
    by_label = {}

    def add(label, hub, seed, kind, seq, entries=None):
        raw = signed(hub=hub, seed=seed, kind=kind, seq=seq, entries=entries)
        value = digest(raw)
        objects[value] = {"hub": hub, "signer": signer(seed), "kind": kind, "canonical": raw.decode("ascii")}
        by_label[label] = value

    add("exact-low", HUB, SEED, "membership", 3)
    add("exact", HUB, SEED, "membership", 5)
    add("lower", OTHER, SEED, "membership", 4)
    add("higher-equal", HUB, SEED2, "membership", 5)
    add("higher", HUB, SEED2, "membership", 6)
    add("kind", HUB, SEED, "revocation", 5)
    add("wrong-digest", OTHER, SEED2, "revocation", 5)
    add("wrong-seq-low", third, SEED, "membership", 3)
    add("wrong-seq-equal", third, SEED, "membership", 5)
    add("tie-low", third, SEED2, "membership", 3)
    add("tie-a", third, SEED2, "membership", 5, ["test-one"])
    add("tie-b", third, SEED2, "membership", 5, ["test-two"])
    unknown = hashlib.sha256(bytes([9]) * 32).hexdigest()
    cases = [
        ("exact", HUB, signer(), "membership", by_label["exact"]),
        ("lower", OTHER, signer(), "membership", "0" * 64),
        ("higher", HUB, signer(SEED2), "membership", "0" * 64),
        ("kind", HUB, signer(), "revocation", by_label["kind"]),
        ("wrong-digest", OTHER, signer(SEED2), "revocation", "0" * 64),
        ("wrong-sequence", third, signer(), "membership", by_label["wrong-seq-low"]),
        ("multiple-digests", third, signer(SEED2), "membership", by_label["tie-b"]),
        ("absent", fourth, signer(), "membership", "0" * 64),
        ("unknown-signer", HUB, unknown, "membership", "0" * 64),
    ]
    if reverse_objects:
        objects = dict(reversed(list(objects.items())))
    if reverse_floors:
        cases.reverse()
    floors = {}
    expected = []
    # Explicit outcomes, independent of the implementation's matching logic.
    unsatisfied = {"lower", "wrong-digest", "wrong-sequence", "absent", "unknown-signer"}
    for label, hub, stream_signer, kind, required_digest in cases:
        value = {"kind": kind, "signer": stream_signer, "hub": hub, "seq": 5, "digest": required_digest}
        floors[f"{kind}:{stream_signer}:{hub}"] = value
        if label in unsatisfied:
            expected.append(value)
    state = {"objects": objects, "floors": floors}
    snapshot = trust._snapshot(state)
    assert snapshot.unsatisfied_floors == tuple(expected)
    assert snapshot.data == state
    assert len(snapshot.unsatisfied_floors) == 5


def test_unsigned_after_signed_retains_all_claims_minima_and_detached_views(trust, home):
    bootstrap(trust, home)
    raw = signed(kind="revocation", entries=[revoked(), revoked(kid=KID2)])
    accept(trust, home, raw, kind="revocation")
    trust.record_hub_revocation(
        brigade_home=home, hub=HUB, principal="bogus-node", kid=KID, cutoff="2026-01-03T00:00:00Z"
    )
    first = trust.load_trust(brigade_home=home)
    identity = next(iter(first.data["binding_conflicts"]))
    assert first.data["barriers"][HUB + ":" + KID]["cutoff"] == TIME
    for cutoff in ("2026-01-03T01:00:00+01:00", "2026-01-04T00:00:00Z"):
        before = saved(home).read_bytes()
        trust.record_hub_revocation(brigade_home=home, hub=HUB, principal="bogus-node", kid=KID, cutoff=cutoff)
        assert saved(home).read_bytes() == before
    trust.record_hub_revocation(
        brigade_home=home, hub=HUB, principal="agent:test", kid=KID, cutoff="2026-01-01T23:00:00Z"
    )
    state = trust.record_hub_revocation(
        brigade_home=home, hub=HUB, principal="bogus-node", kid=KID, cutoff="2026-01-01T00:00:00Z"
    )
    barrier = state.data["barriers"][HUB + ":" + KID]
    assert barrier["principals"] == ["agent:test", "bogus-node", "test-node"]
    assert barrier["cutoff"] == "2026-01-01T00:00:00Z"
    assert len(barrier["sources"]) == 3 and len(state.data["hub_revocations"]) == 2
    assert set(state.data["binding_conflicts"]) == {identity}
    assert state.data["binding_conflicts"][identity]["claims"] == barrier["sources"]
    assert state.data["marks"] == first.data["marks"] and state.data["baselines"] == first.data["baselines"]
    assert state.data["objects"] == first.data["objects"]
    before = saved(home).read_bytes()
    barrier["principals"].clear()
    state.data["binding_conflicts"][identity]["claims"][0]["principal"] = "test-forged"
    assert trust.load_trust(brigade_home=home).data == json.loads(before)
    replay = accept(trust, home, raw, kind="revocation")
    assert replay.status == "replay" and replay.evidence_recorded
    assert saved(home).read_bytes() == before


def test_replaced_signer_cumulative_lists_revoke_unrelated_keys_and_preserve_history(trust, home):
    bootstrap(trust, home)
    earlier = "2026-01-01T00:00:00Z"
    first = signed(kind="revocation", entries=[revoked(principal="bogus-node", cutoff=earlier)])
    accept(trust, home, first, kind="revocation")
    pin(trust, home, seed=SEED2, previous=signer())
    second = signed(kind="revocation", seq=0, seed=SEED2, entries=[revoked(), revoked(kid=KID2)])
    result = accept(trust, home, second, kind="revocation")
    assert result.status == "accepted" and result.evidence_recorded
    state = trust.load_trust(brigade_home=home).data
    identity = next(iter(state["binding_conflicts"]))
    third_kid = "k1_" + crypto.encode_binary(bytes([2]) * 16)
    third = signed(kind="revocation", seq=1, seed=SEED2, entries=[revoked(), revoked(kid=KID2), revoked(kid=third_kid)])
    result = accept(trust, home, third, kind="revocation")
    assert result.status == "accepted" and result.evidence_recorded
    state = trust.load_trust(brigade_home=home).data
    assert len(state["barriers"]) == len(state["objects"]) == 3
    assert set(state["binding_conflicts"]) == {identity}
    assert len(state["binding_conflicts"][identity]["claims"]) == 3
    assert {c["source"] for c in state["binding_conflicts"][identity]["claims"]} == {
        digest(first),
        digest(second),
        digest(third),
    }
    assert state["barriers"][HUB + ":" + KID]["cutoff"] == earlier
    assert state["marks"]["revocation:" + signer()]["seq"] == 1
    assert state["marks"]["revocation:" + signer(SEED2)]["digest"] == digest(third)
    before = saved(home).read_bytes()
    with pytest.raises(crypto.MeshError, match="^statement_signature_invalid$"):
        accept(trust, home, first, kind="revocation")
    assert saved(home).read_bytes() == before
    script = """
from pathlib import Path
import sys
from brigade import fleet_mesh_trust as trust
state = trust.load_trust(brigade_home=Path(sys.argv[1])).data
assert len(state["objects"]) == len(state["barriers"]) == 3
assert len(state["binding_conflicts"]) == 1
assert len(next(iter(state["binding_conflicts"].values()))["claims"]) == 3
"""
    result = subprocess.run([sys.executable, "-c", script, str(home)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    replay = accept(trust, home, third, kind="revocation")
    assert replay.status == "replay" and replay.evidence_recorded
    assert saved(home).read_bytes() == before
    pin(trust, home, previous=signer(SEED2))
    before = saved(home).read_bytes()
    with pytest.raises(crypto.MeshError, match="^mesh_trust_revocation_not_cumulative$"):
        accept(trust, home, kind="revocation", seq=2, entries=[revoked(), revoked(kid=KID2)])
    assert saved(home).read_bytes() == before


def test_same_signer_principal_relabel_refuses_entire_list_without_mutation(trust, home):
    bootstrap(trust, home)
    accept(trust, home, kind="revocation", entries=[revoked(principal="bogus-node")])
    before = saved(home).read_bytes()
    with pytest.raises(crypto.MeshError, match="^mesh_trust_revocation_not_cumulative$"):
        accept(trust, home, kind="revocation", seq=2, entries=[revoked(), revoked(kid=KID2)])
    assert saved(home).read_bytes() == before


@pytest.mark.parametrize(
    "case",
    [
        "principal-set",
        "source-principal",
        "source-digest",
        "source-cutoff",
        "missing-evidence",
        "identity",
        "unsigned-key",
        "foreign-signed-source",
        "missing-binding-table",
        "missing-authenticated-table",
        "extra-binding-group",
        "binding-in-authenticated",
        "authenticated-in-binding",
        "reason-only-reserve",
        "forged-authenticated-payload",
        "legacy-shared-table",
    ],
)
def test_tampered_binding_claims_and_evidence_are_refused(trust, home, case):
    bootstrap(trust, home)
    membership = signed()
    accept(trust, home, membership)
    assert accept(trust, home, entries=["test-other"]).evidence_recorded
    trust.record_hub_revocation(brigade_home=home, hub=HUB, principal="bogus-node", kid=KID, cutoff=TIME)
    accept(trust, home, kind="revocation", entries=[revoked(), revoked(kid=KID2)])
    state = json.loads(saved(home).read_bytes())
    identity, evidence = next(iter(state["binding_conflicts"].items()))
    if case == "principal-set":
        state["barriers"][HUB + ":" + KID]["principals"] = ["test-node"]
    elif case == "source-principal":
        state["barriers"][HUB + ":" + KID]["sources"][0]["principal"] = "test-forged"
    elif case in {"source-digest", "foreign-signed-source"}:
        claim = next(c for c in evidence["claims"] if c["source"] != "hub")
        claim["source"] = "0" * 64 if case == "source-digest" else digest(membership)
    elif case == "source-cutoff":
        evidence["claims"][0]["cutoff"] = "2026-01-03T00:00:00Z"
    elif case == "missing-evidence":
        state["binding_conflicts"] = {}
    elif case == "identity":
        state["binding_conflicts"] = {"0" * 64: evidence}
    elif case == "unsigned-key":
        next(iter(state["hub_revocations"].values()))["principal"] = "test-forged"
    elif case == "missing-binding-table":
        del state["binding_conflicts"]
    elif case == "missing-authenticated-table":
        del state["conflicts"]
    elif case == "extra-binding-group":
        state["binding_conflicts"]["0" * 64] = evidence
    elif case == "binding-in-authenticated":
        state["conflicts"][identity] = state["binding_conflicts"].pop(identity)
    elif case == "authenticated-in-binding":
        auth_id = next(iter(state["conflicts"]))
        state["binding_conflicts"][auth_id] = state["conflicts"].pop(auth_id)
    elif case == "reason-only-reserve":
        forged = dict(evidence, reason="signer_equivocation")
        state["conflicts"][hashlib.sha256(crypto.canonical(forged)).hexdigest()] = forged
    elif case == "legacy-shared-table":
        state["conflicts"].update(state.pop("binding_conflicts"))
    else:
        auth_id = next(iter(state["conflicts"]))
        forged = state["conflicts"].pop(auth_id)
        canonical = forged["rejected"]["canonical"].replace('"seq":1', '"seq":2')
        forged["rejected"]["canonical"] = canonical
        forged["digest"] = hashlib.sha256(canonical.encode("ascii")).hexdigest()
        state["conflicts"][hashlib.sha256(crypto.canonical(forged)).hexdigest()] = forged
    raw = crypto.canonical(state)
    saved(home).write_bytes(raw)
    with pytest.raises(crypto.MeshError, match="^mesh_trust_invalid$"):
        trust.load_trust(brigade_home=home)
    assert saved(home).read_bytes() == raw
    if case == "legacy-shared-table":
        with pytest.raises(crypto.MeshError, match="^mesh_trust_invalid$"):
            trust.initialize_trust(brigade_home=home)
        assert saved(home).read_bytes() == raw


@pytest.mark.parametrize("operation", ["signed", "unsigned"])
@pytest.mark.parametrize("point", ["file-close", "file-fsync", "replace", "directory-fsync"])
def test_binding_evidence_transaction_faults_publish_none_or_complete_generation(
    trust, home, monkeypatch, operation, point
):
    bootstrap(trust, home)
    raw = signed(kind="revocation", entries=[revoked(), revoked(kid=KID2)])
    if operation == "signed":
        trust.record_hub_revocation(brigade_home=home, hub=HUB, principal="bogus-node", kid=KID, cutoff=TIME)
    else:
        accept(trust, home, raw, kind="revocation")
    before = saved(home).read_bytes()

    def apply():
        if operation == "signed":
            return accept(trust, home, raw, kind="revocation")
        return trust.record_hub_revocation(brigade_home=home, hub=HUB, principal="bogus-node", kid=KID, cutoff=TIME)

    with monkeypatch.context() as fault:
        original = os.fsync
        original_open = dirfd.open_child_file
        original_close = os.close
        writable = None

        def open_child(parent, name, flags, *args, **kwargs):
            nonlocal writable
            descriptor = original_open(parent, name, flags, *args, **kwargs)
            if flags & os.O_WRONLY:
                writable = descriptor
            return descriptor

        def close(descriptor):
            original_close(descriptor)
            if descriptor == writable:
                raise OSError("private binding close fault")

        def sync(descriptor):
            directory = stat.S_ISDIR(os.fstat(descriptor).st_mode)
            if directory == (point == "directory-fsync"):
                raise OSError("private binding durability fault")
            original(descriptor)

        def replace(*args, **kwargs):
            raise OSError("private binding replacement fault")

        if point == "file-close":
            fault.setattr(dirfd, "open_child_file", open_child)
            fault.setattr(os, "close", close)
        elif point == "replace":
            fault.setattr(dirfd, "replace_children", replace)
        else:
            fault.setattr(os, "fsync", sync)
        code = "mesh_trust_durability_indeterminate" if point == "directory-fsync" else "mesh_trust_storage_failure"
        with pytest.raises(crypto.MeshError, match="^" + code + "$"):
            apply()
        if point == "directory-fsync":
            complete = saved(home).read_bytes()
            state = trust.load_trust(brigade_home=home).data
            assert len(state["binding_conflicts"]) == len(state["objects"]) == len(state["baselines"]) == 1
            assert len(state["barriers"]) == 2
            assert state["generation"] == json.loads(before)["generation"] + 1
            assert state["barriers"][HUB + ":" + KID]["principals"] == ["bogus-node", "test-node"]
            assert state["marks"]["revocation:" + signer()]["digest"] == digest(raw)
            with pytest.raises(crypto.MeshError, match="^mesh_trust_durability_indeterminate$"):
                apply()
            assert saved(home).read_bytes() == complete
        else:
            assert saved(home).read_bytes() == before
    result = apply()
    state = trust.load_trust(brigade_home=home).data
    assert len(state["binding_conflicts"]) == 1 and len(state["barriers"]) == 2
    assert state["conflicts"] == {}
    assert state["generation"] == json.loads(before)["generation"] + 1
    if operation == "signed":
        assert result.status == ("replay" if point == "directory-fsync" else "accepted")
        assert result.evidence_recorded and result.storage_error is None
    unchanged = saved(home).read_bytes()
    apply()
    assert saved(home).read_bytes() == unchanged


@pytest.mark.parametrize("bound", ["ordinary", "total", "object-count", "claim-count"])
def test_binding_capacity_refuses_complete_transaction_without_eviction(trust, home, monkeypatch, bound):
    bootstrap(trust, home)
    accept(trust, home)
    assert accept(trust, home, entries=["test-other"]).evidence_recorded
    trust.record_hub_revocation(brigade_home=home, hub=HUB, principal="bogus-node", kid=KID, cutoff=TIME)
    before = saved(home).read_bytes()
    if bound == "ordinary":
        ordinary = len(crypto.canonical(dict(json.loads(before), conflicts={})))
        monkeypatch.setattr(trust, "_ORDINARY_BYTES", ordinary + 50)
    elif bound == "total":
        monkeypatch.setattr(trust, "_MAX_BYTES", len(before) + 50)
    elif bound == "object-count":
        monkeypatch.setattr(trust, "_MAX_OBJECTS", 1)
    else:
        monkeypatch.setattr(trust, "_MAX_ENTRIES", 1)
    with pytest.raises(crypto.MeshError, match="^mesh_trust_capacity$"):
        accept(trust, home, kind="revocation", entries=[revoked(), revoked(kid=KID2)])
    assert saved(home).read_bytes() == before


def test_binding_evidence_must_fit_ordinary_bytes_despite_reserved_headroom(trust, home, monkeypatch):
    # Approved budget revision: binding groups consume ordinary bytes, not the authenticated reserve.
    homes = [home / name for name in ("control", "limited")]
    for directory in homes:
        directory.mkdir(mode=0o700)
        bootstrap(trust, directory)
        trust.record_hub_revocation(brigade_home=directory, hub=HUB, principal="bogus-node", kid=KID, cutoff=TIME)
    before = saved(homes[1]).read_bytes()
    raw = signed(kind="revocation", entries=[revoked(), revoked(kid=KID2)])
    assert accept(trust, homes[0], raw, kind="revocation").evidence_recorded
    complete = saved(homes[0]).read_bytes()
    # With no authenticated conflicts, this entire snapshot must fit ordinary bytes.
    bound = len(complete) - 1
    assert len(before) < bound < trust._MAX_BYTES
    monkeypatch.setattr(trust, "_ORDINARY_BYTES", bound)
    with pytest.raises(crypto.MeshError, match="^mesh_trust_capacity$"):
        accept(trust, homes[1], raw, kind="revocation")
    assert saved(homes[1]).read_bytes() == before
    assert trust.load_trust(brigade_home=homes[1]).data == json.loads(before)


def test_full_authenticated_conflict_count_allows_complete_binding_acceptance(trust, home, monkeypatch):
    # Approved replacement of the old conflict-count refusal branch; other refusal cases remain above.
    bootstrap(trust, home)
    accept(trust, home)
    assert accept(trust, home, entries=["test-other"]).evidence_recorded
    trust.record_hub_revocation(brigade_home=home, hub=HUB, principal="bogus-node", kid=KID, cutoff=TIME)
    before = trust.load_trust(brigade_home=home).data
    monkeypatch.setattr(trust, "_MAX_CONFLICTS", 1)
    raw = signed(kind="revocation", entries=[revoked(), revoked(kid=KID2)])
    result = accept(trust, home, raw, kind="revocation")
    assert result.status == "accepted" and result.evidence_recorded and result.storage_error is None
    state = trust.load_trust(brigade_home=home).data
    assert state["conflicts"] == before["conflicts"] and len(state["conflicts"]) == 1
    assert len(state["binding_conflicts"]) == 1
    assert state["barriers"][HUB + ":" + KID]["principals"] == ["bogus-node", "test-node"]
    assert state["barriers"][HUB + ":" + KID2]["principals"] == ["test-node"]
    assert state["marks"]["revocation:" + signer()]["digest"] == digest(raw)
    assert set(state["objects"]) == set(before["objects"]) | {digest(raw)}
    assert state["generation"] == before["generation"] + 1
    unchanged = saved(home).read_bytes()
    replay = accept(trust, home, raw, kind="revocation")
    assert replay.status == "replay" and replay.evidence_recorded and replay.storage_error is None
    assert saved(home).read_bytes() == unchanged


@pytest.mark.parametrize("allegations", [False, True], ids=["control", "unsigned-other-hub"])
def test_unsigned_groups_leave_authenticated_conflict_and_floor_reserve(trust, home, monkeypatch, allegations):
    bootstrap(trust, home)
    accepted = signed()
    accept(trust, home, accepted)
    monkeypatch.setattr(trust, "_MAX_CONFLICTS", 2)
    monkeypatch.setattr(trust, "_MAX_ENTRIES", 4)
    if allegations:
        # Two disputed kids saturate the four-entry unsigned-claim source table.
        for kid in (KID, KID2):
            for principal in ("bogus-one", "bogus-two"):
                trust.record_hub_revocation(brigade_home=home, hub=OTHER, principal=principal, kid=kid, cutoff=TIME)
    before = trust.load_trust(brigade_home=home).data
    equivocation = signed(entries=["test-other"])
    result = accept(trust, home, equivocation)
    assert result.status == "signed_statement_conflict"
    assert result.evidence_recorded and result.storage_error is None
    state = floor(trust, home, signed(entries=["floor-node"])).data
    assert {e["reason"] for e in state["conflicts"].values()} == {"signer_equivocation", "trusted_floor_contradiction"}
    assert len(state["conflicts"]) == 2 and len(state["floors"]) == 1
    assert len(state.get("binding_conflicts", {})) == (2 if allegations else 0)
    for table in ("objects", "marks", "baselines", "barriers", "hub_revocations"):
        assert state[table] == before[table]
    assert state.get("binding_conflicts", {}) == before.get("binding_conflicts", {})
    unchanged = saved(home).read_bytes()
    for raw in (equivocation, accepted):
        replay = accept(trust, home, raw)
        assert replay.status == "signed_statement_conflict" and replay.evidence_recorded
        assert replay.storage_error is None
    floor(trust, home, signed(entries=["floor-node"]))
    assert saved(home).read_bytes() == unchanged
    # The authenticated count is now genuinely exhausted; no third record can evict any evidence.
    refusal = accept(trust, home, entries=["test-third"])
    assert refusal.status == "signed_statement_conflict" and not refusal.evidence_recorded
    assert refusal.storage_error == "mesh_trust_capacity"
    assert saved(home).read_bytes() == unchanged


def test_binding_evidence_coexists_with_rejected_statement_conflicts(trust, home):
    bootstrap(trust, home)
    trust.record_hub_revocation(brigade_home=home, hub=HUB, principal="bogus-node", kid=KID, cutoff=TIME)
    revocations = signed(kind="revocation", entries=[revoked(), revoked(kid=KID2)])
    assert accept(trust, home, revocations, kind="revocation").evidence_recorded
    assert accept(trust, home, seq=1).status == "accepted"
    before = trust.load_trust(brigade_home=home).data
    equivocation = signed(seq=1, entries=["test-node"])
    assert accept(trust, home, equivocation).status == "signed_statement_conflict"
    floor(trust, home, signed(seq=3))
    contradiction = signed(seq=3, entries=["test-other"])
    assert accept(trust, home, contradiction).status == "signed_statement_conflict"
    state = trust.load_trust(brigade_home=home).data
    assert {evidence["reason"] for evidence in state["conflicts"].values()} == {
        "signer_equivocation",
        "trusted_floor_contradiction",
    }
    assert len(state["conflicts"]) == 2 and len(state["binding_conflicts"]) == 1
    assert state["binding_conflicts"] == before["binding_conflicts"]
    for name in ("objects", "marks", "baselines", "barriers", "hub_revocations"):
        assert state[name] == before[name]
    unchanged = saved(home).read_bytes()
    for raw in (equivocation, contradiction):
        result = accept(trust, home, raw)
        assert result.status == "signed_statement_conflict" and result.evidence_recorded
    result = accept(trust, home, revocations, kind="revocation")
    assert result.status == "replay" and result.evidence_recorded
    assert saved(home).read_bytes() == unchanged


@pytest.mark.parametrize("budget", ["ordinary", "total"])
def test_mixed_evidence_byte_boundaries_match_on_save_and_load(trust, home, monkeypatch, budget):
    homes = [home / name for name in ("control", "limited")]
    for directory in homes:
        directory.mkdir(mode=0o700)
        bootstrap(trust, directory)
        trust.record_hub_revocation(brigade_home=directory, hub=HUB, principal="bogus-node", kid=KID, cutoff=TIME)
        accept(trust, directory, kind="revocation", entries=[revoked(), revoked(kid=KID2)])
        accept(trust, directory)
    rejected = signed(entries=["test-other"])
    control = accept(trust, homes[0], rejected)
    assert control.status == "signed_statement_conflict" and control.evidence_recorded
    complete = saved(homes[0]).read_bytes()
    state = json.loads(complete)
    ordinary = len(crypto.canonical(dict(state, conflicts={})))
    assert len(state["binding_conflicts"]) == len(state["conflicts"]) == 1
    assert len(complete) - ordinary > 100
    with monkeypatch.context() as limits:
        limits.setattr(trust, "_ORDINARY_BYTES", ordinary + (100 if budget == "total" else 0))
        limits.setattr(trust, "_MAX_BYTES", len(complete) + (100 if budget == "ordinary" else 0))
        result = accept(trust, homes[1], rejected)
        assert result.status == "signed_statement_conflict" and result.evidence_recorded
        assert result.storage_error is None
        assert saved(homes[1]).read_bytes() == complete
        assert trust.load_trust(brigade_home=homes[1]).data == state
        if budget == "ordinary":
            limits.setattr(trust, "_ORDINARY_BYTES", ordinary - 1)
        else:
            # Authenticated evidence can use unused ordinary space as well as the reserved difference.
            assert len(complete) - ordinary > trust._MAX_BYTES - trust._ORDINARY_BYTES
            limits.setattr(trust, "_MAX_BYTES", len(complete) - 1)
        with pytest.raises(crypto.MeshError, match="^mesh_trust_invalid$"):
            trust.load_trust(brigade_home=homes[1])
        assert saved(homes[1]).read_bytes() == complete


def test_binding_group_entry_boundary_keeps_authenticated_table_independent(trust, home, monkeypatch):
    bootstrap(trust, home)
    accepted = signed(kind="revocation", entries=[revoked(), revoked(kid=KID2)])
    accept(trust, home, accepted, kind="revocation")
    monkeypatch.setattr(trust, "_MAX_ENTRIES", 2)
    monkeypatch.setattr(trust, "_MAX_CONFLICTS", 2)
    for kid in (KID, KID2):
        trust.record_hub_revocation(brigade_home=home, hub=HUB, principal="bogus-node", kid=kid, cutoff=TIME)
    before = trust.load_trust(brigade_home=home).data
    assert len(before["binding_conflicts"]) == 2
    result = accept(trust, home, kind="revocation", entries=[revoked()])
    assert result.status == "signed_statement_conflict" and result.evidence_recorded
    state = floor(trust, home, signed(kind="revocation"), kind="revocation").data
    assert len(state["conflicts"]) == len(state["binding_conflicts"]) == 2
    assert state["binding_conflicts"] == before["binding_conflicts"]
    for table in ("objects", "marks", "baselines", "barriers", "hub_revocations"):
        assert state[table] == before[table]
    assert trust.load_trust(brigade_home=home).data == state


@pytest.mark.parametrize("bound", ["unsigned-claims", "barriers", "sources"])
def test_ordinary_source_bounds_preserve_all_evidence_and_heads(trust, home, monkeypatch, bound):
    bootstrap(trust, home)
    if bound == "sources":
        accept(trust, home, kind="revocation", entries=[revoked()])
        accept(trust, home, kind="revocation", seq=2, entries=[revoked()])
    else:
        for index, principal in enumerate(("test-one", "test-two")):
            trust.record_hub_revocation(
                brigade_home=home,
                hub=HUB,
                principal=principal,
                kid=KID if bound == "unsigned-claims" or index == 0 else KID2,
                cutoff=TIME,
            )
    before = saved(home).read_bytes()
    monkeypatch.setattr(trust, "_MAX_ENTRIES", 2)
    with pytest.raises(crypto.MeshError, match="^mesh_trust_capacity$"):
        if bound == "barriers":
            third = "k1_" + crypto.encode_binary(bytes([2]) * 16)
            accept(trust, home, kind="revocation", entries=[revoked(), revoked(kid=KID2), revoked(kid=third)])
        else:
            trust.record_hub_revocation(
                brigade_home=home,
                hub=HUB,
                principal="test-node",
                cutoff=TIME,
                kid=KID2 if bound == "unsigned-claims" else KID,
            )
    assert saved(home).read_bytes() == before
    assert trust.load_trust(brigade_home=home).data == json.loads(before)


@pytest.fixture
def trust_directory_close_fault(monkeypatch):
    @contextmanager
    def inject(home, point, number=errno.EIO):
        original_open, original_close = os.open, os.close
        seen = {"owned": set(), "root": None, "final": None, "parent": None, "fault": None, "reused": None, "calls": 0}

        original_root = dirfd.open_directory_nofollow
        original_child = dirfd.open_child_directory

        def open_root(path, *args, **kwargs):
            descriptor = original_root(path, *args, **kwargs)
            seen["owned"].add(descriptor)
            seen["root"] = descriptor
            return descriptor

        def open_child(parent, name, *args, **kwargs):
            descriptor = original_child(parent, name, *args, **kwargs)
            seen["owned"].add(descriptor)
            if name == "mesh-keys":
                seen["final"], seen["parent"] = descriptor, parent
            return descriptor

        def closed(descriptor):
            if descriptor == seen["fault"]:
                seen["calls"] += 1
            target = (
                seen["root"] if point == "parent" else seen["parent"] if point == "validation-parent" else seen["final"]
            )
            fail = seen["fault"] is None and descriptor == target
            original_close(descriptor)
            seen["owned"].discard(descriptor)
            if fail:
                seen["fault"], seen["calls"] = descriptor, 1
                replacement = original_open(os.devnull, os.O_RDONLY)
                if replacement != descriptor:
                    os.dup2(replacement, descriptor)
                    original_close(replacement)
                seen["reused"] = descriptor
                raise OSError(number, "private directory close diagnostic", str(home))

        try:
            with monkeypatch.context() as fault:
                fault.setattr(dirfd, "open_directory_nofollow", open_root)
                fault.setattr(dirfd, "open_child_directory", open_child)
                fault.setattr(os, "close", closed)
                yield seen
        finally:
            # The fake owns the replacement and any descriptors leaked by the old walker.
            for descriptor in seen["owned"] | ({seen["reused"]} if seen["reused"] is not None else set()):
                try:
                    original_close(descriptor)
                except OSError as error:
                    assert error.errno == errno.EBADF

    return inject


def test_consumed_parent_directory_close_preserves_new_owner_and_cleans_child(trust, home, trust_directory_close_fault):
    bootstrap(trust, home)
    before = saved(home).read_bytes()
    with trust_directory_close_fault(home, "parent") as seen:
        with pytest.raises(crypto.MeshError, match="^mesh_trust_storage_failure$"):
            trust.load_trust(brigade_home=home)
        assert seen["calls"] == 1 and seen["reused"] is not None
        os.fstat(seen["reused"])
        assert seen["owned"] == set()
        assert saved(home).read_bytes() == before
    assert trust.load_trust(brigade_home=home).data == json.loads(before)


@pytest.mark.parametrize("operation", ["load", "initialize", "import"])
@pytest.mark.parametrize(
    "number, code", [(errno.EIO, "mesh_trust_storage_failure"), (errno.EACCES, "mesh_trust_unsafe")]
)
def test_final_directory_close_has_fixed_diagnosis_and_idempotent_retry(
    trust, home, trust_directory_close_fault, operation, number, code
):
    if operation != "initialize":
        bootstrap(trust, home)
    before = None if operation == "initialize" else saved(home).read_bytes()
    raw = signed(seq=2)
    with trust_directory_close_fault(home, "final", number) as seen:
        with pytest.raises(crypto.MeshError) as caught:
            if operation == "import":
                accept(trust, home, raw)
            elif operation == "initialize":
                trust.initialize_trust(brigade_home=home)
            else:
                trust.load_trust(brigade_home=home)
        assert caught.value.code == str(caught.value) == code
        assert seen["calls"] == 1 and seen["owned"] == set()
        os.fstat(seen["reused"])
    complete = saved(home).read_bytes()
    state = trust.load_trust(brigade_home=home).data
    if operation == "load":
        assert complete == before
    elif operation == "import":
        assert state["generation"] == json.loads(before)["generation"] + 1
        assert set(state["objects"]) == {digest(raw)}
        assert state["marks"]["membership:" + signer()]["digest"] == digest(raw)
        assert accept(trust, home, raw).status == "replay"
    else:
        assert state["generation"] == 1
        assert trust.initialize_trust(brigade_home=home).data == state
    assert saved(home).read_bytes() == complete


@pytest.mark.parametrize("primary", ["backend", "extra", "cancel", "unsafe-child", "unsafe-parent"])
def test_directory_cleanup_preserves_primary_failure(trust, home, monkeypatch, trust_directory_close_fault, primary):
    class Cancelled(BaseException):
        pass

    bootstrap(trust, home)
    accept(trust, home)
    before = saved(home).read_bytes()
    error = (
        crypto.MeshError("mesh_backend_unsupported")
        if primary == "backend"
        else crypto.MeshExtraMissing()
        if primary == "extra"
        else Cancelled()
    )
    point = "validation-parent" if primary == "unsafe-parent" else "final"
    try:
        with monkeypatch.context() as failure:
            if primary.startswith("unsafe"):
                saved(home).parent.chmod(0o750)
            else:

                def unavailable():
                    raise error

                failure.setattr(crypto, "probe_backend", unavailable)
            with trust_directory_close_fault(home, point) as seen:
                with pytest.raises(crypto.MeshError if primary.startswith("unsafe") else type(error)) as caught:
                    if primary.startswith("unsafe"):
                        trust.initialize_trust(brigade_home=home)
                    else:
                        trust.load_trust(brigade_home=home)
                if primary.startswith("unsafe"):
                    assert caught.value.code == str(caught.value) == "mesh_trust_unsafe"
                else:
                    assert caught.value is error
                assert seen["calls"] == 1 and seen["owned"] == set()
                os.fstat(seen["reused"])
                assert saved(home).read_bytes() == before
    finally:
        saved(home).parent.chmod(0o700)
    assert trust.load_trust(brigade_home=home).data == json.loads(before)


@pytest.fixture
def trust_read_cleanup_fault(monkeypatch):
    @contextmanager
    def inject(point, error):
        import fcntl

        original_open, original_close, original_flock = dirfd.open_child_file, os.close, fcntl.flock
        seen = {
            "source": None,
            "source_done": False,
            "source_attempts": 0,
            "directory": None,
            "directory_closes": 0,
            "acquires": 0,
            "unlocks": 0,
            "faults": 0,
            "replacement": None,
            "replacement_live": None,
        }

        def opened(parent, name, flags, *args, **kwargs):
            descriptor = original_open(parent, name, flags, *args, **kwargs)
            if name == "trust-state.json" and not flags & os.O_WRONLY:
                seen["source"], seen["source_done"] = descriptor, False
            return descriptor

        def closed(descriptor):
            if descriptor == seen["directory"]:
                seen["directory_closes"] += 1
            source_attempt = descriptor == seen["source"] and (
                not seen["source_done"] or point == "source-close" and seen["faults"]
            )
            if source_attempt:
                seen["source_attempts"] += 1
                seen["source_done"] = True
            original_close(descriptor)
            if source_attempt and point == "source-close" and not seen["faults"]:
                seen["faults"] += 1
                replacement = os.open(os.devnull, os.O_RDONLY)
                if replacement != descriptor:
                    os.dup2(replacement, descriptor)
                    original_close(replacement)
                seen["replacement"] = descriptor
                raise error

        def flock(descriptor, operation):
            if operation == fcntl.LOCK_UN:
                seen["unlocks"] += 1
                original_flock(descriptor, operation)
                if point == "unlock":
                    seen["faults"] += 1
                    raise error
            else:
                seen["directory"] = descriptor
                seen["acquires"] += 1
                if point == "acquire":
                    seen["faults"] += 1
                    raise error
                original_flock(descriptor, operation)

        try:
            with monkeypatch.context() as fault:
                fault.setattr(dirfd, "open_child_file", opened)
                fault.setattr(os, "close", closed)
                fault.setattr(fcntl, "flock", flock)
                yield seen
        finally:
            # All fault patches are restored before assertions or pytest capture.
            if seen["replacement"] is not None:
                try:
                    os.fstat(seen["replacement"])
                    seen["replacement_live"] = True
                except OSError as failure:
                    assert failure.errno == errno.EBADF
                    seen["replacement_live"] = False
                else:
                    original_close(seen["replacement"])

    return inject


@pytest.mark.parametrize("point", ["source-close", "unlock"])
@pytest.mark.parametrize("primary", ["extra", "backend", "validation", "cancel"])
def test_read_cleanup_preserves_primary_diagnosis(trust, home, monkeypatch, trust_read_cleanup_fault, point, primary):
    class Cancelled(BaseException):
        pass

    bootstrap(trust, home)
    accept(trust, home)
    error = (
        crypto.MeshExtraMissing()
        if primary == "extra"
        else crypto.MeshError("mesh_backend_unsupported")
        if primary == "backend"
        else Cancelled()
    )
    if primary == "validation":
        state = json.loads(saved(home).read_bytes())
        state["v"] = 2
        saved(home).write_bytes(crypto.canonical(state))
    before = saved(home).read_bytes()
    caught = None
    with monkeypatch.context() as primary_fault:
        if primary != "validation":

            def unavailable():
                raise error

            primary_fault.setattr(crypto, "probe_backend", unavailable)
        with trust_read_cleanup_fault(point, OSError(errno.EIO, "private secondary cleanup")) as seen:
            try:
                trust.load_trust(brigade_home=home)
            except BaseException as failure:
                caught = failure
    if primary == "validation":
        assert type(caught) is crypto.MeshError
        assert caught.code == str(caught) == "mesh_trust_invalid"
        with pytest.raises(crypto.MeshError, match="^mesh_trust_invalid$"):
            trust.load_trust(brigade_home=home)
    else:
        assert caught is error
        assert trust.load_trust(brigade_home=home).data == json.loads(before)
    assert seen["faults"] == seen["unlocks"] == seen["directory_closes"] == 1
    assert seen["source_attempts"] == 1
    if point == "source-close":
        assert seen["replacement_live"] is True
    assert saved(home).read_bytes() == before


@pytest.mark.parametrize("point", ["source-close", "unlock"])
@pytest.mark.parametrize("cleanup", ["io", "permission", "cancel"])
def test_cleanup_only_failure_is_not_success_and_retains_error_policy(
    trust, home, trust_read_cleanup_fault, point, cleanup
):
    class Cancelled(BaseException):
        pass

    bootstrap(trust, home)
    accept(trust, home)
    before = saved(home).read_bytes()
    error = (
        Cancelled()
        if cleanup == "cancel"
        else OSError(errno.EIO if cleanup == "io" else errno.EACCES, "private cleanup-only diagnostic")
    )
    caught = None
    with trust_read_cleanup_fault(point, error) as seen:
        try:
            trust.load_trust(brigade_home=home)
        except BaseException as failure:
            caught = failure
    if cleanup == "cancel":
        assert caught is error
    else:
        assert type(caught) is crypto.MeshError
        expected = "mesh_trust_storage_failure" if cleanup == "io" else "mesh_trust_unsafe"
        assert caught.code == str(caught) == expected
    assert seen["faults"] == seen["unlocks"] == seen["directory_closes"] == 1
    assert seen["source_attempts"] == 1
    if point == "source-close":
        assert seen["replacement_live"] is True
    assert saved(home).read_bytes() == before
    assert trust.load_trust(brigade_home=home).data == json.loads(before)


@pytest.mark.parametrize("cleanup", ["io", "cancel"])
def test_unlock_lost_return_reloads_binding_generation_and_replays(trust, home, trust_read_cleanup_fault, cleanup):
    class Cancelled(BaseException):
        pass

    bootstrap(trust, home)
    trust.record_hub_revocation(brigade_home=home, hub=HUB, principal="bogus-node", kid=KID, cutoff=TIME)
    before = trust.load_trust(brigade_home=home).data
    raw = signed(kind="revocation", entries=[revoked(), revoked(kid=KID2)])
    error = OSError(errno.EIO, "private lost-return unlock") if cleanup == "io" else Cancelled()
    caught = None
    with trust_read_cleanup_fault("unlock", error) as seen:
        try:
            accept(trust, home, raw, kind="revocation")
        except BaseException as failure:
            caught = failure
    if cleanup == "cancel":
        assert caught is error
    else:
        assert type(caught) is crypto.MeshError
        assert caught.code == str(caught) == "mesh_trust_storage_failure"
    assert seen["faults"] == seen["unlocks"] == seen["directory_closes"] == 1
    complete = saved(home).read_bytes()
    state = trust.load_trust(brigade_home=home).data
    assert state["generation"] == before["generation"] + 1
    assert set(state["objects"]) == {digest(raw)}
    assert state["marks"]["revocation:" + signer()]["digest"] == digest(raw)
    assert len(state["binding_conflicts"]) == 1 and state["conflicts"] == {}
    retry = accept(trust, home, raw, kind="revocation")
    assert retry.status == "replay" and retry.evidence_recorded and retry.storage_error is None
    assert saved(home).read_bytes() == complete


def test_failed_lock_acquisition_never_unlocks_and_directory_is_closed(trust, home, trust_read_cleanup_fault):
    bootstrap(trust, home)
    before = saved(home).read_bytes()
    caught = None
    with trust_read_cleanup_fault("acquire", OSError(errno.EIO, "private acquire fault")) as seen:
        try:
            trust.load_trust(brigade_home=home)
        except BaseException as failure:
            caught = failure
    assert type(caught) is crypto.MeshError
    assert caught.code == str(caught) == "mesh_trust_storage_failure"
    assert seen["acquires"] == seen["faults"] == seen["directory_closes"] == 1
    assert seen["unlocks"] == seen["source_attempts"] == 0
    assert saved(home).read_bytes() == before


@pytest.fixture
def buffered_snapshot_fault(monkeypatch):
    """Real buffered writes, including close-time reflush and consumed FD faults."""
    import io
    from contextlib import contextmanager

    @contextmanager
    def inject(primary, secondary, *, partial=False, extra_cleanup=False):
        original_fdopen, original_close, original_unlink = os.fdopen, os.close, dirfd.unlink_child
        seen = {
            "descriptor": None,
            "closes": 0,
            "stream_closes": 0,
            "writes": [],
            "streams": [],
            "unlinks": 0,
            "replacement": None,
            "replacement_live": None,
        }

        class ScriptedRaw(io.FileIO):
            def write(self, data):
                seen["writes"].append(len(data))
                if primary is None:
                    return super().write(data)
                if partial and len(seen["writes"]) == 1:
                    # Leave a tail buffered after a genuine partial write.
                    return super().write(data[:-64])
                raise primary if len(seen["writes"]) == (2 if partial else 1) else secondary

            def close(self):
                super().close()  # closefd=False: the snapshot writer still owns the FD.
                if primary is None:
                    raise secondary

        class TrackedBuffer(io.BufferedWriter):
            def close(self):
                seen["stream_closes"] += 1
                super().close()

        def fdopen(fd, mode="r", *args, **kwargs):
            if mode != "wb":
                return original_fdopen(fd, mode, *args, **kwargs)
            seen["descriptor"] = fd
            stream = TrackedBuffer(
                ScriptedRaw(fd, "wb", closefd=False), buffer_size=128 if partial else io.DEFAULT_BUFFER_SIZE
            )
            seen["streams"].append(stream)
            return stream

        def close(fd):
            original_close(fd)
            if fd == seen["descriptor"]:
                seen["closes"] += 1
                if extra_cleanup:
                    replacement = os.open(os.devnull, os.O_RDONLY)
                    if replacement != fd:
                        os.dup2(replacement, fd)
                        original_close(replacement)
                    seen["replacement"] = fd
                    raise BaseException("secondary consumed destination close")

        def unlink(parent, name, **kwargs):
            if name.startswith((".trust-", ".hub-identity-")):
                seen["unlinks"] += 1
                original_unlink(parent, name, **kwargs)
                if extra_cleanup:
                    raise BaseException("secondary temporary unlink lost return")
                return None
            return original_unlink(parent, name, **kwargs)

        try:
            with monkeypatch.context() as patch:
                patch.setattr(os, "fdopen", fdopen)
                patch.setattr(os, "close", close)
                patch.setattr(dirfd, "unlink_child", unlink)
                yield seen
        finally:
            # Fault patches have ended before assertions and pytest capture.
            if seen["replacement"] is not None:
                try:
                    os.fstat(seen["replacement"])
                except OSError:
                    seen["replacement_live"] = False
                else:
                    seen["replacement_live"] = True
                    original_close(seen["replacement"])

    return inject


@pytest.mark.parametrize(
    "operation, secondary, partial, extra_cleanup, primary_error",
    [
        ("initialize", "io", False, False, False),
        ("initialize", "cancel", False, False, False),
        ("conflict", "io", False, False, False),
        ("conflict", "cancel", False, False, False),
        ("accept", "io", True, False, False),
        ("accept", "io", False, True, False),
        ("conflict", "io", False, True, True),
    ],
)
def test_buffered_snapshot_preserves_primary_before_publish(
    trust, home, buffered_snapshot_fault, operation, secondary, partial, extra_cleanup, primary_error
):
    class Cancelled(BaseException):
        pass

    if operation != "initialize":
        bootstrap(trust, home)
        accept(trust, home)
    before = saved(home).read_bytes() if saved(home).exists() else None
    primary = RuntimeError("primary body error") if primary_error else Cancelled("primary buffered write")
    cleanup = (
        OSError(errno.EIO, "secondary stream reflush") if secondary == "io" else Cancelled("secondary stream reflush")
    )
    raw = signed(seq=1, entries=["test-peer"]) if operation == "conflict" else signed(seq=2)
    caught = None
    with buffered_snapshot_fault(primary, cleanup, partial=partial, extra_cleanup=extra_cleanup) as seen:
        try:
            if operation == "initialize":
                trust.initialize_trust(brigade_home=home)
            else:
                accept(trust, home, raw)
        except BaseException as error:
            caught = error
    assert caught is primary
    assert seen["closes"] == seen["stream_closes"] == seen["unlinks"] == 1
    assert len(seen["streams"]) == 1 and seen["streams"][0].closed
    assert len(seen["writes"]) == (3 if partial else 2)
    if partial:
        assert seen["writes"][0] > 128 and seen["writes"][1:] == [64, 64]
    if extra_cleanup:
        assert seen["replacement_live"] is True
    else:
        with pytest.raises(OSError) as closed:
            os.fstat(seen["descriptor"])
        assert closed.value.errno == errno.EBADF
    assert (saved(home).read_bytes() if saved(home).exists() else None) == before
    assert list(saved(home).parent.glob(".trust-*.tmp")) == []
    if operation != "initialize":
        state = trust.load_trust(brigade_home=home).data
        assert state["generation"] == json.loads(before)["generation"]
        assert state["conflicts"] == {} and len(state["objects"]) == 1
    else:
        assert trust.initialize_trust(brigade_home=home).data["generation"] == 1


@pytest.mark.parametrize("secondary", ["io", "cancel"])
def test_buffered_snapshot_close_only_failure_prevents_publish(trust, home, buffered_snapshot_fault, secondary):
    class Cancelled(BaseException):
        pass

    bootstrap(trust, home)
    accept(trust, home)
    before = saved(home).read_bytes()
    cleanup = OSError(errno.EIO, "stream close only") if secondary == "io" else Cancelled()
    caught = None
    with buffered_snapshot_fault(None, cleanup) as seen:
        try:
            accept(trust, home, seq=2)
        except BaseException as error:
            caught = error
    if secondary == "io":
        assert type(caught) is crypto.MeshError and caught.code == "mesh_trust_storage_failure"
    else:
        assert caught is cleanup
    assert seen["closes"] == seen["stream_closes"] == seen["unlinks"] == 1
    assert seen["streams"][0].closed
    assert saved(home).read_bytes() == before
    assert list(saved(home).parent.glob(".trust-*.tmp")) == []


def test_cleanup_only_unlink_io_lost_return_reconciles_trust_generation(trust, home, monkeypatch):
    bootstrap(trust, home)
    accept(trust, home)
    previous = trust.load_trust(brigade_home=home).data
    original_replace, original_unlink = dirfd.replace_children, dirfd.unlink_child
    seen = {"replaced": False, "unlinks": 0}

    def replace(*args, **kwargs):
        original_replace(*args, **kwargs)
        seen["replaced"] = True

    def unlink(parent, name, **kwargs):
        if seen["replaced"] and name.startswith(".trust-"):
            seen["unlinks"] += 1
            raise OSError(errno.EIO, "post-replace cleanup")
        return original_unlink(parent, name, **kwargs)

    caught = None
    raw = signed(seq=2)
    with monkeypatch.context() as patch:
        patch.setattr(dirfd, "replace_children", replace)
        patch.setattr(dirfd, "unlink_child", unlink)
        try:
            accept(trust, home, raw)
        except BaseException as error:
            caught = error
    assert type(caught) is crypto.MeshError and caught.code == "mesh_trust_storage_failure"
    assert seen == {"replaced": True, "unlinks": 1}
    state = trust.load_trust(brigade_home=home).data
    assert state["generation"] == previous["generation"] + 1
    assert state["marks"]["membership:" + signer()] == {"seq": 2, "digest": digest(raw)}
    complete = saved(home).read_bytes()
    assert accept(trust, home, raw).status == "replay"
    assert saved(home).read_bytes() == complete
    assert list(saved(home).parent.glob(".trust-*.tmp")) == []


@pytest.mark.parametrize("operation", ["accept", "conflict"])
@pytest.mark.parametrize("number", [errno.EIO, errno.EPERM])
def test_postrename_cleanup_maps_inside_trust_writer(trust, home, monkeypatch, operation, number):
    bootstrap(trust, home)
    accept(trust, home)
    before = trust.load_trust(brigade_home=home).data
    raw = signed(seq=2) if operation == "accept" else signed(entries=["test-peer"])
    original_replace, original_unlink, original_flush = (
        dirfd.replace_children,
        dirfd.unlink_child,
        dirfd.fsync_directory,
    )
    seen = {"replaced": False, "unlinks": 0, "flushes": 0}

    def replace(*args, **kwargs):
        original_replace(*args, **kwargs)
        seen["replaced"] = True

    def unlink(parent, name, **kwargs):
        if seen["replaced"] and name.startswith(".trust-"):
            seen["unlinks"] += 1
            raise OSError(number, "post-rename temporary cleanup")
        return original_unlink(parent, name, **kwargs)

    def flush(descriptor):
        if seen["replaced"]:
            seen["flushes"] += 1
        original_flush(descriptor)

    result = caught = None
    with monkeypatch.context() as patch:
        patch.setattr(dirfd, "replace_children", replace)
        patch.setattr(dirfd, "unlink_child", unlink)
        patch.setattr(dirfd, "fsync_directory", flush)
        try:
            result = accept(trust, home, raw)
        except BaseException as error:
            caught = error
    state = trust.load_trust(brigade_home=home).data
    complete = saved(home).read_bytes()
    assert state["generation"] == before["generation"] + 1
    assert seen == {"replaced": True, "unlinks": 1, "flushes": 0}
    if operation == "conflict":
        assert state["objects"] == before["objects"] and state["marks"] == before["marks"]
        assert len(state["conflicts"]) == 1
        assert next(iter(state["conflicts"].values()))["digest"] == digest(raw)
        assert caught is None
        assert result.status == "signed_statement_conflict" and not result.evidence_recorded
        assert result.storage_error == "mesh_trust_storage_failure"
    else:
        assert type(caught) is crypto.MeshError and caught.code == "mesh_trust_storage_failure"
        assert len(state["objects"]) == len(before["objects"]) + 1
        assert state["marks"]["membership:" + signer()] == {"seq": 2, "digest": digest(raw)}
        assert state["conflicts"] == before["conflicts"]
    seen["flushes"] = 0
    with monkeypatch.context() as patch:
        patch.setattr(dirfd, "fsync_directory", flush)
        retry = accept(trust, home, raw)
    assert seen["flushes"] == 1
    if operation == "conflict":
        assert retry.status == "signed_statement_conflict" and retry.evidence_recorded
        assert retry.storage_error is None
    else:
        assert retry.status == "replay"
    assert saved(home).read_bytes() == complete
    assert trust.load_trust(brigade_home=home).data["generation"] == state["generation"]
    assert list(saved(home).parent.glob(".trust-*.tmp")) == []
