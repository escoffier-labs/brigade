"""Keep the verify-brigade lever honest.

The `registry/skills/verify-brigade/` skill exists so a change to the Brigade
CLI can be proved by driving the CLI. Its helper is only useful if it still
runs, so CI drives the commands a proof run starts with (new-target,
doctor-target, work-verify, cleanup) and the refusals that keep those drives
away from the operator home and the Brigade checkout.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO_ROOT / "registry" / "skills" / "verify-brigade"
CONTROL = SKILL_DIR / "control-brigade.py"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is required to create a target")


def control(
    *args: str,
    root: Path,
    brigade: str | None = None,
    env_extra: dict[str, str] | None = None,
) -> tuple[int, dict]:
    """Run one control-brigade subcommand and return (exit code, parsed JSON)."""
    brigade = brigade or shlex.join([sys.executable, "-m", "brigade"])
    argv = [sys.executable, str(CONTROL), "--brigade", brigade, "--root", str(root), *args]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO_ROOT / "src"), env.get("PYTHONPATH", "")]))
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=600, env=env, check=False)
    assert proc.stdout, f"no JSON on stdout: {proc.stderr}"
    return proc.returncode, json.loads(proc.stdout)


def sandbox(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    """A fake operator home and temp base for the helper subprocess.

    The helper waves through any path under `$TMPDIR` before it measures a path
    against the operator home. An autouse fixture in `conftest.py` repoints
    `HOME` at a `tmp_path_factory` directory, which lives *inside* the temp
    tree - so a probe under that home is allowed by the `$TMPDIR` clause and
    the home refusal never runs. Hand the subprocess a home and a temp base
    that are siblings instead, so "inside the operator home" is a state these
    tests can actually reach, on any host and in CI.
    """
    home = tmp_path / "sandbox-home"
    temp = tmp_path / "sandbox-tmp"
    home.mkdir()
    temp.mkdir()
    return home, temp, {"HOME": str(home), "TMPDIR": str(temp)}


def test_control_brigade_is_an_executable_helper():
    assert CONTROL.is_file(), "the verify-brigade skill must ship its helper"
    assert os.access(CONTROL, os.X_OK), "control-brigade.py must be executable"
    assert (SKILL_DIR / "SKILL.md").is_file()
    assert (SKILL_DIR / "features" / "README.md").is_file()


def test_new_target_doctor_target_and_cleanup(tmp_path):
    root = tmp_path / "state"

    code, created = control("new-target", "--depth", "repo", "--harnesses", "claude", root=root)
    assert code == 0, created
    assert created["ok"] is True
    assert created["action"] == "new-target"
    target = Path(created["target"])
    assert target.is_dir()
    assert target.is_relative_to(root / "targets")
    assert (target / ".brigade").is_dir()

    code, health = control("doctor-target", "--target", str(target), root=root)
    assert code == 0, health
    assert health["ok"] is True
    assert health["ready"] is True
    assert health["summary"]["failed"] == 0
    assert health["summary"]["total"] > 0

    code, preview = control("cleanup", "--dry-run", root=root)
    assert code == 0, preview
    assert preview["dry_run"] is True
    assert preview["would_remove"] == [str(target)]
    assert target.is_dir(), "a dry run must not remove anything"

    code, removed = control("cleanup", root=root)
    assert code == 0, removed
    assert removed["ok"] is True
    assert removed["removed"] == [str(target)]
    assert not target.exists()


def test_cleanup_refuses_a_target_it_did_not_create(tmp_path):
    root = tmp_path / "state"
    stranger = tmp_path / "not-ours"
    stranger.mkdir()

    code, payload = control("cleanup", "--target", str(stranger), root=root)
    assert code == 1
    assert payload["ok"] is False
    assert "refusing to remove" in payload["error"]
    assert stranger.is_dir()


def test_work_verify_returns_a_receipt(tmp_path):
    root = tmp_path / "state"

    code, created = control("new-target", "--depth", "repo", "--harnesses", "claude", root=root)
    assert code == 0, created
    target = Path(created["target"])

    code, verified = control("work-verify", "--target", str(target), "--command", "python3 --version", root=root)
    assert code == 0, verified
    assert verified["ok"] is True
    assert verified["status"] == "completed"
    assert verified["run_id"], "a work-verify drive must name the receipt it produced"
    receipt = target / ".brigade" / "work" / "verify-runs" / verified["run_id"]
    assert receipt.is_dir(), f"receipt directory missing: {receipt}"
    assert Path(verified["receipt_path"]) == receipt


@pytest.mark.parametrize(
    ("unsafe", "detail"),
    [
        ("home", "inside the operator home"),
        ("under-home", "inside the operator home"),
        ("repo-root", "inside the Brigade checkout"),
        ("under-repo-root", "inside the Brigade checkout"),
        ("outside-state-root", "outside the state root"),
    ],
)
def test_guard_target_refuses_unsafe_targets(tmp_path, unsafe, detail):
    home, temp, env_extra = sandbox(tmp_path)
    root = temp / "state"
    candidates = {
        "home": home,
        "under-home": home / "verify-brigade-refusal-probe",
        "repo-root": REPO_ROOT,
        "under-repo-root": REPO_ROOT / "src",
        "outside-state-root": tmp_path / "not-a-target",
    }

    code, payload = control("doctor-target", "--target", str(candidates[unsafe]), root=root, env_extra=env_extra)
    assert code == 1, payload
    assert payload["ok"] is False
    assert payload["action"] == "doctor-target"
    assert "refusing" in payload["error"], payload
    assert detail in payload["error"], payload


def test_guard_target_accepts_a_path_under_the_state_root(tmp_path):
    root = tmp_path / "state"
    target = root / "targets" / "hand-made"
    target.mkdir(parents=True)

    _, payload = control("doctor-target", "--target", str(target), root=root)
    assert payload["action"] == "doctor-target"
    assert "error" not in payload, payload
    assert payload["target"] == str(target)


def test_root_inside_the_operator_home_is_refused(tmp_path):
    home, _temp, env_extra = sandbox(tmp_path)
    unsafe_root = home / "verify-brigade-refusal-probe"

    code, payload = control("doctor", root=unsafe_root, env_extra=env_extra)
    assert code == 1, payload
    assert payload["ok"] is False
    assert payload["action"] == "doctor"
    assert "refusing --root" in payload["error"], payload
    assert "inside the operator home" in payload["error"], payload
    assert not unsafe_root.exists(), "a refused --root must not be created"


def test_root_inside_the_brigade_checkout_is_refused(tmp_path):
    _home, _temp, env_extra = sandbox(tmp_path)
    unsafe_root = REPO_ROOT / "verify-brigade-refusal-probe"

    code, payload = control("doctor", root=unsafe_root, env_extra=env_extra)
    assert code == 1, payload
    assert payload["ok"] is False
    assert payload["action"] == "doctor"
    assert "refusing --root" in payload["error"], payload
    assert "inside the Brigade checkout" in payload["error"], payload
    assert not unsafe_root.exists(), "a refused --root must not be created"


def test_failed_brigade_init_leaves_no_target(tmp_path):
    root = tmp_path / "state"
    failing = shlex.join([sys.executable, "-c", "raise SystemExit(1)"])

    code, created = control("new-target", root=root, brigade=failing)
    assert code == 3, created
    assert created["ok"] is False
    assert created["stage"] == "brigade-init"
    target = Path(created["target"])
    assert not target.exists(), "a failed init must not leave a directory behind"

    code, removed = control("cleanup", "--target", str(target), root=root)
    assert code == 0, removed
    assert removed["ok"] is True
    assert removed["skipped"] == [{"path": str(target), "reason": "already gone"}]


def test_init_dry_run_writes_nothing(tmp_path):
    root = tmp_path / "state"
    target = root / "targets" / "dry-run"
    target.mkdir(parents=True)

    code, payload = control("init", "--target", str(target), "--dry-run", root=root)
    assert code == 0, payload
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    assert payload["would_run"], "a dry run must show the command it skipped"
    assert list(target.iterdir()) == [], "a dry run must not write into the target"


def test_new_target_dry_run_creates_nothing(tmp_path):
    root = tmp_path / "state"

    code, payload = control("new-target", "--dry-run", root=root)
    assert code == 0, payload
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    assert payload["would_run"], "a dry run must show the commands it skipped"
    assert list((root / "targets").iterdir()) == [], "a dry run must not create a target"
    state = json.loads((root / "state.json").read_text()) if (root / "state.json").exists() else {}
    assert state.get("created_targets", []) == [], "a dry run must not record a target"


def test_work_verify_dry_run_writes_no_receipt(tmp_path):
    root = tmp_path / "state"
    target = root / "targets" / "dry-run"
    target.mkdir(parents=True)

    code, payload = control(
        "work-verify", "--target", str(target), "--command", "python3 --version", "--dry-run", root=root
    )
    assert code == 0, payload
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    assert payload["would_run"], "a dry run must show the command it skipped"
    assert list(target.iterdir()) == [], "a dry run must not write a receipt"


def test_evidence_dry_run_creates_no_directory(tmp_path):
    root = tmp_path / "state"
    evidence_root = tmp_path / "evidence"

    # Dry run first: the stamp has one-second resolution, so a real run in the
    # same second would occupy the directory name the preview names.
    code, preview = control(
        "evidence", "--evidence-root", str(evidence_root), "--label", "probe", "--dry-run", root=root
    )
    assert code == 0, preview
    assert preview["ok"] is True
    assert preview["dry_run"] is True
    assert not evidence_root.exists(), "a dry run must not create an evidence dir"

    code, payload = control("evidence", "--evidence-root", str(evidence_root), "--label", "probe", root=root)
    assert code == 0, payload
    assert payload["dry_run"] is False
    assert Path(payload["evidence_dir"]).is_dir()
    assert (Path(payload["evidence_dir"]) / "manifest.json").is_file()


def test_evidence_root_inside_the_operator_home_is_refused(tmp_path):
    home, temp, env_extra = sandbox(tmp_path)
    root = temp / "state"
    unsafe = home / "verify-brigade-evidence-probe"

    code, payload = control("evidence", "--evidence-root", str(unsafe), root=root, env_extra=env_extra)
    assert code == 1, payload
    assert payload["ok"] is False
    assert payload["action"] == "evidence"
    assert "refusing --evidence-root" in payload["error"], payload
    assert "inside the operator home" in payload["error"], payload
    assert not unsafe.exists(), "a refused --evidence-root must not be created"


def test_state_root_and_captures_are_private(tmp_path):
    root = tmp_path / "state"

    code, payload = control("doctor", root=root)
    assert code in {0, 3}, payload
    assert stat.S_IMODE((root).stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "captures").stat().st_mode) == 0o700
    captures = sorted((root / "captures").glob("*.json"))
    assert captures, "doctor must capture the commands it ran"
    for capture in captures:
        assert stat.S_IMODE(capture.stat().st_mode) == 0o600, capture
