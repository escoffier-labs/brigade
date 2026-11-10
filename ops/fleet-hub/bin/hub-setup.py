#!/usr/bin/env python3
"""Setup for the Fleet Hub Compose recipe: retrieval mode, backend, config and secrets (D30).

`init` picks the retrieval mode and backend, in order:

1. A supported local GPU: the GPU vendor's container runtime is present, a probe
   container sees the GPU, and the card has free memory for a model that passed
   R1. Writes the `gpu` Compose profile, the `local` backend and `hybrid`.
2. Otherwise a remote endpoint the operator supplied (URL and token file). One
   probe embedding checks the dimensions and the reported model digest. Writes
   the `remote` backend and `hybrid`. The embedder container is never created.
3. Otherwise BM25 mode (`lexical`). Setup names every check that failed and
   says how to switch later.

Either hybrid path writes the `[memory.embedder]` table the Rust hub parses
(docs/hub-memory-embedder.md): the request URL and its allowlist entry, the
token file, the request shape, and the model values R1 ran with from
r1-models.json. It never writes a hybrid configuration it could not probe and
never overwrites an existing secret. The one derived file, the operator's
mode-0600 copy of the admin token, follows the container copy. `switch` changes
the mode either way, and `units` renders the systemd templates for review.
Nothing here installs a package, a unit or a container. Stdlib only.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import ipaddress
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

RECIPE = Path(__file__).resolve().parents[1]
DEFAULT_EMBED_IMAGE = "ollama/ollama:0.18.0@sha256:e23e6499890325d31f3454cdc83f7a613a2423c8a9e167bbafea0b0e3c27d7c6"
LOCAL_ENDPOINT = "http://embedder:11434"
# Where Compose mounts the embedder token in the hub container (compose.override.yaml).
EMBED_TOKEN_MOUNT = "/run/secrets/embed_token"
# Where hub/entry.py puts the hub's own copy of that mount, and what hub.toml names.
# The hub accepts only a token file its own user owns with mode 0400 or 0600, and a
# bind mount keeps the operator's ownership, so the entrypoint copies it at each start.
EMBED_TOKEN_COPY = "/run/brigade/embed_token"
# The token files under secrets/: the operator's remote token, and one setup makes for
# the local backend. Separate files, so a switch between backends never reuses the
# other backend's token and never needs to overwrite a secret.
EMBED_TOKEN_FILES = {"remote": "embed-token", "local": "embed-local-token"}
# What each R1 model entry must carry before setup offers it (r1-models.json, schema 2).
R1_KEYS = (
    "name",
    "dimensions",
    "gpu_memory_mib",
    "model_digest",
    "input_chars",
    "query_prefix",
    "query_prefix_version",
)
PROBE_TEXT = "brigade setup probe"
TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
SECRET_FILES = ("admin-token.txt", "findings-key", "front-authkey")
# The operator's copy of the admin token for `brigade memory rescan` and `reindex`
# (`--admin-token-file`), which accept only mode 0600. Compose never mounts it.
OPERATOR_TOKEN = "operator-admin-token.txt"
UNIT_TEMPLATES = (
    ("brigade-hub-compose.service.in", "brigade-hub-compose.service"),
    ("brigade-hub-watchdog.service.in", "brigade-hub-watchdog.service"),
    ("brigade-hub-watchdog.timer.in", "brigade-hub-watchdog.timer"),
    ("brigade-hub-backup.service.in", "brigade-hub-backup.service"),
    ("brigade-hub-backup.timer.in", "brigade-hub-backup.timer"),
    ("brigade.slice.in", "brigade.slice"),
    ("brigade-hub.slice.in", "brigade-hub.slice"),
    ("brigade-embed.slice.in", "brigade-embed.slice"),
    ("user-slice-50-brigade-agent-cpus.conf.in", "user.slice.d/50-brigade-agent-cpus.conf"),
    ("service-50-brigade-agent-cpus.conf.in", "service.d/50-brigade-agent-cpus.conf"),
    ("docker-50-brigade-stop.conf.in", "docker.service.d/50-brigade-stop.conf"),
)

Runner = Callable[[list[str]], tuple[int, str]]
Poster = Callable[[str, dict[str, Any], dict[str, str]], tuple[int, dict[str, str], Any]]


class SetupError(Exception):
    pass


def run_command(argv: list[str]) -> tuple[int, str]:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)
    return done.returncode, done.stdout


def post_json(url: str, body: dict[str, Any], headers: dict[str, str]) -> tuple[int, dict[str, str], Any]:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json", **headers}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, {}, None


@dataclass
class Decision:
    retrieval: str
    backend: str | None = None
    model: str | None = None
    dimensions: int | None = None
    # The full request URL the hub posts to: the base URL plus the shape.
    endpoint: str | None = None
    api: str | None = None
    token_file: str | None = None
    model_digest: str | None = None
    input_chars: int | None = None
    query_prefix: str | None = None
    query_prefix_version: int | None = None
    profiles: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    # The remote token the probe passed with. Kept out of repr so no report or
    # traceback prints it. install_probed_token writes it to secrets/embed-token.
    probed_token: str | None = field(default=None, repr=False, compare=False)


def load_r1(path: Path) -> list[dict[str, Any]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    passed = [model for model in document.get("models", []) if model.get("r1") == "passed"]
    for model in passed:
        missing = [key for key in R1_KEYS if key not in model]
        if missing:
            raise SetupError(f"{path}: model {model.get('name', '?')} lacks {', '.join(missing)} (schema 2)")
    return passed


def hybrid(model: dict[str, Any], **values: Any) -> Decision:
    """A hybrid decision carrying the values R1 ran this model with."""
    return Decision(
        retrieval="hybrid",
        model=model["name"],
        dimensions=int(model["dimensions"]),
        input_chars=int(model["input_chars"]),
        query_prefix=str(model["query_prefix"]),
        query_prefix_version=int(model["query_prefix_version"]),
        token_file=EMBED_TOKEN_COPY,
        **values,
    )


def embed_body(api: str, model: str, text: str) -> dict[str, Any]:
    """The request body the hub sends for this shape (engines/hub/src/memory/embedder.rs)."""
    if api == "v1/embeddings":
        return {"model": model, "input": [text], "encoding_format": "float"}
    return {"model": model, "input": [text], "truncate": False}


def detect_gpu(run: Runner, models: list[dict[str, Any]], image: str) -> tuple[dict[str, Any] | None, list[str]]:
    """Return (model, failed checks). The model is the first R1 model the card has room for."""
    failed: list[str] = []
    rc, out = run(["docker", "info", "--format", "{{json .Runtimes}}"])
    try:
        runtimes = json.loads(out) if rc == 0 else {}
    except ValueError:
        runtimes = {}
    if "nvidia" not in runtimes:
        return None, ["gpu: no GPU container runtime (the GPU vendor's container toolkit is not configured)"]
    rc, out = run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--gpus",
            "1",
            "--entrypoint",
            "nvidia-smi",
            image,
            "--query-gpu=name,memory.free",
            "--format=csv,noheader,nounits",
        ]
    )
    rows = [line.split(",") for line in out.strip().splitlines() if line.strip()] if rc == 0 else []
    if not rows:
        return None, ["gpu: a probe container with one GPU saw no GPU"]
    try:
        free = max(int(row[-1].strip()) for row in rows)
    except ValueError:
        return None, ["gpu: could not read the GPU's free memory"]
    if not models:
        return None, ["gpu: no embedding model has passed R1, so setup offers none"]
    for model in models:
        if free >= int(model["gpu_memory_mib"]):
            return model, failed
    need = min(int(model["gpu_memory_mib"]) for model in models)
    return None, [f"gpu: {free} MiB free on the card, the smallest R1 model needs {need} MiB"]


def endpoint_allowed(url: str) -> bool:
    """HTTPS anywhere, or plain HTTP to a tailnet name or address (the remote-endpoint contract)."""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme == "https" and host:
        return True
    if parts.scheme != "http" or not host:
        return False
    if host.endswith(".ts.net"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address in TAILNET_V4 or address in TAILNET_V6


def probe_remote(
    url: str, token_file: Path, api: str, models: list[dict[str, Any]], post: Poster
) -> tuple[Decision | None, list[str]]:
    if not endpoint_allowed(url):
        return None, ["remote: endpoint must be HTTPS, or HTTP to a tailnet name or address"]
    if not models:
        return None, ["remote: no embedding model has passed R1, so setup offers none"]
    try:
        token = token_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None, [f"remote: token file {token_file} is unreadable"]
    if not token:
        return None, [f"remote: token file {token_file} is empty"]
    # A bearer token has no whitespace or control characters. One with a line break
    # would also put the token into the HTTP library's error message.
    if any(ord(char) < 0x21 or ord(char) == 0x7F for char in token):
        return None, [f"remote: token file {token_file} holds whitespace or control characters inside the token"]
    failures = []
    endpoint = url.rstrip("/") + "/" + api
    for model in models:
        body = embed_body(api, model["name"], PROBE_TEXT)
        try:
            status, headers, payload = post(endpoint, body, {"Authorization": f"Bearer {token}"})
        except (OSError, ValueError) as exc:
            failures.append(f"remote: probe of {model['name']} failed: {_probe_error(exc)}")
            continue
        if status != 200 or not isinstance(payload, dict):
            failures.append(f"remote: probe of {model['name']} answered HTTP {status}")
            continue
        try:
            vector = payload["data"][0]["embedding"] if api == "v1/embeddings" else payload["embeddings"][0]
        except (KeyError, IndexError, TypeError):
            failures.append(f"remote: probe of {model['name']} returned no vector")
            continue
        if len(vector) != int(model["dimensions"]):
            failures.append(f"remote: {model['name']} returned {len(vector)} dimensions, R1 ran {model['dimensions']}")
            continue
        # Where the hub reads it: the body's `model_digest` or the `x-model-digest`
        # header, and both must agree when both appear.
        header_digest = headers.get("x-model-digest")
        body_digest = payload.get("model_digest")
        if header_digest and body_digest and header_digest != body_digest:
            failures.append("remote: the endpoint reports two different model digests")
            continue
        digest = body_digest or header_digest
        if not digest or not isinstance(digest, str):
            failures.append("remote: the endpoint reports no model digest (model_digest or x-model-digest)")
            continue
        decision = hybrid(model, backend="remote", endpoint=endpoint, api=api, model_digest=digest)
        decision.probed_token = token
        return decision, []
    return None, failures


def _probe_error(exc: BaseException) -> str:
    """Why a probe request failed, without the exception's own message.

    That message can quote the request: http.client puts an invalid header value,
    the Authorization header included, in its ValueError. Only the operating
    system's or TLS library's reason text and the exception's type are reported.
    """
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, OSError) and isinstance(reason.strerror, str) and reason.strerror:
        return reason.strerror
    return type(reason).__name__


def decide(
    *,
    run: Runner,
    post: Poster,
    models: list[dict[str, Any]],
    embed_image: str,
    remote_url: str | None,
    remote_token: Path | None,
    remote_api: str,
    skip_gpu: bool = False,
) -> Decision:
    failed: list[str] = []
    if skip_gpu:
        failed.append("gpu: skipped (--no-gpu)")
    else:
        model, gpu_failed = detect_gpu(run, models, embed_image)
        if model is not None:
            # The recipe's embedder container is not running yet, so the digest is
            # the one R1 ran. A server reporting another digest is refused by the hub.
            return hybrid(
                model,
                backend="local",
                endpoint=LOCAL_ENDPOINT + "/api/embed",
                api="api/embed",
                model_digest=str(model["model_digest"]),
                profiles=["gpu"],
            )
        failed += gpu_failed
    if remote_url and remote_token:
        remote, remote_failed = probe_remote(remote_url, remote_token, remote_api, models, post)
        if remote is not None:
            remote.failed = failed
            return remote
        failed += remote_failed
    else:
        failed.append("remote: no remote endpoint given (--remote-url and --remote-token-file)")
    return Decision(retrieval="lexical", failed=failed)


def _toml_string(value: str) -> str:
    # A JSON string is a valid TOML basic string: both escape quotes,
    # backslashes and control characters the same way.
    return json.dumps(value)


def hub_toml(decision: Decision) -> str:
    """`[memory]` with exactly the embedder keys the Rust hub parses (engines/hub/src/memory/embedder.rs)."""
    lines = [
        "# Written by bin/hub-setup.py. The Rust hub reads [memory] (docs/hub-memory-embedder.md).",
        "[memory]",
        f"retrieval = {_toml_string(decision.retrieval)}",
    ]
    if decision.retrieval == "hybrid":
        values = (
            decision.backend,
            decision.endpoint,
            decision.api,
            decision.token_file,
            decision.model,
            decision.model_digest,
            decision.dimensions,
            decision.input_chars,
            decision.query_prefix,
            decision.query_prefix_version,
        )
        if any(value is None for value in values):
            raise SetupError("refusing to write hybrid: the embedder decision is incomplete")
        endpoint = _toml_string(str(decision.endpoint))
        lines += [
            "",
            "[memory.embedder]",
            f"backend = {_toml_string(str(decision.backend))}",
            f"endpoint = {endpoint}",
            # The hub posts only to an endpoint its allowlist names exactly.
            f"allowlist = [{endpoint}]",
            f"token_file = {_toml_string(str(decision.token_file))}",
            f"shape = {_toml_string(str(decision.api))}",
            f"name = {_toml_string(str(decision.model))}",
            f"model_digest = {_toml_string(str(decision.model_digest))}",
            f"dimensions = {int(decision.dimensions or 0)}",
            f"input_chars = {int(decision.input_chars or 0)}",
            f"query_prefix = {_toml_string(str(decision.query_prefix))}",
            f"query_prefix_version = {int(decision.query_prefix_version or 0)}",
        ]
    return "\n".join(lines) + "\n"


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    return values


def write_env(path: Path, values: dict[str, str]) -> None:
    body = "# Written by bin/hub-setup.py. Compose reads it from the project directory.\n"
    body += "".join(f"{key}={values[key]}\n" for key in sorted(values))
    path.write_text(body, encoding="utf-8")


def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _require_private(folder: Path) -> None:
    """Refuse a `secrets/` that group or other users can enter. Its mode is never changed here.

    The mounted secrets are mode 0444 so the hub container's UID can read the
    bind mount, which leaves the folder's mode 0700 as the only thing keeping
    other host users out. A folder found wider than that may already have shown
    its 0444 tokens to those users, so setup stops and says so. Tightening it
    quietly would hide that, and only the operator can decide what to rotate.
    """
    if os.name != "posix":
        return
    mode = _check_dir(folder, "secrets/")
    if mode is not None and mode & 0o077:
        raise SetupError(
            f"secrets/ has mode {mode:04o}, which lets group or other users in, and the token files in it are "
            "mode 0444 for the hub container. Setup wrote nothing and left the folder as it is. Run chmod 700 "
            "on it, rotate any token in it that other users could have read (the admin token, the findings key "
            "and the embedder tokens), then rerun setup"
        )


def _secrets_dir(folder: Path) -> None:
    """Make `secrets/` mode 0700 when it is absent, and refuse an existing one that is wider. Never chmod one.

    A new folder is made under a umask of 0077, so it is 0700 whatever umask
    setup was started with, and then read back for a filesystem that ignores the
    mode. An existing folder is only looked at.
    """
    if _check_dir(folder, "secrets/") is None:
        folder.parent.mkdir(parents=True, exist_ok=True)
        previous = os.umask(0o077)
        try:
            os.mkdir(folder, 0o700)
        except FileExistsError:
            pass  # Made by someone else since the check: an existing folder, so it is checked and never chmodded.
        else:
            created = _check_dir(folder, "secrets/")
            if os.name == "posix" and created != 0o700:
                raise SetupError(
                    f"secrets/ was created with mode {created or 0:04o}, not 0700. Check that this filesystem "
                    "keeps POSIX modes, then rerun setup"
                )
            return
        finally:
            os.umask(previous)
    _require_private(folder)


def write_secret(path: Path, data: bytes, mode: int = 0o444) -> bool:
    """Create a secret once. Mode 0444 inside a 0700 directory: the container's own UID must read the bind mount.

    That holds for the embedder tokens too. The hub's token rule (its own user,
    mode 0400 or 0600) is met by the copy hub/entry.py makes inside the container.
    """
    if path.exists():
        return False
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    os.chmod(path, mode)
    return True


def ensure_operator_token(folder: Path) -> str | None:
    """Keep the operator's mode-0600 copy of the admin token in step with the container copy.

    `brigade memory rescan` and `reindex` take `--admin-token-file` and accept only
    a mode-0600 file, while the container copy must stay 0444 for the hub's UID.
    Creates the copy when it is missing (an install from before this copy existed),
    refreshes it when the container copy changed and resets a drifted mode. Returns
    what it did, or None. Never prints either token.

    On POSIX every step goes through one descriptor for `secrets/` and descriptors
    opened without following a symlink, so swapping a name after the checks cannot
    redirect a read, a chmod or the final rename to another file.
    """
    if os.name != "posix":
        return _ensure_operator_token_by_path(folder)
    try:
        dir_fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        if exc.errno in _NOFOLLOW_ERRNOS | {errno.ENOTDIR}:
            raise SetupError("secrets/ is a symlink or not a directory; move it aside and rerun") from None
        raise
    try:
        return _sync_operator_token(dir_fd)
    finally:
        os.close(dir_fd)


# O_NOFOLLOW on a symlink fails with ELOOP on Linux and macOS, EMLINK on FreeBSD.
_NOFOLLOW_ERRNOS = frozenset({errno.ELOOP, errno.EMLINK})


def _open_regular(name: str, dir_fd: int) -> int | None:
    """A read descriptor for `name` in `secrets/`, never through a symlink. None when it is absent."""
    try:
        # O_NONBLOCK so a FIFO planted under the name is refused instead of blocking the open.
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno in _NOFOLLOW_ERRNOS:
            raise SetupError(f"secrets/{name} is a symlink; move it aside and rerun") from None
        raise
    # The caller owns `fd` only once it is returned, so every other exit closes it here.
    try:
        regular = stat.S_ISREG(os.fstat(fd).st_mode)
    except BaseException:
        os.close(fd)
        raise
    if not regular:
        os.close(fd)
        raise SetupError(f"secrets/{name} is not a regular file; move it aside and rerun")
    return fd


def _read_fd(fd: int) -> bytes:
    chunks = []
    while chunk := os.read(fd, 65536):
        chunks.append(chunk)
    return b"".join(chunks)


def _sync_operator_token(dir_fd: int) -> str | None:
    source = _open_regular("admin-token.txt", dir_fd)
    if source is None:
        raise SetupError("secrets/admin-token.txt is missing")
    try:
        token = _read_fd(source)
    finally:
        os.close(source)
    target = _open_regular(OPERATOR_TOKEN, dir_fd)
    if target is not None:
        try:
            if secrets.compare_digest(_read_fd(target), token):
                if stat.S_IMODE(os.fstat(target).st_mode) == 0o600:
                    return None
                try:
                    os.fchmod(target, 0o600)
                except PermissionError as exc:
                    raise SetupError(
                        f"cannot reset secrets/{OPERATOR_TOKEN} to mode 0600; chmod it as its owner"
                    ) from exc
                return f"{OPERATOR_TOKEN} (mode reset to 0600)"
        finally:
            os.close(target)
        action = f"{OPERATOR_TOKEN} (refreshed: admin-token.txt changed)"
    else:
        action = f"{OPERATOR_TOKEN} (mode 0600, for --admin-token-file)"
    # Write a new private file and rename it over the copy, so the copy is never briefly wider than 0600.
    staging = f".{OPERATOR_TOKEN}.{secrets.token_hex(8)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(staging, flags, 0o600, dir_fd=dir_fd)
    # Any failure before the rename unlinks the staging file in the `finally` below.
    try:
        try:
            handle = os.fdopen(fd, "wb")
        except BaseException:
            # fdopen failed, so no file object owns the descriptor. io.open may already have
            # closed it on a late failure, hence the suppress.
            with contextlib.suppress(OSError):
                os.close(fd)
            raise
        with handle:
            # The umask can narrow the create mode below 0600 (0400 under 0277), which the CLI refuses.
            os.fchmod(handle.fileno(), 0o600)
            handle.write(token)
        os.replace(staging, OPERATOR_TOKEN, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    finally:
        try:
            os.unlink(staging, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
    return action


def _ensure_operator_token_by_path(folder: Path) -> str | None:
    """The same steps by path, for Windows, which has no descriptor-relative opens or POSIX modes."""
    source = folder / "admin-token.txt"
    target = folder / OPERATOR_TOKEN
    token = source.read_bytes()
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise SetupError(f"secrets/{OPERATOR_TOKEN} is not a regular file; move it aside and rerun")
    if target.exists():
        if secrets.compare_digest(target.read_bytes(), token):
            return None
        action = f"{OPERATOR_TOKEN} (refreshed: admin-token.txt changed)"
    else:
        action = f"{OPERATOR_TOKEN} (mode 0600, for --admin-token-file)"
    staging = folder / f".{OPERATOR_TOKEN}.{secrets.token_hex(8)}"
    fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(token)
        os.replace(staging, target)
    finally:
        staging.unlink(missing_ok=True)
    return action


def ensure_secrets(project: Path, *, local_token: bool = False) -> list[str]:
    folder = project / "secrets"
    # Checked before anything is created under it, since that follows the path.
    #
    # Threat model: `secrets/` is 0700, so only its owner and root can change what is in it.
    # Two same-UID races remain and are accepted: swapping `secrets/` for a symlink between
    # this check and the descriptor open in ensure_operator_token, and swapping the staging
    # name before `os.replace` resolves it again. A process that can do either already runs
    # as the owner and can read admin-token.txt directly, so neither race gives it anything
    # new, and the 0700 directory keeps other users out of both.
    if folder.is_symlink():
        raise SetupError("secrets/ is a symlink or not a directory; move it aside and rerun")
    _secrets_dir(folder)
    created = []
    if write_secret(folder / "admin-token.txt", (secrets.token_urlsafe(32) + "\n").encode("ascii")):
        created.append("admin-token.txt")
    operator = ensure_operator_token(folder)
    if operator:
        created.append(operator)
    if write_secret(folder / "findings-key", secrets.token_bytes(32)):
        created.append("findings-key")
    # The front's auth key comes from the private network's admin console. An
    # empty file keeps Compose valid; the front then waits for an interactive login.
    if write_secret(folder / "front-authkey", b""):
        created.append("front-authkey (empty: put the private network's auth key here before the first start)")
    # The hub sends a bearer token to every embedder endpoint and needs a token file
    # for it. The recipe's embedder ignores it, and an authenticating front would check it.
    # Setup makes this one and it only ever goes to the recipe's own embedder, so unlike
    # the remote token it never changes with the endpoint and is created once.
    local = EMBED_TOKEN_FILES["local"]
    if local_token:
        if write_secret(folder / local, (secrets.token_urlsafe(32) + "\n").encode("ascii")):
            created.append(local)
        elif _reset_container_mode(folder, local):
            created.append(f"{local} (mode reset to 0444)")
    return created


def _reset_container_mode(folder: Path, name: str) -> bool:
    """Reset secrets/`name` to 0444 if it has another mode. Returns whether it changed.

    Setup before #2469 wrote the local embedder token 0400, which the hub
    container's UID cannot read through the bind mount. The mode changes through
    a descriptor opened without following a symlink, and a symlink is refused.
    """
    if os.name != "posix":
        return False
    dir_fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        fd = _open_regular(name, dir_fd)
        if fd is None:
            return False
        try:
            if stat.S_IMODE(os.fstat(fd).st_mode) == 0o444:
                return False
            os.fchmod(fd, 0o444)
            return True
        finally:
            os.close(fd)
    finally:
        os.close(dir_fd)


_WINDOWS = os.name == "nt"
REMOTE_TOKEN = EMBED_TOKEN_FILES["remote"]


def _writable_for(path: Path) -> int | None:
    """On Windows, clear the read-only attribute of `path` if it is set, which a rename over it or a delete needs.

    Returns the original mode when it changed something, so a caller whose
    rename or delete then fails can put the attribute back, and None otherwise.
    """
    if not _WINDOWS:
        return None
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode) or info.st_mode & stat.S_IWRITE:
        return None
    os.chmod(path, stat.S_IMODE(info.st_mode) | stat.S_IWRITE)
    return stat.S_IMODE(info.st_mode)


def _remove_file(path: Path) -> None:
    """Delete `path` if it is there."""
    original = _writable_for(path)
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except BaseException:
        if original is not None:
            with contextlib.suppress(OSError):
                os.chmod(path, original)
        raise


def _put_file(path: Path, data: bytes, mode: int) -> None:
    """Write `data` beside `path` with `mode` and rename it over `path`. A symlink at `path` is replaced, never followed."""
    staging = path.with_name(f".{path.name}.{secrets.token_hex(8)}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(staging, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(staging, mode)
        original = _writable_for(path)
        try:
            os.replace(staging, path)
        except BaseException:
            if original is not None:
                with contextlib.suppress(OSError):
                    os.chmod(path, original)
            raise
    finally:
        _remove_file(staging)


def _check_dir(path: Path, label: str) -> int | None:
    """The mode of a directory setup manages, None when it is absent. A symlink or other file there is refused."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(info.st_mode):
        raise SetupError(f"{label} is a symlink or not a directory; move it aside and rerun")
    return stat.S_IMODE(info.st_mode)


class SetupFiles:
    """Every file and directory `init` and `switch` write, as they were before either one started.

    Threat model (#2468): setup runs as the operator on their own host, and
    same-UID races are accepted residuals. The one invariant setup holds through
    any failure, a failure inside the rollback, or process death: secrets/embed-token
    is never on disk next to a config naming a different endpoint than the one
    that token was probed against. A missing token is the safe state: the hub
    then fails closed with embedder-refused and answers in BM25 mode. There is
    no durable journal, so the invariant comes from ordering:

    - Forward (write_install): (a) remove the remote token unless this run
      leaves the endpoint and the token exactly as they are, (b) write
      config/hub.toml and the other generated files, (c) install the new probed
      token last. Process death at any point leaves no token or the matching pair.
    - Rollback (restore): remove the remote token first. If it cannot be
      removed, restore returns early and keeps this run's files in place, since
      that token matches them and putting the old files back would pair it with
      the old endpoint. Otherwise restore every other file and directory, and
      put the old token back only if all of that succeeded. Any later failure
      or BaseException in the rollback leaves no remote token, and setup says
      it must be rerun.

    The local token needs no such rule: it is only ever paired with the recipe's
    own embedder, whose endpoint never changes, and a remote config mounts the
    remote token instead.

    A file that is a symlink or not a regular file, and a managed directory that
    is a symlink or not a directory, is refused before anything is written.
    """

    def __init__(self, project: Path, backup_dir: Path | None = None) -> None:
        self.project = project
        folder = project / "secrets"
        dirs = [project / "config", folder] + ([backup_dir] if backup_dir is not None else [])
        self.dirs = {path: _check_dir(path, self._label(path) + "/") for path in dirs}
        names = (*SECRET_FILES, OPERATOR_TOKEN, *EMBED_TOKEN_FILES.values())
        files = [project / "config" / "hub.toml", project / ".env", project / "compose.override.yaml"]
        files += [folder / name for name in names]
        if backup_dir is not None:
            files.append(backup_dir / "recovery-ledger.jsonl")
        self.files = {path: self._read(path) for path in files}
        self.token = folder / REMOTE_TOKEN

    def _label(self, path: Path) -> str:
        try:
            return path.relative_to(self.project).as_posix()
        except ValueError:
            return str(path)

    def _read(self, path: Path) -> tuple[bytes, int] | None:
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode):
            raise SetupError(f"{self._label(path)} is a symlink; move it aside and rerun")
        if not stat.S_ISREG(info.st_mode):
            raise SetupError(f"{self._label(path)} is not a regular file; move it aside and rerun")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        fd = os.open(path, flags)
        try:
            return _read_fd(fd), stat.S_IMODE(info.st_mode)
        finally:
            os.close(fd)

    def restore(self) -> None:
        """Put everything back, the remote token last and only if everything else went back.

        Returns early, restoring nothing, when the remote token cannot be removed:
        the rollback then keeps this run's files in place, because that token
        matches them and not the files from before. Reports names, never contents.
        A BaseException raised while restoring one path does not stop the others,
        and is raised again at the end.
        """
        left: list[str] = []
        interrupt: BaseException | None = None

        def attempt(label: str, action: Callable[[], None]) -> bool:
            nonlocal interrupt
            try:
                action()
                return True
            except BaseException as exc:
                left.append(label)
                if not isinstance(exc, Exception) and interrupt is None:
                    interrupt = exc
                return False

        # First, so that nothing below can leave a token beside a config it does not match.
        token_gone = attempt(self._label(self.token), lambda: _remove_file(self.token))
        if not token_gone:
            # A token that cannot be removed is the one this run wrote or left alone, so it matches the
            # files on disk now. Putting the old files back would pair it with the old endpoint.
            print(
                f"error: could not remove {self._label(self.token)}, so setup left this run's files in place "
                "to keep that token beside the endpoint it was probed against. Fix the error, then rerun "
                "bin/hub-setup.py",
                file=sys.stderr,
            )
            if interrupt is not None:
                raise interrupt
            return
        for path, saved in self.files.items():
            if path == self.token:
                continue
            if saved is None:
                attempt(self._label(path), lambda path=path: _remove_file(path))
            else:
                attempt(self._label(path), lambda path=path, saved=saved: _put_file(path, *saved))
        for path, mode in self.dirs.items():
            if mode is None:
                attempt(self._label(path), lambda path=path: _remove_created_dir(path))
            else:
                attempt(self._label(path), lambda path=path, mode=mode: _restore_dir_mode(path, mode))
        old_token = self.files.get(self.token)
        if not left and old_token is not None:
            attempt(self._label(self.token), lambda: _put_file(self.token, *old_token))
        if left:
            print(
                f"error: could not put back {', '.join(left)}. No remote embedder token is left on disk, so the hub "
                "answers in BM25 mode until bin/hub-setup.py is rerun.",
                file=sys.stderr,
            )
        if interrupt is not None:
            raise interrupt


def _restore_dir_mode(path: Path, mode: int) -> None:
    """Put back a directory's mode only if this run changed it, so a folder setup refused is never chmodded."""
    if _check_dir(path, str(path)) != mode:
        os.chmod(path, mode)


def _remove_created_dir(path: Path) -> None:
    """Remove a directory this run created, once it is empty. A symlink or file there is refused, never followed."""
    if _check_dir(path, str(path)) is not None:
        path.rmdir()


@contextlib.contextmanager
def all_or_nothing(project: Path, backup_dir: Path | None = None) -> Iterator[None]:
    """Run setup's writes so that a failure at any step puts every file back (SetupFiles has the threat model)."""
    saved = SetupFiles(project, backup_dir)
    try:
        yield
    except BaseException:
        saved.restore()
        raise


def _drop_remote_token(project: Path, decision: Decision) -> bool:
    """Forward step (a): remove secrets/embed-token unless this run leaves the endpoint and the token as they are.

    Then nothing written after this can sit beside a token probed against
    another endpoint, and leaving the remote backend leaves no token behind.
    Returns whether it removed one.
    """
    target = project / "secrets" / REMOTE_TOKEN
    if not os.path.lexists(target):
        return False
    config = project / "config" / "hub.toml"
    if decision.backend == "remote" and decision.probed_token is not None and config.is_file():
        data = (decision.probed_token + "\n").encode("utf-8")
        same_config = config.read_text(encoding="utf-8") == hub_toml(decision)
        if same_config and target.is_file() and secrets.compare_digest(target.read_bytes(), data):
            return False
    _remove_file(target)
    return True


def install_probed_token(project: Path, decision: Decision) -> str | None:
    """Forward step (c): install the token the remote probe passed as secrets/embed-token, mode 0444.

    Runs after config/hub.toml names the endpoint it was probed against. Unlike
    the other secrets it is replaced, never kept: a new endpoint comes with its
    own token. Returns what it did.
    """
    if decision.backend != "remote" or decision.probed_token is None:
        return None
    folder = project / "secrets"
    target = folder / REMOTE_TOKEN
    data = (decision.probed_token + "\n").encode("utf-8")
    # Checked again at the last step, right before the 0444 file is written.
    _require_private(folder)
    if os.path.lexists(target) and (target.is_symlink() or not target.is_file()):
        raise SetupError(f"secrets/{REMOTE_TOKEN} is a symlink or not a regular file; move it aside and rerun")
    old = target.read_bytes() if target.exists() else None
    if old is not None and secrets.compare_digest(old, data) and stat.S_IMODE(target.stat().st_mode) == 0o444:
        return None
    # 0444 inside the 0700 directory, like the other mounted secrets: the hub container's
    # UID must read the bind mount. hub/entry.py makes the hub-owned 0400 copy the hub reads.
    _put_file(target, data, 0o444)
    return REMOTE_TOKEN


def write_install(
    project: Path,
    decision: Decision,
    env_updates: dict[str, str],
    *,
    with_secrets: bool,
    backup_dir: Path | None = None,
) -> list[str]:
    """Everything init and switch write, in the order SetupFiles describes, as one transaction.

    Returns what changed under secrets/, for the report.
    """
    with all_or_nothing(project, backup_dir):
        created = ensure_secrets(project, local_token=decision.backend == "local") if with_secrets else []
        if backup_dir is not None:
            _private_dir(backup_dir)
            # `brigade fleet restore` counts only a ledger file that exists, so a lost
            # ledger is never mistaken for a store that never saw a restore.
            ledger = backup_dir / "recovery-ledger.jsonl"
            if not ledger.exists():
                os.close(os.open(ledger, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        dropped = _drop_remote_token(project, decision)
        apply(project, decision, env_updates)
        installed = install_probed_token(project, decision)
    if installed:
        created.append(f"{installed} (replaced)" if dropped else installed)
    elif dropped:
        created.append(f"{REMOTE_TOKEN} removed (no remote endpoint is configured)")
    return created


def _compose_override(project: Path, decision: Decision) -> None:
    """Mount the embedder token only in hybrid mode, so BM25 mode needs no such file."""
    override = project / "compose.override.yaml"
    if decision.retrieval == "hybrid":
        override.write_text(
            f"# Written by bin/hub-setup.py for the {decision.backend} embedder backend.\n"
            "services:\n  hub:\n    secrets:\n      - admin_token\n      - findings_key\n      - embed_token\n"
            f"secrets:\n  embed_token:\n    file: ./secrets/{EMBED_TOKEN_FILES[str(decision.backend)]}\n",
            encoding="utf-8",
        )
    elif override.exists() and override.read_text(encoding="utf-8").startswith("# Written by bin/hub-setup.py"):
        override.unlink()


def apply(project: Path, decision: Decision, env_updates: dict[str, str]) -> None:
    (project / "config").mkdir(exist_ok=True)
    (project / "config" / "hub.toml").write_text(hub_toml(decision), encoding="utf-8")
    env = read_env(project / ".env")
    env.update(env_updates)
    env["COMPOSE_PROFILES"] = ",".join(decision.profiles)
    env["BRIGADE_RETRIEVAL"] = decision.retrieval
    env["BRIGADE_EMBED_BACKEND"] = decision.backend or "none"
    if decision.model:
        env["BRIGADE_EMBED_MODEL"] = decision.model
    else:
        env.pop("BRIGADE_EMBED_MODEL", None)
    write_env(project / ".env", env)
    _compose_override(project, decision)


def report(decision: Decision) -> str:
    if decision.retrieval == "hybrid":
        where = "the recipe's embedder service on the local GPU" if decision.backend == "local" else decision.endpoint
        lines = [f"retrieval: hybrid, backend {decision.backend}, model {decision.model}, via {where}"]
    else:
        lines = [
            "retrieval: BM25 mode (lexical). No embedder is configured, and the embedder container is never created.",
            "This is a supported configuration. Search answers from BM25 alone.",
        ]
    if decision.failed:
        lines.append("checks that did not pass:")
        lines += [f"  - {item}" for item in decision.failed]
    if decision.retrieval == "lexical":
        lines.append(
            "to switch later: bin/hub-setup.py switch --to hybrid (with --remote-url and --remote-token-file for a "
            "remote endpoint), then follow 'Switch retrieval modes' in docs/runbooks/fleet-hub-dogfood.md"
        )
    return "\n".join(lines)


def _base_env(args: argparse.Namespace) -> dict[str, str]:
    backup_dir = Path(args.backup_dir).expanduser().resolve()
    # The recipe targets Linux hosts; on other platforms name the owner explicitly.
    owner = args.backup_owner or (f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") else "0:0")
    values = {
        "COMPOSE_PROJECT_NAME": args.project_name,
        "BRIGADE_HUB_IMAGE": args.hub_image,
        "BRIGADE_FRONT_HOSTNAME": args.front_hostname,
        "BRIGADE_BACKUP_DIR": str(backup_dir),
        "BRIGADE_BACKUP_OWNER": owner,
        "BRIGADE_BACKUP_KEEP_HOURLY": str(args.keep_hourly),
        "BRIGADE_BACKUP_MIN_FREE_GIB": str(args.min_free_gib),
    }
    if args.brigade_ref:
        if not args.brigade_sha256:
            raise SetupError("--brigade-ref needs --brigade-sha256, the reviewed source archive digest")
        # The image label carries it; without it Compose labels any ref with its default.
        if not args.brigade_version or not re.fullmatch(r"[0-9]+(\.[0-9]+)*[0-9A-Za-z.+-]*", args.brigade_version):
            raise SetupError("--brigade-ref needs --brigade-version, the version in that commit's pyproject.toml")
        values["BRIGADE_REF"] = args.brigade_ref
        values["BRIGADE_SHA256"] = args.brigade_sha256
        values["BRIGADE_VERSION"] = args.brigade_version
    elif args.brigade_sha256 or args.brigade_version:
        raise SetupError("--brigade-sha256 and --brigade-version describe --brigade-ref; pass all three")
    return values


def _decide_from_args(args: argparse.Namespace, run: Runner, post: Poster) -> Decision:
    models = load_r1(Path(args.r1_file))
    token = Path(args.remote_token_file).expanduser() if args.remote_token_file else None
    return decide(
        run=run,
        post=post,
        models=models,
        embed_image=args.embed_image,
        remote_url=args.remote_url,
        remote_token=token,
        remote_api=args.remote_api,
        skip_gpu=args.no_gpu,
    )


def cmd_init(args: argparse.Namespace, run: Runner, post: Poster) -> int:
    project = Path(args.project_dir).resolve()
    if not (project / "compose.yaml").is_file():
        raise SetupError(f"{project} has no compose.yaml; point --project-dir at a copy of ops/fleet-hub")
    env_updates = _base_env(args)
    # The directory as given: _base_env resolves it, which would follow a symlink there.
    given = Path(args.backup_dir).expanduser().absolute()
    _check_dir(given, f"backup directory {given}")
    decision = _decide_from_args(args, run, post)
    backup_dir = Path(env_updates["BRIGADE_BACKUP_DIR"])
    created = write_install(project, decision, env_updates, with_secrets=True, backup_dir=backup_dir)
    print(report(decision))
    if created:
        print("secrets written: " + ", ".join(created))
    print(f"backup directory: {backup_dir} (mode 0700)")
    return 0


def cmd_switch(args: argparse.Namespace, run: Runner, post: Poster) -> int:
    project = Path(args.project_dir).resolve()
    env = read_env(project / ".env")
    if not env:
        raise SetupError("no .env: run init first")
    if args.to == "lexical":
        decision = Decision(retrieval="lexical")
    else:
        decision = _decide_from_args(args, run, post)
        if decision.retrieval != "hybrid":
            print(report(decision))
            raise SetupError("refusing to write hybrid: no embedder passed its probe, so the mode is unchanged")
    changed = write_install(project, decision, {}, with_secrets=decision.retrieval == "hybrid")
    print(report(decision))
    if changed:
        print("secrets written: " + ", ".join(changed))
    if args.to == "lexical":
        print(
            "next: docker compose --profile gpu rm -sf embedder, then docker compose up -d (runbook: Switch retrieval modes)"
        )
    else:
        print("next: docker compose up -d (runbook: Switch retrieval modes)")
    return 0


def render(template: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        template = template.replace(f"@{key}@", value)
    if "@" in "".join(line for line in template.splitlines() if not line.lstrip().startswith("#")):
        leftover = sorted({part.split("@")[0] for part in template.split("@")[1::2]})
        raise SetupError(f"unrendered placeholders: {', '.join(leftover)}")
    return template


def cmd_units(args: argparse.Namespace, run: Runner, post: Poster) -> int:
    project = Path(args.project_dir).resolve()
    out = Path(args.out).resolve()
    values = {
        "COMPOSE_USER": args.compose_user,
        "PROJECT_DIR": str(project),
        "DOCKER": args.docker or shutil.which("docker") or "/usr/bin/docker",
        "HUB_CPUS": args.hub_cpus,
        "EMBED_CPUS": args.embed_cpus,
        "AGENT_CPUS": args.agent_cpus,
        "DB_DEVICE": args.db_device,
        "IO_LATENCY": args.io_latency,
    }
    written = []
    for source, target in UNIT_TEMPLATES:
        text = render((RECIPE / "systemd" / source).read_text(encoding="utf-8"), values)
        path = out / target
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        written.append(str(path))
    print("rendered for review (nothing installed):")
    print("\n".join(f"  {item}" for item in written))
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--project-dir", default=str(RECIPE), help="Compose project directory (default: this recipe).")
    sub = p.add_subparsers(dest="command", required=True)

    def mode_args(q: argparse.ArgumentParser) -> None:
        q.add_argument("--r1-file", default=str(RECIPE / "r1-models.json"))
        q.add_argument("--embed-image", default=DEFAULT_EMBED_IMAGE, help="Image the GPU probe container runs.")
        q.add_argument("--no-gpu", action="store_true", help="Skip the local GPU checks.")
        q.add_argument("--remote-url", default=None, help="Remote embedding endpoint base URL.")
        q.add_argument("--remote-token-file", default=None, help="Bearer token file for the remote endpoint.")
        q.add_argument("--remote-api", choices=("api/embed", "v1/embeddings"), default="api/embed")

    init = sub.add_parser("init", help="Pick the retrieval mode and write .env, config/hub.toml and secrets.")
    mode_args(init)
    init.add_argument("--project-name", default="brigade-hub")
    init.add_argument("--hub-image", default="localhost/brigade-fleet-hub:dogfood")
    init.add_argument("--front-hostname", default="brigade-hub-dogfood")
    init.add_argument("--backup-dir", default=str(Path.home() / "brigade-hub-backups"))
    init.add_argument("--backup-owner", default=None, help="UID:GID the copies belong to (default: this account).")
    init.add_argument("--keep-hourly", type=int, default=48, help="From `brigade fleet backup --plan`.")
    init.add_argument("--min-free-gib", type=float, default=20)
    init.add_argument("--brigade-ref", default=None, help="Reviewed Brigade commit the image installs.")
    init.add_argument("--brigade-sha256", default=None, help="SHA-256 of that commit's source archive.")
    init.add_argument("--brigade-version", default=None, help="Version in that commit's pyproject.toml.")
    init.set_defaults(func=cmd_init)

    switch = sub.add_parser("switch", help="Switch the retrieval mode (runbook: Switch retrieval modes).")
    switch.add_argument("--to", choices=("lexical", "hybrid"), required=True)
    mode_args(switch)
    switch.set_defaults(func=cmd_switch)

    units = sub.add_parser("units", help="Render the systemd templates into a directory for review.")
    units.add_argument("--out", required=True)
    units.add_argument("--compose-user", required=True)
    units.add_argument("--hub-cpus", required=True, help="Two performance cores for the hub slice, e.g. 6-7.")
    units.add_argument("--embed-cpus", required=True, help="One performance core for the embedder slice.")
    units.add_argument("--agent-cpus", required=True, help="Every other core, for user.slice and agent services.")
    units.add_argument(
        "--db-device", required=True, help="Block device that holds Docker's data root, e.g. /dev/nvme0n1."
    )
    units.add_argument("--io-latency", default="10ms")
    units.add_argument("--docker", default=None)
    units.set_defaults(func=cmd_units)
    return p


def main(argv: list[str] | None = None, *, run: Runner = run_command, post: Poster = post_json) -> int:
    args = parser().parse_args(argv)
    try:
        return int(args.func(args, run, post))
    except (SetupError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
