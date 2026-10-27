"""Local custody contracts; all key material and paths are disposable."""

from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
import errno
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from brigade import fleet_mesh_crypto as crypto

HUB = "h_" + crypto.encode_binary(b"h" * 16)
OTHER_HUB = "h_" + crypto.encode_binary(b"j" * 16)
POSIX_ONLY = pytest.mark.skipif(os.name != "posix", reason="custody requires POSIX dirfd/no-follow and flock")


def _load(home, *, create=False, principal="node-a", hub_id=HUB):
    from brigade.fleet_mesh_keys import load_local_keys

    return load_local_keys(brigade_home=home, principal=principal, hub_id=hub_id, create=create)


def _rotate(home):
    from brigade.fleet_mesh_keys import rotate_local_keys

    return rotate_local_keys(brigade_home=home, principal="node-a", hub_id=HUB)


def _state_path(home):
    return next((home / "fleet" / "mesh-keys").glob("*.json"))


def _concurrent_init(home):
    return _load(Path(home), create=True).active.record["kid"]


@POSIX_ONLY
def test_initialization_restart_and_reads_preserve_material_and_files(tmp_path):
    home = tmp_path / "custody"
    first = _load(home, create=True)
    assert first.active.private_keys.signing_key != first.active.private_keys.kem_seed
    assert len(first.retry_key) == 32
    assert first.retired == ()
    snapshot = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in home.rglob("*") if p.is_file()}
    for create in (False, True):
        assert _load(home, create=create) == first
        assert snapshot == {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in snapshot}
    code = """
import sys
from pathlib import Path
from brigade.fleet_mesh_keys import load_local_keys
state = load_local_keys(brigade_home=Path(sys.argv[1]), principal='node-a', hub_id=sys.argv[2])
print(state.active.record['kid'])
"""
    result = subprocess.run([sys.executable, "-c", code, str(home), HUB], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == first.active.record["kid"]
    for path in (home, home / "fleet", home / "fleet" / "mesh-keys"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    assert stat.S_IMODE(_state_path(home).stat().st_mode) == 0o600
    assert repr(first.active.private_keys.signing_key) not in repr(first)
    assert repr(first.active.private_keys.kem_seed) not in repr(first)
    assert repr(first.retry_key) not in repr(first)


@POSIX_ONLY
def test_rotation_retains_history_retry_key_and_valid_predecessor_signatures(tmp_path):
    first = _load(tmp_path, create=True)
    second = _rotate(tmp_path)
    third = _rotate(tmp_path)
    assert first.retry_key == second.retry_key == third.retry_key
    assert third.active.private_keys != second.active.private_keys != first.active.private_keys
    assert third.retired[0].private_keys == first.active.private_keys
    assert third.retired[1].private_keys == second.active.private_keys
    assert third.retired[0].retired_at == second.active.record["created_at"]
    assert third.retired[1].retired_at == third.active.record["created_at"]
    crypto.verify_key_record(second.active.record, previous_record=first.active.record)
    crypto.verify_key_record(third.active.record, previous_record=second.active.record)
    assert _load(tmp_path) == third


@POSIX_ONLY
def test_rotation_accepts_saved_utc_z_timestamp_on_python310(tmp_path, monkeypatch):
    from brigade import fleet_mesh_keys

    first = _load(tmp_path, create=True)
    path = _state_path(tmp_path)
    state = json.loads(path.read_bytes())
    state["entries"][0]["record"] = crypto.build_key_record(
        first.active.private_keys,
        principal="node-a",
        hub=HUB,
        created_at=first.active.record["created_at"].replace("+00:00", "Z"),
    )
    path.write_bytes(crypto.canonical(state))
    saved = _load(tmp_path)
    assert saved.active.record["created_at"].endswith("Z")

    class Python310Datetime(datetime):
        @classmethod
        def fromisoformat(cls, value):
            # Python 3.10 requires the numeric UTC offset. Keep the regression
            # effective on newer interpreters that also accept a literal Z.
            if value.endswith("Z"):
                raise ValueError("Invalid isoformat string")
            return super().fromisoformat(value)

    monkeypatch.setattr(fleet_mesh_keys, "datetime", Python310Datetime)
    rotated = _rotate(tmp_path)
    assert rotated.retry_key == saved.retry_key == first.retry_key
    assert len(rotated.retired) == 1
    assert rotated.retired[0].private_keys == saved.active.private_keys == first.active.private_keys
    assert rotated.retired[0].record == saved.active.record
    assert rotated.retired[0].retired_at == rotated.active.record["created_at"]
    crypto.verify_key_record(rotated.active.record, previous_record=saved.active.record)
    assert _load(tmp_path) == rotated


@POSIX_ONLY
def test_namespaces_are_explicit_and_separate(tmp_path):
    own = _load(tmp_path, create=True)
    other_principal = _load(tmp_path, create=True, principal="agent:worker")
    other_hub = _load(tmp_path, create=True, hub_id=OTHER_HUB)
    assert len({s.active.record["kid"] for s in (own, other_principal, other_hub)}) == 3
    assert len({s.retry_key for s in (own, other_principal, other_hub)}) == 3
    assert _load(tmp_path) == own


@pytest.mark.parametrize("operation", ["read", "rotate"])
@POSIX_ONLY
def test_missing_state_does_not_initialize_implicitly(tmp_path, operation):
    with pytest.raises(crypto.MeshError, match="mesh_keys_missing"):
        (_load if operation == "read" else _rotate)(tmp_path / "missing")
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize("component", ["ancestor", "home", "fleet", "mesh-keys", "state"])
@POSIX_ONLY
def test_symlinks_are_refused_at_every_traversed_component(tmp_path, component):
    home = tmp_path / "real" / "custody"
    home.parent.mkdir(mode=0o700)
    _load(home, create=True)
    if component == "ancestor":
        alias = tmp_path / "alias"
        alias.symlink_to(home.parent, target_is_directory=True)
        home = alias / "custody"
    else:
        target = {
            "home": home,
            "fleet": home / "fleet",
            "mesh-keys": home / "fleet" / "mesh-keys",
            "state": _state_path(home),
        }[component]
        moved = target.with_name(target.name + ".real")
        target.rename(moved)
        target.symlink_to(moved, target_is_directory=component != "state")
    with pytest.raises(crypto.MeshError, match="mesh_keys_unsafe"):
        _load(home, create=True)


@pytest.mark.parametrize("target", ["home", "fleet", "mesh-keys", "state"])
@POSIX_ONLY
def test_unsafe_permissions_are_rejected_without_repair(tmp_path, target):
    _load(tmp_path, create=True)
    path = {
        "home": tmp_path,
        "fleet": tmp_path / "fleet",
        "mesh-keys": tmp_path / "fleet" / "mesh-keys",
        "state": _state_path(tmp_path),
    }[target]
    mode = 0o640 if target == "state" else 0o750
    path.chmod(mode)
    with pytest.raises(crypto.MeshError, match="mesh_keys_unsafe"):
        _load(tmp_path, create=True)
    assert stat.S_IMODE(path.stat().st_mode) == mode


@pytest.mark.parametrize("kind", ["hardlink", "fifo", "directory", "owner"])
@POSIX_ONLY
def test_unsafe_state_files_fail_closed(tmp_path, monkeypatch, kind):
    _load(tmp_path, create=True)
    path = _state_path(tmp_path)
    if kind == "hardlink":
        os.link(path, tmp_path / "duplicate")
    elif kind == "fifo":
        path.unlink()
        os.mkfifo(path, 0o600)
    elif kind == "directory":
        path.unlink()
        path.mkdir(mode=0o700)
    else:
        real_fstat = os.fstat
        inode = path.stat().st_ino

        def foreign_owner(fd):
            info = real_fstat(fd)
            if info.st_ino == inode and stat.S_ISREG(info.st_mode):
                fields = list(info)
                fields[4] = os.geteuid() + 1
                return os.stat_result(fields)
            return info

        monkeypatch.setattr(os, "fstat", foreign_owner)
    with pytest.raises(crypto.MeshError, match="mesh_keys_unsafe"):
        _load(tmp_path, create=True)


@pytest.mark.parametrize(
    "corruption", ["truncated", "duplicate", "principal", "hub", "seed", "record", "retired", "version"]
)
@POSIX_ONLY
def test_corrupt_or_mixed_state_never_regenerates_keys(tmp_path, corruption):
    _load(tmp_path, create=True)
    _rotate(tmp_path)
    path = _state_path(tmp_path)
    state = json.loads(path.read_text())
    if corruption == "truncated":
        raw = b'{"v":'
    elif corruption == "duplicate":
        raw = path.read_bytes().replace(b'"v":1', b'"v":1,"v":1', 1)
    else:
        if corruption in {"principal", "hub"}:
            state["principal" if corruption == "principal" else "hub_id"] = (
                "node-b" if corruption == "principal" else OTHER_HUB
            )
        elif corruption == "seed":
            state["entries"][-1]["kem_seed"] = crypto.encode_binary(b"short")
        elif corruption == "record":
            state["entries"][-1]["record"] = state["entries"][0]["record"]
        elif corruption == "retired":
            state["entries"][0]["retired_at"] = None
        else:
            state["v"] = True
        raw = json.dumps(state).encode()
    path.write_bytes(raw)
    with pytest.raises(crypto.MeshError, match="mesh_keys_invalid"):
        _load(tmp_path, create=True)
    assert path.read_bytes() == raw


@POSIX_ONLY
def test_concurrent_initialization_and_rotation_do_not_lose_keys(tmp_path):
    with ProcessPoolExecutor(max_workers=4) as pool:
        kids = list(pool.map(_concurrent_init, [str(tmp_path)] * 8))
    assert len(set(kids)) == 1
    with ProcessPoolExecutor(max_workers=4) as pool:
        rotations = list(pool.map(_rotate, [tmp_path] * 4))
    final = _load(tmp_path)
    assert len(final.retired) == 4
    assert {entry.record["kid"] for entry in (*final.retired, final.active)} == {kids[0]} | {
        state.active.record["kid"] for state in rotations
    }


@pytest.mark.parametrize("stage", ["before_replace", "after_replace"])
@POSIX_ONLY
def test_process_crash_during_rotation_leaves_one_complete_generation(tmp_path, stage):
    old = _load(tmp_path, create=True)
    code = """
import os, sys
from pathlib import Path
from brigade.fleet_mesh_keys import rotate_local_keys
real_replace = os.replace
def crash(*args, **kwargs):
    if sys.argv[3] == 'after_replace':
        real_replace(*args, **kwargs)
    os._exit(71)
os.replace = crash
rotate_local_keys(brigade_home=Path(sys.argv[1]), principal='node-a', hub_id=sys.argv[2])
"""
    child = subprocess.run([sys.executable, "-c", code, str(tmp_path), HUB, stage], capture_output=True)
    assert child.returncode == 71, child.stderr
    directory = _state_path(tmp_path).parent
    orphans = list(directory.glob(".*.tmp"))
    snapshot = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in directory.iterdir()}
    saved = _load(tmp_path)
    assert snapshot == {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in directory.iterdir()}
    assert saved.retry_key == old.retry_key
    if stage == "before_replace":
        assert saved == old
        assert len(orphans) == 1
        assert stat.S_IMODE(orphans[0].stat().st_mode) == 0o600
    else:
        assert saved.retired[0].private_keys == old.active.private_keys
        crypto.verify_key_record(saved.active.record, previous_record=old.active.record)
    assert _load(tmp_path) == saved
    rotated = _rotate(tmp_path)
    assert rotated.retry_key == old.retry_key
    assert rotated.retired[-1].private_keys == saved.active.private_keys
    assert _load(tmp_path) == rotated
    assert list(directory.glob(".*.tmp")) == []


@POSIX_ONLY
def test_import_without_extra_and_primitive_failure_are_lazy(tmp_path):
    code = """
import importlib.abc, sys
from pathlib import Path
class NoCrypto(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'cryptography' or fullname.startswith('cryptography.'):
            raise ModuleNotFoundError(fullname)
sys.meta_path.insert(0, NoCrypto())
from brigade.fleet_mesh_keys import load_local_keys
from brigade.fleet_mesh_crypto import MeshExtraMissing
assert not any(n.startswith('cryptography') for n in sys.modules)
try:
    load_local_keys(brigade_home=Path(sys.argv[1]), principal='node-a', hub_id=sys.argv[2], create=True)
except MeshExtraMissing:
    pass
else:
    raise AssertionError('missing mesh extra was not reported')
"""
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path), HUB], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert list(tmp_path.rglob("*.json")) == []


@pytest.mark.parametrize("operation", ["load", "create", "rotate"])
def test_non_posix_custody_fails_closed_without_writes(tmp_path, monkeypatch, operation):
    from brigade import fleet_mesh_keys as custody

    home = tmp_path / "absent"
    with monkeypatch.context() as boundary:
        if os.name == "posix":
            boundary.setattr(custody.os, "name", "nt")
        with pytest.raises(crypto.MeshError) as caught:
            if operation == "rotate":
                _rotate(home)
            else:
                _load(home, create=operation == "create")
    assert caught.value.code == "mesh_keys_platform_unsupported"
    assert list(tmp_path.iterdir()) == []


@POSIX_ONLY
@pytest.mark.parametrize("failure", ["backend", "extra"])
def test_saved_keys_preserve_backend_diagnostics_without_mutation(tmp_path, monkeypatch, failure):
    saved = _load(tmp_path, create=True)
    _rotate(tmp_path)
    path = _state_path(tmp_path)
    snapshot = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in path.parent.iterdir()}

    def unavailable():
        if failure == "extra":
            raise crypto.MeshExtraMissing()
        raise crypto.MeshError("mesh_backend_unsupported")

    with monkeypatch.context() as boundary:
        boundary.setattr(crypto, "_backend", unavailable)
        for create in (False, True):
            with pytest.raises(crypto.MeshError) as caught:
                _load(tmp_path, create=create)
            assert caught.value.code == ("mesh_extra_missing" if failure == "extra" else "mesh_backend_unsupported")
            if failure == "extra":
                assert isinstance(caught.value, crypto.MeshExtraMissing)
    assert snapshot == {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in path.parent.iterdir()}
    assert _load(tmp_path).retry_key == saved.retry_key


@POSIX_ONLY
@pytest.mark.parametrize("failure", ["ENOSPC", "EDQUOT", "EIO", "EROFS"])
@pytest.mark.parametrize("stage", ["open", "fsync", "replace"])
def test_storage_io_failure_is_sanitized_and_preserves_active_keys(tmp_path, monkeypatch, failure, stage):
    failure = getattr(errno, failure)
    saved = _load(tmp_path, create=True)
    path = _state_path(tmp_path)
    original = path.read_bytes()
    real_open, real_fsync = os.open, os.fsync

    def fail():
        raise OSError(failure, "private path and material must not escape", str(path))

    def open_file(name, flags, *args, **kwargs):
        if flags & os.O_WRONLY:
            fail()
        return real_open(name, flags, *args, **kwargs)

    def fsync_file(fd):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            fail()
        return real_fsync(fd)

    with monkeypatch.context() as boundary:
        if stage == "open":
            boundary.setattr(os, "open", open_file)
        elif stage == "fsync":
            boundary.setattr(os, "fsync", fsync_file)
        else:
            boundary.setattr(os, "replace", lambda *args, **kwargs: fail())
        with pytest.raises(crypto.MeshError) as caught:
            _rotate(tmp_path)
    assert caught.value.code == "mesh_keys_io_error"
    assert str(caught.value) == "mesh_keys_io_error"
    assert path.read_bytes() == original
    assert _load(tmp_path) == saved
    assert list(path.parent.glob(".*.tmp")) == []


@POSIX_ONLY
@pytest.mark.parametrize("failure", ["ENOSPC", "EDQUOT", "EIO", "EROFS"])
def test_directory_fsync_failure_reloads_new_generation_before_later_rotation(tmp_path, monkeypatch, failure):
    _load(tmp_path, create=True)
    saved = _rotate(tmp_path)
    path = _state_path(tmp_path)
    original = path.read_bytes()
    real_fsync, real_replace = os.fsync, os.replace
    replacements = []

    def replace(*args, **kwargs):
        real_replace(*args, **kwargs)
        replacements.append(path.read_bytes())

    def fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            assert len(replacements) == 1
            raise OSError(getattr(errno, failure), "private path and material must not escape", str(path))
        return real_fsync(fd)

    with monkeypatch.context() as boundary:
        boundary.setattr(os, "replace", replace)
        boundary.setattr(os, "fsync", fsync)
        with pytest.raises(crypto.MeshError) as caught:
            _rotate(tmp_path)
    assert caught.value.code == "mesh_keys_io_error"
    assert str(caught.value) == "mesh_keys_io_error"
    assert len(replacements) == 1 and path.read_bytes() == replacements[0] != original
    snapshot = (path.read_bytes(), path.stat().st_mtime_ns)
    reconciled = _load(tmp_path)
    assert reconciled.retry_key == saved.retry_key
    assert reconciled.active.private_keys != saved.active.private_keys
    assert reconciled.retired[:-1] == saved.retired
    assert reconciled.retired[-1].private_keys == saved.active.private_keys
    assert reconciled.retired[-1].record == saved.active.record
    assert reconciled.retired[-1].retired_at == reconciled.active.record["created_at"]
    crypto.verify_key_record(reconciled.active.record, previous_record=saved.active.record)
    assert _load(tmp_path, create=True) == reconciled
    assert (path.read_bytes(), path.stat().st_mtime_ns) == snapshot
    assert list(path.parent.glob(".*.tmp")) == []
    later = _rotate(tmp_path)
    assert later.retry_key == reconciled.retry_key
    assert later.retired[:-1] == reconciled.retired
    assert later.retired[-1].private_keys == reconciled.active.private_keys
    assert later.retired[-1].record == reconciled.active.record
    crypto.verify_key_record(later.active.record, previous_record=reconciled.active.record)
    assert _load(tmp_path) == later


@POSIX_ONLY
def test_mutation_reaps_only_private_regular_exact_temporary_names(tmp_path, monkeypatch):
    saved = _load(tmp_path, create=True)
    directory = _state_path(tmp_path).parent
    safe = directory / ("." + "a" * 32 + ".tmp")
    safe.write_bytes(b"orphan contents are never decoded or promoted")
    safe.chmod(0o600)
    targets = {
        kind: directory / ("." + digit * 32 + ".tmp")
        for kind, digit in (
            ("symlink", "b"),
            ("hardlink", "c"),
            ("fifo", "d"),
            ("directory", "e"),
            ("mode", "f"),
            ("owner", "0"),
        )
    }
    outside = tmp_path / "unrelated"
    outside.write_bytes(b"leave this alone")
    targets["symlink"].symlink_to(outside)
    os.link(outside, targets["hardlink"])
    outside.chmod(0o600)
    os.mkfifo(targets["fifo"], 0o600)
    targets["directory"].mkdir(mode=0o700)
    for kind in ("mode", "owner"):
        targets[kind].write_bytes(b"untouched")
        targets[kind].chmod(0o640 if kind == "mode" else 0o600)
    for name in ("." + "A" * 32 + ".tmp", ".short.tmp", "unrelated.tmp"):
        path = directory / name
        path.write_bytes(b"untouched")
        path.chmod(0o600)
    names = set(os.listdir(directory))
    real_stat = os.stat
    foreign_inode = targets["owner"].stat().st_ino

    def foreign_owner(*args, **kwargs):
        info = real_stat(*args, **kwargs)
        if info.st_ino == foreign_inode:
            fields = list(info)
            fields[4] = os.geteuid() + 1
            return os.stat_result(fields)
        return info

    with monkeypatch.context() as boundary:
        boundary.setattr(os, "stat", foreign_owner)
        assert _load(tmp_path) == saved
        assert set(os.listdir(directory)) == names
        rotated = _rotate(tmp_path)
    assert set(os.listdir(directory)) == names - {safe.name}
    assert targets["symlink"].is_symlink()
    assert targets["hardlink"].stat().st_nlink == 2
    assert stat.S_ISFIFO(targets["fifo"].stat().st_mode)
    assert targets["directory"].is_dir()
    assert targets["mode"].read_bytes() == targets["owner"].read_bytes() == b"untouched"
    assert outside.read_bytes() == b"leave this alone"
    assert rotated.retry_key == saved.retry_key
    assert _load(tmp_path) == rotated
