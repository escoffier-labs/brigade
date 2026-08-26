"""Codex Cloud adapter: submit a task, poll to a terminal state, return the result.

A roster seat references it as ``cli = "codex-cloud:configured"`` (resolved
from ``CODEX_CLOUD_ENV`` or ``brigade run cloud setup``) or
``cli = "codex-cloud:<env-id>"``. Environment ids come from the Codex Cloud
workspace UI; ``codex cloud list --json`` omits them. The worker's text is the
task's final status plus its unified diff. The diff is NEVER applied locally;
apply it deliberately with ``codex cloud apply <task-id>``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import cloud_tracker, fleet_client, localio, proc

SUBMIT_TIMEOUT = 120.0
POLL_TIMEOUT = 60.0
DIFF_TIMEOUT = 120.0
POLL_INTERVAL = 15.0
DIFF_CAP = 20_000
DEFAULT_MAX_ITEMS = 250
DEFAULT_LIST_TIMEOUT = 10.0
MAX_LIST_PAGES = 20
MAX_LIST_LIMIT = 20
CONFIGURED_SELECTOR = "configured"
DEFAULT_ENV_VAR = "CODEX_CLOUD_ENV"
CONFIG_SCHEMA = "brigade.codex-cloud-config/1"
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
LITERAL_SELECTOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
MAX_CONSECUTIVE_HUB_UNAVAILABLE_RENEWS = 3
_AUTH_FAILURE_RE = re.compile(
    r"(?:\bnot (?:logged|signed) in\b|\bsign[- ]?in\b|\bauthentication\b|\bunauthorized\b|\b401\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ListTasksResult:
    """Bounded ``codex cloud list --json`` inventory with a safe failure reason."""

    tasks: list[dict[str, Any]]
    ok: bool = True
    reason: str | None = None


class CodexCloudConfigError(ValueError):
    """Raised when a Codex Cloud seat cannot resolve its environment id."""


# Status keywords scanned (word-bounded, case-insensitive) in `codex cloud status`.
TERMINAL_OK = ("ready", "completed", "succeeded", "applied", "finished")
TERMINAL_FAIL = ("failed", "errored", "error", "cancelled", "canceled", "expired")
NONTERMINAL = ("pending", "queued", "running", "planning", "paused", "in_progress", "awaiting_plan_approval")

_ID_PATTERNS = (
    re.compile(r"https?://\S*/tasks?/(task_[A-Za-z0-9][A-Za-z0-9_-]{2,})\b", re.I),
    re.compile(r"\btask\s+id\s*[:=]\s*(task_[A-Za-z0-9][A-Za-z0-9_-]{2,})\b", re.I),
    re.compile(r"\b(task_[A-Za-z0-9][A-Za-z0-9_-]{2,})\b"),
)

_GITHUB_REPO_RE = re.compile(
    r"(?:https://github\.com/|git@github\.com:)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?(?:/|$)"
)


def _hub_configured() -> bool:
    """Return True when a fleet hub is configured.

    No-hub developer environments keep the existing local-only dispatch path.
    """
    return bool(fleet_client.load_fleet_config().get("hub_url"))


def _repo_from_cwd(cwd: Path | None) -> str | None:
    """Return an ``owner/repo`` string from the cwd's GitHub origin, if known."""
    try:
        result = proc.run(["git", "remote", "get-url", "origin"], timeout=5.0, cwd=cwd)
    except Exception:  # noqa: BLE001 - repo identification is best-effort
        return None
    if result.code != 0:
        return None
    url = (result.stdout or "").strip()
    if not url:
        return None
    match = _GITHUB_REPO_RE.search(url)
    if match:
        return match.group(1)
    return None


def parse_task_id(text: str) -> str | None:
    """Return only a strict Codex Cloud task id; never an arbitrary stdout token."""
    for pat in _ID_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1)
    return None


def _scan_status(text: str) -> str | None:
    """Return the terminal status keyword from status output.

    The installed CLI prints `[STATUS] <task title>` as the first line, so a
    leading bracket token is authoritative when present. Otherwise scan only
    status-shaped lines (`Status: ...`), so incidental words in a task title
    ("fix failed tests") cannot terminate polling. Whole-text scanning is the
    last resort for unrecognized formats.
    """
    brackets = re.findall(r"^\s*\[([A-Za-z_ -]+)\]", text, re.M)
    if brackets:
        scope = "\n".join(brackets)
    else:
        status_lines = [line for line in text.splitlines() if re.match(r"\s*(task\s+)?(status|state)\b", line, re.I)]
        scope = "\n".join(status_lines) if status_lines else text
    lowered = scope.lower()
    for word in TERMINAL_FAIL + TERMINAL_OK:
        if re.search(rf"\b{word}\b", lowered):
            return word
    return None


def _verified_status(text: str) -> str | None:
    """Return a documented bracket or status-line state, never title prose."""
    bracket = re.search(r"^\s*\[([A-Za-z_ -]+)\]", text, re.M)
    status = re.search(r"^\s*(?:task\s+)?(?:status|state)\s*[:=]\s*([A-Za-z_-]+)", text, re.I | re.M)
    match = bracket or status
    if match is None:
        return None
    token = match.group(1).strip().lower().replace(" ", "_")
    return token if token in TERMINAL_FAIL + TERMINAL_OK + NONTERMINAL else None


def config_path(target: Path) -> Path:
    return Path(target).expanduser().resolve() / ".brigade" / "cloud" / "codex.json"


def resolve_config_target(start: Path | None) -> Path:
    """Return the Brigade workspace root that owns cloud seat configuration."""
    if start is None:
        return Path(".").expanduser().resolve()
    current = Path(start).expanduser()
    try:
        current = current.resolve()
    except OSError:
        pass
    if current.is_file():
        current = current.parent
    home = Path.home()
    git_root: Path | None = None
    try:
        from . import runguard

        if runguard.is_git_worktree(current):
            git_root = runguard.git_root(current)
    except Exception:  # noqa: BLE001 - repo boundary is best-effort
        git_root = None
    for candidate in (current, *current.parents):
        if config_path(candidate).is_file():
            return candidate
        try:
            if (candidate / ".brigade").is_dir():
                return candidate
        except OSError:
            continue
        if candidate == home or (git_root is not None and candidate == git_root):
            break
    return current


def validate_selector_token(token: str) -> None:
    """Reject codex-cloud roster selectors that can only fail at dispatch."""
    if token is None:
        raise CodexCloudConfigError("codex-cloud environment token is empty")
    if token != token.strip():
        raise CodexCloudConfigError("codex-cloud selector must not contain surrounding whitespace")
    if any(ch.isspace() for ch in token):
        raise CodexCloudConfigError("codex-cloud selector must not contain whitespace")
    raw = token.strip()
    if not raw:
        raise CodexCloudConfigError("codex-cloud environment token is empty")
    if raw == CONFIGURED_SELECTOR:
        return
    if raw.startswith("$"):
        if not ENV_NAME_RE.fullmatch(raw[1:]):
            raise CodexCloudConfigError("invalid environment variable name")
        return
    if raw.startswith("-"):
        raise CodexCloudConfigError("codex-cloud literal environment id must not look like a CLI flag")
    if not LITERAL_SELECTOR_RE.fullmatch(raw):
        raise CodexCloudConfigError("invalid codex-cloud literal environment id")


def environment_audit_ref(selector_token: str, environment_id: str) -> dict[str, str]:
    """Return the least-sensitive audit payload for a resolved cloud environment.

    Literal roster tokens already declare the id in the reviewed roster, so
    local receipts may store the raw value. ``configured`` and ``$VAR`` selectors
    resolve from ambient process state; those runs store a stable fingerprint so
    operators can correlate evidence without echoing provider ids into hub or
    public output.
    """
    token = (selector_token or "").strip()
    if token == CONFIGURED_SELECTOR or token.startswith("$"):
        # Truncated unsalted digest for local receipt correlation only; not a secret token.
        digest = hashlib.sha256(environment_id.encode("utf-8")).hexdigest()[:12]
        return {"environment_fingerprint": f"sha256:{digest}"}
    return {"environment_id": environment_id}


def save_environment_config(
    target: Path,
    *,
    environment_id: str | None = None,
    environment_id_env: str | None = None,
) -> Path:
    """Write a local Codex Cloud environment reference. Prefer an env-var name."""
    if (environment_id is None) == (environment_id_env is None):
        raise CodexCloudConfigError("provide exactly one of environment_id or environment_id_env")
    if environment_id_env is not None:
        if not ENV_NAME_RE.fullmatch(environment_id_env):
            raise CodexCloudConfigError("invalid environment variable name")
        environment: dict[str, str] = {"kind": "env", "name": environment_id_env}
    else:
        value = (environment_id or "").strip()
        if not value:
            raise CodexCloudConfigError("environment_id is empty")
        environment = {"kind": "value", "id": value}
    path = config_path(target)
    localio.write_json(path, {"schema": CONFIG_SCHEMA, "environment": environment})
    path.chmod(0o600)
    return path


def load_environment_config(target: Path) -> dict[str, Any] | None:
    path = config_path(resolve_config_target(target))
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CodexCloudConfigError("codex cloud config is unreadable") from exc
    if not isinstance(data, dict) or data.get("schema") != CONFIG_SCHEMA:
        raise CodexCloudConfigError("codex cloud config uses an unsupported schema")
    return data


def _read_env_name(name: str, environ: Mapping[str, str]) -> str:
    if not ENV_NAME_RE.fullmatch(name):
        raise CodexCloudConfigError("invalid environment variable name")
    value = (environ.get(name) or "").strip()
    if not value:
        raise CodexCloudConfigError(f"{name} is unset")
    return value


def _resolve_configured(target: Path, environ: Mapping[str, str]) -> str:
    root = resolve_config_target(target)
    path = config_path(root)
    if path.is_file():
        config = load_environment_config(root)
        spec = config.get("environment") if isinstance(config, dict) else None
        if isinstance(spec, dict):
            kind = spec.get("kind")
            if kind == "env" and isinstance(spec.get("name"), str):
                return _read_env_name(spec["name"], environ)
            if kind == "value" and isinstance(spec.get("id"), str) and spec["id"].strip():
                return spec["id"].strip()
        raise CodexCloudConfigError("configured Codex Cloud environment is unavailable")
    fallback = (environ.get(DEFAULT_ENV_VAR) or "").strip()
    if fallback:
        return fallback
    raise CodexCloudConfigError("configured Codex Cloud environment is unavailable")


def resolve_environment_id(
    token: str,
    *,
    target: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Resolve a roster token to a Codex Cloud environment id.

    Literal tokens pass through. ``$NAME`` reads that process environment
    variable. ``configured`` reads ``.brigade/cloud/codex.json`` or
    ``CODEX_CLOUD_ENV``. The resolved id is never logged here.
    """
    raw = (token or "").strip()
    if not raw:
        raise CodexCloudConfigError("codex-cloud environment token is empty")
    mapping = os.environ if environ is None else environ
    root = resolve_config_target(Path(target) if target is not None else None)
    if raw.startswith("$"):
        return _read_env_name(raw[1:], mapping)
    if raw == CONFIGURED_SELECTOR:
        return _resolve_configured(root, mapping)
    return raw


def _environment_seen(env_id: str, tasks: list[dict[str, Any]]) -> str:
    """Return whether inventory rows expose the resolved environment id."""
    if not tasks:
        return "unknown"
    seen_ids = {
        task_id
        for task in tasks
        if isinstance(task, dict) and isinstance(task_id := task.get("environment_id"), str) and task_id.strip()
    }
    if not seen_ids:
        return "unknown"
    return "true" if env_id in seen_ids else "false"


def doctor(
    target: Path,
    *,
    selector: str = CONFIGURED_SELECTOR,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Report whether a Codex Cloud seat can resolve an environment id."""
    mapping = os.environ if environ is None else environ
    try:
        env_id = resolve_environment_id(selector, target=target, environ=mapping)
        configured = True
        audit = environment_audit_ref(selector, env_id)
    except CodexCloudConfigError:
        configured = False
        audit = None
    return {
        "ok": configured,
        "environment_configured": configured,
        "provider": "codex-cloud",
        "selector": selector,
        **(audit or {}),
    }


def canary(
    target: Path,
    *,
    selector: str = CONFIGURED_SELECTOR,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Bounded inventory check. Never calls ``codex cloud exec`` or ``apply``."""
    mapping = os.environ if environ is None else environ
    try:
        env_id = resolve_environment_id(selector, target=target, environ=mapping)
    except CodexCloudConfigError:
        return {
            "ok": False,
            "environment_configured": False,
            "task_count": 0,
            "apply": False,
            "selector": selector,
            "reason": "environment-unconfigured",
        }
    inventory = list_tasks(cwd=target)
    audit = environment_audit_ref(selector, env_id)
    environment_seen = _environment_seen(env_id, inventory.tasks)
    if not inventory.ok:
        return {
            "ok": False,
            "environment_configured": True,
            "task_count": 0,
            "apply": False,
            "selector": selector,
            "environment_seen": environment_seen,
            "reason": inventory.reason,
            **audit,
        }
    report: dict[str, Any] = {
        "ok": True,
        "environment_configured": True,
        "task_count": len(inventory.tasks),
        "apply": False,
        "selector": selector,
        "environment_seen": environment_seen,
        **audit,
    }
    if environment_seen == "false":
        report["reason"] = "environment-not-seen"
    return report


def _environment_id(task: dict[str, Any]) -> str | None:
    for key in ("environment_id", "environmentId", "env_id", "envId"):
        value = task.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    environment = task.get("environment")
    if isinstance(environment, str) and environment.strip():
        return environment.strip()
    if isinstance(environment, dict):
        value = environment.get("id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _classify_list_failure(result: proc.Result) -> str:
    if result.code == 127:
        return "provider-missing"
    if result.code == 126:
        return "provider-unavailable"
    if result.code == 124:
        return "provider-timeout"
    combined = "\n".join(part for part in (result.stderr, result.stdout) if part).strip()
    if result.code != 0 and _AUTH_FAILURE_RE.search(combined):
        return "auth-failure"
    return "provider-error"


def list_tasks(
    *,
    max_items: int = DEFAULT_MAX_ITEMS,
    cwd: Path | None = None,
    process_registry: proc.ProcessRegistry | None = None,
    command_timeout: float = DEFAULT_LIST_TIMEOUT,
) -> ListTasksResult:
    """Run ``codex cloud list --json`` and return a bounded, sanitized task list.

    Only the documented ``{tasks: [...], cursor}`` schema is accepted. Reads use
    the documented 1-20 item limit and cursor pagination, with fixed page and
    item caps. Task rows retain only id, normalized state, and environment id.
    """
    item_cap = max(0, min(max_items, DEFAULT_MAX_ITEMS))
    sanitized: list[dict[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    seen_task_ids: set[str] = set()
    for _page in range(MAX_LIST_PAGES):
        if len(sanitized) >= item_cap:
            break
        limit = min(MAX_LIST_LIMIT, item_cap - len(sanitized))
        if limit < 1:
            break
        argv = ["codex", "cloud", "list", "--json", "--limit", str(limit)]
        if cursor is not None:
            argv += ["--cursor", cursor]
        if process_registry is None:
            result = proc.run(argv, timeout=command_timeout, cwd=cwd)
        else:
            result = proc.run(argv, timeout=command_timeout, cwd=cwd, process_registry=process_registry)
        if result.code != 0:
            return ListTasksResult([], ok=False, reason=_classify_list_failure(result))
        try:
            data = json.loads((result.stdout or "").strip())
        except json.JSONDecodeError:
            return ListTasksResult([], ok=False, reason="list-json-invalid")
        if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
            return ListTasksResult([], ok=False, reason="list-json-unsupported")
        for raw in data["tasks"]:
            if not isinstance(raw, dict):
                continue
            task_id = raw.get("id")
            if not isinstance(task_id, str) or not parse_task_id(task_id) == task_id:
                continue
            if task_id in seen_task_ids:
                continue
            task: dict[str, Any] = {
                "id": task_id,
                "state": cloud_tracker.normalize_provider_state(raw.get("status") or raw.get("state")),
            }
            environment_id = _environment_id(raw)
            if environment_id is not None:
                task["environment_id"] = environment_id
            sanitized.append(task)
            seen_task_ids.add(task_id)
            if len(sanitized) >= item_cap:
                break
        next_cursor = data.get("cursor")
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    return ListTasksResult(sanitized)


def run_cloud_task(
    prompt: str,
    *,
    env_id: str,
    timeout: float,
    cwd: Path | None = None,
    attempts: int = 1,
    branch: str | None = None,
    poll_interval: float = POLL_INTERVAL,
    sleep=time.sleep,
    clock=time.monotonic,
    process_registry: proc.ProcessRegistry | None = None,
    label: str | None = None,
    register_target: Path | None = None,
    session_id: str | None = None,
    environment_audit: dict[str, str] | None = None,
):
    """Submit, poll, and collect one Codex Cloud task. Mirrors run_agent's contract."""
    from .agents import AgentResult

    def timeout_result(detail: str, *, task_id: str | None = None, text: str = ""):
        return AgentResult(
            text=text[:DIFF_CAP],
            ok=False,
            detail=detail[:200],
            failure_kind="timeout",
            thread_id=task_id,
            status="timeout",
            timed_out=True,
            cloud_environment=environment_audit,
        )

    def fail_result(
        detail: str,
        *,
        task_id: str | None = None,
        status: str = "",
        failure_kind: str = "provider-error",
    ):
        return AgentResult(
            text="",
            ok=False,
            detail=detail[:200],
            failure_phase="dispatch",
            failure_kind=failure_kind,
            thread_id=task_id,
            status=status,
            timed_out=False,
            cloud_environment=environment_audit,
        )

    try:
        hub_configured = _hub_configured()
    except Exception:  # noqa: BLE001 - configuration uncertainty must fail closed
        return fail_result(
            "fleet admission configuration unavailable", status="uncertain", failure_kind="fleet-admission-unavailable"
        )
    lease_id: str | None = None
    lease_id_str = ""
    holder: str | None = None
    prompt_hash = cloud_tracker.prompt_hash(prompt)
    repo: str | None = None
    tracker_label = cloud_tracker.lease_label("codex", None, prompt_hash)
    if hub_configured:
        repo = _repo_from_cwd(cwd)
        tracker_label = cloud_tracker.lease_label("codex", repo, prompt_hash)
        admit = fleet_client.admit_cloud(
            "codex",
            repo=repo,
            label=tracker_label,
            prompt_hash=prompt_hash,
            ttl_seconds=300,
        )
        if not admit.granted:
            failure_kind = "fleet-admission-denied" if admit.reason == "refused" else "fleet-admission-unavailable"
            return fail_result("cloud admission denied", status="denied", failure_kind=failure_kind)
        lease_id = admit.lease.get("lease_id") if isinstance(admit.lease, dict) else None
        holder = admit.holder
        if not isinstance(lease_id, str):
            return fail_result(
                "cloud admission returned no usable lease", status="denied", failure_kind="fleet-admission-denied"
            )
        # Narrow for mypy: the lease id is a string for the rest of the dispatch.
        lease_id_str = lease_id
    argv = ["codex", "cloud", "exec", "--env", env_id]
    if attempts > 1:
        argv += ["--attempts", str(attempts)]
    if branch:
        argv += ["--branch", branch]
    argv.append(prompt)

    def run_command(command: list[str], *, command_timeout: float) -> proc.Result:
        if process_registry is None:
            return proc.run(command, timeout=command_timeout, cwd=cwd)
        return proc.run(command, timeout=command_timeout, cwd=cwd, process_registry=process_registry)

    deadline = clock() + timeout
    submit = run_command(argv, command_timeout=min(timeout, SUBMIT_TIMEOUT))

    task_id = None
    if submit.code == 0:
        task_id = parse_task_id(submit.stdout) or parse_task_id(submit.stderr)

    if hub_configured:
        if submit.code != 0 or task_id is None:
            # Ambiguous provider result: retain the short lease and fail with no retry.
            return AgentResult(
                text="",
                ok=False,
                detail="cloud submit result was ambiguous",
                thread_id=task_id,
                status="uncertain",
                failure_phase="dispatch",
                failure_kind="uncertain",
                timed_out=submit.code == 124,
                cloud_environment=environment_audit,
            )
        bind = fleet_client.bind_cloud(lease_id_str, provider_task_id=task_id, holder=holder)
        if not bind.granted:
            # The provider task is live but the hub could not bind it. The lease
            # is still held; the operator can inspect and release it manually.
            return AgentResult(
                text="",
                ok=False,
                detail="cloud lease bind failed",
                thread_id=task_id,
                status="bind-failed",
                failure_phase="dispatch",
                failure_kind="fleet-bind-failed",
                timed_out=False,
                cloud_environment=environment_audit,
            )
    else:
        if submit.code == 124:
            return timeout_result("cloud submit timed out")
        if submit.code != 0:
            return fail_result("cloud submit failed")
        if task_id is None:
            return fail_result("cloud submit returned no usable task id")

    # Register-on-dispatch (#890): hash only, never store prompt text.
    registry_root = register_target or cwd
    if registry_root is not None:
        try:
            cloud_tracker.register(
                Path(registry_root),
                provider="codex-cloud",
                task_id=task_id,
                label=tracker_label,
                prompt_hash=prompt_hash,
                session_id=session_id,
                branch=branch,
                environment_audit=environment_audit,
                expected_artifact=({"kind": "branch", "pattern": branch} if branch else {"kind": "diff"}),
            )
        except Exception:
            # Registry must not block cloud dispatch; adopt path covers misses.
            pass

    def remaining(floor: float = 5.0) -> float:
        return max(floor, deadline - clock())

    status_text = ""
    status_word = None
    consecutive_hub_unavailable_renews = 0
    while True:
        st = run_command(
            ["codex", "cloud", "status", task_id],
            command_timeout=min(POLL_TIMEOUT, remaining()),
        )
        status_text = (st.stdout + "\n" + st.stderr).strip()
        if st.code == 124:
            if hub_configured:
                return AgentResult(
                    text="",
                    ok=False,
                    detail="cloud status timed out",
                    thread_id=task_id,
                    status="uncertain",
                    failure_phase="dispatch",
                    failure_kind="uncertain",
                    timed_out=True,
                    cloud_environment=environment_audit,
                )
            return timeout_result(
                "cloud status timed out",
                task_id=task_id,
                text="",
            )
        if st.code != 0:
            if hub_configured:
                return AgentResult(
                    text="",
                    ok=False,
                    detail="cloud status unavailable",
                    thread_id=task_id,
                    status="uncertain",
                    failure_phase="dispatch",
                    failure_kind="uncertain",
                    timed_out=False,
                    cloud_environment=environment_audit,
                )
            return fail_result(
                "cloud status unavailable", task_id=task_id, status="uncertain", failure_kind="uncertain"
            )
        verified_status = _verified_status(status_text)
        status_word = verified_status if verified_status in TERMINAL_FAIL + TERMINAL_OK else _scan_status(status_text)
        if hub_configured and verified_status not in TERMINAL_FAIL + TERMINAL_OK:
            status_word = None
        if status_word in TERMINAL_FAIL:
            break
        if status_word in TERMINAL_OK:
            break
        if hub_configured:
            renew = fleet_client.renew_cloud(lease_id_str, ttl_seconds=300, holder=holder)
            if not renew.granted:
                if renew.reason in ("refused", "auth-failed"):
                    return AgentResult(
                        text="",
                        ok=False,
                        detail="cloud lease renew refused",
                        thread_id=task_id,
                        status="uncertain",
                        failure_phase="dispatch",
                        failure_kind="fleet-lease-lost",
                        timed_out=False,
                        cloud_environment=environment_audit,
                    )
                if renew.reason == "hub-unavailable":
                    consecutive_hub_unavailable_renews += 1
                    if consecutive_hub_unavailable_renews >= MAX_CONSECUTIVE_HUB_UNAVAILABLE_RENEWS:
                        return AgentResult(
                            text="",
                            ok=False,
                            detail="cloud lease renew unavailable",
                            thread_id=task_id,
                            status="uncertain",
                            failure_phase="dispatch",
                            failure_kind="fleet-lease-lost",
                            timed_out=False,
                            cloud_environment=environment_audit,
                        )
                else:
                    consecutive_hub_unavailable_renews = 0
            else:
                consecutive_hub_unavailable_renews = 0
        if clock() >= deadline:
            if hub_configured:
                return AgentResult(
                    text="",
                    ok=False,
                    detail="cloud task timed out",
                    thread_id=task_id,
                    status="uncertain",
                    failure_phase="dispatch",
                    failure_kind="uncertain",
                    timed_out=True,
                    cloud_environment=environment_audit,
                )
            return timeout_result(
                "cloud task timed out",
                task_id=task_id,
                text="",
            )
        sleep(poll_interval)

    # Verified terminal state: release the lease now.
    if hub_configured:
        fleet_client.release_cloud(lease_id_str, state=status_word or "released", holder=holder)

    if status_word in TERMINAL_FAIL:
        return AgentResult(
            text="",
            ok=False,
            detail=f"cloud task {status_word}"[:200],
            thread_id=task_id,
            status=status_word,
            cloud_environment=environment_audit,
        )

    diff = run_command(
        ["codex", "cloud", "diff", task_id],
        command_timeout=min(DIFF_TIMEOUT, remaining(floor=30.0)),
    )
    parts = [f"codex cloud task {task_id} [{status_word}]", status_text]
    if diff.code == 124:
        return timeout_result(
            "cloud diff timed out",
            task_id=task_id,
            text="",
        )
    if diff.code != 0:
        parts.append("WARNING: cloud diff failed")
    elif diff.stdout.strip():
        parts += [
            f"Unified diff (NOT applied locally; apply with `codex cloud apply {task_id}`):",
            diff.stdout.strip()[:DIFF_CAP],
        ]
    else:
        parts.append("No diff produced (research or no-change task).")
    text = "\n\n".join(p for p in parts if p)
    return AgentResult(
        text=text,
        ok=True,
        thread_id=task_id,
        status=status_word or "",
        cloud_environment=environment_audit,
    )
