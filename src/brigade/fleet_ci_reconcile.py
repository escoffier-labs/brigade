"""Bounded node-only GET repair of repository CI facts through GitHubReader."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from . import fleet_ci_board as board
from .claude_actions_evidence import _text
from .claude_review_evidence import _head, _object_id, _timestamp
from .github_reader import GitHubReader

PAGE_SIZE = 100
MAX_PAGES = 2
MAX_HEADS = 21
MAX_BYTES = 2 * 1024 * 1024


class Unavailable(ValueError):
    """A constant reason, never provider error bodies or raw diagnostics."""


def _get(reader: GitHubReader, suffix: str, **params: str | int) -> Any:
    try:
        response = reader.get(f"/repos/{reader.repository}{suffix}", params=params, max_bytes=MAX_BYTES, max_age=120)
    except (ValueError, OSError):
        raise Unavailable("transport-unavailable") from None
    if response.status != 200 or response.truncated or response.state not in {"ok", "not-modified"}:
        raise Unavailable(
            "truncated" if response.truncated else response.state if response.state != "ok" else "http-unavailable"
        )
    try:
        return json.loads(response.body), response.headers
    except (ValueError, TypeError):
        raise Unavailable("malformed-response") from None


def _pages(reader: GitHubReader, suffix: str, *, key: str | None = None, **params: str | int) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        raw, headers = _get(reader, suffix, per_page=PAGE_SIZE, page=page, **params)
        values = raw.get(key) if key and isinstance(raw, dict) else raw if key is None else None
        if not isinstance(values, list) or len(values) > PAGE_SIZE or any(not isinstance(v, dict) for v in values):
            raise Unavailable("malformed-page")
        result.extend(values)
        count = raw.get("total_count") if isinstance(raw, dict) else None
        more = 'rel="next"' in headers.get("link", "")
        if count is not None and (type(count) is not int or count < len(result)):
            raise Unavailable("malformed-page-count")
        if not more and len(values) < PAGE_SIZE:
            if count is not None and count > len(result):
                raise Unavailable("incomplete-pagination")
            return result
    raise Unavailable("incomplete-pagination")


def _required(reader: GitHubReader, branch: str, *, protected: bool | None) -> list[dict[str, Any]]:
    branch_path = quote(branch, safe="")
    rules, headers = _get(reader, f"/rules/branches/{branch_path}", per_page=100)
    if not isinstance(rules, list) or len(rules) >= 100 or 'rel="next"' in headers.get("link", ""):
        raise Unavailable("required-scope-unknown")
    if protected is False and not rules:
        return []
    classic, _ = _get(reader, f"/branches/{branch_path}/protection/required_status_checks")
    if not isinstance(classic, dict):
        raise Unavailable("required-scope-unknown")
    checks = classic.get("checks")
    contexts = classic.get("contexts")
    if not isinstance(checks, list) or not isinstance(contexts, list):
        raise Unavailable("required-scope-unknown")
    result = []
    for item in checks:
        if not isinstance(item, dict) or _text(item.get("context"), 255) is None:
            raise Unavailable("required-scope-unknown")
        # GitHub's null/-1 means any app; app-bound requirements stay bound.
        app = item.get("app_id")
        if app not in (None, -1) and _object_id(app) is None:
            raise Unavailable("required-scope-unknown")
        result.append({"context": item["context"], "app_id": None if app == -1 else app})
    for context in contexts:
        if _text(context, 255) is None:
            raise Unavailable("required-scope-unknown")
        if not any(c["context"] == context for c in result):
            result.append({"context": context, "app_id": None})
    for rule in rules:
        if not isinstance(rule, dict):
            raise Unavailable("required-scope-unknown")
        if rule.get("type") != "required_status_checks":
            continue
        params = rule.get("parameters")
        if not isinstance(params, dict) or not isinstance(params.get("required_status_checks"), list):
            raise Unavailable("required-scope-unknown")
        for item in params["required_status_checks"]:
            if not isinstance(item, dict) or _text(item.get("context"), 255) is None:
                raise Unavailable("required-scope-unknown")
            app = item.get("integration_id")
            if app is not None and _object_id(app) is None:
                raise Unavailable("required-scope-unknown")
            requirement = {"context": item["context"], "app_id": app}
            if requirement not in result:
                result.append(requirement)
    if len(result) > 500:
        raise Unavailable("required-scope-unknown")
    return result


def _provider_event(repo: str, kind: str, raw: dict[str, Any], *, head: str | None = None) -> dict[str, Any]:
    identity = {"workflow_run": "run_id", "check_run": "check_run_id", "status": "status_id"}[kind]
    app = raw.get("app")
    return dict(
        repo=repo,
        event=kind,
        **{identity: raw.get("id")},
        run_attempt=raw.get("run_attempt", 1),
        head_sha=raw.get("head_sha") or head,
        status=raw.get("status") if kind != "status" else raw.get("state"),
        conclusion=raw.get("conclusion"),
        name=raw.get("name") or raw.get("context"),
        app_id=app.get("id") if isinstance(app, dict) else None,
        created_at=raw.get("created_at") or raw.get("started_at") or raw.get("completed_at"),
        updated_at=raw.get("updated_at") or raw.get("completed_at") or raw.get("started_at") or raw.get("created_at"),
    )


def reconcile_repo(
    reader: GitHubReader, *, now: str, previous: dict[str, Any] | None = None, refresh_id: str | None = None
) -> dict[str, Any]:
    now = board.stamp(now)
    snapshot: dict[str, Any] = dict(
        repo=reader.repository,
        visibility="unknown",
        default_branch=None,
        head_sha=None,
        observed_at=now,
        required_checks=[],
        required_observed_at=None,
        required_complete=False,
        checks_complete=[],
        check_ids={},
        pulls=[],
        pulls_complete=False,
        complete=False,
        reason=None,
        refresh_id=refresh_id,
    )
    events: list[dict[str, Any]] = []
    errors = []
    try:
        meta, _ = _get(reader, "")
        if (
            not isinstance(meta, dict)
            or type(meta.get("private")) is not bool
            or _text(meta.get("default_branch"), 255) is None
        ):
            raise Unavailable("metadata-scope-unknown")
        snapshot["visibility"] = "private" if meta["private"] else "public"
        if meta["private"]:
            reader.visibility = "private"
        branch = snapshot["default_branch"] = meta["default_branch"]
        ref, _ = _get(reader, f"/branches/{quote(branch, safe='')}")
        sha = (
            _head(ref.get("commit", {}).get("sha"))
            if isinstance(ref, dict) and isinstance(ref.get("commit"), dict)
            else None
        )
        if sha is None:
            raise Unavailable("head-scope-unknown")
        snapshot["head_sha"] = sha
    except Unavailable as exc:
        snapshot["reason"] = str(exc)
        return {"source": "github-reconcile", "events": [], "repositories": [snapshot]}

    cached = previous or {}
    required_time = cached.get("required_observed_at")
    from datetime import datetime

    age = (
        None
        if not required_time
        else (
            datetime.fromisoformat(now.replace("Z", "+00:00"))
            - datetime.fromisoformat(required_time.replace("Z", "+00:00"))
        ).total_seconds()
    )
    if (
        age is not None
        and 0 <= age < 86400
        and cached.get("required_complete")
        and cached.get("default_branch") == branch
    ):
        for field in ("required_checks", "required_observed_at", "required_complete"):
            snapshot[field] = cached[field]
        if not cached["required_complete"]:
            errors.append("required-scope-unknown")
    else:
        try:
            snapshot["required_checks"] = _required(reader, branch, protected=ref.get("protected"))
            snapshot["required_complete"] = True
            snapshot["required_observed_at"] = now
        except Unavailable as exc:
            errors.append(str(exc))
    try:
        runs, _ = _get(reader, "/actions/runs", branch=branch, per_page=1)
        if (
            not isinstance(runs, dict)
            or not isinstance(runs.get("workflow_runs"), list)
            or len(runs["workflow_runs"]) > 1
        ):
            raise Unavailable("workflow-scope-unknown")
        for run in runs["workflow_runs"]:
            event = _provider_event(reader.repository, "workflow_run", run)
            if run.get("head_branch") not in (None, branch):
                raise Unavailable("workflow-branch-unknown")
            event["head_branch"] = branch
            board.normalize_event(event, now=now)
            events.append(event)
            snapshot["latest_run_id"] = event["run_id"]
            snapshot["latest_run_attempt"] = event["run_attempt"]
    except (Unavailable, ValueError, TypeError, AttributeError):
        errors.append("workflow-scope-unknown")
    try:
        prs = _pages(reader, "/pulls", state="open")
        for pr in prs:
            head = pr.get("head")
            if _object_id(pr.get("number")) is None or not isinstance(head, dict) or _head(head.get("sha")) is None:
                raise Unavailable("PR-scope-unknown")
            snapshot["pulls"].append(
                {"number": pr["number"], "head_sha": _head(head["sha"]), "updated_at": _timestamp(pr.get("updated_at"))}
            )
        snapshot["pulls_complete"] = True
    except Unavailable as exc:
        errors.append(str(exc))
    heads = list(dict.fromkeys([sha, *(p["head_sha"] for p in snapshot["pulls"])]))
    if len(heads) > MAX_HEADS:
        errors.append("head-budget-exhausted")
    for head in heads[:MAX_HEADS]:
        try:
            pending = []
            for kind, suffix, key in [("check_run", "check-runs", "check_runs"), ("status", "statuses", None)]:
                for raw in _pages(reader, f"/commits/{head}/{suffix}", key=key):
                    event = _provider_event(reader.repository, kind, raw, head=head)
                    board.normalize_event(event, now=now)
                    if event["head_sha"] != head:
                        raise Unavailable("check-head-mismatch")
                    pending.append(event)
            if len(events) + len(pending) > board.MAX_EVENTS:
                raise Unavailable("event-budget-exhausted")
            events.extend(pending)
            snapshot["checks_complete"].append(head)
            snapshot["check_ids"][head] = [
                f"{e['event']}:{e[board.EVENT_IDS[e['event']]]}:{e['run_attempt']}" for e in pending
            ]
        except (Unavailable, ValueError) as exc:
            errors.append(str(exc) if isinstance(exc, Unavailable) else "check-scope-unknown")
    snapshot["complete"] = not errors
    snapshot["reason"] = errors[0] if errors else None
    return {"source": "github-reconcile", "events": events, "repositories": [snapshot]}
