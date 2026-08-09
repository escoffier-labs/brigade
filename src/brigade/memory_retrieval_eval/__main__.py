"""CLI entry: ``python -m brigade.memory_retrieval_eval``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .harness import DEFAULT_FIXTURE_ROOT, run_eval
from .report import format_json, format_table


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m brigade.memory_retrieval_eval",
        description="Local memory-retrieval eval harness with a grep baseline (#722).",
    )
    parser.add_argument(
        "--fixture-root",
        type=Path,
        default=DEFAULT_FIXTURE_ROOT,
        help="Fixture directory containing memory/cards/ and queries.json.",
    )
    parser.add_argument("--k", type=int, default=None, help="Override Precision/Recall@K (default: queries.json k).")
    parser.add_argument(
        "--adapters",
        default="current,grep,semantic",
        help="Comma-separated adapters to run (default: current,grep,semantic).",
    )
    parser.add_argument("--json", action="store_true", help="Print the full JSON report.")
    args = parser.parse_args(argv)

    adapter_names = [part.strip() for part in str(args.adapters).split(",") if part.strip()]
    try:
        report = run_eval(fixture_root=args.fixture_root, k=args.k, adapters=adapter_names)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        sys.stdout.write(format_json(report))
    else:
        sys.stdout.write(format_table(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
