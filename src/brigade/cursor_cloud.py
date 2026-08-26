"""Cursor Cloud public-beta adapter (#Task3).

Uses only stdlib ``urllib``. All provider credentials travel as Basic auth
(username is the API key; password is empty). Exceptions are sanitized: they
never include the key, the Authorization header, a response body, the prompt, or
a presigned artifact URL.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

DEFAULT_BASE_URL = "https://api.cursor.com"
DEFAULT_DEADLINE = 10.0
DEFAULT_MAX_PAGES = 5
DEFAULT_MAX_ITEMS = 250

# Cursor Cloud Agents API v1 run states.
_ACTIVE_STATES = frozenset({"creating", "running"})
_TERMINAL_STATES = frozenset({"finished", "error", "cancelled", "expired"})


class CursorCloudError(RuntimeError):
    """Raised on Cursor Cloud failures; message carries no secrets or bodies."""


@dataclass(frozen=True)
class LaunchResult:
    ok: bool
    agent_id: str | None = None
    run_id: str | None = None
    reason: str | None = None


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
    """True for states that are finished or failed."""
    return normalize_state(state) in _TERMINAL_STATES


_GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_GITHUB_URL_RE = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/?$")


def normalize_repo(repo: str | None) -> str | None:
    """Return an absolute https://github.com/ URL, or None for unsafe inputs.

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


def _client_agent_id() -> str:
    """Return a stable per-launch client agent id in the Cursor ``bc-<uuid>`` form."""
    return f"bc-{uuid4().hex}"


def _basic_auth_header(api_key: str) -> str:
    token = base64.b64encode(f"{api_key}:".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _build_request(url: str, api_key: str, *, method: str = "GET", data: bytes | None = None) -> urllib.request.Request:
    request = urllib.request.Request(url, method=method, data=data)
    request.add_header("Authorization", _basic_auth_header(api_key))
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    return request


def _read_response(opener, request: urllib.request.Request, deadline: float) -> dict[str, Any]:
    """Return a decoded JSON object, raising the original transport exception.

    HTTPError and URLError are re-raised so callers can distinguish a provider
    refusal (4xx/5xx) from an ambiguous transport failure. Size and JSON errors
    are raised as ``CursorCloudError`` because the provider did respond.
    """
    try:
        response = opener(request, timeout=deadline)
    except (urllib.error.HTTPError, urllib.error.URLError):
        raise
    raw = response.read(64 * 1024 + 1)
    if len(raw) > 64 * 1024:
        raise CursorCloudError("Cursor Cloud response exceeded the size limit")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CursorCloudError("Cursor Cloud response was not valid JSON") from exc
    if not isinstance(payload, dict):
        raise CursorCloudError("Cursor Cloud response was not a JSON object")
    return payload


def _call(opener, request: urllib.request.Request, deadline: float) -> dict[str, Any]:
    try:
        return _read_response(opener, request, deadline)
    except CursorCloudError:
        raise
    except urllib.error.HTTPError as exc:
        # Sanitize: never re-read or include the response body.
        raise CursorCloudError("Cursor Cloud request failed") from exc
    except urllib.error.URLError as exc:
        raise CursorCloudError("Cursor Cloud request failed") from exc
    except Exception as exc:
        raise CursorCloudError("Cursor Cloud request failed") from exc


def _default_opener(request: urllib.request.Request, timeout: float | None = None):
    return urllib.request.urlopen(request, timeout=timeout)


def get_run(
    agent_id: str,
    run_id: str,
    api_key: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    deadline: float = DEFAULT_DEADLINE,
    opener=None,
) -> dict[str, Any]:
    """GET /v1/agents/{agent_id}/runs/{run_id}."""
    url = f"{base_url.rstrip('/')}/v1/agents/{agent_id}/runs/{run_id}"
    payload = _call(opener or _default_opener, _build_request(url, api_key), deadline=deadline)
    return {
        "id": payload.get("id") if isinstance(payload.get("id"), str) else run_id,
        "state": normalize_state(payload.get("status") or payload.get("state")),
    }


def list_agents(
    api_key: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    deadline: float = DEFAULT_DEADLINE,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_items: int = DEFAULT_MAX_ITEMS,
    active_only: bool = False,
    opener=None,
) -> list[dict[str, Any]]:
    """GET /v1/agents with bounded pagination, resolving latestRunId via the run endpoint.

    Returns a list of sanitized agent rows. Each row contains at least ``id``,
    ``name``, ``latestRunId``, and ``latestRunState`` (when resolved).
    """
    opener = opener or _default_opener
    items_seen = 0
    cursor: str | None = None
    agents: list[dict[str, Any]] = []
    for _ in range(max(1, max_pages)):
        url = f"{base_url.rstrip('/')}/v1/agents"
        if cursor:
            url += f"?cursor={urllib.parse.quote(cursor)}"
        payload = _call(opener, _build_request(url, api_key), deadline=deadline)
        raw_agents = payload.get("items")
        if not isinstance(raw_agents, list):
            raise CursorCloudError("Cursor Cloud /v1/agents response missing 'items' array")
        next_cursor = payload.get("nextCursor")
        for raw in raw_agents:
            if not isinstance(raw, dict):
                continue
            agent_id = raw.get("id")
            if not isinstance(agent_id, str):
                continue
            name = raw.get("name")
            latest_run_id = raw.get("latestRunId")
            row: dict[str, Any] = {
                "id": agent_id,
                "name": name if isinstance(name, str) else None,
                "latestRunId": latest_run_id if isinstance(latest_run_id, str) else None,
                "latestRunState": None,
            }
            if row["latestRunId"]:
                try:
                    run = get_run(
                        agent_id, row["latestRunId"], api_key, base_url=base_url, deadline=deadline, opener=opener
                    )
                    row["latestRunState"] = run.get("state")
                except CursorCloudError:
                    # Preserve the agent row even if the run fetch fails.
                    pass
            agents.append(row)
            items_seen += 1
            if items_seen >= max_items:
                break
        if items_seen >= max_items:
            break
        if not isinstance(next_cursor, str) or not next_cursor:
            break
        cursor = next_cursor
    if active_only:
        agents = [a for a in agents if is_active_state(a.get("latestRunState"))]
    return agents


def launch_agent(
    api_key: str,
    *,
    repo: str,
    prompt: str,
    base_url: str = DEFAULT_BASE_URL,
    deadline: float = DEFAULT_DEADLINE,
    auto_create_pr: bool = False,
    opener=None,
) -> LaunchResult:
    """Admit a lease, create a Cursor agent/run, and bind the returned IDs.

    ``repos[].url`` must be an absolute ``https://github.com/owner/repo`` URL;
    ``owner/repo`` is normalized. The Cursor Agents API v1 accepts an optional
    client-supplied ``agentId`` in ``bc-<uuid>`` form for idempotent create; this
    adapter generates one per launch and sends it before POST.

    POST /v1/agents body is ``{"agentId": ..., "prompt": {"text": ...},
    "repos": [{"url": ...}], "autoCreatePR": ...}``. The create response is
    ``{"agent": {"id", "latestRunId", ...}, "run": {"id", "status", ...}}``;
    both identifiers are bound.

    A provider HTTP 4xx/5xx releases the unbound lease as ``submit-failed``.
    A transport timeout, URLError, OSError, TimeoutError, or a malformed /
    oversized response is ambiguous after POST: the lease is held and the result
    reports ``uncertain`` with the client ``agentId`` preserved as the provider
    task id so a later observation or adoption can resolve it.
    """
    from . import cloud_tracker, fleet_client

    canonical_repo = normalize_repo(repo)
    if not canonical_repo:
        return LaunchResult(ok=False, reason="bad-repo")

    prompt_hash = cloud_tracker.prompt_hash(prompt)
    admit = fleet_client.admit_cloud(
        "cursor-cloud",
        repo=canonical_repo,
        label=prompt[:120],
        prompt_hash=prompt_hash,
        ttl_seconds=300,
    )
    if not admit.granted:
        return LaunchResult(ok=False, reason=admit.reason)

    lease_id = admit.lease.get("lease_id") if isinstance(admit.lease, dict) else None
    if not isinstance(lease_id, str):
        return LaunchResult(ok=False, reason="no-lease")
    holder = admit.holder

    client_agent_id = _client_agent_id()
    url = f"{base_url.rstrip('/')}/v1/agents"
    body = {
        "agentId": client_agent_id,
        "prompt": {"text": prompt},
        "repos": [{"url": canonical_repo}],
        "autoCreatePR": bool(auto_create_pr),
    }
    data = json.dumps(body).encode("utf-8")
    try:
        payload = _read_response(
            opener or _default_opener, _build_request(url, api_key, method="POST", data=data), deadline=deadline
        )
    except urllib.error.HTTPError:
        # Provider refused before creating the agent/run; capacity is safe to release.
        fleet_client.release_cloud(lease_id, state="submit-failed", holder=holder)
        return LaunchResult(ok=False, reason="submit-failed")
    except urllib.error.URLError:
        # The request may have landed. Hold the lease and surface an uncertain state.
        return LaunchResult(ok=False, reason="uncertain", agent_id=client_agent_id)
    except TimeoutError:
        return LaunchResult(ok=False, reason="uncertain", agent_id=client_agent_id)
    except OSError:
        return LaunchResult(ok=False, reason="uncertain", agent_id=client_agent_id)
    except CursorCloudError:
        # Malformed or oversized response after a POST is ambiguous: the provider may
        # have accepted the agent. Keep the lease and preserve the client agent id.
        return LaunchResult(ok=False, reason="uncertain", agent_id=client_agent_id)

    raw_agent = payload.get("agent")
    agent = raw_agent if isinstance(raw_agent, dict) else {}
    raw_run = payload.get("run")
    run = raw_run if isinstance(raw_run, dict) else {}
    raw_agent_id = agent.get("id")
    agent_id = raw_agent_id if isinstance(raw_agent_id, str) else client_agent_id
    raw_run_id = run.get("id") or agent.get("latestRunId")
    run_id = raw_run_id if isinstance(raw_run_id, str) else None
    if run_id is None:
        # The provider responded but did not return a usable run id. Treat as ambiguous.
        return LaunchResult(ok=False, reason="uncertain", agent_id=agent_id)

    bind = fleet_client.bind_cloud(lease_id, provider_task_id=agent_id, artifact_ref=run_id, holder=holder)
    if not bind.granted:
        # The agent/run exist, but the hub could not bind them. The lease is still
        # held; the operator can inspect and release manually. Do not release here
        # because the provider work is live and unbound.
        return LaunchResult(ok=False, reason="bind-failed", agent_id=agent_id, run_id=run_id)

    return LaunchResult(ok=True, agent_id=agent_id, run_id=run_id, reason="ok")
