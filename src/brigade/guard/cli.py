from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path

from . import allowlist
from .audit import render_text as render_audit_text, run_audit
from .baseline import (
    DEFAULT_BASELINE_FILENAME,
    Baseline,
    filter_findings,
    init_baseline,
    load_baseline,
    save_baseline,
)
from .engine import scan_text
from .git_scan import _default_repo_policy
from .policy import load_policy
from .report import to_json, to_payload, to_text
from .types import GuardResult, ScanOptions


DEFAULT_EXCLUDE_DIR_NAMES = frozenset(
    {
        "node_modules",
        ".git",
        "dist",
        "build",
        "coverage",
        ".next",
        ".cache",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        "out",
        ".turbo",
        ".parcel-cache",
        "vendor",
        ".claude",
    }
)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "scan":
        return _scan(args)
    if args.command == "redact":
        return _redact(args)
    if args.command == "diff":
        return _diff(args)
    if args.command == "audit":
        return _audit(args)
    if args.command == "baseline":
        return _baseline(args)
    if args.command == "allow":
        if args.allow_action == "add":
            return allowlist.run_add(args.policy, args.values, args.note)
        return allowlist.run_list(args.policy)

    parser.error(f"unknown command: {args.command}")
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="brigade guard",
        description="Policy-driven content scanning and redaction.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("scan", "redact", "diff"):
        cmd = sub.add_parser(name)
        cmd.add_argument("target", nargs="?", help="file to read, or stdin when omitted")
        cmd.add_argument("--policy", help="JSON policy file")
        cmd.add_argument("--opf", action="store_true", help="run optional OPF backend")
        cmd.add_argument("--opf-bin", help="path to opf binary")
        cmd.add_argument("--opf-device", help="OPF device, default comes from policy or cpu")
        cmd.add_argument("--scan-frontmatter", action="store_true", help="scan YAML frontmatter")
        cmd.add_argument("--skip-code-blocks", action="store_true", help="ignore fenced code blocks")
        cmd.add_argument("--no-allow-comments", action="store_true", help="ignore content-guard allow comments")

    sub.choices["scan"].add_argument("--json", action="store_true", help="emit JSON report")
    sub.choices["scan"].add_argument(
        "--baseline",
        help="path to a baseline file; findings already in the baseline are suppressed",
    )
    sub.choices["redact"].add_argument("--in-place", action="store_true", help="rewrite the target file")

    audit_cmd = sub.add_parser("audit", help="aggregate scan results across a directory")
    audit_cmd.add_argument("target", help="directory to audit")
    audit_cmd.add_argument("--policy", help="JSON policy file (default: built-in public-repo policy)")
    audit_cmd.add_argument(
        "--scope",
        choices=("tracked", "tree"),
        default="tracked",
        help="enumerate via 'git ls-files' (tracked, default) or filesystem walk (tree)",
    )
    audit_cmd.add_argument("--json", action="store_true", help="emit JSON report")
    audit_cmd.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero if any blocking findings (default exits 0)",
    )
    audit_cmd.add_argument("--scan-frontmatter", action="store_true", help="scan YAML frontmatter")
    audit_cmd.add_argument("--skip-code-blocks", action="store_true", help="ignore fenced code blocks")
    audit_cmd.add_argument(
        "--no-allow-comments",
        action="store_true",
        help="ignore content-guard allow comments",
    )
    audit_cmd.add_argument("--opf", action="store_true", help="run optional OPF backend")
    audit_cmd.add_argument("--opf-bin", help="path to opf binary")
    audit_cmd.add_argument("--opf-device", help="OPF device, default comes from policy or cpu")

    baseline_cmd = sub.add_parser(
        "baseline",
        help="manage a baseline of pre-existing findings (gitleaks-style)",
    )
    baseline_sub = baseline_cmd.add_subparsers(dest="baseline_action", required=True)

    baseline_init = baseline_sub.add_parser(
        "init",
        help="scan a directory and record current findings as an accepted baseline",
    )
    baseline_init.add_argument("target", help="directory to scan")
    baseline_init.add_argument("--policy", help="JSON policy file")
    baseline_init.add_argument(
        "--output",
        help=(f"output path for the baseline file (default: <target>/{DEFAULT_BASELINE_FILENAME})"),
    )

    allow_cmd = sub.add_parser(
        "allow",
        help="manage allow_values in the private policy (known-public literals)",
    )
    allow_sub = allow_cmd.add_subparsers(dest="allow_action", required=True)

    allow_add = allow_sub.add_parser(
        "add",
        help="append exact literal strings to allow_values, skipping duplicates",
    )
    allow_add.add_argument("values", nargs="+", help="exact matched string(s) to allow")
    allow_add.add_argument(
        "--policy",
        help=("policy file to edit (default: $CONTENT_GUARD_PRIVATE_POLICY or ~/.config/content-guard/internal.json)"),
    )
    allow_add.add_argument("--note", help="provenance note recorded next to the values")

    allow_list = allow_sub.add_parser("list", help="print current allow_values")
    allow_list.add_argument("--policy", help="policy file to read (same default as add)")

    return parser


def _options(args: argparse.Namespace) -> ScanOptions:
    return ScanOptions(
        scan_frontmatter=args.scan_frontmatter,
        scan_code_blocks=not args.skip_code_blocks,
        honor_allow_comments=not args.no_allow_comments,
        include_opf=args.opf,
        opf_device=args.opf_device,
        opf_bin=args.opf_bin,
    )


def _read_target(target: str | None) -> tuple[str, str | None]:
    if not target or target == "-":
        return sys.stdin.read(), None
    path = Path(target)
    return path.read_text(), str(path)


def _scan(args: argparse.Namespace) -> int:
    policy = load_policy(args.policy)
    options = _options(args)
    target_path = Path(args.target) if args.target and args.target != "-" else None

    baseline = load_baseline(Path(args.baseline)) if getattr(args, "baseline", None) else None

    if target_path and target_path.is_dir():
        results = _scan_directory(target_path, policy, options)
        if baseline is not None:
            results = [(p, _apply_baseline(r, baseline, _baseline_rel_path(p, target_path))) for p, r in results]
        blocked = any(result.blocked for _, result in results)
        if args.json:
            print(
                json.dumps(
                    {
                        "ok": not blocked,
                        "blocked": blocked,
                        "files_scanned": len(results),
                        "files": [
                            {"path": str(path), **to_payload(result)} for path, result in results if result.findings
                        ],
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        elif not any(result.findings for _, result in results):
            print(f"Clean. {len(results)} file(s) checked.")
        else:
            for path, result in results:
                if result.findings:
                    print(to_text(result, path=str(path)))
        return 1 if blocked else 0

    text, path = _read_target(args.target)
    result = scan_text(text, policy=policy, options=options)
    if baseline is not None:
        # Single-file scan: match against the file's basename, since baseline
        # entries are stored relative to their init target directory.
        rel = Path(path).name if path else ""
        result = _apply_baseline(result, baseline, rel)
    if args.json:
        print(to_json(result))
    else:
        print(to_text(result, path=path or "<stdin>"))
    return 1 if result.blocked else 0


def _baseline_rel_path(file_path: Path, target_dir: Path) -> str:
    try:
        return str(file_path.relative_to(target_dir))
    except ValueError:
        return str(file_path)


def _apply_baseline(result: GuardResult, baseline: Baseline, rel_path: str) -> GuardResult:
    """Return a new GuardResult with baseline-known findings filtered out."""
    kept = filter_findings(result.findings, baseline, rel_path)
    return GuardResult(text=result.text, redacted_text=result.redacted_text, findings=kept)


def _redact(args: argparse.Namespace) -> int:
    policy = load_policy(args.policy)
    options = _options(args)
    target_path = Path(args.target) if args.target and args.target != "-" else None

    if target_path and target_path.is_dir():
        if not args.in_place:
            print("directory redact requires --in-place", file=sys.stderr)
            return 2
        results = _scan_directory(target_path, policy, options)
        for path, result in results:
            if result.changed:
                path.write_text(result.redacted_text)
        return 1 if any(result.blocked for _, result in results) else 0

    text, path = _read_target(args.target)
    result = scan_text(text, policy=policy, options=options)

    if args.in_place:
        if not path:
            print("--in-place requires a file target", file=sys.stderr)
            return 2
        Path(path).write_text(result.redacted_text)
    else:
        sys.stdout.write(result.redacted_text)
    return 1 if result.blocked else 0


def _diff(args: argparse.Namespace) -> int:
    policy = load_policy(args.policy)
    options = _options(args)
    target_path = Path(args.target) if args.target and args.target != "-" else None

    if target_path and target_path.is_dir():
        results = _scan_directory(target_path, policy, options)
        for path, result in results:
            if not result.changed:
                continue
            _write_diff(result.text, result.redacted_text, str(path))
        return 1 if any(result.blocked for _, result in results) else 0

    text, path = _read_target(args.target)
    result = scan_text(text, policy=policy, options=options)
    source_name = path or "<stdin>"
    _write_diff(text, result.redacted_text, source_name)
    return 1 if result.blocked else 0


def _scan_directory(
    path: Path,
    policy,
    options: ScanOptions,
    exclude_dirs: frozenset[str] = DEFAULT_EXCLUDE_DIR_NAMES,
):
    results = []
    for file_path in sorted(path.rglob("*.md")):
        if exclude_dirs.intersection(file_path.parts):
            continue
        text = file_path.read_text()
        results.append((file_path, scan_text(text, policy=policy, options=options)))
    return results


def _audit(args: argparse.Namespace) -> int:
    target = Path(args.target)
    if not target.exists():
        print(f"audit target does not exist: {target}", file=sys.stderr)
        return 2
    if not target.is_dir():
        print(f"audit target must be a directory: {target}", file=sys.stderr)
        return 2

    policy = load_policy(args.policy) if args.policy else _default_repo_policy()
    options = _options(args)

    report = run_audit(target, policy=policy, scope=args.scope, options=options)

    if args.json:
        print(json.dumps(report.to_payload(), indent=2, sort_keys=True))
    else:
        print(render_audit_text(report))

    if args.strict and report.blocked:
        return 1
    return 0


def _baseline(args: argparse.Namespace) -> int:
    if args.baseline_action != "init":
        print(f"unknown baseline action: {args.baseline_action}", file=sys.stderr)
        return 2

    target = Path(args.target)
    if not target.exists():
        print(f"baseline target does not exist: {target}", file=sys.stderr)
        return 2
    if not target.is_dir():
        print(f"baseline target must be a directory: {target}", file=sys.stderr)
        return 2

    policy = load_policy(args.policy) if args.policy else None
    baseline = init_baseline(target, policy=policy, scope="tree")

    out_path = Path(args.output) if args.output else target / DEFAULT_BASELINE_FILENAME
    out_path.parent.mkdir(parents=True, exist_ok=True)
    save_baseline(baseline, out_path)

    print(f"Baseline written: {out_path} ({len(baseline.entries)} entry(ies) captured)")
    return 0


def _write_diff(text: str, redacted_text: str, source_name: str) -> None:
    diff = difflib.unified_diff(
        text.splitlines(keepends=True),
        redacted_text.splitlines(keepends=True),
        fromfile=source_name,
        tofile=f"{source_name} (redacted)",
    )
    sys.stdout.writelines(diff)
