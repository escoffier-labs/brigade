"""Fleet client: best-effort event reporting to the fleet hub (issue #1123).

``report_event`` POSTs one denormalized run event to the configured hub. On
any failure the event is appended to a local spool
(``<brigade-home>/fleet-spool/<node_id>.jsonl``) and the caller proceeds;
reporting never raises into the journal writer. ``flush_spool`` re-POSTs
spooled events in order; it runs when a spool exists at report time and on
``brigade fleet flush``.

Contract (the journal writer depends on every line of this):

- **Bounded latency.** One HTTP request per ``report_event`` call, executed
  on a worker thread joined with a hard deadline (``REPORT_TIMEOUT_SECONDS``).
  The deadline covers name resolution, connect, send, and the response, so a
  hostname ``hub_url`` with a dead resolver or a slow-drip hub cannot block
  the journal writer. A request that outruns the deadline is abandoned (the
  daemon thread finishes on its own socket timeout) and the event is
  spooled; if the abandoned request did land, the hub's dedupe absorbs the
  replay.
- **Spool integrity.** Spool mutations run under a per-node lock file
  (``<spool>.lock``: ``fcntl.flock`` on POSIX, ``msvcrt.locking`` on
  Windows) plus a process-level lock, so concurrent runs on one node cannot
  erase each other's events. Partial flushes rewrite via a temp file and
  ``os.replace``; a flush that delivered nothing does not touch the file.
- **Bounded spool.** The spool is capped at ``MAX_SPOOL_BYTES``; when an
  append would exceed it the oldest events are dropped (logged once per
  drop on the ``brigade.fleet`` logger). A 4xx response other than
  401/408/429 marks the batch as poison: it is dropped and logged rather
  than retried forever. 401 (bad token), 5xx, and network failures keep the
  spool for a later flush.
- **Never raises.** ``report_event`` swallows ``Exception``.
  ``KeyboardInterrupt``/``SystemExit`` are re-raised only after the event
  has been made durable in the spool, so an interrupt never loses an event
  that the journal already committed.

Configuration (never stores secrets): ``~/.brigade/fleet.toml``::

    [fleet]
    hub_url = "http://<tailscale-ip>:3774"
    token_file = "/path/to/token"

``BRIGADE_FLEET_HUB_URL`` overrides ``hub_url``; the token comes from
``BRIGADE_FLEET_TOKEN`` or the ``token_file`` contents. Empty environment
values count as unset. When no hub is configured the client is a no-op:
zero behavior change for existing users.

Phase 4 (issue #1125) adds hub-arbitrated cross-machine claims:
``acquire_claim`` / ``renew_claim`` / ``release_claim`` are one bounded
request each, and ``repo_claim`` holds a claim (with a renew heartbeat) for
the duration of a run. Hub down or unconfigured degrades to the local
run.lock — a claim failure is never a new way for a run to fail.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import toml_compat as tomllib

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]
try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]

FLEET_CONFIG_REL_PATH = Path(".brigade") / "fleet.toml"
SPOOL_DIRNAME = "fleet-spool"
REPORT_TIMEOUT_SECONDS = 2.0
FLUSH_TIMEOUT_SECONDS = 5.0
FLUSH_BATCH_SIZE = 100
MAX_SPOOL_BYTES = 16 * 1024 * 1024
DEFAULT_CLAIM_TTL_SECONDS = 900
# One acquire is retried once, so a configured-but-blackholed hub costs at
# most 2 x this per `brigade run` before falling open on the local lock.
CLAIM_TIMEOUT_SECONDS = 2.5
# 4xx statuses that are transient or operator-fixable: keep the spool.
_RETRYABLE_4XX = frozenset({401, 408, 429})

_LOG = logging.getLogger("brigade.fleet")
_SPOOL_PROCESS_LOCK = threading.Lock()


class FleetClientError(RuntimeError):
    """Operator-facing configuration problem (never raised on report paths)."""


class _PoisonResponse(Exception):
    """The hub rejected the batch permanently (non-retryable 4xx)."""


def brigade_home() -> Path:
    """Brigade per-user home directory (override with BRIGADE_HOME for tests)."""
    override = os.environ.get("BRIGADE_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".brigade"


def load_fleet_config() -> dict[str, str]:
    """Return {"hub_url": ..., "token": ...}; values may be absent (empty).

    Precedence: ``BRIGADE_FLEET_HUB_URL`` over ``[fleet] hub_url``;
    ``BRIGADE_FLEET_TOKEN`` over the contents of ``[fleet] token_file``. An
    empty or whitespace-only environment value is treated as unset. The file
    is always consulted for whichever value the environment did not set.
    """
    hub_url = os.environ.get("BRIGADE_FLEET_HUB_URL", "").strip()
    token = os.environ.get("BRIGADE_FLEET_TOKEN", "").strip()
    if hub_url and token:
        return {"hub_url": hub_url, "token": token}
    section = _read_fleet_section(brigade_home() / FLEET_CONFIG_REL_PATH.name)
    if not hub_url and isinstance(section.get("hub_url"), str):
        hub_url = section["hub_url"].strip()
    if not token:
        token_file = section.get("token_file")
        if isinstance(token_file, str) and token_file.strip():
            token = _read_token_file(token_file)
    return {"hub_url": hub_url, "token": token}


def _read_token_file(raw_path: str) -> str:
    """Read a token file; tolerate a Windows path written with backslashes
    (tried as-is first, then with backslashes normalized to ``/``)."""
    candidates = [raw_path.strip()]
    if "\\" in raw_path:
        candidates.append(raw_path.strip().replace("\\", "/"))
    for candidate in candidates:
        try:
            return Path(candidate).expanduser().read_text(encoding="utf-8").strip()
        except OSError:
            continue
    return ""


def _read_fleet_section(config_path: Path) -> dict[str, Any]:
    if not config_path.is_file():
        return {}
    try:
        payload = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    section = payload.get("fleet") if isinstance(payload, dict) else None
    return section if isinstance(section, dict) else {}


# --- node identity ---------------------------------------------------------


def find_workspace_for_path(start: Path) -> Path | None:
    """Nearest ancestor (or ``start`` itself) that holds ``.brigade/node.toml``."""
    from . import node as node_mod

    try:
        current = Path(start).expanduser().resolve()
    except OSError:
        current = Path(start).expanduser()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        try:
            if node_mod.node_path(candidate).is_file():
                return candidate
        except OSError:
            continue
    return None


def _home_identity_target() -> Path:
    """Directory whose ``.brigade/node.toml`` is the per-user fallback identity.

    ``node.node_path(target)`` appends ``.brigade/node.toml``, so the target
    for the default ``~/.brigade`` home is ``~`` (→ ``~/.brigade/node.toml``).
    A ``BRIGADE_HOME`` override that does not end in ``.brigade`` is used as
    the target directly (→ ``$BRIGADE_HOME/.brigade/node.toml``).
    """
    home = brigade_home()
    return home.parent if home.name == ".brigade" else home


def resolve_node_id(base_path: Path | None = None) -> str:
    """Best-effort node id: nearest ancestor ``.brigade/node.toml``, then the
    per-user fallback (see ``_home_identity_target``), else ``"unknown"``.
    Never raises."""
    from . import node as node_mod

    start = base_path if base_path is not None else Path.cwd()
    try:
        workspace = find_workspace_for_path(start)
        identity = node_mod.load_identity(workspace) if workspace is not None else None
        if identity is None:
            identity = node_mod.load_identity(_home_identity_target())
    except Exception:
        identity = None
    return identity.node_id if identity is not None else "unknown"


# --- transport ---------------------------------------------------------------


def _post_events_blocking(hub_url: str, token: str, body: Any, *, timeout: float) -> None:
    request = urllib.request.Request(
        hub_url.rstrip("/") + "/events",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                raise FleetClientError(f"hub returned HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        if 400 <= exc.code < 500 and exc.code not in _RETRYABLE_4XX:
            raise _PoisonResponse(f"hub rejected batch with HTTP {exc.code}") from exc
        raise


def _run_with_deadline(fn: Callable[[], Any], *, timeout: float) -> Any:
    """Run ``fn`` on a daemon thread and join with a hard wall-clock deadline.

    The deadline covers DNS, connect, send, and the response (``fn`` must use
    the same value as its socket timeout so the abandoned thread cannot
    linger for long). Raises ``TimeoutError`` when the deadline passes, or
    whatever ``fn`` raised; otherwise returns ``fn``'s result.
    """
    outcome: dict[str, Any] = {}

    def _run() -> None:
        try:
            outcome["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - carried to the caller
            outcome["error"] = exc

    worker = threading.Thread(target=_run, name="brigade-fleet-post", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise TimeoutError(f"fleet hub request exceeded {timeout}s")
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value")


def _post_events(hub_url: str, token: str, body: Any, *, timeout: float) -> None:
    """POST /events under a hard deadline; see ``_run_with_deadline``.

    Raises ``TimeoutError`` when the deadline passes, ``_PoisonResponse`` for
    a permanent rejection, or whatever the request raised.
    """
    _run_with_deadline(lambda: _post_events_blocking(hub_url, token, body, timeout=timeout), timeout=timeout)


# --- spool -------------------------------------------------------------------


def spool_path(node_id: str) -> Path:
    safe_node_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in node_id) or "unknown"
    return brigade_home() / SPOOL_DIRNAME / f"{safe_node_id}.jsonl"


@contextmanager
def _spool_lock(path: Path) -> Iterator[None]:
    """Cross-process + cross-thread guard for one node's spool.

    Mirrors ``run_journal._append_critical_section``'s lock-file shape
    (``fcntl.flock`` on an adjacent lock file) with an ``msvcrt.locking``
    branch so the same guarantee holds on Windows.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with _SPOOL_PROCESS_LOCK:
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        locked = False
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
                locked = True
            elif msvcrt is not None:  # pragma: no cover - Windows
                while True:
                    try:
                        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                        locked = True
                        break
                    except OSError:
                        continue
            yield
        finally:
            try:
                if locked:
                    if fcntl is not None:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    elif msvcrt is not None:  # pragma: no cover - Windows
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            finally:
                os.close(fd)


def _encode(event: dict[str, Any]) -> str:
    return json.dumps(event, sort_keys=True)


def _read_spool(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return events
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


def _write_spool_atomic(path: Path, events: list[dict[str, Any]]) -> None:
    """Replace the spool contents atomically; remove it when empty."""
    if not events:
        path.unlink(missing_ok=True)
        return
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write("\n".join(_encode(e) for e in events) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _spool_append_locked(path: Path, event: dict[str, Any]) -> None:
    """Append under the caller's lock, enforcing ``MAX_SPOOL_BYTES``.

    When the append would exceed the cap, the oldest events are dropped until
    the newest half of the cap remains, and the drop is logged.
    """
    line = _encode(event) + "\n"
    try:
        current = path.stat().st_size
    except OSError:
        current = 0
    if current + len(line.encode("utf-8")) > MAX_SPOOL_BYTES:
        events = _read_spool(path)
        events.append(event)
        kept: list[dict[str, Any]] = []
        budget = MAX_SPOOL_BYTES // 2
        for candidate in reversed(events):
            size = len(_encode(candidate).encode("utf-8")) + 1
            if size > budget:
                break
            budget -= size
            kept.append(candidate)
        kept.reverse()
        dropped = len(events) - len(kept)
        _LOG.warning("fleet spool %s exceeded %d bytes; dropped %d oldest event(s)", path, MAX_SPOOL_BYTES, dropped)
        _write_spool_atomic(path, kept)
        return
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)


def _flush_locked(
    path: Path,
    hub: str,
    token: str,
    *,
    max_batches: int | None,
    timeout: float,
) -> int:
    events = _read_spool(path)
    if not events:
        path.unlink(missing_ok=True)
        return 0
    index = 0
    delivered = 0
    batches = 0
    while index < len(events) and (max_batches is None or batches < max_batches):
        batch = events[index : index + FLUSH_BATCH_SIZE]
        try:
            _post_events(hub, token, batch, timeout=timeout)
        except _PoisonResponse as exc:
            _LOG.warning("fleet spool %s: dropping %d event(s): %s", path, len(batch), exc)
            index += len(batch)
            batches += 1
            continue
        except Exception:
            break
        index += len(batch)
        delivered += len(batch)
        batches += 1
    if index == 0:
        return 0
    _write_spool_atomic(path, events[index:])
    return delivered


def flush_spool(
    node_id: str,
    *,
    hub_url: str | None = None,
    token: str | None = None,
    max_batches: int | None = None,
    timeout: float = FLUSH_TIMEOUT_SECONDS,
) -> int:
    """Re-POST spooled events in order, ``FLUSH_BATCH_SIZE`` per request.

    Delivered (or poison-dropped) events are removed from the spool via an
    atomic rewrite; undelivered ones keep their order so the next flush
    resumes where this one stopped. A flush that makes no progress leaves
    the file untouched. Returns the number of delivered events. Never raises
    for network failures.
    """
    config = load_fleet_config()
    hub = hub_url or config["hub_url"]
    tok = token or config["token"]
    path = spool_path(node_id)
    if not hub or not path.is_file():
        return 0
    with _spool_lock(path):
        return _flush_locked(path, hub, tok, max_batches=max_batches, timeout=timeout)


# --- reporting ---------------------------------------------------------------


def report_event(
    event: dict[str, Any],
    *,
    base_path: Path | None = None,
    node_id: str | None = None,
) -> bool:
    """Report one event to the hub. Returns True when delivered.

    Best-effort by contract: no hub configured → False without side effects;
    delivery failure → the event lands in the local spool and False returns.
    When a spool already exists the new event is appended to it and the spool
    is flushed in order (one request, short deadline) so the hub never sees
    this node's events out of sequence. Exactly one HTTP request is made per
    call, bounded by ``REPORT_TIMEOUT_SECONDS`` end to end.

    ``Exception`` never escapes. ``KeyboardInterrupt``/``SystemExit`` are
    re-raised, but only after the event is durable in the spool.
    """
    try:
        config = load_fleet_config()
        hub = config["hub_url"]
        if not hub:
            return False
        resolved_node_id = node_id or str(event.get("node_id") or "") or resolve_node_id(base_path)
        event = {**event, "node_id": resolved_node_id}
        token = config["token"]
        spool = spool_path(resolved_node_id)
    except Exception:
        return False

    interrupted: BaseException | None = None
    delivered = False
    try:
        if spool.is_file():
            with _spool_lock(spool):
                _spool_append_locked(spool, event)
                try:
                    _flush_locked(spool, hub, token, max_batches=1, timeout=REPORT_TIMEOUT_SECONDS)
                except (KeyboardInterrupt, SystemExit) as exc:
                    interrupted = exc
            delivered = not spool.is_file()
        else:
            try:
                _post_events(hub, token, event, timeout=REPORT_TIMEOUT_SECONDS)
                delivered = True
            except (KeyboardInterrupt, SystemExit) as exc:
                interrupted = exc
            except Exception:
                pass
            if not delivered:
                with _spool_lock(spool):
                    _spool_append_locked(spool, event)
    except (KeyboardInterrupt, SystemExit) as exc:
        # Interrupt during spool bookkeeping: the append/replace steps are
        # atomic, so the spool is consistent; make a last attempt to keep the
        # event, then propagate.
        interrupted = exc
        if not delivered:
            try:
                with _spool_lock(spool):
                    if event not in _read_spool(spool):
                        _spool_append_locked(spool, event)
            except Exception:
                pass
    except Exception:
        return False
    if interrupted is not None:
        raise interrupted
    return delivered


def report_journal_event(envelope: dict[str, Any], *, journal_path: Path | None = None) -> bool:
    """Denormalize a run_event.v1 envelope into a fleet event and report it.

    Identity fields (node_id, repo) come from the workspace that owns the
    journal so the hub view is a plain group-by. Never raises ``Exception``;
    see ``report_event`` for the interrupt contract.
    """
    try:
        raw_payload = envelope.get("payload")
        payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}
        workspace = find_workspace_for_path(journal_path) if journal_path is not None else None
        event = {
            "run_id": envelope.get("run_id"),
            "repo": workspace.name if workspace is not None else None,
            "seat": payload.get("seat"),
            "harness": payload.get("harness"),
            "state": envelope.get("event_type"),
            "ts": envelope.get("recorded_at"),
            "sequence": envelope.get("sequence"),
            "digest": envelope.get("event_digest"),
        }
        if not all(event[k] is not None for k in ("run_id", "state", "ts", "sequence", "digest")):
            return False
    except Exception:
        return False
    return report_event(event, base_path=journal_path)


def fetch_status(*, hub_url: str | None = None, include_all: bool = False) -> list[dict[str, Any]]:
    """GET /status from the hub; raises FleetClientError when unreachable."""
    config = load_fleet_config()
    hub = hub_url or config["hub_url"]
    if not hub:
        raise FleetClientError("no fleet hub configured (~/.brigade/fleet.toml [fleet] hub_url)")
    url = hub.rstrip("/") + ("/status?all=1" if include_all else "/status")
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {config['token']}"})
    try:
        with urllib.request.urlopen(request, timeout=REPORT_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise FleetClientError(f"fleet hub status failed: {exc}") from exc
    runs = payload.get("runs") if isinstance(payload, dict) else None
    return list(runs) if isinstance(runs, list) else []


# --- hub-arbitrated claims (issue #1125, phase 4) ----------------------------


# Mirrors the hub's CLAIM_ID_PATTERN: a claimable identity is well-formed and
# never the literal "unknown" (two identity-less machines must not both be
# granted the same target).
_CLAIM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _node_id_is_claimable(node_id: str) -> bool:
    return node_id != "unknown" and bool(_CLAIM_ID_RE.match(node_id))


@dataclass(frozen=True)
class ClaimDecision:
    """Outcome of one claim operation against the hub.

    ``granted`` is True when the operation succeeded (acquired / renewed /
    released). ``reason`` explains a False: ``"held"`` (another owner holds
    the target; see ``owner``), ``"missing"`` (no live claim to renew or
    release), ``"no-hub"`` (no hub configured), ``"no-identity"`` (this node
    has no usable identity to claim with), or ``"hub-unavailable"``
    (network/hub failure — callers fall back to the local run lock).
    ``holder`` is the fencing token this operation used; renew and release
    must present the same token. ``superseded`` is the unexpired same-node
    claim a ``supersede_dead_owner`` acquire replaced (issue #1141); a
    token-less release reports the deleted row in ``claim``.
    """

    granted: bool
    reason: str
    claim: dict[str, Any] | None = None
    owner: dict[str, Any] | None = None
    detail: str | None = None
    holder: str | None = None
    superseded: dict[str, Any] | None = None
    lock_run_dir: str | None = None


class FleetClaimHeldError(FleetClientError):
    """The hub refused a claim because another owner holds the target."""

    def __init__(self, message: str, owner: dict[str, Any] | None = None):
        super().__init__(message)
        self.owner = owner


def resolve_claim_target(base_path: Path | None = None) -> str:
    """Stable cross-machine claim key for the repo at ``base_path``.

    The owning workspace directory name — the same value the event reporter
    uses for ``repo`` — so claims and status rows group on one key. Falls
    back to the directory's own name when no workspace identity exists.
    """
    start = Path(base_path) if base_path is not None else Path.cwd()
    workspace = find_workspace_for_path(start)
    if workspace is not None:
        return workspace.name
    try:
        resolved = start.expanduser().resolve()
    except OSError:
        resolved = start.expanduser()
    return resolved.name or "unknown"


def _post_claim_blocking(hub_url: str, token: str, body: dict[str, Any], *, timeout: float) -> tuple[int, Any]:
    request = urllib.request.Request(
        hub_url.rstrip("/") + "/claims",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 409):
            # A held/expired refusal is an answer, not a transport failure;
            # a 400 carries the hub's validation message (e.g. an older hub
            # that does not know a request field), worth surfacing verbatim.
            try:
                return exc.code, json.loads(exc.read().decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                return exc.code, {}
        raise


def lease_from_lock_owner(owner: Mapping[str, object] | None) -> dict[str, str] | None:
    """The hub-facing lease of a ``run.lock`` owner payload (``runguard``):
    ``{"token", "acquired_at"?, "run_dir"?}``, or ``None`` when the payload
    carries no owner token (an unattributable lock proves nothing)."""
    if not isinstance(owner, Mapping):
        return None
    token = owner.get("owner_token")
    if not isinstance(token, str) or not token.strip():
        return None
    lease = {"token": token.strip()}
    for key in ("acquired_at", "run_dir"):
        value = owner.get(key)
        if isinstance(value, str) and value.strip():
            lease[key] = value.strip()
    return lease


_CLAIM_OK_KEYS = {"acquire": "granted", "renew": "renewed", "release": "released", "inspect": "inspected"}


def _claim_op(
    action: str,
    target: str,
    *,
    holder: str | None,
    node_id: str | None = None,
    conductor: str | None = None,
    ttl_seconds: int = DEFAULT_CLAIM_TTL_SECONDS,
    hub_url: str | None = None,
    token: str | None = None,
    base_path: Path | None = None,
    scope: str = "holder",
    lock: Mapping[str, str] | None = None,
    supersede: Mapping[str, str] | None = None,
    acquired_at: str | None = None,
) -> ClaimDecision:
    """One claim request; never raises for network or hub failures.

    ``scope`` is the hub's request scope (``holder`` / ``node`` / ``force``,
    see ``fleet_hub``); ``holder`` may be ``None`` only for a token-less
    (``node`` / ``force``) release. ``lock`` is this run's local lease,
    recorded on the row at acquire; ``supersede`` is the dead lease a
    node-scoped acquire presents (see ``lease_from_lock_owner``).
    """
    try:
        config = load_fleet_config()
        hub = hub_url or config["hub_url"]
        if not hub:
            return ClaimDecision(granted=False, reason="no-hub", holder=holder)
        resolved_node = node_id or resolve_node_id(base_path)
        if not _node_id_is_claimable(resolved_node):
            return ClaimDecision(granted=False, reason="no-identity", detail=resolved_node, holder=holder)
        body: dict[str, Any] = {
            "action": action,
            "target": target,
            "node_id": resolved_node,
            "ttl_seconds": int(ttl_seconds),
        }
        if holder is not None:
            body["holder"] = holder
        if scope != "holder":
            body["scope"] = scope
        if lock is not None:
            body["lock"] = dict(lock)
        if supersede is not None:
            body["supersede"] = dict(supersede)
        if acquired_at is not None:
            body["acquired_at"] = acquired_at
        if conductor:
            body["conductor"] = conductor
        tok = token or config["token"]
        status, payload = _run_with_deadline(
            lambda: _post_claim_blocking(hub, tok, body, timeout=CLAIM_TIMEOUT_SECONDS),
            timeout=CLAIM_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        return ClaimDecision(granted=False, reason="hub-unavailable", detail=str(exc), holder=holder)
    payload = payload if isinstance(payload, dict) else {}
    detail = payload.get("error") if isinstance(payload.get("error"), str) else None
    owner = payload.get("owner") if isinstance(payload.get("owner"), dict) else None
    if status == 200 and payload.get(_CLAIM_OK_KEYS[action]) is True:
        claim = payload.get("claim") if isinstance(payload.get("claim"), dict) else None
        superseded = payload.get("superseded") if isinstance(payload.get("superseded"), dict) else None
        run_dir = payload.get("lock_run_dir") if isinstance(payload.get("lock_run_dir"), str) else None
        return ClaimDecision(
            granted=True, reason="ok", claim=claim, holder=holder, superseded=superseded, lock_run_dir=run_dir
        )
    if status in (200, 409):
        return ClaimDecision(
            granted=False, reason="held" if owner is not None else "missing", owner=owner, detail=detail, holder=holder
        )
    return ClaimDecision(
        granted=False, reason="hub-unavailable", detail=detail or f"hub returned HTTP {status}", holder=holder
    )


def acquire_claim(
    target: str,
    *,
    holder: str | None = None,
    lock_owner: Mapping[str, object] | None = None,
    supersede_dead_owner: Mapping[str, object] | None = None,
    **kwargs: Any,
) -> ClaimDecision:
    """Acquire ``target``; a fresh fencing token is minted unless ``holder``
    is passed (pass the same token to retry a lost response idempotently).

    ``lock_owner`` is this run's ``run.lock`` owner payload; its lease is
    recorded on the claim row so a later same-node supersede can match this
    exact row. ``supersede_dead_owner`` is the owner payload of the dead
    lock the local lease reconcile just released (issue #1141): the acquire
    then also takes over an unexpired claim held by this node under that
    exact lease — never another node's, never a row taken under a different
    or newer lease — and the replaced row is returned as
    ``ClaimDecision.superseded``. An owner payload without a token (an
    unattributable lock) supersedes nothing.
    """
    supersede = lease_from_lock_owner(supersede_dead_owner)
    return _claim_op(
        "acquire",
        target,
        holder=holder or uuid4().hex,
        scope="node" if supersede is not None else "holder",
        lock=lease_from_lock_owner(lock_owner),
        supersede=supersede,
        **kwargs,
    )


def renew_claim(target: str, *, holder: str, **kwargs: Any) -> ClaimDecision:
    return _claim_op("renew", target, holder=holder, **kwargs)


def release_claim(
    target: str, *, holder: str | None = None, force: bool = False, acquired_at: str | None = None, **kwargs: Any
) -> ClaimDecision:
    """Release ``target``.

    With ``holder`` the release is fenced to that token (the run's own
    release). Without one it is an operator release (issue #1141): the hub
    deletes the row only if this node owns it (``reason="held"`` names the
    real owner otherwise), or with ``force`` whoever owns it. A non-force
    operator release must pass the ``acquired_at`` of the claim it
    inspected — the hub refuses an unfenced node-scoped delete — so the
    delete hits that exact row (a claim re-acquired since is refused as
    ``held``). The deleted row comes back as ``ClaimDecision.claim``.
    """
    kwargs.pop("ttl_seconds", None)
    scope = "force" if force else ("holder" if holder is not None else "node")
    return _claim_op("release", target, holder=holder, scope=scope, acquired_at=acquired_at, **kwargs)


def inspect_claim(target: str, **kwargs: Any) -> ClaimDecision:
    """The current claim on ``target`` (``claim`` is ``None`` when free).
    When this node owns it, ``lock_run_dir`` is the run directory recorded
    at acquire, so an operator release can first check that run's lock is
    dead (issue #1141). Never raises; ``reason`` explains a failure."""
    kwargs.pop("ttl_seconds", None)
    return _claim_op("inspect", target, holder=None, **kwargs)


def fetch_claims(*, hub_url: str | None = None, include_all: bool = False) -> list[dict[str, Any]]:
    """GET /claims from the hub; raises FleetClientError when unreachable."""
    config = load_fleet_config()
    hub = hub_url or config["hub_url"]
    if not hub:
        raise FleetClientError("no fleet hub configured (~/.brigade/fleet.toml [fleet] hub_url)")
    url = hub.rstrip("/") + ("/claims?all=1" if include_all else "/claims")
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {config['token']}"})
    try:
        with urllib.request.urlopen(request, timeout=REPORT_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise FleetClientError(f"fleet hub claims failed: {exc}") from exc
    claims = payload.get("claims") if isinstance(payload, dict) else None
    return list(claims) if isinstance(claims, list) else []


def _claim_renew_interval(ttl_seconds: int) -> float:
    return max(1.0, ttl_seconds / 3)


ORPHAN_RELEASE_RETRY_SECONDS = 30.0
EXIT_ORPHAN_RELEASE_RETRIES = 2


def _schedule_orphan_release(
    target: str, *, node_id: str, holder: str, conductor: str | None, ttl_seconds: int
) -> None:
    """Best-effort background cleanup after a lost acquire response.

    The hub may have committed a row for this holder that we never saw; left
    alone it would block every other machine for the full TTL. A daemon
    thread retries ``release`` until the hub gives any definitive answer
    (released, already gone, or held by someone else) or the TTL window
    passes and expiry makes the point moot.
    """

    def _loop() -> None:
        deadline = time.monotonic() + ttl_seconds
        while time.monotonic() < deadline:
            time.sleep(min(ORPHAN_RELEASE_RETRY_SECONDS, max(0.0, deadline - time.monotonic())))
            outcome = release_claim(target, node_id=node_id, holder=holder, conductor=conductor)
            if outcome.reason != "hub-unavailable":
                return

    threading.Thread(target=_loop, name="brigade-fleet-claim-orphan-release", daemon=True).start()


@contextmanager
def repo_claim(
    target: str,
    *,
    base_path: Path | None = None,
    conductor: str | None = None,
    ttl_seconds: int = DEFAULT_CLAIM_TTL_SECONDS,
    lock_owner: Mapping[str, object] | None = None,
    supersede_dead_owner: Mapping[str, object] | None = None,
) -> Iterator[ClaimDecision]:
    """Hold a hub-arbitrated claim on ``target`` for the duration of the block.

    The resilience contract from the epic: with no hub configured this is a
    no-op; a node with no usable identity, or an unreachable hub, degrades to
    today's local run.lock behavior with a single log line — never a new
    failure mode. A target held by another owner raises
    ``FleetClaimHeldError`` naming the owner and its expiry.

    Acquire mints a fencing token for this holder, so a sibling run sharing
    the node identity can never release or renew this claim. A lost acquire
    response is retried once with the same token (idempotent on the hub); if
    the hub is still unreachable a background thread keeps trying to release
    any row the lost request may have committed. A 409 with no owner (the
    target freed mid-request) is also retried once instead of failing
    closed. A granted claim is renewed on a daemon heartbeat (every ttl/3; a
    claim the hub lost is re-acquired) and released on exit, best-effort.

    ``lock_owner`` is the ``run.lock`` owner payload of the lease this run
    holds; it is recorded on the claim row (and re-sent by the heartbeat's
    re-acquire). ``supersede_dead_owner`` (issue #1141) is for the caller
    whose acquire of that lease reconciled a dead previous owner: a
    SIGKILLed run leaves an unexpired hub claim under this node with a
    token nobody has, and without this the node is locked out of its own
    repo for the residual TTL. The acquire then replaces the claim taken
    under exactly that dead lease (never another node's, never a claim a
    run in another directory or under a newer lease holds) and logs the
    supersede once. The heartbeat's re-acquire never supersedes.
    """
    try:
        hub_configured = bool(load_fleet_config()["hub_url"])
    except Exception:
        hub_configured = False
    if not hub_configured:
        yield ClaimDecision(granted=False, reason="no-hub")
        return
    node_id = resolve_node_id(base_path)
    if not _node_id_is_claimable(node_id):
        _LOG.warning("no usable fleet node identity (%r); relying on the local run lock alone for %s", node_id, target)
        yield ClaimDecision(granted=False, reason="no-identity", detail=node_id)
        return
    holder = uuid4().hex
    acquire_kwargs: dict[str, Any] = {
        "holder": holder,
        "node_id": node_id,
        "conductor": conductor,
        "ttl_seconds": ttl_seconds,
        "lock_owner": lock_owner,
    }
    decision = acquire_claim(target, supersede_dead_owner=supersede_dead_owner, **acquire_kwargs)
    if not decision.granted and decision.reason in ("hub-unavailable", "missing"):
        # One retry with the same fencing token: a lost response may have
        # committed the row (idempotent for this holder), and a 409 with no
        # owner means the target freed mid-request.
        decision = acquire_claim(target, supersede_dead_owner=supersede_dead_owner, **acquire_kwargs)
    if not decision.granted and decision.reason == "hub-unavailable":
        _LOG.warning("fleet hub unreachable (%s); falling back to the local run lock for %s", decision.detail, target)
        _schedule_orphan_release(target, node_id=node_id, holder=holder, conductor=conductor, ttl_seconds=ttl_seconds)
        yield decision
        return
    if not decision.granted and decision.reason == "missing":
        # Twice refused with no owner to blame: a hub inconsistency, not a
        # real holder. Fail open on the local lock rather than refusing a
        # target nobody holds.
        _LOG.warning("fleet hub refused %s twice without naming an owner; relying on the local run lock", target)
        yield decision
        return
    if not decision.granted:
        owner = decision.owner or {}
        held_by = owner.get("owner_node") or "another node"
        until = owner.get("expires_at") or "its TTL expires"
        if held_by == node_id:
            # Our own node, a token we do not have: a sibling run, or a
            # crashed one whose lease this run did not reconcile. Name the
            # documented ways out (issue #1141) instead of "wait for TTL".
            hint = (
                "this node already holds it under another lease (a sibling run, a same-name workspace, or a "
                f"crashed run this run did not reconcile); free it with `brigade fleet claims --release {target}` "
                "or run with --no-fleet-claim"
            )
        else:
            hint = "another machine or conductor is working this repo (wait for release or TTL expiry)"
        raise FleetClaimHeldError(
            f"repo {target!r} is claimed by node {held_by} until {until}; {hint}", owner=decision.owner
        )
    if decision.superseded is not None:
        _LOG.warning(
            "fleet claim on %s superseded this node's unexpired claim (conductor %s, acquired %s): "
            "the local lease reconcile found that run dead",
            target,
            decision.superseded.get("owner_conductor") or "-",
            decision.superseded.get("acquired_at") or "-",
        )
    stop = threading.Event()
    pending_orphan_release = threading.Event()

    def _renew_loop() -> None:
        warned_unavailable = False
        while not stop.wait(_claim_renew_interval(ttl_seconds)):
            outcome = renew_claim(target, holder=holder, node_id=node_id, conductor=conductor, ttl_seconds=ttl_seconds)
            if not outcome.granted and outcome.reason == "missing":
                if stop.is_set():
                    # "missing" during shutdown is our own release landing
                    # first; re-acquiring here would resurrect the released
                    # claim for a full TTL that nothing ever frees.
                    return
                # The hub lost or expired our row (e.g. hub restart from
                # backup); take it back under the same fencing token and
                # lease, never superseding. Record the possible orphan
                # before starting the request so shutdown cannot miss it if
                # the bounded heartbeat join ends while the re-acquire is
                # still in flight.
                pending_orphan_release.set()
                outcome = acquire_claim(target, **acquire_kwargs)
            if outcome.granted:
                warned_unavailable = False
                continue
            if outcome.reason == "hub-unavailable":
                if not warned_unavailable:
                    _LOG.warning("fleet claim heartbeat for %s: hub unreachable; retrying", target)
                    warned_unavailable = True
                continue
            _LOG.warning(
                "fleet claim heartbeat for %s lost ownership (%s); continuing on the local run lock",
                target,
                outcome.reason,
            )
            return

    heartbeat = threading.Thread(target=_renew_loop, name="brigade-fleet-claim-renew", daemon=True)
    heartbeat.start()
    try:
        yield decision
    finally:
        stop.set()
        # Drain the heartbeat before releasing: an in-flight renew/acquire
        # landing after the DELETE would resurrect the row for a full TTL.
        # The join is bounded (one renew plus one acquire round-trip); a
        # thread stuck longer than that is covered by the stop.is_set()
        # guard above.
        heartbeat.join(timeout=2 * CLAIM_TIMEOUT_SECONDS + max(1.0, CLAIM_TIMEOUT_SECONDS))
        release_outcome = release_claim(target, holder=holder, node_id=node_id, conductor=conductor)
        if pending_orphan_release.is_set() and release_outcome.reason == "hub-unavailable":
            # The CLI normally exits as soon as this context unwinds, so a
            # daemon retry would die before its first request. Keep the retry
            # budget inline and short: two additional calls, each already
            # bounded by CLAIM_TIMEOUT_SECONDS.
            for _ in range(EXIT_ORPHAN_RELEASE_RETRIES):
                release_outcome = release_claim(target, holder=holder, node_id=node_id, conductor=conductor)
                if release_outcome.reason != "hub-unavailable":
                    break
