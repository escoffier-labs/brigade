"""CLI surface for the Grok Bot approved build selector.

The parser registration and dispatch live here so `run_cloud.py` keeps only a
two-line hook. Output is counts, the selected issue number, and opaque job
handles: policy contents, tokens, and paths never reach stdout or stderr.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable


def register(subparsers: Any, add_target: Callable[[argparse.ArgumentParser], None]) -> None:
    """Add `brigade run cloud grokbot build-feed` to the grokbot command group."""
    parser = subparsers.add_parser(
        "build-feed",
        help="Select one approved GitHub issue for a Grok Bot implementation worker.",
    )
    add_target(parser)
    parser.add_argument("--policy", type=Path, required=True, help="Path to the approved private build policy.")
    parser.add_argument("--apply", action="store_true", help="Enqueue after selection. Default is preview only.")


def dispatch(args: Any, target: Path) -> int:
    """Preview or enqueue one approved build job without exposing policy content."""
    from .. import grokbot_build_feed, grokbot_mcp
    from .run_cloud import _feed_hub_identity

    actor_kind: str | None = None
    try:
        if args.apply:
            identity = _feed_hub_identity(target)
            actor_kind = identity.actor_kind
            with identity.context:
                result = grokbot_build_feed.apply(target, args.policy)
        else:
            result = grokbot_build_feed.preflight(target, args.policy)
    except grokbot_mcp.ConfigurationError:
        print("error: Grok Bot feed configuration is invalid", file=sys.stderr)
        return 2
    except grokbot_build_feed.BuildFeedError as exc:
        if exc.action is not None and exc.actor_kind is None:
            exc.actor_kind = actor_kind
        print(f"error: {exc.public_detail()}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    _print_result(result)
    return 0


def _print_result(result: dict) -> None:
    """Render the safe build selection projection without policy or issue contents."""
    parts = [f"created={result['created']}", f"reason={result['reason']}"]
    if result["issue_number"] is not None:
        parts.append(f"issue={result['issue_number']}")
    if result["scout_report"] is not None:
        parts.append(f"scout_report={result['scout_report']}")
    print("grokbot build-feed: " + " ".join(parts))
    handle = result.get("handle")
    if handle is not None:
        print(f"job {handle['job_id']} state={handle['state']}")
