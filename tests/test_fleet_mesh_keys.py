"""Local custody contracts; all key material and paths are disposable."""

from concurrent.futures import ProcessPoolExecutor
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


def test_namespaces_are_explicit_and_separate(tmp_path):
    own = _load(tmp_path, create=True)
    other_principal = _load(tmp_path, create=True, principal="agent:worker")
    other_hub = _load(tmp_path, create=True, hub_id=OTHER_HUB)
    assert len({s.active.record["kid"] for s in (own, other_principal, other_hub)}) == 3
    assert len({s.retry_key for s in (own, other_principal, other_hub)}) == 3
    assert _load(tmp_path) == own


@pytest.mark.parametrize("operation", ["read", "rotate"])
def test_missing_state_does_not_initialize_implicitly(tmp_path, operation):
    with pytest.raises(crypto.MeshError, match="mesh_keys_missing"):
        (_load if operation == "read" else _rotate)(tmp_path / "missing")
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize("component", ["ancestor", "home", "fleet", "mesh-keys", "state"])
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
def test_unsafe_state_files_fail_closed(tmp_path, kind):
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
        if os.geteuid() != 0:
            pytest.skip("changing file ownership requires root")
        path.chown(1, 1) if hasattr(path, "chown") else os.chown(path, 1, 1)
    with pytest.raises(crypto.MeshError, match="mesh_keys_unsafe"):
        _load(tmp_path, create=True)


@pytest.mark.parametrize(
    "corruption", ["truncated", "duplicate", "principal", "hub", "seed", "record", "retired", "version"]
)
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
    saved = _load(tmp_path, create=True)
    assert saved.retry_key == old.retry_key
    if stage == "before_replace":
        assert saved == old
    else:
        assert saved.retired[0].private_keys == old.active.private_keys
        crypto.verify_key_record(saved.active.record, previous_record=old.active.record)
    assert _load(tmp_path) == saved


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
