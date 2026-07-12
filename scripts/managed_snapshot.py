#!/usr/bin/env python3
"""Generate or validate Brigade's bundled managed-station snapshot."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from brigade import localio, managed_snapshot  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="write a snapshot from explicit manifest paths")
    mode.add_argument("--check", action="store_true", help="validate the committed snapshot")
    parser.add_argument("manifests", nargs="*", type=Path)
    args = parser.parse_args()

    path = managed_snapshot.snapshot_path()
    if args.write:
        if not args.manifests:
            parser.error("--write requires at least one manifest path")
        payload = managed_snapshot.build_snapshot(args.manifests)
        localio.write_text_atomic(path, managed_snapshot.render_snapshot(payload))
        print(f"managed snapshot: wrote {path} ({len(payload['records'])} manifests)")
        return 0
    if args.manifests:
        parser.error("manifest paths are accepted only with --write")

    try:
        payload = managed_snapshot.load_snapshot(path)
    except ValueError as exc:
        print(f"managed snapshot: {exc}", file=sys.stderr)
        return 1
    expected = managed_snapshot.render_snapshot(payload)
    actual = path.read_text()
    if actual != expected:
        print(f"managed snapshot: non-canonical JSON at {path}", file=sys.stderr)
        return 1
    print(f"managed snapshot: ok ({len(payload['records'])} manifests)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
