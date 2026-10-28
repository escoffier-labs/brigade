"""Node side of the Projects board: one bounded read-only GraphQL query per page.

Runs through the node's own ``gh`` (the write-capable, private-read path). One
page of 100 items costs one GraphQL rate-limit point. Nothing here writes to
GitHub. The result is a closed-schema snapshot the Hub validates again.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlparse

from . import fleet_project_board as board

PAGE_SIZE = 100
MAX_PAGES = board.MAX_ITEMS // PAGE_SIZE
MIN_INTERVAL = timedelta(minutes=5)
GH_TIMEOUT_SECONDS = 60
SCOPE_HINT = "the GitHub token lacks the read:project scope; add it with: gh auth refresh -s read:project"

QUERY = """
query($owner: String!, $number: Int!, $after: String) {
  organization(login: $owner) {
    projectV2(number: $number) {
      id
      title
      public
      field(name: "Status") { ... on ProjectV2SingleSelectField { options { name } } }
      items(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          fieldValueByName(name: "Status") { ... on ProjectV2ItemFieldSingleSelectValue { name } }
          content {
            __typename
            ... on Issue {
              number
              title
              state
              url
              updatedAt
              repository { nameWithOwner }
              labels(first: 8) { nodes { name color } }
              assignees(first: 5) { nodes { login } }
              parent { number }
              subIssuesSummary { total completed percentCompleted }
              closedByPullRequestsReferences(first: 5) { nodes { number repository { nameWithOwner } } }
            }
          }
        }
      }
    }
  }
  rateLimit { cost remaining }
}
""".strip()

Runner = Callable[[Sequence[str]], "tuple[int, str, str]"]


class CollectError(RuntimeError):
    """A sync failure with a message safe to print (no token, no board content)."""


def run_gh(argv: Sequence[str]) -> tuple[int, str, str]:
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell
            list(argv), capture_output=True, text=True, timeout=GH_TIMEOUT_SECONDS, check=False
        )
    except FileNotFoundError as exc:
        raise CollectError("the gh CLI is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise CollectError("gh timed out querying GitHub") from exc
    return done.returncode, done.stdout, done.stderr


def _clean(value: Any, limit: int) -> str:
    text = "".join(ch if ch.isprintable() else " " for ch in value) if isinstance(value, str) else ""
    return " ".join(text.split())[:limit]


def _classify(payload: Any, stderr: str) -> CollectError:
    errors = payload.get("errors") if isinstance(payload, dict) else None
    messages = [str(e.get("message", "")) for e in errors if isinstance(e, dict)] if isinstance(errors, list) else []
    kinds = [str(e.get("type", "")) for e in errors if isinstance(e, dict)] if isinstance(errors, list) else []
    blob = " ".join(messages + [stderr]).lower()
    if "INSUFFICIENT_SCOPES" in kinds or "read:project" in blob or "required scopes" in blob:
        return CollectError(SCOPE_HINT)
    if "could not resolve to an organization" in blob or "not_found" in " ".join(kinds).lower():
        return CollectError("the organization or project was not found, or this token cannot see it")
    if "gh auth login" in blob or "not logged in" in blob or "authentication" in blob:
        return CollectError("gh is not authenticated; run gh auth login")
    return CollectError("GitHub rejected the board query")


def _page(runner: Runner, owner: str, number: int, after: str | None) -> dict[str, Any]:
    argv = ["gh", "api", "graphql", "-f", f"query={QUERY}", "-F", f"owner={owner}", "-F", f"number={number}"]
    if after:
        argv += ["-f", f"after={after}"]
    code, out, err = runner(argv)
    try:
        payload = json.loads(out) if out.strip() else None
    except json.JSONDecodeError:
        payload = None
    if code != 0 or not isinstance(payload, dict) or payload.get("errors") or not isinstance(payload.get("data"), dict):
        raise _classify(payload, err or "")
    return payload["data"]


def _map(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _github_host(url: Any) -> bool:
    try:
        return urlparse(url).hostname == "github.com" if isinstance(url, str) else False
    except ValueError:
        return False


def _item(node: Mapping[str, Any], options: Sequence[str]) -> dict[str, Any] | None:
    content = node.get("content")
    if not isinstance(content, Mapping) or content.get("__typename") != "Issue":
        return None
    number = content.get("number")
    repo = (
        (content.get("repository") or {}).get("nameWithOwner")
        if isinstance(content.get("repository"), Mapping)
        else None
    )
    if type(number) is not int or number < 1 or not isinstance(repo, str) or not board._REPO.fullmatch(repo):
        return None
    if not _github_host(content.get("url")) or content.get("state") not in ("OPEN", "CLOSED"):
        return None
    status_value = node.get("fieldValueByName")
    status = status_value.get("name") if isinstance(status_value, Mapping) else None
    labels = []
    for label in ((content.get("labels") or {}).get("nodes") or [])[: board.MAX_LABELS]:
        name = _clean(label.get("name"), board.MAX_NAME) if isinstance(label, Mapping) else ""
        if name:
            color = str(label.get("color") or "").lower()
            labels.append({"name": name, "color": color if board._COLOR.fullmatch(color) else None})
    assignees = [
        login
        for a in ((content.get("assignees") or {}).get("nodes") or [])[: board.MAX_ASSIGNEES]
        if isinstance(a, Mapping) and isinstance(login := a.get("login"), str) and board._LOGIN.fullmatch(login)
    ]
    parent = content.get("parent")
    parent_number = parent.get("number") if isinstance(parent, Mapping) else None
    summary = _map(content.get("subIssuesSummary"))
    total, done = summary.get("total"), summary.get("completed")
    total = total if type(total) is int and 0 <= total <= 10000 else 0
    done = done if type(done) is int and 0 <= done <= total else 0
    linked = []
    for pr in ((content.get("closedByPullRequestsReferences") or {}).get("nodes") or [])[: board.MAX_PRS]:
        origin = (pr.get("repository") or {}).get("nameWithOwner") if isinstance(pr, Mapping) else None
        value = pr.get("number") if isinstance(pr, Mapping) else None
        # Links are built from the issue's own repository, so a pull request elsewhere is not shown.
        if origin == repo and type(value) is int and value > 0 and value not in linked:
            linked.append(value)
    stamp = board.parse_time(content.get("updatedAt"))
    return {
        "number": number,
        "repo": repo,
        "title": _clean(content.get("title"), board.MAX_TITLE) or f"Issue #{number}",
        "state": content["state"],
        "url": board.issue_url(repo, number),
        "status": status if status in options else None,
        "labels": labels,
        "assignees": assignees,
        "parent": parent_number if type(parent_number) is int and parent_number > 0 else None,
        "sub_total": total,
        "sub_done": done,
        "linked_prs": linked,
        "updated_at": board._iso(stamp or datetime.fromtimestamp(0, timezone.utc)),
    }


def collect(owner: str, number: int, *, runner: Runner = run_gh, now: datetime | None = None) -> dict[str, Any]:
    """Page through the board (capped) and return a snapshot, or raise ``CollectError``."""
    if not board._OWNER.fullmatch(owner) or number < 1:
        raise CollectError("owner or project number is invalid")
    stamp = now or datetime.now(timezone.utc)
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    skipped = cost = 0
    remaining = 0
    after: str | None = None
    complete = False
    meta: Mapping[str, Any] = {}
    options: list[str] = []
    for page_index in range(MAX_PAGES):
        data = _page(runner, owner, number, after)
        project = _map(data.get("organization")).get("projectV2")
        if not isinstance(project, Mapping):
            raise CollectError("the organization or project was not found, or this token cannot see it")
        limit = _map(data.get("rateLimit"))
        cost += limit["cost"] if type(limit.get("cost")) is int else 0
        remaining = limit["remaining"] if type(limit.get("remaining")) is int else remaining
        if page_index == 0:
            meta = project
            field = _map(project.get("field"))
            options = [
                name
                for o in (field.get("options") or [])[: board.MAX_OPTIONS]
                if isinstance(o, Mapping) and (name := _clean(o.get("name"), board.MAX_NAME))
            ]
            options = list(dict.fromkeys(options))
        block = _map(project.get("items"))
        for node in block.get("nodes") or []:
            parsed = _item(node, options) if isinstance(node, Mapping) else None
            key = (parsed["repo"], parsed["number"]) if parsed else None
            if parsed is None or key in seen:
                skipped += 1
            elif len(items) < board.MAX_ITEMS:
                seen.add(key)  # type: ignore[arg-type]
                items.append(parsed)
        info = _map(block.get("pageInfo"))
        cursor = info.get("endCursor")
        if info.get("hasNextPage") is not True:
            complete = True
            break
        if not isinstance(cursor, str) or not cursor:
            break
        after = cursor
    project_id = meta.get("id")
    snapshot: dict[str, Any] = {
        "schema_version": board.SCHEMA_VERSION,
        "owner": owner,
        "number": number,
        "title": _clean(meta.get("title"), board.MAX_TITLE) or f"Project {number}",
        "url": board.board_url(owner, number),
        "public": meta.get("public") is True,
        "fields": {"status_options": options},
        "items": items,
        "observed_at": board._iso(stamp),
        "complete": complete,
        "skipped": skipped,
        "rate_limit": {"cost": cost, "remaining": remaining},
    }
    if isinstance(project_id, str) and board._NODE_ID.fullmatch(project_id):
        snapshot["project_id"] = project_id
    return board.validate_snapshot(snapshot)


# ---------------------------------------------------------------- interval guard


def state_path(home: Path) -> Path:
    return home / "projects-board-state.json"


def _state_key(owner: str, number: int) -> str:
    return f"{owner}/{number}"


def last_success(home: Path, owner: str, number: int) -> datetime | None:
    try:
        data = json.loads(state_path(home).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return board.parse_time(data.get(_state_key(owner, number))) if isinstance(data, dict) else None


def record_success(home: Path, owner: str, number: int, now: datetime) -> None:
    path = state_path(home)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data = data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        data = {}
    data[_state_key(owner, number)] = board._iso(now)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    path.chmod(0o600)


def too_soon(home: Path, owner: str, number: int, now: datetime) -> timedelta | None:
    """Time left until the next sync is allowed, or None when one may run."""
    last = last_success(home, owner, number)
    if last is None or now - last >= MIN_INTERVAL:
        return None
    return MIN_INTERVAL - (now - last)
