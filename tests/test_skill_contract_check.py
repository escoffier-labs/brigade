"""The skill contract check and the ``skill-note`` manifest (#2589).

These tests use fixture text only. The live skill text is deliberately never checked here,
so a worse revision of the bundled skill merges in normal CI and fails attestation instead.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from brigade import verify_manifest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/skill_contract_check.py"
GOOD = """---
name: note
version: 1.0.0
---

## Process

1. Decide which mode applies.
2. Review the conversation.
3. Write the `.md` file to `~/notes/<topic-slug>.md`.
4. Sync to the configured Obsidian inbox.
5. Tell the user the exact file path and confirm the sync.

Every note MUST start with YAML frontmatter.

## Sync target

## Quality rules
"""


def load():
    spec = importlib.util.spec_from_file_location("skill_contract_check_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(skill_dir: Path):
    return subprocess.run([sys.executable, str(SCRIPT), str(skill_dir)], capture_output=True, text=True)


def test_fixture_text_with_every_required_step_passes(tmp_path):
    (tmp_path / "SKILL.md").write_text(GOOD)
    result = run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert load().missing_markers(GOOD) == []


@pytest.mark.parametrize(
    "dropped",
    [
        "4. Sync to the configured Obsidian inbox.\n",
        "5. Tell the user the exact file path and confirm the sync.\n",
        "## Quality rules\n",
    ],
)
def test_a_worse_revision_that_drops_a_required_step_fails(tmp_path, dropped):
    (tmp_path / "SKILL.md").write_text(GOOD.replace(dropped, ""))
    result = run(tmp_path)
    assert result.returncode == 1
    assert "missing" in result.stderr


def test_a_missing_skill_file_fails(tmp_path):
    result = run(tmp_path)
    assert result.returncode == 1 and "cannot read SKILL.md" in result.stderr


def test_the_skill_note_manifest_is_valid_and_scopes_the_skill_bundle_and_the_check():
    payload = json.loads((ROOT / "verify/manifests/skill-note.json").read_text())
    assert verify_manifest.validate_manifest_payload(payload) == []
    manifest = verify_manifest.manifest_from_payload(payload)
    assert (manifest.manifest_id, manifest.artifact_kind, manifest.artifact_id) == ("skill-note", "skill", "note")
    assert manifest.subject_path == "src/brigade/templates/skills/note/SKILL.md"
    for touched in (
        manifest.subject_path,
        "src/brigade/templates/skills/note/skill.json",
        "scripts/skill_contract_check.py",
    ):
        assert verify_manifest.path_in_manifest_scope(manifest, touched), touched
    # The ledger files and the recompute logic are outside the scope, so a ledger change never attests itself.
    for untouched in (
        "outcome-ledger/skills.json",
        "scripts/ledger_recompute.py",
        "tests/test_skill_contract_check.py",
    ):
        assert not verify_manifest.path_in_manifest_scope(manifest, untouched), untouched
    assert [check.command for check in manifest.checks] == [
        "python3 scripts/skill_contract_check.py src/brigade/templates/skills/note"
    ]
