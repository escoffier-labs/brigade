"""Source-side Brigade worker routing with the local ``t3-fleet`` client as remote transport.

``brigade run`` stays the policy consumer. This module resolves the central
routing decision for one worker seat *before* the local model lease is taken,
and, when policy selected another machine, hands execution to that machine
through the public ``t3-fleet`` client under a Hub-issued delegation.

Ownership boundary (frozen delegation contract):

* The source resolves the route, creates the delegation from the existing
  decision plus an immutable source revision, and keeps a read-only model plan
  projection. It never acquires a worker model lease and never calls a provider
  for a remote branch.
* The target performs prepare -> context application -> ack -> launch proof ->
  provider execution and renews the target-owned reservation as the execution
  claim. Reservation release is target-only and the Hub enforces that, so a
  blocked source route leaves the reservation to its own TTL rather than
  attempting a release it is not authorized to make.
* A remote selection is never silently downgraded to a local run. Every refusal
  is a routing rejection with an explicit reason.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

SCHEMA = "brigade.fleet_t3_transport.v1"
CONSUMER = "brigade-run"
ADAPTER = "t3-fleet"
CLIENT_EXECUTABLE = "t3-fleet"

RESULT_SCHEMA = "t3-fleet.result.v1"
ERROR_SCHEMA = "t3-fleet.error.v1"

#: Route outcomes. ``origin`` keeps the existing local lease + provider path.
ROUTE_ORIGIN = "origin"
ROUTE_REMOTE = "remote"
ROUTE_BLOCKED = "blocked"

#: Normalized T3 states. Each stays distinct in the Brigade receipt: a wait
#: timeout is not terminal success and never releases anything.
STATE_SUCCEEDED = "succeeded"
TERMINAL_STATES = frozenset({"succeeded", "failed", "interrupted"})
PENDING_STATES = frozenset({"queued", "preflight", "dispatching", "running"})
ATTENTION_STATES = frozenset({"needs_attention", "blocked_offline"})
UNKNOWN_STATE = "unknown"

DEFAULT_CALL_TIMEOUT = 120.0
DEFAULT_WAIT_TIMEOUT = 3600.0
MAX_OUTPUT_BYTES = 262_144
MAX_TITLE_CHARS = 120
MAX_DETAIL_CHARS = 200

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class TransportDenial(Exception):
    """A bounded, redacted refusal from the source transport or the T3 client."""

    def __init__(self, code: str, message: str, *, request_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = _one_line(message)
        self.request_id = request_id


def _one_line(value: Any, limit: int = MAX_DETAIL_CHARS) -> str:
    text = _CONTROL_RE.sub(" ", str(value or "")).strip()
    return text[:limit]


def _bounded(value: Any, limit: int = MAX_OUTPUT_BYTES) -> str:
    text = str(value or "")
    encoded = text.encode("utf-8", "replace")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", "ignore")


# --- source route resolution -------------------------------------------------


@dataclass(frozen=True)
class RouteResolution:
    """Where policy said this seat's work runs, resolved before any lease."""

    route: str
    reason: str
    seat: str
    origin_machine: str | None = None
    origin_node: str | None = None
    target_machine: str | None = None
    decision_id: str | None = None
    reservation_id: str | None = None
    session_id: str | None = None
    repo_identity: str | None = None
    workload: str | None = None
    policy_version: int | None = None
    policy_digest: str | None = None
    override_reason: str | None = None
    binding: dict[str, Any] | None = None

    @property
    def is_remote(self) -> bool:
        return self.route == ROUTE_REMOTE

    def receipt(self) -> dict[str, Any]:
        """Bounded routing receipt. No prompt, credentials, or private paths."""
        return {
            "schema": SCHEMA,
            "consumer": CONSUMER,
            "adapter": ADAPTER,
            "route": self.route,
            "reason": self.reason,
            "seat": self.seat,
            "origin_machine": self.origin_machine,
            "origin_node": self.origin_node,
            "target_machine": self.target_machine,
            "decision_id": self.decision_id,
            "reservation_id": self.reservation_id,
            "session_id": self.session_id,
            "repo_identity": self.repo_identity,
            "workload": self.workload,
            "policy_version": self.policy_version,
            "policy_digest": self.policy_digest,
            "override_reason": self.override_reason,
        }


def resolve_origin_machine(document: Mapping[str, Any], node_id: str) -> str | None:
    """Map this node's authenticated identity to its configured machine name.

    The Hub already enforces that a ``node_id`` maps to exactly one machine, so
    this is a lookup in the central configured mapping, not a name heuristic.
    A hostname is never guessed and a literal ``"local"`` is never invented.
    """
    if not node_id or node_id == "unknown":
        return None
    machines = document.get("machines")
    if not isinstance(machines, Mapping):
        return None
    for name in sorted(machines):
        record = machines[name]
        if isinstance(record, Mapping) and record.get("node_id") == node_id:
            return str(name)
    return None


def target_binding(document: Mapping[str, Any], seat: str) -> dict[str, Any] | None:
    """The exact ``t3_fleet``/native launch binding for a seat, or ``None``.

    A Brigade CLI-only seat (``bindings.brigade.cli`` with no T3 instance) is
    deliberately not T3-launchable: the caller turns ``None`` into a routing
    rejection rather than guessing an instance id.
    """
    from . import fleet_model_roster, fleet_policy

    try:
        bindings = fleet_policy.effective_seat_bindings(document, CONSUMER, seat)
    except Exception:
        return None
    return fleet_model_roster.adapter_plan_binding(CONSUMER, bindings, adapter=ADAPTER)


def immutable_source_revision(cwd: Path, *, runner: Callable[..., Any] = subprocess.run) -> str:
    """Return a clean 40-character HEAD, or refuse with ``remote-context-unavailable``.

    Dirty tracked content or relevant untracked content means the approved work
    context cannot be reproduced on the target. HEAD is never silently
    substituted, and the machine policy is never violated by falling back to a
    local run. Ignored paths (local credentials, build output) are excluded by
    ``git status --porcelain`` and are never transferred.
    """
    try:
        head = runner(
            ["git", "-C", str(cwd), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=DEFAULT_CALL_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise TransportDenial("remote-context-unavailable", f"source revision is unreadable: {exc}") from exc
    if getattr(head, "returncode", 1) != 0:
        raise TransportDenial("remote-context-unavailable", "source checkout has no resolvable HEAD revision")
    revision = _one_line(getattr(head, "stdout", ""), 64)
    if SHA_RE.match(revision) is None:
        raise TransportDenial("remote-context-unavailable", "source HEAD is not an immutable 40-character revision")
    try:
        status = runner(
            ["git", "-C", str(cwd), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=DEFAULT_CALL_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise TransportDenial("remote-context-unavailable", f"source cleanliness is unreadable: {exc}") from exc
    if getattr(status, "returncode", 1) != 0:
        raise TransportDenial("remote-context-unavailable", "source checkout cleanliness is unverifiable")
    if str(getattr(status, "stdout", "") or "").strip():
        raise TransportDenial(
            "remote-context-unavailable",
            "source checkout has uncommitted tracked or untracked content; the approved revision is not reproducible",
        )
    return revision


def resolve_route(
    *,
    seat: str,
    session_id: str,
    workload: str,
    repo_identity: str | None,
    node_id: str,
    document: Mapping[str, Any],
    policy_client: Any,
    machine_override: str | None = None,
    override_reason: str | None = None,
    work_id: str | None = None,
) -> RouteResolution:
    """Ask the central routing authority where this seat's work runs.

    Called before the local model lease. An explicit ``machine_override`` is an
    ordinary policy receipt: it still travels through the Hub with its reason
    and never bypasses privacy, capacity, or credential constraints.
    """
    origin_machine = resolve_origin_machine(document, node_id)
    if origin_machine is None:
        return RouteResolution(
            route=ROUTE_BLOCKED,
            reason="origin-identity-unresolved",
            seat=seat,
            origin_node=node_id or None,
            session_id=session_id,
            repo_identity=repo_identity,
            workload=workload,
        )
    if not repo_identity:
        return RouteResolution(
            route=ROUTE_BLOCKED,
            reason="repo-unverified",
            seat=seat,
            origin_machine=origin_machine,
            origin_node=node_id,
            session_id=session_id,
            workload=workload,
        )

    from .fleet_client_policy import FleetPolicyClientError

    try:
        decision = policy_client.route_work(
            consumer=CONSUMER,
            repo=repo_identity,
            session_id=session_id,
            origin=origin_machine,
            workload=workload,
            machine=machine_override,
            seat=seat if machine_override or override_reason else None,
            override_reason=override_reason,
            work_id=work_id,
        )
    except FleetPolicyClientError as exc:
        return RouteResolution(
            route=ROUTE_BLOCKED,
            reason=exc.code or "route-unavailable",
            seat=seat,
            origin_machine=origin_machine,
            origin_node=node_id,
            session_id=session_id,
            repo_identity=repo_identity,
            workload=workload,
            override_reason=override_reason,
        )

    selected = decision.get("selected") if isinstance(decision, Mapping) else None
    common: dict[str, Any] = {
        "seat": seat,
        "origin_machine": origin_machine,
        "origin_node": node_id,
        "session_id": session_id,
        "repo_identity": repo_identity,
        "workload": workload,
        "override_reason": override_reason,
    }
    if isinstance(decision, Mapping):
        common["decision_id"] = decision.get("decision_id")
        common["reservation_id"] = decision.get("reservation_id")
        common["policy_version"] = decision.get("policy_version")
        common["policy_digest"] = decision.get("policy_digest")

    if not isinstance(selected, Mapping) or not selected.get("machine"):
        reason = _one_line((decision or {}).get("reason") if isinstance(decision, Mapping) else "") or "unselected"
        return RouteResolution(route=ROUTE_BLOCKED, reason=reason, **common)

    target_machine = str(selected["machine"])
    selected_seat = str(selected.get("seat") or seat)
    common["seat"] = selected_seat
    common["target_machine"] = target_machine
    if target_machine == origin_machine:
        return RouteResolution(route=ROUTE_ORIGIN, reason="origin-selected", **common)

    binding = target_binding(document, selected_seat)
    if binding is None:
        return RouteResolution(route=ROUTE_BLOCKED, reason="t3-binding-missing", **common)
    return RouteResolution(route=ROUTE_REMOTE, reason="remote-selected", binding=binding, **common)


# --- public t3-fleet client --------------------------------------------------


@dataclass
class T3FleetClient:
    """Bounded wrapper over the public ``t3-fleet`` command surface.

    Only ``doctor``, ``capacity``, ``submit``, ``status``, ``wait``, and
    ``recover`` are reachable. Connection details, endpoints, worktree paths,
    and private state locations are the client's own configuration and are
    never constructed here. The prompt travels in a 0600 file, never on argv.
    """

    executable: str = CLIENT_EXECUTABLE
    timeout: float = DEFAULT_CALL_TIMEOUT
    runner: Callable[..., Any] = subprocess.run
    cwd: Path | None = None

    def _invoke(self, argv: Sequence[str], *, timeout: float | None = None) -> dict[str, Any]:
        command = [self.executable, *argv]
        try:
            completed = self.runner(
                command,
                capture_output=True,
                text=True,
                timeout=timeout if timeout is not None else self.timeout,
                check=False,
                cwd=str(self.cwd) if self.cwd is not None else None,
            )
        except FileNotFoundError as exc:
            raise TransportDenial("t3-client-missing", "the local t3-fleet client is not installed") from exc
        except subprocess.TimeoutExpired as exc:
            raise TransportDenial("t3-client-timeout", f"t3-fleet {argv[0]} exceeded its bounded timeout") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise TransportDenial("t3-client-error", f"t3-fleet {argv[0]} failed to run: {exc}") from exc

        stdout = _bounded(getattr(completed, "stdout", "") or "")
        stderr = _one_line(getattr(completed, "stderr", "") or "")
        code = int(getattr(completed, "returncode", 1) or 0)
        try:
            payload = json.loads(stdout) if stdout.strip() else None
        except json.JSONDecodeError:
            payload = None

        if isinstance(payload, Mapping) and payload.get("schema") == ERROR_SCHEMA:
            raw_error = payload.get("error")
            error: Mapping[str, Any] = raw_error if isinstance(raw_error, Mapping) else payload
            raise TransportDenial(
                _one_line(error.get("code") or "t3-error", 64) or "t3-error",
                _one_line(error.get("message") or "t3-fleet refused the call"),
                request_id=_request_id(payload) or _request_id(error),
            )
        if code != 0:
            if "unrecognized arguments" in stderr or "--delegation-id" in stderr:
                raise TransportDenial(
                    "t3-delegation-unsupported",
                    "the installed t3-fleet client does not accept --delegation-id",
                )
            raise TransportDenial("t3-client-error", stderr or f"t3-fleet {argv[0]} exited {code}")
        if not isinstance(payload, Mapping):
            raise TransportDenial("t3-client-error", f"t3-fleet {argv[0]} returned an unusable envelope")
        return dict(payload)

    def doctor(self, *, host: str | None = None, repository: str | None = None) -> dict[str, Any]:
        argv = ["doctor"]
        if host:
            argv += ["--host", host]
        if repository:
            argv += ["--repository", repository]
        return self._invoke(argv)

    def capacity(self, *, host: str | None = None) -> dict[str, Any]:
        argv = ["capacity"]
        if host:
            argv += ["--host", host]
        return self._invoke(argv)

    def submit(
        self,
        *,
        delegation_id: str,
        title: str,
        prompt: str,
        source_revision: str,
        runtime_mode: str = "full-access",
        interaction_mode: str = "default",
    ) -> dict[str, Any]:
        """Submit under an existing delegation. The delegation carries identity.

        ``--delegation-id`` is the only adoption flag: the source never supplies
        a consumer or adapter override, never overrides a peer-qualified request
        id, and never sends host or repository aliases it would have to guess.
        ``--source-revision`` only confirms the receipt; T3 rejects a conflict.
        """
        handle, path = tempfile.mkstemp(prefix="brigade-t3-", suffix=".prompt")
        try:
            os.chmod(path, 0o600)
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(prompt)
            argv = [
                "submit",
                "--delegation-id",
                delegation_id,
                "--source-revision",
                source_revision,
                "--title",
                _title(title),
                "--prompt-file",
                path,
                "--runtime-mode",
                runtime_mode,
                "--interaction-mode",
                interaction_mode,
            ]
            return self._invoke(argv)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def status(self, *, request_id: str, include_output: bool = True) -> dict[str, Any]:
        argv = ["status", "--request-id", request_id]
        if include_output:
            argv.append("--include-output")
        return self._invoke(argv)

    def wait(self, *, request_id: str, timeout: float, include_output: bool = True) -> dict[str, Any]:
        argv = ["wait", "--request-id", request_id, "--timeout", str(int(timeout))]
        if include_output:
            argv.append("--include-output")
        return self._invoke(argv, timeout=timeout + self.timeout)

    def recover(self, *, request_id: str, include_output: bool = True) -> dict[str, Any]:
        argv = ["recover", "--request-id", request_id]
        if include_output:
            argv.append("--include-output")
        return self._invoke(argv)


def _title(raw: str) -> str:
    text = _CONTROL_RE.sub(" ", str(raw or "")).strip()
    return (text[:MAX_TITLE_CHARS] or "brigade delegated work").strip()


def _request_id(payload: Any) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    value = payload.get("request_id")
    return str(value) if isinstance(value, str) and value else None


# --- remote execution --------------------------------------------------------


@dataclass(frozen=True)
class RemoteOutcome:
    """One remote execution attempt, mapped for a Brigade worker result."""

    ok: bool
    state: str
    text: str = ""
    detail: str = ""
    failure_kind: str | None = None
    failure_phase: str | None = None
    request_id: str | None = None
    delegation_id: str | None = None
    route: RouteResolution | None = None
    disposition: str | None = None
    handoff: dict[str, Any] = field(default_factory=dict)
    source_revision: str | None = None

    def receipt(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": SCHEMA,
            "consumer": CONSUMER,
            "adapter": ADAPTER,
            "state": self.state,
            "request_id": self.request_id,
            "delegation_id": self.delegation_id,
            "source_revision": self.source_revision,
        }
        if self.route is not None:
            payload["route"] = self.route.receipt()
        if self.disposition:
            payload["disposition"] = self.disposition
        if self.handoff:
            payload["handoff"] = dict(self.handoff)
        if self.failure_kind:
            payload["failure_kind"] = self.failure_kind
        return payload


#: Frozen handoff field names, shared with the T3 coordinator. ``worktree_ref``
#: is an opaque target-owned request reference, never a filesystem path.
HANDOFF_FIELDS = (
    "target_machine",
    "repository",
    "branch",
    "source_revision",
    "request_id",
    "worktree_ref",
)


def _handoff(payload: Mapping[str, Any]) -> dict[str, Any]:
    raw = payload.get("handoff")
    if not isinstance(raw, Mapping):
        return {}
    return {key: raw[key] for key in HANDOFF_FIELDS if key in raw}


def _state(payload: Mapping[str, Any]) -> str:
    result = payload.get("result")
    if isinstance(result, Mapping) and isinstance(result.get("state"), str):
        return str(result["state"])
    return _one_line(payload.get("state") or UNKNOWN_STATE, 64) or UNKNOWN_STATE


def _result_body(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    result = payload.get("result")
    return result if isinstance(result, Mapping) else payload


def _text_of(payload: Mapping[str, Any]) -> str:
    body = _result_body(payload)
    for key in ("output", "text", "summary"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return _bounded(value)
    return ""


def _outcome_from(
    payload: Mapping[str, Any],
    *,
    route: RouteResolution,
    delegation_id: str,
    request_id: str | None,
    source_revision: str,
) -> RemoteOutcome:
    body = _result_body(payload)
    state = _state(payload)
    handoff = _handoff(body) or _handoff(payload)
    disposition = body.get("disposition") if isinstance(body.get("disposition"), str) else None
    # A pending handoff is only meaningful for a terminal execution that kept a
    # target worktree. Running or unknown never claims a completed handoff.
    if state not in TERMINAL_STATES:
        disposition = None
        handoff = {}
    ok = state == STATE_SUCCEEDED
    return RemoteOutcome(
        ok=ok,
        state=state,
        text=_text_of(payload),
        detail=("" if ok else _one_line(body.get("detail") or body.get("message") or f"remote task state {state!r}")),
        failure_kind=None if ok else f"t3-{state.replace('_', '-')}",
        failure_phase=None if ok else "dispatch",
        request_id=request_id or _request_id(payload) or _request_id(body),
        delegation_id=delegation_id,
        route=route,
        disposition=disposition,
        handoff=handoff,
        source_revision=source_revision,
    )


def execute_remote(
    resolution: RouteResolution,
    prompt: str,
    *,
    title: str,
    cwd: Path,
    client: T3FleetClient,
    policy_client: Any,
    parent_request_id: str,
    wait_timeout: float = DEFAULT_WAIT_TIMEOUT,
    source_revision: str | None = None,
    on_handle: Callable[[str, str], None] | None = None,
) -> RemoteOutcome:
    """Run one delegated worker task on the policy-selected target machine.

    Proof order: immutable source revision -> delegation create -> doctor and
    capacity eligibility -> submit -> persist the qualified request id ->
    bounded wait. The source takes no model lease and makes no provider call.
    """
    assert resolution.is_remote and resolution.decision_id
    try:
        revision = source_revision or immutable_source_revision(cwd)
    except TransportDenial as exc:
        return _denied(resolution, exc, phase="preflight", delegation_id=None, source_revision=None)

    from .fleet_client_policy import FleetPolicyClientError

    try:
        delegation = policy_client.create_delegation(
            decision_id=resolution.decision_id,
            source_revision=revision,
            parent_request_id=parent_request_id,
        )
    except FleetPolicyClientError as exc:
        return _denied(
            resolution,
            TransportDenial(exc.code or "delegation-unavailable", exc.message),
            phase="preflight",
            delegation_id=None,
            source_revision=revision,
        )
    delegation_id = str(delegation.get("delegation_id") or "") if isinstance(delegation, Mapping) else ""
    if not delegation_id:
        return _denied(
            resolution,
            TransportDenial("delegation-unavailable", "delegation create returned no delegation id"),
            phase="preflight",
            delegation_id=None,
            source_revision=revision,
        )

    request_id: str | None = None
    try:
        _require_eligible(client)
        submitted = client.submit(
            delegation_id=delegation_id,
            title=title,
            prompt=prompt,
            source_revision=revision,
        )
        request_id = _request_id(submitted)
        if not request_id:
            raise TransportDenial("t3-client-error", "t3-fleet submit returned no qualified request id")
        # Persist the qualified handle before any poll so an interrupted source
        # recovers the existing request instead of resubmitting the prompt.
        if on_handle is not None:
            on_handle(delegation_id, request_id)
        state = _state(submitted)
        if state in TERMINAL_STATES or state in ATTENTION_STATES:
            return _outcome_from(
                submitted,
                route=resolution,
                delegation_id=delegation_id,
                request_id=request_id,
                source_revision=revision,
            )
        waited = client.wait(request_id=request_id, timeout=wait_timeout)
        return _outcome_from(
            waited,
            route=resolution,
            delegation_id=delegation_id,
            request_id=request_id,
            source_revision=revision,
        )
    except TransportDenial as exc:
        handle = request_id or exc.request_id
        if handle and exc.code in {"unknown_state", "t3-client-timeout", "t3-client-error"}:
            return _recover(
                resolution,
                client,
                request_id=handle,
                delegation_id=delegation_id,
                source_revision=revision,
                cause=exc,
            )
        return _denied(
            resolution,
            exc,
            phase="dispatch" if handle else "preflight",
            delegation_id=delegation_id,
            source_revision=revision,
            request_id=handle,
        )


def _require_eligible(client: T3FleetClient) -> None:
    """Refuse dispatch when the client reports an ineligible or full target."""
    doctor = client.doctor()
    _reject_ineligible(doctor, "t3-target-ineligible")
    capacity = client.capacity()
    _reject_ineligible(capacity, "t3-target-at-capacity")


def _reject_ineligible(payload: Mapping[str, Any], code: str) -> None:
    hosts = payload.get("hosts")
    if not isinstance(hosts, Sequence) or isinstance(hosts, (str, bytes)):
        return
    states = {_one_line(host.get("state") or host.get("status"), 32) for host in hosts if isinstance(host, Mapping)}
    states.discard("")
    if not states:
        return
    if "eligible" not in states:
        raise TransportDenial(code, f"no eligible t3 target: {', '.join(sorted(states))}")


def _recover(
    resolution: RouteResolution,
    client: T3FleetClient,
    *,
    request_id: str,
    delegation_id: str,
    source_revision: str,
    cause: TransportDenial,
) -> RemoteOutcome:
    """Recover an unknown outcome. The original prompt is never resubmitted."""
    try:
        recovered = client.recover(request_id=request_id)
    except TransportDenial as exc:
        return _denied(
            resolution,
            exc,
            phase="dispatch",
            delegation_id=delegation_id,
            source_revision=source_revision,
            request_id=request_id,
            state=UNKNOWN_STATE,
        )
    outcome = _outcome_from(
        recovered,
        route=resolution,
        delegation_id=delegation_id,
        request_id=request_id,
        source_revision=source_revision,
    )
    if outcome.ok:
        return outcome
    return RemoteOutcome(
        ok=False,
        state=outcome.state,
        text=outcome.text,
        detail=outcome.detail or cause.message,
        failure_kind=outcome.failure_kind,
        failure_phase="dispatch",
        request_id=request_id,
        delegation_id=delegation_id,
        route=resolution,
        disposition=outcome.disposition,
        handoff=outcome.handoff,
        source_revision=source_revision,
    )


def _denied(
    resolution: RouteResolution,
    exc: TransportDenial,
    *,
    phase: str,
    delegation_id: str | None,
    source_revision: str | None,
    request_id: str | None = None,
    state: str | None = None,
) -> RemoteOutcome:
    return RemoteOutcome(
        ok=False,
        state=state or exc.code,
        detail=exc.message,
        failure_kind=exc.code,
        failure_phase=phase,
        request_id=request_id,
        delegation_id=delegation_id,
        route=resolution,
        source_revision=source_revision,
    )


# --- source transport binding ------------------------------------------------

DEFAULT_WORKLOAD = "development"


@dataclass(frozen=True)
class RemoteDispatch:
    """What ``run_transport.dispatch`` must do instead of the local lease path."""

    resolution: RouteResolution
    outcome: RemoteOutcome | None = None

    @property
    def blocked(self) -> bool:
        return self.outcome is None

    def receipt(self) -> dict[str, Any]:
        if self.outcome is not None:
            return self.outcome.receipt()
        return {
            "schema": SCHEMA,
            "consumer": CONSUMER,
            "adapter": ADAPTER,
            "route": self.resolution.route,
            "routing": self.resolution.receipt(),
        }


@dataclass
class SourceTransport:
    """Resolve one worker seat's route and, when remote, run it through T3.

    ``__call__`` returns ``None`` for an origin route, which leaves the existing
    local lease and provider path untouched. It is wired only into the worker
    dispatch path: ``planning._orchestrator_attempt`` never receives it, so
    local orchestration capacity stays distinct from worker capacity and an
    orchestrator is never recursively delegated.
    """

    cwd: Path
    run_id: str
    repo_identity: str | None
    node_id: str
    document: Mapping[str, Any]
    policy_client: Any
    client: T3FleetClient
    workload: str = DEFAULT_WORKLOAD
    machine_override: str | None = None
    override_reason: str | None = None
    wait_timeout: float = DEFAULT_WAIT_TIMEOUT
    on_handle: Callable[[str, str], None] | None = None
    _revision: str | None = None

    def session_id(self, seat: str) -> str:
        return f"{self.run_id}:{seat}"

    def parent_request_id(self, seat: str) -> str:
        return f"brigade-run:{self.run_id}:{seat}"

    def resolve(self, seat: str) -> RouteResolution:
        return resolve_route(
            seat=seat,
            session_id=self.session_id(seat),
            workload=self.workload,
            repo_identity=self.repo_identity,
            node_id=self.node_id,
            document=self.document,
            policy_client=self.policy_client,
            machine_override=self.machine_override,
            override_reason=self.override_reason,
        )

    def __call__(self, seat: str, task: str, prompt: str) -> RemoteDispatch | None:
        resolution = self.resolve(seat)
        if resolution.route == ROUTE_ORIGIN:
            return None
        if resolution.route == ROUTE_BLOCKED:
            return RemoteDispatch(resolution=resolution)
        if self._revision is None:
            try:
                self._revision = immutable_source_revision(self.cwd)
            except TransportDenial as exc:
                return RemoteDispatch(
                    resolution=resolution,
                    outcome=_denied(
                        resolution,
                        exc,
                        phase="preflight",
                        delegation_id=None,
                        source_revision=None,
                    ),
                )
        outcome = execute_remote(
            resolution,
            prompt,
            title=task,
            cwd=self.cwd,
            client=self.client,
            policy_client=self.policy_client,
            parent_request_id=self.parent_request_id(seat),
            wait_timeout=self.wait_timeout,
            source_revision=self._revision,
            on_handle=self.on_handle,
        )
        return RemoteDispatch(resolution=resolution, outcome=outcome)


def build_source_transport(
    *,
    cwd: Path | None,
    run_id: str,
    snapshot: Mapping[str, Any] | None = None,
    policy_client: Any | None = None,
    client: T3FleetClient | None = None,
    node_id: str | None = None,
    document: Mapping[str, Any] | None = None,
    machine_override: str | None = None,
    override_reason: str | None = None,
    workload: str = DEFAULT_WORKLOAD,
    on_handle: Callable[[str, str], None] | None = None,
) -> SourceTransport | None:
    """Wire the remote transport, or ``None`` to keep the existing local path.

    Returns ``None`` unless this checkout is enrolled *and* the authoritative
    policy has routing enabled. A standalone or routing-disabled fleet keeps
    every seat on the local lease and provider path, unchanged.
    """
    from . import fleet_client, fleet_client_cloud, fleet_session_bootstrap
    from . import fleet_client_policy as default_policy_client

    if cwd is None:
        return None
    loaded = snapshot if snapshot is not None else fleet_client_cloud.load_model_policy_snapshot()
    if fleet_session_bootstrap.classify_enrollment(loaded) != "enrolled":
        return None
    resolved_client = policy_client if policy_client is not None else default_policy_client
    if document is None:
        try:
            shown = resolved_client.show_policy()
        except Exception:
            return None
        document = shown.get("document") if isinstance(shown, Mapping) else None
    if not isinstance(document, Mapping):
        return None
    routing = document.get("routing")
    if not isinstance(routing, Mapping) or routing.get("enabled") is not True:
        return None
    return SourceTransport(
        cwd=cwd,
        run_id=run_id,
        repo_identity=fleet_session_bootstrap._repo_identity(cwd, None),
        node_id=node_id if node_id is not None else fleet_client.resolve_node_id(),
        document=document,
        policy_client=resolved_client,
        client=client if client is not None else T3FleetClient(cwd=cwd),
        workload=workload,
        machine_override=machine_override,
        override_reason=override_reason,
        on_handle=on_handle,
    )
