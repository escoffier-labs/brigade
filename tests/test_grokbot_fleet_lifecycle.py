"""Pack lifecycle, diagnostics, canary, and unit-hardening tests."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from brigade.grokbot_fleet.contracts import FleetError
from brigade.grokbot_fleet.lifecycle import (
    render_unit,
    validate_disjoint_state_paths,
)
from brigade import grokbot_ops, grokbot_packs
from tests.test_grokbot_ops import assert_listener_recovery_policy

SECRET = "not-a-real-token-value-32chars!!"


def _fleet_paths(tmp_path: Path) -> dict[str, Path]:
    runtime = tmp_path / "runtime.json"
    ledger = tmp_path / "ledger" / "ledger.json"
    actions = tmp_path / "actions"
    approvals = tmp_path / "approvals"
    runtime.write_text("{}", encoding="utf-8")
    ledger.parent.mkdir(mode=0o700)
    actions.mkdir(mode=0o700)
    approvals.mkdir(mode=0o700)
    os.chmod(ledger.parent, 0o700)
    os.chmod(actions, 0o700)
    os.chmod(approvals, 0o700)
    return {
        "runtime_path": runtime,
        "ledger_path": ledger,
        "action_state_path": actions,
        "approval_dir": approvals,
    }


def test_disjoint_paths_reject_overlap_dots_and_relative():
    with pytest.raises(FleetError) as caught:
        validate_disjoint_state_paths("/var/lib/a", "/var/lib/a/ledger.json", "/var/lib/b", "/var/lib/c")
    assert caught.value.code == "invalid_request"
    assert "/var/lib/a" not in str(caught.value)
    with pytest.raises(FleetError):
        validate_disjoint_state_paths("/var/lib/../a", "/var/lib/b", "/var/lib/c", "/var/lib/d")
    with pytest.raises(FleetError):
        validate_disjoint_state_paths("var/lib/a", "/var/lib/b", "/var/lib/c", "/var/lib/d")


def test_absolute_reference_accepts_posix_and_drive_roots_and_rejects_unc():
    from brigade.grokbot_fleet.lifecycle import validate_absolute_reference as validate_ref
    from brigade.grokbot_fleet.runtime_config import _required_absolute_path

    # A drive-less root such as "/var/lib/state" carries no drive on Windows and
    # resolves against the current drive, so it can alias C:\var\lib\state.
    if os.name == "nt":
        with pytest.raises(FleetError):
            validate_ref("/var/lib/state")
        with pytest.raises(FleetError):
            _required_absolute_path("/var/lib/state")
        for accepted in (r"C:\state\dir", "C:/state/dir"):
            assert isinstance(validate_ref(accepted), str)
            assert isinstance(_required_absolute_path(accepted), str)
    else:
        assert isinstance(validate_ref("/var/lib/state"), str)
        assert isinstance(_required_absolute_path("/var/lib/state"), str)
        for drive_path in (r"C:\state\dir", "C:/state/dir"):
            with pytest.raises(FleetError):
                validate_ref(drive_path)
            with pytest.raises(FleetError):
                _required_absolute_path(drive_path)
    for rejected in (
        r"\\server\share\path",
        r"\\.\pipe\x",
        r"\\?\C:\path",
        "//server/share",
        r"/\server/share",
        r"\/server/share",
        "relative/path",
        "/var/lib/../escape",
        r"C:\state\..\escape",
    ):
        with pytest.raises(FleetError):
            validate_ref(rejected)
        with pytest.raises(FleetError):
            _required_absolute_path(rejected)


def test_paths_overlap_detects_backslash_nesting(monkeypatch):
    import ntpath
    from types import SimpleNamespace

    from brigade.grokbot_fleet import runtime_config as runtime_mod

    with monkeypatch.context() as patched:
        patched.setattr(runtime_mod, "os", SimpleNamespace(name="nt", path=ntpath))
        assert runtime_mod.paths_overlap(r"C:\a", r"C:\a\b") is True
        assert runtime_mod.paths_overlap(r"C:\a\b", r"C:\a") is True


def test_paths_overlap_treats_backslash_as_literal_on_posix():
    from brigade.grokbot_fleet.runtime_config import paths_overlap

    if os.name != "posix":
        pytest.skip("backslash is a literal filename character only on POSIX")
    assert paths_overlap("/a/b\\c", "/a/b") is False


def test_paths_overlap_is_case_insensitive_on_windows(monkeypatch):
    import ntpath
    from types import SimpleNamespace

    from brigade.grokbot_fleet import runtime_config as runtime_mod

    with monkeypatch.context() as patched:
        patched.setattr(runtime_mod, "os", SimpleNamespace(name="nt", path=ntpath))
        assert runtime_mod.paths_overlap(r"C:\Brigade\State", r"c:\brigade") is True
        assert runtime_mod.paths_overlap(r"c:\brigade", r"C:\Brigade\State") is True


def test_paths_overlap_handles_root_operand_and_rejects_trailing_alias(monkeypatch):
    import ntpath
    from types import SimpleNamespace

    from brigade.grokbot_fleet import runtime_config as runtime_mod
    from brigade.grokbot_fleet import lifecycle as lifecycle_mod
    from brigade.grokbot_fleet.runtime_config import _required_absolute_path

    assert runtime_mod.paths_overlap("/", "/var/x") is True
    assert runtime_mod.paths_overlap("/var/x", "/") is True
    with monkeypatch.context() as patched:
        patched.setattr(runtime_mod, "os", SimpleNamespace(name="nt", path=ntpath))
        patched.setattr(lifecycle_mod, "os", SimpleNamespace(name="nt", path=ntpath))
        assert runtime_mod.paths_overlap("C:\\", r"C:\foo") is True
        assert runtime_mod.paths_overlap(r"C:\foo", "C:\\") is True
        assert runtime_mod.paths_overlap(r"C:\a", r"C:\a\b") is True
        assert isinstance(lifecycle_mod.validate_absolute_reference(r"C:\state"), str)
        assert isinstance(_required_absolute_path(r"C:\state"), str)
        for rejected in (r"C:\state.", r"C:\state "):
            with pytest.raises(FleetError):
                lifecycle_mod.validate_absolute_reference(rejected)
            with pytest.raises(FleetError):
                _required_absolute_path(rejected)
    for rejected in ("/var/lib/state.", "/var/lib/state "):
        with pytest.raises(FleetError):
            lifecycle_mod.validate_absolute_reference(rejected)
        with pytest.raises(FleetError):
            _required_absolute_path(rejected)
    assert isinstance(lifecycle_mod.validate_absolute_reference("/var/lib/my.dir/state"), str)
    assert isinstance(_required_absolute_path("/var/lib/my.dir/state"), str)


def test_disjoint_paths_reject_backslash_nested_drive_paths():
    with pytest.raises(FleetError):
        validate_disjoint_state_paths(
            r"C:\b\state\runtime.json",
            r"C:\b\other\ledger.json",
            r"C:\b\state",
            r"C:\b\approvals",
        )


def test_absolute_reference_rejects_non_ascii_drive_letter():
    from brigade.grokbot_fleet.lifecycle import validate_absolute_reference as validate_ref
    from brigade.grokbot_fleet.runtime_config import _required_absolute_path

    for rejected in ("Ｃ:\\state", "µ:/x"):
        with pytest.raises(FleetError):
            validate_ref(rejected)
        with pytest.raises(FleetError):
            _required_absolute_path(rejected)


def test_pack_setup_doctor_canary_and_unit_hide_secrets(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    paths = _fleet_paths(tmp_path)
    preview = grokbot_packs.preview_setup(
        tmp_path,
        "fleet-steward",
        bearer_env="TEST_GROKBOT_BEARER",
        **paths,
    )
    assert preview["apply"] is False
    assert not grokbot_packs.instance_config_path(tmp_path, "fleet-steward").exists()
    assert str(paths["runtime_path"]) not in json.dumps(preview)
    grokbot_packs.apply_setup(
        tmp_path,
        "fleet-steward",
        bearer_env="TEST_GROKBOT_BEARER",
        **paths,
    )
    checks = grokbot_packs.doctor(tmp_path, "fleet-steward")
    canary = grokbot_packs.canary(tmp_path, "fleet-steward")
    unit = grokbot_packs.render_install_service(tmp_path, "fleet-steward")
    sanitized = json.dumps({"checks": checks, "canary": canary})
    assert SECRET not in sanitized
    assert str(paths["runtime_path"]) not in sanitized
    assert SECRET not in unit
    assert str(paths["runtime_path"]) not in unit
    assert "--pack" in unit
    assert "fleet-steward" in unit
    assert "127.0.0.1:8771" in unit
    assert "NoNewPrivileges=yes" in unit
    assert "KillMode=mixed" in unit
    assert "StandardOutput=journal" in unit
    assert "ProtectSystem=strict" in unit
    assert canary["ok"] is False
    assert all(set(check) == {"check", "status"} for check in checks)
    unit_dir = tmp_path / "units"
    installed = grokbot_packs.apply_install_service(tmp_path, "fleet-steward", out_dir=unit_dir)
    assert installed["unit"] == grokbot_ops.unit_name("fleet-steward")
    grokbot_packs.apply_remove(tmp_path, "fleet-steward", unit_dir=unit_dir)
    assert not grokbot_packs.instance_config_path(tmp_path, "fleet-steward").exists()


def test_unit_rendering_does_not_resolve_secret_values(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    paths = _fleet_paths(tmp_path)
    grokbot_packs.apply_setup(
        tmp_path,
        "fleet-steward",
        bearer_env="TEST_GROKBOT_BEARER",
        **paths,
    )
    unit = render_unit(tmp_path)
    assert SECRET not in unit
    assert "--bearer-env TEST_GROKBOT_BEARER" in unit or "--bearer-env" in unit
    read_write = unit.split("ReadWritePaths=", 1)[-1]
    assert str(paths["approval_dir"]) not in read_write
    assert str(paths["runtime_path"]) not in unit


def test_successful_canary_does_not_mutate_ledger_findings_or_action_state(tmp_path: Path, monkeypatch):
    from brigade.grokbot_fleet import lifecycle as lifecycle_mod
    from brigade.grokbot_fleet.contracts import TOOLS
    from brigade.grokbot_fleet.ledger import FleetLedger

    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    paths = _fleet_paths(tmp_path)
    grokbot_packs.apply_setup(
        tmp_path,
        "fleet-steward",
        bearer_env="TEST_GROKBOT_BEARER",
        **paths,
    )
    ledger = FleetLedger(str(paths["ledger_path"]))
    finding = {
        "finding_id": "control-plane:unreachable",
        "target_alias": "control-plane",
        "proposed_action_id": "inspect-host",
        "reason": "Host is unreachable",
        "blast_radius": "one registered host",
        "verification_id": "verify-host-reachability",
        "rollback_id": "no-rollback",
    }
    ledger.replace_findings("control-plane", [finding])
    seeded = Path(paths["action_state_path"]) / "proposals" / "seed.json"
    seeded.parent.mkdir(mode=0o700)
    os.chmod(seeded.parent, 0o700)
    seeded.write_text('{"seed":true}', encoding="utf-8")
    os.chmod(seeded, 0o600)

    def snapshot() -> tuple[bytes, object, dict[str, bytes | str]]:
        ledger_bytes = paths["ledger_path"].read_bytes()
        findings = json.loads(ledger_bytes)["findings"]
        tree: dict[str, bytes | str] = {}
        root = Path(paths["action_state_path"])
        for item in sorted(root.rglob("*")):
            relative = str(item.relative_to(root))
            tree[relative] = item.read_bytes() if item.is_file() else "dir"
        return ledger_bytes, findings, tree

    before = snapshot()

    monkeypatch.setattr(
        grokbot_ops,
        "_request_json",
        lambda *_args, **_kwargs: {"ok": True, "service": "grokbot-fleet-steward"},
    )
    monkeypatch.setattr(grokbot_ops, "_anonymous_health_status", lambda *_args, **_kwargs: 401)
    monkeypatch.setattr(grokbot_ops, "_tools_list", lambda *_args, **_kwargs: [{"name": name} for name in TOOLS])

    result = lifecycle_mod.canary(tmp_path)
    assert result["ok"] is True
    assert result["auth_rejected_without_bearer"] is True
    assert set(result["tools"]) == set(TOOLS)
    assert snapshot() == before
    assert "execute_remediation" not in result
    assert ledger.finding("control-plane:unreachable")["proposed_action_id"] == "inspect-host"


def test_runtime_and_state_paths_reject_symlinks(tmp_path: Path):
    from brigade.grokbot_fleet.lifecycle import validate_runtime_file, validate_state_directory

    real = tmp_path / "runtime.json"
    real.write_text("{}", encoding="utf-8")
    link = tmp_path / "runtime.link"
    link.symlink_to(real)
    with pytest.raises(FleetError) as caught:
        validate_runtime_file(str(link))
    assert caught.value.code == "invalid_request"
    assert str(link) not in str(caught.value)
    directory = tmp_path / "actions"
    directory.mkdir(mode=0o700)
    os.chmod(directory, 0o700)
    linked_dir = tmp_path / "actions.link"
    linked_dir.symlink_to(directory)
    with pytest.raises(FleetError):
        validate_state_directory(str(linked_dir), must_exist=True)


def test_fleet_unit_uses_shared_listener_recovery_policy(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    grokbot_packs.apply_setup(
        tmp_path,
        "fleet-steward",
        bearer_env="TEST_GROKBOT_BEARER",
        **_fleet_paths(tmp_path),
    )
    assert_listener_recovery_policy(render_unit(tmp_path))


def test_windows_branch_rejects_drive_less_roots(monkeypatch):
    """The Windows drive-root gate must be exercised on POSIX hosts too.

    A drive-less root such as "/var/lib/state" resolves against the current
    drive on Windows, so it can name the same tree as C:\\var\\lib\\state while
    comparing disjoint. The platform decision is read from each module's own
    ``os`` binding, which is the seam patched here.
    """
    import ntpath
    from types import SimpleNamespace

    from brigade.grokbot_fleet import lifecycle as lifecycle_mod
    from brigade.grokbot_fleet import runtime_config as runtime_mod

    windows = SimpleNamespace(name="nt", path=ntpath)
    drive_less = ("/var/lib/state", "/var/lib/state/ledger.json", "/", "\\var\\lib\\state")
    with monkeypatch.context() as patched:
        patched.setattr(lifecycle_mod, "os", windows)
        patched.setattr(runtime_mod, "os", windows)

        # Proof the simulated branch is live: a drive root is accepted only here.
        assert isinstance(lifecycle_mod.validate_absolute_reference(r"C:\var\lib\state"), str)
        assert isinstance(runtime_mod._required_absolute_path(r"C:\var\lib\state"), str)

        for rejected in drive_less:
            with pytest.raises(FleetError):
                lifecycle_mod.validate_absolute_reference(rejected)
            with pytest.raises(FleetError):
                runtime_mod._required_absolute_path(rejected)

    # Outside the simulation the POSIX contract is unchanged.
    assert isinstance(lifecycle_mod.validate_absolute_reference("/var/lib/state"), str)
    with pytest.raises(FleetError):
        lifecycle_mod.validate_absolute_reference(r"C:\var\lib\state")


def test_windows_disjoint_paths_reject_drive_less_alias_of_drive_path(monkeypatch):
    """A drive-less path and a drive path can name one tree yet compare disjoint.

    ``paths_overlap`` cannot see that aliasing, so the validator has to refuse
    the drive-less operand before the disjoint check ever runs.
    """
    import ntpath
    from types import SimpleNamespace

    from brigade.grokbot_fleet import lifecycle as lifecycle_mod
    from brigade.grokbot_fleet import runtime_config as runtime_mod

    windows = SimpleNamespace(name="nt", path=ntpath)
    with monkeypatch.context() as patched:
        patched.setattr(lifecycle_mod, "os", windows)
        patched.setattr(runtime_mod, "os", windows)
        # The pair normalizes to two different strings even though Windows
        # resolves both against the same drive.
        assert runtime_mod.paths_overlap("/var/lib/state", r"C:\var\lib\state") is False
        with pytest.raises(FleetError):
            lifecycle_mod.validate_disjoint_state_paths(
                "/var/lib/state",
                r"C:\var\lib\state\ledger.json",
                r"C:\var\lib\actions",
                r"C:\var\lib\approvals",
            )
        with pytest.raises(FleetError):
            runtime_mod.load_fleet_action_paths_env(
                {
                    "GROKBOT_FLEET_ACTION_STATE_PATH": "/var/lib/state",
                    "GROKBOT_FLEET_APPROVAL_DIR": r"C:\var\lib\state",
                }
            )
        # An all-drive-rooted overlapping pair is still caught by paths_overlap.
        with pytest.raises(FleetError):
            runtime_mod.load_fleet_action_paths_env(
                {
                    "GROKBOT_FLEET_ACTION_STATE_PATH": r"C:\var\lib\state",
                    "GROKBOT_FLEET_APPROVAL_DIR": r"C:\var\lib\state\approvals",
                }
            )
        # A genuinely disjoint drive-rooted pair still loads.
        assert runtime_mod.load_fleet_action_paths_env(
            {
                "GROKBOT_FLEET_ACTION_STATE_PATH": r"C:\var\lib\actions",
                "GROKBOT_FLEET_APPROVAL_DIR": r"C:\var\lib\approvals",
            }
        ) == {"action_state_path": r"C:\var\lib\actions", "approval_dir": r"C:\var\lib\approvals"}
    # POSIX still accepts the drive-less pair and still rejects a real overlap.
    assert (
        lifecycle_mod.validate_disjoint_state_paths(
            "/var/lib/runtime.json", "/var/lib/ledger.json", "/var/lib/actions", "/var/lib/approvals"
        )["approval_dir"]
        == "/var/lib/approvals"
    )


def test_fleet_private_reads_fail_closed_with_the_read_error(monkeypatch, tmp_path: Path):
    """Read APIs must raise secure-owner-read-unavailable before touching the filesystem."""
    from brigade.grokbot_fleet import actions as actions_mod

    present = tmp_path / "state.json"
    present.write_text(json.dumps({"k": "v"}), encoding="utf-8")
    os.chmod(present, 0o600)
    absent = tmp_path / "absent.json"

    # POSIX behavior is unchanged.
    assert actions_mod._read_safe_json(present) == {"k": "v"}
    with pytest.raises(FileNotFoundError):
        actions_mod._read_safe_json(absent)
    assert actions_mod._list_json_files(tmp_path) == [present]

    with monkeypatch.context() as patched:
        patched.setattr(actions_mod, "SECURE_OWNER_READ_AVAILABLE", False)
        patched.setattr(actions_mod, "SECURE_OWNER_WRITE_AVAILABLE", False)
        for target in (present, absent):
            with pytest.raises(FleetError) as caught:
                actions_mod._read_safe_json(target)
            # A FileNotFoundError here would mean the gate ran after os.open.
            assert caught.value.code == "unavailable"
            assert caught.value.message == "secure-owner-read-unavailable"
        with pytest.raises(FleetError) as listed:
            actions_mod._list_json_files(tmp_path)
        assert listed.value.message == "secure-owner-read-unavailable"
