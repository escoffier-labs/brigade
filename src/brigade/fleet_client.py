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
    node_token_file = "/path/to/node-token"   # this machine's identity
    token_file = "/path/to/admin-token"       # optional: control plane only

``BRIGADE_FLEET_HUB_URL`` overrides ``hub_url``. Events and claims are sent
with the **node token** (issue #1150: the hub derives ``node_id`` from it),
from ``BRIGADE_FLEET_NODE_TOKEN`` or the ``node_token_file`` contents. The
**admin token** (``BRIGADE_FLEET_TOKEN`` / ``token_file``) is for
``brigade fleet nodes`` and reads. When no node token is configured the
client falls back to the admin token for everything — the deprecated
shared-token mode, which the hub accepts only with ``--allow-admin-writes``
— and logs one WARNING per process saying so, so existing deployments keep
working until each machine is enrolled. Empty environment values count as
unset. When no hub is configured the client is a no-op: zero behavior
change for existing users.

Identity (#1161): everything this machine sends to the hub is stamped with
its **home** identity, the ``node.toml`` under ``BRIGADE_HOME``, resolved
by ``resolve_node_id``, which ignores where it is asked. A workspace's own
``.brigade/node.toml`` stays local (per-checkout state), and an event
payload's ``node_id`` can never override the machine: both would let one
machine masquerade as a fleet of two. ``report_journal_event`` still derives
``repo`` from the workspace that owns the journal.

Phase 4 (issue #1125) adds hub-arbitrated cross-machine claims:
``acquire_claim`` / ``renew_claim`` / ``release_claim`` are one bounded
request each, and ``repo_claim`` holds a claim (with a renew heartbeat) for
the duration of a run. Hub down or unconfigured degrades to the local
run.lock; a claim failure is never a new way for a run to fail. The one
exception is a credential refusal: HTTP 401/403 are stable ``auth-failed``
outcomes (never retried, no orphan-release fallback) and ``repo_claim``
fails closed rather than running unprotected on bad credentials.

Known liveness gaps, tracked in escoffier-labs/brigade#1219: a node whose claim
renewals are selectively blocked keeps retrying without a local lease-expiry
deadline, and a worker blocked in a silent child wait may outlive a lost claim
until the queued KeyboardInterrupt is delivered.
"""

from __future__ import annotations

import _thread
import errno
import json
import logging
import os
import re
import secrets
import stat
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
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
# Cloud admission fences provider work, so its transport uses the same hard
# deadline as claims. A missing or unavailable hub is a refusal, never a
# reason to start provider work locally.
CLOUD_TIMEOUT_SECONDS = 2.5
MAX_CLOUD_RESPONSE_BYTES = 64 * 1024
# 4xx statuses that are transient or operator-fixable: keep the spool. 403
# is a credential mismatch (a node token that is not this node's, or the
# admin token on a hub without --allow-admin-writes), fixed by config.
_RETRYABLE_4XX = frozenset({401, 403, 408, 429})

_LOG = logging.getLogger("brigade.fleet")
_SPOOL_PROCESS_LOCK = threading.Lock()
# The deprecated shared-token fallback is warned about once per process,
# not once per journal append.
_SHARED_TOKEN_WARNED = False
_SHARED_TOKEN_WARNING = (
    "fleet: no node token configured; sending events and claims with the shared fleet token "
    "(deprecated shared-token mode, which the hub accepts only with --allow-admin-writes). "
    "Enroll this machine with 'brigade fleet nodes add <node_id>' and set [fleet] node_token_file "
    "in ~/.brigade/fleet.toml"
)


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


def load_fleet_settings() -> dict[str, str]:
    """Return {"hub_url", "admin_token", "node_token"}; values may be empty.

    Precedence: ``BRIGADE_FLEET_HUB_URL`` over ``[fleet] hub_url``;
    ``BRIGADE_FLEET_TOKEN`` over the contents of ``[fleet] token_file``
    (the admin token); ``BRIGADE_FLEET_NODE_TOKEN`` over the contents of
    ``[fleet] node_token_file`` (this machine's node token). An empty or
    whitespace-only environment value is treated as unset. The file is
    consulted for whichever value the environment did not set. No fallback
    between the two token kinds happens here (see ``load_fleet_config``).
    """
    hub_url = os.environ.get("BRIGADE_FLEET_HUB_URL", "").strip()
    admin_token = os.environ.get("BRIGADE_FLEET_TOKEN", "").strip()
    node_token = os.environ.get("BRIGADE_FLEET_NODE_TOKEN", "").strip()
    if not (hub_url and admin_token and node_token):
        section = _read_fleet_section(brigade_home() / FLEET_CONFIG_REL_PATH.name)
        if not hub_url and isinstance(section.get("hub_url"), str):
            hub_url = section["hub_url"].strip()
        if not admin_token:
            token_file = section.get("token_file")
            if isinstance(token_file, str) and token_file.strip():
                admin_token = _read_token_file(token_file)
        if not node_token:
            node_token_file = section.get("node_token_file")
            if isinstance(node_token_file, str) and node_token_file.strip():
                node_token = _read_token_file(node_token_file)
    return {"hub_url": hub_url, "admin_token": admin_token, "node_token": node_token}


def load_fleet_config() -> dict[str, str]:
    """Return {"hub_url": ..., "token": ...}; values may be absent (empty).

    ``token`` is the credential this machine sends as itself: its node token
    (``BRIGADE_FLEET_NODE_TOKEN`` / ``[fleet] node_token_file``), or, when
    none is configured, the shared admin token (``BRIGADE_FLEET_TOKEN`` /
    ``[fleet] token_file``) — the deprecated shared-token mode, logged as
    one WARNING per process when a hub is configured. See
    ``load_fleet_settings`` for precedence.
    """
    global _SHARED_TOKEN_WARNED
    settings = load_fleet_settings()
    token = settings["node_token"]
    if not token and settings["admin_token"]:
        token = settings["admin_token"]
        if settings["hub_url"] and not _SHARED_TOKEN_WARNED:
            _SHARED_TOKEN_WARNED = True
            _LOG.warning(_SHARED_TOKEN_WARNING)
    return {"hub_url": settings["hub_url"], "token": token}


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


def home_identity_target() -> Path:
    """Directory whose ``.brigade/node.toml`` is this machine's hub identity.

    ``node.node_path(target)`` appends ``.brigade/node.toml``, so the target
    for the default ``~/.brigade`` home is ``~`` (→ ``~/.brigade/node.toml``).
    A ``BRIGADE_HOME`` override that does not end in ``.brigade`` is used as
    the target directly (→ ``$BRIGADE_HOME/.brigade/node.toml``). This is the
    same resolution ``resolve_node_id`` and ``brigade node --machine`` use.
    """
    home = brigade_home()
    return home.parent if home.name == ".brigade" else home


def resolve_node_id(base_path: Path | None = None) -> str:
    """The machine's hub-facing node id: the identity at the per-user home
    (see ``home_identity_target``), else ``"unknown"``. Never raises.

    ``base_path`` is accepted for call-site compatibility and deliberately
    ignored (#1161): the nearest workspace ``.brigade/node.toml`` is
    workspace-local state. Two checkouts on one machine are one machine,
    so it never becomes this machine's hub identity.
    """
    from . import node as node_mod

    try:
        identity = node_mod.load_identity(home_identity_target())
    except Exception:
        identity = None
    return identity.node_id if identity is not None else "unknown"


# --- transport ---------------------------------------------------------------

# Hub traffic never goes through a configured proxy (#1154): the bearer token
# must not be handed to HTTP_PROXY/HTTPS_PROXY before it reaches the (plain
# HTTP, tailnet-bound) hub. An explicit empty ProxyHandler disables all
# proxy resolution for these requests regardless of environment.
#
# Redirects are restricted to the origin the request already targets (#1157):
# urllib's default redirect handler replays Authorization (the bearer token)
# on every hop, so a cross-origin 302 from a compromised hub or an
# on-path responder would leak it. Cross-origin redirects are refused (the
# resulting HTTPError surfaces as an ordinary transport failure). Origins
# compare with default ports normalized (#1157 round 2): http://hub and
# http://hub:80 are the same origin.

_DEFAULT_SCHEME_PORTS = {"http": 80, "https": 443}


def _origin(url: urllib.parse.SplitResult) -> tuple[str, str, int | None]:
    """(scheme, host, effective port) for a URL split; the port defaults per
    scheme when absent so explicit default ports compare equal."""
    scheme = url.scheme.lower()
    try:
        port = url.port
    except ValueError:
        # A malformed port never matches anything: refuse the redirect.
        port = -1
    if port is None:
        port = _DEFAULT_SCHEME_PORTS.get(scheme)
    return scheme, (url.hostname or "").lower(), port


def _same_origin(first: str, second: str) -> bool:
    return _origin(urllib.parse.urlsplit(first)) == _origin(urllib.parse.urlsplit(second))


class _HubRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _same_origin(req.full_url, newurl):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_HUB_OPENER = urllib.request.build_opener(
    _HubRedirectHandler(),
    urllib.request.ProxyHandler({}),
)


def _hub_open(request: urllib.request.Request, *, timeout: float):
    return _HUB_OPENER.open(request, timeout=timeout)


def _post_events_blocking(hub_url: str, token: str, body: Any, *, timeout: float) -> None:
    request = urllib.request.Request(
        hub_url.rstrip("/") + "/events",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with _hub_open(request, timeout=timeout) as response:
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


# Spool files are private (#1154): refuse a symlinked path where the OS can
# tell us (O_NOFOLLOW is POSIX-only; Windows opens through the link target
# but its per-user homes are not world-writable in the same way).
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
# Regular-file opens ignore O_NONBLOCK, but it makes an open on a FIFO or
# device fail fast instead of blocking the journal writer forever.
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
# Bounded retries for the exclusive temp-name loop in _write_spool_atomic;
# a name from secrets.token_hex(16) colliding with a planted file is
# already negligible, this only caps pathological adversarial spraying.
_SPOOL_TEMP_ATTEMPTS = 8


def _dir_fd_supported() -> bool:
    """Whether descriptor-relative spool operations are available here.

    Windows implements none of the ``dir_fd`` parameters (``os.supports_dir_fd``
    is empty there), so its spool writes must take the path-based fallback
    instead. Read at call time so the live platform decides.
    """
    if sys.platform == "win32":
        return False
    # ``os.replace`` shares ``os.rename``'s dir_fd support: supports_dir_fd
    # registers the latter only.
    return all(fn in os.supports_dir_fd for fn in (os.open, os.rename, os.unlink, os.stat))


def _ensure_private_dir(path: Path) -> int | None:
    """Create ``path`` (and parents), enforce 0700 through a directory
    descriptor, and return that descriptor for ``dir_fd`` use.

    Privacy is proven through the descriptor, not the path (#1157 round 2):
    the directory is opened ``O_DIRECTORY|O_NOFOLLOW`` so a symlink swapped
    in after any earlier check fails the open instead of redirecting every
    write below it, forced to 0700 via ``fchmod``, and re-stat'ed through
    the same descriptor to verify the bits landed. When privacy cannot be
    enforced or verified, writing is refused (``FleetClientError``) rather
    than warned about. The caller must close the returned descriptor when it
    has finished opening files beneath the directory.

    On platforms without descriptor-relative APIs (Windows: ``dir_fd`` is
    unsupported per ``os.supports_dir_fd``, which also rules out
    ``os.fchmod``), returns ``None`` so callers take their path-based
    fallback: a symlinked spool directory is still refused, privacy is a
    best-effort path ``chmod`` whose failure only warns — the documented
    platform gap (#1157 round 3).
    """
    path.mkdir(parents=True, exist_ok=True)
    if not _dir_fd_supported():
        if os.path.islink(path):
            raise FleetClientError(f"fleet spool directory {path} is a symlink")
        try:
            os.chmod(path, 0o700)
        except OSError as exc:
            _LOG.warning("fleet spool directory %s could not be forced to 0700: %s", path, exc)
        return None
    try:
        fd = os.open(str(path), os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            # A symlinked spool directory would redirect every write below
            # it; refuse instead of following attacker-chosen parents.
            raise FleetClientError(f"fleet spool directory {path} is a symlink") from exc
        if exc.errno == errno.ENOTDIR:
            raise FleetClientError(f"fleet spool directory {path} is not a directory") from exc
        raise FleetClientError(f"fleet spool directory {path} could not be opened safely: {exc}") from exc
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise FleetClientError(f"fleet spool directory {path} is not a directory")
        try:
            os.fchmod(fd, 0o700)
        except OSError as exc:
            raise FleetClientError(f"fleet spool directory {path} could not be forced to 0700: {exc}") from exc
        mode = stat.S_IMODE(os.fstat(fd).st_mode)
        if mode != 0o700:
            raise FleetClientError(
                f"fleet spool directory {path} could not be made private "
                f"(mode {mode:03o}, expected 700); refusing to write"
            )
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_private(path: Path, flags: int, *, dir_fd: int | None = None) -> int:
    """Open a spool file no-follow and 0600, refusing non-regular files.

    A symlink at the spool path raises OSError (O_NOFOLLOW) instead of
    following it; anything that is not a regular file after the open (a
    FIFO a local attacker swapped in) is closed and refused. ``dir_fd``
    (the descriptor validated by ``_ensure_private_dir``) scopes the name
    to the spool directory so a swapped parent cannot redirect it.
    """
    fd = os.open(
        str(path) if dir_fd is None else path.name,
        flags | _O_NOFOLLOW | _O_CLOEXEC | _O_NONBLOCK,
        0o600,
        dir_fd=dir_fd,
    )
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise FleetClientError(f"fleet spool {path} is not a regular file")
        # The creation mode only applies to a fresh file: an existing spool
        # file left permissive (umask change, manual chmod) keeps its mode
        # (#1157), so force it on every open.
        _fchmod = getattr(os, "fchmod", None)
        if _fchmod is not None:
            try:
                _fchmod(fd, 0o600)
            except OSError as exc:
                _LOG.warning("fleet spool %s could not be forced to 0600: %s", path, exc)
    except Exception:
        os.close(fd)
        raise
    return fd


@contextmanager
def _spool_lock(path: Path) -> Iterator[int | None]:
    """Cross-process + cross-thread guard for one node's spool.

    Mirrors ``run_journal._append_critical_section``'s lock-file shape
    (``fcntl.flock`` on an adjacent lock file) with an ``msvcrt.locking``
    branch so the same guarantee holds on Windows. Yields the spool
    directory descriptor validated by ``_ensure_private_dir`` (POSIX) so
    the guarded operations open their files ``dir_fd``, or ``None`` where
    that hardening is unavailable (Windows).
    """
    dir_fd = _ensure_private_dir(path.parent)
    lock_name = path.name + ".lock"
    try:
        with _SPOOL_PROCESS_LOCK:
            if dir_fd is not None:
                fd = os.open(lock_name, os.O_RDWR | os.O_CREAT | _O_NOFOLLOW | _O_CLOEXEC, 0o600, dir_fd=dir_fd)
            else:  # pragma: no cover - Windows
                fd = os.open(str(path.with_name(lock_name)), os.O_RDWR | os.O_CREAT | _O_CLOEXEC, 0o600)
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
                yield dir_fd
            finally:
                try:
                    if locked:
                        if fcntl is not None:
                            fcntl.flock(fd, fcntl.LOCK_UN)
                        elif msvcrt is not None:  # pragma: no cover - Windows
                            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                finally:
                    os.close(fd)
    finally:
        if dir_fd is not None:
            os.close(dir_fd)


def _encode(event: dict[str, Any]) -> str:
    return json.dumps(event, sort_keys=True)


def _read_spool(path: Path, *, dir_fd: int | None = None) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    try:
        fd = _open_private(path, os.O_RDONLY, dir_fd=dir_fd)
        with os.fdopen(fd, encoding="utf-8") as handle:
            text = handle.read()
    except (OSError, FleetClientError):
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


def _write_spool_atomic(path: Path, events: list[dict[str, Any]], *, dir_fd: int | None = None) -> None:
    """Replace the spool contents atomically; remove it when empty.

    The temp file is created exclusive with an unpredictable name in the
    spool directory (#1154), never a predictable ``.tmp`` path a local
    attacker could pre-create or symlink to. The final ``os.replace`` is a
    rename: it swaps the directory entry itself and never follows a symlink
    planted at the destination. When a validated directory descriptor is
    available, both the temp creation and the replace address it by name
    relative to that descriptor (#1157 round 3) — ``tempfile.mkstemp``
    cannot target a descriptor, so the temp is opened here with
    ``O_CREAT|O_EXCL|O_NOFOLLOW`` and 0600 — so a directory swapped in
    under the old path cannot redirect the rewrite.
    """
    if not events:
        if dir_fd is not None:
            try:
                os.unlink(path.name, dir_fd=dir_fd)
            except FileNotFoundError:
                pass
        else:
            path.unlink(missing_ok=True)
        return
    if dir_fd is None:
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write("\n".join(_encode(e) for e in events) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return
    # Descriptor-scoped rewrite (#1157 round 3): create and replace by
    # names relative to the validated descriptor.
    tmp_name = ""
    try:
        fd = -1
        for _ in range(_SPOOL_TEMP_ATTEMPTS):
            candidate = f".{path.name}.{secrets.token_hex(16)}.tmp"
            try:
                fd = os.open(
                    candidate,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC,
                    0o600,
                    dir_fd=dir_fd,
                )
            except FileExistsError:
                continue
            tmp_name = candidate
            break
        if not tmp_name:
            raise FleetClientError(f"fleet spool {path}: exhausted private temp-name attempts")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(_encode(e) for e in events) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        if tmp_name:
            try:
                os.unlink(tmp_name, dir_fd=dir_fd)
            except FileNotFoundError:
                pass
        raise


def _spool_append_locked(path: Path, event: dict[str, Any], *, dir_fd: int | None = None) -> None:
    """Append under the caller's lock, enforcing ``MAX_SPOOL_BYTES``.

    When the append would exceed the cap, the oldest events are dropped until
    the newest half of the cap remains, and the drop is logged.
    """
    line = _encode(event) + "\n"
    try:
        if dir_fd is not None:
            current = os.stat(path.name, dir_fd=dir_fd).st_size
        else:
            current = path.stat().st_size
    except OSError:
        current = 0
    if current + len(line.encode("utf-8")) > MAX_SPOOL_BYTES:
        events = _read_spool(path, dir_fd=dir_fd)
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
        _write_spool_atomic(path, kept, dir_fd=dir_fd)
        return
    try:
        fd = _open_private(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, dir_fd=dir_fd)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(line)
    except FleetClientError:
        _LOG.warning("fleet spool %s refused: not a regular file", path)
        return


def _flush_locked(
    path: Path,
    hub: str,
    token: str,
    *,
    max_batches: int | None,
    timeout: float,
    dir_fd: int | None = None,
) -> int:
    events = _read_spool(path, dir_fd=dir_fd)
    if not events:
        if dir_fd is not None:
            try:
                os.unlink(path.name, dir_fd=dir_fd)
            except FileNotFoundError:
                pass
        else:
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
    _write_spool_atomic(path, events[index:], dir_fd=dir_fd)
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
    with _spool_lock(path) as dir_fd:
        return _flush_locked(path, hub, tok, max_batches=max_batches, timeout=timeout, dir_fd=dir_fd)


# --- reporting ---------------------------------------------------------------


def report_event(
    event: dict[str, Any],
    *,
    base_path: Path | None = None,
    node_id: str | None = None,
) -> bool:
    """Report one event to the hub. Returns True when delivered.

    The event is always stamped with this machine's identity
    (``resolve_node_id``): a ``node_id`` already present in the payload, or
    passed as the ``node_id`` keyword kept for source compatibility, is
    ignored unconditionally and never sent (#1161). Best-effort by contract:
    no hub configured → False without side effects; delivery failure → the
    event lands in the local spool and False returns. When a spool already
    exists the new event is appended to it and the spool is flushed in order
    (one request, short deadline) so the hub never sees this node's events
    out of sequence. Exactly one HTTP request is made per call, bounded by
    ``REPORT_TIMEOUT_SECONDS`` end to end.

    ``Exception`` never escapes. ``KeyboardInterrupt``/``SystemExit`` are
    re-raised, but only after the event is durable in the spool.

    ``base_path`` (e.g. the journal path) is accepted and ignored: identity
    resolution never depends on where a request originated (#1161).
    """
    try:
        config = load_fleet_config()
        hub = config["hub_url"]
        if not hub:
            return False
        resolved_node_id = resolve_node_id()
        event = {**event, "node_id": resolved_node_id}
        token = config["token"]
        spool = spool_path(resolved_node_id)
    except Exception:
        return False

    interrupted: BaseException | None = None
    delivered = False
    try:
        if spool.is_file():
            with _spool_lock(spool) as dir_fd:
                _spool_append_locked(spool, event, dir_fd=dir_fd)
                try:
                    _flush_locked(spool, hub, token, max_batches=1, timeout=REPORT_TIMEOUT_SECONDS, dir_fd=dir_fd)
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
                with _spool_lock(spool) as dir_fd:
                    _spool_append_locked(spool, event, dir_fd=dir_fd)
    except (KeyboardInterrupt, SystemExit) as exc:
        # Interrupt during spool bookkeeping: the append/replace steps are
        # atomic, so the spool is consistent; make a last attempt to keep the
        # event, then propagate.
        interrupted = exc
        if not delivered:
            try:
                with _spool_lock(spool) as dir_fd:
                    if event not in _read_spool(spool, dir_fd=dir_fd):
                        _spool_append_locked(spool, event, dir_fd=dir_fd)
            except Exception as exc:
                _LOG.warning("interrupted fleet event could not be made durable in the spool: %s", exc)
    except Exception as exc:
        # A failed spool write must never be silent (#1157): whatever broke
        # it, say why before giving up on the undelivered event.
        _LOG.warning("fleet event was not delivered and could not be spooled: %s", exc)
        return False
    if interrupted is not None:
        raise interrupted
    return delivered


def report_journal_event(envelope: dict[str, Any], *, journal_path: Path | None = None) -> bool:
    """Denormalize a run_event.v1 envelope into a fleet event and report it.

    ``repo`` comes from the workspace that owns the journal so the hub view
    is a plain group-by; ``node_id`` is always this machine's home identity
    (#1161), never the journal workspace's local one. Never raises
    ``Exception``; see ``report_event`` for the interrupt contract.
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


def report_external_event(
    *,
    target: str,
    harness: str,
    role: str | None = None,
    job: str | None = None,
    session: str,
    state: str,
) -> bool:
    """Best-effort lifecycle event for an external harness session.

    The hub receives only the opaque identity labels needed to locate the
    session. Queue payloads, artifacts, headers, and credentials never enter
    the event. ``session`` contributes to the digest but is deliberately not
    a hub event field; the claim row is its lookup surface.
    """
    try:
        labels = validated_opaque_labels(harness=harness, role=role, job=job, session=session)
        event = {
            "run_id": labels["job"] or labels["session"] or session,
            "repo": target,
            "seat": labels["role"],
            "harness": labels["harness"],
            "state": state,
            "ts": datetime.now(timezone.utc).isoformat(),
            "sequence": time.time_ns(),
        }
        digest_input = json.dumps({**event, "session": labels["session"]}, sort_keys=True, separators=(",", ":"))
        event["digest"] = sha256(digest_input.encode("utf-8")).hexdigest()
    except Exception:
        return False
    return report_event(event)


def fetch_status(*, hub_url: str | None = None, include_all: bool = False) -> list[dict[str, Any]]:
    """GET /status from the hub; raises FleetClientError when unreachable."""
    config = load_fleet_config()
    hub = hub_url or config["hub_url"]
    if not hub:
        raise FleetClientError("no fleet hub configured (~/.brigade/fleet.toml [fleet] hub_url)")
    url = hub.rstrip("/") + ("/status?all=1" if include_all else "/status")
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {config['token']}"})
    try:
        with _hub_open(request, timeout=REPORT_TIMEOUT_SECONDS) as response:
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
OPAQUE_LABEL_MAX_CHARS = 128
OPAQUE_LABEL_FIELDS = ("harness", "role", "job", "session")
EXTERNAL_OUTCOMES = frozenset({"external.completed", "external.failed", "external.canceled"})


def _node_id_is_claimable(node_id: str) -> bool:
    return node_id != "unknown" and bool(_CLAIM_ID_RE.match(node_id))


def _contains_controls(value: str) -> bool:
    return any(ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F for ch in value)


def validate_opaque_label(field: str, value: str | None) -> str | None:
    """Return a stripped opaque label, or None when omitted. Raises ValueError."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > OPAQUE_LABEL_MAX_CHARS:
        raise ValueError(
            f"claim field {field!r} must be a non-empty string at most {OPAQUE_LABEL_MAX_CHARS} characters"
        )
    if _contains_controls(value):
        raise ValueError(f"claim field {field!r} must not contain control characters")
    return value.strip()


def validated_opaque_labels(**labels: str | None) -> dict[str, str | None]:
    """Validate the acquire-only identity labels. Extra keys are ignored."""
    return {field: validate_opaque_label(field, labels.get(field)) for field in OPAQUE_LABEL_FIELDS}


@dataclass(frozen=True)
class ClaimDecision:
    """Outcome of one claim operation against the hub.

    ``granted`` is True when the operation succeeded (acquired / renewed /
    released). ``reason`` explains a False: ``"held"`` (another owner holds
    the target; see ``owner``), ``"missing"`` (no live claim to renew or
    release), ``"no-hub"`` (no hub configured), ``"no-identity"`` (this node
    has no usable identity to claim with), ``"auth-failed"`` (the hub
    refused this node's credentials (HTTP 401/403), a stable outcome that
    is never retried or fallen back from; see ``FleetClaimAuthError``), or
    ``"hub-unavailable"`` (network/hub failure; callers fall back to the
    local run lock).
    ``holder`` is the fencing token this operation used; renew and release
    must present the same token. ``superseded`` is the unexpired same-node
    claim a ``supersede_dead_owner`` acquire replaced (issue #1141); a
    token-less release reports the deleted row in ``claim``.
    For a granted ``repo_claim`` hold, ``cancel_event`` is the cooperative
    cancel channel (#1157 round 2): it is set when ownership is lost under
    abort policy, and is the *required* unwind signal for guarded blocks
    entered off the main thread (where ``interrupt_main`` cannot reach).
    """

    granted: bool
    reason: str
    claim: dict[str, Any] | None = None
    owner: dict[str, Any] | None = None
    detail: str | None = None
    holder: str | None = None
    superseded: dict[str, Any] | None = None
    lock_run_dir: str | None = None
    cancel_event: threading.Event | None = None


class FleetClaimHeldError(FleetClientError):
    """The hub refused a claim because another owner holds the target."""

    def __init__(self, message: str, owner: dict[str, Any] | None = None):
        super().__init__(message)
        self.owner = owner


class FleetClaimAuthError(FleetClaimHeldError):
    """The hub refused a claim on credentials (HTTP 401/403): the token is
    unknown, revoked, or not allowed to write.

    Unlike transport failures this never degrades to the local run lock
    (#1161): retrying cannot heal an enrollment problem and running
    unprotected would pretend the hub had answered. ``repo_claim`` raises
    this instead of yielding, so dispatch fails closed.
    """


@dataclass(frozen=True)
class CloudDecision:
    """Outcome of one cloud-lease operation.

    ``holder`` is returned only to the local caller that supplied or minted
    it. The hub never returns holder tokens, and ``lease`` is restricted to
    the public lease fields before this value leaves the transport layer.
    """

    granted: bool
    reason: str
    lease: dict[str, Any] | None = None
    detail: str | None = None
    holder: str | None = None


_CLOUD_OK_KEYS = {"admit": "admitted", "bind": "bound", "renew": "renewed", "release": "released"}
_CLOUD_LEASE_FIELDS = frozenset(
    {
        "lease_id",
        "provider",
        "provider_task_id",
        "repo",
        "label",
        "owner_node",
        "owner_conductor",
        "state",
        "admitted_at",
        "renewed_at",
        "ttl_seconds",
        "expires_at",
        "artifact_ref",
        "released_at",
        "expired",
    }
)
_MODEL_POLICY_FIELDS = frozenset({"provider", "model", "seat", "enabled", "limit", "notes"})


def _bounded_json_response(response: Any) -> Any:
    """Decode one small hub response without retaining arbitrary bodies."""
    raw = response.read(MAX_CLOUD_RESPONSE_BYTES + 1)
    if len(raw) > MAX_CLOUD_RESPONSE_BYTES:
        raise FleetClientError("fleet hub cloud response exceeded the size limit")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FleetClientError("fleet hub cloud response was not valid JSON") from exc


def _post_cloud_blocking(hub_url: str, token: str, body: dict[str, Any], *, timeout: float) -> tuple[int, Any]:
    """POST one cloud operation, returning only a bounded decoded payload."""
    request = urllib.request.Request(
        hub_url.rstrip("/") + "/cloud",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with _hub_open(request, timeout=timeout) as response:
            return response.status, _bounded_json_response(response)
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 401, 403, 409):
            try:
                return exc.code, _bounded_json_response(exc)
            except FleetClientError:
                return exc.code, {}
        raise


def _post_model_policy_blocking(hub_url: str, token: str, body: dict[str, Any], *, timeout: float) -> tuple[int, Any]:
    """POST /models mutation, returning only a bounded decoded payload."""
    request = urllib.request.Request(
        hub_url.rstrip("/") + "/models",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with _hub_open(request, timeout=timeout) as response:
            return response.status, _bounded_json_response(response)
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 401, 403, 409):
            try:
                return exc.code, _bounded_json_response(exc)
            except FleetClientError:
                return exc.code, {}
        raise


def _get_cloud_blocking(hub_url: str, path: str, token: str, *, timeout: float) -> Any:
    request = urllib.request.Request(
        hub_url.rstrip("/") + path,
        headers={"Authorization": f"Bearer {token}"},
    )
    with _hub_open(request, timeout=timeout) as response:
        if response.status != 200:
            raise FleetClientError(f"fleet hub cloud request failed: HTTP {response.status}")
        return _bounded_json_response(response)


def _safe_cloud_lease(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    return {key: value for key, value in raw.items() if key in _CLOUD_LEASE_FIELDS}


def _safe_model_policy(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    return {key: value for key, value in raw.items() if key in _MODEL_POLICY_FIELDS}


def _normalize_cloud_prompt_hash(value: str | None) -> str | None:
    """Strip the cloud-tracker ``sha256:`` prefix before hub admission."""
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return stripped.removeprefix("sha256:")


def _cloud_op(
    action: str,
    *,
    provider: str | None = None,
    lease_id: str | None = None,
    holder: str | None = None,
    node_id: str | None = None,
    hub_url: str | None = None,
    token: str | None = None,
    base_path: Path | None = None,
    **fields: Any,
) -> CloudDecision:
    """One bounded cloud operation that fails closed on every uncertainty."""
    lease = lease_id or uuid4().hex
    fence = holder or uuid4().hex
    try:
        config = load_fleet_config()
        hub = hub_url or config["hub_url"]
        if not hub:
            return CloudDecision(False, "no-hub", holder=fence)
        node = node_id or resolve_node_id(base_path)
        if not _node_id_is_claimable(node):
            return CloudDecision(False, "no-identity", detail=node, holder=fence)
        body: dict[str, Any] = {"action": action, "lease_id": lease, "node_id": node, "holder": fence}
        if provider is not None:
            body["provider"] = provider
        if "prompt_hash" in fields:
            fields = dict(fields)
            fields["prompt_hash"] = _normalize_cloud_prompt_hash(fields.get("prompt_hash"))
        body.update(fields)
        status, payload = _run_with_deadline(
            lambda: _post_cloud_blocking(hub, token or config["token"], body, timeout=CLOUD_TIMEOUT_SECONDS),
            timeout=CLOUD_TIMEOUT_SECONDS,
        )
    except Exception:
        return CloudDecision(False, "hub-unavailable", holder=fence)
    payload = payload if isinstance(payload, dict) else {}
    safe_lease = _safe_cloud_lease(payload.get("lease"))
    if status in (401, 403):
        return CloudDecision(False, "auth-failed", holder=fence)
    if status == 200 and payload.get(_CLOUD_OK_KEYS[action]) is True:
        # The client minted the lease id and sent it in this request. Keep that
        # request identity authoritative even if an older or malformed hub
        # omits it from the public lease projection. Callers must always be able
        # to bind, renew, or release a successful lease.
        safe_lease = dict(safe_lease or {})
        safe_lease["lease_id"] = lease
        return CloudDecision(True, "ok", lease=safe_lease, holder=fence)
    if status in (200, 409):
        return CloudDecision(False, "refused", lease=safe_lease, holder=fence)
    return CloudDecision(False, "hub-unavailable", holder=fence)


def admit_cloud(
    provider: str,
    *,
    repo: str | None = None,
    label: str | None = None,
    prompt_hash: str | None = None,
    conductor: str | None = None,
    ttl_seconds: int = 300,
    **kwargs: Any,
) -> CloudDecision:
    return _cloud_op(
        "admit",
        provider=provider,
        repo=repo,
        label=label,
        prompt_hash=prompt_hash,
        conductor=conductor,
        ttl_seconds=ttl_seconds,
        **kwargs,
    )


def bind_cloud(
    lease_id: str, provider_task_id: str, *, artifact_ref: str | None = None, **kwargs: Any
) -> CloudDecision:
    return _cloud_op("bind", lease_id=lease_id, provider_task_id=provider_task_id, artifact_ref=artifact_ref, **kwargs)


def renew_cloud(lease_id: str, *, ttl_seconds: int = 900, **kwargs: Any) -> CloudDecision:
    return _cloud_op("renew", lease_id=lease_id, ttl_seconds=ttl_seconds, **kwargs)


def release_cloud(lease_id: str, *, state: str = "released", **kwargs: Any) -> CloudDecision:
    return _cloud_op("release", lease_id=lease_id, state=state, **kwargs)


def fetch_cloud(*, hub_url: str | None = None, include_all: bool = False) -> dict[str, Any]:
    """Return the hub's sanitized cloud snapshot, never holder capabilities."""
    config = load_fleet_config()
    hub = hub_url or config["hub_url"]
    if not hub:
        raise FleetClientError("no fleet hub configured (~/.brigade/fleet.toml [fleet] hub_url)")
    try:
        payload = _run_with_deadline(
            lambda: _get_cloud_blocking(
                hub, "/cloud?all=1" if include_all else "/cloud", config["token"], timeout=CLOUD_TIMEOUT_SECONDS
            ),
            timeout=CLOUD_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        raise FleetClientError("fleet hub cloud read failed") from exc
    if not isinstance(payload, dict):
        return {"leases": [], "policy": {}}
    leases = payload.get("leases")
    policy = payload.get("policy")
    return {
        "leases": [safe for item in leases if (safe := _safe_cloud_lease(item)) is not None]
        if isinstance(leases, list)
        else [],
        "policy": policy if isinstance(policy, dict) else {},
    }


def fetch_model_policy(*, hub_url: str | None = None) -> list[dict[str, Any]]:
    """Return the bounded, sanitized model-policy rows from the hub."""
    snapshot = load_model_policy_snapshot(hub_url=hub_url)
    state = snapshot["state"]
    if state == "unconfigured":
        raise FleetClientError("no fleet hub configured (~/.brigade/fleet.toml [fleet] hub_url)")
    if state == "auth-failed":
        raise FleetClientError("fleet hub model policy read rejected this node's credentials")
    if state != "authoritative":
        raise FleetClientError("fleet hub model policy read failed")
    return list(snapshot["models"])


def load_model_policy_snapshot(*, hub_url: str | None = None) -> dict[str, Any]:
    """Classify one bounded model-policy read for run admission.

    A missing hub preserves standalone Brigade behavior. A configured hub that
    cannot be reached is classified separately so new-run admission can fail
    closed without affecting work already in flight. Explicit credential
    rejection is also distinct. A successful read is authoritative even when
    the registry is empty.
    """
    config = load_fleet_config()
    hub = hub_url or config["hub_url"]
    if not hub:
        return {"state": "unconfigured", "models": []}
    try:
        payload = _run_with_deadline(
            lambda: _get_cloud_blocking(hub, "/models", config["token"], timeout=CLOUD_TIMEOUT_SECONDS),
            timeout=CLOUD_TIMEOUT_SECONDS,
        )
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return {"state": "auth-failed", "models": []}
        return {"state": "unavailable", "models": []}
    except Exception:
        return {"state": "unavailable", "models": []}
    models = payload.get("models") if isinstance(payload, dict) else None
    safe_models = (
        [safe for item in models if (safe := _safe_model_policy(item)) is not None] if isinstance(models, list) else []
    )
    return {"state": "authoritative", "models": safe_models}


def set_model_policy(
    provider: str, model: str, seat: str, *, enabled: bool, limit: int | None = None, notes: str | None = None
) -> dict[str, Any]:
    """Set one seat's provider/model policy with the configured admin token."""
    body = {
        "action": "set",
        "provider": provider,
        "model": model,
        "seat": seat,
        "enabled": enabled,
        "limit": limit,
        "notes": notes,
    }
    try:
        settings = load_fleet_settings()
        hub = settings["hub_url"]
        if not hub:
            raise FleetClientError("no fleet hub configured (~/.brigade/fleet.toml [fleet] hub_url)")
        admin_token = settings["admin_token"]
        if not admin_token:
            raise FleetClientError(
                "no fleet admin token configured (~/.brigade/fleet.toml [fleet] token_file or BRIGADE_FLEET_TOKEN)"
            )
        status, payload = _run_with_deadline(
            lambda: _post_model_policy_blocking(hub, admin_token, body, timeout=CLOUD_TIMEOUT_SECONDS),
            timeout=CLOUD_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        raise FleetClientError("fleet hub model policy update failed") from exc
    payload = payload if isinstance(payload, dict) else {}
    if status == 200:
        policy = _safe_model_policy(payload.get("policy"))
        return policy if policy else {}
    detail = payload.get("error") if isinstance(payload.get("error"), str) else None
    raise FleetClientError(f"fleet hub model policy update refused: HTTP {status}{': ' + detail if detail else ''}")


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
        with _hub_open(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 401, 403, 409):
            # A held/expired refusal is an answer, not a transport failure;
            # a 400 carries the hub's validation message (e.g. an older hub
            # that does not know a request field), a 403 names the node the
            # caller's token is bound to, and a 401 says why (unknown or
            # revoked token), all worth surfacing verbatim.
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
    harness: str | None = None,
    role: str | None = None,
    job: str | None = None,
    session: str | None = None,
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
        try:
            labels = validated_opaque_labels(harness=harness, role=role, job=job, session=session)
        except ValueError as exc:
            return ClaimDecision(granted=False, reason="invalid", detail=str(exc), holder=holder)
        for field, value in labels.items():
            if value is not None:
                body[field] = value
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
    if status in (401, 403):
        # A stable credential answer (#1161): retrying cannot heal it and it
        # must never be mistaken for a transport failure (which would fail
        # open onto the local lock). Callers treat "auth-failed" as final.
        return ClaimDecision(
            granted=False, reason="auth-failed", detail=detail or f"hub returned HTTP {status}", holder=holder
        )
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
    harness: str | None = None,
    role: str | None = None,
    job: str | None = None,
    session: str | None = None,
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
        harness=harness,
        role=role,
        job=job,
        session=session,
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
        with _hub_open(request, timeout=REPORT_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise FleetClientError(f"fleet hub claims failed: {exc}") from exc
    claims = payload.get("claims") if isinstance(payload, dict) else None
    return list(claims) if isinstance(claims, list) else []


# --- per-node credentials (issue #1150): admin-token control plane ----------


def _admin_request(path: str, body: dict[str, Any] | None, *, what: str) -> dict[str, Any]:
    """One ``/nodes`` request with the admin token; raises ``FleetClientError``
    with the hub's message on any refusal (the token itself never appears)."""
    settings = load_fleet_settings()
    hub = settings["hub_url"]
    if not hub:
        raise FleetClientError("no fleet hub configured (~/.brigade/fleet.toml [fleet] hub_url)")
    admin_token = settings["admin_token"]
    if not admin_token:
        raise FleetClientError(
            "no fleet admin token configured (~/.brigade/fleet.toml [fleet] token_file or BRIGADE_FLEET_TOKEN)"
        )
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Authorization": f"Bearer {admin_token}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        hub.rstrip("/") + path, data=data, headers=headers, method="POST" if data else "GET"
    )
    try:
        with _hub_open(request, timeout=REPORT_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            refusal = json.loads(exc.read().decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            refusal = {}
        detail = refusal.get("error") if isinstance(refusal, dict) and isinstance(refusal.get("error"), str) else ""
        raise FleetClientError(f"fleet hub {what} failed: HTTP {exc.code}{': ' + detail if detail else ''}") from exc
    except Exception as exc:
        raise FleetClientError(f"fleet hub {what} failed: {exc}") from exc
    return payload if isinstance(payload, dict) else {}


def add_node(node_id: str, *, label: str | None = None) -> dict[str, Any]:
    """Enroll ``node_id`` on the hub (admin token); the payload carries the
    node's token exactly once — the hub never returns it again."""
    body: dict[str, Any] = {"action": "add", "node_id": node_id}
    if label:
        body["label"] = label
    return _admin_request("/nodes", body, what="node enroll")


def revoke_node(node_id: str) -> dict[str, Any]:
    """Revoke ``node_id``'s token on the hub (admin token)."""
    return _admin_request("/nodes", {"action": "revoke", "node_id": node_id}, what="node revoke")


def fetch_nodes() -> list[dict[str, Any]]:
    """GET /nodes from the hub (admin token); raises FleetClientError when refused or unreachable."""
    nodes = _admin_request("/nodes", None, what="nodes").get("nodes")
    return list(nodes) if isinstance(nodes, list) else []


def _claim_renew_interval(ttl_seconds: int) -> float:
    return max(1.0, ttl_seconds / 3)


ORPHAN_RELEASE_RETRY_SECONDS = 30.0
EXIT_ORPHAN_RELEASE_RETRIES = 2
# A timed-out acquire can still be in flight when cleanup starts (#1157): a
# "missing" answer inside this window may precede the abandoned request's
# commit, so it is retried instead of accepted as definitive.
ORPHAN_RELEASE_UNCERTAINTY_SECONDS = 2 * CLAIM_TIMEOUT_SECONDS + 1.0

# What happens when the heartbeat learns another owner holds the claim
# (#1152): "abort" (default) interrupts the run's dispatch so two machines
# never keep working one repo unarbitrated; "continue" is the documented
# opt-out for solo machines, via BRIGADE_FLEET_CLAIM_LOSS=continue.
CLAIM_LOSS_POLICIES = ("abort", "continue")
CLAIM_LOSS_ENV = "BRIGADE_FLEET_CLAIM_LOSS"

# How long a lost-claim abort waits for the caller's ``on_claim_lost``
# callback before forcing the interrupt anyway (#1157 round 2): notifying is
# best-effort, the fail-closed abort is not.
CLAIM_LOST_CALLBACK_GRACE_SECONDS = 5.0


def _claim_loss_policy(explicit: str | None = None) -> str:
    if explicit in CLAIM_LOSS_POLICIES:
        return explicit
    value = os.environ.get(CLAIM_LOSS_ENV, "").strip().lower()
    return value if value in CLAIM_LOSS_POLICIES else "abort"


def _schedule_orphan_release(
    target: str, *, node_id: str, holder: str, conductor: str | None, ttl_seconds: int
) -> None:
    """Best-effort background cleanup after a lost acquire response.

    The hub may have committed a row for this holder that we never saw; left
    alone it would block every other machine for the full TTL. A daemon
    thread retries ``release`` until the hub gives any definitive answer
    (released, already gone, or held by someone else) or the TTL window
    passes and expiry makes the point moot. The first attempt happens
    immediately (#1157): a short-lived ``brigade run`` that lost its acquire
    response must release before process exit kills this daemon thread.
    """

    def _loop() -> None:
        started = time.monotonic()
        deadline = started + ttl_seconds
        while time.monotonic() < deadline:
            outcome = release_claim(target, node_id=node_id, holder=holder, conductor=conductor)
            if outcome.reason == "hub-unavailable":
                time.sleep(min(ORPHAN_RELEASE_RETRY_SECONDS, max(0.0, deadline - time.monotonic())))
                continue
            if outcome.reason == "missing" and time.monotonic() - started < ORPHAN_RELEASE_UNCERTAINTY_SECONDS:
                # Not definitive yet (#1157): the acquire whose response we
                # lost may still commit its row after this "missing". Keep
                # retrying until the uncertainty window closes.
                time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
                continue
            return

    threading.Thread(target=_loop, name="brigade-fleet-claim-orphan-release", daemon=True).start()


def _abort_claim_owner(
    target: str,
    reason: str,
    *,
    owner_thread: threading.Thread,
    cancel_event: threading.Event,
    on_claim_lost: Callable[[str | None], None] | None,
) -> None:
    """Fail-closed abort of a guarded block whose claim was lost (#1152).

    The interrupt is triggered independently of the caller's callback (#1157
    round 2): a watchdog daemon forces it once the callback exceeds
    ``CLAIM_LOST_CALLBACK_GRACE_SECONDS`` (a blocking callback delays
    notification, never the abort), and the callback runs under a
    ``BaseException`` catch so ``SystemExit`` raised inside it is logged
    instead of honored. The callback completes (or the grace expires)
    *before* the interrupt fires, so callers can record their state and have
    it observed by the time the interrupt lands. When the claim's owner is
    not the process main thread, ``_thread.interrupt_main()`` would stab an
    unrelated thread while the owner kept working, so the interrupt is
    replaced by the cooperative channel: ``cancel_event`` is already set and
    the guarded block must unwind through ``ClaimDecision.cancel_event``.
    The watchdog's grace fire and the callback-finally fire share one
    lock-guarded check-and-set (#1157 round 3), so the grace boundary can
    never deliver two interrupts.
    """
    owner_is_main = owner_thread is threading.main_thread()
    # Cooperative observers learn first, whatever happens to the callback.
    cancel_event.set()

    def _interrupt() -> None:
        if owner_is_main:
            _thread.interrupt_main()
        else:
            # Not a silent no-op (#1157 round 2): name the requirement.
            _LOG.warning(
                "fleet claim on %s lost (%s): repo_claim was entered off the main thread; "
                "the guarded block must unwind via ClaimDecision.cancel_event",
                target,
                reason,
            )

    if on_claim_lost is None:
        _interrupt()
        return
    done = threading.Event()
    fired = threading.Event()
    # One-shot gate (#1157 round 3): at the grace boundary the watchdog and
    # the callback's finally can both observe "not fired"; check-and-set is
    # atomic under this lock, so exactly one path interrupts.
    fired_gate = threading.Lock()

    def _fire_once(*, forced: bool) -> None:
        with fired_gate:
            if fired.is_set():
                return
            fired.set()
        if forced:
            _LOG.warning(
                "fleet claim-lost callback for %s exceeded %.1fs; forcing the abort",
                target,
                CLAIM_LOST_CALLBACK_GRACE_SECONDS,
            )
        _interrupt()

    def _watchdog() -> None:
        if done.wait(CLAIM_LOST_CALLBACK_GRACE_SECONDS) or fired.is_set():
            return
        _fire_once(forced=True)

    threading.Thread(target=_watchdog, name="brigade-fleet-claim-abort", daemon=True).start()
    try:
        on_claim_lost(reason)
    except BaseException:
        _LOG.warning("fleet claim-lost callback raised; interrupting anyway", exc_info=True)
    finally:
        done.set()
        _fire_once(forced=False)


@contextmanager
def repo_claim(
    target: str,
    *,
    base_path: Path | None = None,
    conductor: str | None = None,
    ttl_seconds: int = DEFAULT_CLAIM_TTL_SECONDS,
    lock_owner: Mapping[str, object] | None = None,
    supersede_dead_owner: Mapping[str, object] | None = None,
    on_credential_failure: Callable[[str | None], None] | None = None,
    on_claim_lost: Callable[[str | None], None] | None = None,
    claim_loss_policy: str | None = None,
) -> Iterator[ClaimDecision]:
    """Hold a hub-arbitrated claim on ``target`` for the duration of the block.

    The resilience contract from the epic: with no hub configured this is a
    no-op; a node with no usable identity, or an unreachable hub, degrades to
    today's local run.lock behavior with a single log line — never a new
    failure mode. A target held by another owner raises
    ``FleetClaimHeldError`` naming the owner and its expiry. An auth refusal
    (HTTP 401/403) also raises ``FleetClaimAuthError``, failing closed
    instead of running unprotected on credentials the hub rejected (#1161).

    Acquire mints a fencing token for this holder, so a sibling run sharing
    the node identity can never release or renew this claim. A lost acquire
    response is retried once with the same token (idempotent on the hub); if
    the hub is still unreachable a background thread keeps trying to release
    any row the lost request may have committed. A 409 with no owner (the
    target freed mid-request) is also retried once instead of failing
    closed. A granted claim is renewed on a daemon heartbeat (every ttl/3; a
    claim the hub lost is re-acquired) and released on exit, best-effort.

    When that heartbeat's renew/re-acquire is refused on credentials (the
    token was revoked mid-run), the heartbeat stops and ``on_credential_failure``
    is called once from the heartbeat thread with the hub's detail when given
    detail (#1161). The CLI passes a callback that aborts the active dispatch;
    without one the credential refusal is logged loudly and the guarded block
    keeps running under the local lock: library callers never get signals or
    process-global behavior from this context manager.

    Lost ownership (#1152) fails closed by default: when the heartbeat learns
    another owner holds the claim (a renew answered 409 held-by-another, or
    the re-acquire refused as ``held``), it logs once and aborts — with no
    ``on_claim_lost`` callback the main thread is interrupted through the
    same path as Ctrl-C, unwinding the active dispatch so two machines never
    keep working one repo unarbitrated. The abort is independent of any
    callback (#1157 round 2): a callback that blocks is cut off after
    ``CLAIM_LOST_CALLBACK_GRACE_SECONDS`` and one that raises
    ``SystemExit``/``KeyboardInterrupt`` is logged, and the interrupt fires
    either way. Pass ``on_claim_lost`` to react yourself, or set
    ``claim_loss_policy="continue"`` / the environment variable
    ``BRIGADE_FLEET_CLAIM_LOSS=continue`` as the documented opt-out for solo
    machines, restoring the old log-and-continue behavior.

    Off-main-thread owners (#1157 round 2): ``_thread.interrupt_main()``
    targets the process main thread, so when ``repo_claim`` is entered from
    a worker thread the abort cannot be delivered as a KeyboardInterrupt
    there — raising it would stab an unrelated thread. Instead the yielded
    decision's ``cancel_event`` is set (and a WARNING names it): guarded
    work off the main thread MUST observe ``decision.cancel_event`` and
    unwind promptly; that cooperative check is the documented requirement
    for non-main-thread claim owners.

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
    if not decision.granted and decision.reason == "auth-failed":
        # Fail closed (#1161): bad credentials would turn "the hub refused"
        # into "every node runs unprotected on its local lock". No retry,
        # no orphan-release fallback; enrollment is an operator fix.
        raise FleetClaimAuthError(
            f"fleet hub rejected this node's credentials for {target!r} ({decision.detail}); enroll this "
            "machine with 'brigade fleet nodes add <node_id>' and set [fleet] node_token_file in "
            "~/.brigade/fleet.toml",
            owner=decision.owner,
        )
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
    # Cooperative cancel channel (#1157 round 2): set on lost ownership so
    # the owner thread's guarded block can observe it — the only abort path
    # when repo_claim was entered off the main thread.
    cancel_event = threading.Event()
    owner_thread = threading.current_thread()

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
            if outcome.reason == "auth-failed":
                # The token was revoked (or never valid) mid-run (#1161).
                # Unlike other lost-ownership outcomes this must not be read
                # as "continue on the local lock": the credential problem is
                # the operator's to fix, so hand the news to the caller and
                # stop heartbeating. Network failures keep retrying above.
                _LOG.warning(
                    "fleet claim heartbeat for %s: the hub rejected this node's credentials (%s); "
                    "canceling the guarded run",
                    target,
                    outcome.detail,
                )
                if on_credential_failure is not None:
                    try:
                        on_credential_failure(outcome.detail)
                    except Exception:
                        _LOG.warning("fleet credential-failure callback raised; the run keeps unwinding", exc_info=True)
                return
            if _claim_loss_policy(claim_loss_policy) == "continue":
                # Documented opt-out (#1152) for solo machines: the old
                # log-and-keep-going behavior, still loud.
                _LOG.warning(
                    "fleet claim heartbeat for %s lost ownership (%s); continuing on the local run lock",
                    target,
                    outcome.reason,
                )
                return
            if outcome.reason != "held":
                # No live claim anywhere (e.g. the re-acquire raced a
                # concurrent release): nothing to arbitrate, so this keeps
                # the old log-and-stop behavior rather than interrupting.
                _LOG.warning(
                    "fleet claim heartbeat for %s lost the claim (%s); continuing on the local run lock",
                    target,
                    outcome.reason,
                )
                return
            _LOG.warning(
                "fleet claim heartbeat for %s lost ownership (%s); another owner holds the claim — "
                "aborting the guarded run (opt out with %s=continue)",
                target,
                outcome.reason,
                CLAIM_LOSS_ENV,
            )
            _abort_claim_owner(
                target,
                outcome.reason,
                owner_thread=owner_thread,
                cancel_event=cancel_event,
                on_claim_lost=on_claim_lost,
            )
            return

    heartbeat = threading.Thread(target=_renew_loop, name="brigade-fleet-claim-renew", daemon=True)
    heartbeat.start()
    try:
        yield replace(decision, cancel_event=cancel_event)
    finally:
        stop.set()
        # Drain the heartbeat before releasing: an in-flight renew/acquire
        # landing after the DELETE would resurrect the row for a full TTL.
        # The join is bounded (one renew plus one acquire round-trip); a
        # thread stuck longer than that is covered by the stop.is_set()
        # guard above.
        heartbeat.join(timeout=2 * CLAIM_TIMEOUT_SECONDS + max(1.0, CLAIM_TIMEOUT_SECONDS))
        release_outcome = release_claim(target, holder=holder, node_id=node_id, conductor=conductor)
        if pending_orphan_release.is_set() and release_outcome.reason in ("hub-unavailable", "missing"):
            # The CLI normally exits as soon as this context unwinds, so a
            # daemon retry would die before its first request. Keep the retry
            # budget inline and short: two additional calls, each already
            # bounded by CLAIM_TIMEOUT_SECONDS. A definitive "missing" is
            # retried too (#1157): an abandoned re-acquire may still be in
            # flight and commit its row right after this release was told
            # the claim is gone, which would leak it for the full TTL.
            for _ in range(EXIT_ORPHAN_RELEASE_RETRIES):
                # Space each retry across one outstanding-request timeout
                # (#1157): back-to-back retries can all complete before an
                # abandoned re-acquire commits its row; sleeping one request
                # deadline first gives that late commit time to land where
                # this release can still delete it.
                time.sleep(CLAIM_TIMEOUT_SECONDS)
                release_outcome = release_claim(target, holder=holder, node_id=node_id, conductor=conductor)
                if release_outcome.reason not in ("hub-unavailable", "missing"):
                    break
            if release_outcome.reason in ("hub-unavailable", "missing"):
                _LOG.warning(
                    "fleet claim on %s: exit cleanup could not confirm the orphaned claim's release "
                    "(last answer: %s); the row lives until its TTL expires",
                    target,
                    release_outcome.reason,
                )
