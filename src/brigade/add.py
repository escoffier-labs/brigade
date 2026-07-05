"""`brigade add <station>` - install and wire a station's managed tools."""

from __future__ import annotations

import sys
from pathlib import Path

from . import doctor as _doctor
from . import managed
from .install import DEFAULT_WIRED_SKILLS
from .registry import resolve as resolve_station
from . import station_manifest


def run(target: Path, station: str, *, install_manifest: bool = False) -> int:
    manifest_path = station_manifest.manifest_path(station)
    if manifest_path is not None:
        return _run_manifest_add(station, install_manifest=install_manifest)

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
            print("Optional Skillet sidecar roster, after installing the sidecar CLI:")
            print("  skills add escoffier-labs/skillet")
            print("  skills add escoffier-labs/skillet --list")
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


def _run_manifest_add(ref: str, *, install_manifest: bool) -> int:
    try:
        manifest = station_manifest.load(ref)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"station manifest: {manifest.name} ({manifest.station})")
    print(f"  summary: {manifest.summary}")
    print(f"  path: {manifest.path}")
    rc = 0
    for tool in manifest.tools:
        installed = managed.proc.which(tool.command) is not None
        marker = "installed" if installed else "missing"
        print(f"  [{marker}] {tool.name}: {tool.summary or tool.command}")
        if tool.install:
            if install_manifest and not installed:
                print(f"    install: {' '.join(tool.install)}")
                result = managed.proc.run(list(tool.install), timeout=300)
                if result.code != 0:
                    detail = result.stderr.strip()[:120] or result.stdout.strip()[:120]
                    print(f"    [fail] install exited {result.code}: {detail}", file=sys.stderr)
                    rc = 1
            else:
                print(f"    install: {' '.join(tool.install)}")
        for surface in tool.surfaces:
            extras = []
            if surface.timeout_seconds is not None:
                extras.append(f"timeout={surface.timeout_seconds:g}s")
            if surface.max_chars is not None:
                extras.append(f"max_chars={surface.max_chars}")
            suffix = f" ({', '.join(extras)})" if extras else ""
            print(f"    surface[{surface.kind}]: {' '.join(surface.command)}{suffix}")
    if not install_manifest:
        print("\nManifest install commands were not run. Re-run with `--install` to execute them.")
    return rc
