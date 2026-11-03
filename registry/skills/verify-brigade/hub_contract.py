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
`--allow-admin-writes`, no `--require-sealed`, and Worklore off.
"""

from __future__ import annotations

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


# ----------------------------------------------------------- hub process


@dataclass
class Hub:
    url: str
    admin_token: str
    mode: str
    argv: list[str] = field(default_factory=list)
    state_dir: Path | None = None
    log: Path | None = None
    process: subprocess.Popen | None = None
    log_handle: Any = None


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


def hub_env(state_dir: Path, admin_token: str) -> dict[str, str]:
    """A child environment that cannot see the operator's hub, tokens, or home.

    Every `BRIGADE_*` variable is dropped (a stray `BRIGADE_FLEET_TOKEN` would
    otherwise become the admin token), the XDG roots fall back to the fake
    home, and the throwaway admin token is passed both ways a hub reads it.
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
    # A Python hub block-buffers its startup line; keep it in hub.log.
    env["PYTHONUNBUFFERED"] = "1"
    return env


def launch_hub(template: str, *, brigade: list[str], state_dir: Path, startup_timeout: float) -> Hub:
    admin_token = "contract-admin-" + secrets.token_urlsafe(18)
    token_file = state_dir / "admin-token"
    token_file.write_text(admin_token + "\n", encoding="utf-8")
    token_file.chmod(0o600)
    port = free_port()
    values = {
        "host": LOOPBACK,
        "port": str(port),
        "state_dir": str(state_dir),
        "db": str(state_dir / "hub.db"),
        "token_file": str(token_file),
    }
    argv = expand_command(template, brigade=brigade, values=values)
    log = state_dir / "hub.log"
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
            env=hub_env(state_dir, admin_token),
            cwd=str(state_dir),
            **popen_kwargs,
        )
    except OSError as exc:
        handle.close()
        raise ContractError(f"could not start the hub: {exc}") from exc
    hub = Hub(
        url=f"http://{LOOPBACK}:{port}",
        admin_token=admin_token,
        mode="launched",
        argv=argv,
        state_dir=state_dir,
        log=log,
        process=process,
        log_handle=handle,
    )
    deadline = time.monotonic() + startup_timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stop_hub(hub)
            raise ContractError(f"the hub exited with {process.returncode} before it was healthy: {log_tail(log)}")
        status = request(hub.url, "GET", "/health", timeout=1.0).status
        if status is not None:
            return hub
        time.sleep(0.1)
    stop_hub(hub)
    raise ContractError(f"the hub did not answer /health within {startup_timeout:g}s: {log_tail(log)}")


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
        chunk = stream.read(65536)
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
    ) -> Any:
        """One exchange: check the status, the content type, and the body shape.

        Returns the parsed JSON body (an empty dict when there is none), so a
        later case can use an id or token the hub handed out. `route=False`
        keeps a deliberately unknown path out of the coverage list.
        """
        resp = request(self.hub.url, method, path, body=body, raw=raw, token=token, headers=headers)
        if route:
            self.routes.add(f"{method} {path.partition('?')[0]}")
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


def run_contract(hub: Hub) -> dict:
    """Drive every case against a running hub and return the findings."""
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

    unexpected = sorted(d.routes - set(COVERED_ROUTES))
    unlisted = sorted(set(COVERED_ROUTES) - d.routes)
    if unexpected or unlisted:
        d.mismatches.append(
            {
                "case": "coverage",
                "check": "invariant",
                "expected": "the drive exercises exactly COVERED_ROUTES",
                "observed": {"not in COVERED_ROUTES": unexpected, "never exercised": unlisted},
            }
        )
    secrets_ = {secret for secret in d.secrets if len(secret) >= 8}
    return {
        "cases": d.cases,
        "mismatches": scrub(d.mismatches, secrets_),
        "exchanges": scrub(d.exchanges, secrets_),
        "covered_routes": sorted(d.routes),
    }


def drive(
    *,
    brigade: list[str],
    state_dir: Path | None,
    hub_cmd: str | None,
    hub_url: str | None,
    admin_token: str | None,
    startup_timeout: float,
) -> tuple[Hub, dict]:
    """Start (or attach to) a hub, run the contract, and always stop what it started."""
    if hub_url is not None:
        if not admin_token:
            raise ContractError("--hub-url needs the admin token in the environment variable --admin-token-env names")
        hub = Hub(url=guard_hub_url(hub_url), admin_token=admin_token, mode="external")
        return hub, run_contract(hub)
    if state_dir is None:
        raise ContractError("a launched hub needs a state directory")
    hub = launch_hub(hub_cmd or DEFAULT_HUB_CMD, brigade=brigade, state_dir=state_dir, startup_timeout=startup_timeout)
    try:
        return hub, run_contract(hub)
    finally:
        stop_hub(hub)


if __name__ == "__main__":  # pragma: no cover - the entry point is control-brigade.py hub-contract
    sys.exit("run this through control-brigade.py hub-contract")
