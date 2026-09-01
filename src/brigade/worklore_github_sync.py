"""Discover labeled GitHub issues for Worklore imports."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

from . import toml_compat as tomllib
from . import worklore_client, worklore_store
from .fleet_client import FLEET_CONFIG_REL_PATH, brigade_home
from .worklore_github import (
    GitHubObservationError,
    bounded_acceptance,
    bounded_title,
    normalize_issue,
)

SEARCH_TIMEOUT_SECONDS = 30
DEFAULT_LABEL = "burn-queue"
DEFAULT_STATE = "all"
ORG_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
EXTERNAL_KEY_RE = re.compile(r"^([^/#]+)/([^/#]+)#(\d+)$")
SEARCH_JSON_FIELDS = "number,title,body,labels,state,url,updatedAt,repository"
VIEW_JSON_FIELDS = "number,title,body,labels,state,url,updatedAt"
ALLOWED_STATES = frozenset({"all", "open", "closed"})
ALLOWED_VISIBILITY = frozenset({"public", "private"})
BATCH_SIZE = 500
SEARCH_RESULT_LIMIT = BATCH_SIZE
MAX_REPORTED_SKIPPED = BATCH_SIZE
# Reconciliation refreshes one previously linked issue per subprocess call, each of which
# may take SEARCH_TIMEOUT_SECONDS. A ledger with thousands of stale links would otherwise
# turn one sync into hours of GitHub calls, so a run spends at most this many refreshes and
# this much wall clock across every org. Links left unrefreshed are reported as skipped and
# picked up by the next run.
RECONCILE_MAX_REFRESHES = 200
RECONCILE_BUDGET_SECONDS = 300.0
HUB_STATUS_RE = re.compile(r"\bHTTP (\d{3})\b")
HUB_CODE_BY_STATUS = {401: "hub-unauthorized", 403: "hub-unauthorized", 409: "hub-conflict"}
HTTP_404_SIGNALS = frozenset({"HTTP 404", "HTTP 404\n", "HTTP 404\r\n"})
RECEIPT_UNSAFE = str.maketrans(
    {
        ":": "-",
        "/": "-",
        "\\": "-",
        "<": "-",
        ">": "-",
        '"': "-",
        "|": "-",
        "?": "-",
        "*": "-",
    }
)


class GitHubSyncError(Exception):
    """A GitHub discovery or reconcile step failed with a bounded message."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class _DiscoveryResult(list[dict[str, Any]]):
    """Normalized observations plus a bounded count of rejected provider records."""

    def __init__(self, observations: Sequence[dict[str, Any]] = (), *, skipped: int = 0) -> None:
        super().__init__(observations)
        self.skipped = min(max(skipped, 0), MAX_REPORTED_SKIPPED)


class _UnsafeGitHubObservation(Exception):
    """A provider record that cannot cross the Worklore safety boundary."""


_GitHubLink = tuple[Mapping[str, Any], Mapping[str, Any], str, str, int]


class _RefreshBudget:
    """The refresh count and wall clock one reconcile run may spend."""

    def __init__(self, *, max_refreshes: int | None = None, seconds: float | None = None) -> None:
        limit = RECONCILE_MAX_REFRESHES if max_refreshes is None else max_refreshes
        window = RECONCILE_BUDGET_SECONDS if seconds is None else seconds
        self.remaining = max(int(limit), 0)
        self.deadline = time.monotonic() + max(float(window), 0.0)

    def spend(self) -> bool:
        """Claim one refresh, or return False once the count or the clock is spent."""
        if self.remaining <= 0 or time.monotonic() >= self.deadline:
            self.remaining = 0
            return False
        self.remaining -= 1
        return True


def _config_error(message: str) -> GitHubSyncError:
    return GitHubSyncError(message, code="field-bound")


def _adapter_failure() -> GitHubSyncError:
    return GitHubSyncError("adapter command failed", code="retryable-adapter-failure")


def _search_truncated() -> GitHubSyncError:
    return GitHubSyncError("search result limit reached", code="search-truncated")


def load_github_config() -> dict[str, Any]:
    """Read ``[fleet.worklore.github]`` from the fleet config."""
    section = _read_github_section()
    if section is None:
        raise _config_error("GitHub Worklore config is missing orgs")
    label = section.get("label", DEFAULT_LABEL)
    if not isinstance(label, str) or not label.strip():
        raise _config_error("label must be a non-empty string")
    state = section.get("state", DEFAULT_STATE)
    if not isinstance(state, str) or state.strip() not in ALLOWED_STATES:
        raise _config_error("state must be all, open, or closed")
    return {
        "label": label.strip(),
        "state": state.strip(),
        "orgs": _parse_orgs(section.get("orgs")),
    }


def _read_github_section() -> dict[str, Any] | None:
    config_path = brigade_home() / FLEET_CONFIG_REL_PATH.name
    if not config_path.is_file():
        return None
    try:
        payload = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    fleet = payload.get("fleet") if isinstance(payload, dict) else None
    worklore = fleet.get("worklore") if isinstance(fleet, dict) else None
    github = worklore.get("github") if isinstance(worklore, dict) else None
    return github if isinstance(github, dict) else None


def _parse_orgs(raw: object) -> list[dict[str, str]]:
    if not isinstance(raw, list) or not raw:
        raise _config_error("orgs must be a non-empty list")
    orgs: list[dict[str, str]] = []
    seen_names: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            raise _config_error("orgs items must be tables")
        name = item.get("name")
        visibility = item.get("visibility")
        if not isinstance(name, str) or not ORG_NAME_RE.fullmatch(name):
            raise _config_error("orgs name is invalid")
        if visibility not in ALLOWED_VISIBILITY:
            raise _config_error("orgs visibility must be public or private")
        folded = name.lower()
        if folded in seen_names:
            raise _config_error("orgs name is duplicated")
        seen_names.add(folded)
        orgs.append({"name": name, "visibility": str(visibility)})
    return orgs


def _require_label(label: object) -> str:
    if not isinstance(label, str) or not label.strip():
        raise _config_error("label must be a non-empty string")
    return label.strip()


def _require_state(state: object) -> str:
    if not isinstance(state, str) or state.strip() not in ALLOWED_STATES:
        raise _config_error("state must be all, open, or closed")
    return state.strip()


def _use_octopool(visibility: str) -> bool:
    return visibility == "public" and shutil.which("octopool") is not None


def _search_argv(org_name: str, *, label: str, state: str, use_octopool: bool) -> list[str]:
    argv = [
        "gh",
        "search",
        "issues",
        "--owner",
        org_name,
        "--label",
        label,
        "--limit",
        str(SEARCH_RESULT_LIMIT),
        "--json",
        SEARCH_JSON_FIELDS,
    ]
    if state in {"open", "closed"}:
        argv.extend(["--state", state])
    if use_octopool:
        return ["octopool", *argv]
    return argv


def _view_argv(owner: str, repo: str, number: int, *, use_octopool: bool) -> list[str]:
    argv = [
        "gh",
        "issue",
        "view",
        str(number),
        "--repo",
        f"{owner}/{repo}",
        "--json",
        VIEW_JSON_FIELDS,
    ]
    if use_octopool:
        return ["octopool", *argv]
    return argv


def _run_github(argv: list[str], *, use_octopool: bool) -> subprocess.CompletedProcess[str]:
    env = None
    if use_octopool:
        env = os.environ.copy()
        env["OCTOPOOL_NO_FALLBACK"] = "1"
    try:
        return subprocess.run(  # noqa: S603 - argv is constructed, never shell-interpolated
            argv,
            timeout=SEARCH_TIMEOUT_SECONDS,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise _adapter_failure() from None


def _is_http_404(result: subprocess.CompletedProcess[str]) -> bool:
    if result.returncode == 0:
        return False
    for stream in (result.stdout or "", result.stderr or ""):
        if stream in HTTP_404_SIGNALS:
            return True
        try:
            payload = json.loads(stream)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, Mapping) and payload.get("status") == 404:
            return True
    return False


def _parse_search_stdout(stdout: str) -> list[object]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        raise _adapter_failure() from None
    if not isinstance(payload, list):
        raise _adapter_failure()
    return payload


def _parse_view_stdout(stdout: str) -> dict[str, Any]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        raise _adapter_failure() from None
    if not isinstance(payload, dict):
        raise _adapter_failure()
    return payload


def _search_item_owner(item: Mapping[str, Any]) -> str | None:
    repository = item.get("repository")
    if not isinstance(repository, Mapping):
        return None
    name_with_owner = repository.get("nameWithOwner")
    if not isinstance(name_with_owner, str):
        return None
    owner, separator, remainder = name_with_owner.strip().partition("/")
    if not separator or not owner or not remainder:
        return None
    return owner.lower()


def _normalize_search_payload(payload: list[object], *, org_name: str) -> _DiscoveryResult:
    expected_owner = org_name.lower()
    observations = _DiscoveryResult()
    for item in payload:
        if not isinstance(item, Mapping):
            raise _adapter_failure()
        if _search_item_owner(item) != expected_owner:
            raise _adapter_failure()
        try:
            observations.append(normalize_issue(item))
        except GitHubObservationError:
            observations.skipped = min(observations.skipped + 1, MAX_REPORTED_SKIPPED)
    return observations


def discover_issues(orgs: object, *, label: str, state: str) -> _DiscoveryResult:
    """List labeled issues for each org through octopool or direct gh."""
    parsed_orgs = _parse_orgs(orgs)
    label_value = _require_label(label)
    state_value = _require_state(state)
    observations = _DiscoveryResult()
    for org in parsed_orgs:
        use_octopool = _use_octopool(org["visibility"])
        result = _run_github(
            _search_argv(org["name"], label=label_value, state=state_value, use_octopool=use_octopool),
            use_octopool=use_octopool,
        )
        if result.returncode != 0:
            raise _adapter_failure()
        payload = _parse_search_stdout(result.stdout)
        if len(payload) >= SEARCH_RESULT_LIMIT:
            raise _search_truncated()
        normalized = _normalize_search_payload(payload, org_name=org["name"])
        observations.extend(normalized)
        observations.skipped = min(observations.skipped + normalized.skipped, MAX_REPORTED_SKIPPED)
    return observations


def _parse_external_key(raw: object) -> tuple[str, str, int]:
    if not isinstance(raw, str):
        raise _config_error("external key is invalid")
    match = EXTERNAL_KEY_RE.fullmatch(raw.strip())
    if match is None:
        raise _config_error("external key is invalid")
    owner, repo, number_text = match.groups()
    if repo.lower().endswith(".git"):
        repo = repo[: -len(".git")]
    if not repo:
        raise _config_error("external key is invalid")
    number = int(number_text)
    if number < 1:
        raise _config_error("external key is invalid")
    return owner.lower(), repo.lower(), number


def _github_links(existing_items: object) -> tuple[list[_GitHubLink], int]:
    """Return well-formed github links plus a count of malformed ones skipped."""
    if existing_items is None:
        existing_items = []
    if not isinstance(existing_items, list):
        raise _config_error("existing items must be an array")
    links: list[_GitHubLink] = []
    skipped = 0
    for item in existing_items:
        if not isinstance(item, Mapping):
            skipped += 1
            continue
        item_links = item.get("links")
        if item_links is None:
            continue
        if not isinstance(item_links, list):
            skipped += 1
            continue
        for link in item_links:
            if not isinstance(link, Mapping):
                skipped += 1
                continue
            if link.get("link_type") != "github":
                continue
            try:
                owner, repo, number = _parse_external_key(link.get("external_key"))
            except GitHubSyncError:
                skipped += 1
                continue
            links.append((item, link, owner, repo, number))
    return links, skipped


def _complete_link_projections(existing_items: object) -> list[object]:
    """Expand capped item link projections before using them for reconciliation."""
    if not isinstance(existing_items, list):
        raise _config_error("existing items must be an array")
    completed: list[object] = []
    for item in existing_items:
        if not isinstance(item, Mapping) or item.get("links_truncated") is not True:
            completed.append(item)
            continue
        work_id = item.get("work_id")
        if not isinstance(work_id, str) or not work_id.strip():
            raise GitHubSyncError("complete link listing could not be proven", code="links-incomplete")
        try:
            links = worklore_client.list_item_links_all(work_id)
        except worklore_client.FleetClientError as exc:
            raise GitHubSyncError("complete link listing could not be proven", code="links-incomplete") from exc
        expanded = dict(item)
        expanded["links"] = links
        expanded["links_truncated"] = False
        completed.append(expanded)
    return completed


def _require_existing_link_url(url: object, *, owner: str, repo: str, number: int) -> str:
    if not isinstance(url, str) or not url.strip():
        raise _UnsafeGitHubObservation()
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "github.com" or parsed.params or parsed.query or parsed.fragment:
        raise _UnsafeGitHubObservation()
    parts = parsed.path.strip("/").split("/")
    if len(parts) != 4 or parts[2] != "issues":
        raise _UnsafeGitHubObservation()
    url_owner, url_repo, _, url_number = parts
    if url_repo.lower().endswith(".git"):
        raise _UnsafeGitHubObservation()
    if url_owner.lower() != owner or url_repo.lower() != repo or url_number != str(number):
        raise _UnsafeGitHubObservation()
    return url


def _stale_tombstone(
    item: Mapping[str, Any],
    link: Mapping[str, Any],
    *,
    owner: str,
    repo: str,
    number: int,
) -> dict[str, Any]:
    try:
        title = bounded_title(item.get("title"), "existing item title")
        acceptance = bounded_acceptance(link.get("source_acceptance"), "existing link source_acceptance")
    except GitHubObservationError:
        raise _UnsafeGitHubObservation() from None
    source_policy = link.get("source_policy")
    if not isinstance(source_policy, str) or not source_policy.strip():
        raise _UnsafeGitHubObservation()
    url = _require_existing_link_url(link.get("url"), owner=owner, repo=repo, number=number)
    tombstone: dict[str, Any] = {
        "external_key": f"{owner}/{repo}#{number}",
        "link_type": "github",
        "title": title,
        "acceptance": acceptance,
        "source_policy": source_policy,
        "proposed_status": None,
        "url": url,
        "stale": True,
    }
    updated_at = link.get("external_updated_at")
    if updated_at is not None:
        if not isinstance(updated_at, str) or not updated_at.strip():
            raise _UnsafeGitHubObservation()
        tombstone["external_updated_at"] = updated_at
    return tombstone


def _refresh_missing_link(
    *,
    owner: str,
    repo: str,
    number: int,
    visibility: str,
    item: Mapping[str, Any],
    link: Mapping[str, Any],
) -> dict[str, Any] | None:
    use_octopool = _use_octopool(visibility)
    result = _run_github(
        _view_argv(owner, repo, number, use_octopool=use_octopool),
        use_octopool=use_octopool,
    )
    if result.returncode != 0:
        if _is_http_404(result):
            if use_octopool:
                return None
            return _stale_tombstone(item, link, owner=owner, repo=repo, number=number)
        raise _adapter_failure()
    payload = dict(_parse_view_stdout(result.stdout))
    payload["repository"] = {"nameWithOwner": f"{owner}/{repo}"}
    try:
        return normalize_issue(payload)
    except GitHubObservationError:
        raise _UnsafeGitHubObservation() from None


def discover_and_reconcile(
    orgs: object,
    *,
    label: str,
    state: str,
    existing_items: object,
    budget: _RefreshBudget | None = None,
) -> _DiscoveryResult:
    """Discover labeled issues, then refresh previously linked GitHub issues under a budget.

    ``budget`` bounds how many refreshes this run may make and how long it may spend on
    them. Pass one budget across several calls to bound a whole multi-org sync.
    """
    parsed_orgs = _parse_orgs(orgs)
    refresh_budget = _RefreshBudget() if budget is None else budget
    discovered = discover_issues(parsed_orgs, label=label, state=state)
    links, malformed = _github_links(_complete_link_projections(existing_items))
    observations = _DiscoveryResult(discovered, skipped=discovered.skipped + malformed)
    seen = {str(observation["external_key"]) for observation in observations}
    org_by_name = {org["name"].lower(): org for org in parsed_orgs}
    for item, link, owner, repo, number in links:
        key = f"{owner}/{repo}#{number}"
        if key in seen:
            continue
        org = org_by_name.get(owner)
        if org is None:
            continue
        if not refresh_budget.spend():
            observations.skipped = min(observations.skipped + 1, MAX_REPORTED_SKIPPED)
            continue
        visibility = org["visibility"]
        try:
            refreshed = _refresh_missing_link(
                owner=owner,
                repo=repo,
                number=number,
                visibility=visibility,
                item=item,
                link=link,
            )
        except _UnsafeGitHubObservation:
            observations.skipped = min(observations.skipped + 1, MAX_REPORTED_SKIPPED)
            continue
        if refreshed is not None:
            observations.append(refreshed)
            seen.add(key)
    return observations


def _hub_error_code(exc: Exception) -> str:
    """Classify a hub refusal so a permanent 4xx is not retried as transient."""
    match = HUB_STATUS_RE.search(str(exc))
    if match is None:
        return "hub-unavailable"
    status = int(match.group(1))
    if not 400 <= status < 500:
        return "hub-unavailable"
    return HUB_CODE_BY_STATUS.get(status, "hub-rejected")


def _hub_error(code: str) -> GitHubSyncError:
    return GitHubSyncError(code, code=code)


def _receipt_write_failed() -> GitHubSyncError:
    return GitHubSyncError("receipt-write-failed", code="receipt-write-failed")


def _safe_adapter_id(adapter_id: str) -> str:
    return adapter_id.translate(RECEIPT_UNSAFE)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _batch_idempotency_key(org_name: str, observations: Sequence[Mapping[str, Any]]) -> str:
    digest = worklore_store.observation_fingerprint(observations)
    return f"github:{org_name.lower()}:{digest}"


def _as_count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _chunks(observations: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    if not observations:
        return [[]]
    return [observations[index : index + size] for index in range(0, len(observations), size)]


def _ensure_private_receipt_dir() -> Path:
    directory = brigade_home() / "worklore" / "github"
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    try:
        directory.chmod(0o700)
        directory.parent.chmod(0o700)
    except OSError:
        pass
    return directory


def _write_receipt(
    adapter_id: str,
    *,
    idempotency_key: str,
    observation_count: int,
    last_error_code: str | None,
    refused: int = 0,
    skipped: int = 0,
) -> None:
    directory = _ensure_private_receipt_dir()
    path = directory / f"{_safe_adapter_id(adapter_id)}.json"
    payload = {
        "adapter_id": adapter_id,
        "idempotency_key": idempotency_key,
        "last_error_code": last_error_code,
        "observation_count": observation_count,
        "refused": _as_count(refused),
        "skipped": min(max(skipped, 0), MAX_REPORTED_SKIPPED),
        "updated_at": _utc_now(),
    }
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _write_receipt_best_effort(
    adapter_id: str,
    *,
    idempotency_key: str,
    observation_count: int,
    last_error_code: str | None,
    refused: int = 0,
    skipped: int = 0,
) -> None:
    try:
        _write_receipt(
            adapter_id,
            idempotency_key=idempotency_key,
            observation_count=observation_count,
            last_error_code=last_error_code,
            refused=refused,
            skipped=skipped,
        )
    except OSError:
        return


def _write_listing_failure_receipts(parsed_orgs: Sequence[Mapping[str, Any]], code: str) -> None:
    for org in parsed_orgs:
        _write_receipt_best_effort(
            f"github:{org['name'].lower()}",
            idempotency_key="",
            observation_count=0,
            last_error_code=code,
        )


def sync_github(
    orgs: object,
    *,
    label: str = DEFAULT_LABEL,
    state: str = DEFAULT_STATE,
) -> dict[str, Any]:
    """Discover labeled GitHub issues and post idempotent Worklore import batches."""
    parsed_orgs = _parse_orgs(orgs)
    label_value = _require_label(label)
    state_value = _require_state(state)
    try:
        items = worklore_client.list_items_all(source="github")
    except worklore_client.WorkloreListingError as exc:
        code = "hub-rejected"
        _write_listing_failure_receipts(parsed_orgs, code)
        raise _hub_error(code) from exc
    except worklore_client.FleetClientError as exc:
        code = _hub_error_code(exc)
        _write_listing_failure_receipts(parsed_orgs, code)
        raise _hub_error(code) from None
    try:
        completed_items = _complete_link_projections(items)
    except GitHubSyncError as exc:
        _write_listing_failure_receipts(parsed_orgs, exc.code)
        raise _hub_error(exc.code) from None
    posted = 0
    observation_count = 0
    refused_count = 0
    skipped_count = 0
    # One budget for the whole sync, so total reconcile work stays bounded no matter how
    # many organizations are configured.
    refresh_budget = _RefreshBudget()
    for org in parsed_orgs:
        adapter_id = f"github:{org['name'].lower()}"
        observations = discover_and_reconcile(
            [org],
            label=label_value,
            state=state_value,
            existing_items=completed_items,
            budget=refresh_budget,
        )
        skipped = min(max(int(getattr(observations, "skipped", 0)), 0), MAX_REPORTED_SKIPPED)
        skipped_count = min(skipped_count + skipped, MAX_REPORTED_SKIPPED)
        org_refused = 0
        ordered_observations = sorted(observations, key=lambda item: str(item["external_key"]))
        for chunk in _chunks(ordered_observations, BATCH_SIZE):
            idempotency_key = _batch_idempotency_key(org["name"], chunk)
            envelope = {
                "adapter_id": adapter_id,
                "source_type": "github",
                "idempotency_key": idempotency_key,
                "observations": chunk,
            }
            try:
                result = worklore_client.import_batch(envelope)
            except worklore_client.FleetClientError as exc:
                code = _hub_error_code(exc)
                _write_receipt_best_effort(
                    adapter_id,
                    idempotency_key=idempotency_key,
                    observation_count=len(chunk),
                    last_error_code=code,
                    refused=org_refused,
                    skipped=skipped,
                )
                raise _hub_error(code) from None
            refused = _as_count(result.get("refused")) if isinstance(result, Mapping) else 0
            org_refused += refused
            refused_count += refused
            try:
                _write_receipt(
                    adapter_id,
                    idempotency_key=idempotency_key,
                    observation_count=len(chunk),
                    last_error_code="hub-partial" if org_refused else None,
                    refused=org_refused,
                    skipped=skipped,
                )
            except OSError:
                raise _receipt_write_failed() from None
            posted += 1
            observation_count += len(chunk)
    return {
        "posted": posted,
        "observation_count": observation_count,
        "refused": refused_count,
        "skipped": skipped_count,
    }
