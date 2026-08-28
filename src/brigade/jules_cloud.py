"""Jules Cloud alpha adapter (Task 4).

Uses only stdlib ``urllib``. Credentials travel as ``X-Goog-Api-Key``.

Every provider payload is sanitized before it leaves this module: only IDs,
normalized states, bounded RFC 3339 timestamps, the source owner/repo/branches
needed for matching, a validated Jules session URL, and a validated GitHub pull
request URL survive. Prompts, titles, descriptions, activity payloads,
artifacts, patches, shell output, raw response bodies, and credentials are
dropped. Exceptions carry none of those either.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

DEFAULT_BASE_URL = "https://jules.googleapis.com/v1alpha"
DEFAULT_DEADLINE = 10.0
DEFAULT_PAGE_SIZE = 50
DEFAULT_MAX_PAGES = 5
DEFAULT_MAX_ITEMS = 250
_DEFAULT_SCHEME_PORTS = {"http": 80, "https": 443}

# Strict paging bounds. A caller may tighten these but never loosen them.
MAX_PAGE_SIZE = 100
MAX_PAGES_LIMIT = 20
MAX_ITEMS_LIMIT = 1000
MAX_RESPONSE_BYTES = 64 * 1024

# The only automation mode this adapter will ever send.
AUTO_CREATE_PR = "AUTO_CREATE_PR"

# Jules session states from the v1alpha API.
_TERMINAL_STATES = frozenset({"failed", "completed"})
# All non-terminal Jules states consume capacity until explicitly terminal.
_ACTIVE_STATES = frozenset(
    {
        "queued",
        "planning",
        "awaiting_plan_approval",
        "awaiting_user_feedback",
        "in_progress",
        "paused",
        "state_unspecified",
    }
)


class JulesCloudError(RuntimeError):
    """Raised on Jules Cloud failures; message carries no secrets or bodies."""


@dataclass(frozen=True)
class LaunchResult:
    ok: bool
    session_id: str | None = None
    reason: str | None = None
    source_name: str | None = None
    starting_branch: str | None = None


def normalize_state(value: object) -> str | None:
    """Return a lower-cased state string, or None for empty/unknown inputs."""
    if not isinstance(value, str):
        return None
    word = value.strip().lower()
    return word or None


def is_active_state(state: str | None) -> bool:
    """True for states that should hold capacity."""
    return normalize_state(state) in _ACTIVE_STATES


def is_terminal_state(state: str | None) -> bool:
    """True for terminal Jules states."""
    return normalize_state(state) in _TERMINAL_STATES


_GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_GITHUB_URL_RE = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}/?$")
_GITHUB_PR_URL_RE = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}/pull/[0-9]{1,12}$")
_JULES_SESSION_URL_RE = re.compile(r"^https://jules\.google\.com/[A-Za-z0-9/_-]{0,128}$")
_SOURCE_NAME_RE = re.compile(r"^sources/(?:[A-Za-z0-9_.-]{1,64}/){0,3}[A-Za-z0-9_.-]{1,64}$")
_OWNER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$")

_MAX_BRANCHES = 200


def normalize_repo(repo: str | None) -> str | None:
    """Return an absolute https://github.com/owner/repo URL, or None for unsafe inputs.

    Accepts ``owner/repo`` and already-absolute ``https://github.com/owner/repo``
    strings. Rejects non-GitHub hosts, ``http://``, and other unsafe forms.
    """
    if not isinstance(repo, str):
        return None
    stripped = repo.strip()
    if not stripped:
        return None
    if _GITHUB_URL_RE.match(stripped):
        return stripped.rstrip("/")
    if _GITHUB_REPO_RE.match(stripped):
        return f"https://github.com/{stripped}"
    return None


def split_repo(repo: str | None) -> tuple[str, str] | None:
    """Return ``(owner, name)`` for a repo this adapter accepts, else None."""
    canonical = normalize_repo(repo)
    if not canonical:
        return None
    owner, _, name = canonical.removeprefix("https://github.com/").partition("/")
    if not owner or not name:
        return None
    return owner, name


def _normalize_base_url(base_url: str) -> str:
    """Return a bounded, scheme-checked base URL.

    ``https`` is required for real hosts; plain ``http`` is accepted only for
    loopback so tests can point the adapter at a local stub.
    """
    if not isinstance(base_url, str) or not base_url.strip():
        raise JulesCloudError("base_url must be a non-empty string")
    trimmed = base_url.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(trimmed)
    if not parsed.netloc:
        raise JulesCloudError("base_url must include a host")
    if parsed.query or parsed.fragment:
        raise JulesCloudError("base_url must not carry a query or fragment")
    loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme == "https" or (parsed.scheme == "http" and loopback):
        return trimmed
    raise JulesCloudError("base_url must use https (http is allowed only for loopback)")


def _validate_session_id(session_id: object) -> str:
    """Return a path-safe session id, rejecting empty and slash-bearing input."""
    if not isinstance(session_id, str):
        raise JulesCloudError("session id must be a string")
    token = session_id.strip()
    if not token:
        raise JulesCloudError("session id must not be empty")
    if "/" in token:
        raise JulesCloudError("session id must not contain a path separator")
    return token


def _quote_segment(value: str) -> str:
    """Percent-encode a single path segment; ``safe=''`` keeps slashes escaped."""
    return urllib.parse.quote(value, safe="")


def _validate_automation_mode(value: object) -> str | None:
    """Return None or ``AUTO_CREATE_PR``; every other value is rejected."""
    if value is None:
        return None
    if isinstance(value, str) and value.strip() == AUTO_CREATE_PR:
        return AUTO_CREATE_PR
    raise JulesCloudError("automation_mode must be omitted or AUTO_CREATE_PR")


def _validate_source_name(value: object) -> str:
    if not isinstance(value, str) or not _SOURCE_NAME_RE.match(value.strip()):
        raise JulesCloudError("source_name must be a connected 'sources/<id>' name")
    return value.strip()


def _validate_branch(value: object) -> str:
    if not isinstance(value, str) or not _BRANCH_RE.match(value.strip()):
        raise JulesCloudError("starting_branch must be a bounded branch name")
    return value.strip()


def _bounded_timestamp(value: object) -> str | None:
    """Return an RFC 3339 timestamp string, or None when it is not one."""
    if not isinstance(value, str):
        return None
    token = value.strip()
    if len(token) > 40 or not _TIMESTAMP_RE.match(token):
        return None
    return token


def _validated_session_url(value: object) -> str | None:
    if isinstance(value, str) and _JULES_SESSION_URL_RE.match(value.strip()):
        return value.strip()
    return None


def _validated_pr_url(value: object) -> str | None:
    if isinstance(value, str) and _GITHUB_PR_URL_RE.match(value.strip()):
        return value.strip()
    return None


def _id_from_name(value: object, prefix: str) -> str | None:
    """Return the trailing id of a ``<prefix>/<id>`` resource name."""
    if not isinstance(value, str):
        return None
    token = value.strip()
    if not token.startswith(prefix):
        return None
    tail = token[len(prefix) :]
    if not tail or "/" in tail:
        return None
    return tail


def _find_pr_url(raw: dict[str, Any]) -> str | None:
    """Look for a GitHub PR URL in the shapes the alpha API has used."""
    candidates: list[Any] = [raw.get("pullRequestUrl")]
    pull_request = raw.get("pullRequest")
    if isinstance(pull_request, dict):
        candidates.append(pull_request.get("url"))
    outputs = raw.get("outputs")
    if isinstance(outputs, list):
        for output in outputs[:20]:
            if not isinstance(output, dict):
                continue
            candidates.append(output.get("pullRequestUrl"))
            nested = output.get("pullRequest")
            if isinstance(nested, dict):
                candidates.append(nested.get("url"))
    for candidate in candidates:
        validated = _validated_pr_url(candidate)
        if validated:
            return validated
    return None


def sanitize_session(raw: object) -> dict[str, Any] | None:
    """Reduce a session payload to the fields this adapter is allowed to keep."""
    if not isinstance(raw, dict):
        return None
    session_id = raw.get("id")
    if not isinstance(session_id, str) or not session_id.strip():
        session_id = _id_from_name(raw.get("name"), "sessions/")
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    return {
        "id": session_id.strip(),
        "state": normalize_state(raw.get("state")),
        "create_time": _bounded_timestamp(raw.get("createTime")),
        "update_time": _bounded_timestamp(raw.get("updateTime")),
        "url": _validated_session_url(raw.get("url")),
        "pull_request_url": _find_pr_url(raw),
    }


def sanitize_activity(raw: object) -> dict[str, Any] | None:
    """Keep an activity's id and timestamp only; payloads and text are dropped."""
    if not isinstance(raw, dict):
        return None
    activity_id = raw.get("id")
    if not isinstance(activity_id, str) or not activity_id.strip():
        activity_id = _id_from_name(raw.get("name"), "activities/")
    if not isinstance(activity_id, str) or not activity_id.strip():
        return None
    return {
        "id": activity_id.strip(),
        "create_time": _bounded_timestamp(raw.get("createTime")),
    }


def _sanitize_branches(raw: object) -> list[str]:
    """Return bounded branch names from the shapes the alpha API has used."""
    if not isinstance(raw, list):
        return []
    names: list[str] = []
    for item in raw[:_MAX_BRANCHES]:
        candidate: object = None
        if isinstance(item, str):
            candidate = item
        elif isinstance(item, dict):
            candidate = item.get("displayName") or item.get("name")
        if not isinstance(candidate, str):
            continue
        token = candidate.strip()
        if _BRANCH_RE.match(token) and token not in names:
            names.append(token)
    return names


def _sanitize_default_branch(raw: object) -> str | None:
    candidate: object = raw
    if isinstance(raw, dict):
        candidate = raw.get("displayName") or raw.get("name")
    if not isinstance(candidate, str):
        return None
    token = candidate.strip()
    return token if _BRANCH_RE.match(token) else None


def sanitize_source(raw: object) -> dict[str, Any] | None:
    """Keep only the connected-source fields needed to match and target a repo."""
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    if not isinstance(name, str) or not _SOURCE_NAME_RE.match(name.strip()):
        return None
    github = raw.get("githubRepo")
    if not isinstance(github, dict):
        return None
    owner = github.get("owner")
    repo = github.get("repo")
    if not isinstance(owner, str) or not _OWNER_RE.match(owner.strip()):
        return None
    if not isinstance(repo, str) or not _OWNER_RE.match(repo.strip()):
        return None
    branches = _sanitize_branches(github.get("branches"))
    default_branch = _sanitize_default_branch(github.get("defaultBranch"))
    if default_branch and default_branch not in branches and len(branches) < _MAX_BRANCHES:
        branches.append(default_branch)
    return {
        "name": name.strip(),
        "owner": owner.strip(),
        "repo": repo.strip(),
        "branches": branches,
        "default_branch": default_branch,
    }


def _build_request(url: str, api_key: str, *, method: str = "GET", data: bytes | None = None) -> urllib.request.Request:
    request = urllib.request.Request(url, method=method, data=data)
    request.add_header("X-Goog-Api-Key", api_key)
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    return request


def _default_opener(request: urllib.request.Request, timeout: float | None = None):
    return _JULES_OPENER.open(request, timeout=timeout)


def _origin(url: str) -> tuple[str, str, int | None]:
    split = urllib.parse.urlsplit(url)
    try:
        port = split.port
    except ValueError:
        port = -1
    scheme = split.scheme.lower()
    return scheme, (split.hostname or "").lower(), _DEFAULT_SCHEME_PORTS.get(scheme) if port is None else port


class _JulesRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Only follow redirects that retain the exact secure origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _origin(req.full_url) != _origin(newurl):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_JULES_OPENER = urllib.request.build_opener(_JulesRedirectHandler(), urllib.request.ProxyHandler({}))


def _read_response(
    opener,
    request: urllib.request.Request,
    deadline: float,
    *,
    allow_empty: bool = False,
) -> dict[str, Any]:
    """Return a decoded JSON object, raising the original transport exception.

    HTTPError and URLError are re-raised so callers can distinguish a provider
    refusal (4xx/5xx) from an ambiguous transport failure. Size and JSON errors
    are raised as ``JulesCloudError`` because the provider did respond. With
    ``allow_empty`` an empty success body decodes to ``{}``.
    """
    try:
        response = opener(request, timeout=deadline)
    except (urllib.error.HTTPError, urllib.error.URLError):
        raise
    raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise JulesCloudError("Jules Cloud response exceeded the size limit")
    if allow_empty and not raw.strip():
        return {}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JulesCloudError("Jules Cloud response was not valid JSON") from exc
    if not isinstance(payload, dict):
        raise JulesCloudError("Jules Cloud response was not a JSON object")
    return payload


def _call(
    opener,
    request: urllib.request.Request,
    deadline: float,
    *,
    allow_empty: bool = False,
) -> dict[str, Any]:
    try:
        return _read_response(opener, request, deadline, allow_empty=allow_empty)
    except JulesCloudError:
        raise
    except urllib.error.HTTPError as exc:
        # Sanitize: never re-read or include the response body.
        raise JulesCloudError("Jules Cloud request failed") from exc
    except urllib.error.URLError as exc:
        raise JulesCloudError("Jules Cloud request failed") from exc
    except Exception as exc:
        raise JulesCloudError("Jules Cloud request failed") from exc


def _check_paging(page_size: int, max_pages: int, max_items: int) -> None:
    if not isinstance(page_size, int) or isinstance(page_size, bool) or not 1 <= page_size <= MAX_PAGE_SIZE:
        raise JulesCloudError(f"page_size must be between 1 and {MAX_PAGE_SIZE}")
    if not isinstance(max_pages, int) or isinstance(max_pages, bool) or not 1 <= max_pages <= MAX_PAGES_LIMIT:
        raise JulesCloudError(f"max_pages must be between 1 and {MAX_PAGES_LIMIT}")
    if not isinstance(max_items, int) or isinstance(max_items, bool) or not 1 <= max_items <= MAX_ITEMS_LIMIT:
        raise JulesCloudError(f"max_items must be between 1 and {MAX_ITEMS_LIMIT}")


def _paginated_list(
    endpoint: str,
    api_key: str,
    *,
    list_key: str,
    sanitizer,
    base_url: str = DEFAULT_BASE_URL,
    opener=None,
    deadline: float = DEFAULT_DEADLINE,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> list[dict[str, Any]]:
    """Bounded paginated GET returning sanitized rows only."""
    _check_paging(page_size, max_pages, max_items)
    root = _normalize_base_url(base_url)
    opener = opener or _default_opener
    cursor: str | None = None
    items: list[dict[str, Any]] = []
    for _ in range(max_pages):
        query = {"pageSize": str(page_size)}
        if cursor:
            query["pageToken"] = cursor
        url = f"{root}/{endpoint}?{urllib.parse.urlencode(query)}"
        payload = _call(opener, _build_request(url, api_key), deadline=deadline)
        raw_items = payload.get(list_key)
        if not isinstance(raw_items, list):
            raise JulesCloudError(f"Jules Cloud /{endpoint} response missing '{list_key}' array")
        next_cursor = payload.get("nextPageToken")
        for raw in raw_items:
            if len(items) >= max_items:
                break
            row = sanitizer(raw)
            if row is not None:
                items.append(row)
        if len(items) >= max_items:
            break
        if not isinstance(next_cursor, str) or not next_cursor:
            break
        cursor = next_cursor
    return items


def list_sources(
    api_key: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    opener=None,
    deadline: float = DEFAULT_DEADLINE,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> list[dict[str, Any]]:
    """GET /sources. Read-only; returns sanitized connected-source rows."""
    return _paginated_list(
        "sources",
        api_key,
        list_key="sources",
        sanitizer=sanitize_source,
        base_url=base_url,
        opener=opener,
        deadline=deadline,
        page_size=page_size,
        max_pages=max_pages,
        max_items=max_items,
    )


def resolve_source(
    api_key: str,
    repo: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    opener=None,
    deadline: float = DEFAULT_DEADLINE,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> dict[str, Any] | None:
    """Return the connected source whose ``githubRepo`` matches ``repo``.

    Matching is exact and case-insensitive on both ``owner`` and ``repo``; a
    partial or fuzzy match is never accepted. Returns None when the requested
    repository is not connected, which callers must treat as "do not admit and
    do not mutate the provider".
    """
    parts = split_repo(repo)
    if parts is None:
        return None
    owner, name = parts
    sources = list_sources(
        api_key,
        base_url=base_url,
        opener=opener,
        deadline=deadline,
        page_size=page_size,
        max_pages=max_pages,
        max_items=max_items,
    )
    for source in sources:
        if source["owner"].lower() == owner.lower() and source["repo"].lower() == name.lower():
            return source
    return None


def select_branch(source: dict[str, Any], requested: str | None = None) -> str | None:
    """Validate ``requested`` against the source, or pick an unambiguous default.

    A requested branch must appear in the source's listed branches. With no
    request, the source default wins, then ``main``, then a sole listed branch.
    Anything ambiguous returns None so the caller refuses to launch.
    """
    branches = source.get("branches") if isinstance(source, dict) else None
    branches = branches if isinstance(branches, list) else []
    default_branch = source.get("default_branch") if isinstance(source, dict) else None
    if requested is not None:
        token = requested.strip() if isinstance(requested, str) else ""
        return token if token and token in branches else None
    if isinstance(default_branch, str) and default_branch in branches:
        return default_branch
    if "main" in branches:
        return "main"
    if len(branches) == 1:
        return branches[0]
    return None


def list_sessions(
    api_key: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    opener=None,
    deadline: float = DEFAULT_DEADLINE,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> list[dict[str, Any]]:
    """GET /sessions with bounded pagination; rows are sanitized."""
    return _paginated_list(
        "sessions",
        api_key,
        list_key="sessions",
        sanitizer=sanitize_session,
        base_url=base_url,
        opener=opener,
        deadline=deadline,
        page_size=page_size,
        max_pages=max_pages,
        max_items=max_items,
    )


def get_session(
    session_id: str,
    api_key: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    opener=None,
    deadline: float = DEFAULT_DEADLINE,
) -> dict[str, Any]:
    """GET /sessions/{id}, returning the sanitized session row."""
    token = _validate_session_id(session_id)
    root = _normalize_base_url(base_url)
    url = f"{root}/sessions/{_quote_segment(token)}"
    payload = _call(opener or _default_opener, _build_request(url, api_key), deadline=deadline)
    session = sanitize_session(payload)
    if session is None:
        raise JulesCloudError("Jules Cloud session response had no usable id")
    return session


def list_activities(
    session_id: str,
    api_key: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    opener=None,
    deadline: float = DEFAULT_DEADLINE,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> list[dict[str, Any]]:
    """GET /sessions/{id}/activities with bounded pagination; rows are sanitized."""
    token = _validate_session_id(session_id)
    return _paginated_list(
        f"sessions/{_quote_segment(token)}/activities",
        api_key,
        list_key="activities",
        sanitizer=sanitize_activity,
        base_url=base_url,
        opener=opener,
        deadline=deadline,
        page_size=page_size,
        max_pages=max_pages,
        max_items=max_items,
    )


def create_session(
    api_key: str,
    *,
    prompt: str,
    source_name: str,
    starting_branch: str,
    title: str | None = None,
    require_plan_approval: bool = True,
    automation_mode: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    opener=None,
    deadline: float = DEFAULT_DEADLINE,
) -> dict[str, Any]:
    """POST /sessions against an already-resolved connected source.

    ``source_name`` must be the exact ``sources/<id>`` name returned by
    ``GET /sources`` and ``starting_branch`` must already be validated against
    that source; this helper does no discovery of its own. ``requirePlanApproval``
    defaults to true and ``automationMode`` is omitted unless explicitly set to
    ``AUTO_CREATE_PR``. The create response must carry a top-level ``id``.
    This low-level helper re-raises ``urllib.error.HTTPError`` so callers can
    distinguish a provider refusal from an ambiguous transport failure.
    """
    canonical_source = _validate_source_name(source_name)
    canonical_branch = _validate_branch(starting_branch)
    mode = _validate_automation_mode(automation_mode)
    root = _normalize_base_url(base_url)
    url = f"{root}/sessions"
    body: dict[str, Any] = {
        "prompt": prompt,
        "sourceContext": {
            "source": canonical_source,
            "githubRepoContext": {"startingBranch": canonical_branch},
        },
        "requirePlanApproval": bool(require_plan_approval),
    }
    if title is not None:
        body["title"] = title
    if mode is not None:
        body["automationMode"] = mode
    data = json.dumps(body).encode("utf-8")
    payload = _read_response(
        opener or _default_opener,
        _build_request(url, api_key, method="POST", data=data),
        deadline=deadline,
    )
    raw_id = payload.get("id")
    if not isinstance(raw_id, str) or not raw_id.strip():
        raise JulesCloudError("Jules Cloud create response had no top-level id")
    session = sanitize_session(payload)
    if session is None:
        raise JulesCloudError("Jules Cloud create response had no usable id")
    return session


def approve_plan(
    session_id: str,
    api_key: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    opener=None,
    deadline: float = DEFAULT_DEADLINE,
) -> dict[str, Any]:
    """POST /sessions/{id}:approvePlan with an empty JSON body.

    A success with an empty body is normal for this method and decodes to ``{}``.
    """
    token = _validate_session_id(session_id)
    root = _normalize_base_url(base_url)
    url = f"{root}/sessions/{_quote_segment(token)}:approvePlan"
    _call(
        opener or _default_opener,
        _build_request(url, api_key, method="POST", data=b"{}"),
        deadline=deadline,
        allow_empty=True,
    )
    # The response body carries nothing this adapter is allowed to keep.
    return {}


def _preflight_registry(register_target: Path | None) -> bool:
    """Confirm registry-directory durability without reading or rewriting its live file."""
    if register_target is None:
        return True
    from . import cloud_tracker

    descriptor: int | None = None
    created_sidecar: Path | None = None
    try:
        registry_dir = cloud_tracker.registry_path(Path(register_target)).parent
        registry_dir.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        for _ in range(4):
            candidate = registry_dir / f".registry-preflight-{uuid4().hex}"
            try:
                descriptor = os.open(os.fspath(candidate), flags, 0o600)
            except FileExistsError:
                continue
            created_sidecar = candidate
            break
        if descriptor is None:
            return False
        written = os.write(descriptor, b"brigade-registry-preflight\n")
        if written != len(b"brigade-registry-preflight\n"):
            return False
        os.fsync(descriptor)
    except Exception:
        return False
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                return False
        if created_sidecar is not None:
            try:
                created_sidecar.unlink()
            except OSError:
                return False
    return True


def _register_bound_launch(
    register_target: Path | None,
    *,
    task_id: str,
    label: str | None,
    prompt_hash: str,
    lease_holder: str | None,
    repo: str | None,
) -> bool:
    """Persist bound IDs and the private holder. Never raise back into launch."""
    if register_target is None:
        return True
    from . import cloud_tracker

    if isinstance(label, str) and label.strip():
        resolved_label = label.strip()
    else:
        resolved_label = cloud_tracker.lease_label("jules", repo, prompt_hash)
    try:
        cloud_tracker.register(
            Path(register_target),
            provider="jules",
            task_id=task_id,
            label=resolved_label,
            prompt_hash=prompt_hash,
            session_id=task_id,
            expected_artifact={"kind": "diff"},
            lease_holder=lease_holder,
        )
    except Exception:
        return False
    return True


def launch_agent(
    api_key: str,
    *,
    repo: str,
    prompt: str,
    title: str | None = None,
    starting_branch: str | None = None,
    auto_create_pr: bool = False,
    base_url: str = DEFAULT_BASE_URL,
    opener=None,
    deadline: float = DEFAULT_DEADLINE,
    register_target: Path | None = None,
    label: str | None = None,
) -> LaunchResult:
    """Resolve the connected source, admit a lease, create a session, bind the id.

    Order matters. ``GET /sources`` runs first and is read-only: if the requested
    ``owner/repo`` is not connected, or the branch cannot be validated against
    that source, there is no admission and no provider mutation. Only then is a
    lease admitted and ``POST /sessions`` issued with the exact connected
    ``sources/<id>`` name.

    The lease label is derived from provider, repo, and prompt hash. Prompt text
    is never persisted as a label.

    ``requirePlanApproval`` is always true and ``automationMode`` is omitted
    unless ``auto_create_pr`` is True. A provider HTTP 4xx/5xx releases the
    unbound lease as ``submit-failed``. A transport timeout, ``URLError``,
    ``OSError``, ``TimeoutError``, or malformed/oversized response after POST is
    ambiguous: the lease is held and the result reports ``uncertain``. Exactly
    one POST is issued in every path; an ambiguous result is never retried here.
    """
    from . import cloud_tracker, fleet_client

    canonical_repo = normalize_repo(repo)
    if not canonical_repo:
        return LaunchResult(ok=False, reason="bad-repo")
    if not _preflight_registry(register_target):
        return LaunchResult(ok=False, reason="registry-unwritable")

    # Read-only source resolution, before any admission or mutation.
    try:
        source = resolve_source(
            api_key,
            canonical_repo,
            base_url=base_url,
            opener=opener,
            deadline=deadline,
        )
    except JulesCloudError:
        return LaunchResult(ok=False, reason="source-lookup-failed")
    if source is None:
        return LaunchResult(ok=False, reason="source-not-found")

    branch = select_branch(source, starting_branch)
    if branch is None:
        return LaunchResult(ok=False, reason="bad-branch", source_name=source["name"])

    prompt_hash = cloud_tracker.prompt_hash(prompt)
    admit = fleet_client.admit_cloud(
        "jules",
        repo=canonical_repo,
        label=cloud_tracker.lease_label("jules", canonical_repo, prompt_hash),
        prompt_hash=prompt_hash,
        ttl_seconds=300,
    )
    if not admit.granted:
        return LaunchResult(ok=False, reason=admit.reason)

    lease_id = admit.lease.get("lease_id") if isinstance(admit.lease, dict) else None
    if not isinstance(lease_id, str):
        return LaunchResult(ok=False, reason="no-lease")
    holder = admit.holder

    automation_mode = AUTO_CREATE_PR if auto_create_pr else None
    try:
        session = create_session(
            api_key,
            prompt=prompt,
            source_name=source["name"],
            starting_branch=branch,
            title=title,
            require_plan_approval=True,
            automation_mode=automation_mode,
            base_url=base_url,
            opener=opener,
            deadline=deadline,
        )
    except urllib.error.HTTPError:
        # Provider refused before creating the session; capacity is safe to release.
        fleet_client.release_cloud(lease_id, state="submit-failed", holder=holder)
        return LaunchResult(ok=False, reason="submit-failed", source_name=source["name"], starting_branch=branch)
    except urllib.error.URLError:
        # The request may have landed. Hold the lease and surface an uncertain state.
        return LaunchResult(ok=False, reason="uncertain", source_name=source["name"], starting_branch=branch)
    except TimeoutError:
        return LaunchResult(ok=False, reason="uncertain", source_name=source["name"], starting_branch=branch)
    except OSError:
        return LaunchResult(ok=False, reason="uncertain", source_name=source["name"], starting_branch=branch)
    except JulesCloudError:
        # Malformed, oversized, or id-less response after a POST is ambiguous: the
        # provider may have accepted the session. Keep the lease; do not retry.
        return LaunchResult(ok=False, reason="uncertain", source_name=source["name"], starting_branch=branch)

    session_id = session["id"]
    bind = fleet_client.bind_cloud(lease_id, provider_task_id=session_id, holder=holder)
    if not bind.granted:
        # The session exists, but the hub could not bind it. The lease is still
        # held; the operator can inspect and release manually. Do not release here
        # because the provider work is live and unbound.
        return LaunchResult(
            ok=False,
            reason="bind-failed",
            session_id=session_id,
            source_name=source["name"],
            starting_branch=branch,
        )

    if not _register_bound_launch(
        register_target,
        task_id=session_id,
        label=label,
        prompt_hash=prompt_hash,
        lease_holder=holder,
        repo=canonical_repo,
    ):
        return LaunchResult(
            ok=False,
            reason="tracking-failed",
            session_id=session_id,
            source_name=source["name"],
            starting_branch=branch,
        )
    return LaunchResult(
        ok=True,
        session_id=session_id,
        reason="ok",
        source_name=source["name"],
        starting_branch=branch,
    )
