"""Command-line JSON renderer for the issue #845 memory quality eval."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .quality import DEFAULT_QUALITY_ROOT, evaluate_memory_quality


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only memory quality evaluation (#845).")
    parser.add_argument("--root", type=Path, default=DEFAULT_QUALITY_ROOT)
    args = parser.parse_args(argv)
    print(json.dumps(evaluate_memory_quality(args.root), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
