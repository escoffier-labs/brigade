"""Bounded Actions snapshots through an injected, read-only GET transport.

No discovery or live client is supplied. The caller independently confirms each
run attempt's immutable definition applicability. The transport owns blocking
timeouts, authorization and routing, with no redirects or identity fallback.
Elapsed checks reject late responses but cannot interrupt a blocking call.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

from brigade.claude_actions_evidence import (
    MAX_PROVIDER_ID,
    ConfirmedWorkflow,
    _provider_id,
    _repository,
    _sha,
    _timestamp,
    normalize_run,
)
from brigade.claude_review_import import GetResponse, GetTransport

_EVENTS = {
    "check_run",
    "check_suite",
    "create",
    "delete",
    "deployment",
    "deployment_status",
    "discussion",
    "discussion_comment",
    "fork",
    "gollum",
    "issue_comment",
    "issues",
    "label",
    "merge_group",
    "milestone",
    "page_build",
    "public",
    "pull_request",
    "pull_request_review",
    "pull_request_review_comment",
    "pull_request_target",
    "push",
    "registry_package",
    "release",
    "repository_dispatch",
    "schedule",
    "status",
    "watch",
    "workflow_call",
    "workflow_dispatch",
    "workflow_run",
}


@dataclass(frozen=True)
class Limits:
    """Shared collection budgets, plus per-endpoint pages and per-body bounds."""

    max_pages: int = 10
    max_requests: int = 50
    max_objects: int = 1000
    max_bytes: int = 4 * 1024 * 1024
    max_body_bytes: int = 512 * 1024
    per_page: int = 100
    max_json_depth: int = 32
    max_json_nodes: int = 20000
    max_elapsed_seconds: float = 30

    def __post_init__(self) -> None:
        for value, ceiling in (
            (self.max_pages, 100),
            (self.max_requests, 1000),
            (self.max_objects, 10000),
            (self.max_bytes, 16 * 1024 * 1024),
            (self.max_body_bytes, 1024 * 1024),
            (self.per_page, 100),
            (self.max_json_depth, 64),
            (self.max_json_nodes, 100000),
        ):
            if type(value) is not int or not 0 < value <= ceiling:
                raise ValueError("read limits require positive bounded integers")
        if (
            type(self.max_elapsed_seconds) not in (int, float)
            or not math.isfinite(self.max_elapsed_seconds)
            or not 0 < self.max_elapsed_seconds <= 300
        ):
            raise ValueError("elapsed limit requires positive bounded seconds")


class FixtureTransport:
    """Offline default: missing fixture resources are unavailable, never empty."""

    def __init__(self, responses: Mapping[tuple[str, int], GetResponse] | None = None):
        self.responses = responses or {}

    def get(self, path: str, *, params: Mapping[str, str | int], max_bytes: int) -> GetResponse:
        page = params.get("page", 1)
        return self.responses.get((path, int(page)), GetResponse(404, b""))


def _integer(text: str) -> int:
    if len(text.lstrip("-")) > 19:
        raise ValueError("integer bound")
    value = int(text)
    if not -MAX_PROVIDER_ID <= value <= MAX_PROVIDER_ID:
        raise ValueError("integer bound")
    return value


def _unsupported_number(text: str) -> Any:
    raise ValueError("unsupported number")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _payload(body: bytes, limits: Limits) -> dict[str, Any]:
    # Bound nesting before invoking the recursive stdlib decoder, including
    # unknown fields. Quotes and escaped backslashes cannot disguise nesting.
    depth = 0
    quoted = escaped = False
    for byte in body:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
        elif byte in (91, 123):
            depth += 1
            if depth > limits.max_json_depth:
                raise ValueError("depth bound")
        elif byte in (93, 125):
            depth -= 1
    value = json.loads(
        body.decode("utf-8"),
        parse_int=_integer,
        parse_float=_unsupported_number,
        parse_constant=_unsupported_number,
        object_pairs_hook=_pairs,
    )
    if not isinstance(value, dict):
        raise ValueError("object required")
    pending: list[Any] = [value]
    visited = 0
    while pending:
        item = pending.pop()
        visited += 1
        if visited > limits.max_json_nodes:
            raise ValueError("node bound")
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return value


class _Reader:
    def __init__(self, transport: GetTransport, limits: Limits, clock: Callable[[], float]):
        self.transport = transport
        self.limits = limits
        self.clock = clock
        self.started = self.clock()
        self.usage = {"requests": 0, "objects": 0, "bytes": 0}
        self.reasons: list[str] = []
        self.sources: list[dict[str, Any]] = []

    def reason(self, reason: str, source: dict[str, Any] | None = None) -> None:
        if reason not in self.reasons:
            self.reasons.append(reason)
        if source is not None:
            source["complete"] = False
            if reason not in source["reasons"]:
                source["reasons"].append(reason)

    def elapsed(self, source: dict[str, Any]) -> bool:
        try:
            now = self.clock()
            valid = (
                type(now) in (int, float)
                and type(self.started) in (int, float)
                and math.isfinite(now)
                and math.isfinite(self.started)
                and 0 <= now - self.started < self.limits.max_elapsed_seconds
            )
        except Exception:
            valid = False
        if not valid:
            self.reason("elapsed-budget", source)
        return valid

    def object(self, source: dict[str, Any]) -> bool:
        if self.usage["objects"] >= self.limits.max_objects:
            self.reason("object-budget", source)
            return False
        self.usage["objects"] += 1
        source["objects"] += 1
        return True

    def read(self, path: str, *, page: int | None = None) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        source: dict[str, Any] = {
            "source_url": f"https://api.github.com{path}",
            "page": page,
            "complete": True,
            "reasons": [],
            "http_status": None,
            "objects": 0,
            "bytes": 0,
        }
        self.sources.append(source)
        if not self.elapsed(source):
            return None, source
        for dimension, limit, reason in (
            ("requests", self.limits.max_requests, "request-budget"),
            ("objects", self.limits.max_objects, "object-budget"),
            ("bytes", self.limits.max_bytes, "byte-budget"),
        ):
            if self.usage[dimension] >= limit:
                self.reason(reason, source)
                return None, source
        cap = min(self.limits.max_body_bytes, self.limits.max_bytes - self.usage["bytes"])
        params: dict[str, str | int] = {} if page is None else {"page": page, "per_page": self.limits.per_page}
        self.usage["requests"] += 1
        try:
            response = self.transport.get(path, params=params, max_bytes=cap)
        except Exception:
            self.elapsed(source)
            self.reason("transport-unavailable", source)
            return None, source
        if not self.elapsed(source):
            return None, source
        if (
            not isinstance(response, GetResponse)
            or type(response.status) is not int
            or not 100 <= response.status <= 599
            or not isinstance(response.body, bytes)
            or type(response.truncated) is not bool
        ):
            self.reason("malformed-response", source)
            return None, source
        source["http_status"] = response.status
        source["bytes"] = min(cap, len(response.body))
        self.usage["bytes"] += source["bytes"]
        if response.status != 200:
            self.reason(
                {403: "access-denied", 404: "unavailable", 429: "rate-limited"}.get(
                    response.status, "http-unavailable"
                ),
                source,
            )
        if len(response.body) > cap or response.truncated:
            self.reason("truncated-response", source)
            if len(response.body) > cap:
                self.reason("byte-budget", source)
        if not source["complete"]:
            return None, source
        try:
            return _payload(response.body, self.limits), source
        except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
            self.reason("malformed-payload", source)
            return None, source


def _identity(row: dict[str, Any], repository: str, workflow_id: int) -> tuple[int, int] | None:
    run_id, attempt = _provider_id(row.get("id")), _provider_id(row.get("run_attempt"))
    repo = row.get("repository")
    if (
        run_id is None
        or attempt is None
        or _provider_id(row.get("workflow_id")) != workflow_id
        or not isinstance(repo, dict)
        or _repository(repo.get("full_name")) != repository
    ):
        return None
    return run_id, attempt


def collect_actions(
    transport: GetTransport | None = None,
    *,
    repository: str,
    workflow_id: int,
    current_head_sha: str,
    observed_at: str,
    confirmations: Mapping[tuple[int, int], ConfirmedWorkflow] | None = None,
    limits: Limits | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Read one workflow's bounded snapshots without inferring applicability.

    Confirmations are independent caller attestations for exact run/attempts,
    including the immutable definition revision that applied to each. No
    definitions are accepted here. No current SHA, name, metadata read or other
    attempt can create a confirmation. Results describe this poll only, with no
    persistence, last-writer refresh or complete-absence assertion.
    """
    canonical, current, observed = _repository(repository), _sha(current_head_sha), _timestamp(observed_at)
    if canonical is None or _provider_id(workflow_id) is None or current is None or observed is None:
        raise ValueError("canonical repository, workflow ID, full head and observation time are required")
    bounds = limits if limits is not None else Limits()
    if not isinstance(bounds, Limits):
        raise ValueError("bounded read limits are required")
    supplied = confirmations if confirmations is not None else {}
    if not isinstance(supplied, Mapping) or len(supplied) > bounds.max_objects:
        raise ValueError("bounded per-attempt confirmations are required")
    trusted: dict[tuple[int, int], ConfirmedWorkflow] = {}
    for identity, confirmation in supplied.items():
        if (
            not isinstance(identity, tuple)
            or len(identity) != 2
            or any(_provider_id(i) is None for i in identity)
            or not isinstance(confirmation, ConfirmedWorkflow)
            or confirmation.repository != canonical
            or confirmation.workflow_id != workflow_id
        ):
            raise ValueError("confirmation must match repository, workflow and run attempt")
        trusted[identity] = confirmation
    reader = _Reader(transport if transport is not None else FixtureTransport(), bounds, clock)
    base = f"/repos/{canonical}/actions"
    workflow_path = f"{base}/workflows/{workflow_id}"
    workflow: dict[str, Any] | None = None
    metadata, source = reader.read(workflow_path)
    if metadata is not None and reader.object(source):
        if _provider_id(metadata.get("id")) != workflow_id:
            reader.reason("workflow-identity-mismatch", source)
        else:
            state = metadata.get("state")
            workflow = {
                "workflow_id": workflow_id,
                "state": state
                if state in ("active", "disabled_manually", "disabled_inactivity", "deleted")
                else "unknown",
                "authority": "github-workflow-metadata",
                "source_url": source["source_url"],
                "revision": None,
                "observed_at": observed,
                "created_at": _timestamp(metadata.get("created_at")),
                "updated_at": _timestamp(metadata.get("updated_at")),
                "applicability": "unknown",
            }

    rows: dict[tuple[int, int], dict[str, Any]] = {}
    row_sources: dict[tuple[int, int], dict[str, Any]] = {}
    conflicts: set[tuple[int, int]] = set()
    quarantined: list[dict[str, Any]] = []
    duplicates = 0

    def quarantine(row: dict[str, Any], reason: str) -> None:
        quarantined.append(
            {
                "run_id": _provider_id(row.get("id")),
                "run_attempt": _provider_id(row.get("run_attempt")),
                "reason": reason,
                "applicability": "unknown",
                "observed_at": observed,
            }
        )

    def accept(row: dict[str, Any], source: dict[str, Any]) -> tuple[int, int] | None:
        nonlocal duplicates
        identity = _identity(row, canonical, workflow_id)
        if identity is None:
            reader.reason("run-identity-mismatch", source)
            quarantine(row, "run-identity-mismatch")
        elif identity in rows:
            duplicates += 1
            # JSON preserves boolean/integer distinctions that Python equality loses.
            if json.dumps(rows[identity], sort_keys=True) != json.dumps(row, sort_keys=True):
                conflicts.add(identity)
                reader.reason("conflicting-identity", source)
        else:
            rows[identity] = row
            row_sources[identity] = source
        return identity

    seen_pages: set[str] = set()
    run_ids: set[int] = set()
    total: int | None = None
    for page in range(1, bounds.max_pages + 1):
        payload, source = reader.read(f"{workflow_path}/runs", page=page)
        if payload is None:
            break
        items, count = payload.get("workflow_runs"), payload.get("total_count")
        if (
            not isinstance(items, list)
            or type(count) is not int
            or not 0 <= count <= MAX_PROVIDER_ID
            or (total is not None and total != count)
            or any(not isinstance(item, dict) for item in items)
        ):
            reader.reason("malformed-payload", source)
            break
        total = count
        if len(items) > bounds.per_page:
            reader.reason("oversized-page", source)
            break
        fingerprint = hashlib.sha256(json.dumps(items, sort_keys=True).encode()).hexdigest()
        if items and fingerprint in seen_pages:
            reader.reason("repeated-page", source)
            break
        seen_pages.add(fingerprint)
        for row in items:
            if not reader.object(source):
                break
            accepted_identity = accept(row, source)
            if accepted_identity is not None:
                run_ids.add(accepted_identity[0])
        if not source["complete"]:
            break
        if len(run_ids) >= total:
            if len(run_ids) != total:
                reader.reason("inconsistent-total", source)
            break
        if len(items) < bounds.per_page:
            reader.reason("pagination-truncated", source)
            break
        if page == bounds.max_pages:
            reader.reason("page-budget", source)

    # The list reports latest attempts only. Expand earlier attempts under the
    # same shared budgets. A denied/missing attempt cannot become an empty run.
    for run_id, latest in list(rows):
        for attempt in range(1, latest):
            if (run_id, attempt) in rows:
                continue
            row, source = reader.read(f"{base}/runs/{run_id}/attempts/{attempt}")
            if row is None or not reader.object(source):
                break
            if _identity(row, canonical, workflow_id) != (run_id, attempt):
                reader.reason("attempt-identity-mismatch", source)
                quarantine(row, "attempt-identity-mismatch")
                break
            accept(row, source)

    runs: list[dict[str, Any]] = []
    for identity, row in rows.items():
        if identity in conflicts:
            quarantine(row, "conflicting-identity")
            continue
        applied_confirmation = trusted.get(identity)
        if applied_confirmation is None:
            quarantine(row, "unconfirmed-applicability")
            continue
        # Do not retain arbitrary provider branch text, which can contain private
        # content. All other exports pass through the normalization authority.
        raw_event = row.get("event")
        event = raw_event if isinstance(raw_event, str) and raw_event in _EVENTS else None
        safe_row = dict(row, head_branch=None, event=event)
        evidence = normalize_run(
            safe_row, workflow=applied_confirmation, observed_at=observed, current_head_sha=current
        )
        record = asdict(evidence)
        record["identity_key"] = f"github-actions:{canonical}:{identity[0]}:{identity[1]}"
        record["authority"] = "caller-confirmed-definition-and-github-run"
        record["applicability"] = "confirmed"
        record["source_url"] = row_sources[identity]["source_url"]
        record["source_page"] = row_sources[identity]["page"]
        runs.append(record)
    return {
        "provider": "github-actions",
        "repository": canonical,
        "workflow_id": workflow_id,
        "current_head_sha": current,
        "observed_at": observed,
        "workflow": workflow,
        "runs": runs,
        "quarantined": quarantined,
        "current_head_identities": [r["identity_key"] for r in runs if r["head_relation"] == "current-head"],
        "duplicate_objects": duplicates,
        "complete": not reader.reasons,
        "incomplete_reasons": reader.reasons,
        "absence": "unknown",
        "sources": reader.sources,
        "usage": reader.usage,
        "limits": asdict(bounds),
    }
