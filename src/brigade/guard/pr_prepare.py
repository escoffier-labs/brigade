from __future__ import annotations

import argparse
import json
import re
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
from .types import GuardResult, ScanOptions


def prepare_pr_body(
    text: str,
    *,
    source: Path | None,
    policy_path: Path | PathLike[str] | Traversable,
    out_dir: Path,
    name: str,
    strict: bool = False,
    include_opf: bool = False,
    opf_bin: str | None = None,
    opf_device: str | None = None,
) -> tuple[dict, GuardResult]:
    safe_name = _safe_name(name)
    draft_path = out_dir / f"{safe_name}.draft.md"
    public_path = out_dir / f"{safe_name}.public.md"
    report_path = out_dir / f"{safe_name}.content-guard.json"

    result = scan_text(
        text,
        policy=load_policy(policy_path),
        options=ScanOptions(
            include_opf=include_opf,
            opf_bin=opf_bin,
            opf_device=opf_device,
        ),
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    draft_path.write_text(text)
    public_path.write_text(result.redacted_text)

    payload = {
        **to_payload(result),
        "source": str(source) if source else "<stdin>",
        "draft": str(draft_path),
        "sanitized": str(public_path),
        "report": str(report_path),
        "publish_body_file": str(public_path),
        "advisory": bool(result.blocked and not strict),
        "strict": bool(strict),
    }
    report_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    return payload, result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="content-guard-pr-prepare",
        description="Prepare a guarded PR body bundle for later publishing.",
    )
    parser.add_argument("target", nargs="?", help="PR body markdown file, or stdin when omitted")
    parser.add_argument("--policy", help="policy file, default: policies/pr-draft.json near project root")
    parser.add_argument("--out-dir", help="output directory, default: .content-guard/pr-drafts")
    parser.add_argument("--name", help="output basename, default: target stem or pr-body")
    parser.add_argument("--strict", action="store_true", help="return nonzero when blocked findings exist")
    parser.add_argument("--json", action="store_true", help="emit machine-readable publish handoff")
    parser.add_argument("--opf", action="store_true", help="run optional OPF backend")
    parser.add_argument("--opf-bin", help="path to opf binary")
    parser.add_argument("--opf-device", help="OPF device, default comes from policy or cpu")
    args = parser.parse_args(argv)

    text, source = _read_target(args.target)
    policy_path = Path(args.policy) if args.policy else _default_policy_path()
    out_dir = Path(args.out_dir) if args.out_dir else Path(".content-guard") / "pr-drafts"
    payload, result = prepare_pr_body(
        text,
        source=source,
        policy_path=policy_path,
        out_dir=out_dir,
        name=args.name or _default_name(source),
        strict=args.strict,
        include_opf=args.opf,
        opf_bin=args.opf_bin,
        opf_device=args.opf_device,
    )

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(to_text(result, path=str(source) if source else "<stdin>"))
        print(f"draft={payload['draft']}")
        print(f"sanitized={payload['sanitized']}")
        print(f"report={payload['report']}")
        print(f"publish_body_file={payload['publish_body_file']}")
        print(f"gh_body_arg=--body-file {payload['publish_body_file']}")
        if payload["blocked"] and not args.strict:
            print("advisory=true")

    return 1 if payload["blocked"] and args.strict else 0


def _read_target(target: str | None) -> tuple[str, Path | None]:
    if not target or target == "-":
        return sys.stdin.read(), None
    path = Path(target)
    return path.read_text(), path


def _default_policy_path() -> Path | PathLike[str] | Traversable:
    return default_policy("pr-draft.json")


def _default_name(source: Path | None) -> str:
    if source is None:
        return "pr-body"
    return source.stem or "pr-body"


def _safe_name(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip(".-")
    return name or "pr-body"


if __name__ == "__main__":
    raise SystemExit(main())
