"""Bounded, read-only managed review collection through an injected GET boundary.

The transport owns authorization/routing and must enforce the requested byte cap
while reading. This module has no live transport, persistence or provider actions.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

from brigade.claude_review_evidence import _head, _object_id, _timestamp, normalize_check_run

_OWNER = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")
_REPO = re.compile(r"[A-Za-z0-9_.-]{1,100}")
_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,99}")
_PATH = re.compile(r"[A-Za-z0-9_. /-]{1,512}")
_STATES = {"APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED", "PENDING"}


@dataclass(frozen=True)
class Limits:
    """Page cap is per endpoint; other caps are shared across the collection."""

    max_pages: int = 20
    max_requests: int = 100
    max_objects: int = 1000
    max_bytes: int = 4 * 1024 * 1024
    per_page: int = 100

    def __post_init__(self) -> None:
        for value, ceiling in (
            (self.max_pages, 100),
            (self.max_requests, 1000),
            (self.max_objects, 10000),
            (self.max_bytes, 16 * 1024 * 1024),
            (self.per_page, 100),
        ):
            if type(value) is not int or not 0 < value <= ceiling:
                raise ValueError("collection limits must be positive bounded integers")


@dataclass(frozen=True)
class GetResponse:
    status: int
    body: bytes
    truncated: bool = False


class GetTransport(Protocol):
    def get(self, path: str, *, params: Mapping[str, str | int], max_bytes: int) -> GetResponse:
        """GET a fixed API path, with no redirects or fallback, bounded on read."""
        ...


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _constant(value: str) -> Any:
    raise ValueError("non-JSON constant")


def _count(value: object) -> int | None:
    return value if type(value) is int and 0 <= value <= 2**63 - 1 else None


def _reason(source: dict[str, Any], reason: str) -> None:
    if reason not in source["reasons"]:
        source["reasons"].append(reason)
    source["complete"] = False


class _Reader:
    def __init__(self, transport: GetTransport, limits: Limits):
        self.transport = transport
        self.limits = limits
        self.usage = {"requests": 0, "objects": 0, "bytes": 0}
        self.sources: list[dict[str, Any]] = []

    def read(self, path: str, resource: str, *, checks: bool = False) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        source: dict[str, Any] = {
            "resource": resource,
            "endpoint": path,
            "complete": True,
            "reasons": [],
            "pages": 0,
            "objects": 0,
            "bytes": 0,
            "duplicate_objects": 0,
            "http_status": None,
        }
        self.sources.append(source)
        rows: list[dict[str, Any]] = []
        seen_pages: set[str] = set()
        seen_check_ids: set[int] = set()
        total: int | None = None
        for page in range(1, self.limits.max_pages + 1):
            for dimension, limit in (
                ("requests", self.limits.max_requests),
                ("objects", self.limits.max_objects),
                ("bytes", self.limits.max_bytes),
            ):
                if self.usage[dimension] >= limit:
                    _reason(
                        source,
                        {"requests": "request-budget", "objects": "object-budget", "bytes": "byte-budget"}[dimension],
                    )
                    return rows, source
            cap = self.limits.max_bytes - self.usage["bytes"]
            params: dict[str, str | int] = {"per_page": self.limits.per_page, "page": page}
            if checks:
                params["filter"] = "all"
            self.usage["requests"] += 1
            source["pages"] += 1
            try:
                response = self.transport.get(path, params=params, max_bytes=cap)
            except Exception:
                # Never retain exception text, which may contain URLs or credentials.
                _reason(source, "transport-unavailable")
                return rows, source
            if (
                not isinstance(response, GetResponse)
                or type(response.status) is not int
                or not 100 <= response.status <= 599
                or not isinstance(response.body, bytes)
                or type(response.truncated) is not bool
            ):
                _reason(source, "malformed-response")
                return rows, source
            source["http_status"] = response.status
            consumed = min(len(response.body), cap)
            self.usage["bytes"] += consumed
            source["bytes"] += consumed
            if response.status != 200:
                _reason(
                    source,
                    {403: "access-denied", 404: "unavailable", 429: "rate-limited"}.get(
                        response.status, "http-unavailable"
                    ),
                )
            if len(response.body) > cap or response.truncated:
                _reason(source, "byte-budget")
                _reason(source, "truncated-response")
            if not source["complete"]:
                return rows, source
            try:
                payload = json.loads(response.body, object_pairs_hook=_pairs, parse_constant=_constant)
                if checks and not isinstance(payload, dict):
                    raise ValueError("not a check envelope")
                items = payload.get("check_runs") if checks else payload
                if not isinstance(items, list):
                    raise ValueError("not a list")
                if checks:
                    page_total = _count(payload.get("total_count"))
                    if page_total is None or (total is not None and page_total != total):
                        raise ValueError("invalid or changing total")
                    total = page_total
                fingerprint = hashlib.sha256(json.dumps(items, sort_keys=True).encode()).hexdigest()
            except (ValueError, TypeError, UnicodeError, RecursionError):
                _reason(source, "malformed-payload")
                return rows, source
            if items and fingerprint in seen_pages:
                _reason(source, "repeated-page")
                return rows, source
            seen_pages.add(fingerprint)
            for item in items:
                if self.usage["objects"] >= self.limits.max_objects:
                    _reason(source, "object-budget")
                    return rows, source
                self.usage["objects"] += 1
                source["objects"] += 1
                if isinstance(item, dict):
                    rows.append(item)
                    if checks:
                        identity = _object_id(item.get("id"))
                        if identity is None:
                            _reason(source, "malformed-object-id")
                        elif identity in seen_check_ids:
                            _reason(source, "overlapping-pagination")
                        else:
                            seen_check_ids.add(identity)
                else:
                    rows.append({})  # Preserve the annotation ordinal of malformed items.
                    _reason(source, "malformed-object")
            if len(items) > self.limits.per_page:
                _reason(source, "oversized-page")
                return rows, source
            if total is not None and source["objects"] >= total:
                if len(seen_check_ids) != total:
                    _reason(source, "inconsistent-total")
                return rows, source
            if len(items) < self.limits.per_page:
                if total is not None and len(seen_check_ids) != total:
                    _reason(source, "truncated-page")
                return rows, source
        _reason(source, "page-budget")
        return rows, source


def _dedupe(rows: list[dict[str, Any]], source: dict[str, Any]) -> list[dict[str, Any]]:
    seen: dict[int, dict[str, Any]] = {}
    for row in rows:
        identity = _object_id(row.get("id"))
        if identity is None:
            _reason(source, "malformed-object-id")
        elif identity in seen:
            source["duplicate_objects"] += 1
            if row != seen[identity]:
                _reason(source, "conflicting-object")
        else:
            seen[identity] = row
    return list(seen.values())


def _time(value: object, observed: str, source: dict[str, Any]) -> str | None:
    parsed = _timestamp(value)
    if value is not None and parsed is None:
        _reason(source, "malformed-timestamp")
    if parsed is not None and datetime.fromisoformat(parsed) > datetime.fromisoformat(observed):
        _reason(source, "future-timestamp")
        return None
    return parsed


def _location(row: dict[str, Any], *, annotation: bool = False) -> dict[str, Any] | None:
    path = row.get("path")
    if not isinstance(path, str) or not _PATH.fullmatch(path) or any(p in {"", ".", ".."} for p in path.split("/")):
        return None
    end = _object_id(row.get("end_line" if annotation else "original_line", row.get("line")))
    raw_start = row.get("start_line" if annotation else "original_start_line")
    start = _object_id(raw_start) if annotation or raw_start is not None else end
    if start is None or end is None or start > end:
        return None
    if not annotation and row.get("side") not in ("RIGHT", "LEFT"):
        return None
    location = {"path": path, "start_line": start, "end_line": end}
    if not annotation:
        location["side"] = row["side"]
        start_side = row.get("start_side")
        if start_side is not None:
            if start_side not in ("RIGHT", "LEFT"):
                return None
            location["start_side"] = start_side
    return location


def _url(value: object, owner: str, repo: str, paths: set[str], fragments: set[str]) -> str | None:
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) <= 32 or ord(c) >= 127 for c in value):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.netloc != "github.com" or "?" in value or "%" in value:
        return None
    prefix = f"/{owner}/{repo}/"
    if (
        not parsed.path.startswith(prefix)
        or parsed.path[len(prefix) :] not in paths
        or parsed.fragment not in fragments
    ):
        return None
    return value


def _coverage(head: str | None, current: str) -> str:
    return "unknown" if head is None else "current" if head == current else "stale"


def collect_managed_review(
    transport: GetTransport,
    *,
    owner: str,
    repo: str,
    pr_number: int,
    current_head: str,
    expected_app_id: int,
    expected_app_slug: str,
    observed_at: str,
    limits: Limits | None = None,
) -> dict[str, Any]:
    """Return a sanitized JSON-compatible observation, never a merge decision.

    Inputs are independently trusted canonical repository, PR, head and app.
    Unattributed review/inline records are quarantined with explicit identity
    disposition, never promoted using a login or an unsupported app field.
    """
    current = _head(current_head)
    observed = _timestamp(observed_at)
    if (
        not isinstance(owner, str)
        or not _OWNER.fullmatch(owner)
        or not isinstance(repo, str)
        or not _REPO.fullmatch(repo)
        or repo in {".", ".."}
        or _object_id(pr_number) is None
        or current is None
        or observed is None
        or _object_id(expected_app_id) is None
        or not isinstance(expected_app_slug, str)
        or not _SLUG.fullmatch(expected_app_slug)
        or expected_app_slug == "github-actions"
    ):
        raise ValueError("canonical repository, positive PR, full head, trusted app and observation time are required")
    reader = _Reader(transport, limits or Limits())
    base = f"/repos/{owner}/{repo}"
    checks: list[dict[str, Any]] = []
    annotations: list[dict[str, Any]] = []
    rows, source = reader.read(f"{base}/commits/{current}/check-runs", "check-runs", checks=True)
    for row in _dedupe(rows, source):
        normalized = normalize_check_run(
            row,
            expected_app_id=expected_app_id,
            expected_app_slug=expected_app_slug,
            current_head=current,
            observed_at=observed,
        )
        if normalized is None:
            continue
        record = asdict(normalized)
        record["identity_key"] = normalized.identity_key
        record["started_at"] = _time(row.get("started_at"), observed, source)
        record["completed_at"] = _time(row.get("completed_at"), observed, source)
        bad_time = any(row.get(key) is not None and record[key] is None for key in ("started_at", "completed_at"))
        record["current_findings_known"] = normalized.current_findings_known and not bad_time
        record["current_zero_findings"] = normalized.current_zero_findings and not bad_time
        record["source_url"] = _url(
            normalized.source_url,
            owner,
            repo,
            {f"runs/{normalized.check_id}", f"check-runs/{normalized.check_id}"},
            {""},
        )
        if record["source_url"] is None:
            record["source_url"] = _url(
                row.get("html_url"),
                owner,
                repo,
                {f"runs/{normalized.check_id}", f"check-runs/{normalized.check_id}"},
                {""},
            )
        output = row.get("output")
        annotation_count = output.get("annotations_count") if isinstance(output, dict) else None
        record["annotations_expected"] = _count(annotation_count)
        if annotation_count is not None and record["annotations_expected"] is None:
            _reason(source, "malformed-annotations-count")
        record["superseded_by"] = None
        record["supersession_disposition"] = "unknown"
        if normalized.coverage != "current":
            _reason(source, "unexpected-reviewed-head")
        if normalized.lifecycle == "unknown":
            _reason(source, "unknown-lifecycle")
        checks.append(record)
    # Use start time, never completion arrival or maximum check id. Ties and
    # missing start times cannot establish ordering. Coverage remains per head.
    for record in checks:
        newer = [
            c
            for c in checks
            if c["reviewed_head"] == record["reviewed_head"]
            and c["started_at"] is not None
            and record["started_at"] is not None
            and datetime.fromisoformat(c["started_at"]) > datetime.fromisoformat(record["started_at"])
        ]
        peers = [c for c in checks if c["reviewed_head"] == record["reviewed_head"]]
        if record["started_at"] is not None and all(c["started_at"] is not None for c in peers):
            record["supersession_disposition"] = "latest-observed" if not newer else "superseded"
            if any(c is not record and c["started_at"] == record["started_at"] for c in peers):
                record["supersession_disposition"] = "unknown"
        if newer:
            record["current_findings_known"] = False
            record["current_zero_findings"] = False
            earliest = min(datetime.fromisoformat(c["started_at"]) for c in newer)
            candidates = [c for c in newer if datetime.fromisoformat(c["started_at"]) == earliest]
            if len(candidates) == 1:
                record["superseded_by"] = candidates[0]["check_id"]
    for record in checks:
        if not source["complete"] or record["supersession_disposition"] == "unknown":
            record["current_findings_known"] = False
            record["current_zero_findings"] = False
    for record in checks:
        check_id = record["check_id"]
        rows, source = reader.read(f"{base}/check-runs/{check_id}/annotations", f"annotations:{check_id}")
        expected_count = record["annotations_expected"]
        if expected_count is not None and expected_count != source["objects"]:
            _reason(source, "annotations-count-mismatch")
        for ordinal, row in enumerate(rows, 1):
            location = _location(row, annotation=True)
            level = row.get("annotation_level")
            if location is None or level not in ("notice", "warning", "failure"):
                _reason(source, "malformed-annotation")
            annotations.append(
                {
                    "identity_key": f"github:check-run:{expected_app_id}:{check_id}:annotation:{ordinal}",
                    "identity_kind": "parent-ordinal-location",
                    "check_id": check_id,
                    "ordinal": ordinal,
                    "location": location,
                    "annotation_level": level if level in ("notice", "warning", "failure") else None,
                    "reviewed_head": record["reviewed_head"],
                    "coverage": record["coverage"],
                    "observed_at": observed,
                    "source_url": record["source_url"],
                    "ambiguity": "no-universal-immutable-id",
                    "parser_disposition": "location-only",
                }
            )
    reviews: list[dict[str, Any]] = []
    comments: list[dict[str, Any]] = []
    for kind, path, destination in (
        ("reviews", f"{base}/pulls/{pr_number}/reviews", reviews),
        ("inline-comments", f"{base}/pulls/{pr_number}/comments", comments),
        ("issue-comments", f"{base}/issues/{pr_number}/comments", comments),
    ):
        rows, source = reader.read(path, kind)
        for row in _dedupe(rows, source):
            identity = row["id"]
            app = row.get("performed_via_github_app")
            user = row.get("user")
            # This app metadata is supported for issue comments. It is not a
            # documented identity binding on review or inline-comment schemas.
            trusted = (
                kind == "issue-comments"
                and isinstance(app, dict)
                and isinstance(user, dict)
                and user.get("type") == "Bot"
                and _object_id(app.get("id")) == expected_app_id
                and app.get("slug") == expected_app_slug
            )
            if not trusted:
                _reason(source, "identity-unverified")
            head = (
                _head(row.get("original_commit_id" if kind == "inline-comments" else "commit_id"))
                if kind != "issue-comments"
                else None
            )
            fragment = (
                f"pullrequestreview-{identity}"
                if kind == "reviews"
                else f"discussion_r{identity}"
                if kind == "inline-comments"
                else f"issuecomment-{identity}"
            )
            fragments = {fragment}
            if kind == "inline-comments":
                fragments.add(f"discussion-diff-{identity}")
            state = row.get("state")
            record = {
                "identity_key": f"github:{kind}:{identity}",
                "provider_id": identity,
                "source": kind,
                "provider_identity": "verified-app" if trusted else "unverified",
                "app_id": expected_app_id if trusted else None,
                "app_slug": expected_app_slug if trusted else None,
                "reviewed_head": head,
                "current_head": current,
                "coverage": _coverage(head, current),
                "observed_at": observed,
                "created_at": _time(row.get("created_at"), observed, source),
                "updated_at": _time(row.get("updated_at"), observed, source),
                "submitted_at": _time(row.get("submitted_at"), observed, source),
                "state": state if isinstance(state, str) and state in _STATES and kind == "reviews" else None,
                "source_url": _url(row.get("html_url"), owner, repo, {f"pull/{pr_number}"}, fragments),
                "location": _location(row) if kind == "inline-comments" else None,
                "review_id": _object_id(row.get("pull_request_review_id")) if kind == "inline-comments" else None,
                "thread_disposition": "not-collected",
                "parser_disposition": "not-parsed",
            }
            destination.append(record)
    # Location alone does not prove equivalence, especially for unverified
    # inline authors. Keep every representation and never add their counts.
    for annotation in annotations:
        location = annotation["location"]
        if location is not None and (
            any(
                other is not annotation
                and other["location"] == location
                and other["reviewed_head"] == annotation["reviewed_head"]
                for other in annotations
            )
            or any(
                c["location"] is not None
                and all(c["location"].get(key) == value for key, value in location.items())
                and c["reviewed_head"] == annotation["reviewed_head"]
                for c in comments
            )
        ):
            annotation["ambiguity"] = "possible-duplicate-representation"
    reasons = [f"{s['resource']}:{reason}" for s in reader.sources for reason in s["reasons"]]
    return {
        "provider": "github",
        "repository": f"{owner}/{repo}",
        "pr_number": pr_number,
        "app_id": expected_app_id,
        "app_slug": expected_app_slug,
        "current_head": current,
        "observed_at": observed,
        "checks": checks,
        "annotations": annotations,
        "reviews": reviews,
        "comments": comments,
        "finding_totals": None,
        "complete": not reasons,
        "incomplete_reasons": reasons,
        "sources": reader.sources,
        "usage": reader.usage,
        "limits": asdict(reader.limits),
        "remaining_acceptance": [
            "verified-review-and-inline-app-identity",
            "graphql-thread-disposition",
            "cross-observation-history",
            "hub-worklore-json-fleethub-integration",
        ],
    }
