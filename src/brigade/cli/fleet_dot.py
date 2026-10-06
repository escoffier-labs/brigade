"""CLI registration for explicit, bounded Dot metadata reports."""

from __future__ import annotations

import argparse
import json
import stat
import sys
from pathlib import Path

from ..fleet_dot import MAX_REPORT_BYTES, DotReportError, parse_report, read_report, report_session


def register(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("dot", help="Report explicitly known Dot cloud sessions.")
    commands = parser.add_subparsers(dest="dot_command", required=True)
    report = commands.add_parser("report", help="Validate metadata offline by default; publishing is explicit.")
    report.add_argument("--metadata-file", type=Path, help="Read a bounded JSON metadata file instead of stdin.")
    report.add_argument(
        "--publish", action="store_true", help="Publish with this enrolled node's existing fleet clients."
    )
    report.add_argument(
        "--holder-file", type=Path, help="Protected existing Worklore holder file, read only with --publish."
    )
    report.set_defaults(func=dispatch)


def dispatch(args: argparse.Namespace) -> int:
    try:
        if args.metadata_file is None:
            data = sys.stdin.buffer.read(MAX_REPORT_BYTES + 1)
        else:
            path = args.metadata_file
            if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
                raise DotReportError("metadata-file must be a regular file")
            with path.open("rb") as stream:
                data = stream.read(MAX_REPORT_BYTES + 1)
        raw = read_report(data)
        parsed = parse_report(raw)
        nonce = None
        if args.publish and args.holder_file is not None:
            if not (parsed.snapshot.cloud_context or {}).get("work_id"):
                raise DotReportError("holder-file requires an explicit work_id")
            from ..grokbot_mcp import ConfigurationError, load_hub_token_file

            try:
                # Existing owner-private, no-follow descriptor reader. Never
                # generate credentials or include them in report projections.
                nonce = load_hub_token_file(args.holder_file)
            except ConfigurationError:
                raise DotReportError("invalid protected holder-file") from None
        result = report_session(raw, publish=args.publish, holder_nonce=nonce)
    except (DotReportError, OSError):
        print(json.dumps({"error": "invalid bounded Dot metadata; no publication attempted"}))
        return 2
    print(json.dumps(result, sort_keys=True, indent=2))
    return 1 if any(value.startswith(("failed:", "unknown:")) for value in result["components"].values()) else 0
