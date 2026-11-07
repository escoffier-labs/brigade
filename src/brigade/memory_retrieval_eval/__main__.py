"""CLI entry: ``python -m brigade.memory_retrieval_eval``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .harness import DEFAULT_FIXTURE_ROOT, run_eval
from brigade.memory_hub_client import HubRefusal, HubUnavailable

from .hub import HubOptions, stable_digest
from .gates import evaluate_gate
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
        default="current,bm25,substring,grep,semantic",
        help="Comma-separated adapters to run (default: current,bm25,substring,grep,semantic).",
    )
    parser.add_argument("--json", action="store_true", help="Print the full JSON report.")
    parser.add_argument("--hub-url", help="Read-only fixture hub, with an explicit credential and scope.")
    parser.add_argument("--hub-token-file", type=Path)
    parser.add_argument("--hub-scope", default="fleet")
    parser.add_argument("--hub-binary", type=Path)
    parser.add_argument("--hub-memory-config", type=Path, help="Explicit memory TOML for a disposable candidate hub.")
    parser.add_argument("--candidate-embedder", help="Expected full embedder id, checked against every hybrid reply.")
    parser.add_argument(
        "--raw-ranking", action="store_true", help="Raw candidate order for R0 and before/after measurement."
    )
    parser.add_argument("--aggregate-only", action="store_true", help="Omit per-query data from reports.")
    parser.add_argument(
        "--gate", choices=("R0", "R1"), help="R1 checks the public half only, private model qualification is separate."
    )
    args = parser.parse_args(argv)

    adapter_names = [part.strip() for part in str(args.adapters).split(",") if part.strip()]
    try:
        if args.gate:
            adapter_names = ["bm25", "hub-lexical" if args.gate == "R0" else "hub-hybrid"]
        report = run_eval(
            fixture_root=args.fixture_root,
            k=args.k,
            adapters=adapter_names,
            hub_options=HubOptions(
                url=args.hub_url,
                token_file=args.hub_token_file,
                scope=args.hub_scope,
                binary=args.hub_binary,
                memory_config=args.hub_memory_config,
                raw=args.raw_ranking or args.gate == "R0",
                candidate_embedder=args.candidate_embedder,
            ),
        )
        if args.gate:
            report["gate"] = evaluate_gate(report, args.gate)
        if args.aggregate_only:
            for block in report["adapters"].values():
                block.pop("queries", None)
        report.pop("digest", None)
        report["digest"] = stable_digest(report)
    except (HubRefusal, HubUnavailable):
        print("error: hub eval request refused or unavailable", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        sys.stdout.write(format_json(report))
    else:
        sys.stdout.write(format_table(report))
    return 0 if not args.gate or report["gate"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
