"""Title and guidance copy for the control-plane empty states on the Deck.

Each entry is ``(title, hint)`` for ``ui_theme.empty_state``. Titles that carry
"unknown" keep absence of data from reading as zero, idle, or clear.
"""

from __future__ import annotations

COPY: dict[str, tuple[str, str]] = {
    "no_projection": (
        "No control-plane projection attached",
        "Policy version, machine capacity, quota, routing receipts, queue eligibility, and integration readiness "
        "are all unknown, which is not the same as nothing being wrong.",
    ),
    "capacity": (
        "No machine capacity reported (unknown, not idle)",
        "Machines appear here once the control plane reports their load and free slots.",
    ),
    "quota": (
        "No quota observations (unknown, not unlimited)",
        "Rows appear here when a provider reports usage for a quota pool.",
    ),
    "routes": (
        "No routing receipts (unknown, not unrouted)",
        "A receipt is recorded here each time the router selects a seat for a task.",
    ),
    "blocked_none": (
        "No blocked reasons reported",
        "Queued items that cannot run are listed here with the reason they are held.",
    ),
    "blocked_unknown": (
        "Blocked reasons unknown",
        "The queue section did not report blocked reasons, so an empty list does not mean nothing is held.",
    ),
    "integrations_none": (
        "No integration readiness reports",
        "Each integration appears here once it reports routine and auth state in a snapshot.",
    ),
    "integrations_unknown": (
        "Integration readiness unknown",
        "No telemetry section reported, so readiness cannot be shown for any integration.",
    ),
    "headroom_unknown": (
        "Effective headroom unknown",
        "No centrally computed quota projection is attached, so the raw observations below are history, "
        "not available headroom.",
    ),
    "headroom_no_rows": (
        "No effective quota rows",
        "The projection is attached but lists no accounts, so headroom cannot be shown.",
    ),
    "queue_unknown": (
        "Queue unknown",
        "The control-plane snapshot has no queue section, so queue state cannot be shown.",
    ),
    "no_jobs": ("No managed jobs reported", "Jobs appear here when a station reports work it is managing."),
    "no_stations": ("No stations configured", "Start the hub with `--deck-config` to add stations to the Deck."),
    "no_leases": ("No active leases", "Leases appear here while this provider runs work."),
    "no_clouds": (
        "No cloud providers configured",
        "Providers appear here when the hub reports cloud worker capacity.",
    ),
}
