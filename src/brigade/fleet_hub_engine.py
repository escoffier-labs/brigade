"""`brigade fleet serve --engine rust`: the Rust hub in front of the Python hub (S10).

`brigade-hub` takes the port, starts the Python hub as its managed loopback
upstream and keeps every flag `fleet serve` already has. This module:

1. migrates the database with the Python schema owner (`init_db`), so an
   unknown newer `user_version` stops startup before anything binds;
2. finds the `brigade-hub` binary that `brigade setup --component brigade-hub`
   installed (or one named with `--hub-binary` and `--hub-sha256`) and checks
   its SHA-256 against the recorded checksum before running it;
3. starts `brigade-hub` as its only child, forwards SIGTERM and SIGINT, and
   writes the listening and clean-exit lines the Python hub writes;
4. runs a health watchdog: after `failures` consecutive failed `/health`
   checks, or as many checks that find only loopback in the network namespace
   when that watch is on, it stops `brigade-hub` and exits non-zero so the
   service manager restarts the unit.

If `brigade-hub` exits for any reason, this process exits with its status, so
the service manager sees a crash as a crash. Rollback to the Python engine is
removing `--engine rust`: both engines serve the same database file.
"""

from __future__ import annotations

import hashlib
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import brigade

COMPONENT = "brigade-hub"
STARTED = "brigade fleet hub listening on"
STOPPED = "brigade fleet hub stopped cleanly"
POLL_SECONDS = 0.25
# Watchdog defaults. Each can be overridden through the environment.
WATCHDOG_INTERVAL = 10.0
WATCHDOG_FAILURES = 3
STARTUP_SECONDS = 120.0
# After the watchdog's SIGTERM: brigade-hub's 25 s drain plus 10 s for the
# Python upstream's own shutdown, then SIGKILL.
STOP_GRACE = 40.0
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class EngineError(Exception):
    """A refusal before `brigade-hub` starts."""


@dataclass(frozen=True)
class Binary:
    path: Path
    sha256: str
    source: str


def resolve_binary(
    hub_binary: Path | None,
    hub_sha256: str | None,
    *,
    hub_sha256_file: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Binary:
    """The `brigade-hub` to run and the checksum it must match.

    A container image records the checksum of the binary it built in a file,
    which `--hub-sha256-file` names.
    """
    if hub_sha256 is not None and hub_sha256_file is not None:
        raise EngineError("--hub-sha256 and --hub-sha256-file cannot be used together")
    if hub_sha256_file is not None:
        try:
            hub_sha256 = Path(hub_sha256_file).read_text(encoding="ascii").split()[0]
        except (OSError, UnicodeDecodeError, IndexError) as exc:
            raise EngineError(f"cannot read a SHA-256 from {hub_sha256_file}") from exc
    if (hub_binary is None) != (hub_sha256 is None):
        raise EngineError("--hub-binary needs --hub-sha256 or --hub-sha256-file, and they need --hub-binary")
    if hub_binary is not None and hub_sha256 is not None:
        digest = hub_sha256.strip().lower()
        if not _SHA256.fullmatch(digest):
            raise EngineError("--hub-sha256 must be 64 hexadecimal characters")
        return Binary(Path(hub_binary).expanduser().absolute(), digest, "--hub-binary")
    from . import component_install, component_paths, component_state

    roots = component_install.resolve_roots(env=env)
    state_path = Path(component_paths.installed_state_path(roots.data_root))
    state = component_state.load_installed_state(state_path)
    record = state.components.get(COMPONENT) if state is not None else None
    if state is None or record is None:
        raise EngineError(
            "brigade-hub is not installed; run `brigade setup --component brigade-hub`, "
            "or name a binary with --hub-binary and --hub-sha256"
        )
    if state.brigade_version != brigade.__version__:
        # The Rust hub and its Python upstream ship from one release.
        raise EngineError(
            f"installed brigade-hub is from Brigade {state.brigade_version}, this is {brigade.__version__}; "
            "run `brigade setup --component brigade-hub` again"
        )
    return Binary(Path(record.executable), record.sha256, f"installed state {state_path}")


def verify_binary(binary: Binary) -> None:
    """Refuse a missing, non-executable or altered binary."""
    if not binary.path.is_file():
        raise EngineError(f"brigade-hub binary missing: {binary.path}")
    if not os.access(binary.path, os.X_OK):
        raise EngineError(f"brigade-hub binary is not executable: {binary.path}")
    digest = hashlib.sha256()
    with binary.path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != binary.sha256:
        raise EngineError(f"brigade-hub binary {binary.path} does not match the checksum from {binary.source}")


def hub_argv(
    binary: Path,
    *,
    host: str,
    port: int,
    db_path: Path,
    token_file: Path | None,
    allow_admin_writes: bool,
    require_sealed: bool,
    deck_config_path: Path | None,
    trust_tailscale_identity: bool,
    trust_forwarded_proto: bool,
    route_modes: Sequence[str],
) -> list[str]:
    argv = [str(binary), "--host", host, "--port", str(port), "--db", str(db_path)]
    if token_file is not None:
        argv += ["--token-file", str(token_file)]
    if deck_config_path is not None:
        argv += ["--deck-config", str(deck_config_path)]
    for flag, enabled in (
        ("--allow-admin-writes", allow_admin_writes),
        ("--require-sealed", require_sealed),
        ("--trust-tailscale-identity", trust_tailscale_identity),
        ("--trust-forwarded-proto", trust_forwarded_proto),
    ):
        if enabled:
            argv.append(flag)
    for mode in route_modes:
        argv += ["--route-mode", mode]
    return argv


def health_url(host: str, port: int) -> str:
    """Where the watchdog checks `/health`: the bound address, or loopback for a wildcard bind."""
    probe = {"0.0.0.0": "127.0.0.1", "::": "::1", "": "127.0.0.1"}.get(host, host)
    return f"http://{'[' + probe + ']' if ':' in probe else probe}:{port}/health"


def healthy(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return response.status == 200
    except OSError:
        return False


def only_loopback(net: Path = Path("/sys/class/net")) -> bool:
    """True when the namespace has no interface but loopback (the holder restarted)."""
    try:
        names = [entry.name for entry in net.iterdir()]
    except OSError:
        return False
    return bool(names) and all(name == "lo" for name in names)


def exit_code(returncode: int) -> int:
    # A child killed by signal N reports -N; a shell reports 128 + N.
    return 128 - returncode if returncode < 0 else returncode


def _env_number(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise EngineError(f"{name} must be a number") from exc
    if value <= 0:
        raise EngineError(f"{name} must be positive")
    return value


def run(
    *,
    host: str,
    port: int,
    db_path: Path,
    token_file: Path | None,
    allow_admin_writes: bool,
    require_sealed: bool,
    deck_config_path: Path | None,
    trust_tailscale_identity: bool,
    trust_forwarded_proto: bool,
    route_modes: Sequence[str] = (),
    hub_binary: Path | None = None,
    hub_sha256: str | None = None,
    hub_sha256_file: Path | None = None,
    env: Mapping[str, str] | None = None,
    healthy: Callable[[str], bool] = healthy,
    only_loopback: Callable[[], bool] = only_loopback,
) -> int:
    environ = os.environ if env is None else env
    try:
        if not sys.platform.startswith("linux"):
            raise EngineError("--engine rust runs on Linux only; brigade-hub ships for linux-amd64 and linux-arm64")
        interval = _env_number(environ, "BRIGADE_HUB_WATCHDOG_INTERVAL", WATCHDOG_INTERVAL)
        failures = int(_env_number(environ, "BRIGADE_HUB_WATCHDOG_FAILURES", WATCHDOG_FAILURES))
        startup = _env_number(environ, "BRIGADE_HUB_WATCHDOG_STARTUP", STARTUP_SECONDS)
        watch_namespace = environ.get("BRIGADE_HUB_WATCHDOG_NETNS", "") == "1"
        binary = resolve_binary(hub_binary, hub_sha256, hub_sha256_file=hub_sha256_file, env=environ)
        verify_binary(binary)
        db_path = Path(db_path).expanduser()
        from . import fleet_hub

        # The Python hub owns the schema (D6). Migrate, or refuse a newer
        # file, before brigade-hub or its upstream opens it.
        conn = fleet_hub.init_db(db_path)
        try:
            # init_db sets WAL only when it migrates, and `fleet restore`
            # publishes its file in rollback-journal mode. brigade-hub's
            # readers take no busy wait, so in that mode every Python write
            # during startup refuses them. WAL persists in the file.
            journal = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        finally:
            conn.close()
        if str(journal).lower() != "wal":
            raise EngineError(f"fleet hub database {db_path} could not switch to WAL (journal mode {journal})")
    except EngineError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - init_db raises sqlite3, OS and hub errors alike
        print(f"error: fleet hub database: {exc}", file=sys.stderr)
        return 2
    argv = hub_argv(
        binary.path,
        host=host,
        port=port,
        db_path=db_path,
        token_file=token_file,
        allow_admin_writes=allow_admin_writes,
        require_sealed=require_sealed,
        deck_config_path=deck_config_path,
        trust_tailscale_identity=trust_tailscale_identity,
        trust_forwarded_proto=trust_forwarded_proto,
        route_modes=route_modes,
    )
    return _supervise(
        argv,
        url=health_url(host, port),
        banner=(
            f"{STARTED} {host}:{port} (brigade-hub front, managed Python upstream; db {db_path}; "
            f"route modes {', '.join(route_modes) or 'defaults'})"
        ),
        interval=interval,
        failures=failures,
        startup=startup,
        watch_namespace=watch_namespace,
        healthy=healthy,
        only_loopback=only_loopback,
    )


def _supervise(
    argv: list[str],
    *,
    url: str,
    banner: str,
    interval: float,
    failures: int,
    startup: float,
    watch_namespace: bool,
    healthy: Callable[[str], bool],
    only_loopback: Callable[[], bool],
) -> int:
    child: subprocess.Popen[bytes] | None = None
    pending: int | None = None
    stopping = False
    # The managed upstream runs this interpreter, so both engines come from
    # one Brigade install unless the operator names another.
    child_env = dict(os.environ)
    child_env.setdefault("BRIGADE_HUB_PYTHON", sys.executable)

    def send(signum: int) -> None:
        if child is None:
            return
        try:
            child.send_signal(signum)
        except ProcessLookupError:
            pass

    def forward(signum: int, _frame: object) -> None:
        # Installed before the child exists, so a stop during the spawn is
        # kept and forwarded below instead of killing this process.
        nonlocal stopping, pending
        stopping = True
        if child is None:
            pending = signum
        else:
            send(signum)

    previous_handlers = {sig: signal.signal(sig, forward) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        # brigade-hub starts with both blocked (the mask survives fork and
        # exec) and unblocks them once its handlers exist, so a stop forwarded
        # right after the spawn never meets its pre-main default action.
        mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT})
        try:
            child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, env=child_env)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        if pending is not None:
            send(pending)
        reason = _watch(
            child,
            url=url,
            banner=banner,
            interval=interval,
            failures=failures,
            startup=startup,
            watch_namespace=watch_namespace,
            healthy=healthy,
            only_loopback=only_loopback,
            stopping=lambda: stopping,
        )
        if reason is not None:
            print(f"brigade fleet hub watchdog: {reason}; stopping brigade-hub", file=sys.stderr, flush=True)
            send(signal.SIGTERM)
            try:
                child.wait(timeout=STOP_GRACE)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
            return 1
        returncode = child.wait()
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
    if returncode == 0 and stopping:
        print(STOPPED, flush=True)
    elif not stopping:
        print(f"brigade-hub exited with {returncode}", file=sys.stderr, flush=True)
    return exit_code(returncode)


def _watch(
    child: subprocess.Popen[bytes],
    *,
    url: str,
    banner: str,
    interval: float,
    failures: int,
    startup: float,
    watch_namespace: bool,
    healthy: Callable[[str], bool],
    only_loopback: Callable[[], bool],
    stopping: Callable[[], bool],
) -> str | None:
    """Wait for the child to exit, or return why the watchdog must stop it."""
    started = time.monotonic()
    served = False
    sick = lonely = 0
    next_check = started
    while child.poll() is None:
        if stopping():
            return None
        now = time.monotonic()
        if now >= next_check:
            if healthy(url):
                if not served:
                    print(banner, flush=True)
                    served = True
                sick = 0
            elif served:
                sick += 1
            elif now - started > startup:
                return f"/health did not answer within {startup:g} s of startup"
            lonely = lonely + 1 if watch_namespace and only_loopback() else 0
            if sick >= failures:
                return f"/health failed {failures} checks in a row"
            if lonely >= failures:
                return f"no network interface but loopback in {failures} checks"
            # Check quickly until the first answer, then at the interval.
            next_check = now + (interval if served else POLL_SECONDS)
        try:
            child.wait(timeout=POLL_SECONDS)
        except subprocess.TimeoutExpired:
            pass
    return None
