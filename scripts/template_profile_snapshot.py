#!/usr/bin/env python3
"""Generate or validate Brigade's bundled template-profile render snapshot."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from brigade import localio, template_profiles  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="write the bundled template-profile snapshot")
    mode.add_argument("--check", action="store_true", help="validate the committed template-profile snapshot")
    args = parser.parse_args()

    path = template_profiles.snapshot_path()
    if args.write:
        payload = template_profiles.build_snapshot()
        localio.write_text_atomic(path, template_profiles.render_snapshot(payload))
        print(f"template profile snapshot: wrote {path} ({len(payload['profiles'])} profiles)")
        return 0

    try:
        payload = template_profiles.load_snapshot(path, verify_contracts=True)
    except ValueError as exc:
        print(f"template profile snapshot: {exc}", file=sys.stderr)
        return 1
    expected = template_profiles.render_snapshot(payload)
    actual = path.read_text(encoding="utf-8")
    if actual != expected:
        # Rebuild from builtins so check catches missing profiles / stale renders.
        rebuilt = template_profiles.render_snapshot(template_profiles.build_snapshot())
        if actual != rebuilt:
            print(f"template profile snapshot: drift at {path}; run with --write", file=sys.stderr)
            return 1
        print(f"template profile snapshot: non-canonical JSON at {path}", file=sys.stderr)
        return 1
    rebuilt = template_profiles.build_snapshot()
    if rebuilt["profiles"] != payload["profiles"] or rebuilt["renders"] != payload["renders"]:
        print(f"template profile snapshot: drift at {path}; run with --write", file=sys.stderr)
        return 1
    if rebuilt["brigade_version"] != payload["brigade_version"]:
        print(
            f"template profile snapshot: brigade_version drift "
            f"({payload['brigade_version']!r} != {rebuilt['brigade_version']!r})",
            file=sys.stderr,
        )
        return 1
    print(f"template profile snapshot: ok ({len(payload['profiles'])} profiles)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
