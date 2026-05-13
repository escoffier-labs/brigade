"""`solo-mise openclaw-fragments` / `hermes-fragments` — write config fragments.

These never mutate a live config. They drop JSON fragments into the chosen
output directory so the user can `jq -s '.[0] * .[1]'` them in by hand.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

from .templates import template_root


HARNESS_FILES = {
    "openclaw": [
        "model-aliases.openclaw.json",
        "ollama-memory-search.openclaw.json",
        "acp-escalation.openclaw.json",
        "README.md",
    ],
    "hermes": [
        "workspace.harness.json",
        "memory-handoff.harness.json",
        "model-lanes.harness.json",
        "README.md",
    ],
}


def write_fragments(out: Path, harness: str) -> int:
    if harness not in HARNESS_FILES:
        print(f"solo-mise: unknown harness: {harness}", file=sys.stderr)
        return 2

    out = out.expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)

    src_dir = template_root() / harness
    for name in HARNESS_FILES[harness]:
        src = src_dir / name
        if not src.is_file():
            print(f"solo-mise: template missing: {src}", file=sys.stderr)
            return 3
        dest = out / name
        shutil.copyfile(src, dest)

    print(f"solo-mise: wrote {harness} fragments to {out}")
    print()
    print("Next steps:")
    print(f"  - inspect each fragment under {out}")
    if harness == "openclaw":
        print(
            "  - merge with: jq -s '.[0] * .[1]' ~/.openclaw/openclaw.json "
            f"{out}/<fragment>.json > /tmp/merged.json"
        )
        print(
            "  - verify with: solo-mise doctor --target ~/.openclaw/workspace --harness openclaw"
        )
    elif harness == "hermes":
        print(
            "  - the Hermes adapter is experimental; validate against your real Hermes install"
        )
    return 0
