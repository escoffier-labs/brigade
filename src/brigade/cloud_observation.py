"""Deadlines and pagination for read-only cloud tracker inventories."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from concurrent.futures import Future, TimeoutError
from threading import Thread
from typing import Any, TypeVar

CODEX_BUDGET_SECONDS = 30.0
CURSOR_BUDGET_SECONDS = 20.0
GITHUB_BUDGET_SECONDS = 20.0

T = TypeVar("T")
PageReport = Callable[[list[dict[str, Any]], bool], None]


def start_observation(
    name: str,
    budget: float,
    observe: Callable[[float, Callable[[T], None]], T],
    failure: Callable[[str, T | None], T],
) -> Callable[[], T]:
    """Start a read immediately and return a deadline-bounded result reader."""
    expires = time.monotonic() + budget
    future: Future[T] = Future()
    progress: T | None = None

    def publish(snapshot: T) -> None:
        nonlocal progress
        if time.monotonic() < expires:
            progress = snapshot

    def run() -> None:
        try:
            # Leave time for the transport to unwind and return completed pages.
            result = observe(expires - min(1.0, budget / 10), publish)
        except Exception:  # noqa: BLE001 - a provider failure must stay isolated
            result = failure("transport-failure", progress)
        if time.monotonic() <= expires:
            future.set_result(result)

    Thread(target=run, name=f"brigade-observe-{name}", daemon=True).start()

    def wait() -> T:
        try:
            return future.result(timeout=max(0.0, expires - time.monotonic()))
        except TimeoutError:
            return failure("observation-timeout", progress)

    return wait


def github_pages(
    read: Callable[[list[str]], tuple[int, str, str]],
    endpoint: str,
    parse: Callable[[dict[str, Any]], dict[str, Any] | None],
    on_page: PageReport,
) -> tuple[list[dict[str, Any]], bool]:
    """Keep parsed rows from earlier REST pages when a later page fails."""
    rows: list[dict[str, Any]] = []
    previous_page = None
    page = 1
    valid = True
    while True:
        query = endpoint if page == 1 else f"{endpoint}&page={page}"
        code, stdout, _ = read(["gh", "api", query])
        if code != 0:
            return rows, False
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            return rows, False
        if not isinstance(data, list) or data == previous_page:
            return rows, False
        for item in data:
            row = parse(item) if isinstance(item, dict) else None
            if row is None:
                valid = False
            else:
                rows.append(row)
        final_page = len(data) < 100
        on_page(rows, final_page and valid)
        if final_page:
            return rows, valid
        previous_page = data
        page += 1


def github_branch(item: dict[str, Any]) -> dict[str, Any] | None:
    name = item.get("name")
    return {"name": name} if isinstance(name, str) and name else None


def github_pr(item: dict[str, Any]) -> dict[str, Any] | None:
    head = item.get("head")
    state, url = item.get("state"), item.get("html_url")
    if not isinstance(head, dict) or not isinstance(head.get("ref"), str) or state not in ("open", "closed"):
        return None
    return {
        "head": head["ref"],
        "state": "MERGED" if item.get("merged_at") else state.upper(),
        "number": item.get("number"),
        "url": url if isinstance(url, str) else None,
        "isDraft": item.get("draft"),
        "headRefOid": head.get("sha"),
    }
