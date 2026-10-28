"""Offline identity persistence with deterministic public test keys."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from brigade import dirfd
from brigade import fleet_mesh_crypto as crypto
from brigade import fleet_mesh_statements as statements

ORIGIN = "https://hub.example"
OTHER = "https://other.example"
SEED = bytes(range(32))
SEED2 = bytes(reversed(range(32)))
TIME = "2026-01-02T00:00:00Z"


def key(seed=SEED):
    return Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes_raw()


def hub(seed=SEED):
    return "h_" + crypto.encode_binary(hashlib.sha256(crypto.LABELS["hub-id"] + key(seed)).digest()[:16])


def signer(seed=SEED):
    return hashlib.sha256(key(seed)).hexdigest()


def signed(*, origin=ORIGIN, seq=3, seed=SEED, **fields):
    value = {
        "v": 1,
        "hub_id": hub(seed),
        "hub_sig_pub": crypto.encode_binary(key(seed)),
        "endpoint": origin,
        "require_sealed": False,
        "max_envelope_bytes": 98304,
        "envelope_versions": [1],
        "suites": ["bm1"],
        "backend": "full",
        "capabilities": ["messages"],
        "created_at": TIME,
        "seq": seq,
        **fields,
    }
    return crypto.canonical(statements.sign_statement(value, seed, expected_kind="hub-identity"))


def digest(raw):
    return hashlib.sha256(crypto.canonical(json.loads(raw))).hexdigest()


@pytest.fixture
def identity():
    return importlib.import_module("brigade.fleet_mesh_hub_identity")


@pytest.fixture
def home(tmp_path):
    if not dirfd.posix_available():
        pytest.skip("identity snapshots require POSIX dirfd and flock")
    path = tmp_path / "private"
    path.mkdir(mode=0o700)
    return path


def saved(home):
    return home / "fleet" / "mesh-keys" / "hub-identity-state.json"


def observe(identity, home, raw=None, *, origin=ORIGIN, **fields):
    return identity.observe_identity(brigade_home=home, origin=origin, raw=raw or signed(origin=origin, **fields))


def load(identity, home, origin=ORIGIN):
    return identity.load_identity(brigade_home=home, origin=origin)


def floor(identity, home, raw, *, claimed_digest=None):
    value = json.loads(raw)
    return identity.require_identity_floor(
        brigade_home=home,
        kind="hub-identity",
        signer=hashlib.sha256(crypto.decode_binary(value["hub_sig_pub"], 32)).hexdigest(),
        hub=value["hub_id"],
        seq=value["seq"],
        digest=claimed_digest or digest(raw),
    )


def test_explicit_init_observed_restart_and_detached_results(home):
    identity = importlib.import_module("brigade.fleet_mesh_hub_identity")
    with pytest.raises(crypto.MeshError, match="^hub_identity_unverified$"):
        load(identity, home)
    assert not (home / "fleet").exists()
    identity.initialize_identity_store(brigade_home=home)
    before = saved(home).read_bytes()
    with pytest.raises(crypto.MeshError, match="^hub_identity_unverified$"):
        load(identity, home)
    assert saved(home).read_bytes() == before
    raw = signed()
    assert observe(identity, home, raw).status == "accepted"
    snapshot = load(identity, home, "HTTPS://HUB.EXAMPLE:443/")
    assert snapshot.identity == json.loads(raw)
    assert snapshot.provenance == "observed"
    assert snapshot.policy_freshness == "unproven"
    assert snapshot.unsatisfied_floors == ()
    state = snapshot.data
    assert state["objects"][digest(raw)] == raw.decode("ascii")
    assert state["marks"]["hub-identity:" + signer()] == {"seq": 3, "digest": digest(raw)}
    assert state["origins"][ORIGIN]["first_digest"] == digest(raw)
    snapshot.identity["seq"] = 99
    state["origins"].clear()
    assert load(identity, home).identity["seq"] == 3
    assert saved(home).stat().st_mode & 0o777 == 0o600
    assert saved(home).parent.stat().st_mode & 0o777 == 0o700
    assert observe(identity, home, raw).status == "replay"
    persisted = saved(home).read_bytes()
    identity.initialize_identity_store(brigade_home=home)
    assert saved(home).read_bytes() == persisted


@pytest.mark.parametrize("fault", ["signature", "malformed", "origin", "hub", "migration"])
def test_bad_bootstrap_never_creates_or_changes_storage(identity, home, fault):
    raw = signed()
    value = json.loads(raw)
    if fault == "signature":
        value["sig"] = crypto.encode_binary(bytes(64))
    elif fault == "malformed":
        value["extra"] = 1
    elif fault == "origin":
        raw = signed(origin=OTHER)
    elif fault == "hub":
        value["hub_id"] = hub(SEED2)
    else:
        value = {"identity": value, "migration": {}}
    if fault != "origin":
        raw = crypto.canonical(value)
    for initialized in (False, True):
        if initialized:
            identity.initialize_identity_store(brigade_home=home)
        before = saved(home).read_bytes() if initialized else None
        with pytest.raises(crypto.MeshError):
            observe(identity, home, raw)
        if initialized:
            assert saved(home).read_bytes() == before
        else:
            assert not (home / "fleet").exists()


def test_global_mark_survives_new_origin_and_stale_origin_load_reimport(identity, home):
    identity.initialize_identity_store(brigade_home=home)
    raw = signed(seq=3)
    observe(identity, home, raw)
    observe(identity, home, origin=OTHER, seq=4)
    complete = saved(home).read_bytes()
    assert len(load(identity, home, OTHER).data["objects"]) == 2
    for action in (lambda: load(identity, home), lambda: observe(identity, home, raw)):
        with pytest.raises(crypto.MeshError, match="^signed_statement_rollback$"):
            action()
    assert saved(home).read_bytes() == complete
    observe(identity, home, seq=5)
    assert load(identity, home).identity["seq"] == 5
    assert len(load(identity, home).data["objects"]) == 3
    with pytest.raises(crypto.MeshError, match="^signed_statement_rollback$"):
        observe(identity, home, origin="https://third.example", seq=2)


def test_same_sequence_conflict_is_sticky_after_higher_heads(identity, home):
    identity.initialize_identity_store(brigade_home=home)
    first = signed()
    observe(identity, home, first)
    conflicting = signed(require_sealed=True)
    result = observe(identity, home, conflicting)
    assert result.status == "signed_statement_conflict" and result.evidence_recorded
    assert result.storage_error is None
    state = load(identity, home).data
    assert set(state["objects"]) == {digest(first)}
    assert len(state["conflicts"]) == 1
    observe(identity, home, seq=4)
    before = saved(home).read_bytes()
    assert observe(identity, home, conflicting).status == "signed_statement_conflict"
    assert saved(home).read_bytes() == before
    # A previously unseen old equivocation is still authenticated against history.
    second = signed(capabilities=[])
    assert observe(identity, home, second).evidence_recorded
    assert len(load(identity, home).data["conflicts"]) == 2


@pytest.mark.parametrize("floor_first", [True, False])
def test_floor_contradiction_retains_signed_history_in_either_order(identity, home, floor_first):
    identity.initialize_identity_store(brigade_home=home)
    raw = signed()
    if not floor_first:
        observe(identity, home, raw)
        observe(identity, home, seq=5)
    snap = floor(identity, home, raw, claimed_digest="0" * 64)
    assert snap.policy_freshness == "unproven"
    if floor_first:
        assert snap.unsatisfied_floors
    result = observe(identity, home, raw)
    assert result.status == "signed_statement_conflict" and result.evidence_recorded
    floor(identity, home, signed(seq=6))
    assert observe(identity, home, raw).evidence_recorded
    observe(identity, home, seq=7)
    state = load(identity, home).data
    assert len(state["conflicts"]) == 1
    assert (digest(raw) in state["objects"]) is not floor_first
    assert load(identity, home).unsatisfied_floors == ()


def test_floors_are_exact_full_signed_monotone_scoped_and_detached(identity, home):
    identity.initialize_identity_store(brigade_home=home)
    raw = signed()
    unsigned = json.loads(raw)
    del unsigned["sig"]
    wrong = hashlib.sha256(crypto.canonical(unsigned)).hexdigest()
    snap = floor(identity, home, raw, claimed_digest=wrong)
    assert snap.unsatisfied_floors[0]["digest"] == wrong
    snap.unsatisfied_floors[0]["digest"] = "f" * 64
    assert observe(identity, home, raw).status == "signed_statement_conflict"
    before = saved(home).read_bytes()
    with pytest.raises(crypto.MeshError, match="^mesh_identity_floor_not_tightened$"):
        floor(identity, home, raw)
    assert saved(home).read_bytes() == before
    floor(identity, home, signed(seq=4))
    with pytest.raises(crypto.MeshError, match="^signed_statement_rollback$"):
        observe(identity, home, seq=2)
    observe(identity, home, seq=4)
    assert load(identity, home).unsatisfied_floors == ()
    assert load(identity, home).policy_freshness == "unproven"


@pytest.mark.parametrize("source", ["operator", "invite"])
def test_confirmation_only_upgrades_existing_same_identity_preserving_times(identity, home, source):
    identity.initialize_identity_store(brigade_home=home)
    with pytest.raises(crypto.MeshError, match="^hub_identity_unverified$"):
        identity.confirm_identity(brigade_home=home, origin=ORIGIN, expected_hub_id=hub(), source=source)
    observe(identity, home)
    original = load(identity, home).data["origins"][ORIGIN]
    snap = identity.confirm_identity(brigade_home=home, origin=ORIGIN, expected_hub_id=hub(), source=source)
    assert snap.provenance == "verified" and snap.policy_freshness == "unproven"
    assert source in snap.data["origins"][ORIGIN]["confirmations"]
    before = saved(home).read_bytes()
    identity.confirm_identity(brigade_home=home, origin=ORIGIN, expected_hub_id=hub(), source=source)
    assert saved(home).read_bytes() == before
    observe(identity, home, seq=4)
    entry = load(identity, home).data["origins"][ORIGIN]
    assert entry["accepted_at"] == original["accepted_at"]
    assert entry["first_digest"] == original["first_digest"]
    assert load(identity, home).provenance == "verified"
    before = saved(home).read_bytes()
    with pytest.raises(crypto.MeshError, match="^hub_identity_changed$"):
        identity.confirm_identity(brigade_home=home, origin=ORIGIN, expected_hub_id=hub(SEED2), source=source)
    with pytest.raises(crypto.MeshError, match="^hub_identity_changed$"):
        observe(identity, home, seed=SEED2, seq=50)
    assert saved(home).read_bytes() == before


def test_concurrent_origins_do_not_lose_updates_or_touch_other_stores(identity, home):
    identity.initialize_identity_store(brigade_home=home)
    custody = saved(home).parent / "custody.json"
    trust = saved(home).parent / "trust-state.json"
    custody.write_bytes(b"untouched custody")
    trust.write_bytes(b"untouched trust")

    def add(index):
        seed = bytes([index + 1]) * 32
        return observe(identity, home, origin=f"https://hub{index}.example", seed=seed)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(add, range(8)))
    assert all(result.status == "accepted" for result in results)
    state = load(identity, home, "https://hub0.example").data
    assert len(state["origins"]) == len(state["marks"]) == len(state["objects"]) == 8
    assert custody.read_bytes() == b"untouched custody" and trust.read_bytes() == b"untouched trust"


@pytest.mark.parametrize("limit", ["_MAX_OBJECTS", "_MAX_ORIGINS", "_MAX_BYTES", "_MAX_DIRECTORY_ENTRIES"])
def test_capacity_never_evicts_or_advances_identity(identity, home, monkeypatch, limit):
    identity.initialize_identity_store(brigade_home=home)
    observe(identity, home)
    before = saved(home).read_bytes()
    if limit == "_MAX_OBJECTS":
        bound = 1
    elif limit == "_MAX_ORIGINS":
        bound = 1
    elif limit == "_MAX_BYTES":
        bound = len(before) + 1
    else:
        bound = 0
    with monkeypatch.context() as limits:
        limits.setattr(identity, limit, bound)
        with pytest.raises(crypto.MeshError, match="^mesh_identity_capacity$"):
            observe(identity, home, origin=OTHER, seq=4)
    assert saved(home).read_bytes() == before
    assert len(load(identity, home).data["objects"]) == 1


def test_conflict_capacity_preserves_refusal_and_floor_transaction(identity, home, monkeypatch):
    identity.initialize_identity_store(brigade_home=home)
    raw = signed()
    observe(identity, home, raw)
    before = saved(home).read_bytes()
    with monkeypatch.context() as limits:
        limits.setattr(identity, "_MAX_CONFLICTS", 0)
        result = observe(identity, home, require_sealed=True)
        assert result.status == "signed_statement_conflict" and not result.evidence_recorded
        assert result.storage_error == "mesh_identity_capacity"
        with pytest.raises(crypto.MeshError, match="^mesh_identity_capacity$"):
            floor(identity, home, raw, claimed_digest="0" * 64)
    assert saved(home).read_bytes() == before
    assert observe(identity, home, require_sealed=True).evidence_recorded


@pytest.mark.parametrize(
    "damage",
    ["mode", "link", "symlink", "directory", "json", "noncanonical", "duplicate", "mark", "lineage", "evidence"],
)
def test_unsafe_or_corrupt_state_is_never_repaired(identity, home, tmp_path, damage):
    identity.initialize_identity_store(brigade_home=home)
    observe(identity, home)
    observe(identity, home, require_sealed=True)
    path = saved(home)
    raw = path.read_bytes()
    if damage == "mode":
        path.chmod(0o640)
    elif damage == "link":
        os.link(path, tmp_path / "linked")
    elif damage == "symlink":
        other = tmp_path / "outside"
        path.rename(other)
        path.symlink_to(other)
    elif damage == "directory":
        path.parent.chmod(0o750)
    elif damage in {"json", "noncanonical", "duplicate"}:
        path.write_bytes(
            b"{" if damage == "json" else raw + b"\n" if damage == "noncanonical" else b'{"v":1,' + raw[1:]
        )
    else:
        state = json.loads(raw)
        if damage == "mark":
            state["marks"].clear()
        elif damage == "lineage":
            state["origins"][ORIGIN]["first_digest"] = "0" * 64
        else:
            next(iter(state["conflicts"].values()))["digest"] = "0" * 64
        path.write_bytes(crypto.canonical(state))
    before = path.read_bytes()
    for action in (
        lambda: load(identity, home),
        lambda: identity.initialize_identity_store(brigade_home=home),
        lambda: observe(identity, home, seq=4),
    ):
        with pytest.raises(crypto.MeshError, match="^mesh_identity_(unsafe|invalid)$"):
            action()
    assert path.read_bytes() == before
    path.parent.chmod(0o700)


@pytest.mark.parametrize("argument", ["source", "kind", "origin", "signer", "seq"])
def test_explicit_policy_input_refusal_does_not_write(identity, home, argument):
    identity.initialize_identity_store(brigade_home=home)
    observe(identity, home)
    before = saved(home).read_bytes()
    with pytest.raises(crypto.MeshError):
        if argument == "source":
            identity.confirm_identity(brigade_home=home, origin=ORIGIN, expected_hub_id=hub(), source="response")
        elif argument == "origin":
            observe(identity, home, origin=ORIGIN + "/path")
        else:
            args = {"kind": "hub-identity", "signer": signer(), "hub": hub(), "seq": 3, "digest": digest(signed())}
            args[argument] = "manifest" if argument == "kind" else "0" * 63 if argument == "signer" else True
            identity.require_identity_floor(brigade_home=home, **args)
    assert saved(home).read_bytes() == before


def test_platform_refusal_precedes_filesystem(identity, home, monkeypatch):
    with monkeypatch.context() as fault:
        fault.setattr(dirfd, "posix_available", lambda: False)
        for action in (
            lambda: identity.initialize_identity_store(brigade_home=home),
            lambda: load(identity, home),
            lambda: observe(identity, home),
        ):
            with pytest.raises(crypto.MeshError, match="^mesh_identity_platform_unsupported$"):
                action()
    assert not (home / "fleet").exists()


@pytest.fixture
def lifecycle_fault(monkeypatch):
    """Inject once at a real syscall; consume close and protect its new owner."""
    import errno
    import fcntl
    from contextlib import contextmanager

    @contextmanager
    def inject(point, error):
        original_open, original_close = dirfd.open_child_file, os.close
        original_fsync, original_replace, original_flock = os.fsync, dirfd.replace_children, fcntl.flock
        original_directory, original_child = dirfd.open_directory_nofollow, dirfd.open_child_directory
        seen = {
            "source": None,
            "writer": None,
            "root": None,
            "final": None,
            "faults": 0,
            "replacement": None,
            "live": None,
        }

        def fail():
            seen["faults"] += 1
            raise error

        def opened(parent, name, flags, *args, **kwargs):
            fd = original_open(parent, name, flags, *args, **kwargs)
            if name == "hub-identity-state.json":
                seen["source"] = fd
            elif name.startswith(".hub-identity-"):
                seen["writer"] = fd
            return fd

        def root(*args, **kwargs):
            fd = original_directory(*args, **kwargs)
            seen["root"] = fd
            return fd

        def child(parent, name, *args, **kwargs):
            fd = original_child(parent, name, *args, **kwargs)
            if name == "mesh-keys":
                seen["final"] = fd
            return fd

        def closed(fd):
            target = seen.get(
                {
                    "source-close": "source",
                    "writer-close": "writer",
                    "parent-close": "root",
                    "final-close": "final",
                }.get(point, "")
            )
            original_close(fd)
            if not seen["faults"] and fd == target:
                replacement = os.open(os.devnull, os.O_RDONLY)
                if replacement != fd:
                    os.dup2(replacement, fd)
                    original_close(replacement)
                seen["replacement"] = fd
                fail()

        def fsync(fd):
            original_fsync(fd)
            if not seen["faults"] and (
                (point == "file-fsync" and fd == seen["writer"]) or (point == "directory-fsync" and fd == seen["final"])
            ):
                fail()

        def replace(*args, **kwargs):
            if point == "replace" and not seen["faults"]:
                fail()
            return original_replace(*args, **kwargs)

        def flock(fd, operation):
            if point == "acquire" and operation != fcntl.LOCK_UN and not seen["faults"]:
                fail()
            original_flock(fd, operation)
            if point == "unlock" and operation == fcntl.LOCK_UN and not seen["faults"]:
                fail()

        try:
            with monkeypatch.context() as patch:
                patch.setattr(dirfd, "open_child_file", opened)
                patch.setattr(dirfd, "open_directory_nofollow", root)
                patch.setattr(dirfd, "open_child_directory", child)
                patch.setattr(os, "close", closed)
                patch.setattr(os, "fsync", fsync)
                patch.setattr(dirfd, "replace_children", replace)
                patch.setattr(fcntl, "flock", flock)
                yield seen
        finally:
            if seen["replacement"] is not None:
                try:
                    os.fstat(seen["replacement"])
                except OSError as failure:
                    assert failure.errno == errno.EBADF
                    seen["live"] = False
                else:
                    seen["live"] = True
                    original_close(seen["replacement"])

    return inject


@pytest.mark.parametrize("point", ["file-fsync", "writer-close", "replace", "directory-fsync", "unlock", "final-close"])
def test_public_mutation_faults_leave_complete_generations_and_reconcile(identity, home, lifecycle_fault, point):
    import errno

    identity.initialize_identity_store(brigade_home=home)
    observe(identity, home)
    before = saved(home).read_bytes()
    raw = signed(seq=4)
    with lifecycle_fault(point, OSError(errno.EIO, "private fault")) as seen:
        with pytest.raises(crypto.MeshError) as caught:
            observe(identity, home, raw)
    assert caught.value.code == (
        "mesh_identity_durability_indeterminate" if point == "directory-fsync" else "mesh_identity_storage_failure"
    )
    assert seen["faults"] == 1
    if "close" in point:
        assert seen["live"] is True
    complete = saved(home).read_bytes()
    committed = point in {"directory-fsync", "unlock", "final-close"}
    assert (complete != before) is committed
    assert load(identity, home).identity["seq"] == (4 if committed else 3)
    assert observe(identity, home, raw).status == ("replay" if committed else "accepted")
    if committed:
        assert saved(home).read_bytes() == complete


@pytest.mark.parametrize("point", ["source-close", "parent-close", "acquire", "unlock", "final-close"])
def test_read_cleanup_fixed_errors_and_descriptor_ownership(identity, home, lifecycle_fault, point):
    import errno

    identity.initialize_identity_store(brigade_home=home)
    observe(identity, home)
    before = saved(home).read_bytes()
    with lifecycle_fault(point, OSError(errno.EIO, "private fault", str(home))) as seen:
        with pytest.raises(crypto.MeshError, match="^mesh_identity_storage_failure$"):
            load(identity, home)
    assert seen["faults"] == 1
    if "close" in point:
        assert seen["live"] is True
    assert saved(home).read_bytes() == before
    assert load(identity, home).identity["seq"] == 3


@pytest.mark.parametrize("primary", ["backend", "extra", "cancel", "invalid"])
@pytest.mark.parametrize("point", ["source-close", "unlock", "final-close"])
def test_primary_read_error_survives_secondary_cleanup(identity, home, monkeypatch, lifecycle_fault, primary, point):
    import errno

    class Cancelled(BaseException):
        pass

    identity.initialize_identity_store(brigade_home=home)
    observe(identity, home)
    error = (
        crypto.MeshError("mesh_backend_unsupported")
        if primary == "backend"
        else crypto.MeshExtraMissing()
        if primary == "extra"
        else Cancelled()
    )
    if primary == "invalid":
        state = json.loads(saved(home).read_bytes())
        state["v"] = 2
        saved(home).write_bytes(crypto.canonical(state))
    caught = None
    with monkeypatch.context() as patch:
        if primary != "invalid":

            def unavailable():
                raise error

            patch.setattr(crypto, "probe_backend", unavailable)
        with lifecycle_fault(point, OSError(errno.EIO, "secondary cleanup")):
            try:
                load(identity, home)
            except BaseException as failure:
                caught = failure
    if primary == "invalid":
        assert type(caught) is crypto.MeshError and caught.code == "mesh_identity_invalid"
    else:
        assert caught is error


def test_noop_retries_flush_instead_of_reporting_unflushed_success(identity, home, lifecycle_fault):
    import errno

    identity.initialize_identity_store(brigade_home=home)
    observe(identity, home)
    raw = signed()
    before = saved(home).read_bytes()
    with lifecycle_fault("directory-fsync", OSError(errno.EIO, "flush")):
        with pytest.raises(crypto.MeshError, match="^mesh_identity_durability_indeterminate$"):
            observe(identity, home, raw)
    assert saved(home).read_bytes() == before
    assert observe(identity, home, raw).status == "replay"


def test_historical_cross_origin_equivocation_is_retained_after_global_advance(identity, home):
    identity.initialize_identity_store(brigade_home=home)
    observe(identity, home, seq=3)
    observe(identity, home, origin=OTHER, seq=4)
    observe(identity, home, seq=5)
    result = observe(identity, home, origin="https://third.example", seq=4)
    assert result.status == "signed_statement_conflict" and result.evidence_recorded
    state = load(identity, home).data
    assert len(state["origins"]) == 2 and len(state["objects"]) == 3
    assert len(state["conflicts"]) == 1


def test_cancellation_during_post_replace_cleanup_is_not_swallowed(identity, home, monkeypatch):
    class Cancelled(BaseException):
        pass

    identity.initialize_identity_store(brigade_home=home)
    observe(identity, home)
    original = dirfd.unlink_child
    error = Cancelled()

    def cancel(parent, name, **kwargs):
        if name.startswith(".hub-identity-"):
            raise error
        return original(parent, name, **kwargs)

    caught = None
    with monkeypatch.context() as patch:
        patch.setattr(dirfd, "unlink_child", cancel)
        try:
            observe(identity, home, seq=4)
        except BaseException as failure:
            caught = failure
    assert caught is error
    # Replacement completed, so retry reconciles the exact durable candidate.
    assert load(identity, home).identity["seq"] == 4
    before = saved(home).read_bytes()
    assert observe(identity, home, seq=4).status == "replay"
    assert saved(home).read_bytes() == before


@pytest.mark.parametrize("primary", ["io", "cancel", "backend", "extra"])
def test_writer_primary_survives_consumed_cleanup_close(identity, home, monkeypatch, primary):
    import errno
    import stat

    class Cancelled(BaseException):
        pass

    identity.initialize_identity_store(brigade_home=home)
    observe(identity, home)
    before = saved(home).read_bytes()
    original_fsync, original_close = os.fsync, os.close
    seen = {"destination": None, "replacement": None}
    error = (
        OSError(errno.EIO, "primary writer")
        if primary == "io"
        else Cancelled()
        if primary == "cancel"
        else crypto.MeshExtraMissing()
        if primary == "extra"
        else crypto.MeshError("mesh_backend_unsupported")
    )

    def fsync(fd):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            seen["destination"] = fd
            raise error
        return original_fsync(fd)

    def close(fd):
        original_close(fd)
        if fd == seen["destination"] and seen["replacement"] is None:
            replacement = os.open(os.devnull, os.O_RDONLY)
            if replacement != fd:
                os.dup2(replacement, fd)
                original_close(replacement)
            seen["replacement"] = fd
            raise Cancelled()

    caught = None
    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, "fsync", fsync)
            patch.setattr(os, "close", close)
            try:
                observe(identity, home, seq=4)
            except BaseException as failure:
                caught = failure
        assert seen["replacement"] is not None
        os.fstat(seen["replacement"])
    finally:
        if seen["replacement"] is not None:
            original_close(seen["replacement"])
    if primary == "io":
        assert type(caught) is crypto.MeshError and caught.code == "mesh_identity_storage_failure"
    else:
        assert caught is error
    assert saved(home).read_bytes() == before


@pytest.mark.parametrize("point", ["file-fsync", "directory-fsync", "unlock", "final-close"])
def test_conflict_failure_never_hides_refusal_or_advances_head(identity, home, lifecycle_fault, point):
    import errno

    identity.initialize_identity_store(brigade_home=home)
    observe(identity, home)
    with lifecycle_fault(point, OSError(errno.EIO, "conflict write")):
        result = observe(identity, home, require_sealed=True)
    assert result.status == "signed_statement_conflict" and not result.evidence_recorded
    assert result.storage_error == (
        "mesh_identity_durability_indeterminate" if point == "directory-fsync" else "mesh_identity_storage_failure"
    )
    state = load(identity, home).data
    assert len(state["objects"]) == 1 and load(identity, home).identity["require_sealed"] is False
    assert observe(identity, home, require_sealed=True).evidence_recorded


def test_busy_inode_lock_and_physical_path_refusal(identity, home, monkeypatch, tmp_path):
    import fcntl
    from brigade import fleet_mesh_trust

    identity.initialize_identity_store(brigade_home=home)
    observe(identity, home)
    before = saved(home).read_bytes()
    descriptor = dirfd.open_directory_nofollow(saved(home).parent)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with monkeypatch.context() as patch:
            patch.setattr(fleet_mesh_trust, "_LOCK_SECONDS", 0)
            with pytest.raises(crypto.MeshError, match="^mesh_identity_busy$"):
                observe(identity, home, seq=4)
    finally:
        os.close(descriptor)
    alias = tmp_path / "alias"
    alias.symlink_to(home, target_is_directory=True)
    with pytest.raises(crypto.MeshError, match="^mesh_identity_unsafe$"):
        identity.load_identity(brigade_home=alias, origin=ORIGIN)
    assert saved(home).read_bytes() == before


@pytest.mark.parametrize("operation", ["initialize", "confirm", "floor"])
def test_lost_return_reconciliation_for_every_mutator(identity, home, lifecycle_fault, operation):
    import errno

    if operation != "initialize":
        identity.initialize_identity_store(brigade_home=home)
        observe(identity, home)
    raw = signed(seq=4)

    def action():
        if operation == "initialize":
            return identity.initialize_identity_store(brigade_home=home)
        if operation == "confirm":
            return identity.confirm_identity(brigade_home=home, origin=ORIGIN, expected_hub_id=hub(), source="operator")
        return floor(identity, home, raw)

    with lifecycle_fault("final-close", OSError(errno.EIO, "lost return")):
        with pytest.raises(crypto.MeshError, match="^mesh_identity_storage_failure$"):
            action()
    complete = saved(home).read_bytes()
    action()
    assert saved(home).read_bytes() == complete
    if operation == "confirm":
        assert load(identity, home).provenance == "verified"
    elif operation == "floor":
        assert load(identity, home).unsatisfied_floors[0]["seq"] == 4


def test_independent_floor_and_confirmation_cannot_make_stale_origin_current(identity, home):
    identity.initialize_identity_store(brigade_home=home)
    first = signed()
    observe(identity, home, first)
    floor(identity, home, first)
    identity.confirm_identity(brigade_home=home, origin=ORIGIN, expected_hub_id=hub(), source="operator")
    observe(identity, home, origin=OTHER, seq=4)
    before = saved(home).read_bytes()
    for action in (
        lambda: load(identity, home),
        lambda: identity.confirm_identity(brigade_home=home, origin=ORIGIN, expected_hub_id=hub(), source="invite"),
    ):
        with pytest.raises(crypto.MeshError, match="^signed_statement_rollback$"):
            action()
    assert saved(home).read_bytes() == before
    assert load(identity, home, OTHER).provenance == "observed"
    assert load(identity, home, OTHER).policy_freshness == "unproven"


def test_authenticated_evidence_reserve_does_not_let_ordinary_writes_advance(identity, home, monkeypatch):
    identity.initialize_identity_store(brigade_home=home)
    observe(identity, home)
    before = saved(home).read_bytes()
    # A conflict has a generation with the same ordinary data and can use headroom.
    ordinary = len(crypto.canonical(dict(json.loads(before), conflicts={})))
    with monkeypatch.context() as limits:
        limits.setattr(identity, "_ORDINARY_BYTES", ordinary)
        result = observe(identity, home, require_sealed=True)
        assert result.status == "signed_statement_conflict" and result.evidence_recorded
        complete = saved(home).read_bytes()
        with pytest.raises(crypto.MeshError, match="^mesh_identity_capacity$"):
            observe(identity, home, seq=4)
        assert saved(home).read_bytes() == complete
    with monkeypatch.context() as limits:
        limits.setattr(identity, "_MAX_BYTES", len(complete))
        result = observe(identity, home, capabilities=[])
        assert result.status == "signed_statement_conflict" and not result.evidence_recorded
        assert result.storage_error == "mesh_identity_capacity"
    assert saved(home).read_bytes() == complete


def test_conflict_capacity_diagnosis_survives_secondary_directory_close(identity, home, monkeypatch, lifecycle_fault):
    import errno

    identity.initialize_identity_store(brigade_home=home)
    observe(identity, home)
    before = saved(home).read_bytes()
    with monkeypatch.context() as limits:
        limits.setattr(identity, "_MAX_CONFLICTS", 0)
        with lifecycle_fault("final-close", OSError(errno.EIO, "secondary")):
            result = observe(identity, home, require_sealed=True)
    assert result.status == "signed_statement_conflict" and not result.evidence_recorded
    assert result.storage_error == "mesh_identity_capacity"
    assert saved(home).read_bytes() == before


def test_restart_uses_persisted_evidence_and_load_never_flushes(identity, home, monkeypatch):
    import subprocess
    import sys

    identity.initialize_identity_store(brigade_home=home)
    observe(identity, home)
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; from brigade.fleet_mesh_hub_identity import load_identity; import sys; s=load_identity(brigade_home=Path(sys.argv[1]), origin='https://hub.example'); print(s.identity['seq'], s.provenance, s.policy_freshness)",
            str(home),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert process.returncode == 0, process.stderr
    assert process.stdout.strip() == "3 observed unproven"
    with monkeypatch.context() as patch:

        def forbidden(*args, **kwargs):
            raise AssertionError("load attempted a write")

        patch.setattr(dirfd, "fsync_directory", forbidden)
        patch.setattr(dirfd, "replace_children", forbidden)
        patch.setattr(dirfd, "unlink_child", forbidden)
        assert load(identity, home).identity["seq"] == 3


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
        ("accept", "io", False, False, False),
        ("accept", "cancel", False, False, False),
        ("conflict", "io", False, False, False),
        ("conflict", "cancel", False, False, False),
        ("accept", "io", True, False, False),
        ("accept", "io", False, True, False),
        ("conflict", "io", False, True, True),
    ],
)
def test_buffered_snapshot_preserves_primary_before_publish(
    identity, home, buffered_snapshot_fault, operation, secondary, partial, extra_cleanup, primary_error
):
    import errno

    class Cancelled(BaseException):
        pass

    identity.initialize_identity_store(brigade_home=home)
    observe(identity, home)
    before = saved(home).read_bytes()
    primary = RuntimeError("primary body error") if primary_error else Cancelled("primary buffered write")
    cleanup = (
        OSError(errno.EIO, "secondary stream reflush") if secondary == "io" else Cancelled("secondary stream reflush")
    )
    raw = signed(require_sealed=True) if operation == "conflict" else signed(seq=4)
    caught = None
    with buffered_snapshot_fault(primary, cleanup, partial=partial, extra_cleanup=extra_cleanup) as seen:
        try:
            observe(identity, home, raw)
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
    assert saved(home).read_bytes() == before
    assert list(saved(home).parent.glob(".hub-identity-*.tmp")) == []
    state = load(identity, home).data
    assert state["generation"] == json.loads(before)["generation"]
    assert state["conflicts"] == {} and len(state["objects"]) == 1
    assert load(identity, home).identity["seq"] == 3


@pytest.mark.parametrize("secondary", ["io", "cancel"])
def test_buffered_snapshot_close_only_failure_prevents_publish(identity, home, buffered_snapshot_fault, secondary):
    import errno

    class Cancelled(BaseException):
        pass

    identity.initialize_identity_store(brigade_home=home)
    observe(identity, home)
    before = saved(home).read_bytes()
    cleanup = OSError(errno.EIO, "stream close only") if secondary == "io" else Cancelled()
    caught = None
    with buffered_snapshot_fault(None, cleanup) as seen:
        try:
            observe(identity, home, seq=4)
        except BaseException as error:
            caught = error
    if secondary == "io":
        assert type(caught) is crypto.MeshError and caught.code == "mesh_identity_storage_failure"
    else:
        assert caught is cleanup
    assert seen["closes"] == seen["stream_closes"] == seen["unlinks"] == 1
    assert seen["streams"][0].closed
    assert saved(home).read_bytes() == before
    assert list(saved(home).parent.glob(".hub-identity-*.tmp")) == []


def test_cleanup_only_unlink_io_lost_return_reconciles_identity_generation(identity, home, monkeypatch):
    import errno

    identity.initialize_identity_store(brigade_home=home)
    observe(identity, home)
    previous = load(identity, home).data
    original_replace, original_unlink = dirfd.replace_children, dirfd.unlink_child
    seen = {"replaced": False, "unlinks": 0}

    def replace(*args, **kwargs):
        original_replace(*args, **kwargs)
        seen["replaced"] = True

    def unlink(parent, name, **kwargs):
        if seen["replaced"] and name.startswith(".hub-identity-"):
            seen["unlinks"] += 1
            raise OSError(errno.EIO, "post-replace cleanup")
        return original_unlink(parent, name, **kwargs)

    caught = None
    raw = signed(seq=4)
    with monkeypatch.context() as patch:
        patch.setattr(dirfd, "replace_children", replace)
        patch.setattr(dirfd, "unlink_child", unlink)
        try:
            observe(identity, home, raw)
        except BaseException as error:
            caught = error
    assert type(caught) is crypto.MeshError and caught.code == "mesh_identity_storage_failure"
    assert seen == {"replaced": True, "unlinks": 1}
    state = load(identity, home).data
    assert state["generation"] == previous["generation"] + 1
    assert state["marks"]["hub-identity:" + signer()] == {"seq": 4, "digest": digest(raw)}
    complete = saved(home).read_bytes()
    assert observe(identity, home, raw).status == "replay"
    assert saved(home).read_bytes() == complete
    assert list(saved(home).parent.glob(".hub-identity-*.tmp")) == []


@pytest.mark.parametrize("operation", ["accept", "conflict"])
@pytest.mark.parametrize("number", ["EIO", "EPERM"])
def test_postrename_cleanup_maps_inside_identity_writer(identity, home, monkeypatch, operation, number):
    import errno

    number = getattr(errno, number)
    identity.initialize_identity_store(brigade_home=home)
    observe(identity, home)
    before = load(identity, home).data
    raw = signed(seq=4) if operation == "accept" else signed(require_sealed=True)
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
        if seen["replaced"] and name.startswith(".hub-identity-"):
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
            result = observe(identity, home, raw)
        except BaseException as error:
            caught = error
    state = load(identity, home).data
    complete = saved(home).read_bytes()
    assert state["generation"] == before["generation"] + 1
    assert seen == {"replaced": True, "unlinks": 1, "flushes": 0}
    if operation == "conflict":
        assert state["objects"] == before["objects"] and state["marks"] == before["marks"]
        assert state["origins"] == before["origins"]
        assert len(state["conflicts"]) == 1
        assert next(iter(state["conflicts"].values()))["digest"] == digest(raw)
        assert caught is None
        assert result.status == "signed_statement_conflict" and not result.evidence_recorded
        assert result.storage_error == "mesh_identity_storage_failure"
    else:
        assert type(caught) is crypto.MeshError and caught.code == "mesh_identity_storage_failure"
        assert len(state["objects"]) == len(before["objects"]) + 1
        assert state["marks"]["hub-identity:" + signer()] == {"seq": 4, "digest": digest(raw)}
        assert load(identity, home).identity["seq"] == 4
        assert state["conflicts"] == before["conflicts"]
    seen["flushes"] = 0
    with monkeypatch.context() as patch:
        patch.setattr(dirfd, "fsync_directory", flush)
        retry = observe(identity, home, raw)
    assert seen["flushes"] == 1
    if operation == "conflict":
        assert retry.status == "signed_statement_conflict" and retry.evidence_recorded
        assert retry.storage_error is None
    else:
        assert retry.status == "replay"
    assert saved(home).read_bytes() == complete
    assert load(identity, home).data["generation"] == state["generation"]
    assert list(saved(home).parent.glob(".hub-identity-*.tmp")) == []
