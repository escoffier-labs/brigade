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

It never writes a hybrid configuration it could not probe and never overwrites
an existing secret. The one derived file, the operator's mode-0600 copy of the
admin token, follows the container copy. `switch` changes the mode either way,
and `units` renders the systemd templates for review. Nothing here installs a
package, a unit or a container. Stdlib only.
"""

from __future__ import annotations

import argparse
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
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

RECIPE = Path(__file__).resolve().parents[1]
DEFAULT_EMBED_IMAGE = "ollama/ollama:0.18.0@sha256:e23e6499890325d31f3454cdc83f7a613a2423c8a9e167bbafea0b0e3c27d7c6"
LOCAL_ENDPOINT = "http://embedder:11434"
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
    endpoint: str | None = None
    api: str | None = None
    token_file: str | None = None
    model_digest: str | None = None
    profiles: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


def load_r1(path: Path) -> list[dict[str, Any]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    return [model for model in document.get("models", []) if model.get("r1") == "passed"]


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
    failures = []
    for model in models:
        if api == "v1/embeddings":
            body: dict[str, Any] = {"model": model["name"], "input": [PROBE_TEXT]}
        else:
            body = {"model": model["name"], "input": PROBE_TEXT}
        try:
            status, headers, payload = post(url.rstrip("/") + "/" + api, body, {"Authorization": f"Bearer {token}"})
        except (OSError, ValueError) as exc:
            failures.append(f"remote: probe of {model['name']} failed: {exc}")
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
        digest = headers.get("x-embedder-model-digest") or payload.get("model_digest")
        if not digest:
            failures.append("remote: the endpoint reports no model digest, which the contract requires")
            continue
        return (
            Decision(
                retrieval="hybrid",
                backend="remote",
                model=model["name"],
                dimensions=int(model["dimensions"]),
                endpoint=url,
                api=api,
                token_file="/run/secrets/embed_token",
                model_digest=str(digest),
            ),
            [],
        )
    return None, failures


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
            return Decision(
                retrieval="hybrid",
                backend="local",
                model=model["name"],
                dimensions=int(model["dimensions"]),
                endpoint=LOCAL_ENDPOINT,
                api="api/embed",
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


def hub_toml(decision: Decision) -> str:
    lines = [
        "# Written by bin/hub-setup.py. Read by the planned Rust hub (S10, S15); today's Python hub ignores it.",
        "[memory]",
        f'retrieval = "{decision.retrieval}"',
    ]
    if decision.retrieval == "hybrid":
        lines += [
            "",
            "[memory.embedder]",
            f'backend = "{decision.backend}"',
            f'endpoint = "{decision.endpoint}"',
            f'api = "{decision.api}"',
            f'model = "{decision.model}"',
            f"dimensions = {decision.dimensions}",
        ]
        if decision.token_file:
            lines.append(f'token_file = "{decision.token_file}"')
        if decision.model_digest:
            lines.append(f'model_digest = "{decision.model_digest}"')
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


def write_secret(path: Path, data: bytes) -> bool:
    """Create a secret once. Mode 0444 inside a 0700 directory: the container's own UID must read the bind mount."""
    if path.exists():
        return False
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    os.chmod(path, 0o444)
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
    if not stat.S_ISREG(os.fstat(fd).st_mode):
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
    try:
        with os.fdopen(fd, "wb") as handle:
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


def ensure_secrets(project: Path, *, remote_token: Path | None) -> list[str]:
    folder = project / "secrets"
    # Checked before anything is created or chmodded under it, since both follow the path.
    if folder.is_symlink():
        raise SetupError("secrets/ is a symlink or not a directory; move it aside and rerun")
    _private_dir(folder)
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
    if remote_token is not None and write_secret(folder / "embed-token", remote_token.read_bytes()):
        created.append("embed-token")
    return created


def _compose_override(project: Path, decision: Decision) -> None:
    """Mount the remote token only for the remote backend, so other modes need no such file."""
    override = project / "compose.override.yaml"
    if decision.backend == "remote":
        override.write_text(
            "# Written by bin/hub-setup.py for the remote embedder backend.\n"
            "services:\n  hub:\n    secrets:\n      - admin_token\n      - findings_key\n      - embed_token\n"
            "secrets:\n  embed_token:\n    file: ./secrets/embed-token\n",
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
    decision = _decide_from_args(args, run, post)
    token = Path(args.remote_token_file).expanduser() if decision.backend == "remote" else None
    created = ensure_secrets(project, remote_token=token)
    backup_dir = Path(env_updates["BRIGADE_BACKUP_DIR"])
    _private_dir(backup_dir)
    # `brigade fleet restore` counts only a ledger file that exists, so a lost
    # ledger is never mistaken for a store that never saw a restore.
    ledger = backup_dir / "recovery-ledger.jsonl"
    if not ledger.exists():
        os.close(os.open(ledger, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    apply(project, decision, env_updates)
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
        token = Path(args.remote_token_file).expanduser() if decision.backend == "remote" else None
        ensure_secrets(project, remote_token=token)
    apply(project, decision, {})
    print(report(decision))
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
