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


# Public peer operations guard against substitution, stale evidence and lost returns.
PEER_ORIGIN = "https://peer-hub.example"
PEER_HUB = "h_" + crypto.encode_binary(hashlib.sha256(crypto.LABELS["hub-id"] + key()).digest()[:16])
PEER_PRINCIPAL = "test-peer"


def peer_identity(*, origin=PEER_ORIGIN, seq=1):
    value = {
        "v": 1,
        "hub_id": PEER_HUB,
        "hub_sig_pub": crypto.encode_binary(key()),
        "endpoint": origin,
        "require_sealed": False,
        "max_envelope_bytes": 98304,
        "envelope_versions": [1],
        "suites": ["bm1"],
        "backend": "full",
        "capabilities": ["messages"],
        "created_at": TIME,
        "seq": seq,
    }
    return crypto.canonical(statements.sign_statement(value, SEED, expected_kind="hub-identity"))


def peer_record(number=1, *, previous=None, created_at=None, hub=PEER_HUB, principal=PEER_PRINCIPAL, **extra):
    # Independent public fake KEM bytes suffice for record authentication tests.
    seed = hashlib.sha256(b"public peer fixture" + bytes([number])).digest()
    enc = bytes([number]) * crypto.PUBLIC_KEY_BYTES
    value = {
        "v": 1,
        "hub": hub,
        "principal": principal,
        "kid": crypto.key_id(key(seed), enc),
        "sig_pub": crypto.encode_binary(key(seed)),
        "enc_pub": crypto.encode_binary(enc),
        "suites": ["bm1"],
        "created_at": created_at or f"2026-01-02T00:{number:02}:00Z",
        **extra,
    }
    if previous is not None:
        old = json.loads(previous)
        value["prev_kid"] = old["kid"]
    value["self_sig"] = crypto.sign(value, seed, label="key")
    if previous is not None:
        old_number = crypto.decode_binary(old["enc_pub"], crypto.PUBLIC_KEY_BYTES)[0]
        old_seed = hashlib.sha256(b"public peer fixture" + bytes([old_number])).digest()
        unsigned = {k: v for k, v in value.items() if k != "self_sig"}
        value["prev_sig"] = crypto.sign(unsigned, old_seed, label="key")
    return crypto.canonical(value)


def peer_setup(trust, home, *, members=(), initialize=True):
    identity = importlib.import_module("brigade.fleet_mesh_hub_identity")
    trust.initialize_trust(brigade_home=home)
    identity.initialize_identity_store(brigade_home=home)
    identity.observe_identity(brigade_home=home, origin=PEER_ORIGIN, raw=peer_identity())
    pin(trust, home, hub=PEER_HUB)
    accept(trust, home, hub=PEER_HUB, seq=7, entries=list(members))
    if initialize:
        trust.initialize_peers(brigade_home=home, origin=PEER_ORIGIN)
    return identity


def peer_observe(trust, home, raw, *, chain=(), require_verified=False, **kwargs):
    return trust.observe_peer(
        brigade_home=home,
        origin=PEER_ORIGIN,
        principal=PEER_PRINCIPAL,
        raw=raw,
        chain=chain,
        require_verified=require_verified,
        **kwargs,
    )


def peer_accept(trust, home, raw, *, require_verified, expected=None, fingerprint=None):
    return trust.accept_peer(
        brigade_home=home,
        origin=PEER_ORIGIN,
        principal=PEER_PRINCIPAL,
        raw=raw,
        expected_fingerprint=fingerprint or crypto.fingerprint(crypto.decode_binary(json.loads(raw)["sig_pub"], 32)),
        expected_head_digest=expected,
        require_verified=require_verified,
    )


def peer_load(trust, home, *, require_verified=False):
    return trust.load_peer(
        brigade_home=home,
        origin=PEER_ORIGIN,
        principal=PEER_PRINCIPAL,
        require_verified=require_verified,
    )


def peer_history(trust, home, raw, instant, *, require_verified=False):
    value = json.loads(raw)
    return trust.record_for(
        brigade_home=home,
        origin=PEER_ORIGIN,
        hub=value["hub"],
        principal=value["principal"],
        kid=value["kid"],
        signed_created_at=instant,
        require_verified=require_verified,
    )


def test_peers_explicit_v2_extension_preserves_v1_and_read_only(trust, home, monkeypatch):
    peer_setup(trust, home, initialize=False)
    before = saved(home).read_bytes()
    old = json.loads(before)
    assert old["v"] == 1 and "peers" not in old and "peer_records" not in old
    with pytest.raises(crypto.MeshError, match="^mesh_peers_missing$"):
        peer_load(trust, home)
    assert saved(home).read_bytes() == before
    extended = trust.initialize_peers(brigade_home=home, origin=PEER_ORIGIN)
    assert extended.data == dict(old, v=2, generation=old["generation"] + 1, peers={}, peer_records={})
    complete = saved(home).read_bytes()
    trust.initialize_peers(brigade_home=home, origin=PEER_ORIGIN)
    assert saved(home).read_bytes() == complete

    def forbidden(*args, **kwargs):
        raise AssertionError("read attempted mutation")

    with monkeypatch.context() as patch:
        patch.setattr(dirfd, "fsync_directory", forbidden)
        patch.setattr(dirfd, "unlink_child", forbidden)
        view = peer_load(trust, home)
        assert view.head is None and view.use.error == "recipient_key_missing"
        assert view.policy_freshness == "unproven"
    accept(trust, home, hub=PEER_HUB, seq=8)
    assert trust.load_trust(brigade_home=home).data["peers"] == {}


@pytest.mark.parametrize("mutation", ["v", "missing", "extra", "record", "provenance", "interval"])
def test_peers_v2_validates_complete_schema_and_retained_evidence(trust, home, mutation):
    peer_setup(trust, home, members=[PEER_PRINCIPAL])
    raw = peer_record()
    peer_observe(trust, home, raw)
    state = json.loads(saved(home).read_bytes())
    if mutation == "v":
        state["v"] = 3
    elif mutation == "missing":
        del state["peer_records"]
    elif mutation == "extra":
        state["unknown"] = {}
    elif mutation == "record":
        next(iter(state["peer_records"].values()))["canonical"] = peer_record(2).decode()
    elif mutation == "provenance":
        next(iter(state["peer_records"].values()))["confirmation"] = {"fingerprint": "verified"}
    else:
        next(iter(state["peer_records"].values()))["predecessor"] = "1" * 64
    saved(home).write_bytes(crypto.canonical(state))
    before = saved(home).read_bytes()
    with pytest.raises(crypto.MeshError, match="^mesh_trust_invalid$"):
        peer_load(trust, home)
    assert saved(home).read_bytes() == before


@pytest.mark.parametrize(
    "listed,required,want", [(False, False, "unverified"), (True, False, "observed"), (True, True, "unverified")]
)
def test_peers_observation_confirmation_and_detachment(trust, home, listed, required, want):
    identity = peer_setup(trust, home, members=[PEER_PRINCIPAL] if listed else [])
    identity_path = saved(home).with_name("hub-identity-state.json")
    identity_bytes = identity_path.read_bytes()
    raw = peer_record()
    first = peer_observe(trust, home, raw, require_verified=required)
    assert first.disposition == "accepted" and first.peer.provenance == want
    assert first.peer.use.eligible == (want == "observed")
    first.peer.data["peers"].clear()
    assert peer_load(trust, home).head == raw
    complete = saved(home).read_bytes()
    assert peer_observe(trust, home, raw, require_verified=required).disposition == "replay"
    assert saved(home).read_bytes() == complete
    confirmed = peer_accept(trust, home, raw, expected=digest(raw), require_verified=False)
    assert confirmed.peer.provenance == "verified" and confirmed.peer.use.eligible
    assert peer_load(trust, home, require_verified=True).use.eligible
    complete = saved(home).read_bytes()
    assert peer_accept(trust, home, raw, expected=digest(raw), require_verified=False).disposition == "replay"
    assert saved(home).read_bytes() == complete and identity_path.read_bytes() == identity_bytes
    identity.confirm_identity(brigade_home=home, origin=PEER_ORIGIN, expected_hub_id=PEER_HUB, source="operator")
    assert peer_load(trust, home).identity_provenance == "verified"
    assert peer_load(trust, home).policy_freshness == "unproven"
    assert saved(home).read_bytes() == complete


@pytest.mark.parametrize(
    "bad",
    [
        "signature",
        "kem",
        "kid",
        "principal",
        "hub",
        "mesh",
        "duplicate",
        "deep",
        "large",
        "unicode",
        "timestamp",
        "suite",
        "role",
        "fingerprint",
        "short-fingerprint",
        "cas",
    ],
)
def test_peers_invalid_candidates_never_poison_or_write(trust, home, bad):
    peer_setup(trust, home)
    original = peer_record()
    peer_accept(trust, home, original, require_verified=False)
    value = json.loads(peer_record(2))
    raw = None
    fp = crypto.fingerprint(crypto.decode_binary(value["sig_pub"], 32))
    expected = digest(original)
    if bad == "signature":
        value["self_sig"] = crypto.encode_binary(bytes(64))
    elif bad == "kem":
        value["enc_pub"] = crypto.encode_binary(bytes(crypto.PUBLIC_KEY_BYTES))
    elif bad == "kid":
        value["kid"] = KID
    elif bad in {"principal", "hub", "mesh", "timestamp", "suite", "role"}:
        fields = {
            "principal": {"principal": "other-peer"},
            "hub": {"hub": OTHER},
            "mesh": {"hub": "mesh"},
            "timestamp": {"created_at": "yesterday"},
            "suite": {"suites": ["bad"]},
            "role": {"role": "node"},
        }
        raw = peer_record(2, **fields[bad])
    elif bad == "duplicate":
        raw = b'{"v":1,' + crypto.canonical(value)[1:]
    elif bad == "deep":
        raw = b'{"x":' + b"[" * 5 + b"0" + b"]" * 5 + b"}"
    elif bad == "large":
        raw = b" " * (crypto.MAX_RAW_ENVELOPE_BYTES + 1)
    elif bad == "unicode":
        raw = b'{"x":"\\ud800"}'
    elif bad == "fingerprint":
        fp = crypto.fingerprint(key())
    elif bad == "short-fingerprint":
        fp = "AAAA"
    elif bad == "cas":
        expected = "0" * 64
    raw = raw or crypto.canonical(value)
    before = saved(home).read_bytes()
    with pytest.raises(crypto.MeshError):
        peer_accept(trust, home, raw, expected=expected, fingerprint=fp, require_verified=False)
    assert saved(home).read_bytes() == before
    if bad not in {"fingerprint", "short-fingerprint", "cas"}:
        with pytest.raises(crypto.MeshError):
            peer_observe(trust, home, raw)
        assert saved(home).read_bytes() == before
    assert peer_load(trust, home).use.eligible


def test_peers_membership_current_use_history_continuity_and_changed(trust, home):
    peer_setup(trust, home, members=[PEER_PRINCIPAL])
    first = peer_record()
    second = peer_record(2, previous=first)
    peer_observe(trust, home, first)
    assert peer_load(trust, home, require_verified=True).use.error == "recipient_key_unverified"
    accept(trust, home, hub=PEER_HUB, seq=8)
    assert peer_load(trust, home).use.error == "recipient_key_not_member"
    assert peer_history(trust, home, first, json.loads(first)["created_at"]).error == "sender_key_not_member"
    assert peer_observe(trust, home, second).disposition == "accepted"
    assert peer_load(trust, home).head == second
    accept(trust, home, hub=PEER_HUB, seq=9, entries=[PEER_PRINCIPAL])
    assert peer_load(trust, home).use.eligible
    assert peer_history(trust, home, first, json.loads(first)["created_at"]).eligible
    unrelated = peer_record(3)
    assert peer_observe(trust, home, unrelated).disposition == "changed"
    assert peer_load(trust, home).head == second
    third = peer_record(4, previous=second)
    assert peer_observe(trust, home, third).disposition == "accepted"
    assert peer_load(trust, home).use.error == "recipient_key_changed"
    accept(trust, home, hub=PEER_HUB, seq=10, entries=[PEER_PRINCIPAL])
    peer_observe(trust, home, third)
    assert peer_load(trust, home).use.error == "recipient_key_changed"
    assert peer_accept(trust, home, unrelated, expected=digest(third), require_verified=False).disposition == "refused"
    fresh = peer_record(5)
    result = peer_accept(trust, home, fresh, expected=digest(third), require_verified=False)
    assert result.disposition == "accepted" and result.peer.use.eligible
    assert len(result.peer.history) == 4
    assert result.peer.provenance == "verified"


@pytest.mark.parametrize("links", [0, 1, 8, 9])
def test_peers_chain_bound_and_restart(trust, home, links):
    peer_setup(trust, home)
    first = peer_record()
    peer_accept(trust, home, first, require_verified=False)
    chain = []
    for number in range(2, links + 2):
        chain.append(peer_record(number, previous=chain[-1] if chain else first))
    result = peer_observe(trust, home, chain[-1] if chain else first, chain=tuple(chain))
    assert result.disposition == ("changed" if links == 9 else "accepted" if links else "replay")
    view = peer_load(trust, home)
    assert len(view.history) == (1 if links == 9 else links + 1)
    assert view.use.error == ("recipient_key_changed" if links == 9 else None)


@pytest.mark.parametrize("bad", ["gap", "order", "loop", "instant", "revoked", "principal"])
def test_peers_failed_authenticated_chain_retains_changed(trust, home, bad):
    peer_setup(trust, home)
    first = peer_record()
    peer_accept(trust, home, first, require_verified=False)
    second = peer_record(2, previous=first)
    third = peer_record(3, previous=second)
    chain = (second, third)
    if bad == "gap":
        chain = (third,)
    elif bad == "order":
        chain = (third, second, third)
    elif bad == "loop":
        chain = (second, first, third)
    elif bad == "instant":
        second = peer_record(2, previous=first, created_at="2026-01-02T01:01:00+01:00")
        third = peer_record(3, previous=second)
        chain = (second, third)
    elif bad == "revoked":
        trust.record_hub_revocation(
            brigade_home=home, hub=PEER_HUB, principal="other-peer", kid=json.loads(second)["kid"], cutoff=TIME
        )
    else:
        second = peer_record(2, previous=first, principal="other-peer")
        chain = (second, third)
    result = peer_observe(trust, home, third, chain=chain)
    assert result.disposition == "changed" and result.evidence_recorded
    assert peer_load(trust, home).head == first
    assert peer_load(trust, home).use.error == "recipient_key_changed"
    before = saved(home).read_bytes()
    assert peer_observe(trust, home, third, chain=chain).disposition == "changed"
    assert saved(home).read_bytes() == before


def test_peers_manual_cas_history_and_cross_principal_irreversible_barrier(trust, home):
    peer_setup(trust, home)
    first = peer_record()
    second = peer_record(2)
    peer_accept(trust, home, first, require_verified=False)
    trust.record_hub_revocation(
        brigade_home=home, hub=PEER_HUB, principal="other-peer", kid=json.loads(first)["kid"], cutoff=TIME
    )
    assert peer_load(trust, home).use.error == "recipient_key_revoked"
    result = peer_accept(trust, home, first, require_verified=False)
    assert result.disposition == "replay" and result.peer.use.error == "recipient_key_revoked"
    assert peer_history(trust, home, first, "2026-01-02T00:00:00Z").error == "sender_key_revoked"
    peer_accept(trust, home, second, expected=digest(first), require_verified=False)
    assert peer_load(trust, home).use.eligible
    before = saved(home).read_bytes()
    assert peer_accept(trust, home, first, expected=digest(second), require_verified=False).disposition == "refused"
    assert saved(home).read_bytes() == before
    alternate = peer_record(2, created_at="2026-01-02T00:03:00Z")
    assert peer_accept(trust, home, alternate, expected=digest(second), require_verified=False).disposition == "refused"
    assert saved(home).read_bytes() == before


@pytest.mark.parametrize(
    "instant,eligible",
    [
        ("2026-01-02T00:00:59Z", False),
        ("2026-01-02T00:01:00Z", True),
        ("2026-01-02T00:01:59Z", True),
        ("2026-01-02T00:02:00Z", True),
        ("2026-01-02T00:06:59.999999Z", True),
        ("2026-01-02T00:07:00Z", False),
    ],
)
def test_peers_history_interval_only_retirement_skew(trust, home, instant, eligible):
    peer_setup(trust, home)
    first = peer_record()
    peer_accept(trust, home, first, require_verified=False)
    peer_observe(trust, home, peer_record(2, previous=first))
    use = peer_history(trust, home, first, instant)
    assert use.record == first and use.eligible == eligible
    assert use.validity["start"] == "2026-01-02T00:01:00Z"
    assert use.validity["retirement"] == "2026-01-02T00:02:00Z"
    assert use.error == (None if eligible else "mesh_peer_outside_interval")


@pytest.mark.parametrize("prerequisite", ["identity", "origin", "floor", "stale"])
def test_peers_identity_prerequisites_refuse_without_extension(trust, home, prerequisite):
    identity = peer_setup(trust, home, initialize=False)
    origin = PEER_ORIGIN
    if prerequisite == "identity":
        saved(home).with_name("hub-identity-state.json").unlink()
        want = "hub_identity_unverified"
    elif prerequisite == "origin":
        origin = "https://unknown.example"
        want = "hub_identity_unverified"
    elif prerequisite == "floor":
        target = peer_identity(seq=2)
        identity.require_identity_floor(
            brigade_home=home, kind="hub-identity", hub=PEER_HUB, signer=signer(), seq=2, digest=digest(target)
        )
        want = "hub_identity_floor_unsatisfied"
    else:
        identity.observe_identity(
            brigade_home=home, origin="https://other.example", raw=peer_identity(origin="https://other.example", seq=2)
        )
        want = "signed_statement_rollback"
    before = saved(home).read_bytes()
    with pytest.raises(crypto.MeshError, match="^" + want + "$"):
        trust.initialize_peers(brigade_home=home, origin=origin)
    assert saved(home).read_bytes() == before
    if prerequisite == "floor":
        identity.observe_identity(brigade_home=home, origin=PEER_ORIGIN, raw=target)
        assert (
            trust.initialize_peers(brigade_home=home, origin="HTTPS://PEER-HUB.EXAMPLE:443/").policy_freshness
            == "unproven"
        )
        peer_accept(trust, home, peer_record(), require_verified=False)
        identity.require_identity_floor(
            brigade_home=home,
            kind="hub-identity",
            hub=PEER_HUB,
            signer=signer(),
            seq=3,
            digest=digest(peer_identity(seq=3)),
        )
        with pytest.raises(crypto.MeshError, match="^hub_identity_floor_unsatisfied$"):
            peer_load(trust, home)
        with pytest.raises(crypto.MeshError, match="^hub_identity_floor_unsatisfied$"):
            peer_accept(trust, home, peer_record(2), expected=digest(peer_record()), require_verified=False)


def test_peers_current_membership_scoped_floor_and_replaced_anchor(trust, home):
    peer_setup(trust, home, members=[PEER_PRINCIPAL])
    raw = peer_record()
    peer_observe(trust, home, raw)
    pin(trust, home, hub=OTHER)
    accept(trust, home, hub=OTHER, seq=8)
    # A global mark on a different hub is not this hub's accepted membership.
    assert peer_load(trust, home).use.eligible
    floor(trust, home, signed(hub=PEER_HUB, seq=9), hub=PEER_HUB)
    assert peer_load(trust, home).use.error == "recipient_key_unverified"
    pin(trust, home, hub=PEER_HUB, seed=SEED2, previous=signer())
    accept(trust, home, hub=PEER_HUB, seed=SEED2, seq=0, entries=[PEER_PRINCIPAL])
    view = peer_load(trust, home)
    assert view.use.error == "mesh_peer_membership_reconciliation_unsupported"
    assert view.use.floors[0]["signer"] == signer()
    peer_accept(trust, home, raw, expected=digest(raw), require_verified=False)
    assert peer_load(trust, home).use.eligible
    pin(trust, home, hub=PEER_HUB, previous=signer(SEED2))
    accept(trust, home, hub=PEER_HUB, seq=9)
    assert peer_load(trust, home).use.eligible  # Explicit peer confirmation survives removal.


def test_peers_exact_unverified_promotion_and_old_anchor_never_reactivates(trust, home):
    peer_setup(trust, home)
    raw = peer_record()
    peer_observe(trust, home, raw)
    accept(trust, home, hub=PEER_HUB, seq=8, entries=[PEER_PRINCIPAL])
    assert peer_load(trust, home).use.error == "recipient_key_unverified"
    peer_observe(trust, home, raw, require_verified=True)
    assert peer_load(trust, home).provenance == "unverified"
    peer_observe(trust, home, raw)
    assert peer_load(trust, home).provenance == "observed"
    pin(trust, home, hub=PEER_HUB, seed=SEED2, previous=signer())
    assert peer_load(trust, home).use.error == "recipient_key_unverified"
    accept(trust, home, hub=PEER_HUB, seed=SEED2, seq=0, entries=[PEER_PRINCIPAL])
    assert peer_load(trust, home).use.eligible
    peer_observe(trust, home, peer_record(2, previous=raw))
    assert (
        peer_history(trust, home, raw, json.loads(raw)["created_at"], require_verified=True).error
        == "sender_key_unverified"
    )


def test_peers_completed_chain_retry_and_verified_confirmation_keep_original_times(trust, home):
    peer_setup(trust, home)
    first = peer_record()
    peer_accept(trust, home, first, require_verified=False)
    before = saved(home).read_bytes()
    assert peer_accept(trust, home, first, expected=digest(first), require_verified=False).disposition == "replay"
    assert saved(home).read_bytes() == before
    second = peer_record(2, previous=first)
    third = peer_record(3, previous=second)
    chain = (second, third)
    peer_observe(trust, home, third, chain=chain)
    before = saved(home).read_bytes()
    assert peer_observe(trust, home, third, chain=chain).disposition == "replay"
    assert saved(home).read_bytes() == before
    assert peer_observe(trust, home, first).disposition == "refused"
    assert saved(home).read_bytes() == before


def test_peers_invalid_intermediate_self_signature_cannot_poison(trust, home):
    peer_setup(trust, home)
    first = peer_record()
    peer_accept(trust, home, first, require_verified=False)
    second = peer_record(2, previous=first)
    third = peer_record(3, previous=second)
    value = json.loads(second)
    value["self_sig"] = crypto.encode_binary(bytes(64))
    before = saved(home).read_bytes()
    with pytest.raises(crypto.MeshError, match="^mesh_peer_record_invalid$"):
        peer_observe(trust, home, third, chain=(crypto.canonical(value), third))
    assert saved(home).read_bytes() == before
    assert peer_load(trust, home).use.eligible


@pytest.mark.parametrize(
    "mutation", ["missing-required", "bool-required", "recipient-tuple", "sender-tuple", "old-hub"]
)
def test_peers_use_arguments_are_explicit_and_current_hub_only(trust, home, mutation):
    peer_setup(trust, home)
    raw = peer_record()
    peer_accept(trust, home, raw, require_verified=False)
    args = dict(
        brigade_home=home, origin=PEER_ORIGIN, principal=PEER_PRINCIPAL, purpose="recipient", require_verified=False
    )
    if mutation == "missing-required":
        del args["require_verified"]
    elif mutation == "bool-required":
        args["require_verified"] = 0
    elif mutation == "recipient-tuple":
        args["kid"] = json.loads(raw)["kid"]
    elif mutation == "sender-tuple":
        args["purpose"] = "sender"
    else:
        args.update(purpose="sender", hub=OTHER, kid=json.loads(raw)["kid"], signed_created_at=TIME)
    before = saved(home).read_bytes()
    with pytest.raises((crypto.MeshError, TypeError)):
        with trust.peer_for_use(**args):
            pytest.fail("invalid role selected a key")
    assert saved(home).read_bytes() == before


@pytest.mark.parametrize("operation", ["accept", "changed", "chain", "initialize"])
@pytest.mark.parametrize("fault", ["replace", "postrename", "unlink", "fsync", "unlock", "close"])
def test_peers_writer_lost_returns_and_exact_retry(trust, home, monkeypatch, operation, fault):
    import fcntl

    peer_setup(trust, home, initialize=operation != "initialize")
    first = peer_record()
    second = peer_record(2, previous=first if operation == "chain" else None)
    third = peer_record(3, previous=second) if operation == "chain" else None
    if operation in {"changed", "chain"}:
        peer_accept(trust, home, first, require_verified=False)
    raw = second if operation == "changed" else first

    def action():
        if operation == "initialize":
            return trust.initialize_peers(brigade_home=home, origin=PEER_ORIGIN)
        if operation == "changed":
            return peer_observe(trust, home, raw)
        if operation == "chain":
            return peer_observe(trust, home, third, chain=(second, third))
        return peer_accept(trust, home, raw, require_verified=False)

    before = saved(home).read_bytes()
    inode = saved(home).parent.stat().st_ino
    original_replace, original_unlink = dirfd.replace_children, dirfd.unlink_child
    original_fsync, original_flock, original_close = dirfd.fsync_directory, fcntl.flock, os.close
    seen = {"replaced": False, "faults": 0}

    def fail():
        seen["faults"] += 1
        raise OSError(errno.EIO, "public peer lost return")

    def replace(*args, **kwargs):
        if fault == "replace":
            fail()
        original_replace(*args, **kwargs)
        seen["replaced"] = True
        if fault == "postrename":
            fail()

    def unlink(parent, name, **kwargs):
        if fault == "unlink" and seen["replaced"] and name.startswith(".trust-"):
            fail()
        return original_unlink(parent, name, **kwargs)

    def fsync(fd):
        if fault == "fsync" and seen["replaced"]:
            fail()
        return original_fsync(fd)

    def flock(fd, mode):
        original_flock(fd, mode)
        if fault == "unlock" and mode == fcntl.LOCK_UN and seen["replaced"]:
            fail()

    def close(fd):
        hit = fault == "close" and seen["replaced"] and os.fstat(fd).st_ino == inode
        original_close(fd)
        if hit:
            fail()

    caught = result = None
    with monkeypatch.context() as patch:
        patch.setattr(dirfd, "replace_children", replace)
        patch.setattr(dirfd, "unlink_child", unlink)
        patch.setattr(dirfd, "fsync_directory", fsync)
        patch.setattr(fcntl, "flock", flock)
        patch.setattr(os, "close", close)
        try:
            result = action()
        except BaseException as error:
            caught = error
    assert seen["faults"] == 1
    if operation == "changed":
        assert caught is None and result.disposition == "changed"
        assert result.error == "recipient_key_changed" and not result.evidence_recorded
        assert result.storage_error in {"mesh_trust_storage_failure", "mesh_trust_durability_indeterminate"}
    else:
        assert type(caught) is crypto.MeshError
        assert caught.code in {"mesh_trust_storage_failure", "mesh_trust_durability_indeterminate"}
    complete = saved(home).read_bytes()
    assert (complete == before) == (fault == "replace")
    if fault != "replace":
        assert json.loads(complete)["generation"] == json.loads(before)["generation"] + 1
    retry = action()
    if operation != "initialize":
        assert retry.disposition == (
            "changed" if operation == "changed" else "accepted" if fault == "replace" else "replay"
        )
    if fault != "replace":
        assert saved(home).read_bytes() == complete
    if operation == "accept":
        trust.record_hub_revocation(
            brigade_home=home, hub=PEER_HUB, principal="other-peer", kid=json.loads(raw)["kid"], cutoff=TIME
        )
        assert action().peer.use.error == "recipient_key_revoked"


def test_peers_ordinary_capacity_preserves_authenticated_reserve(trust, home, monkeypatch):
    peer_setup(trust, home)
    before = saved(home).read_bytes()
    ordinary = len(crypto.canonical(dict(json.loads(before), conflicts={})))
    with monkeypatch.context() as patch:
        patch.setattr(trust, "_ORDINARY_BYTES", ordinary + 100)
        with pytest.raises(crypto.MeshError, match="^mesh_trust_capacity$"):
            peer_accept(trust, home, peer_record(), require_verified=False)
        assert saved(home).read_bytes() == before
        conflict = signed(hub=PEER_HUB, seq=7, entries=[PEER_PRINCIPAL])
        assert accept(trust, home, conflict, hub=PEER_HUB).status == "signed_statement_conflict"
        complete = saved(home).read_bytes()
        assert len(complete) > ordinary + 100
        with pytest.raises(crypto.MeshError, match="^mesh_trust_capacity$"):
            peer_accept(trust, home, peer_record(), require_verified=False)
        assert saved(home).read_bytes() == complete
    assert peer_load(trust, home).head is None


def test_peers_read_never_adopts_temp_and_existing_mutators_preserve_history(trust, home):
    identity = peer_setup(trust, home)
    raw = peer_record()
    peer_accept(trust, home, raw, require_verified=False)
    before = peer_load(trust, home).data
    leftover = saved(home).with_name(".trust-" + "f" * 32 + ".tmp")
    leftover.write_bytes(b"uncommitted")
    leftover.chmod(0o600)
    peer_load(trust, home)
    peer_history(trust, home, raw, json.loads(raw)["created_at"])
    assert leftover.read_bytes() == b"uncommitted"
    accept(trust, home, hub=PEER_HUB, seq=8)
    floor(trust, home, signed(hub=PEER_HUB, seq=9), hub=PEER_HUB)
    trust.record_hub_revocation(brigade_home=home, hub=PEER_HUB, principal="other-peer", kid=KID, cutoff=TIME)
    pin(trust, home, hub=PEER_HUB, seed=SEED2, previous=signer())
    identity.observe_identity(brigade_home=home, origin=PEER_ORIGIN, raw=peer_identity(seq=2))
    after = peer_load(trust, home).data
    assert after["peers"] == before["peers"] and after["peer_records"] == before["peer_records"]


@pytest.mark.parametrize("operation", ["accept", "changed"])
@pytest.mark.parametrize("primary", ["write", "cancel", "none"])
def test_peers_buffered_failures_preserve_primary_and_complete_generation(
    trust, home, buffered_snapshot_fault, operation, primary
):
    class Cancelled(BaseException):
        pass

    peer_setup(trust, home)
    first, candidate = peer_record(), peer_record(2)
    if operation == "changed":
        peer_accept(trust, home, first, require_verified=False)
    before = saved(home).read_bytes()
    failure = (
        OSError(errno.ENOSPC, "primary write") if primary == "write" else Cancelled() if primary == "cancel" else None
    )
    caught = result = None
    with buffered_snapshot_fault(failure, OSError(errno.EIO, "secondary close reflush"), extra_cleanup=True) as seen:
        try:
            result = (
                peer_observe(trust, home, candidate)
                if operation == "changed"
                else peer_accept(trust, home, first, require_verified=False)
            )
        except BaseException as error:
            caught = error
    if primary == "cancel":
        assert caught is failure
    elif operation == "changed":
        assert caught is None and result.disposition == "changed"
        assert not result.evidence_recorded and result.storage_error == "mesh_trust_storage_failure"
    else:
        assert type(caught) is crypto.MeshError and caught.code == "mesh_trust_storage_failure"
    assert seen["closes"] == seen["stream_closes"] == seen["unlinks"] == 1
    assert seen["replacement_live"]
    assert saved(home).read_bytes() == before
    assert trust.load_trust(brigade_home=home).data["v"] == 2


@pytest.mark.parametrize("writer", ["revocation", "membership", "identity"])
def test_peers_locked_use_blocks_process_writer_and_next_scope_rechecks(trust, home, writer):
    peer_setup(trust, home, members=[PEER_PRINCIPAL])
    first = peer_record()
    peer_observe(trust, home, first)
    marker = home / "writer-started"
    script = """
import json, sys
from pathlib import Path
from brigade import fleet_mesh_trust as trust, fleet_mesh_hub_identity as identity
home, marker, writer, hub, kid, membership, raw_identity = sys.argv[1:]
Path(marker).write_text("started")
if writer == "revocation":
    trust.record_hub_revocation(brigade_home=Path(home), hub=hub, principal="other-peer", kid=kid, cutoff="2026-01-02T00:00:00Z")
elif writer == "membership":
    trust.import_statement(brigade_home=Path(home), hub=hub, kind="membership", raw=membership.encode())
else:
    identity.observe_identity(brigade_home=Path(home), origin="https://peer-hub.example", raw=raw_identity.encode())
print("committed", flush=True)
"""
    before = saved(home).read_bytes()
    with trust.peer_for_use(
        brigade_home=home, origin=PEER_ORIGIN, principal=PEER_PRINCIPAL, purpose="recipient", require_verified=False
    ) as use:
        assert use.eligible and use.record == first
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(home),
                str(marker),
                writer,
                PEER_HUB,
                json.loads(first)["kid"],
                signed(hub=PEER_HUB, seq=8).decode(),
                peer_identity(seq=2).decode(),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            import time

            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert marker.exists()
            with pytest.raises(subprocess.TimeoutExpired):
                process.communicate(timeout=0.2)
            assert saved(home).read_bytes() == before and use.eligible
        except BaseException:
            process.kill()
            process.communicate()
            raise
    output, error = process.communicate(timeout=10)
    assert process.returncode == 0 and output.strip() == "committed", error
    fresh = peer_load(trust, home)
    assert (
        fresh.use.error
        == {"revocation": "recipient_key_revoked", "membership": "recipient_key_not_member", "identity": None}[writer]
    )
    if writer == "identity":
        assert fresh.identity_generation == use.identity_generation + 1
    else:
        assert fresh.trust_generation == use.trust_generation + 1

    # Caller exceptions/cancellation release the very same inode lock.
    class Cancelled(BaseException):
        pass

    with pytest.raises(Cancelled):
        with trust.peer_for_use(
            brigade_home=home, origin=PEER_ORIGIN, principal=PEER_PRINCIPAL, purpose="recipient", require_verified=False
        ):
            raise Cancelled()
    assert peer_load(trust, home).head == first


def test_peers_process_cas_race_has_one_accepted_head(trust, home):
    peer_setup(trust, home)
    script = """
import sys
from pathlib import Path
from brigade import fleet_mesh_trust as trust, fleet_mesh_crypto as crypto
try:
    result = trust.accept_peer(brigade_home=Path(sys.argv[1]), origin="https://peer-hub.example", principal="test-peer", raw=sys.argv[2].encode(), expected_fingerprint=sys.argv[3], expected_head_digest=None, require_verified=False)
    print(result.disposition)
except crypto.MeshError as error:
    print(error.code)
"""
    candidates = (peer_record(), peer_record(2))
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(home),
                raw.decode(),
                crypto.fingerprint(crypto.decode_binary(json.loads(raw)["sig_pub"], 32)),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for raw in candidates
    ]
    outputs = []
    for process in processes:
        output, error = process.communicate(timeout=10)
        assert process.returncode == 0, error
        outputs.append(output.strip())
    assert sorted(outputs) == ["accepted", "mesh_peer_head_cas_mismatch"]
    view = peer_load(trust, home)
    assert view.head == candidates[outputs.index("accepted")] and len(view.history) == 1


@pytest.mark.parametrize("backend", ["extra", "unsupported", "platform"])
def test_peers_optional_backend_and_platform_preserve_diagnostics(trust, home, monkeypatch, backend):
    peer_setup(trust, home)
    raw = peer_record()
    peer_accept(trust, home, raw, require_verified=False)
    before = saved(home).read_bytes()

    def fail():
        raise crypto.MeshExtraMissing() if backend == "extra" else crypto.MeshError("mesh_backend_unsupported")

    with monkeypatch.context() as patch:
        if backend == "platform":
            patch.setattr(dirfd, "posix_available", lambda: False)
            want = "mesh_trust_platform_unsupported"
        else:
            patch.setattr(crypto, "probe_backend", fail)
            want = "mesh_extra_missing" if backend == "extra" else "mesh_backend_unsupported"
        for action in (
            lambda: peer_load(trust, home),
            lambda: peer_accept(trust, home, raw, require_verified=False),
            lambda: peer_observe(trust, home, raw),
        ):
            with pytest.raises(crypto.MeshError) as caught:
                action()
            assert caught.value.code == want
    assert saved(home).read_bytes() == before


def test_peers_record_count_capacity_does_not_block_evidence_without_growth(trust, home, monkeypatch):
    peer_setup(trust, home)
    raw = peer_record()
    peer_accept(trust, home, raw, require_verified=False)
    for number in range(2, 17):
        raw = peer_record(number, previous=raw)
        peer_observe(trust, home, raw)
    before = saved(home).read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(trust, "_MAX_ENTRIES", 16)
        with pytest.raises(crypto.MeshError, match="^mesh_trust_capacity$"):
            peer_observe(trust, home, peer_record(17, previous=raw))
        assert saved(home).read_bytes() == before
        accept(trust, home, hub=PEER_HUB, seq=8)
        trust.record_hub_revocation(
            brigade_home=home, hub=PEER_HUB, principal="other-peer", kid=json.loads(raw)["kid"], cutoff=TIME
        )
        after = peer_load(trust, home)
        assert len(after.history) == 16 and after.use.error == "recipient_key_revoked"


def test_peers_missing_identity_never_initializes_files(trust, home):
    for action in (
        lambda: trust.initialize_peers(brigade_home=home, origin=PEER_ORIGIN),
        lambda: peer_load(trust, home),
        lambda: peer_accept(trust, home, peer_record(), require_verified=False),
    ):
        with pytest.raises(crypto.MeshError) as caught:
            action()
        assert caught.value.code == "hub_identity_unverified"
        assert not (home / "fleet").exists()


def test_peers_conflicts_are_retained_evidence_without_admitting_rejected_membership(trust, home):
    identity = peer_setup(trust, home, members=[PEER_PRINCIPAL])
    first = peer_record()
    peer_observe(trust, home, first)
    conflict = signed(hub=PEER_HUB, seq=7)
    assert accept(trust, home, conflict, hub=PEER_HUB).status == "signed_statement_conflict"
    value = json.loads(peer_identity())
    value["require_sealed"] = True
    del value["sig"]
    fork = crypto.canonical(statements.sign_statement(value, SEED, expected_kind="hub-identity"))
    assert (
        identity.observe_identity(brigade_home=home, origin=PEER_ORIGIN, raw=fork).status == "signed_statement_conflict"
    )
    view = peer_load(trust, home)
    assert view.use.eligible and view.use.membership["members"] == [PEER_PRINCIPAL]
    assert {e["digest"] for e in view.use.conflicts} == {digest(conflict), digest(fork)}
    assert view.identity_digest == digest(peer_identity())
    assert view.policy_freshness == "unproven"


def test_peers_manual_resolution_preserves_original_observation_confirmation_and_retired_cas(trust, home):
    peer_setup(trust, home, members=[PEER_PRINCIPAL])
    first = peer_record()
    peer_observe(trust, home, first)
    observed_at = peer_load(trust, home).history[0]["accepted_at"]
    peer_observe(trust, home, peer_record(2))
    assert peer_accept(trust, home, first, expected=digest(first), require_verified=False).peer.use.eligible
    confirmed = peer_load(trust, home).history[0]["confirmation"].copy()
    second = peer_record(3, previous=first)
    peer_observe(trust, home, second)
    with pytest.raises(crypto.MeshError, match="^mesh_peer_head_cas_mismatch$"):
        peer_accept(trust, home, first, expected=digest(first), require_verified=False)
    view = peer_load(trust, home)
    assert view.history[0]["accepted_at"] == observed_at and view.history[0]["confirmation"] == confirmed
    assert view.history[0]["observation"]["digest"] == digest(signed(hub=PEER_HUB, seq=7, entries=[PEER_PRINCIPAL]))
    assert view.head == second and view.provenance == "verified"


def test_peers_cross_hub_chain_refuses_without_changed_evidence(trust, home):
    peer_setup(trust, home)
    first = peer_record()
    peer_accept(trust, home, first, require_verified=False)
    foreign = peer_record(2, previous=first, hub=OTHER)
    final = peer_record(3, previous=foreign)
    before = saved(home).read_bytes()
    with pytest.raises(crypto.MeshError, match="^mesh_peer_scope_mismatch$"):
        peer_observe(trust, home, final, chain=(foreign, final))
    assert saved(home).read_bytes() == before


@pytest.mark.parametrize("fault", ["unlock", "close"])
def test_peers_use_primary_cancellation_wins_over_cleanup(
    trust, home, trust_read_cleanup_fault, trust_directory_close_fault, fault
):
    class Cancelled(BaseException):
        pass

    peer_setup(trust, home)
    first = peer_record()
    peer_accept(trust, home, first, require_verified=False)
    primary = Cancelled()
    caught = None
    scope = (
        trust_read_cleanup_fault("unlock", OSError(errno.EIO, "secondary unlock"))
        if fault == "unlock"
        else trust_directory_close_fault(home, "final")
    )
    with scope:
        try:
            with trust.peer_for_use(
                brigade_home=home,
                origin=PEER_ORIGIN,
                principal=PEER_PRINCIPAL,
                purpose="recipient",
                require_verified=True,
            ) as use:
                assert use.eligible
                raise primary
        except BaseException as error:
            caught = error
    assert caught is primary
    assert peer_load(trust, home).use.eligible


def test_peers_bad_predecessor_signature_is_authenticated_changed(trust, home):
    peer_setup(trust, home)
    first = peer_record()
    peer_accept(trust, home, first, require_verified=False)
    candidate = json.loads(peer_record(2, previous=first))
    candidate["prev_sig"] = crypto.encode_binary(bytes(64))
    raw = crypto.canonical(candidate)
    result = peer_observe(trust, home, raw)
    assert result.disposition == "changed" and result.evidence_recorded
    assert result.peer.head == first and result.peer.use.error == "recipient_key_changed"
    assert result.peer.use.changed[0]["canonical"].encode() == raw


def test_peers_effective_barrier_keeps_all_sources_and_earliest_cutoff(trust, home):
    peer_setup(trust, home)
    first = peer_record()
    peer_accept(trust, home, first, require_verified=False)
    kid = json.loads(first)["kid"]
    trust.record_hub_revocation(
        brigade_home=home, hub=PEER_HUB, principal="other-peer", kid=kid, cutoff="2026-01-02T01:00:00Z"
    )
    accept(
        trust,
        home,
        hub=PEER_HUB,
        kind="revocation",
        seq=7,
        entries=[revoked(kid=kid, principal=PEER_PRINCIPAL, cutoff="2026-01-02T00:30:00Z")],
    )
    pin(trust, home, hub=PEER_HUB, seed=SEED2, previous=signer())
    accept(
        trust,
        home,
        hub=PEER_HUB,
        seed=SEED2,
        kind="revocation",
        seq=0,
        entries=[revoked(kid=kid, principal="third-peer", cutoff="2026-01-02T00:10:00Z")],
    )
    use = peer_history(trust, home, first, "2026-01-02T00:01:00Z")
    assert use.error == "sender_key_revoked" and use.record == first
    assert use.barrier["cutoff"] == "2026-01-02T00:10:00Z"
    assert use.barrier["principals"] == ["other-peer", PEER_PRINCIPAL, "third-peer"]
    assert len(use.barrier["sources"]) == 3
    before = saved(home).read_bytes()
    assert peer_accept(trust, home, first, require_verified=False).peer.use.error == "recipient_key_revoked"
    assert saved(home).read_bytes() == before
    second = peer_record(2)
    peer_accept(trust, home, second, expected=digest(first), require_verified=False)
    assert peer_load(trust, home).use.eligible
    assert peer_history(trust, home, first, "2026-01-02T00:09:59.999999Z").error == "sender_key_revoked"
    assert peer_history(trust, home, first, "2026-01-02T00:10:00Z").error == "sender_key_revoked"


def test_peers_operations_never_change_private_custody_or_identity(trust, home):
    from brigade import fleet_mesh_keys as custody

    peer_setup(trust, home, members=[PEER_PRINCIPAL])
    custody.load_local_keys(brigade_home=home, principal="local-node", hub_id=PEER_HUB, create=True)
    directory = saved(home).parent
    protected = {path.name: path.read_bytes() for path in directory.iterdir() if path.name != "trust-state.json"}
    assert len(protected) == 2
    first = peer_record()
    peer_observe(trust, home, first)
    peer_accept(trust, home, first, expected=digest(first), require_verified=False)
    peer_observe(trust, home, peer_record(2, previous=first))
    peer_history(trust, home, first, json.loads(first)["created_at"])
    peer_load(trust, home)
    assert {
        path.name: path.read_bytes() for path in directory.iterdir() if path.name != "trust-state.json"
    } == protected


def test_peers_full_peer_table_preserves_peers_during_evidence_import(trust, home, monkeypatch):
    peer_setup(trust, home)
    for number in range(1, 17):
        principal = f"test-peer-{number}"
        raw = peer_record(number, principal=principal)
        trust.accept_peer(
            brigade_home=home,
            origin=PEER_ORIGIN,
            principal=principal,
            raw=raw,
            expected_fingerprint=crypto.fingerprint(crypto.decode_binary(json.loads(raw)["sig_pub"], 32)),
            expected_head_digest=None,
            require_verified=False,
        )
    before = saved(home).read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(trust, "_MAX_ENTRIES", 16)
        with pytest.raises(crypto.MeshError, match="^mesh_trust_capacity$"):
            peer_accept(trust, home, peer_record(17), require_verified=False)
        assert saved(home).read_bytes() == before
        accept(trust, home, hub=PEER_HUB, seq=8)
        result = accept(trust, home, signed(hub=PEER_HUB, seq=8, entries=[PEER_PRINCIPAL]), hub=PEER_HUB)
        assert result.status == "signed_statement_conflict" and result.evidence_recorded
        after = trust.load_trust(brigade_home=home).data
        assert after["peers"] == json.loads(before)["peers"]
        assert after["peer_records"] == json.loads(before)["peer_records"]


# Consolidated R1 regressions: bounded peer admission and current proof dependencies.
@pytest.mark.parametrize("table", ["peer_records", "changes"])
@pytest.mark.parametrize("bad", [{}, [], 1])
def test_r1_nonstring_canonical_has_fixed_corruption_error(trust, home, table, bad):
    peer_setup(trust, home)
    peer_accept(trust, home, peer_record(), require_verified=False)
    peer_observe(trust, home, peer_record(2))
    state = json.loads(saved(home).read_bytes())
    entries = state["peer_records"] if table == "peer_records" else next(iter(state["peers"].values()))["changes"]
    next(iter(entries.values()))["canonical"] = bad
    saved(home).write_bytes(crypto.canonical(state))
    before = saved(home).read_bytes()
    with pytest.raises(crypto.MeshError, match="^mesh_trust_invalid$"):
        peer_load(trust, home)
    assert saved(home).read_bytes() == before


@pytest.mark.parametrize("bad", ["self-first", "self-middle", "self-eighth", "nonbytes", "too-long"])
def test_r1_ninth_successor_authenticates_all_bounded_input_before_changed(trust, home, monkeypatch, bad):
    peer_setup(trust, home)
    first = peer_record()
    peer_accept(trust, home, first, require_verified=False)
    chain = []
    for number in range(2, 12):
        chain.append(peer_record(number, previous=chain[-1] if chain else first))
    candidate = chain[8]
    supplied = chain[:9]
    if bad.startswith("self"):
        index = {"self-first": 0, "self-middle": 4, "self-eighth": 7}[bad]
        value = json.loads(supplied[index])
        value["self_sig"] = crypto.encode_binary(bytes(64))
        supplied[index] = crypto.canonical(value)
    elif bad == "nonbytes":
        supplied[4] = object()
    else:
        supplied, candidate = chain, chain[-1]
    before = saved(home).read_bytes()
    with monkeypatch.context() as patch:
        if bad == "too-long":

            def forbidden(*args, **kwargs):
                raise AssertionError("oversized tuple reached crypto")

            patch.setattr(crypto, "verify_key_record", forbidden)
        with pytest.raises(crypto.MeshError):
            peer_observe(trust, home, candidate, chain=tuple(supplied))
    assert saved(home).read_bytes() == before and peer_load(trust, home).use.eligible


@pytest.mark.parametrize("successors", [1, 8])
def test_r1_chain_is_successors_only_and_leading_head_never_poisons(trust, home, successors):
    peer_setup(trust, home)
    first = peer_record()
    peer_accept(trust, home, first, require_verified=False)
    links = [first]
    for number in range(2, successors + 2):
        links.append(peer_record(number, previous=links[-1]))
    before = saved(home).read_bytes()
    with pytest.raises(crypto.MeshError, match="^mesh_peer_chain_invalid$"):
        peer_observe(trust, home, links[-1], chain=tuple(links))
    assert saved(home).read_bytes() == before and peer_load(trust, home).use.eligible


@pytest.mark.parametrize("primary_kind", ["io", "missing", "cancel"])
@pytest.mark.parametrize("secondary_kind", ["io", "cancel"])
def test_r1_use_preserves_same_body_error_over_secondary_cleanup(
    trust, home, trust_read_cleanup_fault, primary_kind, secondary_kind
):
    class Cancelled(BaseException):
        pass

    peer_setup(trust, home)
    peer_accept(trust, home, peer_record(), require_verified=False)
    primary = {
        "io": OSError(errno.EIO, "caller crypto I/O"),
        "missing": FileNotFoundError("caller input"),
        "cancel": Cancelled("caller cancellation"),
    }[primary_kind]
    secondary = (
        OSError(errno.EIO, "secondary release") if secondary_kind == "io" else Cancelled("secondary cancellation")
    )
    caught = None
    with trust_read_cleanup_fault("unlock", secondary):
        try:
            with trust.peer_for_use(
                brigade_home=home,
                origin=PEER_ORIGIN,
                principal=PEER_PRINCIPAL,
                purpose="recipient",
                require_verified=True,
            ):
                raise primary
        except BaseException as error:
            caught = error
    assert caught is primary
    assert peer_load(trust, home).use.eligible


@pytest.mark.parametrize("policy", [None, 0, "1"])
def test_r1_accept_requires_explicit_boolean_policy(trust, home, policy):
    peer_setup(trust, home)
    raw = peer_record()
    before = saved(home).read_bytes()
    args = dict(
        brigade_home=home,
        origin=PEER_ORIGIN,
        principal=PEER_PRINCIPAL,
        raw=raw,
        expected_fingerprint=crypto.fingerprint(crypto.decode_binary(json.loads(raw)["sig_pub"], 32)),
        expected_head_digest=None,
    )
    with pytest.raises(crypto.MeshError, match="^mesh_peer_arguments_invalid$"):
        trust.accept_peer(**args, require_verified=policy)
    with pytest.raises(TypeError):
        trust.accept_peer(**args)
    assert saved(home).read_bytes() == before


def test_r1_accept_refusal_and_replay_inspection_use_explicit_policy(trust, home):
    peer_setup(trust, home, members=[PEER_PRINCIPAL])
    raw = peer_record()
    peer_observe(trust, home, raw)
    alternate = peer_record(created_at="2026-01-02T00:02:00Z")
    args = dict(
        brigade_home=home,
        origin=PEER_ORIGIN,
        principal=PEER_PRINCIPAL,
        expected_fingerprint=crypto.fingerprint(crypto.decode_binary(json.loads(raw)["sig_pub"], 32)),
        expected_head_digest=digest(raw),
        require_verified=True,
    )
    before = saved(home).read_bytes()
    refusal = trust.accept_peer(raw=alternate, **args)
    assert refusal.disposition == "refused" and refusal.peer.use.error == "recipient_key_unverified"
    assert saved(home).read_bytes() == before
    assert trust.accept_peer(raw=raw, **args).peer.use.eligible
    complete = saved(home).read_bytes()
    replay = trust.accept_peer(raw=raw, **args)
    assert replay.disposition == "replay" and replay.peer.use.eligible
    assert saved(home).read_bytes() == complete


def test_r1_floor_facts_are_current_hub_only_and_source_tagged(trust, home):
    identity = peer_setup(trust, home, members=[PEER_PRINCIPAL])
    peer_observe(trust, home, peer_record())
    floor(trust, home, signed(hub=PEER_HUB, seq=7, entries=[PEER_PRINCIPAL]), hub=PEER_HUB)
    identity.require_identity_floor(
        brigade_home=home, kind="hub-identity", hub=PEER_HUB, signer=signer(), seq=1, digest=digest(peer_identity())
    )
    identity.require_identity_floor(
        brigade_home=home, kind="hub-identity", hub=OTHER, signer=signer(SEED2), seq=9, digest="f" * 64
    )
    facts = peer_load(trust, home).use.floors
    assert len(facts) == 2 and {f["source"] for f in facts} == {"trust", "identity"}
    assert all(f["hub"] == PEER_HUB for f in facts)
    assert all("source" not in f for f in trust.load_trust(brigade_home=home).data["floors"].values())


def r1_revoke(trust, home, raw, *, signed_claim=False, cutoff=None):
    value = json.loads(raw)
    cutoff = cutoff or value["created_at"]
    if signed_claim:
        return accept(
            trust,
            home,
            hub=PEER_HUB,
            kind="revocation",
            seq=1,
            entries=[revoked(principal="other-peer", kid=value["kid"], cutoff=cutoff)],
        )
    return trust.record_hub_revocation(
        brigade_home=home, hub=PEER_HUB, principal="other-peer", kid=value["kid"], cutoff=cutoff
    )


@pytest.mark.parametrize("provenance", ["verified", "observed"])
@pytest.mark.parametrize("cutoff", [None, "2026-01-02T01:30:00+01:00"])
@pytest.mark.parametrize("signed_claim", [False, True])
def test_r1_revoked_ancestor_refuses_current_and_historical_use_without_rewriting(
    trust, home, provenance, cutoff, signed_claim
):
    peer_setup(trust, home, members=[PEER_PRINCIPAL])
    first = peer_record()
    second = peer_record(2, previous=first, created_at="2026-01-02T01:02:00+01:00")
    if provenance == "verified":
        peer_accept(trust, home, first, require_verified=False)
    else:
        peer_observe(trust, home, first)
    peer_observe(trust, home, second)
    r1_revoke(trust, home, first, signed_claim=signed_claim, cutoff=cutoff)
    before = saved(home).read_bytes()
    view = peer_load(trust, home)
    assert not view.use.eligible and view.use.error == "recipient_key_changed"
    assert view.use.key_status == "changed" and view.provenance == provenance
    assert view.head == second and view.use.barrier is None and view.use.changed == ()
    refusal = view.use.continuity_refusal
    assert refusal.reason == "continuity_ancestor_revoked"
    assert refusal.ancestor_digest == digest(first)
    assert refusal.ancestor_ref == next(
        name for name, entry in view.data["peer_records"].items() if entry["digest"] == digest(first)
    )
    assert refusal.barrier["kid"] == json.loads(first)["kid"] and refusal.barrier["principals"] == ["other-peer"]
    refusal.barrier["sources"].clear()
    with trust.peer_for_use(
        brigade_home=home, origin=PEER_ORIGIN, principal=PEER_PRINCIPAL, purpose="recipient", require_verified=True
    ) as use:
        assert use.error == "recipient_key_changed" and use.continuity_refusal.barrier["sources"]
    historical = peer_history(trust, home, second, json.loads(second)["created_at"], require_verified=True)
    assert historical.error == "sender_key_changed" and historical.record == second and historical.barrier is None
    assert saved(home).read_bytes() == before
    accept(trust, home, hub=PEER_HUB, seq=8)
    assert peer_load(trust, home).use.error == "recipient_key_changed"
    accept(trust, home, hub=PEER_HUB, seq=9, entries=[PEER_PRINCIPAL])
    assert peer_load(trust, home).use.error == "recipient_key_changed"


def test_r1_observed_promotion_does_not_reset_continuity_dependency(trust, home):
    peer_setup(trust, home)
    first = peer_record()
    second = peer_record(2, previous=first)
    peer_observe(trust, home, first)
    peer_observe(trust, home, second)
    accept(trust, home, hub=PEER_HUB, seq=8, entries=[PEER_PRINCIPAL])
    peer_observe(trust, home, second)
    assert peer_load(trust, home).provenance == "observed"
    r1_revoke(trust, home, first)
    for required in (False, True):
        view = peer_load(trust, home, require_verified=required)
        assert view.use.error == "recipient_key_changed" and view.provenance == "observed"
        assert view.use.continuity_refusal.ancestor_digest == digest(first)


def test_r1_full_accumulated_ancestry_and_independent_boundaries(trust, home):
    peer_setup(trust, home)
    first = peer_record()
    peer_accept(trust, home, first, require_verified=False)
    records = [first]
    for number in range(2, 12):
        records.append(peer_record(number, previous=records[-1]))
    peer_observe(trust, home, records[8], chain=tuple(records[1:9]))
    peer_observe(trust, home, records[-1], chain=tuple(records[9:]))
    r1_revoke(trust, home, first)
    view = peer_load(trust, home)
    assert view.use.error == "recipient_key_changed" and view.use.continuity_refusal.ancestor_digest == digest(first)
    before = saved(home).read_bytes()
    with pytest.raises(crypto.MeshError):
        peer_accept(
            trust,
            home,
            records[-1],
            expected=digest(records[-1]),
            fingerprint=crypto.fingerprint(key()),
            require_verified=False,
        )
    with pytest.raises(crypto.MeshError, match="^mesh_peer_head_cas_mismatch$"):
        peer_accept(trust, home, records[-1], expected="0" * 64, require_verified=False)
    assert saved(home).read_bytes() == before
    recovered = peer_accept(trust, home, records[-1], expected=digest(records[-1]), require_verified=False)
    assert recovered.peer.use.eligible and recovered.peer.use.continuity_refusal is None
    complete = saved(home).read_bytes()
    assert (
        peer_accept(trust, home, records[-1], expected=digest(records[-1]), require_verified=False).disposition
        == "replay"
    )
    assert saved(home).read_bytes() == complete
    assert peer_history(trust, home, records[8], json.loads(records[8])["created_at"]).error == "sender_key_changed"
    next_record = peer_record(12, previous=records[-1])
    peer_observe(trust, home, next_record)
    assert peer_load(trust, home).use.eligible
    r1_revoke(trust, home, records[-1])
    view = peer_load(trust, home)
    assert view.use.error == "recipient_key_changed" and view.use.continuity_refusal.ancestor_digest == digest(
        records[-1]
    )
    peer_accept(trust, home, next_record, expected=digest(next_record), require_verified=False)
    assert peer_load(trust, home).use.eligible
    r1_revoke(trust, home, next_record)
    view = peer_load(trust, home)
    assert view.use.error == "recipient_key_revoked" and view.use.continuity_refusal is None


def test_r1_known_tainted_head_cannot_advance_but_manual_fresh_recovery_works(trust, home):
    peer_setup(trust, home)
    first = peer_record()
    second = peer_record(2, previous=first)
    third = peer_record(3, previous=second)
    peer_accept(trust, home, first, require_verified=False)
    peer_observe(trust, home, second)
    r1_revoke(trust, home, first)
    result = peer_observe(trust, home, third)
    assert result.disposition == "changed" and result.peer.head == second
    assert result.peer.use.continuity_refusal.ancestor_digest == digest(first)
    assert peer_accept(trust, home, third, expected=digest(second), require_verified=False).peer.use.eligible
    assert peer_history(trust, home, second, json.loads(second)["created_at"]).error == "sender_key_changed"


def test_r1_distinct_change_cap_retains_resolved_claims_and_replays(trust, home):
    peer_setup(trust, home)
    first = peer_record()
    peer_accept(trust, home, first, require_verified=False)
    for number in range(2, 10):
        result = peer_observe(trust, home, peer_record(number))
        assert result.disposition == "changed" and result.evidence_recorded
    before = saved(home).read_bytes()
    result = peer_observe(trust, home, peer_record(10))
    assert (
        result.disposition == "changed"
        and not result.evidence_recorded
        and result.storage_error == "mesh_trust_capacity"
    )
    assert result.peer.data == json.loads(before) and saved(home).read_bytes() == before
    assert peer_observe(trust, home, peer_record(2)).evidence_recorded
    assert saved(home).read_bytes() == before
    peer_accept(trust, home, first, expected=digest(first), require_verified=False)
    resolved = saved(home).read_bytes()
    assert len(next(iter(json.loads(resolved)["peers"].values()))["changes"]) == 8
    result = peer_observe(trust, home, peer_record(11))
    assert (
        result.disposition == "changed"
        and not result.evidence_recorded
        and result.storage_error == "mesh_trust_capacity"
    )
    assert saved(home).read_bytes() == resolved and result.peer.use.eligible


def r1_wire_size(value):
    return len(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")
    )


@pytest.mark.parametrize(
    "operation",
    ["initialize", "unverified", "observed", "promotion", "confirm", "replace", "resolution", "chain", "changed"],
)
@pytest.mark.parametrize("budget", ["aggregate", "ordinary", "total"])
def test_r1_every_peer_commit_measures_complete_next_generation(trust, home, monkeypatch, operation, budget):
    with monkeypatch.context() as clock:
        clock.setattr(trust, "_peer_now", lambda: TIME)
        peer_setup(
            trust,
            home,
            members=[PEER_PRINCIPAL] if operation == "observed" else [],
            initialize=operation != "initialize",
        )
        first = peer_record()
        if operation not in {"initialize", "unverified", "observed"}:
            peer_observe(trust, home, first)
        if operation == "promotion":
            accept(trust, home, hub=PEER_HUB, seq=8, entries=[PEER_PRINCIPAL])
        if operation == "resolution":
            peer_observe(trust, home, peer_record(2))
        if budget == "total":
            entries = [] if operation in {"observed", "promotion"} else ["other-peer"]
            seq = 8 if operation == "promotion" else 7
            assert (
                accept(trust, home, signed(hub=PEER_HUB, seq=seq, entries=entries), hub=PEER_HUB).status
                == "signed_statement_conflict"
            )
        state = json.loads(saved(home).read_bytes())
        state["generation"] = 9
        saved(home).write_bytes(crypto.canonical(state))
        before = saved(home).read_bytes()
        chain = []
        for number in range(2, 10):
            chain.append(peer_record(number, previous=chain[-1] if chain else first))

        def action():
            if operation == "initialize":
                return trust.initialize_peers(brigade_home=home, origin=PEER_ORIGIN)
            if operation in {"unverified", "observed", "promotion"}:
                return peer_observe(trust, home, first)
            if operation in {"confirm", "resolution"}:
                return peer_accept(trust, home, first, expected=digest(first), require_verified=False)
            if operation == "replace":
                return peer_accept(trust, home, peer_record(2), expected=digest(first), require_verified=False)
            if operation == "chain":
                return peer_observe(trust, home, chain[-1], chain=tuple(chain))
            return peer_observe(trust, home, peer_record(2))

        action()
        expected = saved(home).read_bytes()
        after = json.loads(expected)
        assert after["generation"] == 10
        peers = {name: after[name] for name in ("peers", "peer_records")}
        measured = {
            "aggregate": r1_wire_size(peers),
            "ordinary": r1_wire_size(dict(after, conflicts={})),
            "total": len(expected),
        }[budget]
        if budget != "aggregate":
            old_generation_size = r1_wire_size(
                dict(after, generation=9, **({"conflicts": {}} if budget == "ordinary" else {}))
            )
            assert measured == old_generation_size + 1
        saved(home).write_bytes(before)
        with monkeypatch.context() as limits:
            limits.setattr(trust, "_PEER_HEADROOM", 512, raising=False)
            attr = {"aggregate": "_PEER_BYTES", "ordinary": "_ORDINARY_BYTES", "total": "_MAX_BYTES"}[budget]
            bound = measured + (0 if budget == "aggregate" else 512)
            limits.setattr(trust, attr, bound - 1, raising=False)
            if operation == "changed":
                failed = action()
                assert (
                    failed.disposition == "changed"
                    and not failed.evidence_recorded
                    and failed.storage_error == "mesh_trust_capacity"
                )
                assert failed.peer.data == json.loads(before)
            else:
                with pytest.raises(crypto.MeshError, match="^mesh_trust_capacity$"):
                    action()
            assert saved(home).read_bytes() == before
            limits.setattr(trust, attr, bound)
            action()
            assert saved(home).read_bytes() == expected


def r1_growth(trust, home, mode, number, previous):
    if mode == "changes":
        raw = peer_record(number)
        return raw, peer_observe(trust, home, raw)
    if mode == "contacts":
        principal = f"unknown-peer-{number}"
        raw = peer_record(number, principal=principal)
        return raw, trust.observe_peer(
            brigade_home=home, origin=PEER_ORIGIN, principal=principal, raw=raw, require_verified=False
        )
    raw = peer_record(number, previous=previous)
    return raw, peer_observe(trust, home, raw)


@pytest.mark.parametrize("mode", ["changes", "contacts", "rotations"])
@pytest.mark.parametrize("victim_action", ["revocation", "removal"])
@pytest.mark.parametrize("budget", ["ordinary", "total"])
def test_r1_peer_growth_preserves_room_for_independent_clean_victim_evidence(
    trust, home, monkeypatch, mode, victim_action, budget
):
    victim = "clean-peer"
    peer_setup(trust, home, members=sorted([PEER_PRINCIPAL, victim]))
    first = peer_record()
    peer_accept(trust, home, first, require_verified=False)
    victim_raw = peer_record(20, principal=victim)
    if victim_action == "revocation":
        trust.accept_peer(
            brigade_home=home,
            origin=PEER_ORIGIN,
            principal=victim,
            raw=victim_raw,
            expected_fingerprint=crypto.fingerprint(crypto.decode_binary(json.loads(victim_raw)["sig_pub"], 32)),
            expected_head_digest=None,
            require_verified=False,
        )
        statement = signed(
            hub=PEER_HUB,
            kind="revocation",
            seq=1,
            entries=[revoked(principal=victim, kid=json.loads(victim_raw)["kid"], cutoff=TIME)],
        )
    else:
        trust.observe_peer(
            brigade_home=home, origin=PEER_ORIGIN, principal=victim, raw=victim_raw, require_verified=False
        )
        statement = signed(hub=PEER_HUB, seq=8, entries=[PEER_PRINCIPAL])

    def victim_use():
        return trust.load_peer(brigade_home=home, origin=PEER_ORIGIN, principal=victim, require_verified=False).use

    assert victim_use().eligible
    before = saved(home).read_bytes()
    previous = first
    for number in (2, 3):
        previous, _ = r1_growth(trust, home, mode, number, previous)
    two = json.loads(saved(home).read_bytes())
    kind = "revocation" if victim_action == "revocation" else "membership"
    accept(trust, home, statement, hub=PEER_HUB, kind=kind)
    with_statement = json.loads(saved(home).read_bytes())
    measure = (lambda state: r1_wire_size(dict(state, conflicts={}))) if budget == "ordinary" else r1_wire_size
    delta = measure(with_statement) - measure(two)
    assert delta > 0
    hard_limit = measure(two) + delta // 2
    saved(home).write_bytes(before)
    with monkeypatch.context() as limits:
        limits.setattr(trust, "_PEER_HEADROOM", delta + 1000, raising=False)
        limits.setattr(trust, "_ORDINARY_BYTES" if budget == "ordinary" else "_MAX_BYTES", hard_limit)
        previous = first
        admitted = 0
        for number in range(2, 20):
            old = saved(home).read_bytes()
            try:
                next_record, result = r1_growth(trust, home, mode, number, previous)
            except crypto.MeshError as error:
                assert error.code == "mesh_trust_capacity" and mode != "changes"
                assert saved(home).read_bytes() == old
                break
            if not result.evidence_recorded:
                assert (
                    mode == "changes"
                    and result.disposition == "changed"
                    and result.storage_error == "mesh_trust_capacity"
                )
                assert saved(home).read_bytes() == old
                break
            admitted += 1
            previous = next_record
        else:
            pytest.fail("peer admission did not reach its bounded ceiling")
        assert admitted > 0
        assert victim_use().eligible
        saturated = json.loads(saved(home).read_bytes())
        result = accept(trust, home, statement, hub=PEER_HUB, kind=kind)
        assert result.status == "accepted"
        assert victim_use().error == (
            "recipient_key_revoked" if victim_action == "revocation" else "recipient_key_not_member"
        )
        after = trust.load_trust(brigade_home=home).data
        assert after["peers"] == saturated["peers"] and after["peer_records"] == saturated["peer_records"]


@pytest.mark.parametrize("mode", ["contacts", "rotations"])
def test_r1_aggregate_limit_covers_all_principals_and_unverified_rotation_growth(trust, home, monkeypatch, mode):
    peer_setup(trust, home)
    first = peer_record()
    peer_observe(trust, home, first)
    assert peer_load(trust, home).provenance == "unverified"
    before = saved(home).read_bytes()
    previous = first
    for number in (2, 3):
        previous, _ = r1_growth(trust, home, mode, number, previous)
    reference = json.loads(saved(home).read_bytes())
    peer_bytes = r1_wire_size({name: reference[name] for name in ("peers", "peer_records")})
    saved(home).write_bytes(before)
    with monkeypatch.context() as limits:
        limits.setattr(trust, "_PEER_BYTES", peer_bytes - 1, raising=False)
        previous, result = r1_growth(trust, home, mode, 2, first)
        assert result.disposition == "accepted"
        old = saved(home).read_bytes()
        with pytest.raises(crypto.MeshError, match="^mesh_trust_capacity$"):
            r1_growth(trust, home, mode, 3, previous)
        assert saved(home).read_bytes() == old
        assert all(len(peer["changes"]) == 0 for peer in json.loads(old)["peers"].values())


def test_r1_legacy_imports_can_consume_headroom_and_exact_peer_retry_still_flushes(trust, home, monkeypatch):
    peer_setup(trust, home)
    first = peer_record()
    peer_accept(trust, home, first, require_verified=False)
    state = json.loads(saved(home).read_bytes())
    ordinary = r1_wire_size(dict(state, conflicts={}))
    with monkeypatch.context() as limits:
        limits.setattr(trust, "_PEER_HEADROOM", 2000, raising=False)
        limits.setattr(trust, "_ORDINARY_BYTES", ordinary + 2000)
        for seq in (8, 9):
            assert accept(trust, home, hub=PEER_HUB, seq=seq).status == "accepted"
            assert peer_load(trust, home).head == first
        before = saved(home).read_bytes()
        assert len(crypto.canonical(dict(json.loads(before), conflicts={}))) > ordinary
        with pytest.raises(crypto.MeshError, match="^mesh_trust_capacity$"):
            peer_observe(trust, home, peer_record(2, previous=first))
        assert saved(home).read_bytes() == before
        assert peer_accept(trust, home, first, require_verified=False).disposition == "replay"
        assert saved(home).read_bytes() == before


def test_r1_prefixed_valid_over_soft_limit_snapshot_is_not_corruption_or_legacy_import_blocker(
    trust, home, monkeypatch
):
    peer_setup(trust, home)
    first = peer_record()
    peer_accept(trust, home, first, require_verified=False)
    state = json.loads(saved(home).read_bytes())
    peer = next(iter(state["peers"].values()))
    for number in range(2, 11):
        raw = peer_record(number)
        name = hashlib.sha256(crypto.canonical([digest(first), digest(raw)])).hexdigest()
        peer["changes"][name] = {
            "canonical": raw.decode("ascii"),
            "digest": digest(raw),
            "head": digest(first),
            "resolved_by": "",
        }
    saved(home).write_bytes(crypto.canonical(state))
    before = saved(home).read_bytes()
    with monkeypatch.context() as limits:
        limits.setattr(
            trust,
            "_PEER_BYTES",
            r1_wire_size({name: state[name] for name in ("peers", "peer_records")}) - 1,
            raising=False,
        )
        assert peer_load(trust, home).use.error == "recipient_key_changed"
        assert peer_observe(trust, home, peer_record(2)).evidence_recorded
        assert saved(home).read_bytes() == before
        assert peer_accept(trust, home, first, require_verified=False).disposition == "replay"
        assert saved(home).read_bytes() == before
        failed = peer_observe(trust, home, peer_record(11))
        assert (
            failed.disposition == "changed"
            and not failed.evidence_recorded
            and failed.storage_error == "mesh_trust_capacity"
        )
        assert saved(home).read_bytes() == before
        assert accept(trust, home, hub=PEER_HUB, seq=8).status == "accepted"
        assert len(next(iter(peer_load(trust, home).data["peers"].values()))["changes"]) == 9


@pytest.mark.parametrize("secondary_kind", ["io", "cancel"])
def test_r1_clean_use_body_still_surfaces_cleanup_failure(trust, home, trust_read_cleanup_fault, secondary_kind):
    class Cancelled(BaseException):
        pass

    peer_setup(trust, home)
    peer_accept(trust, home, peer_record(), require_verified=False)
    secondary = OSError(errno.EIO, "release only") if secondary_kind == "io" else Cancelled()
    caught = None
    with trust_read_cleanup_fault("unlock", secondary):
        try:
            with trust.peer_for_use(
                brigade_home=home,
                origin=PEER_ORIGIN,
                principal=PEER_PRINCIPAL,
                purpose="recipient",
                require_verified=True,
            ) as use:
                assert use.eligible
        except BaseException as error:
            caught = error
    if secondary_kind == "io":
        assert type(caught) is crypto.MeshError and caught.code == "mesh_trust_storage_failure"
    else:
        assert caught is secondary
    assert peer_load(trust, home).use.eligible


def test_r1_confirmation_before_ancestor_revocation_preserves_directional_boundary(trust, home):
    peer_setup(trust, home)
    first = peer_record()
    second = peer_record(2, previous=first)
    third = peer_record(3, previous=second)
    peer_accept(trust, home, first, require_verified=True)
    peer_observe(trust, home, second)
    original = peer_load(trust, home).history[-1].copy()
    peer_accept(trust, home, second, expected=digest(second), require_verified=True)
    peer_observe(trust, home, third)
    r1_revoke(trust, home, first)
    view = peer_load(trust, home, require_verified=True)
    assert view.use.eligible and view.use.continuity_refusal is None
    assert peer_history(trust, home, second, json.loads(second)["created_at"], require_verified=True).eligible
    for field in ("canonical", "accepted_at", "method", "predecessor", "observation"):
        assert view.history[1][field] == original[field]
    assert len(view.data["barriers"]) == 1
    r1_revoke(trust, home, second)
    use = peer_load(trust, home).use
    assert use.error == "recipient_key_changed" and use.continuity_refusal.ancestor_digest == digest(second)
    peer_accept(trust, home, third, expected=digest(third), require_verified=True)
    assert peer_load(trust, home).use.eligible
    assert peer_history(trust, home, second, json.loads(second)["created_at"]).error == "sender_key_revoked"


def test_r1_explicit_anchor_reactivation_resumes_old_membership_marks_and_floors(trust, home):
    peer_setup(trust, home, members=[PEER_PRINCIPAL])
    peer_observe(trust, home, peer_record())
    floor(trust, home, signed(hub=PEER_HUB, seq=7, entries=[PEER_PRINCIPAL]), hub=PEER_HUB)
    pin(trust, home, hub=PEER_HUB, seed=SEED2, previous=signer())
    assert peer_load(trust, home).use.error == "recipient_key_unverified"
    accept(trust, home, hub=PEER_HUB, seed=SEED2, seq=0)
    assert peer_load(trust, home).use.error == "recipient_key_not_member"
    pin(trust, home, hub=PEER_HUB, previous=signer(SEED2))
    assert peer_load(trust, home).use.eligible
    before = saved(home).read_bytes()
    with pytest.raises(crypto.MeshError, match="^signed_statement_rollback$"):
        accept(trust, home, hub=PEER_HUB, seq=6)
    assert saved(home).read_bytes() == before
    floor(trust, home, signed(hub=PEER_HUB, seq=8), hub=PEER_HUB)
    assert peer_load(trust, home).use.error == "recipient_key_unverified"
    assert accept(trust, home, hub=PEER_HUB, seq=8, entries=[PEER_PRINCIPAL]).status == "signed_statement_conflict"
    assert peer_load(trust, home).use.error == "recipient_key_unverified"
    accept(trust, home, hub=PEER_HUB, seq=9, entries=[PEER_PRINCIPAL])
    view = peer_load(trust, home)
    assert view.use.eligible and view.policy_freshness == "unproven"
    assert view.data["hubs"][PEER_HUB] == [signer(), signer(SEED2), signer()]
    assert len(view.data["conflicts"]) == 1 and len(view.data["floors"]) == 1


def test_r1_body_io_error_survives_final_descriptor_close_failure(trust, home, trust_directory_close_fault):
    peer_setup(trust, home)
    peer_accept(trust, home, peer_record(), require_verified=True)
    primary = OSError(errno.EIO, "local crypto input")
    caught = None
    with trust_directory_close_fault(home, "final") as seen:
        try:
            with trust.peer_for_use(
                brigade_home=home,
                origin=PEER_ORIGIN,
                principal=PEER_PRINCIPAL,
                purpose="recipient",
                require_verified=True,
            ):
                raise primary
        except BaseException as error:
            caught = error
        assert seen["calls"] == 1
    assert caught is primary
    assert peer_load(trust, home).use.eligible


# R2 admission protection is deliberately separate from the metadata clock.
@pytest.fixture
def r2_admission_clock(trust, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(trust, "_peer_admission_clock", lambda: trust._instant(TIME), raising=False)
        yield patch


@pytest.mark.parametrize("listed,required", [(True, False), (False, False), (True, True)])
def test_r2_far_future_first_contact_refuses_without_any_pin(trust, home, r2_admission_clock, listed, required):
    peer_setup(trust, home, members=[PEER_PRINCIPAL] if listed else [])
    raw = peer_record(created_at="9999-12-31T23:59:59Z")
    before = saved(home).read_bytes()
    with pytest.raises(crypto.MeshError, match="^mesh_peer_created_in_future$"):
        peer_observe(trust, home, raw, require_verified=required)
    assert saved(home).read_bytes() == before
    assert peer_load(trust, home).head is None


@pytest.mark.parametrize(
    "created_at,allowed",
    [
        ("2026-01-02T00:05:00Z", True),
        ("2026-01-02T01:05:00+01:00", True),
        ("2026-01-02T00:05:00.000001Z", False),
        ("2026-01-01T19:05:00.000001-05:00", False),
    ],
)
@pytest.mark.parametrize("operation", ["observe", "manual", "changed"])
def test_r2_future_admission_inclusive_boundary_and_utc_offsets(
    trust, home, r2_admission_clock, created_at, allowed, operation
):
    peer_setup(trust, home, members=[PEER_PRINCIPAL])
    if operation == "changed":
        peer_accept(trust, home, peer_record(), require_verified=True)
        assert peer_load(trust, home, require_verified=True).use.eligible
    raw = peer_record(2, created_at=created_at)
    before = saved(home).read_bytes()

    def action():
        if operation == "manual":
            return peer_accept(trust, home, raw, require_verified=True)
        return peer_observe(trust, home, raw)

    if allowed:
        result = action()
        assert result.disposition == ("changed" if operation == "changed" else "accepted")
        assert result.evidence_recorded
    else:
        with pytest.raises(crypto.MeshError, match="^mesh_peer_created_in_future$"):
            action()
        assert saved(home).read_bytes() == before
        if operation == "changed":
            assert peer_load(trust, home).use.eligible


@pytest.mark.parametrize("position", [0, 1, 2])
def test_r2_future_continuity_input_never_advances_or_poisons(trust, home, r2_admission_clock, position):
    peer_setup(trust, home)
    first = peer_record()
    peer_accept(trust, home, first, require_verified=True)
    chain = []
    for index, number in enumerate((2, 3, 4)):
        chain.append(
            peer_record(
                number,
                previous=chain[-1] if chain else first,
                created_at="9999-12-31T23:59:59Z" if index == position else None,
            )
        )
    before = saved(home).read_bytes()
    assert peer_load(trust, home, require_verified=True).use.eligible
    with pytest.raises(crypto.MeshError, match="^mesh_peer_created_in_future$"):
        peer_observe(trust, home, chain[-1], chain=tuple(chain))
    assert saved(home).read_bytes() == before and peer_load(trust, home).use.eligible


def test_r2_batch_captures_one_under_lock_cutoff_and_accepts_equal_boundary(
    trust, home, r2_admission_clock, monkeypatch
):
    import fcntl

    peer_setup(trust, home)
    first = peer_record()
    peer_accept(trust, home, first, require_verified=True)
    second = peer_record(2, previous=first)
    third = peer_record(3, previous=second, created_at="2026-01-02T00:05:00Z")
    original_flock = fcntl.flock
    held = False
    calls = 0

    def flock(fd, operation):
        nonlocal held
        result = original_flock(fd, operation)
        held = operation != fcntl.LOCK_UN
        return result

    def clock():
        nonlocal calls
        assert held
        calls += 1
        return trust._instant(TIME)

    with monkeypatch.context() as patch:
        patch.setattr(fcntl, "flock", flock)
        patch.setattr(trust, "_peer_admission_clock", clock, raising=False)
        result = peer_observe(trust, home, third, chain=(second, third))
    assert calls == 1 and result.disposition == "accepted" and result.peer.head == third


@pytest.mark.parametrize("manual_kind", ["first", "replacement", "confirmation"])
def test_r2_every_new_manual_target_or_confirmation_is_time_admitted(trust, home, r2_admission_clock, manual_kind):
    peer_setup(trust, home, members=[PEER_PRINCIPAL])
    expected = None
    raw = peer_record(2, created_at="9999-12-31T23:59:59Z")
    if manual_kind == "replacement":
        first = peer_record()
        peer_accept(trust, home, first, require_verified=True)
        expected = digest(first)
        assert peer_load(trust, home).use.eligible
    elif manual_kind == "confirmation":
        raw = peer_record(created_at="2026-01-02T00:04:00Z")
        peer_observe(trust, home, raw)
        assert peer_load(trust, home).use.eligible
        expected = digest(raw)
        r2_admission_clock.setattr(trust, "_peer_admission_clock", lambda: trust._instant("2026-01-01T23:00:00Z"))
    before = saved(home).read_bytes()
    with pytest.raises(crypto.MeshError, match="^mesh_peer_created_in_future$"):
        peer_accept(trust, home, raw, expected=expected, require_verified=True)
    assert saved(home).read_bytes() == before


def test_r2_near_future_substitute_requires_later_real_rotation_and_virtual_wait(trust, home, r2_admission_clock):
    peer_setup(trust, home, members=[PEER_PRINCIPAL])
    substitute = peer_record(30, created_at="2026-01-02T00:05:00Z")
    result = peer_observe(trust, home, substitute)
    assert result.peer.provenance == "observed" and result.peer.use.eligible
    original = saved(home).read_bytes()
    old_real = peer_record(31, created_at="2026-01-02T00:01:00Z")
    refused = peer_accept(trust, home, old_real, expected=digest(substitute), require_verified=True)
    assert refused.disposition == "refused" and refused.error == "mesh_peer_history_transition_unsupported"
    assert saved(home).read_bytes() == original
    r2_admission_clock.setattr(trust, "_peer_admission_clock", lambda: trust._instant("2026-01-02T00:05:00.000001Z"))
    fresh_real = peer_record(32, created_at="2026-01-02T00:05:00.000001Z")
    accepted = peer_accept(trust, home, fresh_real, expected=digest(substitute), require_verified=True)
    assert accepted.disposition == "accepted" and accepted.peer.use.eligible
    assert accepted.peer.provenance == "verified" and accepted.peer.history[0]["canonical"].encode() == substitute


def test_r2_stolen_key_future_successor_cannot_prevent_clean_revocation_recovery(trust, home, r2_admission_clock):
    peer_setup(trust, home)
    first = peer_record()
    peer_accept(trust, home, first, require_verified=True)
    future = peer_record(2, previous=first, created_at="9999-12-31T23:59:59Z")
    before = saved(home).read_bytes()
    assert peer_load(trust, home).use.eligible
    with pytest.raises(crypto.MeshError, match="^mesh_peer_created_in_future$"):
        peer_observe(trust, home, future)
    assert saved(home).read_bytes() == before and peer_load(trust, home).head == first
    r1_revoke(trust, home, first, signed_claim=True)
    assert peer_load(trust, home).use.error == "recipient_key_revoked"
    real = peer_record(3)
    accepted = peer_accept(trust, home, real, expected=digest(first), require_verified=True)
    assert accepted.disposition == "accepted" and accepted.peer.use.eligible
    assert len(accepted.peer.data["barriers"]) == 1


@pytest.mark.parametrize("completed", ["observation", "manual", "continuity", "changed"])
def test_r2_completed_retries_and_retained_changes_do_not_readmit_after_clock_rollback(
    trust, home, r2_admission_clock, completed
):
    peer_setup(trust, home, members=[PEER_PRINCIPAL])
    first = peer_record()
    if completed == "observation":
        peer_observe(trust, home, first)

        def action():
            return peer_observe(trust, home, first)
    else:
        peer_accept(trust, home, first, require_verified=True)
        if completed == "manual":

            def action():
                return peer_accept(trust, home, first, require_verified=True)
        elif completed == "changed":
            candidate = peer_record(2)
            peer_observe(trust, home, candidate)

            def action():
                return peer_observe(trust, home, candidate)
        else:
            second = peer_record(2, previous=first)
            third = peer_record(3, previous=second)
            peer_observe(trust, home, third, chain=(second, third))

            def action():
                return peer_observe(trust, home, third, chain=(second, third))

    before = saved(home).read_bytes()

    def forbidden():
        raise AssertionError("completed replay consulted admission clock")

    r2_admission_clock.setattr(trust, "_peer_admission_clock", forbidden)
    snapshot = peer_load(trust, home)
    assert snapshot.head is not None
    historical = peer_history(trust, home, first, json.loads(first)["created_at"])
    assert historical.record == first
    result = action()
    assert result.disposition == ("changed" if completed == "changed" else "replay") and result.evidence_recorded
    assert saved(home).read_bytes() == before


def test_r2_seen_changed_candidate_is_readmitted_when_complete_chain_would_advance(trust, home, r2_admission_clock):
    peer_setup(trust, home)
    first = peer_record()
    middle = peer_record(2, previous=first)
    candidate = peer_record(3, previous=middle, created_at="2026-01-02T00:04:00Z")
    peer_accept(trust, home, first, require_verified=True)
    assert peer_load(trust, home).use.eligible
    assert peer_observe(trust, home, candidate).disposition == "changed"
    before = saved(home).read_bytes()
    r2_admission_clock.setattr(trust, "_peer_admission_clock", lambda: trust._instant("2026-01-01T23:00:00Z"))
    with pytest.raises(crypto.MeshError, match="^mesh_peer_created_in_future$"):
        peer_observe(trust, home, candidate, chain=(middle, candidate))
    assert saved(home).read_bytes() == before and peer_load(trust, home).head == first


def test_r2_stored_future_history_remains_clock_independent_and_is_not_repaired(trust, home, r2_admission_clock):
    peer_setup(trust, home, members=[PEER_PRINCIPAL])
    peer_observe(trust, home, peer_record())
    future = peer_record(created_at="9999-12-31T23:59:59Z")
    state = json.loads(saved(home).read_bytes())
    entry = next(iter(state["peer_records"].values()))
    entry.update(canonical=future.decode(), digest=digest(future))
    saved(home).write_bytes(crypto.canonical(state))
    before = saved(home).read_bytes()

    def forbidden():
        raise AssertionError("stored history consulted admission clock")

    r2_admission_clock.setattr(trust, "_peer_admission_clock", forbidden)
    assert peer_load(trust, home).head == future
    assert peer_history(trust, home, future, json.loads(future)["created_at"]).record == future
    assert peer_observe(trust, home, future).disposition == "replay"
    real = peer_record(2)
    result = peer_accept(trust, home, real, expected=digest(future), require_verified=True)
    assert result.disposition == "refused" and result.error == "mesh_peer_history_transition_unsupported"
    assert saved(home).read_bytes() == before


def test_r2_retained_observation_promotion_is_not_new_history_admission(trust, home, r2_admission_clock):
    peer_setup(trust, home)
    raw = peer_record(created_at="2026-01-02T00:04:00Z")
    peer_observe(trust, home, raw)
    original = peer_load(trust, home).history[0]
    accept(trust, home, hub=PEER_HUB, seq=8, entries=[PEER_PRINCIPAL])

    def forbidden():
        raise AssertionError("promotion consulted admission clock")

    r2_admission_clock.setattr(trust, "_peer_admission_clock", forbidden)
    result = peer_observe(trust, home, raw)
    assert result.disposition == "accepted" and result.peer.provenance == "observed"
    assert result.peer.history[0]["canonical"] == original["canonical"]
    assert result.peer.history[0]["accepted_at"] == original["accepted_at"]


@pytest.mark.parametrize("error_kind", ["value", "io", "cancel"])
@pytest.mark.parametrize("chain_kind", ["explicit", "implicit", "suppressed"])
@pytest.mark.parametrize("cleanup", ["none", "unlock-io", "unlock-cancel", "close-io", "close-cancel"])
def test_r2_use_preserves_caller_cause_context_and_suppression(
    trust, home, monkeypatch, error_kind, chain_kind, cleanup
):
    import fcntl

    class Cancelled(BaseException):
        pass

    peer_setup(trust, home)
    peer_accept(trust, home, peer_record(), require_verified=True)
    primary = {
        "value": ValueError("caller"),
        "io": OSError(errno.EIO, "caller I/O"),
        "cancel": Cancelled("caller cancellation"),
    }[error_kind]
    cause = KeyError("explicit caller cause")
    context = KeyError("implicit caller context")
    secondary = (
        OSError(errno.EIO, "secondary cleanup") if cleanup.endswith("io") else Cancelled("secondary cancellation")
    )
    original_close, original_flock = os.close, fcntl.flock
    inode = saved(home).parent.stat().st_ino
    faults = 0

    def flock(fd, mode):
        nonlocal faults
        original_flock(fd, mode)
        if mode == fcntl.LOCK_UN:
            faults += 1
            raise secondary

    def close(fd):
        nonlocal faults
        hit = os.fstat(fd).st_ino == inode
        original_close(fd)
        if hit:
            faults += 1
            raise secondary

    expected = caught = None
    with monkeypatch.context() as patch:
        if cleanup.startswith("unlock"):
            patch.setattr(fcntl, "flock", flock)
        elif cleanup.startswith("close"):
            patch.setattr(os, "close", close)
        try:
            with trust.peer_for_use(
                brigade_home=home,
                origin=PEER_ORIGIN,
                principal=PEER_PRINCIPAL,
                purpose="recipient",
                require_verified=True,
            ):
                try:
                    raise context
                except KeyError:
                    try:
                        if chain_kind == "explicit":
                            raise primary from cause
                        if chain_kind == "suppressed":
                            raise primary from None
                        raise primary
                    except BaseException as error:
                        expected = (error.__cause__, error.__context__, error.__suppress_context__)
                        raise
        except BaseException as error:
            caught = error
    assert caught is primary and expected is not None
    assert caught.__cause__ is expected[0]
    assert caught.__context__ is expected[1]
    assert caught.__suppress_context__ is expected[2]
    assert faults == (0 if cleanup == "none" else 1)
    assert peer_load(trust, home).use.eligible


@pytest.mark.parametrize("future_index", [None, 0, 4, 8])
def test_r2_nine_authenticated_links_check_new_changed_admission_times(trust, home, r2_admission_clock, future_index):
    peer_setup(trust, home)
    first = peer_record()
    peer_accept(trust, home, first, require_verified=True)
    chain = []
    for index, number in enumerate(range(2, 11)):
        instant = "9999-12-31T23:59:59Z" if index == future_index else f"2026-01-02T00:02:{index + 1:02}Z"
        chain.append(peer_record(number, previous=chain[-1] if chain else first, created_at=instant))
    before = saved(home).read_bytes()
    assert peer_load(trust, home).use.eligible
    if future_index is None:
        result = peer_observe(trust, home, chain[-1], chain=tuple(chain))
        assert result.disposition == "changed" and result.evidence_recorded
        assert result.peer.head == first and result.peer.use.error == "recipient_key_changed"
    else:
        with pytest.raises(crypto.MeshError, match="^mesh_peer_created_in_future$"):
            peer_observe(trust, home, chain[-1], chain=tuple(chain))
        assert saved(home).read_bytes() == before and peer_load(trust, home).use.eligible


@pytest.mark.parametrize("future_head", [False, True])
@pytest.mark.parametrize("clock_mode", ["forbidden", "rollback"])
def test_r3_confirmed_head_resolves_changed_without_time_readmission(
    trust, home, r2_admission_clock, future_head, clock_mode
):
    peer_setup(trust, home)
    first = peer_record(created_at="9999-12-31T23:59:59Z") if future_head else peer_record()
    if future_head:
        r2_admission_clock.setattr(trust, "_peer_admission_clock", lambda: trust._instant("9999-12-31T23:59:59Z"))
    assert peer_accept(trust, home, first, require_verified=True).peer.use.eligible
    changed = peer_observe(trust, home, peer_record(2))
    assert changed.disposition == "changed" and changed.evidence_recorded
    original = peer_load(trust, home, require_verified=True)
    before = json.loads(saved(home).read_bytes())
    assert original.use.error == "recipient_key_changed" and original.history[0]["confirmation"]

    def clock():
        if clock_mode == "forbidden":
            raise AssertionError("resolution consulted admission clock")
        return trust._instant(json.loads(first)["created_at"]) - 3_600_000_000

    r2_admission_clock.setattr(trust, "_peer_admission_clock", clock)
    result = peer_accept(trust, home, first, expected=digest(first), require_verified=True)
    assert result.disposition == "accepted" and result.peer.use.eligible
    assert result.peer.head == first and result.peer.provenance == "verified"
    assert result.peer.history == original.history
    complete = saved(home).read_bytes()
    after = json.loads(complete)
    assert after["generation"] == before["generation"] + 1
    assert after["peer_records"] == before["peer_records"]
    old_peer, new_peer = next(iter(before["peers"].values())), next(iter(after["peers"].values()))
    assert new_peer["history"] == old_peer["history"]
    assert new_peer["changes"] == {
        name: dict(change, resolved_by=digest(first)) for name, change in old_peer["changes"].items()
    }
    assert new_peer["acceptance"]["head"] == digest(first)
    retry = peer_accept(trust, home, first, expected=digest(first), require_verified=True)
    assert retry.disposition == "replay" and retry.peer.use.eligible
    assert saved(home).read_bytes() == complete


def test_r3_unconfirmed_future_head_still_requires_time_admission(trust, home, r2_admission_clock):
    peer_setup(trust, home, members=[PEER_PRINCIPAL])
    first = peer_record(created_at="9999-12-31T23:59:59Z")
    r2_admission_clock.setattr(trust, "_peer_admission_clock", lambda: trust._instant("9999-12-31T23:59:59Z"))
    observed = peer_observe(trust, home, first)
    assert observed.peer.provenance == "observed" and observed.peer.use.eligible
    assert not observed.peer.history[0]["confirmation"]
    peer_observe(trust, home, peer_record(2))
    before = saved(home).read_bytes()
    r2_admission_clock.setattr(trust, "_peer_admission_clock", lambda: trust._instant(TIME))
    with pytest.raises(crypto.MeshError, match="^mesh_peer_created_in_future$"):
        peer_accept(trust, home, first, expected=digest(first), require_verified=True)
    assert saved(home).read_bytes() == before
    assert peer_load(trust, home).use.error == "recipient_key_changed"


def test_r3_confirmed_resolution_still_checks_serialized_capacity(trust, home, r2_admission_clock):
    peer_setup(trust, home)
    first = peer_record()
    peer_accept(trust, home, first, require_verified=True)
    assert peer_observe(trust, home, peer_record(2)).evidence_recorded
    before = saved(home).read_bytes()
    state = json.loads(before)
    r2_admission_clock.setattr(
        trust, "_PEER_BYTES", r1_wire_size({name: state[name] for name in ("peers", "peer_records")})
    )

    def forbidden():
        raise AssertionError("resolution consulted admission clock")

    r2_admission_clock.setattr(trust, "_peer_admission_clock", forbidden)
    with pytest.raises(crypto.MeshError, match="^mesh_trust_capacity$"):
        peer_accept(trust, home, first, expected=digest(first), require_verified=True)
    assert saved(home).read_bytes() == before
    assert peer_load(trust, home).use.error == "recipient_key_changed"
