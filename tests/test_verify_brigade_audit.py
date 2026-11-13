"""Keep the verify-brigade feature map in step with the CLI.

`control-brigade.py audit` compares the feature map (the status headers in
`features/*.md` and the known-gap list in `features/MAINTENANCE.md`) with the
live command tree the real CLI prints. These tests run the shipped map through
it, then run edited copies of the map to prove each failure is caught. The one
fake CLI here is a labeled stand-in: it forwards to the real CLI and adds a
single command, so the audit has a surface no feature covers.
"""

from __future__ import annotations

import shlex
import shutil
import sys
from pathlib import Path

import pytest

from tests.test_verify_brigade_skill import SKILL_DIR, control, sandbox

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is required by the shared helper fixtures")

FEATURES = SKILL_DIR / "features"
ACTIVE = {
    "hub-connector.md",
    "init-and-doctor.md",
    "work-verify-receipts.md",
    "guard-parity.md",
    "hub-contract.md",
    "storage-parity.md",
}
PAUSED = {"handoff-lint.md", "run-dry-run.md", "grokbot-pack-lifecycle.md", "run-cloud-grokbot-queue.md"}

# Stand-in CLI: the real one, plus a top-level `zz-fake-surface` in the
# completion tree. Everything else passes through untouched.
EXTRA_COMMAND_BRIGADE = """\
import subprocess, sys
argv = sys.argv[1:]
proc = subprocess.run([sys.executable, "-m", "brigade", *argv], capture_output=True, text=True)
out = proc.stdout
if argv == ["completions", "bash"]:
    out = out.replace('["brigade"]="', '["brigade"]="zz-fake-surface ', 1)
sys.stdout.write(out)
sys.stderr.write(proc.stderr)
raise SystemExit(proc.returncode)
"""


def audit(*extra: str, brigade: str | None = None, env_extra: dict[str, str] | None = None) -> tuple[int, dict]:
    return control("audit", *extra, root=None, brigade=brigade, env_extra=env_extra)


def map_copy(tmp_path: Path) -> Path:
    copy = tmp_path / "features"
    shutil.copytree(FEATURES, copy)
    return copy


def rewrite(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{old!r} not in {path.name}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def set_header(path: Path, key: str, value: str | None) -> None:
    """Replace one `key: value` header line, or drop it when `value` is None."""
    lines = path.read_text(encoding="utf-8").splitlines()
    index = next(i for i, line in enumerate(lines) if line.startswith(f"{key}: "))
    if value is None:
        del lines[index]
    else:
        lines[index] = f"{key}: {value}"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_shipped_map_passes_the_audit():
    code, payload = audit()
    assert code == 0, payload["failures"]
    assert payload["ok"] is True
    assert payload["failures"] == []
    assert payload["errors"] == []
    assert payload["broken"] == []
    assert payload["stale_gaps"] == []
    # Every uncovered command is a documented gap, not silent drift.
    assert payload["uncovered"], "the map does not cover the whole CLI, so gaps must be listed"
    assert all(item["known_gap"] and item["note"] for item in payload["uncovered"])
    assert payload["summary"]["uncovered_undocumented"] == 0

    statuses = {item["file"]: item["status"] for item in payload["features"]}
    assert {name for name, status in statuses.items() if status == "active"} == ACTIVE
    assert {name for name, status in statuses.items() if status == "paused"} == PAUSED
    paused = {item["feature"]: item for item in payload["paused"]}
    assert set(paused) == PAUSED
    assert all(item["reason"] and item["unpause"] for item in paused.values())
    assert paused["run-dry-run.md"]["unpause"] == "#2191"
    assert "hub memory v2" in paused["handoff-lint.md"]["reason"]


def test_audit_finds_an_uncovered_fake_surface(tmp_path):
    fake = tmp_path / "extra_command_brigade.py"
    fake.write_text(EXTRA_COMMAND_BRIGADE, encoding="utf-8")

    code, payload = audit(brigade=shlex.join([sys.executable, str(fake)]))
    assert code == 3, payload
    assert payload["ok"] is False
    by_surface = {item["surface"]: item for item in payload["uncovered"]}
    assert by_surface["zz-fake-surface"] == {"surface": "zz-fake-surface", "known_gap": False, "note": None}
    assert payload["failures"] == ["uncovered surface with no feature and no known gap: zz-fake-surface"]


def test_paused_feature_without_a_reason_fails_validation(tmp_path):
    features = map_copy(tmp_path)
    rewrite(features / "handoff-lint.md", "reason: the handoff format", "note: the handoff format")
    rewrite(features / "run-dry-run.md", "unpause: #2191\n", "")

    code, payload = audit("--features-dir", str(features))
    assert code == 3, payload
    assert "handoff-lint.md: a paused feature needs a reason" in payload["errors"]
    assert "handoff-lint.md: unknown header key 'note'" in payload["errors"]
    assert any(error.startswith("run-dry-run.md: a paused feature needs `unpause:") for error in payload["errors"])
    assert all(f"map error: {error}" in payload["failures"] for error in payload["errors"])


def test_paused_features_are_reported_not_silently_skipped(tmp_path):
    features = map_copy(tmp_path)
    rewrite(features / "guard-parity.md", "status: active", "status: paused")
    rewrite(
        features / "guard-parity.md", "surfaces: guard scan, guard redact", "surfaces: guard scan, guard no-such-verb"
    )
    rewrite(features / "guard-parity.md", "last-drive:", "reason: a test pause\nunpause: #1\nlast-drive:")

    code, payload = audit("--features-dir", str(features))
    assert code == 0, payload["failures"]
    paused = {item["feature"]: item for item in payload["paused"]}
    assert paused["guard-parity.md"] == {
        "feature": "guard-parity.md",
        "surfaces": ["guard scan", "guard no-such-verb"],
        "reason": "a test pause",
        "unpause": "#1",
    }
    # The pause skips the feature's own checks, so a missing verb is not broken,
    # and the paused surface is still claimed rather than reported uncovered.
    assert payload["broken"] == []
    assert "guard scan" not in {item["surface"] for item in payload["uncovered"]}
    assert payload["summary"]["paused"] == len(PAUSED) + 1


def test_an_active_feature_with_a_missing_command_is_broken(tmp_path):
    features = map_copy(tmp_path)
    rewrite(features / "work-verify-receipts.md", "surfaces: work verify run", "surfaces: work verify nope")

    code, payload = audit("--features-dir", str(features))
    assert code == 3, payload
    assert payload["broken"] == [
        {"feature": "work-verify-receipts.md", "surface": "work verify nope", "detail": "not in the CLI command tree"}
    ]
    assert (
        "broken surface: work-verify-receipts.md -> work verify nope (not in the CLI command tree)"
        in payload["failures"]
    )


def test_a_stand_in_proof_covers_nothing(tmp_path):
    features = map_copy(tmp_path)
    rewrite(features / "guard-parity.md", "proof: drive", "proof: stand-in")

    code, payload = audit("--features-dir", str(features))
    assert code == 3, payload
    assert payload["stand_ins"] == ["guard-parity.md"]
    # With its only drive relabeled as a stand-in, nothing covers `guard`, so the
    # whole command is uncovered and the `guard *` gap no longer matches anything.
    uncovered = {item["surface"]: item for item in payload["uncovered"]}
    assert uncovered["guard"] == {"surface": "guard", "known_gap": False, "note": None}
    assert payload["failures"] == [
        "uncovered surface with no feature and no known gap: guard",
        "stale known gap: guard * (matches no uncovered surface)",
    ]


def test_stale_gaps_and_unknown_drive_steps_fail(tmp_path):
    features = map_copy(tmp_path)
    rewrite(
        features / "MAINTENANCE.md",
        "```audit-gaps\n",
        "```audit-gaps\nzz-gone  removed long ago\nguard scan  covered\n",
    )
    rewrite(features / "init-and-doctor.md", "drive: new-target,", "drive: no-such-step, new-target,")

    code, payload = audit("--features-dir", str(features))
    assert code == 3, payload
    assert {"gap": "zz-gone", "reason": "not in the CLI command tree"} in payload["stale_gaps"]
    assert {"gap": "guard scan", "reason": "a feature covers it now"} in payload["stale_gaps"]
    assert "init-and-doctor.md: drive step 'no-such-step' is not a control-brigade.py subcommand" in payload["errors"]


def test_an_old_drive_is_reported_without_failing(tmp_path):
    features = map_copy(tmp_path)
    set_header(features / "work-verify-receipts.md", "last-drive", "2000-01-01 0000000")
    set_header(features / "guard-parity.md", "last-drive", None)

    code, payload = audit("--features-dir", str(features))
    assert code == 0, payload["failures"]
    stale = {item["feature"]: item for item in payload["stale"]}
    assert stale["work-verify-receipts.md"]["last_drive"]["date"] == "2000-01-01"
    assert stale["work-verify-receipts.md"]["reason"] == "older than 45 days"
    assert stale["guard-parity.md"] == {"feature": "guard-parity.md", "last_drive": None, "reason": "no drive recorded"}


def test_audit_writes_nothing(tmp_path):
    home, temp, env = sandbox(tmp_path)
    before = sorted(path.relative_to(FEATURES) for path in FEATURES.rglob("*"))

    code, payload = audit(env_extra=env)
    assert code == 0, payload["failures"]
    assert list(temp.iterdir()) == [], "audit must not create a state root"
    assert list(home.iterdir()) == []
    assert sorted(path.relative_to(FEATURES) for path in FEATURES.rglob("*")) == before
