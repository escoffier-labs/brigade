#!/usr/bin/env python3
"""Entrypoint for the dogfood hub container: `brigade-hub` in front of the Python hub.

When the image's Brigade has the Rust engine (S10) and the image recorded the
binary's checksum, this execs `brigade fleet serve --engine rust`, which checks
the binary, supervises it and runs the watchdog. The recipe's default
BRIGADE_REF predates that engine, so otherwise it falls back to the S31
wrapper below. Bump the pin past S10 at the next release and the fallback
goes unused.

`brigade-hub` runs in managed mode. It starts `python3 -m brigade fleet serve`
as its child on an ephemeral loopback port, gives it a per-start key, and
exits non-zero if that child dies. This wrapper is the interim stand-in for the
planned `fleet serve --engine rust` (S10): it starts `brigade-hub` as its only
child, forwards SIGTERM and SIGINT to it, and writes the two lines the S31
checks read from the container log, which the Python hub wrote before the Rust
front took the port:

- the listening line, once `/health` answers on the front's own port;
- the clean-exit line, when `brigade-hub` exits 0 after a stop signal.
  `brigade-hub` exits 0 only when both hubs shut down cleanly: its drain of at
  most 25 s ended, and the Python upstream, sent SIGTERM after it, ran its own
  shutdown and exited 0 within 10 s. An upstream it had to kill, an upstream
  that exited non-zero, or a stop that came during startup makes it exit 2,
  and its log says which.

Any other exit passes through unchanged, so the restart policy sees a crash as
a crash.

Before either hub starts, two steps make setup's hybrid configuration usable:

- The embedder token. Compose bind-mounts the operator's secrets/ file, which
  keeps the operator's ownership, and the hub accepts only a token file its own
  user owns with mode 0400 or 0600. `deliver_embed_token` copies the mount into
  a hub-owned 0400 file on the container's private tmpfs, which is the path
  setup writes into hub.toml. A symlink at the mount is refused. With no mount
  (BM25 mode) nothing is copied. A mount that cannot be copied stops the start
  only when the memory config is hybrid and its embedder reads that copy
  (`embed_token_use`). Otherwise the hub starts with a warning and no copy.
- `--memory-config`. compose.yaml names setup's hub.toml. An image whose
  Brigade predates that flag would refuse it, so `supported_argv` drops it
  there and says so. Such a hub runs on `hub.toml` beside its database, which
  in this recipe means BM25 mode.

Stdlib only.
"""

from __future__ import annotations

import errno
import importlib.util
import inspect
import os
import secrets
import signal
import stat
import subprocess
import sys
import tomllib
import urllib.request

HUB = os.environ.get("BRIGADE_HUB_BIN", "brigade-hub")
# Where the dogfood stage installs brigade-hub and the checksum it recorded.
ENGINE_BINARY = "/usr/local/bin/brigade-hub"
ENGINE_SUM = "/usr/local/share/brigade/brigade-hub.sha256"
# Where Compose mounts the embedder token, and the hub-owned copy the hub reads
# (bin/hub-setup.py, EMBED_TOKEN_MOUNT and EMBED_TOKEN_COPY).
EMBED_TOKEN_MOUNT = "/run/secrets/embed_token"
EMBED_TOKEN_COPY = "/run/brigade/embed_token"
EMBED_TOKEN_BYTES = 8192
STARTED = "brigade fleet hub listening on"
STOPPED = "brigade fleet hub stopped cleanly"
POLL_SECONDS = 0.25


def option(argv: list[str], name: str, default: str) -> str:
    """The last value of `--name VALUE` or `--name=VALUE` in argv."""
    value = default
    for index, item in enumerate(argv):
        if item == name and index + 1 < len(argv):
            value = argv[index + 1]
        elif item.startswith(name + "="):
            value = item.split("=", 1)[1]
    return value


def route_modes(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, item in enumerate(argv[:-1]) if item == "--route-mode"] + [
        item.split("=", 1)[1] for item in argv if item.startswith("--route-mode=")
    ]


def healthy(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=1) as response:
            return response.status == 200
    except OSError:
        return False


def exit_code(returncode: int) -> int:
    # A child killed by signal N reports -N; a shell reports 128 + N.
    return 128 - returncode if returncode < 0 else returncode


def deliver_embed_token(source: str = EMBED_TOKEN_MOUNT, target: str = EMBED_TOKEN_COPY) -> str | None:
    """Copy the mounted embedder token to a file this user owns with mode 0400. Returns what went wrong, or None.

    The source is opened without following a symlink and must be a regular
    file. The copy is written under a new name in a directory only this user can
    write and renamed over the target, so the target is never a partial file and
    a symlink planted at the target is replaced, never followed. With no mounted
    token any earlier copy is removed. Never prints the token.
    """
    if os.name != "posix":
        # The container is Linux. Elsewhere there is no mount, owner or mode to act on.
        return None
    folder = os.path.dirname(target)
    try:
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return remove_embed_token_copy(target)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            return f"embedder token {source} is a symlink; setup writes a regular file"
        return f"cannot read embedder token {source}: {exc.strerror}; rerun bin/hub-setup.py, which writes it mode 0444"
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > EMBED_TOKEN_BYTES:
            return f"embedder token {source} is not a regular file of at most {EMBED_TOKEN_BYTES} bytes"
        token = os.read(fd, EMBED_TOKEN_BYTES + 1)
    finally:
        os.close(fd)
    try:
        held = os.lstat(folder)
    except OSError:
        return f"{folder} is missing; compose.yaml mounts it as a tmpfs owned by the hub's user"
    if not stat.S_ISDIR(held.st_mode) or held.st_uid != os.geteuid() or stat.S_IMODE(held.st_mode) & 0o077:
        return f"{folder} must be a directory owned by the hub's user with mode 0700"
    staging = os.path.join(folder, f".embed_token.{secrets.token_hex(8)}")
    try:
        out = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o400)
        try:
            # The umask cannot widen 0400, and fchmod keeps it exact.
            os.fchmod(out, 0o400)
            os.write(out, token)
        finally:
            os.close(out)
        os.replace(staging, target)
    except OSError as exc:
        return f"cannot write the embedder token copy {target}: {exc.strerror}"
    finally:
        try:
            os.unlink(staging)
        except OSError:
            pass
    return None


def remove_embed_token_copy(target: str = EMBED_TOKEN_COPY) -> str | None:
    """Remove the hub's copy of the embedder token, if there is one. Returns what went wrong, or None."""
    try:
        os.unlink(target)
    except (FileNotFoundError, NotADirectoryError):
        pass
    except OSError as exc:
        return f"cannot remove the earlier embedder token copy {target}: {exc.strerror}"
    return None


def memory_config_path(argv: list[str]) -> str:
    """The hub.toml this hub will read: the one `--memory-config` names, else the one beside its database."""
    named = option(argv, "--memory-config", "")
    if named:
        return named
    db = os.path.expanduser(option(argv, "--db", "~/.brigade/fleet-hub.db"))
    return os.path.join(os.path.dirname(db), "hub.toml")


def embed_token_use(config: str, copy: str = EMBED_TOKEN_COPY) -> tuple[bool, str]:
    """Whether the hub started on `config` reads the token copy, and a clause saying why.

    Only a hybrid config whose embedder posts to an endpoint with `token_file`
    naming the copy needs it. A config that is absent, unreadable or not valid
    TOML counts as not using it: the hub reads the same file next and reports
    that itself, and a named config it refuses stops it.
    """
    try:
        with open(config, "rb") as handle:
            memory = tomllib.load(handle).get("memory")
    except FileNotFoundError:
        return False, f"there is no memory config at {config}, which means BM25 mode"
    except (OSError, ValueError) as exc:
        return (
            False,
            f"the memory config {config} cannot be read here ({type(exc).__name__}) and the hub checks it next",
        )
    if not isinstance(memory, dict) or memory.get("retrieval") != "hybrid":
        return False, f"the memory config {config} does not select hybrid retrieval"
    embedder = memory.get("embedder")
    embedder = embedder if isinstance(embedder, dict) else {}
    token_file = embedder.get("token_file")
    posts = bool(embedder.get("query_endpoint") or embedder.get("endpoint"))
    if posts and isinstance(token_file, str) and os.path.normpath(token_file) == os.path.normpath(copy):
        backend = embedder.get("backend")
        return True, f"the memory config {config} selects hybrid retrieval and its {backend} embedder reads {copy}"
    return False, f"the embedder in the memory config {config} does not read {copy}"


def engine_supports_memory_config() -> bool:
    """Whether this image's `fleet serve --engine rust` passes `--memory-config` to brigade-hub."""
    try:
        from brigade import fleet_hub_engine
    except Exception:  # noqa: BLE001 - any import failure means an image without it
        return False
    return "memory_config" in inspect.signature(fleet_hub_engine.hub_argv).parameters


def supported_argv(argv: list[str], supported: bool) -> list[str]:
    """`argv` as given, or without `--memory-config` for an image that predates it, with a line saying so."""
    if supported or not any(item == "--memory-config" or item.startswith("--memory-config=") for item in argv):
        return argv
    kept: list[str] = []
    skip = False
    for item in argv:
        if skip:
            skip = False
        elif item == "--memory-config":
            skip = True
        elif not item.startswith("--memory-config="):
            kept.append(item)
    print(
        "warning: this image's Brigade predates --memory-config, so the hub reads hub.toml beside its "
        "database and runs in BM25 mode. Build with a BRIGADE_REF that has it to use setup's config/hub.toml.",
        file=sys.stderr,
        flush=True,
    )
    return kept


def engine_command(argv: list[str]) -> list[str] | None:
    """`fleet serve --engine rust` with these flags, when this image supports it."""
    if not os.path.isfile(ENGINE_SUM):
        return None
    try:
        if importlib.util.find_spec("brigade.fleet_hub_engine") is None:
            return None
    except ModuleNotFoundError:
        return None
    return [
        sys.executable,
        "-m",
        "brigade",
        "fleet",
        "serve",
        "--engine",
        "rust",
        "--hub-binary",
        ENGINE_BINARY,
        "--hub-sha256-file",
        ENGINE_SUM,
        *argv,
    ]


def main(argv: list[str]) -> int:
    # First, so the config looked at below is the one this image's hub will read.
    argv = supported_argv(argv, engine_supports_memory_config())
    problem = deliver_embed_token()
    if problem is not None:
        used, why = embed_token_use(memory_config_path(argv))
        # Whatever went wrong, no copy from an earlier start stays behind for a later config to pick up.
        leftover = None if used else remove_embed_token_copy()
        if used or leftover is not None:
            print(f"error: {leftover or problem} ({why})", file=sys.stderr, flush=True)
            return 2
        print(
            f"warning: {problem}. The hub starts without the embedder token, since {why}. "
            "Rerun bin/hub-setup.py to rewrite the token before switching to hybrid retrieval.",
            file=sys.stderr,
            flush=True,
        )
    command = engine_command(argv)
    if command is not None:
        # Same PID, so the init process's signals reach fleet serve directly.
        os.execv(command[0], command)
    host = option(argv, "--host", "127.0.0.1")
    port = option(argv, "--port", "3774")
    db = option(argv, "--db", "~/.brigade/fleet-hub.db")
    child: subprocess.Popen[bytes] | None = None
    pending: int | None = None
    stopping = False

    def send(signum: int) -> None:
        if child is None:
            return
        try:
            child.send_signal(signum)
        except ProcessLookupError:
            pass

    def forward(signum: int, _frame: object) -> None:
        # Installed before the child exists, so a stop during the spawn is
        # kept and forwarded below instead of killing this wrapper.
        nonlocal stopping, pending
        stopping = True
        if child is None:
            pending = signum
        else:
            send(signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    # The child starts with both blocked (the mask survives fork and exec)
    # and `brigade-hub` unblocks them once its handlers exist, so neither a
    # stop forwarded right after the spawn nor one sent to the process group
    # meets its pre-main default action.
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT})
    try:
        child = subprocess.Popen([HUB, *argv])
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)
    if pending is not None:
        send(pending)
    url = f"http://{'[' + host + ']' if ':' in host else host}:{port}/health"
    while child.poll() is None and not stopping:
        if healthy(url):
            modes = ", ".join(route_modes(argv)) or "defaults"
            print(
                f"{STARTED} {host}:{port} (brigade-hub front, managed Python upstream; db {db}; route modes {modes})",
                flush=True,
            )
            break
        try:
            child.wait(timeout=POLL_SECONDS)
        except subprocess.TimeoutExpired:
            pass
    returncode = child.wait()
    if returncode == 0 and stopping:
        print(STOPPED, flush=True)
    return exit_code(returncode)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
