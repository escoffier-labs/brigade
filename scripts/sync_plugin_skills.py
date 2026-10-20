#!/usr/bin/env python3
"""Check the agent plugin's copies of canonical Brigade skills; --write syncs them.

With no flags the script exits 1 and names each plugin copy that differs from
its canonical source. The plugin directory is a build output for these files,
never a place to edit them. Edit src/brigade/templates/skills/<name>/SKILL.md,
then run with --write.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SYNCED = [
    ("src/brigade/templates/skills/brigade-work/SKILL.md", "plugins/brigade/skills/brigade-work/SKILL.md"),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="rewrite plugin copies from the canonical sources")
    args = parser.parse_args()
    drift = []
    for source_rel, copy_rel in SYNCED:
        source = (ROOT / source_rel).read_bytes()
        copy = ROOT / copy_rel
        if copy.is_file() and copy.read_bytes() == source:
            continue
        if args.write:
            copy.parent.mkdir(parents=True, exist_ok=True)
            copy.write_bytes(source)
            print(f"wrote {copy_rel}")
        else:
            drift.append(copy_rel)
    for rel in drift:
        print(f"plugin skill drift: {rel} (run scripts/sync_plugin_skills.py --write)", file=sys.stderr)
    return 1 if drift else 0


if __name__ == "__main__":
    raise SystemExit(main())
