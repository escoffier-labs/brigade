"""A fake GitHub Projects board snapshot: fake titles, two epics, an item in every status."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

OWNER = "example-org"
NUMBER = 7
REPO = "example-org/widgets"
OPTIONS = ["Backlog", "Ready", "In Progress", "Review", "Merge queue", "Done", "Blocked"]


def _item(number: int, title: str, status: str | None, now: datetime, **extra: Any) -> dict[str, Any]:
    item = {
        "number": number,
        "repo": REPO,
        "title": title,
        "state": "CLOSED" if status == "Done" else "OPEN",
        "url": f"https://github.com/{REPO}/issues/{number}",
        "status": status,
        "labels": [],
        "assignees": [],
        "parent": None,
        "sub_total": 0,
        "sub_done": 0,
        "linked_prs": [],
        "updated_at": (now - timedelta(minutes=number)).replace(microsecond=0).isoformat(),
    }
    item.update(extra)
    return item


def snapshot(now: datetime | None = None, *, complete: bool = True) -> dict[str, Any]:
    now = now or datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
    fleet = {"name": "fleet", "color": "1d76db"}
    bug = {"name": "bug", "color": "d73a4a"}
    items = [
        _item(101, "epic: Sample alpha rollout", "In Progress", now, sub_total=4, sub_done=2, labels=[fleet]),
        _item(102, "epic: Sample beta cleanup", "Ready", now, sub_total=2, sub_done=1),
        _item(
            103, "Add sample widget parser", "In Progress", now, parent=101, labels=[fleet, bug], linked_prs=[301, 302]
        ),
        _item(104, "Sample widget docs", "Review", now, parent=101, linked_prs=[303]),
        _item(105, "Sample widget release notes", "Merge queue", now, parent=101),
        _item(106, "Sample widget retired path", "Done", now, parent=101),
        _item(107, "Sample cleanup step one", "Done", now, parent=102),
        _item(108, "Sample cleanup step two", "Backlog", now, parent=102, labels=[{"name": "no-color", "color": None}]),
        _item(109, "Sample blocked dependency", "Blocked", now, labels=[bug]),
        _item(110, "Sample unplanned idea", None, now),
        _item(111, "Sample ready task", "Ready", now, assignees=["sample-dev"]),
    ]
    return {
        "schema_version": 1,
        "owner": OWNER,
        "number": NUMBER,
        "title": "Example Delivery",
        "url": f"https://github.com/orgs/{OWNER}/projects/{NUMBER}",
        "public": False,
        "project_id": "PVT_example0001",
        "fields": {"status_options": list(OPTIONS)},
        "items": items,
        "observed_at": (now - timedelta(minutes=6)).replace(microsecond=0).isoformat(),
        "complete": complete,
        "skipped": 1,
        "rate_limit": {"cost": 1, "remaining": 4999},
    }
