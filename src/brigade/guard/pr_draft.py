from __future__ import annotations

import argparse
import json
import sys

if sys.version_info >= (3, 11):
    from importlib.resources.abc import Traversable
else:  # pragma: no cover - 3.10 fallback
    from importlib.abc import Traversable
from os import PathLike
from pathlib import Path

from .engine import scan_text
from .policy import default_policy, load_policy
from .report import to_payload, to_text
from .types import ScanOptions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="content-guard-pr",
        description="Advisory PR draft guard that writes sanitized copy and report files.",
    )
    parser.add_argument("target", help="PR body markdown file")
    parser.add_argument("--policy", help="policy file, default: policies/pr-draft.json near project root")
    parser.add_argument("--output", help="sanitized output path, default: <target>.public.md")
    parser.add_argument("--report", help="JSON report path, default: <target>.content-guard.json")
    parser.add_argument("--strict", action="store_true", help="return nonzero when blocked findings exist")
    parser.add_argument("--opf", action="store_true", help="run optional OPF backend")
    parser.add_argument("--opf-bin", help="path to opf binary")
    parser.add_argument("--opf-device", help="OPF device, default comes from policy or cpu")
    args = parser.parse_args(argv)

    target = Path(args.target)
    text = target.read_text()
    policy_path = Path(args.policy) if args.policy else _default_policy_path()
    output_path = Path(args.output) if args.output else _default_output_path(target)
    report_path = Path(args.report) if args.report else _default_report_path(target)

    result = scan_text(
        text,
        policy=load_policy(policy_path),
        options=ScanOptions(
            include_opf=args.opf,
            opf_bin=args.opf_bin,
            opf_device=args.opf_device,
        ),
    )

    output_path.write_text(result.redacted_text)
    report_path.write_text(
        json.dumps({**to_payload(result), "source": str(target), "output": str(output_path)}, indent=2, sort_keys=True)
    )

    print(to_text(result, path=str(target)))
    print(f"sanitized={output_path}")
    print(f"report={report_path}")
    if result.blocked and not args.strict:
        print("advisory=true")

    return 1 if result.blocked and args.strict else 0


def _default_policy_path() -> Path | PathLike[str] | Traversable:
    return default_policy("pr-draft.json")


def _default_output_path(target: Path) -> Path:
    if target.suffix:
        return target.with_name(f"{target.stem}.public{target.suffix}")
    return target.with_name(f"{target.name}.public.md")


def _default_report_path(target: Path) -> Path:
    return target.with_name(f"{target.stem}.content-guard.json")


if __name__ == "__main__":
    raise SystemExit(main())
