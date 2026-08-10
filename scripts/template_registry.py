#!/usr/bin/env python3
"""Generate or validate Brigade's bundled template-profile registry."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from brigade import localio, template_registry  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="write the bundled template registry")
    mode.add_argument("--check", action="store_true", help="validate the committed template registry")
    args = parser.parse_args()

    path = template_registry.registry_path()
    if args.write:
        payload = template_registry.build_registry()
        localio.write_text_atomic(path, template_registry.render_registry(payload))
        print(f"template registry: wrote {path} ({len(payload['profiles'])} profiles)")
        return 0

    try:
        payload = template_registry.load_registry(path)
    except ValueError as exc:
        print(f"template registry: {exc}", file=sys.stderr)
        return 1
    expected = template_registry.render_registry(payload)
    actual = path.read_text(encoding="utf-8")
    if actual != expected:
        print(f"template registry: non-canonical JSON at {path}", file=sys.stderr)
        return 1
    print(f"template registry: ok ({len(payload['profiles'])} profiles)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
