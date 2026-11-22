#!/usr/bin/env python3
"""Check that a skill's text still carries the steps its contract requires (#2589).

The ``skill-note`` verify manifest runs this against the bundled ``note`` skill. It is
not a pytest test on purpose. The normal CI suite never runs it on the live skill
text, so a worse revision can merge. The ``outcome-attest`` run on the merge commit
fails it, which is the evidence the outcome ledger counts as a regression.

Usage: skill_contract_check.py [SKILL_DIR]   (default: src/brigade/templates/skills/note)
Exit 0 when every required marker is present, 1 and a list of misses otherwise.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

DEFAULT_DIR = "src/brigade/templates/skills/note"

# (label, regex that must match somewhere in SKILL.md)
REQUIRED = (
    ("frontmatter names the skill", r"(?m)\A---\nname: note\n"),
    ("process step: choose a mode", r"(?m)^1\. Decide which mode applies"),
    ("process step: write the file", r"(?m)^3\. Write the `\.md` file to `~/notes/"),
    ("process step: sync to the inbox", r"(?m)^4\. Sync to the configured Obsidian inbox"),
    ("process step: report the path", r"(?m)^5\. Tell the user the exact file path and confirm the sync"),
    ("frontmatter rule", r"Every note MUST start with YAML frontmatter"),
    ("sync target section", r"(?m)^## Sync target$"),
    ("quality rules section", r"(?m)^## Quality rules$"),
)


def missing_markers(text: str) -> list[str]:
    return [label for label, pattern in REQUIRED if re.search(pattern, text) is None]


def main(argv: list[str]) -> int:
    skill_dir = Path(argv[1] if len(argv) > 1 else DEFAULT_DIR)
    try:
        text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    except OSError as exc:
        print(f"skill contract: cannot read SKILL.md: {exc}", file=sys.stderr)
        return 1
    misses = missing_markers(text)
    for label in misses:
        print(f"skill contract: missing {label}", file=sys.stderr)
    if misses:
        return 1
    print(f"skill contract: ok ({len(REQUIRED)} markers)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
