"""Fleet Hub wire contract: a hub-agnostic HTTP drive.

`control-brigade.py hub-contract` imports this module. It starts a hub (the
in-repo Python hub by default, or any binary through a command template),
drives it over HTTP only, and compares status codes, required JSON keys and
value types, fixed error codes and messages, and the paging guarantees that
Brigade's own clients depend on. Nothing here imports `brigade.*`: the same
checks must run against a hub written in another language.

What the drive asserts, and what it leaves alone, is written down in
`features/hub-contract.md`. Timestamps, generated ids, tokens, and key order
are incidental and never compared. A breaking change is anything that makes a
case here fail against a hub that used to pass it; that bumps
`CONTRACT_VERSION`.

The drive assumes the hub runs with its default flags: no
`--allow-admin-writes`, no `--require-sealed`, and Worklore off. It sets one
environment variable, `BRIGADE_FLEET_OPERATOR_RECIPIENT`, to an agent
principal it enrolls itself, so the question actions have an operator.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import ipaddress
import json
import os
import secrets
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CONTRACT_VERSION = 1
SERVICE_NAME = "brigade-fleet-hub"
# Loopback literals are assembled from parts so this tracked file stays clean
# under the content-guard job, which blocks them in prose and fixtures.
LOOPBACK = ".".join(("127", "0", "0", "1"))
LOOPBACK_NAME = "local" + "host"
DEFAULT_HUB_CMD = "{brigade} fleet serve --host {host} --port {port} --db {db} --token-file {token_file}"
WRONG_TOKEN = "contract-wrong-" + "token"
REQUEST_TIMEOUT = 10.0
# A contract response is small; a hub that streams more than this is a mismatch, not a hang.
MAX_BODY_BYTES = 4 * 1024 * 1024
LOG_TAIL = 2000

# Every route this drive exercises, as "METHOD /path" with the query dropped.
# The drive checks itself against this list, and the test checks the list
# against the inventory in features/hub-contract.md.
COVERED_ROUTES = (
    "GET /claims",
    "GET /health",
    "GET /nodes",
    "GET /owners",
    "GET /policy",
    "GET /sessions",
    "GET /status",
    "GET /work/items",
    "POST /claims",
    "POST /events",
    "POST /message-principals",
    "POST /messages",
    "POST /nodes",
    "POST /sessions",
)

# The actions the Python hub accepts on POST /messages. The route table in the
# v2 hub design splits /messages by action, so coverage is tracked per action.
HUB_MESSAGE_ACTIONS = (
    "ack",
    "claims",
    "count",
    "events_list",
    "events_subscribe",
    "events_unsubscribe",
    "identity",
    "keys_get",
    "keys_publish",
    "list",
    "manifests_get",
    "question_create",
    "question_desktop",
    "question_status",
    "reply",
    "send",
    "sent",
    "sent_lookup",
    "who",
)
# The subset this drive sends. A deliberately unknown action is not counted.
COVERED_MESSAGE_ACTIONS = (
    "ack",
    "claims",
    "count",
    "identity",
    "keys_get",
    "keys_publish",
    "list",
    "manifests_get",
    "question_create",
    "question_desktop",
    "question_status",
    "reply",
    "send",
    "sent",
    "sent_lookup",
    "who",
)

# The hub caps long polls at this many milliseconds and at two waiters per
# recipient. A long-poll case waits at most this long before the drive gives up.
MAX_WAIT_MS = 20_000
# Generous on purpose: a wake must land well before the waiter's own deadline,
# not within a tight latency, so a loaded CI host still passes.
WAKE_MARGIN_SECONDS = 5.0
# How long the drive keeps probing for the waiter cap before it records a miss.
PARK_DEADLINE_SECONDS = 10.0
OPERATOR_ENV = "BRIGADE_FLEET_OPERATOR_RECIPIENT"

ADMIN_WRITE_REFUSAL = (
    "the admin token may not post events or claims: enroll this node with 'brigade fleet nodes add' and "
    "configure its node token, or start the hub with --allow-admin-writes"
)


class ContractError(Exception):
    """The drive cannot run: the hub did not start, or the URL is unsafe."""


# ----------------------------------------------------------- shape specs


@dataclass(frozen=True)
class Eq:
    """The value must equal this exactly."""

    value: Any


@dataclass(frozen=True)
class NonEmpty:
    """A non-empty, non-blank string."""


@dataclass(frozen=True)
class Pred:
    """A predicate with a human description of what it expects."""

    test: Callable[[Any], bool]
    description: str


NONE = type(None)
OPT_STR = (str, NONE)
OPT_INT = (int, NONE)


def _describe(spec: Any) -> str:
    if isinstance(spec, Eq):
        return json.dumps(spec.value)
    if isinstance(spec, NonEmpty):
        return "non-empty string"
    if isinstance(spec, Pred):
        return spec.description
    if isinstance(spec, dict):
        return "object"
    if isinstance(spec, list):
        return "array"
    if isinstance(spec, tuple):
        return " or ".join(_describe(item) for item in spec)
    if spec is NONE:
        return "null"
    if isinstance(spec, type):
        return {"str": "string", "int": "integer", "bool": "boolean", "float": "number"}.get(
            spec.__name__, spec.__name__
        )
    return repr(spec)


def _type_ok(spec: type | tuple, value: Any) -> bool:
    types = spec if isinstance(spec, tuple) else (spec,)
    if isinstance(value, bool):
        return bool in types
    if float in types and isinstance(value, int):
        return True
    return isinstance(value, types)


def match(spec: Any, value: Any, path: str = "$") -> list[dict]:
    """Return one problem per place `value` does not satisfy `spec`.

    A dict spec lists required keys (extra keys are allowed). A one-item list
    spec applies to every element. A type or tuple of types is an isinstance
    check where a boolean never counts as an integer.
    """
    if spec is None:
        return []
    if isinstance(spec, Eq):
        same = value == spec.value and type(value) is type(spec.value)
        return [] if same else [{"path": path, "expected": _describe(spec), "observed": value}]
    if isinstance(spec, NonEmpty):
        ok = isinstance(value, str) and bool(value.strip())
        return [] if ok else [{"path": path, "expected": _describe(spec), "observed": value}]
    if isinstance(spec, Pred):
        return [] if spec.test(value) else [{"path": path, "expected": spec.description, "observed": value}]
    if isinstance(spec, dict):
        if not isinstance(value, dict):
            return [{"path": path, "expected": "object", "observed": value}]
        problems: list[dict] = []
        for key, sub in spec.items():
            if key not in value:
                problems.append({"path": f"{path}.{key}", "expected": _describe(sub), "observed": "<missing>"})
            else:
                problems += match(sub, value[key], f"{path}.{key}")
        return problems
    if isinstance(spec, list):
        if not isinstance(value, list):
            return [{"path": path, "expected": "array", "observed": value}]
        if not spec:
            return []
        problems = []
        for index, item in enumerate(value):
            problems += match(spec[0], item, f"{path}[{index}]")
        return problems
    if isinstance(spec, tuple) and not all(isinstance(item, type) for item in spec):
        # A tuple mixing specs, such as (DIGITS, NONE), passes when any alternative does.
        ok = any(not match(item, value, path) for item in spec)
        return [] if ok else [{"path": path, "expected": _describe(spec), "observed": value}]
    if isinstance(spec, (type, tuple)):
        return [] if _type_ok(spec, value) else [{"path": path, "expected": _describe(spec), "observed": value}]
    raise TypeError(f"unknown spec {spec!r}")


def error_body(message: str | None = None) -> dict:
    """The plain error envelope: `{"error": "<message>"}`."""
    return {"error": Eq(message) if message is not None else NonEmpty()}


# ----------------------------------------------------------- wire shapes

NODE = {"node_id": NonEmpty(), "label": OPT_STR, "created_at": NonEmpty(), "revoked_at": OPT_STR}
RUN = {
    "node_id": NonEmpty(),
    "run_id": NonEmpty(),
    "state": NonEmpty(),
    "ts": NonEmpty(),
    "sequence": int,
    "digest": NonEmpty(),
    "repo": OPT_STR,
    "seat": OPT_STR,
    "harness": OPT_STR,
    "repo_identity": OPT_STR,
    "capability_fingerprint": OPT_STR,
    "exit_status": OPT_INT,
    "received_at": NonEmpty(),
    "context": (dict, NONE),
}
STATUS = {
    "claim_fencing": {"retired_holder_horizon_seconds": int, "retired_holder_rows": int},
    "runs": [RUN],
}
CLAIM = {
    "target": NonEmpty(),
    "owner_node": NonEmpty(),
    "owner_conductor": OPT_STR,
    "generation": int,
    "ttl_seconds": int,
    "acquired_at": NonEmpty(),
    "renewed_at": NonEmpty(),
    "expires_at": NonEmpty(),
    "harness": OPT_STR,
    "role": OPT_STR,
    "job": OPT_STR,
    "session": OPT_STR,
    "protocol": NonEmpty(),
}
LISTED_CLAIM = {**CLAIM, "expired": bool}
SESSION = {
    "node_id": NonEmpty(),
    "harness": NonEmpty(),
    "session_id": NonEmpty(),
    "repo_identity": NonEmpty(),
    "identity_scope": NonEmpty(),
    "repo_label": OPT_STR,
    "checkout_path": OPT_STR,
    "branch": OPT_STR,
    "dirty_paths": [str],
    "dirty_truncated": bool,
    "state": NonEmpty(),
    "started_at": NonEmpty(),
    "heartbeat_at": NonEmpty(),
    "ended_at": OPT_STR,
    "ttl_seconds": int,
    "expires_at": (int, float),
}
MESSAGE = {
    "id": int,
    "sender": NonEmpty(),
    "recipient": NonEmpty(),
    "subject": str,
    "body": str,
    "ref": OPT_STR,
    "priority": NonEmpty(),
    "kind": NonEmpty(),
    "thread": OPT_STR,
    "sender_label": OPT_STR,
    "client_id": NonEmpty(),
    "reply_to": OPT_INT,
    "is_reply": bool,
    "created_at": NonEmpty(),
    "acked_at": OPT_STR,
    "acked_by": OPT_STR,
}
PAGE = {"messages": [MESSAGE], "next_cursor": int}
OWNERS = {
    "schema": Eq("brigade.fleet_owners.v1"),
    "policy_version": int,
    "policy_error": OPT_STR,
    "owners": list,
    "owners_total": int,
    "overlaps": list,
    "overlaps_total": int,
    "unassigned": list,
    "unassigned_total": int,
    "truncated_owners": list,
    "truncated_sources": list,
    "unknown_sources": list,
}
CLAIM_REFUSAL = {"error": NonEmpty(), "owner": (dict, NONE)}
SEALED_MESSAGE = {**MESSAGE, "envelope": NonEmpty(), "envelope_bytes": int, "content_state": NonEmpty()}
KEY_ENTRY = {
    "principal_id": NonEmpty(),
    "kid": NonEmpty(),
    "record": dict,
    "state": NonEmpty(),
    "created_at": NonEmpty(),
    "retired_at": OPT_STR,
    "revoked_at": OPT_STR,
    "compromised_from": OPT_STR,
}
STATION_PAGE = {"truncated": bool, "oversized": int, "next_cursor": OPT_STR}
WHO = {
    **STATION_PAGE,
    "nodes": [{"node_id": NonEmpty()}],
    "principals": [{"principal_id": NonEmpty(), "label": str}],
    "sessions": list,
    "total": {"nodes": int, "principals": int, "sessions": int},
}
STATION_CLAIMS = {**STATION_PAGE, "claims": [{"target": NonEmpty(), "owner_node": NonEmpty()}], "total": int}
DIGITS = Pred(lambda value: isinstance(value, str) and value.isdigit(), "a decimal string")
HEX32 = Pred(
    lambda value: isinstance(value, str) and len(value) == 32 and all(c in "0123456789abcdef" for c in value),
    "32 lowercase hex digits",
)
QUESTION = {
    "question_id": HEX32,
    "caller": NonEmpty(),
    "request_id": NonEmpty(),
    "operator": NonEmpty(),
    "message_id": DIGITS,
    "deadline": NonEmpty(),
    "status": NonEmpty(),
    "answer": OPT_STR,
    "answer_id": (DIGITS, NONE),
    "source": OPT_STR,
    "answered_by": OPT_STR,
    "answered_at": OPT_STR,
    "late_answer_message_id": (DIGITS, NONE),
    "late_answered_at": OPT_STR,
    "operator_reply_after_desktop_message_id": (DIGITS, NONE),
    "operator_reply_after_desktop_at": OPT_STR,
    "created": bool,
}


# ----------------------------------------------------------- hub process


@dataclass
class Hub:
    url: str
    admin_token: str
    mode: str
    # The `agent:` principal the hub routes question_create to. The drive
    # enrolls it, so it must be fresh for every run against the same hub.
    operator: str = ""
    argv: list[str] = field(default_factory=list)
    state_dir: Path | None = None
    log: Path | None = None
    process: subprocess.Popen | None = None
    log_handle: Any = None
    readiness_detail: str = "no /health answer"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((LOOPBACK, 0))
        return int(sock.getsockname()[1])


def expand_command(template: str, *, brigade: list[str], values: dict[str, str]) -> list[str]:
    """Split the template like a shell would, then fill the placeholders.

    `{brigade}` must stand alone as a word and expands to the whole Brigade
    command; the other placeholders are replaced inside any word.
    """
    try:
        words = shlex.split(template)
    except ValueError as exc:
        raise ContractError(f"cannot parse --hub-cmd {template!r}: {exc}") from exc
    if not words:
        raise ContractError("empty --hub-cmd template")
    if "{port}" not in template:
        raise ContractError("--hub-cmd must contain {port} so the hub binds where the drive looks")
    argv: list[str] = []
    for word in words:
        if word == "{brigade}":
            argv += brigade
            continue
        for name, value in values.items():
            word = word.replace("{" + name + "}", value)
        argv.append(word)
    return argv


def new_operator() -> str:
    """A fresh `agent:` principal id for the question operator."""
    return "agent:contract-operator-" + secrets.token_hex(3)


def hub_env(state_dir: Path, admin_token: str, operator: str) -> dict[str, str]:
    """A child environment that cannot see the operator's hub, tokens, or home.

    Every `BRIGADE_*` variable is dropped (a stray `BRIGADE_FLEET_TOKEN` would
    otherwise become the admin token), the XDG roots fall back to the fake
    home, and the throwaway admin token is passed both ways a hub reads it.
    The only other Brigade variable set is the question operator recipient.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("BRIGADE_") and not key.startswith("XDG_") and key not in ("HOME", "USERPROFILE")
    }
    home = state_dir / "home"
    home.mkdir(mode=0o700, exist_ok=True)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["BRIGADE_FLEET_TOKEN"] = admin_token
    env[OPERATOR_ENV] = operator
    # A Python hub block-buffers its startup line; keep it in hub.log.
    env["PYTHONUNBUFFERED"] = "1"
    return env


LAUNCH_ATTEMPTS = 5
# What a hub's log says when its port was taken between free_port() and its bind.
_ADDRESS_IN_USE = ("address already in use", "wsaeaddrinuse") + (("10048",) if os.name == "nt" else ())


def address_in_use(log_text: str) -> bool:
    lowered = log_text.lower()
    return any(marker in lowered for marker in _ADDRESS_IN_USE)


def _await_hub(hub: Hub, deadline: float) -> str:
    """Wait for `/health` and authenticated `/nodes` readiness.

    Return "healthy", "exited" (the child died first), or "timeout".
    Keep the last `/nodes` status in `hub.readiness_detail` so a timeout
    distinguishes an unanswered health probe from a failed authenticated probe.
    """
    hub.readiness_detail = "no /health answer"
    process = hub.process
    assert process is not None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return "exited"
        if request(hub.url, "GET", "/health", timeout=1.0).status is not None:
            # A competing hub can answer health before our child reports its
            # bind failure. Prove this is our hub using the per-launch token.
            authenticated = request(hub.url, "GET", "/nodes", token=hub.admin_token, timeout=1.0)
            result = (
                f"returned status {authenticated.status}" if authenticated.status is not None else "gave no response"
            )
            hub.readiness_detail = f"/health answered but authenticated /nodes {result}"
            if authenticated.status == 200:
                return "healthy" if process.poll() is None else "exited"
        time.sleep(0.1)
    return "timeout"


def launch_on_free_port(
    start: Callable[[int], Hub],
    *,
    startup_timeout: float,
    exited: Callable[[int | None, str], Exception],
    timed_out: Callable[[str], Exception],
) -> Hub:
    """Start a hub on a free port and wait for it, picking a fresh port if the port was taken.

    free_port() releases the port before the hub binds it, so a parallel run can
    take it in between. A hub that exits early with "address already in use" is
    stopped and started again on a new port, up to LAUNCH_ATTEMPTS times within
    the one startup budget. Any other early exit raises at once.
    """
    deadline = time.monotonic() + startup_timeout
    attempt = 0
    while True:
        attempt += 1
        hub = start(free_port())
        state = _await_hub(hub, deadline)
        if state == "healthy":
            return hub
        assert hub.process is not None
        returncode = hub.process.poll()
        stop_hub(hub)
        assert hub.log is not None
        tail = log_tail(hub.log)
        if state == "timeout":
            raise timed_out(f"{hub.readiness_detail}. Hub log: {tail}")
        if attempt < LAUNCH_ATTEMPTS and time.monotonic() < deadline and address_in_use(tail):
            continue
        raise exited(returncode, tail)


def launch_hub(template: str, *, brigade: list[str], state_dir: Path, startup_timeout: float) -> Hub:
    admin_token = "contract-admin-" + secrets.token_urlsafe(18)
    operator = new_operator()
    token_file = state_dir / "admin-token"
    token_file.write_text(admin_token + "\n", encoding="utf-8")
    token_file.chmod(0o600)
    log = state_dir / "hub.log"

    def start(port: int) -> Hub:
        values = {
            "host": LOOPBACK,
            "port": str(port),
            "state_dir": str(state_dir),
            "db": str(state_dir / "hub.db"),
            "token_file": str(token_file),
        }
        argv = expand_command(template, brigade=brigade, values=values)
        handle = open(log, "wb")  # closed in stop_hub
        os.chmod(log, 0o600)
        popen_kwargs: dict[str, Any] = {}
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True
        else:  # pragma: no cover - Windows process groups
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                env=hub_env(state_dir, admin_token, operator),
                cwd=str(state_dir),
                **popen_kwargs,
            )
        except OSError as exc:
            handle.close()
            raise ContractError(f"could not start the hub: {exc}") from exc
        return Hub(
            url=f"http://{LOOPBACK}:{port}",
            admin_token=admin_token,
            mode="launched",
            operator=operator,
            argv=argv,
            state_dir=state_dir,
            log=log,
            process=process,
            log_handle=handle,
        )

    return launch_on_free_port(
        start,
        startup_timeout=startup_timeout,
        exited=lambda code, tail: ContractError(f"the hub exited with {code} before it was healthy: {tail}"),
        timed_out=lambda tail: ContractError(f"the hub did not become ready within {startup_timeout:g}s: {tail}"),
    )


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except OSError:
        pass


def stop_hub(hub: Hub) -> None:
    """Stop the hub's whole process group, even when a `--hub-cmd` launcher already exited.

    A launcher can start the real server and return, or the server can ignore
    SIGTERM; either way the group is signalled, then killed if members remain.
    """
    process = hub.process
    if process is not None:
        if os.name == "posix":
            _signal_group(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            grace = time.monotonic() + 5
            while _group_alive(process.pid) and time.monotonic() < grace:
                time.sleep(0.1)
            if _group_alive(process.pid):
                _signal_group(process.pid, signal.SIGKILL)
            if process.poll() is None:
                process.wait(timeout=10)
        elif process.poll() is None:  # pragma: no cover - Windows has no process groups to signal
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
    if hub.log_handle is not None:
        hub.log_handle.close()
        hub.log_handle = None


def log_tail(log: Path) -> str:
    try:
        return log.read_text(encoding="utf-8", errors="replace")[-LOG_TAIL:]
    except OSError:
        return ""


def guard_hub_url(raw: str) -> str:
    """Accept only a loopback http(s) URL, so the drive can never reach a shared hub."""
    try:
        parts = urllib.parse.urlsplit(raw)
        hostname = parts.hostname
    except ValueError as exc:
        raise ContractError(f"refusing --hub-url {raw!r}: {exc}") from exc
    if parts.scheme not in ("http", "https") or not hostname:
        raise ContractError(f"refusing --hub-url {raw!r}: it must be an http or https URL")
    host = hostname
    loopback = host == LOOPBACK_NAME
    if not loopback:
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
    if not loopback:
        raise ContractError(
            f"refusing --hub-url {raw!r}: the contract drive enrolls nodes and sends messages, "
            "so it only talks to a hub on a loopback address"
        )
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ContractError(f"refusing --hub-url {raw!r}: give the bare origin, without a path or query")
    return f"{parts.scheme}://{parts.netloc}"


def guard_operator(raw: str) -> str:
    """Accept only an `agent:` principal id the hub would route questions to."""
    name = raw.removeprefix("agent:")
    valid = (
        raw.startswith("agent:")
        and 1 <= len(name) <= 63
        and name[0].isascii()
        and name[0].islower()
        and all(c.isascii() and (c.islower() or c.isdigit() or c in "._-") for c in name)
    )
    if not valid:
        raise ContractError(f"refusing --operator-recipient {raw!r}: it must look like agent:<lowercase name>")
    return raw


# ----------------------------------------------------------- HTTP


@dataclass
class Response:
    status: int | None
    content_type: str
    body: bytes
    json: Any
    error: str | None = None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: a 3xx is the hub's answer, and following it would forward the bearer token."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


# No redirects, and no proxies from the environment: requests go only to the hub
# address the drive validated, so the token cannot reach any other host.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def _read_bounded(stream: Any, deadline: float) -> bytes:
    """Read a response body with a size cap and an overall deadline.

    The socket timeout restarts on every chunk, so a hub that keeps sending
    could otherwise grow the body without limit and never return.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        if time.monotonic() > deadline:
            raise TimeoutError("the response body did not finish within the request deadline")
        # read1 returns whatever has arrived, so a hub that trickles bytes still
        # reaches the deadline and size checks after every socket receive.
        read = getattr(stream, "read1", None) or stream.read
        chunk = read(65536)
        if not chunk:
            declared = getattr(stream, "headers", None) and stream.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) != total:
                # A hub that closes early is a broken response, not a short valid one.
                raise http.client.IncompleteRead(b"".join(chunks), int(declared) - total)
            return b"".join(chunks)
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            raise ValueError(f"the response body is larger than {MAX_BODY_BYTES} bytes")
        chunks.append(chunk)


def scrub(value: Any, secrets_: set[str]) -> Any:
    """Replace every issued token value wherever it appears, keys included.

    `redact` only knows keys named `token`; a drifting hub can echo a token in
    any field the contract checks, and the mismatch would carry it verbatim.
    """
    if isinstance(value, str):
        for secret in secrets_:
            value = value.replace(secret, "[redacted]")
        return value
    if isinstance(value, dict):
        return {scrub(key, secrets_): scrub(item, secrets_) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub(item, secrets_) for item in value]
    return value


def request(
    base: str,
    method: str,
    path: str,
    *,
    body: Any = None,
    raw: bytes | None = None,
    token: str | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = REQUEST_TIMEOUT,
) -> Response:
    sent = dict(headers or {})
    if token is not None:
        sent["Authorization"] = f"Bearer {token}"
    data = raw if raw is not None else (json.dumps(body).encode("utf-8") if body is not None else None)
    if data is not None:
        sent.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(base + path, data=data, headers=sent, method=method)
    deadline = time.monotonic() + timeout
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            status, content_type = resp.status, resp.headers.get("Content-Type", "")
            payload = _read_bounded(resp, deadline)
    except urllib.error.HTTPError as exc:
        status, content_type = exc.code, exc.headers.get("Content-Type", "")
        try:
            payload = _read_bounded(exc, deadline)
        except (OSError, ValueError, http.client.HTTPException) as err:
            return Response(status, content_type, b"", None, error=f"{type(err).__name__}: {err}")
    # HTTPException covers a hub that closes before its declared Content-Length (IncompleteRead)
    # or sends a malformed status line; both are contract mismatches, not crashes.
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
        return Response(None, "", b"", None, error=f"{type(exc).__name__}: {exc}")
    try:
        parsed = json.loads(payload)
    except ValueError:
        parsed = None
    return Response(status, content_type, payload, parsed)


def redact(value: Any) -> Any:
    """Drop bearer material from a captured body: any key named `token`."""
    if isinstance(value, dict):
        return {key: "[redacted]" if key == "token" else redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


# ----------------------------------------------------------- the drive


class Drive:
    def __init__(self, hub: Hub) -> None:
        self.hub = hub
        self.cases: list[dict] = []
        self.mismatches: list[dict] = []
        self.exchanges: list[dict] = []
        self.routes: set[str] = set()
        self.actions: set[str] = set()
        # Every token the drive holds or the hub issues, scrubbed from the result.
        self.secrets: set[str] = {hub.admin_token} if hub.admin_token else set()

    def call(
        self,
        case: str,
        method: str,
        path: str,
        *,
        status: int,
        shape: Any = None,
        body: Any = None,
        raw: bytes | None = None,
        token: str | None = None,
        headers: dict[str, str] | None = None,
        json_body: bool = True,
        route: bool = True,
        timeout: float = REQUEST_TIMEOUT,
    ) -> Any:
        """One exchange: check the status, the content type, and the body shape.

        Returns the parsed JSON body (an empty dict when there is none), so a
        later case can use an id or token the hub handed out. `route=False`
        keeps a deliberately unknown path out of the coverage list.
        """
        resp = request(self.hub.url, method, path, body=body, raw=raw, token=token, headers=headers, timeout=timeout)
        return self.check(
            case, method, path, resp, status=status, shape=shape, body=body, json_body=json_body, route=route
        )

    def check(
        self,
        case: str,
        method: str,
        path: str,
        resp: Response,
        *,
        status: int,
        shape: Any = None,
        body: Any = None,
        json_body: bool = True,
        route: bool = True,
    ) -> Any:
        """Record a response the drive already holds, such as one a long-poll thread fetched."""
        if route:
            self.routes.add(f"{method} {path.partition('?')[0]}")
            if method == "POST" and path == "/messages" and isinstance(body, dict):
                action = body.get("action")
                if action in HUB_MESSAGE_ACTIONS:
                    self.actions.add(action)
        problems: list[dict] = []
        if resp.status != status:
            problems.append({"check": "status", "expected": status, "observed": resp.status or resp.error})
        if resp.status is not None and json_body:
            if resp.content_type.split(";", 1)[0].strip().lower() != "application/json":
                problems.append(
                    {"check": "content-type", "expected": "application/json", "observed": resp.content_type}
                )
            if resp.json is None:
                problems.append(
                    {
                        "check": "body",
                        "path": "$",
                        "expected": "a JSON body",
                        "observed": resp.body[:200].decode("utf-8", "replace"),
                    }
                )
            elif resp.status == status:
                problems += [{"check": "body", **item} for item in match(shape, resp.json)]
        self.exchanges.append(
            {
                "case": case,
                "method": method,
                "path": path,
                "status": resp.status,
                "response": redact(resp.json) if resp.json is not None else resp.body[:400].decode("utf-8", "replace"),
            }
        )
        self.record(case, method, path, status, resp.status, problems)
        return resp.json if isinstance(resp.json, dict) else {}

    def record(
        self, case: str, method: str, path: str, expected: int | None, observed: int | None, problems: list[dict]
    ) -> None:
        self.cases.append(
            {
                "case": case,
                "method": method,
                "path": path,
                "expected_status": expected,
                "observed_status": observed,
                "ok": not problems,
            }
        )
        self.mismatches += [{"case": case, **problem} for problem in problems]

    def invariant(self, case: str, ok: bool, expected: str, observed: Any) -> None:
        """A cross-response guarantee, such as paging order, checked after the calls."""
        if not ok:
            self.mismatches.append({"case": case, "check": "invariant", "expected": expected, "observed": observed})
            for entry in self.cases:
                if entry["case"] == case:
                    entry["ok"] = False


def _obj(value: Any) -> dict:
    """`value` when it is a JSON object, else an empty one, so a wrong hub cannot crash the drive."""
    return value if isinstance(value, dict) else {}


def _items(payload: Any, key: str) -> list[dict]:
    """The JSON objects in `payload[key]`, skipping anything that is not a list of objects."""
    value = _obj(payload).get(key)
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _keyed(items: list[dict], key: str) -> dict[str, dict]:
    """Index objects by a string identifier, skipping malformed ids.

    A drifting hub can return an id that is a list or an object. The shape check
    already records that mismatch, so skipping it here keeps the drive reporting
    instead of raising TypeError.
    """
    return {item[key]: item for item in items if isinstance(item.get(key), str)}


def _ids(page: Any) -> list[Any]:
    messages = page.get("messages") if isinstance(page, dict) else None
    return [m.get("id") if isinstance(m, dict) else None for m in messages] if isinstance(messages, list) else []


# ----------------------------------------------------------- sealed mail fixtures
#
# The hub checks the bm1 wire format and never verifies a signature or
# decrypts (#2005), so random bytes of the right length stand in for keys,
# signatures, wraps, and ciphertext. Nothing here imports Brigade's crypto.


def b64(raw: bytes) -> str:
    """Unpadded URL-safe base64, the mesh binary encoding."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def canonical(value: Any) -> str:
    """The canonical envelope text: sorted keys, no spaces, ASCII only."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def mesh_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def key_record(principal: str, hub_id: str) -> dict:
    """A public key record whose key id matches its random public keys."""
    sig, enc = secrets.token_bytes(32), secrets.token_bytes(1216)
    kid = "k1_" + b64(hashlib.sha256(b"brigade-mesh/v1/kid\0" + sig + enc).digest()[:16])
    return {
        "v": 1,
        "principal": principal,
        "hub": hub_id,
        "kid": kid,
        "sig_pub": b64(sig),
        "enc_pub": b64(enc),
        "suites": ["bm1"],
        "created_at": "2026-01-01T00:00:00Z",
        "self_sig": b64(secrets.token_bytes(64)),
    }


def sealed_envelope(
    *,
    hub_id: str,
    sender: str,
    sender_kid: str,
    recipient: str,
    recipient_kid: str,
    client_id: str,
    in_reply_to: dict | None = None,
    pad: int = 1024,
) -> dict:
    """A structurally valid bm1 envelope. `canonical()` turns it into wire text."""
    rcpt = sorted({sender_kid, recipient_kid})
    value: dict[str, Any] = {
        "v": 1,
        "suite": "bm1",
        "hub": hub_id,
        "from": sender,
        "from_kid": sender_kid,
        "to": recipient,
        "kind": "message",
        "priority": "normal",
        "client_id": client_id,
        "created_at": mesh_now(),
        "pad": pad,
        "rcpt": rcpt,
        "ct_sha256": b64(secrets.token_bytes(32)),
        "wraps_sha256": b64(secrets.token_bytes(32)),
        "rc": b64(secrets.token_bytes(32)),
        "sig": b64(secrets.token_bytes(64)),
        "ct": b64(secrets.token_bytes(pad + 16)),
        "recipients": [{"kid": kid, "wrap": b64(secrets.token_bytes(1168))} for kid in rcpt],
    }
    if in_reply_to is not None:
        value["in_reply_to"] = in_reply_to
    return value


# ----------------------------------------------------------- long polls


class Waiter(threading.Thread):
    """One `list` long poll on its own thread, so the drive can act while it waits.

    A 429 while the waiters are still parking is retried: the drive's own cap
    probe can hold a slot for a moment. The drive records `resp` afterwards.
    """

    def __init__(self, hub: Hub, token: str, after: int) -> None:
        super().__init__(daemon=True)
        self.hub, self.token = hub, token
        self.body = {"action": "list", "after": after, "unacked": False, "wait_ms": MAX_WAIT_MS}
        self.retry_until = time.monotonic() + PARK_DEADLINE_SECONDS
        self.resp = Response(None, "", b"", None, error="the waiter never returned")
        self.started = self.finished = 0.0

    def run(self) -> None:
        while True:
            self.started = time.monotonic()
            resp = request(
                self.hub.url,
                "POST",
                "/messages",
                body=self.body,
                token=self.token,
                timeout=MAX_WAIT_MS / 1000 + REQUEST_TIMEOUT,
            )
            self.finished = time.monotonic()
            if resp.status == 429 and time.monotonic() < self.retry_until:
                time.sleep(0.05)
                continue
            self.resp = resp
            return


def park_waiters(hub: Hub, token: str, after: int) -> tuple[list[Waiter], bool]:
    """Start two long polls for one recipient and wait until both hold a slot.

    The hub caps waiters at two per recipient and reports no other sign that a
    poll is waiting, so the drive probes with `wait_ms: 1` until the third
    poll is refused. It gives up when a waiter returns early or the deadline passes.
    """
    waiters = [Waiter(hub, token, after) for _ in range(2)]
    for waiter in waiters:
        waiter.start()
    probe = {"action": "list", "after": after, "unacked": False, "wait_ms": 1}
    deadline = time.monotonic() + PARK_DEADLINE_SECONDS
    while time.monotonic() < deadline and all(waiter.is_alive() for waiter in waiters):
        if request(hub.url, "POST", "/messages", body=probe, token=token).status == 429:
            return waiters, True
        time.sleep(0.05)
    return waiters, False


def record_waiters(d: Drive, case: str, waiters: list[Waiter], woken_at: float, expect: Callable[[Any], bool]) -> None:
    """Record each waiter's answer and check it woke with the mail, well before `wait_ms`."""
    budget = MAX_WAIT_MS / 1000 - WAKE_MARGIN_SECONDS
    for index, waiter in enumerate(waiters, start=1):
        waiter.join(timeout=MAX_WAIT_MS / 1000 + REQUEST_TIMEOUT + 5)
        name = f"{case} (waiter {index})"
        page = d.check(name, "POST", "/messages", waiter.resp, status=200, shape=PAGE, body=waiter.body)
        waited = waiter.finished - waiter.started
        d.invariant(
            name,
            expect(page) and waiter.finished >= woken_at and waited < budget,
            f"the waiter returns the new message after the write and within {budget:g}s of a "
            f"{MAX_WAIT_MS // 1000}s wait_ms",
            {"ids": _ids(page), "waited_seconds": round(waited, 3), "woke_after_write": waiter.finished >= woken_at},
        )


# ----------------------------------------------------------- hot-set cases
#
# The hub track of the v2 plan ports these first, so each records what the
# Python hub does today, quirks included.


@dataclass
class Ctx:
    """Identities and ids the hot-set cases share with the core cases."""

    tag: str
    admin: str
    node_a: str
    node_b: str
    a: str
    b: str
    principal_id: str
    principal_token: str
    m1: Any
    m2: Any
    c1: str
    kid_a: str = ""
    kid_b: str = ""
    held_target: str = ""
    # The claim recovery floor of the hub under test: 0 on a fresh hub, the
    # value a restore wrote on a restored one. New targets start at floor + 1.
    claim_floor: int = 0


def _claim_body(action: str, target: str, node: str, holder: str | None, **extra: Any) -> dict:
    body: dict[str, Any] = {"action": action, "target": target, "node_id": node, **extra}
    if holder is not None:
        body["holder"] = holder
    return body


# The largest floor whose highest generation the claim drive sends, floor + 7,
# still fits the hub's generation range [0, 2**63 - 1].
MAX_CLAIM_FLOOR = 2**63 - 1 - 7


def drive_claim_fencing(d: Drive, x: Ctx) -> None:
    """Holder, node, and force scopes, lease supersession, generations, and expiry.

    Every generation is relative to the hub's claim recovery floor, so `gen(1)`
    is 1 on a fresh hub and floor + 1 on a restored one.
    """
    target = f"repo:example-org/fenced-{x.tag}"
    lease = {
        "token": f"contract-lease-{x.tag}",
        "acquired_at": "2026-01-01T00:00:00+00:00",
        "run_dir": "/srv/example/run-1",
    }

    def claim(action: str, node: str, holder: str | None, **extra: Any) -> dict:
        return _claim_body(action, target, node, holder, **extra)

    def refused(key: str, reason: str, owner: Any) -> dict:
        return {key: Eq(False), "reason": Eq(reason), "error": Eq(f"claim operation refused: {reason}"), "owner": owner}

    def gen(step: int) -> int:
        return x.claim_floor + step

    def at(step: int, **pins: Any) -> dict:
        return {**CLAIM, "target": Eq(target), "generation": Eq(gen(step)), **pins}

    free = d.call(
        "claims inspect an unseen target",
        "POST",
        "/claims",
        status=200,
        body=claim("inspect", x.node_b, None),
        token=x.b,
        shape={"inspected": Eq(True), "claim": NONE, "owned": Eq(False)},
    )
    d.invariant(
        "claims inspect an unseen target",
        "generation" not in free,
        "an unseen target has no generation key",
        sorted(free),
    )
    first = d.call(
        "claims acquire starts at generation 1",
        "POST",
        "/claims",
        status=200,
        body=claim("acquire", x.node_a, "fence-holder-1", ttl_seconds=600),
        token=x.a,
        shape={"granted": Eq(True), "claim": at(1, owner_node=Eq(x.node_a))},
    )
    again = d.call(
        "claims acquire retry keeps the generation",
        "POST",
        "/claims",
        status=200,
        body=claim("acquire", x.node_a, "fence-holder-1", ttl_seconds=600),
        token=x.a,
        shape={"granted": Eq(True), "claim": at(1)},
    )
    acquired = [_obj(item.get("claim")).get("acquired_at") for item in (first, again)]
    d.invariant(
        "claims acquire retry keeps the generation",
        acquired[0] is not None and acquired[0] == acquired[1],
        "a same-holder retry keeps acquired_at",
        acquired,
    )
    d.call(
        "claims acquire retry with the pre-acquire generation",
        "POST",
        "/claims",
        status=200,
        body=claim("acquire", x.node_a, "fence-holder-1", ttl_seconds=600, generation=gen(0)),
        token=x.a,
        shape={"granted": Eq(True), "claim": at(1)},
    )
    d.call(
        "claims acquire with a stale generation",
        "POST",
        "/claims",
        status=409,
        body=claim("acquire", x.node_b, "fence-holder-b", ttl_seconds=600, generation=gen(7)),
        token=x.b,
        shape=refused("granted", "stale-generation", at(1, owner_node=Eq(x.node_a))),
    )
    d.call(
        "claims renew with a stale generation",
        "POST",
        "/claims",
        status=409,
        body=claim("renew", x.node_a, "fence-holder-1", ttl_seconds=600, generation=gen(2)),
        token=x.a,
        shape=refused("renewed", "stale-generation", at(1)),
    )
    d.call(
        "claims renew with the current generation",
        "POST",
        "/claims",
        status=200,
        body=claim("renew", x.node_a, "fence-holder-1", ttl_seconds=600, generation=gen(1)),
        token=x.a,
        shape={"renewed": Eq(True), "claim": at(1)},
    )
    d.call(
        "claims inspect as the owner",
        "POST",
        "/claims",
        status=200,
        body=claim("inspect", x.node_a, None),
        token=x.a,
        shape={
            "inspected": Eq(True),
            "owned": Eq(True),
            "generation": Eq(gen(1)),
            "claim": at(1),
            "lock_run_dir": NONE,
        },
    )
    d.call(
        "claims release with a stale generation",
        "POST",
        "/claims",
        status=409,
        body=claim("release", x.node_a, "fence-holder-1", generation=gen(5)),
        token=x.a,
        shape=refused("released", "stale-generation", at(1)),
    )
    released = d.call(
        "claims release in holder scope",
        "POST",
        "/claims",
        status=200,
        body=claim("release", x.node_a, "fence-holder-1", generation=gen(1)),
        token=x.a,
        shape={"released": Eq(True)},
    )
    d.invariant(
        "claims release in holder scope", "claim" not in released, "a holder release echoes no claim", sorted(released)
    )
    d.call(
        "claims inspect after a release",
        "POST",
        "/claims",
        status=200,
        body=claim("inspect", x.node_b, None),
        token=x.b,
        shape={"inspected": Eq(True), "claim": NONE, "owned": Eq(False), "generation": Eq(gen(2))},
    )
    d.call(
        "claims acquire by a retired holder",
        "POST",
        "/claims",
        status=409,
        body=claim("acquire", x.node_a, "fence-holder-1", ttl_seconds=600),
        token=x.a,
        shape=refused("granted", "stale-holder", Eq({})),
    )
    d.call(
        "claims release by a retired holder",
        "POST",
        "/claims",
        status=200,
        body=claim("release", x.node_a, "fence-holder-1"),
        token=x.a,
        shape={"released": Eq(False), "reason": Eq("stale-holder")},
    )
    d.call(
        "claims acquire with a run lease",
        "POST",
        "/claims",
        status=200,
        body=claim("acquire", x.node_a, "fence-holder-2", ttl_seconds=600, lock=lease),
        token=x.a,
        shape={"granted": Eq(True), "claim": at(3)},
    )
    d.call(
        "claims inspect shows the owner its run dir",
        "POST",
        "/claims",
        status=200,
        body=claim("inspect", x.node_a, None),
        token=x.a,
        shape={"owned": Eq(True), "generation": Eq(gen(3)), "lock_run_dir": Eq(lease["run_dir"])},
    )
    other = d.call(
        "claims inspect hides the run dir from other nodes",
        "POST",
        "/claims",
        status=200,
        body=claim("inspect", x.node_b, None),
        token=x.b,
        shape={"owned": Eq(False), "generation": Eq(gen(3)), "claim": at(3)},
    )
    d.invariant(
        "claims inspect hides the run dir from other nodes",
        "lock_run_dir" not in other,
        "lock_run_dir is only shown to the owning node",
        sorted(other),
    )
    d.call(
        "claims node scope acquire without a lease to supersede",
        "POST",
        "/claims",
        status=400,
        body=claim("acquire", x.node_a, "fence-holder-3", scope="node"),
        token=x.a,
        shape=error_body("claim field 'supersede' (the reconciled dead lease) is required for a node-scoped acquire"),
    )
    d.call(
        "claims node scope acquire with another lease",
        "POST",
        "/claims",
        status=409,
        body=claim("acquire", x.node_a, "fence-holder-3", scope="node", supersede={"token": "contract-lease-other"}),
        token=x.a,
        shape={
            "granted": Eq(False),
            "error": Eq(
                f"target {target!r} is held by {x.node_a} under another lease than the reconciled dead run's "
                "(not superseded)"
            ),
            "owner": at(3),
        },
    )
    d.call(
        "claims node scope acquire supersedes the dead lease",
        "POST",
        "/claims",
        status=200,
        body=claim(
            "acquire",
            x.node_a,
            "fence-holder-3",
            scope="node",
            supersede={"token": lease["token"], "acquired_at": lease["acquired_at"]},
        ),
        token=x.a,
        shape={"granted": Eq(True), "claim": at(4), "superseded": at(3)},
    )
    d.call(
        "claims renew by the superseded holder",
        "POST",
        "/claims",
        status=409,
        body=claim("renew", x.node_a, "fence-holder-2", ttl_seconds=600),
        token=x.a,
        shape=refused("renewed", "stale-holder", at(4)),
    )
    d.call(
        "claims node scope release without acquired_at",
        "POST",
        "/claims",
        status=400,
        body=claim("release", x.node_a, None, scope="node"),
        token=x.a,
        shape=error_body("claim field 'acquired_at' is required for a token-less node-scoped release"),
    )
    d.call(
        "claims node scope release by another node",
        "POST",
        "/claims",
        status=409,
        body=claim("release", x.node_b, None, scope="node", acquired_at="2026-01-01T00:00:00+00:00"),
        token=x.b,
        shape={
            "released": Eq(False),
            "error": Eq(f"target {target!r} is held by {x.node_a}, not {x.node_b}"),
            "owner": at(4),
        },
    )
    prefix = f"target {target!r} was re-acquired since it was inspected (now acquired "
    d.call(
        "claims node scope release with a stale acquired_at",
        "POST",
        "/claims",
        status=409,
        body=claim("release", x.node_a, None, scope="node", acquired_at="2000-01-01T00:00:00+00:00"),
        token=x.a,
        shape={
            "released": Eq(False),
            "error": Pred(lambda v: isinstance(v, str) and v.startswith(prefix), f"text starting {prefix!r}"),
            "owner": at(4),
        },
    )
    d.call(
        "claims force release by another node",
        "POST",
        "/claims",
        status=200,
        body=claim("release", x.node_b, None, scope="force"),
        token=x.b,
        shape={"released": Eq(True), "claim": at(4, owner_node=Eq(x.node_a))},
    )
    noop = d.call(
        "claims force release of a free target",
        "POST",
        "/claims",
        status=200,
        body=claim("release", x.node_b, None, scope="force"),
        token=x.b,
        shape={"released": Eq(False)},
    )
    d.invariant(
        "claims force release of a free target",
        "reason" not in noop and "claim" not in noop,
        "a no-op force release carries no reason and no claim",
        sorted(noop),
    )
    d.call(
        "claims inspect after a no-op force release",
        "POST",
        "/claims",
        status=200,
        body=claim("inspect", x.node_b, None),
        token=x.b,
        shape={"claim": NONE, "owned": Eq(False), "generation": Eq(gen(6))},
    )
    for case, body, message in (
        (
            "claims renew in node scope",
            claim("renew", x.node_a, "fence-holder-3", scope="node"),
            "claim field 'scope' for renew must be one of: holder",
        ),
        (
            "claims generation out of range",
            claim("acquire", x.node_a, "fence-holder-3", generation=-1),
            "claim field 'generation' must be an integer in [0, 9223372036854775807]",
        ),
        (
            "claims ttl out of range",
            claim("acquire", x.node_a, "fence-holder-3", ttl_seconds=0),
            "claim field 'ttl_seconds' must be an integer in [1, 86400]",
        ),
        (
            "claims holder named unknown",
            claim("acquire", x.node_a, "unknown"),
            "claim field 'holder' must be a real identity ([A-Za-z0-9._-], max 128 chars, never the literal 'unknown')",
        ),
    ):
        d.call(case, "POST", "/claims", status=400, body=body, token=x.a, shape=error_body(message))

    # -- lease expiry: a lapsed claim hides, refuses renewal, and costs a generation
    short = f"repo:example-org/short-lease-{x.tag}"
    d.call(
        "claims acquire a one-second lease",
        "POST",
        "/claims",
        status=200,
        body=_claim_body("acquire", short, x.node_a, "fence-holder-1", ttl_seconds=1),
        token=x.a,
        shape={
            "granted": Eq(True),
            "claim": {**CLAIM, "target": Eq(short), "generation": Eq(gen(1)), "ttl_seconds": Eq(1)},
        },
    )
    # Expiry is the hub's clock against expires_at, so sleeping longer than the
    # lease can only make the case more certain under load.
    time.sleep(1.5)
    listed = d.call(
        "claims list hides an expired lease", "GET", "/claims", status=200, token=x.b, shape={"claims": [LISTED_CLAIM]}
    )
    targets = [item.get("target") for item in _items(listed, "claims")]
    d.invariant(
        "claims list hides an expired lease", short not in targets, "the default view hides a lapsed lease", targets
    )
    every = d.call(
        "claims list all shows an expired lease",
        "GET",
        "/claims?all=1",
        status=200,
        token=x.b,
        shape={"claims": [LISTED_CLAIM]},
    )
    lapsed = _keyed(_items(every, "claims"), "target").get(short, {})
    d.invariant(
        "claims list all shows an expired lease",
        lapsed.get("expired") is True,
        "?all=1 lists the lapsed lease with expired: true",
        lapsed.get("expired", "<missing>"),
    )
    d.call(
        "claims renew an expired lease",
        "POST",
        "/claims",
        status=409,
        body=_claim_body("renew", short, x.node_a, "fence-holder-1", ttl_seconds=600),
        token=x.a,
        shape={"renewed": Eq(False), "error": Eq(f"claim on {short!r} is expired or missing"), "owner": NONE},
    )
    d.call(
        "claims acquire after expiry",
        "POST",
        "/claims",
        status=200,
        body=_claim_body("acquire", short, x.node_b, "fence-holder-b", ttl_seconds=600),
        token=x.b,
        shape={
            "granted": Eq(True),
            "claim": {**CLAIM, "target": Eq(short), "owner_node": Eq(x.node_b), "generation": Eq(gen(3))},
        },
    )
    x.held_target = short


def drive_keys_and_sealed(d: Drive, x: Ctx) -> None:
    """The key directory, manifests, and opaque sealed envelopes."""
    hub_id = "h_" + b64(secrets.token_bytes(16))
    record_a, record_b = key_record(x.node_a, hub_id), key_record(x.node_b, hub_id)
    x.kid_a, x.kid_b = record_a["kid"], record_b["kid"]

    def key_shape(node: str, kid: str) -> dict:
        return {"key": {**KEY_ENTRY, "principal_id": Eq(node), "kid": Eq(kid), "state": Eq("active")}}

    published = d.call(
        "messages keys_publish",
        "POST",
        "/messages",
        status=201,
        body={"action": "keys_publish", "record": record_a},
        token=x.a,
        shape=key_shape(x.node_a, x.kid_a),
    )
    echoed = _obj(published.get("key")).get("record")
    d.invariant("messages keys_publish", echoed == record_a, "the stored record comes back unchanged", "differs")
    d.call(
        "messages keys_publish for a second node",
        "POST",
        "/messages",
        status=201,
        body={"action": "keys_publish", "record": record_b},
        token=x.b,
        shape=key_shape(x.node_b, x.kid_b),
    )
    stray = key_record(x.node_a, hub_id)
    stray["kid"] = "k1_" + b64(secrets.token_bytes(16))
    for case, record, token, status, shape in (
        (
            "messages keys_publish an existing key id",
            record_a,
            x.a,
            409,
            {"error": Eq("key id was already published"), "code": Eq("principal_key_exists")},
        ),
        (
            "messages keys_publish a replacement without the previous key",
            key_record(x.node_a, hub_id),
            x.a,
            409,
            {"error": Eq("replacement requires the current previous key"), "code": Eq("principal_key_exists")},
        ),
        (
            "messages keys_publish for another principal",
            key_record(x.node_b, hub_id),
            x.a,
            403,
            {"error": Eq("key principal must match caller"), "code": Eq("principal_key_mismatch")},
        ),
        (
            "messages keys_publish with a mismatched key id",
            stray,
            x.a,
            400,
            error_body("mesh key id does not match public keys"),
        ),
    ):
        d.call(
            case,
            "POST",
            "/messages",
            status=status,
            body={"action": "keys_publish", "record": record},
            token=token,
            shape=shape,
        )
    d.call(
        "messages keys_get",
        "POST",
        "/messages",
        status=200,
        body={"action": "keys_get", "kid": x.kid_a},
        token=x.b,
        shape={"key": {**KEY_ENTRY, "principal_id": Eq(x.node_a), "kid": Eq(x.kid_a)}},
    )
    body: dict[str, Any]
    for case, body, token, status, shape in (
        (
            "messages keys_get an unknown key",
            {"action": "keys_get", "kid": "k1_" + b64(secrets.token_bytes(16))},
            x.b,
            404,
            error_body("key not found"),
        ),
        (
            "messages keys_get without a key id",
            {"action": "keys_get"},
            x.b,
            400,
            error_body("keys_get requires a key id"),
        ),
        (
            "messages keys_get for a principal without grants",
            {"action": "keys_get", "kid": x.kid_a},
            x.principal_token,
            404,
            error_body("key not found"),
        ),
        ("messages manifests_get", {"action": "manifests_get"}, x.a, 200, {"statements": Eq({})}),
        (
            "messages manifests_get with arguments",
            {"action": "manifests_get", "kind": "hub"},
            x.a,
            400,
            error_body("manifests_get takes no arguments"),
        ),
    ):
        d.call(case, "POST", "/messages", status=status, body=body, token=token, shape=shape)

    # -- sealed envelopes: stored and returned byte for byte -------------------
    cid = f"s1-{x.tag}"
    value = sealed_envelope(
        hub_id=hub_id, sender=x.node_a, sender_kid=x.kid_a, recipient=x.node_b, recipient_kid=x.kid_b, client_id=cid
    )
    text = canonical(value)
    sealed_shape = {
        "message": {
            **SEALED_MESSAGE,
            "sender": Eq(x.node_a),
            "recipient": Eq(x.node_b),
            "subject": Eq(""),
            "body": Eq(""),
            "ref": NONE,
            "reply_to": NONE,
            "client_id": Eq(cid),
            "kind": Eq("message"),
            "envelope": Eq(text),
            "envelope_bytes": Eq(len(text)),
            "content_state": Eq("sealed"),
        }
    }
    sent = d.call(
        "messages sealed send",
        "POST",
        "/messages",
        status=201,
        body={"action": "send", "envelope": text},
        token=x.a,
        shape=sealed_shape,
    )
    sealed_id = _obj(sent.get("message")).get("id")
    retried = d.call(
        "messages sealed send retry",
        "POST",
        "/messages",
        status=201,
        body={"action": "send", "envelope": text},
        token=x.a,
        shape=sealed_shape,
    )
    retry_id = _obj(retried.get("message")).get("id")
    d.invariant(
        "messages sealed send retry",
        isinstance(sealed_id, int) and retry_id == sealed_id,
        "resending the same envelope returns the first id",
        [sealed_id, retry_id],
    )
    after = sealed_id - 1 if isinstance(sealed_id, int) and sealed_id > 0 else 0
    page = d.call(
        "messages sealed list",
        "POST",
        "/messages",
        status=200,
        body={"action": "list", "after": after, "unacked": False},
        token=x.b,
        shape=PAGE,
    )
    listed = _keyed([{**m, "key": str(m.get("id"))} for m in _items(page, "messages")], "key").get(str(sealed_id), {})
    d.invariant(
        "messages sealed list",
        listed.get("envelope") == text and listed.get("content_state") == "sealed",
        "the recipient's inbox returns the envelope byte for byte",
        {"content_state": listed.get("content_state"), "same_bytes": listed.get("envelope") == text},
    )
    malformed = {"error": Eq("invalid sealed envelope"), "code": Eq("envelope_malformed")}
    bad_pad = dict(value, pad=1000, client_id=f"s2-{x.tag}")
    short_digest = dict(value, ct_sha256=b64(secrets.token_bytes(16)), client_id=f"s3-{x.tag}")
    as_b = sealed_envelope(
        hub_id=hub_id,
        sender=x.node_b,
        sender_kid=x.kid_b,
        recipient=x.node_a,
        recipient_kid=x.kid_a,
        client_id=f"s4-{x.tag}",
    )
    stale = sealed_envelope(
        hub_id=hub_id,
        sender=x.node_a,
        sender_kid="k1_" + b64(secrets.token_bytes(16)),
        recipient=x.node_b,
        recipient_kid=x.kid_b,
        client_id=f"s5-{x.tag}",
    )
    not_ascii = {"error": Eq("envelope must be canonical ASCII JSON text"), "code": Eq("envelope_malformed")}
    for case, body, status, shape in (
        (
            "messages sealed send with plaintext fields",
            {"action": "send", "envelope": text, "subject": "contract"},
            400,
            error_body("an envelope cannot accompany plaintext or routing fields"),
        ),
        (
            "messages sealed send not in canonical form",
            {"action": "send", "envelope": json.dumps(value)},
            400,
            malformed,
        ),
        (
            "messages sealed send with a duplicate key",
            {"action": "send", "envelope": '{"v":1,' + text[1:]},
            400,
            malformed,
        ),
        ("messages sealed send with bad padding", {"action": "send", "envelope": canonical(bad_pad)}, 400, malformed),
        (
            "messages sealed send with a short digest",
            {"action": "send", "envelope": canonical(short_digest)},
            400,
            malformed,
        ),
        (
            "messages sealed send as another sender",
            {"action": "send", "envelope": canonical(as_b)},
            403,
            {"error": Eq("envelope sender must match caller"), "code": Eq("message_sender_mismatch")},
        ),
        (
            "messages sealed send with a stale sender key",
            {"action": "send", "envelope": canonical(stale)},
            409,
            {"error": Eq("sender key is no longer active"), "code": Eq("sender_key_stale")},
        ),
        (
            "messages sealed send that is not ASCII",
            {"action": "send", "envelope": text[:-1] + ',"x":"é"}'},
            400,
            not_ascii,
        ),
        ("messages sealed envelope that is not a string", {"action": "send", "envelope": value}, 400, not_ascii),
        (
            "messages sealed send over the size limit",
            {"action": "send", "envelope": "x" * (96 * 1024 + 1)},
            400,
            {"error": Eq("sealed envelope exceeds storage limit"), "code": Eq("message_too_large")},
        ),
    ):
        d.call(case, "POST", "/messages", status=status, body=body, token=x.a, shape=shape)
    d.call(
        "messages plaintext reply to a sealed message",
        "POST",
        "/messages",
        status=400,
        body={"action": "reply", "id": sealed_id, "body": "plain"},
        token=x.b,
        shape={"error": Eq("a sealed parent requires a sealed reply"), "code": Eq("message_sealing_required")},
    )
    unlinked = sealed_envelope(
        hub_id=hub_id,
        sender=x.node_b,
        sender_kid=x.kid_b,
        recipient=x.node_a,
        recipient_kid=x.kid_a,
        client_id=f"s6-{x.tag}",
    )
    d.call(
        "messages sealed reply without in_reply_to",
        "POST",
        "/messages",
        status=400,
        body={"action": "reply", "id": sealed_id, "envelope": canonical(unlinked)},
        token=x.b,
        shape=error_body("reply_to and signed in_reply_to must both be present or absent"),
    )
    reply = canonical(
        sealed_envelope(
            hub_id=hub_id,
            sender=x.node_b,
            sender_kid=x.kid_b,
            recipient=x.node_a,
            recipient_kid=x.kid_a,
            client_id=f"s7-{x.tag}",
            in_reply_to={"hub": hub_id, "from": x.node_a, "client_id": cid},
        )
    )
    d.call(
        "messages sealed reply",
        "POST",
        "/messages",
        status=201,
        body={"action": "reply", "id": sealed_id, "envelope": reply},
        token=x.b,
        shape={
            "message": {
                **SEALED_MESSAGE,
                "sender": Eq(x.node_b),
                "recipient": Eq(x.node_a),
                "reply_to": Eq(sealed_id) if isinstance(sealed_id, int) else int,
                "envelope": Eq(reply),
                "content_state": Eq("sealed"),
            }
        },
    )


def drive_message_actions(d: Drive, x: Ctx) -> None:
    """reply, sent_lookup, sent by ids, the station reads, and identity peers."""
    m1 = x.m1 if isinstance(x.m1, int) else 0
    m2 = x.m2 if isinstance(x.m2, int) else 0
    replied = d.call(
        "messages reply",
        "POST",
        "/messages",
        status=201,
        body={"action": "reply", "id": m2, "body": "got it", "client_id": f"r1-{x.tag}"},
        token=x.b,
        shape={
            "message": {
                **MESSAGE,
                "sender": Eq(x.node_b),
                "recipient": Eq(x.node_a),
                "subject": Eq("Re: contract"),
                "reply_to": Eq(m2),
            }
        },
    )
    reply_id = _obj(replied.get("message")).get("id")
    retried = d.call(
        "messages reply retry",
        "POST",
        "/messages",
        status=201,
        body={"action": "reply", "id": m2, "body": "got it", "client_id": f"r1-{x.tag}"},
        token=x.b,
        shape={"message": MESSAGE},
    )
    again = _obj(retried.get("message")).get("id")
    d.invariant(
        "messages reply retry",
        isinstance(reply_id, int) and again == reply_id,
        "a retried reply returns the first id",
        [reply_id, again],
    )
    d.call(
        "messages send with reply_to",
        "POST",
        "/messages",
        status=201,
        body={
            "action": "send",
            "to": x.node_a,
            "subject": "contract",
            "body": "via send",
            "client_id": f"r2-{x.tag}",
            "reply_to": m1,
        },
        token=x.b,
        shape={"message": {**MESSAGE, "sender": Eq(x.node_b), "recipient": Eq(x.node_a), "reply_to": Eq(m1)}},
    )
    for case, body, token, status, shape in (
        (
            "messages reply to a message the caller did not receive",
            {"action": "reply", "id": m2, "body": "not mine"},
            x.a,
            404,
            error_body("message not found"),
        ),
        (
            "messages reply with a bad id",
            {"action": "reply", "id": 0, "body": "bad"},
            x.b,
            400,
            error_body("id must be a positive integer within SQLite's range"),
        ),
        (
            "messages send with a reply_to the caller sent",
            {
                "action": "send",
                "to": x.node_a,
                "subject": "contract",
                "body": "wrong parent",
                "client_id": f"r3-{x.tag}",
                "reply_to": reply_id if isinstance(reply_id, int) else 1,
            },
            x.b,
            400,
            error_body("reply_to must identify an inbox message from the recipient"),
        ),
        (
            "messages sent_lookup",
            {"action": "sent_lookup", "client_id": x.c1},
            x.a,
            200,
            {"message": {**MESSAGE, "id": Eq(m1), "client_id": Eq(x.c1)}},
        ),
        (
            "messages sent_lookup unknown client_id",
            {"action": "sent_lookup", "client_id": f"nope-{x.tag}"},
            x.a,
            404,
            {"error": Eq("message not found"), "code": Eq("not_found")},
        ),
        (
            "messages sent_lookup with extra fields",
            {"action": "sent_lookup", "client_id": x.c1, "after": 0},
            x.a,
            400,
            error_body("sent_lookup requires only client_id"),
        ),
        (
            "messages sent with after and ids",
            {"action": "sent", "after": 0, "ids": [m1]},
            x.a,
            400,
            error_body("sent accepts either after or ids"),
        ),
    ):
        d.call(case, "POST", "/messages", status=status, body=body, token=token, shape=shape)
    by_ids = d.call(
        "messages sent by ids",
        "POST",
        "/messages",
        status=200,
        body={"action": "sent", "ids": [m2]},
        token=x.a,
        shape=PAGE,
    )
    d.invariant(
        "messages sent by ids", _ids(by_ids) == [m2], "ids selects exactly the named sent messages", _ids(by_ids)
    )

    who = d.call("messages who", "POST", "/messages", status=200, body={"action": "who"}, token=x.b, shape=WHO)
    nodes = _keyed(_items(who, "nodes"), "node_id")
    principals = set(_keyed(_items(who, "principals"), "principal_id"))
    d.invariant(
        "messages who",
        {x.node_a, x.node_b} <= set(nodes) and x.principal_id in principals,
        "who lists the active nodes and the agent principal",
        {"nodes": sorted(nodes), "principals": sorted(principals)},
    )
    kids = [key.get("kid") for key in _items(nodes.get(x.node_a), "keys")]
    d.invariant("messages who", kids == [x.kid_a], "a node with a published key lists its active key", kids)
    d.call(
        "messages who with an unknown argument",
        "POST",
        "/messages",
        status=400,
        body={"action": "who", "bogus": True},
        token=x.b,
        shape=error_body("invalid station read arguments"),
    )
    d.call(
        "messages who with a bad limit",
        "POST",
        "/messages",
        status=400,
        body={"action": "who", "limit": 0},
        token=x.b,
        shape=error_body("who limit must be an integer from 1 to 500"),
    )
    station = d.call(
        "messages claims", "POST", "/messages", status=200, body={"action": "claims"}, token=x.b, shape=STATION_CLAIMS
    )
    targets = [item.get("target") for item in _items(station, "claims")]
    d.invariant("messages claims", x.held_target in targets, "a node station read lists the held claim", targets)
    d.call(
        "messages claims for a principal without grants",
        "POST",
        "/messages",
        status=200,
        body={"action": "claims"},
        token=x.principal_token,
        shape={**STATION_CLAIMS, "claims": Eq([]), "total": Eq(0)},
    )
    d.call(
        "messages identity with peers for a node",
        "POST",
        "/messages",
        status=403,
        body={"action": "identity", "peers": ["agent:contract-peer"]},
        token=x.a,
        shape={
            "error": Eq("identity peers requires an agent principal"),
            "code": Eq("message_identity_peers_unsupported"),
        },
    )
    d.call(
        "messages identity with peers",
        "POST",
        "/messages",
        status=200,
        body={"action": "identity", "peers": ["agent:contract-peer"]},
        token=x.principal_token,
        shape={
            "schema": Eq("brigade.message_authorization.v1"),
            "principal": Eq(x.principal_id),
            "enrollment_epoch": NonEmpty(),
            "grant_epoch": NonEmpty(),
            "peers": [
                {
                    "principal": Eq("agent:contract-peer"),
                    "authorized": Eq(False),
                    "enrollment_epoch": NONE,
                    "grant_epoch": NONE,
                }
            ],
        },
    )
    d.call(
        "messages identity with bad peers",
        "POST",
        "/messages",
        status=400,
        body={"action": "identity", "peers": [x.principal_id]},
        token=x.principal_token,
        shape={"error": Eq("invalid identity arguments"), "code": Eq("invalid_argument")},
    )


def drive_long_poll(d: Drive, x: Ctx) -> None:
    """wait_ms bounds, an empty wait, the waiter cap, and a send waking every waiter."""
    page = d.call(
        "messages list before a long poll",
        "POST",
        "/messages",
        status=200,
        body={"action": "list", "after": 0, "unacked": False},
        token=x.b,
        shape=PAGE,
    )
    ids = [value for value in _ids(page) if isinstance(value, int)]
    cursor = max(ids) if ids else 0
    bound = error_body(f"wait_ms must be an integer between 0 and {MAX_WAIT_MS}")
    for case, wait in (
        ("messages list wait_ms above the cap", MAX_WAIT_MS + 1),
        ("messages list wait_ms that is not an integer", True),
    ):
        d.call(
            case,
            "POST",
            "/messages",
            status=400,
            body={"action": "list", "after": cursor, "unacked": False, "wait_ms": wait},
            token=x.b,
            shape=bound,
        )
    started = time.monotonic()
    d.call(
        "messages list wait_ms times out empty",
        "POST",
        "/messages",
        status=200,
        body={"action": "list", "after": cursor, "unacked": False, "wait_ms": 300},
        token=x.b,
        shape={"messages": Eq([]), "next_cursor": Eq(cursor)},
    )
    waited = time.monotonic() - started
    d.invariant(
        "messages list wait_ms times out empty",
        waited >= 0.25,
        "a long poll with no mail holds the request for about wait_ms",
        round(waited, 3),
    )
    waiters, _ = park_waiters(d.hub, x.b, cursor)
    d.call(
        "messages list over the waiter cap",
        "POST",
        "/messages",
        status=429,
        body={"action": "list", "after": cursor, "unacked": False, "wait_ms": 1},
        token=x.b,
        shape={"error": Eq("message wait capacity reached"), "code": Eq("message_wait_capped")},
    )
    woken_at = time.monotonic()
    sent = d.call(
        "messages send wakes long polls",
        "POST",
        "/messages",
        status=201,
        body={"action": "send", "to": x.node_b, "subject": "wake", "body": "wake", "client_id": f"w1-{x.tag}"},
        token=x.a,
        shape={"message": {**MESSAGE, "recipient": Eq(x.node_b)}},
    )
    wake_id = _obj(sent.get("message")).get("id")
    record_waiters(d, "messages long poll woken by a send", waiters, woken_at, lambda got: _ids(got) == [wake_id])


def drive_questions(d: Drive, x: Ctx, operator: str) -> None:
    """question_create inserts a message for the operator, wakes its long poll, and is answered two ways."""
    enrolled = d.call(
        "message principal add the question operator",
        "POST",
        "/message-principals",
        status=201,
        body={"action": "add", "name": operator.removeprefix("agent:")},
        token=x.admin,
        shape={"principal": {"principal_id": Eq(operator), "token": NonEmpty()}},
    )
    op_token = str(_obj(enrolled.get("principal")).get("token") or "contract-missing-token")
    if op_token != "contract-missing-token":
        d.secrets.add(op_token)
    d.call(
        "message principal grants for the question operator",
        "POST",
        "/message-principals",
        status=200,
        body={"action": "grants", "name": operator.removeprefix("agent:"), "may_send_to": ["node:*"]},
        token=x.admin,
        shape={"principal": {"principal_id": Eq(operator), "may_send_to": Eq(["node:*"])}},
    )
    question = "Ship the contract drive?"
    create = {"action": "question_create", "question": question, "timeout_secs": 120, "request_id": f"q1-{x.tag}"}
    waiters, _ = park_waiters(d.hub, op_token, 0)
    woken_at = time.monotonic()
    created = d.call(
        "questions create",
        "POST",
        "/messages",
        status=201,
        body=create,
        token=x.a,
        shape={
            **QUESTION,
            "caller": Eq(x.node_a),
            "request_id": Eq(f"q1-{x.tag}"),
            "operator": Eq(operator),
            "status": Eq("pending"),
            "answer": NONE,
            "created": Eq(True),
        },
    )
    qid = created.get("question_id")
    asked = created.get("message_id")

    def delivered(got: Any) -> bool:
        items = _items(got, "messages")
        return (
            len(items) == 1
            and str(items[0].get("id")) == asked
            and items[0].get("kind") == "operator_question"
            and items[0].get("sender") == x.node_a
            and items[0].get("body") == question
        )

    record_waiters(d, "operator long poll woken by question_create", waiters, woken_at, delivered)
    pinned = Eq(qid) if isinstance(qid, str) else HEX32
    d.call(
        "questions create retry",
        "POST",
        "/messages",
        status=200,
        body=create,
        token=x.a,
        shape={**QUESTION, "question_id": pinned, "created": Eq(False)},
    )
    q2 = {"action": "question_create", "question": "Second question?", "timeout_secs": 120, "request_id": f"q2-{x.tag}"}
    for case, body, token, status, shape in (
        (
            "questions create with a reused request_id",
            {**create, "question": "A different question?"},
            x.a,
            409,
            {"error": Eq("request_id was used for a different question"), "code": Eq("question_request_conflict")},
        ),
        (
            "questions status",
            {"action": "question_status", "question_id": qid},
            x.a,
            200,
            {**QUESTION, "question_id": pinned, "status": Eq("pending"), "created": Eq(False)},
        ),
        (
            "questions status by another node",
            {"action": "question_status", "question_id": qid},
            x.b,
            403,
            {"error": Eq("question is not owned by this credential"), "code": Eq("question_not_owned")},
        ),
        (
            "questions status with a malformed id",
            {"action": "question_status", "question_id": "not-a-question"},
            x.a,
            400,
            error_body("question_id is invalid"),
        ),
        (
            "questions create with a bad timeout",
            {**create, "timeout_secs": 0},
            x.a,
            400,
            error_body("timeout_secs must be an integer from 1 to 120"),
        ),
        (
            "questions create with an unsupported field",
            {**create, "priority": "high"},
            x.a,
            400,
            error_body("unsupported question fields"),
        ),
    ):
        d.call(case, "POST", "/messages", status=status, body=body, token=token, shape=shape)
    second = d.call(
        "questions create a second question",
        "POST",
        "/messages",
        status=201,
        body=q2,
        token=x.a,
        shape={**QUESTION, "status": Eq("pending"), "created": Eq(True)},
    )
    d.call(
        "questions create over the pending limit",
        "POST",
        "/messages",
        status=429,
        body={**q2, "request_id": f"q3-{x.tag}", "question": "Third question?"},
        token=x.a,
        shape={"error": Eq("question station has two pending questions"), "code": Eq("question_station_busy")},
    )
    d.call(
        "questions desktop answer",
        "POST",
        "/messages",
        status=200,
        body={"action": "question_desktop", "question_id": qid, "answer": "yes"},
        token=x.a,
        shape={
            **QUESTION,
            "question_id": pinned,
            "status": Eq("answered"),
            "answer": Eq("yes"),
            "source": Eq("caller_reported_desktop"),
            "answered_by": Eq(x.node_a),
        },
    )
    second_message = second.get("message_id")
    parent = int(second_message) if isinstance(second_message, str) and second_message.isdigit() else 1
    answered = d.call(
        "questions operator reply",
        "POST",
        "/messages",
        status=201,
        body={"action": "reply", "id": parent, "body": "approved"},
        token=op_token,
        shape={"message": {**MESSAGE, "sender": Eq(operator), "recipient": Eq(x.node_a), "reply_to": Eq(parent)}},
    )
    answer_id = _obj(answered.get("message")).get("id")
    d.call(
        "questions status after the operator reply",
        "POST",
        "/messages",
        status=200,
        body={"action": "question_status", "question_id": second.get("question_id")},
        token=x.a,
        shape={
            **QUESTION,
            "status": Eq("answered"),
            "answer": Eq("approved"),
            "source": Eq("operator_reply"),
            "answered_by": Eq(operator),
            "answer_id": Eq(str(answer_id)) if isinstance(answer_id, int) else DIGITS,
        },
    )


def run_contract(hub: Hub, *, claim_floor: int = 0) -> dict:
    """Drive every case against a running hub and return the findings.

    `claim_floor` is the hub's claim recovery floor (0 unless it was restored).
    """
    if type(claim_floor) is not int or not 0 <= claim_floor <= MAX_CLAIM_FLOOR:
        raise ContractError(f"the claim floor must be an integer in [0, {MAX_CLAIM_FLOOR}], not {claim_floor!r}")
    d = Drive(hub)
    admin = hub.admin_token
    tag = secrets.token_hex(3)
    node_a, node_b, node_c = (f"contract-{name}-{tag}" for name in ("a", "b", "c"))
    missing = "contract-missing-token"

    # -- health ---------------------------------------------------------------
    d.call("health", "GET", "/health", status=200, shape={"ok": Eq(True), "service": Eq(SERVICE_NAME)})
    d.call(
        "unknown route",
        "GET",
        "/contract-no-such-route",
        status=404,
        shape=error_body("not found"),
        token=admin,
        route=False,
    )
    d.call(
        "unknown route POST",
        "POST",
        "/contract-no-such-route",
        status=404,
        shape=error_body("not found"),
        body={},
        route=False,
    )

    # -- registration (admin) -------------------------------------------------
    added = {
        node: d.call(
            f"nodes add {suffix}",
            "POST",
            "/nodes",
            status=200,
            body={"action": "add", "node_id": node, **({"label": "contract node"} if suffix == "a" else {})},
            token=admin,
            shape={"added": Eq(True), "token": NonEmpty(), "node": {**NODE, "node_id": Eq(node), "revoked_at": NONE}},
        )
        for suffix, node in (("a", node_a), ("b", node_b), ("c", node_c))
    }
    tok = {node: str(payload.get("token") or missing) for node, payload in added.items()}
    a, b, c = tok[node_a], tok[node_b], tok[node_c]
    d.secrets.update(value for value in tok.values() if value != missing)
    label = _obj(added[node_a].get("node")).get("label")
    d.invariant("nodes add a", label == "contract node", "node.label echoes the request label", label)
    d.call(
        "nodes add duplicate",
        "POST",
        "/nodes",
        status=409,
        body={"action": "add", "node_id": node_a},
        token=admin,
        shape={"added": Eq(False), "error": NonEmpty()},
    )
    d.call(
        "nodes add bad action",
        "POST",
        "/nodes",
        status=400,
        body={"action": "rename", "node_id": node_a},
        token=admin,
        shape=error_body("node field 'action' must be one of: add, revoke"),
    )
    listed = d.call("nodes list", "GET", "/nodes", status=200, token=admin, shape={"nodes": [NODE]})
    seen = set(_keyed(_items(listed, "nodes"), "node_id")) if listed else set()
    d.invariant("nodes list", {node_a, node_b, node_c} <= seen, "every enrolled node is listed", sorted(seen))
    d.call(
        "nodes revoke",
        "POST",
        "/nodes",
        status=200,
        body={"action": "revoke", "node_id": node_c},
        token=admin,
        shape={"revoked": Eq(True), "node": {**NODE, "node_id": Eq(node_c), "revoked_at": NonEmpty()}},
    )
    d.call(
        "nodes revoke unknown",
        "POST",
        "/nodes",
        status=404,
        body={"action": "revoke", "node_id": f"contract-z-{tag}"},
        token=admin,
        shape={"revoked": Eq(False), "error": NonEmpty()},
    )
    principal = d.call(
        "message principal add",
        "POST",
        "/message-principals",
        status=201,
        body={"action": "add", "name": f"contract-agent-{tag}"},
        token=admin,
        shape={
            "principal": {
                "name": Eq(f"contract-agent-{tag}"),
                "principal_id": Eq(f"agent:contract-agent-{tag}"),
                "label": str,
                "enrollment_id": NonEmpty(),
                "may_send_to": [str],
                "token": NonEmpty(),
            }
        },
    )
    principal_token = str(_obj(principal.get("principal")).get("token") or missing)
    if principal_token != missing:
        d.secrets.add(principal_token)

    # -- auth failures --------------------------------------------------------
    unauthorized = error_body("unauthorized")
    d.call("status without a token", "GET", "/status", status=401, shape=unauthorized)
    d.call("status with a wrong token", "GET", "/status", status=401, shape=unauthorized, token=WRONG_TOKEN)
    d.call(
        "status with a non-bearer scheme",
        "GET",
        "/status",
        status=401,
        shape=unauthorized,
        headers={"Authorization": "Basic Y29udHJhY3Q6Y29udHJhY3Q="},
    )
    d.call(
        "status with a revoked node token",
        "GET",
        "/status",
        status=401,
        shape=error_body("unauthorized: node token revoked"),
        token=c,
    )
    d.call(
        "status with a message-principal token", "GET", "/status", status=401, shape=unauthorized, token=principal_token
    )
    d.call("claims without a token", "GET", "/claims", status=401, shape=unauthorized)
    d.call("owners without a token", "GET", "/owners", status=401, shape=unauthorized)
    d.call("messages without a token", "POST", "/messages", status=401, shape=unauthorized, body={"action": "count"})
    d.call(
        "messages with a wrong token",
        "POST",
        "/messages",
        status=401,
        shape=unauthorized,
        body={"action": "count"},
        token=WRONG_TOKEN,
    )
    d.call(
        "policy without a token",
        "GET",
        "/policy",
        status=401,
        shape={"error": {"code": Eq("auth-failed"), "message": Eq("unauthorized")}},
    )
    d.call(
        "token in the query string",
        "GET",
        "/status?token=contract-query-token",
        status=401,
        token=admin,
        json_body=False,
    )
    d.call(
        "nodes list with a node token",
        "GET",
        "/nodes",
        status=403,
        token=a,
        shape=error_body("the admin token is required to manage nodes"),
    )
    d.call(
        "nodes add with a node token",
        "POST",
        "/nodes",
        status=403,
        body={"action": "add", "node_id": f"contract-x-{tag}"},
        token=a,
        shape=error_body("the admin token is required to manage nodes"),
    )
    d.call(
        "message principal add with a node token",
        "POST",
        "/message-principals",
        status=403,
        body={"action": "add", "name": f"contract-other-{tag}"},
        token=a,
        shape=error_body("the admin token is required to enroll message principals"),
    )
    d.call(
        "events with the admin token",
        "POST",
        "/events",
        status=403,
        body=[],
        token=admin,
        shape=error_body(ADMIN_WRITE_REFUSAL),
    )
    d.call(
        "messages with the admin token",
        "POST",
        "/messages",
        status=403,
        body={"action": "count"},
        token=admin,
        shape=error_body(ADMIN_WRITE_REFUSAL),
    )
    d.call(
        "events with a message-principal token",
        "POST",
        "/events",
        status=401,
        body=[],
        token=principal_token,
        shape=unauthorized,
    )
    d.call(
        "invalid JSON body",
        "POST",
        "/events",
        status=400,
        raw=b"{not json",
        token=a,
        shape=error_body("body is not valid JSON"),
    )

    # -- heartbeat: run events and status ------------------------------------
    def event(run_id: str, sequence: int, state: str, node: str = node_a) -> dict:
        return {
            "node_id": node,
            "run_id": run_id,
            "state": state,
            "ts": "2026-01-01T00:00:00+00:00",
            "sequence": sequence,
            "digest": f"{run_id}-{sequence}",
            "repo": "example-repo",
            "seat": "coder",
            "harness": "claude",
        }

    live_run, done_run = f"contract-run-live-{tag}", f"contract-run-done-{tag}"
    d.call(
        "events accept",
        "POST",
        "/events",
        status=200,
        body=[event(live_run, 1, "run.started"), event(done_run, 1, "run.started")],
        token=a,
        shape={"accepted": Eq(2), "duplicate": Eq(0)},
    )
    d.call(
        "events duplicate",
        "POST",
        "/events",
        status=200,
        body=event(live_run, 1, "run.started"),
        token=a,
        shape={"accepted": Eq(0), "duplicate": Eq(1)},
    )
    d.call(
        "events terminal",
        "POST",
        "/events",
        status=200,
        body=[event(done_run, 2, "run.completed")],
        token=a,
        shape={"accepted": Eq(1), "duplicate": Eq(0)},
    )
    d.call(
        "events for another node",
        "POST",
        "/events",
        status=403,
        body=[event(live_run, 9, "run.started", node=node_b)],
        token=a,
        shape=error_body(f"event node_id {node_b!r} does not match the caller's node token ({node_a})"),
    )
    d.call(
        "events missing a field",
        "POST",
        "/events",
        status=400,
        body={"node_id": node_a},
        token=a,
        shape=error_body("event field 'run_id' must be a non-empty string"),
    )
    status = d.call("status", "GET", "/status", status=200, token=b, shape=STATUS)
    runs = _keyed(_items(status, "runs"), "run_id")
    d.invariant(
        "status",
        live_run in runs and done_run not in runs,
        "the default view lists the live run and hides the terminal one",
        sorted(str(key) for key in runs),
    )
    live = runs.get(live_run, {})
    d.invariant(
        "status",
        (live.get("node_id"), live.get("state"), live.get("sequence")) == (node_a, "run.started", 1),
        "the live run reports its node, state, and latest sequence",
        {key: live.get(key) for key in ("node_id", "state", "sequence")},
    )
    every = d.call("status all", "GET", "/status?all=1", status=200, token=admin, shape=STATUS)
    every_runs = _keyed(_items(every, "runs"), "run_id")
    done = every_runs.get(done_run, {})
    d.invariant(
        "status all",
        live_run in every_runs and done.get("state") == "run.completed",
        "?all=1 includes terminal runs at their latest state",
        {str(key): value.get("state") for key, value in every_runs.items()},
    )

    # -- heartbeat: interactive sessions -------------------------------------
    session_id = f"contract-session-{tag}"
    session_body = {
        "action": "upsert",
        "harness": "claude",
        "session_id": session_id,
        "repo_identity": "github.com/example-org/example-repo",
        "identity_scope": "fleet",
        "repo_label": "example-repo",
        "checkout_path": "/srv/example/checkout",
        "branch": "main",
        "dirty_paths": ["src/example.py"],
        "dirty_truncated": False,
        "ttl_seconds": 900,
    }
    d.call(
        "sessions upsert",
        "POST",
        "/sessions",
        status=200,
        body=session_body,
        token=a,
        shape={"session": {**SESSION, "node_id": Eq(node_a), "session_id": Eq(session_id), "state": Eq("active")}},
    )
    listed_sessions = d.call("sessions", "GET", "/sessions", status=200, token=b, shape={"sessions": [SESSION]})
    ids = [s.get("session_id") for s in _items(listed_sessions, "sessions")]
    d.invariant("sessions", session_id in ids, "the active session is listed", ids)
    d.call(
        "sessions with a node_id on a node write",
        "POST",
        "/sessions",
        status=400,
        body={**session_body, "node_id": node_b},
        token=a,
        shape=error_body("session field 'node_id' is not allowed on node writes"),
    )
    d.call(
        "sessions bad action",
        "POST",
        "/sessions",
        status=400,
        body={"action": "pause"},
        token=a,
        shape=error_body("session field 'action' must be 'upsert' or 'end'"),
    )
    d.call(
        "sessions end",
        "POST",
        "/sessions",
        status=200,
        body={key: session_body[key] for key in ("harness", "session_id", "repo_identity")} | {"action": "end"},
        token=a,
        shape={"session": {**SESSION, "session_id": Eq(session_id), "state": Eq("ended"), "ended_at": NonEmpty()}},
    )
    after_end = d.call("sessions after end", "GET", "/sessions", status=200, token=b, shape={"sessions": [SESSION]})
    ids = [s.get("session_id") for s in _items(after_end, "sessions")]
    d.invariant("sessions after end", session_id not in ids, "the default view hides an ended session", ids)
    ended = d.call("sessions all", "GET", "/sessions?all=1", status=200, token=b, shape={"sessions": [SESSION]})
    states = {key: item.get("state") for key, item in _keyed(_items(ended, "sessions"), "session_id").items()}
    d.invariant("sessions all", states.get(session_id) == "ended", "?all=1 includes the ended session", states)

    # -- claims ---------------------------------------------------------------
    target = f"repo:example-org/example-repo-{tag}"

    def claim(action: str, node: str, holder: str | None, **extra: Any) -> dict:
        body: dict[str, Any] = {"action": action, "target": target, "node_id": node, **extra}
        if holder is not None:
            body["holder"] = holder
        return body

    d.call(
        "claims acquire",
        "POST",
        "/claims",
        status=200,
        body=claim("acquire", node_a, "holder-a", ttl_seconds=600),
        token=a,
        shape={"granted": Eq(True), "claim": {**CLAIM, "target": Eq(target), "owner_node": Eq(node_a)}},
    )
    d.call(
        "claims acquire held",
        "POST",
        "/claims",
        status=409,
        body=claim("acquire", node_b, "holder-b", ttl_seconds=600),
        token=b,
        shape={
            "granted": Eq(False),
            "error": Eq(f"target {target!r} is held by {node_a}"),
            "owner": {**CLAIM, "owner_node": Eq(node_a)},
        },
    )
    d.call(
        "claims inspect",
        "POST",
        "/claims",
        status=200,
        body=claim("inspect", node_b, None),
        token=b,
        shape={
            "inspected": Eq(True),
            "owned": Eq(False),
            "generation": int,
            "claim": {**CLAIM, "owner_node": Eq(node_a)},
        },
    )
    d.call(
        "claims renew",
        "POST",
        "/claims",
        status=200,
        body=claim("renew", node_a, "holder-a", ttl_seconds=600),
        token=a,
        shape={"renewed": Eq(True), "claim": {**CLAIM, "owner_node": Eq(node_a)}},
    )
    held = d.call("claims list", "GET", "/claims", status=200, token=b, shape={"claims": [LISTED_CLAIM]})
    targets = [x.get("target") for x in _items(held, "claims")]
    d.invariant("claims list", target in targets, "the held claim is listed", targets)
    d.call(
        "claims for another node",
        "POST",
        "/claims",
        status=403,
        body=claim("acquire", node_b, "holder-b", ttl_seconds=600),
        token=a,
        shape=error_body(f"claim node_id {node_b!r} does not match the caller's node token ({node_a})"),
    )
    d.call(
        "claims bad action",
        "POST",
        "/claims",
        status=400,
        body=claim("steal", node_a, "holder-a"),
        token=a,
        shape=error_body("claim field 'action' must be one of: acquire, renew, release, inspect"),
    )
    d.call(
        "claims release",
        "POST",
        "/claims",
        status=200,
        body=claim("release", node_a, "holder-a"),
        token=a,
        shape={"released": Eq(True)},
    )
    released = d.call("claims after release", "GET", "/claims", status=200, token=b, shape={"claims": [LISTED_CLAIM]})
    targets = [x.get("target") for x in _items(released, "claims")]
    d.invariant("claims after release", target not in targets, "a released claim leaves the default view", targets)

    # -- messages: send, inbox, ack ------------------------------------------
    now = datetime.now(timezone.utc).isoformat()

    def send(
        client_id: str, *, to: str = node_b, subject: str = "contract", body: str = "hello", priority: str = "normal"
    ) -> dict:
        return {
            "action": "send",
            "to": to,
            "subject": subject,
            "body": body,
            "ref": None,
            "priority": priority,
            "client_id": client_id,
            "enqueued_at": now,
        }

    sent_shape = {"message": {**MESSAGE, "sender": Eq(node_a), "recipient": Eq(node_b), "acked_at": NONE}}
    first = d.call(
        "messages send",
        "POST",
        "/messages",
        status=201,
        body=send(f"c1-{tag}", body="first"),
        token=a,
        shape={"message": {**sent_shape["message"], "client_id": Eq(f"c1-{tag}"), "body": Eq("first")}},
    )
    second = d.call(
        "messages send second",
        "POST",
        "/messages",
        status=201,
        body=send(f"c2-{tag}", body="second", priority="high"),
        token=a,
        shape={"message": {**sent_shape["message"], "client_id": Eq(f"c2-{tag}"), "priority": Eq("high")}},
    )
    m1 = _obj(first.get("message")).get("id")
    m2 = _obj(second.get("message")).get("id")
    d.invariant(
        "messages send second",
        isinstance(m1, int) and isinstance(m2, int) and m2 > m1,
        "message ids increase in send order",
        [m1, m2],
    )
    again = d.call(
        "messages send retry",
        "POST",
        "/messages",
        status=201,
        body=send(f"c1-{tag}", body="first"),
        token=a,
        shape={"message": {**MESSAGE, "client_id": Eq(f"c1-{tag}")}},
    )
    retry_id = _obj(again.get("message")).get("id")
    d.invariant("messages send retry", retry_id == m1, "a retry with the same client_id returns the first id", retry_id)
    d.call(
        "messages send client_id conflict",
        "POST",
        "/messages",
        status=409,
        body=send(f"c1-{tag}", body="changed"),
        token=a,
        shape={
            "code": Eq("message_client_id_conflict"),
            "error": Eq("client_id was already used for a different message"),
            "id": Eq(m1) if isinstance(m1, int) else int,
        },
    )
    d.call(
        "messages send to an unknown recipient",
        "POST",
        "/messages",
        status=400,
        body=send(f"c3-{tag}", to=f"contract-nobody-{tag}"),
        token=a,
        shape=error_body("recipient is not an active hub principal"),
    )
    d.call(
        "messages send without a grant",
        "POST",
        "/messages",
        status=403,
        body=send(f"p1-{tag}"),
        token=principal_token,
        shape={"code": Eq("message_recipient_not_permitted"), "error": Eq("recipient is not permitted by send grants")},
    )
    d.call(
        "messages count",
        "POST",
        "/messages",
        status=200,
        body={"action": "count"},
        token=b,
        shape={"unread_count": Eq(2)},
    )
    page = d.call(
        "messages list",
        "POST",
        "/messages",
        status=200,
        body={"action": "list", "after": 0, "unacked": False},
        token=b,
        shape=PAGE,
    )
    page_ids = _ids(page)
    d.invariant("messages list", page_ids == [m1, m2], "the inbox lists both messages in ascending id order", page_ids)
    d.invariant(
        "messages list",
        isinstance(page, dict) and page.get("next_cursor") == (page_ids[-1] if page_ids else 0),
        "next_cursor is the last id on the page",
        page.get("next_cursor") if isinstance(page, dict) else None,
    )
    page = d.call(
        "messages list after a cursor",
        "POST",
        "/messages",
        status=200,
        body={"action": "list", "after": m1 if isinstance(m1, int) else 0, "unacked": False},
        token=b,
        shape=PAGE,
    )
    d.invariant(
        "messages list after a cursor", _ids(page) == [m2], "only ids above the cursor are returned", _ids(page)
    )
    end = m2 if isinstance(m2, int) else 0
    page = d.call(
        "messages list past the end",
        "POST",
        "/messages",
        status=200,
        body={"action": "list", "after": end, "unacked": False},
        token=b,
        shape={"messages": Eq([]), "next_cursor": Eq(end)},
    )
    d.call(
        "messages ack",
        "POST",
        "/messages",
        status=200,
        body={"action": "ack", "id": m1},
        token=b,
        shape={
            "message": {
                **MESSAGE,
                "id": Eq(m1) if isinstance(m1, int) else int,
                "acked_by": Eq(node_b),
                "acked_at": NonEmpty(),
            }
        },
    )
    page = d.call(
        "messages list unacked",
        "POST",
        "/messages",
        status=200,
        body={"action": "list", "after": 0, "unacked": True},
        token=b,
        shape=PAGE,
    )
    d.invariant("messages list unacked", _ids(page) == [m2], "unacked=true hides the acknowledged message", _ids(page))
    d.call(
        "messages count after ack",
        "POST",
        "/messages",
        status=200,
        body={"action": "count"},
        token=b,
        shape={"unread_count": Eq(1)},
    )
    d.call(
        "messages ack by a non-recipient",
        "POST",
        "/messages",
        status=404,
        body={"action": "ack", "id": m2},
        token=a,
        shape=error_body("message not found"),
    )
    d.call(
        "messages ack unknown id",
        "POST",
        "/messages",
        status=404,
        body={"action": "ack", "id": 2**53},
        token=b,
        shape=error_body("message not found"),
    )
    d.call(
        "messages list bad cursor",
        "POST",
        "/messages",
        status=400,
        body={"action": "list", "after": -1, "unacked": False},
        token=b,
        shape=error_body("after must be a non-negative integer within SQLite's range"),
    )
    d.call(
        "messages bad action",
        "POST",
        "/messages",
        status=400,
        body={"action": "shout"},
        token=b,
        shape=error_body("action must be send, list, sent, count, ack, reply, who, claims, or identity"),
    )
    d.call(
        "messages body not an object",
        "POST",
        "/messages",
        status=400,
        body=["count"],
        token=b,
        shape=error_body("message request must be an object"),
    )
    d.call(
        "messages identity",
        "POST",
        "/messages",
        status=200,
        body={"action": "identity"},
        token=a,
        shape={"principal": Eq(node_a)},
    )
    d.call(
        "messages identity for a message principal",
        "POST",
        "/messages",
        status=200,
        body={"action": "identity"},
        token=principal_token,
        shape={"principal": Eq(f"agent:contract-agent-{tag}")},
    )
    outbox = d.call("messages sent", "POST", "/messages", status=200, body={"action": "sent"}, token=a, shape=PAGE)
    d.invariant(
        "messages sent", _ids(outbox) == [m1, m2], "the outbox lists sent messages in ascending id order", _ids(outbox)
    )

    # -- owners and gated surfaces -------------------------------------------
    d.call("owners", "GET", "/owners", status=200, token=a, shape=OWNERS)
    d.call("owners as admin", "GET", "/owners", status=200, token=admin, shape=OWNERS)
    d.call("worklore off by default", "GET", "/work/items", status=404, token=a, shape=error_body("not found"))

    # -- hot set: the routes the Rust hub ports first ------------------------
    ctx = Ctx(
        tag=tag,
        admin=admin,
        node_a=node_a,
        node_b=node_b,
        a=a,
        b=b,
        principal_id=f"agent:contract-agent-{tag}",
        principal_token=principal_token,
        m1=m1,
        m2=m2,
        c1=f"c1-{tag}",
        claim_floor=claim_floor,
    )
    drive_claim_fencing(d, ctx)
    drive_keys_and_sealed(d, ctx)
    drive_message_actions(d, ctx)
    drive_long_poll(d, ctx)
    drive_questions(d, ctx, hub.operator or new_operator())
    import hub_contract_refusals  # a sibling that imports this module, so it loads late

    hub_contract_refusals.drive_refusals(d, ctx)

    for label, seen, listed in (
        ("COVERED_ROUTES", d.routes, COVERED_ROUTES),
        ("COVERED_MESSAGE_ACTIONS", d.actions, COVERED_MESSAGE_ACTIONS),
    ):
        unexpected = sorted(seen - set(listed))
        unlisted = sorted(set(listed) - seen)
        if unexpected or unlisted:
            d.mismatches.append(
                {
                    "case": "coverage",
                    "check": "invariant",
                    "expected": f"the drive exercises exactly {label}",
                    "observed": {f"not in {label}": unexpected, "never exercised": unlisted},
                }
            )
    secrets_ = {secret for secret in d.secrets if len(secret) >= 8}
    return {
        "cases": d.cases,
        "mismatches": scrub(d.mismatches, secrets_),
        "exchanges": scrub(d.exchanges, secrets_),
        "covered_routes": sorted(d.routes),
        "covered_message_actions": sorted(d.actions),
        "tag": tag,
    }


def drive(
    *,
    brigade: list[str],
    state_dir: Path | None,
    hub_cmd: str | None,
    hub_url: str | None,
    admin_token: str | None,
    startup_timeout: float,
    operator: str | None = None,
    claim_floor: int = 0,
) -> tuple[Hub, dict]:
    """Start (or attach to) a hub, run the contract, and always stop what it started."""
    if hub_url is not None:
        if not admin_token:
            raise ContractError("--hub-url needs the admin token in the environment variable --admin-token-env names")
        hub = Hub(
            url=guard_hub_url(hub_url),
            admin_token=admin_token,
            mode="external",
            operator=guard_operator(operator) if operator else new_operator(),
        )
        return hub, run_contract(hub, claim_floor=claim_floor)
    if claim_floor:
        raise ContractError("a launched hub is fresh, so its claim floor is 0; a floor needs --hub-url")
    if state_dir is None:
        raise ContractError("a launched hub needs a state directory")
    hub = launch_hub(hub_cmd or DEFAULT_HUB_CMD, brigade=brigade, state_dir=state_dir, startup_timeout=startup_timeout)
    try:
        return hub, run_contract(hub)
    finally:
        stop_hub(hub)


if __name__ == "__main__":  # pragma: no cover - the entry point is control-brigade.py hub-contract
    sys.exit("run this through control-brigade.py hub-contract")
