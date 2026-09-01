"""Normalize one GitHub issue into a Worklore import observation."""

from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import urlparse

from brigade.work_cmd.ledger.import_model import extract_issue_acceptance

from .worklore_validate import (
    ACCEPTANCE_ITEM_MAX,
    ACCEPTANCE_MAX_ITEMS,
    TITLE_MAX,
    WorkloreValidationError,
    assert_no_private_data,
    contains_controls,
)

ISSUE_FIELDS = frozenset(
    {
        "number",
        "title",
        "body",
        "labels",
        "state",
        "url",
        "updatedAt",
        "repository",
        "stale",
    }
)
BURN_QUEUE_LABEL = "burn-queue"


class GitHubObservationError(Exception):
    """A GitHub issue cannot be turned into a Worklore observation."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _unknown_field(name: object) -> GitHubObservationError:
    return GitHubObservationError(f"unknown field: {name}", code="unknown-field")


def _field_bound(message: str) -> GitHubObservationError:
    return GitHubObservationError(message, code="field-bound")


def _reject_private_data(value: object, field: str) -> str:
    """Apply the hub's canonical private-data rules client side, in the adapter's error type."""
    if not isinstance(value, str):
        raise _field_bound(f"{field} must be a string")
    try:
        return assert_no_private_data(value, field)
    except WorkloreValidationError as exc:
        raise GitHubObservationError(str(exc), code=exc.code) from None


def bounded_title(value: object, field: str = "title") -> str:
    """Trim a provider title to the Worklore contract the hub enforces."""
    text = _reject_private_data(value, field).strip()
    if not text:
        raise _field_bound(f"{field} is required")
    if contains_controls(text):
        raise _field_bound(f"{field} must not contain control characters")
    return text[:TITLE_MAX]


def bounded_acceptance(values: object, field: str = "acceptance") -> list[str]:
    """Trim provider acceptance to the item count and length the hub accepts."""
    if values is None:
        return []
    if not isinstance(values, list):
        raise _field_bound(f"{field} must be an array of strings")
    bounded: list[str] = []
    for index, value in enumerate(values):
        text = _reject_private_data(value, f"{field}[{index}]").strip()[:ACCEPTANCE_ITEM_MAX].strip()
        if not text or contains_controls(text):
            continue
        bounded.append(text)
        if len(bounded) == ACCEPTANCE_MAX_ITEMS:
            break
    return bounded


def _normalize_repo(name_with_owner: object) -> tuple[str, str]:
    if not isinstance(name_with_owner, str) or not name_with_owner.strip():
        raise _field_bound("repository.nameWithOwner is required")
    owner, separator, repo = name_with_owner.strip().partition("/")
    if not separator or not owner or not repo or "/" in repo:
        raise _field_bound("repository.nameWithOwner must be owner/repo")
    if repo.lower().endswith(".git"):
        repo = repo[: -len(".git")]
    if not repo:
        raise _field_bound("repository.nameWithOwner must be owner/repo")
    return owner.lower(), repo.lower()


def _parse_issue_url(url: object, *, owner: str, repo: str, number: int) -> str:
    if not isinstance(url, str):
        raise _field_bound("url must be an https GitHub issue URL")
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "github.com" or parsed.params or parsed.query or parsed.fragment:
        raise _field_bound("url must be https://github.com/{owner}/{repo}/issues/{number}")
    parts = parsed.path.strip("/").split("/")
    if len(parts) != 4 or parts[2] != "issues":
        raise _field_bound("url must be https://github.com/{owner}/{repo}/issues/{number}")
    url_owner, url_repo, _, url_number = parts
    if url_repo.lower().endswith(".git"):
        raise _field_bound("url must be https://github.com/{owner}/{repo}/issues/{number}")
    if url_owner.lower() != owner or url_repo.lower() != repo:
        raise _field_bound("url repository must match repository.nameWithOwner")
    if url_number != str(number):
        raise _field_bound("url issue number must match number")
    return url


def _label_names(labels: object) -> list[str]:
    if labels is None:
        return []
    if not isinstance(labels, list):
        raise _field_bound("labels must be an array")
    names: list[str] = []
    for item in labels:
        if not isinstance(item, Mapping) or not isinstance(item.get("name"), str):
            raise _field_bound("labels items must have a name")
        names.append(item["name"])
    return names


def _issue_number(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _field_bound("number must be a positive integer")
    return value


def normalize_issue(issue: Mapping[str, Any]) -> dict[str, Any]:
    """Map one GitHub issue payload to a Worklore import observation."""
    if not isinstance(issue, Mapping):
        raise _field_bound("issue must be an object")
    unknown = [name for name in issue if name not in ISSUE_FIELDS]
    if unknown:
        raise _unknown_field(unknown[0])
    number = _issue_number(issue.get("number"))
    title = bounded_title(issue.get("title"))
    body = issue.get("body", "")
    if body is None:
        body = ""
    _reject_private_data(body, "body")
    repository = issue.get("repository")
    if not isinstance(repository, Mapping):
        raise _field_bound("repository is required")
    owner, repo = _normalize_repo(repository.get("nameWithOwner"))
    url = _parse_issue_url(issue.get("url"), owner=owner, repo=repo, number=number)
    state_raw = issue.get("state")
    if not isinstance(state_raw, str) or not state_raw.strip():
        raise _field_bound("state is required")
    state = state_raw.strip().lower()
    if state not in {"open", "closed"}:
        raise _field_bound("state must be open or closed")
    labels = _label_names(issue.get("labels"))
    if state == "closed":
        source_policy = "closed"
        proposed_status: str | None = "completed"
    elif BURN_QUEUE_LABEL in labels:
        source_policy = "eligible"
        proposed_status = None
    else:
        source_policy = "label-removed"
        proposed_status = None
    observation: dict[str, Any] = {
        "external_key": f"{owner}/{repo}#{number}",
        "link_type": "github",
        "title": title,
        "acceptance": bounded_acceptance(extract_issue_acceptance(body)),
        "source_policy": source_policy,
        "proposed_status": proposed_status,
        "url": url,
    }
    updated_at = issue.get("updatedAt")
    # The hub will not project an observation without a parseable revision, and this is
    # where one could be fabricated. It is not. An issue GitHub returned without
    # ``updatedAt`` is passed through unstamped so the hub refuses it by name and counts
    # it; dropping it here would turn a reported refusal into a silent skip.
    if updated_at is not None:
        if not isinstance(updated_at, str) or not updated_at.strip():
            raise _field_bound("updatedAt must be a timestamp string")
        observation["external_updated_at"] = updated_at
    if "stale" in issue:
        stale = issue["stale"]
        if not isinstance(stale, bool):
            raise _field_bound("stale must be a boolean")
        observation["stale"] = stale
    return observation
