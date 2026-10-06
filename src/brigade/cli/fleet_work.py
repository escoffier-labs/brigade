"""Worklore parser registration; dispatch handlers stay in the fleet module."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping


def register(
    fleet_sub: argparse._SubParsersAction,
    handlers: Mapping[str, Callable[[argparse.Namespace], int]],
) -> None:
    p_work = fleet_sub.add_parser("work", help="Create, inspect, and schedule Worklore items on the fleet hub.")
    work_sub = p_work.add_subparsers(dest="work_command", metavar="<work-command>")
    work_sub.required = True
    from . import fleet_work_ownership

    fleet_work_ownership.register(work_sub)
    p_work_create = work_sub.add_parser("create", help="Create a native Worklore item (operator node token).")
    p_work_create.add_argument("--title", required=True, help="Item title.")
    p_work_create.add_argument("--kind", required=True, help="Item kind (fleet, admin, or other allowed kind).")
    p_work_create.add_argument(
        "--idempotency-key",
        default=None,
        help="Idempotency key for native create; generated when omitted.",
    )
    p_work_create.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    p_work_create.set_defaults(func=handlers["create"])
    p_work_show = work_sub.add_parser("show", help="Show one Worklore item.")
    p_work_show.add_argument("work_id", help="Worklore item id.")
    p_work_show.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    p_work_show.set_defaults(func=handlers["show"])
    p_work_list = work_sub.add_parser("list", help="List Worklore items.")
    p_work_list.add_argument(
        "--source",
        default=None,
        help="Filter by source (github, brigade, or native).",
    )
    p_work_list.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    p_work_list.set_defaults(func=handlers["list"])
    p_work_burn = work_sub.add_parser("burn", help="Show one page of the current burn queue.")
    p_work_burn.add_argument("--limit", type=int, default=None, help="Items per page (1-100, default 50).")
    p_work_burn.add_argument("--cursor", default=None, help="Page cursor from a previous burn read.")
    p_work_burn.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    p_work_burn.set_defaults(func=handlers["burn"])
    p_work_next = work_sub.add_parser(
        "next", help="Show the first eligible burn item with a dry-run route (read-only, never reserves)."
    )
    p_work_next.add_argument("--consumer", default="brigade-run", help="Routing consumer (default brigade-run).")
    p_work_next.add_argument("--workload", default="general", help="Routing workload (default general).")
    p_work_next.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    p_work_next.set_defaults(func=handlers["next"])
    p_work_patch = work_sub.add_parser(
        "patch", help="Patch scheduling fields on a Worklore item (operator node token)."
    )
    p_work_patch.add_argument("work_id", help="Worklore item id.")
    p_work_patch.add_argument("--if-match", required=True, help="Expected item version.")
    p_work_patch.add_argument("--title", default=None, help="Replacement title.")
    p_work_patch.add_argument("--description", default=None, help="Replacement description.")
    p_work_patch.add_argument("--scope", default=None, help="Replacement scope.")
    p_work_patch.add_argument("--priority", default=None, help="Replacement priority.")
    p_work_patch.add_argument("--burn-rank", type=int, default=None, help="Replacement burn rank.")
    burn_eligible = p_work_patch.add_mutually_exclusive_group()
    burn_eligible.add_argument(
        "--burn-eligible",
        dest="burn_eligible",
        action="store_const",
        const=True,
        default=None,
        help="Mark the item eligible for burn scheduling.",
    )
    burn_eligible.add_argument(
        "--no-burn-eligible",
        dest="burn_eligible",
        action="store_const",
        const=False,
        help="Mark the item ineligible for burn scheduling.",
    )
    p_work_patch.add_argument("--token-appetite", default=None, help="Replacement token appetite.")
    p_work_patch.add_argument("--execution-mode", default=None, help="Replacement execution mode.")
    p_work_patch.add_argument("--acceptance", action="append", default=None, help="Acceptance criterion (repeatable).")
    p_work_patch.add_argument("--blocker", default=None, help="Replacement blocker text.")
    p_work_patch.add_argument("--review-after", default=None, help="Replacement review-after timestamp.")
    p_work_patch.add_argument("--spend-by", default=None, help="Replacement spend-by timestamp.")
    p_work_patch.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    p_work_patch.set_defaults(func=handlers["patch"])
    p_work_transition = work_sub.add_parser(
        "transition", help="Move a Worklore item to another status (operator node token)."
    )
    p_work_transition.add_argument("work_id", help="Worklore item id.")
    p_work_transition.add_argument("--to-status", required=True, help="Target status.")
    p_work_transition.add_argument("--if-match", required=True, help="Expected item version.")
    p_work_transition.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    p_work_transition.set_defaults(func=handlers["transition"])
    p_work_attempt = work_sub.add_parser("attempt", help="Record a started, failed, or reset attempt.")
    p_work_attempt.add_argument("work_id", help="Worklore item id.")
    p_work_attempt.add_argument(
        "--action",
        required=True,
        choices=("started", "failed", "reset"),
        help="Attempt action. reset requires an operator node token.",
    )
    p_work_attempt.add_argument("--run-id", default=None, help="Fleet run id for started or failed.")
    p_work_attempt.add_argument("--if-match", required=True, help="Expected item version.")
    p_work_attempt.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    p_work_attempt.set_defaults(func=handlers["attempt"])
    p_work_link = work_sub.add_parser("link", help="Add an operator-managed link (operator node token).")
    p_work_link.add_argument("work_id", help="Worklore item id.")
    p_work_link.add_argument("--link-type", required=True, help="Link type (url, github, or brigade).")
    p_work_link.add_argument("--external-key", required=True, help="Stable external identity.")
    p_work_link.add_argument("--url", default=None, help="Optional https URL.")
    p_work_link.add_argument("--display-ref", default=None, help="Optional display label.")
    p_work_link.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    p_work_link.set_defaults(func=handlers["link"])
    p_work_unlink = work_sub.add_parser("unlink", help="Remove one operator-managed link (operator node token).")
    p_work_unlink.add_argument("work_id", help="Worklore item id.")
    p_work_unlink.add_argument("link_id", help="Link id.")
    p_work_unlink.add_argument("--if-match", default=None, help="Optional expected item version.")
    p_work_unlink.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    p_work_unlink.set_defaults(func=handlers["unlink"])
    p_work_sync_github = work_sub.add_parser(
        "sync-github",
        help="Import labeled GitHub issues into Worklore.",
    )
    p_work_sync_github.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    p_work_sync_github.set_defaults(func=handlers["sync_github"])
    p_work_sync_brigade = work_sub.add_parser(
        "sync-brigade",
        help="Import configured Brigade ledgers into Worklore.",
    )
    p_work_sync_brigade.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    p_work_sync_brigade.set_defaults(func=handlers["sync_brigade"])
