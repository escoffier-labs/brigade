"""`brigade add <station>` - install and wire a station's managed tools."""

from __future__ import annotations

import sys
from pathlib import Path

from . import doctor as _doctor
from . import managed
from .install import DEFAULT_WIRED_SKILLS
from .registry import resolve as resolve_station


def run(target: Path, station: str) -> int:
    st = resolve_station(station)
    if st is None:
        print(f"error: unknown station {station!r}", file=sys.stderr)
        return 2

    tools = managed.for_station(st.name)
    if not tools:
        if st.name == "skills":
            print("station 'skills' ships built-in Brigade skills:")
            for skill_id in DEFAULT_WIRED_SKILLS:
                print(f"  [built-in] {skill_id}")
            print()
            print("Optional Skillet sidecar roster:")
            print("  npx skills add escoffier-labs/skillet")
            print("  npx skills add escoffier-labs/skillet --list")
            print()
            print("Run `brigade init --harnesses codex` to wire the built-in skills into Codex.")
            return 0
        print(f"station {st.name!r} has no managed tools to add.")
        return 0

    ctx = _doctor.build_context(target)
    rc = 0
    for tool in tools:
        if tool.detect():
            print(f"  [skip] {tool.name} already installed")
        else:
            print(f"  [install] {tool.name}: {' '.join(tool.install_args)}")
            r = managed.proc.run(tool.install_args, timeout=300)
            if r.code != 0:
                print(f"  [fail] {tool.name} install exited {r.code}: {r.stderr.strip()[:120]}", file=sys.stderr)
                rc = 1
                continue
        for status, name, detail in tool.wire(ctx):
            print(f"  [{status.lower()}] {name}: {detail}")
    print(f"\nRun `brigade doctor --target {target}` to verify.")
    return rc
