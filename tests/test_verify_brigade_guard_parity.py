"""Keep the verify-brigade guard parity drive honest.

`control-brigade.py guard-parity` drives the real `brigade guard scan` and
`brigade guard redact` on a fixture of lookbehind and backreference cases and
compares the CLI output to goldens pasted from main. It is the gate a regex
engine swap must pass, so these tests assert only on what the helper reports
from CLI output. Nothing here imports the guard engine or its rules.
"""

from __future__ import annotations

import shlex
import shutil
import sys
from pathlib import Path

import pytest

from tests.test_verify_brigade_skill import control

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is required to create a target")

# Fixture lines that must produce findings, with their counts, and lines where
# a lookaround or a mismatched backreference must stop every match. Line 14 is
# the recorded windows-home-path recall gap (see features/guard-parity.md).
POSITIVE_LINES = {2: 1, 4: 1, 6: 1, 8: 2, 10: 1, 12: 1, 13: 1, 16: 1, 17: 1, 18: 1}
NEGATIVE_LINES = (3, 5, 7, 9, 11, 14, 15, 19)
RULE_IDS = [
    "example-phone-555",
    "home-path",
    "localhost-bare",
    "private-hostname",
    "root-path",
    "us-phone",
    "windows-home-path",
]

# A stand-in `brigade` that forwards to the real CLI, then drifts the way a
# broken engine would: the scan loses its last finding and stdout redaction
# changes one replacement. `--in-place` is left alone, so the byte check passes.
DRIFTING_BRIGADE = """\
import json, subprocess, sys
argv = sys.argv[1:]
stdin = sys.stdin.read() if argv[:2] == ["guard", "redact"] and argv[-1].endswith("policy.json") else None
proc = subprocess.run([sys.executable, "-m", "brigade", *argv], capture_output=True, text=True, input=stdin)
out = proc.stdout
if argv[:2] == ["guard", "scan"]:
    payload = json.loads(out)
    payload["findings"].pop()
    out = json.dumps(payload)
elif argv[:2] == ["guard", "redact"] and "--in-place" not in argv:
    out = out.replace("[redacted-host]", "[redacted-hostname]")
sys.stdout.write(out)
sys.stderr.write(proc.stderr)
raise SystemExit(proc.returncode)
"""


def test_guard_parity_matches_the_goldens(tmp_path):
    root = tmp_path / "state"
    code, created = control("new-target", "--depth", "repo", "--harnesses", "claude", root=root)
    assert code == 0, created
    target = Path(created["target"])

    code, payload = control("guard-parity", "--target", str(target), root=root)
    assert code == 0, payload
    assert payload["ok"] is True
    assert payload["mismatches"] == []
    assert payload["rule_ids"] == RULE_IDS

    per_line: dict[int, int] = {}
    for line, *_ in payload["findings"]:
        per_line[line] = per_line.get(line, 0) + 1
    assert per_line == POSITIVE_LINES
    assert not set(NEGATIVE_LINES) & set(per_line), "a lookaround negative case produced a finding"

    for name in ("scan", "redact", "redact_stdin", "redact_in_place"):
        assert payload[name]["returncode"] == 0, payload[name]
    assert (target / "guard-parity" / "fixture.txt").is_file()

    code, removed = control("cleanup", root=root)
    assert code == 0, removed
    assert not target.exists()


def test_guard_parity_reports_a_drifted_engine(tmp_path):
    root = tmp_path / "state"
    target = root / "targets" / "drift"
    target.mkdir(parents=True)
    fake = tmp_path / "drifting_brigade.py"
    fake.write_text(DRIFTING_BRIGADE, encoding="utf-8")

    code, payload = control(
        "guard-parity", "--target", str(target), root=root, brigade=shlex.join([sys.executable, str(fake)])
    )
    assert code == 3, payload
    assert payload["ok"] is False
    by_check = {item["check"]: item for item in payload["mismatches"]}
    assert set(by_check) == {"scan findings", "redact stdout", "redact stdin stdout"}, by_check
    assert by_check["scan findings"]["missing"] == [[18, 7, 606, 614, "root-path", "redact"]]
    assert by_check["scan findings"]["unexpected"] == []
    assert "[redacted-hostname]" in by_check["redact stdout"]["observed"]


# A stand-in `brigade` that outlives the helper's per-call timeout.
SLEEPING_BRIGADE = """\
import time
time.sleep(30)
"""


def test_guard_parity_reports_a_timed_out_engine(tmp_path):
    root = tmp_path / "state"
    target = root / "targets" / "timeout"
    target.mkdir(parents=True)
    fake = tmp_path / "sleeping_brigade.py"
    fake.write_text(SLEEPING_BRIGADE, encoding="utf-8")

    code, payload = control(
        "--timeout",
        "1",
        "guard-parity",
        "--target",
        str(target),
        root=root,
        brigade=shlex.join([sys.executable, str(fake)]),
    )
    assert code == 3, payload
    assert payload["ok"] is False
    assert payload["scan"]["timed_out"] is True
    checks = {item["check"] for item in payload["mismatches"]}
    assert "scan exit code" in checks, checks
    assert "scan findings" in checks, checks


def test_guard_parity_dry_run_writes_nothing(tmp_path):
    root = tmp_path / "state"
    target = root / "targets" / "dry-run"
    target.mkdir(parents=True)

    code, payload = control("guard-parity", "--target", str(target), "--dry-run", root=root)
    assert code == 0, payload
    assert payload["dry_run"] is True
    assert len(payload["would_run"]) == 4
    assert list(target.iterdir()) == [], "a dry run must not write the fixture"


def test_guard_parity_refuses_a_target_outside_the_state_root(tmp_path):
    root = tmp_path / "state"
    stranger = tmp_path / "not-a-target"
    stranger.mkdir()

    code, payload = control("guard-parity", "--target", str(stranger), root=root)
    assert code == 1, payload
    assert payload["ok"] is False
    assert "refusing to use" in payload["error"], payload
    assert list(stranger.iterdir()) == []


def test_guard_parity_feature_is_registered():
    skill_dir = Path(__file__).resolve().parent.parent / "registry" / "skills" / "verify-brigade"
    features = (skill_dir / "features" / "README.md").read_text(encoding="utf-8")
    doc = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    assert "[guard-parity.md](guard-parity.md)" in features
    assert (skill_dir / "features" / "guard-parity.md").is_file()
    assert '$C guard-parity --target "$TARGET"' in doc
    feature = (skill_dir / "features" / "guard-parity.md").read_text(encoding="utf-8")
    for heading in (
        "## Sub-features",
        "## How to get to it (user POV)",
        "## Driving it with control-brigade",
        "## Gotchas",
    ):
        assert heading in feature, heading
