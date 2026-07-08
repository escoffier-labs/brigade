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
from typing import Any

from .engine import scan_text
from .policy import default_policy, load_policy
from .report import to_payload
from .types import ScanOptions

POLICY_ALIASES = {
    "openclaw-message": "openclaw-message.json",
    "pr-draft": "pr-draft.json",
    "public-content": "public-content.json",
    "public-repo": "public-repo.json",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="content-guard-n8n-advisory",
        description="Scan one n8n text payload from stdin and return JSON for advisory workflow branching.",
    )
    parser.add_argument("--policy", help="policy alias or JSON path, default: public-content")
    parser.add_argument("--strict", action="store_true", help="return nonzero when blocked findings exist")
    parser.add_argument("--opf", action="store_true", help="run optional OPF backend")
    parser.add_argument("--opf-bin", help="path to opf binary")
    parser.add_argument("--opf-device", help="OPF device, default comes from policy or cpu")
    args = parser.parse_args(argv)

    try:
        request = json.loads(sys.stdin.read() or "{}")
        payload = run_advisory_check(request, args)
    except ValueError as exc:
        print(json.dumps({"ok": False, "blocked": True, "error": str(exc)}, indent=2, sort_keys=True))
        return 2

    print(json.dumps(payload, indent=2, sort_keys=True))
    return 1 if args.strict and payload["blocked"] else 0


def run_advisory_check(request: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise ValueError("stdin JSON must be an object")

    text = request.get("text")
    if not isinstance(text, str):
        raise ValueError("stdin JSON must include string field 'text'")

    policy_value = request.get("policy") or args.policy or "public-content"
    if not isinstance(policy_value, str):
        raise ValueError("'policy' must be a string alias or JSON path")

    source = request.get("source")
    name = request.get("name")
    policy_path = _policy_path(policy_value)
    result = scan_text(
        text,
        policy=load_policy(policy_path),
        options=ScanOptions(
            include_opf=args.opf,
            opf_bin=args.opf_bin,
            opf_device=args.opf_device,
        ),
    )

    return {
        **to_payload(result),
        "advisory": bool(result.blocked),
        "policy": str(policy_path),
        "source": source if isinstance(source, str) else "<stdin>",
        "name": name if isinstance(name, str) else "n8n-content",
        "sanitized_text": result.redacted_text,
    }


def _policy_path(value: str) -> Path | PathLike[str] | Traversable:
    path = Path(value)
    if path.is_file():
        return path

    alias = POLICY_ALIASES.get(value)
    if alias:
        return default_policy(alias)

    raise ValueError(f"unknown policy alias or file: {value}")


if __name__ == "__main__":
    raise SystemExit(main())
