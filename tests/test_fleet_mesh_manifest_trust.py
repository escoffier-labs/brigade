"""Offline manifests retain authenticated evidence without granting peer use."""

from dataclasses import replace
import errno
import json
import os
import stat
from types import SimpleNamespace

import pytest

from brigade import dirfd
from brigade import fleet_mesh_crypto as crypto
from brigade import fleet_mesh_statements as statements
from brigade import fleet_mesh_trust as trust
from tests import test_fleet_mesh_trust as fixtures
from tests.test_fleet_mesh_transport import prepared as prepared, server as server, transport as transport


@pytest.fixture
def home(tmp_path):
    if not dirfd.posix_available():
        pytest.skip("owner-only trust transactions require POSIX dirfd and flock")
    result = tmp_path / "private"
    result.mkdir(mode=0o700)
    fixtures.bootstrap(trust, result)
    return result


def manifest(*, hub=fixtures.HUB, seq=1, seed=fixtures.SEED, keys=()):
    return crypto.canonical(
        statements.sign_statement(
            {"v": 1, "hub": hub, "seq": seq, "created_at": fixtures.TIME, "keys": list(keys)},
            seed,
            expected_kind="manifest",
        )
    )


def entry(*, hub=fixtures.HUB, kid=fixtures.KID, principal="test-peer"):
    return {"hub": hub, "kid": kid, "principal": principal, "fingerprint": crypto.fingerprint(fixtures.key())}


def admit(home, raw=None, *, hub=fixtures.HUB):
    return trust.import_manifest(brigade_home=home, hub=hub, raw=manifest() if raw is None else raw)


def floor(home, raw, *, hub=fixtures.HUB, seed=fixtures.SEED):
    return trust.require_floor(
        brigade_home=home,
        hub=hub,
        kind="manifest",
        signer=fixtures.signer(seed),
        seq=json.loads(raw)["seq"],
        digest=fixtures.digest(raw),
    )


def test_manifest_explicit_admission_reopen_and_canonical_replay(home):
    assert callable(getattr(trust, "import_manifest", None)), "explicit offline manifest API is missing"
    raw = manifest(keys=[entry()])
    result = admit(home, raw)
    assert result.status == "accepted" and result.digest == fixtures.digest(raw)
    complete = fixtures.saved(home).read_bytes()
    snapshot = trust.load_trust(brigade_home=home)
    assert snapshot.data["objects"][result.digest]["canonical"].encode() == raw
    assert snapshot.data["marks"]["manifest:" + fixtures.signer()] == {"seq": 1, "digest": result.digest}
    assert snapshot.data["barriers"] == snapshot.data["baselines"] == {}
    reformatted = json.dumps(json.loads(raw), indent=2).encode()
    assert admit(home, reformatted).status == "replay"
    assert fixtures.saved(home).read_bytes() == complete
    with pytest.raises(crypto.MeshError, match="^statement_kind_unsupported$"):
        trust.import_statement(brigade_home=home, hub=fixtures.HUB, kind="manifest", raw=raw)
    assert fixtures.saved(home).read_bytes() == complete


def test_manifest_global_marks_equivocation_rollback_and_kind_independence(home):
    fixtures.pin(trust, home, hub=fixtures.OTHER)
    raw = manifest(seq=7)
    admit(home, raw)
    conflict = admit(home, manifest(hub=fixtures.OTHER, seq=7), hub=fixtures.OTHER)
    assert conflict.status == "signed_statement_conflict" and conflict.evidence_recorded
    snapshot = trust.load_trust(brigade_home=home).data
    assert snapshot["marks"]["manifest:" + fixtures.signer()]["digest"] == fixtures.digest(raw)
    assert len(snapshot["conflicts"]) == 1
    for kind in ("membership", "revocation"):
        assert fixtures.accept(trust, home, kind=kind, seq=0).status == "accepted"
    admit(home, manifest(hub=fixtures.OTHER, seq=8), hub=fixtures.OTHER)
    with pytest.raises(crypto.MeshError, match="^signed_statement_rollback$"):
        admit(home, raw)
    assert admit(home, manifest(hub=fixtures.OTHER, seq=7), hub=fixtures.OTHER) == conflict
    assert fixtures.digest(raw) in trust.load_trust(brigade_home=home).data["objects"]


@pytest.mark.parametrize("bad", ["signer", "hub", "signature", "entries", "kind", "bytes"])
def test_manifest_authentication_precedes_classification_without_writes(home, bad):
    admit(home, manifest(seq=2))
    before = fixtures.saved(home).read_bytes()
    raw = manifest(seq=2, seed=fixtures.SEED2) if bad == "signer" else manifest(seq=2)
    if bad == "hub":
        raw = manifest(seq=2, hub=fixtures.OTHER)
    if bad in {"signature", "entries"}:
        value = json.loads(raw)
        if bad == "signature":
            value["sig"] = crypto.encode_binary(bytes(64))
        else:
            value["keys"] = [dict(entry(), fingerprint="invalid")]
        raw = crypto.canonical(value)
    if bad == "kind":
        raw = fixtures.signed(seq=2)
    if bad == "bytes":
        raw = b" " * 2098177
    with pytest.raises(crypto.MeshError):
        admit(home, raw)
    assert fixtures.saved(home).read_bytes() == before


@pytest.mark.parametrize("first", ["floor", "object"])
def test_manifest_floor_contradiction_both_arrival_orders_and_advanced_head(home, first):
    raw = manifest(seq=3)
    expected = manifest(seq=3, keys=[entry()])
    if first == "object":
        admit(home, raw)
        admit(home, manifest(seq=4))
    floor(home, expected)
    result = admit(home, raw)
    assert result.status == "signed_statement_conflict" and result.evidence_recorded
    snapshot = trust.load_trust(brigade_home=home)
    assert len(snapshot.data["conflicts"]) == 1
    assert (fixtures.digest(raw) in snapshot.data["objects"]) == (first == "object")
    assert bool(snapshot.unsatisfied_floors) == (first == "floor")
    floor(home, manifest(seq=5))
    assert admit(home, raw) == result


def test_manifest_floors_scoped_by_hub_signer_kind_and_anchor_reactivation(home):
    fixtures.pin(trust, home, hub=fixtures.OTHER)
    floor(home, manifest(seq=6))
    with pytest.raises(crypto.MeshError, match="^signed_statement_rollback$"):
        admit(home, manifest(seq=5))
    assert admit(home, manifest(hub=fixtures.OTHER, seq=1), hub=fixtures.OTHER).status == "accepted"
    assert fixtures.accept(trust, home, seq=0).status == "accepted"
    admit(home, manifest(seq=6))
    fixtures.pin(trust, home, seed=fixtures.SEED2, previous=fixtures.signer())
    assert admit(home, manifest(seq=0, seed=fixtures.SEED2)).status == "accepted"
    with pytest.raises(crypto.MeshError):
        admit(home, manifest(seq=6))
    fixtures.pin(trust, home, previous=fixtures.signer(fixtures.SEED2))
    assert admit(home, manifest(seq=6)).status == "replay"
    with pytest.raises(crypto.MeshError, match="^signed_statement_rollback$"):
        admit(home, manifest(seq=5))
    assert len(trust.load_trust(brigade_home=home).data["hubs"][fixtures.HUB]) == 3


@pytest.mark.parametrize("limit", ["objects", "ordinary-bytes", "total-bytes", "conflicts"])
def test_manifest_capacity_does_not_evict_evidence(home, monkeypatch, limit):
    admit(home)
    before = fixtures.saved(home).read_bytes()
    with monkeypatch.context() as fault:
        if limit == "objects":
            fault.setattr(trust, "_MAX_OBJECTS", 1)
        elif limit == "ordinary-bytes":
            fault.setattr(trust, "_ORDINARY_BYTES", len(before) + 50)
        elif limit == "total-bytes":
            fault.setattr(trust, "_MAX_BYTES", len(before) + 50)
        else:
            fault.setattr(trust, "_MAX_CONFLICTS", 0)
        if limit == "conflicts":
            result = admit(home, manifest(keys=[entry()]))
            assert result.status == "signed_statement_conflict"
            assert not result.evidence_recorded and result.storage_error == "mesh_trust_capacity"
        else:
            with pytest.raises(crypto.MeshError, match="^mesh_trust_capacity$"):
                admit(home, manifest(seq=2))
    assert fixtures.saved(home).read_bytes() == before
    assert len(trust.load_trust(brigade_home=home).data["objects"]) == 1


@pytest.mark.parametrize("point", ["file-fsync", "replace", "directory-fsync", "lost-return", "cleanup"])
def test_manifest_durability_faults_reopen_and_same_operation_retry(home, monkeypatch, point):
    before = fixtures.saved(home).read_bytes()
    raw = manifest()
    with monkeypatch.context() as fault:
        original_sync = os.fsync
        original_flush = trust._flush

        def sync(descriptor):
            directory = stat.S_ISDIR(os.fstat(descriptor).st_mode)
            if directory == (point == "directory-fsync"):
                raise OSError(errno.EIO, "synthetic fsync failure")
            return original_sync(descriptor)

        def fail(*args, **kwargs):
            raise OSError(errno.EIO, "synthetic publication failure")

        def lost(descriptor):
            original_flush(descriptor)
            raise crypto.MeshError("mesh_trust_storage_failure")

        if point == "directory-fsync":
            fault.setattr(dirfd, "fsync_directory", fail)
        elif point == "file-fsync":
            # Module-local proxy leaves process-wide os and pytest capture alone.
            fault.setattr(trust, "os", SimpleNamespace(**{**vars(os), "fsync": sync}))
        elif point == "replace":
            fault.setattr(dirfd, "replace_children", fail)
        elif point == "lost-return":
            fault.setattr(trust, "_flush", lost)
        else:
            orphan = fixtures.saved(home).parent / (".trust-" + "a" * 32 + ".tmp")
            orphan.write_bytes(b"synthetic orphan")
            orphan.chmod(0o600)
            fault.setattr(dirfd, "unlink_child", fail)
        code = "mesh_trust_durability_indeterminate" if point == "directory-fsync" else "mesh_trust_storage_failure"
        with pytest.raises(crypto.MeshError, match="^" + code + "$"):
            admit(home, raw)
    published = point in {"directory-fsync", "lost-return"}
    snapshot = trust.load_trust(brigade_home=home)
    assert (fixtures.digest(raw) in snapshot.data["objects"]) == published
    if not published:
        assert fixtures.saved(home).read_bytes() == before
    complete = fixtures.saved(home).read_bytes()
    assert admit(home, raw).status == ("replay" if published else "accepted")
    if published:
        assert fixtures.saved(home).read_bytes() == complete
    assert fixtures.digest(raw) in trust.load_trust(brigade_home=home).data["objects"]


@pytest.mark.parametrize("mutation", ["canonical", "mark"])
def test_manifest_corruption_refuses_without_repair(home, mutation):
    admit(home)
    value = json.loads(fixtures.saved(home).read_bytes())
    if mutation == "canonical":
        value["objects"][fixtures.digest(manifest())]["canonical"] = manifest(seq=9).decode()
    else:
        value["marks"]["manifest:" + fixtures.signer()]["seq"] = 9
    corrupt = crypto.canonical(value)
    fixtures.saved(home).write_bytes(corrupt)
    with pytest.raises(crypto.MeshError, match="^mesh_trust_invalid$"):
        trust.load_trust(brigade_home=home)
    assert fixtures.saved(home).read_bytes() == corrupt


@pytest.mark.parametrize("state", ["missing", "observed", "verified", "changed", "revoked", "not-member"])
def test_manifest_listing_removal_and_relisting_never_change_peer_evidence_or_use(home, state):
    fixtures.peer_setup(trust, home, members=[] if state == "not-member" else [fixtures.PEER_PRINCIPAL])
    raw = fixtures.peer_record()
    value = json.loads(raw)
    if state in {"verified", "revoked"}:
        fixtures.peer_accept(trust, home, raw, require_verified=True)
    elif state != "missing":
        fixtures.peer_observe(trust, home, raw)
    if state == "changed":
        fixtures.peer_observe(trust, home, fixtures.peer_record(3))
    if state == "revoked":
        fixtures.accept(
            trust, home, hub=fixtures.PEER_HUB, kind="revocation", entries=[fixtures.revoked(kid=value["kid"])]
        )
    before = trust.load_trust(brigade_home=home).data
    peers = [fixtures.peer_load(trust, home, require_verified=required) for required in (False, True)]
    listed = entry(hub=fixtures.PEER_HUB, kid=value["kid"], principal=fixtures.PEER_PRINCIPAL)
    for seq, keys in enumerate(([listed], [], [listed]), 1):
        admit(home, manifest(hub=fixtures.PEER_HUB, seq=seq, keys=keys), hub=fixtures.PEER_HUB)
        after = trust.load_trust(brigade_home=home).data
        # Only retained objects, stream marks and generation may change.
        assert after == dict(before, objects=after["objects"], marks=after["marks"], generation=after["generation"])
        for required, previous in zip((False, True), peers, strict=True):
            current = fixtures.peer_load(trust, home, require_verified=required)
            assert current.data == after
            assert current.trust_generation == after["generation"]
            assert current.use is not None and previous.use is not None
            assert current.use.trust_generation == after["generation"]
            assert (
                replace(
                    current,
                    data=previous.data,
                    trust_generation=previous.trust_generation,
                    use=replace(current.use, trust_generation=previous.use.trust_generation),
                )
                == previous
            )


def test_manifest_non_posix_refusal_precedes_path_access(monkeypatch):
    class ForbiddenPath:
        def __fspath__(self):
            pytest.fail("filesystem path accessed before platform refusal")

    monkeypatch.setattr(trust, "os", SimpleNamespace(name="nt"))
    with pytest.raises(crypto.MeshError, match="^mesh_trust_platform_unsupported$"):
        trust.import_manifest(brigade_home=ForbiddenPath(), hub=fixtures.HUB, raw=manifest())


def test_old_snapshot_read_only_and_legacy_kind_restriction_refuses_without_repair(home, monkeypatch):
    before = fixtures.saved(home).read_bytes()
    trust.load_trust(brigade_home=home)
    assert fixtures.saved(home).read_bytes() == before
    admit(home)
    retained = fixtures.saved(home).read_bytes()
    # The pre-slice reader's retained-kind restriction, without Git-history dependency.
    with monkeypatch.context() as legacy:
        legacy.setattr(trust, "_KINDS", {"membership", "revocation"})
        with pytest.raises(crypto.MeshError, match="^mesh_trust_invalid$"):
            trust.load_trust(brigade_home=home)
    assert fixtures.saved(home).read_bytes() == retained
    assert trust.load_trust(brigade_home=home).data["objects"]


@pytest.mark.parametrize("missing", ["state", "anchor"])
def test_manifest_missing_prerequisite_never_initializes(tmp_path, missing):
    if not dirfd.posix_available():
        pytest.skip("owner-only trust transactions require POSIX dirfd and flock")
    home = tmp_path / "empty"
    home.mkdir(mode=0o700)
    if missing == "anchor":
        trust.initialize_trust(brigade_home=home)
    before = {p.relative_to(home): p.read_bytes() for p in home.rglob("*") if p.is_file()}
    code = "mesh_trust_missing" if missing == "state" else "mesh_trust_anchor_missing"
    with pytest.raises(crypto.MeshError, match="^" + code + "$"):
        admit(home)
    assert {p.relative_to(home): p.read_bytes() for p in home.rglob("*") if p.is_file()} == before


def test_manifest_replay_requires_flush_and_read_only_load_does_not_repair(home, monkeypatch):
    admit(home)
    before = fixtures.saved(home).read_bytes()

    def fail(*args):
        raise OSError(errno.EIO, "synthetic directory fsync failure")

    with monkeypatch.context() as fault:
        fault.setattr(dirfd, "fsync_directory", fail)
        assert trust.load_trust(brigade_home=home).data["objects"]
        with pytest.raises(crypto.MeshError, match="^mesh_trust_durability_indeterminate$"):
            admit(home)
    assert fixtures.saved(home).read_bytes() == before
    assert admit(home).status == "replay"


@pytest.mark.parametrize("point", ["directory-fsync", "capacity"])
def test_manifest_conflict_failure_preserves_diagnosis_and_retries(home, monkeypatch, point):
    admit(home)
    rejected = manifest(keys=[entry()])
    before = fixtures.saved(home).read_bytes()

    def fail(*args):
        raise OSError(errno.EIO, "synthetic conflict fsync failure")

    with monkeypatch.context() as fault:
        if point == "capacity":
            fault.setattr(trust, "_MAX_CONFLICTS", 0)
        else:
            fault.setattr(dirfd, "fsync_directory", fail)
        result = admit(home, rejected)
        assert result.status == "signed_statement_conflict" and not result.evidence_recorded
        assert result.storage_error == (
            "mesh_trust_capacity" if point == "capacity" else "mesh_trust_durability_indeterminate"
        )
    snapshot = trust.load_trust(brigade_home=home).data
    assert len(snapshot["conflicts"]) == (point == "directory-fsync")
    if point == "capacity":
        assert fixtures.saved(home).read_bytes() == before
    assert snapshot["marks"]["manifest:" + fixtures.signer()]["digest"] == fixtures.digest(manifest())
    assert admit(home, rejected).evidence_recorded


@pytest.mark.parametrize("unsupported", ["manifest", "future-kind"])
def test_transport_still_refuses_whole_bundle_after_offline_manifest_admission(
    prepared, server, transport, unsupported
):
    if prepared is None:
        pytest.skip("owner-only trust transactions require POSIX dirfd and flock")
    home, authority, _, identity = prepared
    hub = identity["hub_id"]
    fixtures.pin(trust, home, hub=hub, seed=fixtures.SEED2)
    raw = manifest(hub=hub, seed=fixtures.SEED2)
    assert admit(home, raw, hub=hub).status == "accepted"
    bundle = {
        "membership": json.loads(fixtures.signed(hub=hub, seed=fixtures.SEED2)),
        unsupported: json.loads(raw),
        "revocation": json.loads(fixtures.signed(hub=hub, seed=fixtures.SEED2, kind="revocation")),
    }
    server.callback = lambda body: (
        (200, {"principal": "node-a"}) if body["action"] == "identity" else (200, {"statements": bundle})
    )
    before = {p.relative_to(home): p.read_bytes() for p in home.rglob("*") if p.is_file()}
    with pytest.raises(transport.MeshTransportError) as caught:
        transport.import_hub_statements(brigade_home=home, authority=authority, expected_principal="node-a")
    assert caught.value.code == "mesh_transport_statements_unsupported"
    assert caught.value.remote_effect == "none"
    assert {p.relative_to(home): p.read_bytes() for p in home.rglob("*") if p.is_file()} == before
