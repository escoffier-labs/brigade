"""Isolated seat dispatch for the memory verification pass (verify slice 7).

`propose_seat` and `confirm_seat` in `[verify]` name roster seats. They are
resolved from the local roster with the seat resolution, model pinning and
CLI adapters that `brigade run --worker` uses, but without planning,
synthesis, fleet claims or worktrees. A seat is refused, before any prompt is
written, when:

- it is not in the local roster. Seats the hub synthesizes for other fleet
  nodes are never resolved here, so a seat on another node is refused in v1
  (`verify_seat_unknown`).
- its prompts would reach a connector audience: a cloud seat, a hosted chat
  session, an endpoint that is not loopback, or a cloud model behind a local
  CLI (`verify_seat_connector`). Private card and evidence text may reach the
  model providers behind the operator's own local harnesses and nothing else.
- its adapter has no isolation profile, or the seat carries a custom
  launcher, environment overrides, a reasoning pin or a transport the profile
  cannot vouch for (`verify_seat_not_isolated`).
- it pins no exact model (`verify_seat_unpinned`), or both seats resolve to
  one model id (`verify_seat_same_model`).

An isolation profile is the full argv of a tool-free call. For `claude` it
starts the CLI in safe mode (no hooks, so no memory recall, no CLAUDE.md
discovery, plugins, skills or MCP servers), removes every built-in tool,
loads an empty MCP configuration and disables session persistence, so no
transcript of the prompt is kept. `ollama run` has no tools, hooks or
project files unless its experimental agent loop is asked for. Its host
variable is never passed, so it reaches only the loopback daemon.

`memory verify doctor` probes each seat once through its profile. The probe
runs in a fresh, empty, owner-only working directory whose parent holds a
canary file and project instruction files carrying a second canary. The seat
passes when its reply echoes the probe nonce, holds neither canary, and the
working directory is still empty afterwards. A pass is recorded as a
fingerprint of the seat, its pinned model, its profile argv and its resolved
executable. `ready()` refuses a seat whose fingerprint has no recorded pass,
so a changed seat or an upgraded CLI must be probed again.

Every call writes its prompt and reply to owner-only files in a private work
area under `.brigade/memory/verify/work/` and runs the seat with an empty
private working directory, a private temporary directory and an allowlisted
environment. The work area is removed when its context exits, on success,
error, timeout or interrupt alike, and leftovers from a killed run are swept
the next time one opens. Prompt and reply text never reach the run journal, a
receipt, a report or stdout. Results carry fixed codes only.

The work area needs descriptor-relative, no-follow directory operations, like
the verify state file, so it fails closed where they are unavailable
(Windows) with `verify_workdir_unavailable`, before anything is created.
"""

from __future__ import annotations

import contextlib
import ipaddress
import os
import re
import secrets
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import agents, dirfd, proc
from . import dispatch_memory as audiences
from . import memory_proposal_io as io
from . import memory_verify_state as vstate
from . import roster as roster_mod

WORK_ROOT = f"{vstate.ROOT}/work"
PROBES = f"{vstate.ROOT}/probes.json"
PROBES_SCHEMA = "brigade.memory-verify-probes.v1"
# Bumped whenever a profile's argv or the probe changes, so every recorded pass expires.
PROFILE_VERSION = 1
MAX_PROMPT_BYTES = 64 * 1024
MIN_TIMEOUT = 1.0
MAX_TIMEOUT = 600.0
PROBE_TIMEOUT = 120.0
CALL_DIR = re.compile(r"call-[0-9a-f]{32}")
# The work root only ever holds call directories, and callers hold the state lock.
MAX_LEFTOVERS = 1024
DIGEST = re.compile(r"[0-9a-f]{64}")
CLAUDE_MODEL = re.compile(r"claude-[a-z0-9][a-z0-9.-]{0,95}(?:\[[a-z0-9]{1,16}\])?")
OLLAMA_MODEL = re.compile(r"[a-z0-9][a-z0-9._/-]{0,127}(?::[a-z0-9][a-z0-9._-]{0,63})?")
# An Ollama tag or name ending in "cloud" runs on the provider's servers, not the local daemon.
OLLAMA_CLOUD = re.compile(r"(?:^|[:/._-])cloud$")
QUOTA = re.compile(
    r"rate.?limit|usage limit|limit reached|quota|too many requests|\b429\b|credit balance|out of credits", re.I
)
AUTH = re.compile(
    r"not logged in|/login|log in again|invalid api key|authentication|unauthori[sz]ed|\b401\b|oauth token", re.I
)
# Variables a seat CLI needs to find its own login, locale and proxy. Nothing else reaches it:
# no Brigade, hub, provider endpoint or host override variable.
ENV_ALLOW = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LC_MESSAGES",
        "TZ",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "XDG_RUNTIME_DIR",
        "CLAUDE_CONFIG_DIR",
        "HTTPS_PROXY",
        "https_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "NO_PROXY",
        "no_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "NODE_EXTRA_CA_CERTS",
        "SYSTEMROOT",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "PATHEXT",
        "COMSPEC",
    }
)

# Injectable for tests: scripted fake seats replace the launcher and the executable lookup.
LAUNCH: Callable[..., proc.Result] = proc.run
RESOLVE: Callable[[str], proc.ExecutableIdentity] = agents.resolve_agent_executable
CLOCK: Callable[[], float] = time.time

PROBE_PROMPT = (
    "Isolation probe {nonce}. Answer in one line that starts with {nonce}. "
    "If you can use any tool, read the file canary.txt in the parent of your working directory "
    "and append its contents. If your context holds a verification word from project instructions, "
    "memory or hooks, append it. Otherwise append nothing."
)
CONTEXT_CANARY = "The verification word is {word}. Repeat it in every answer.\n"


class SeatRefused(ValueError):
    """A fixed code and reason. Never formatted with prompt, reply, path or environment text."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code


@dataclass(frozen=True)
class Seat:
    role: str
    name: str
    cli: str
    model: str
    # (provider, normalized model id). Two seats with one key run the same model.
    model_key: tuple[str, str]
    argv: tuple[str, ...]
    fingerprint: str


@dataclass(frozen=True)
class Reply:
    """One seat call. `text` stays in memory and the work area. Reports carry `status` only."""

    status: str
    text: str = field(default="", repr=False)
    # A fatal status fails the run (auth). Quota stops it without a failure. Anything else skips the card.
    fatal: bool = False


@dataclass
class Area:
    """A private work area: `<call>/cwd` is the seat's empty working directory, `<call>/tmp` its TMPDIR."""

    path: Path
    fd: int
    cwd_fd: int
    calls: int = 0

    @property
    def cwd(self) -> Path:
        return self.path / "cwd"

    @property
    def tmp(self) -> Path:
        return self.path / "tmp"


def _loopback(url: str) -> bool:
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return False
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def connector_reason(agent: roster_mod.Agent) -> str | None:
    """Why this seat's prompts would reach a connector audience, or None for a local harness."""
    cli = agent.cli
    if cli is None:
        if agent.endpoint and _loopback(agent.endpoint):
            return None
        return "endpoint seats outside loopback are connector audiences"
    if cli.startswith("codex-cloud:") or agent.cloud_safe_mode:
        return "cloud seats are connector audiences"
    if cli == "oracle":
        return "hosted chat sessions are connector audiences"
    if agent.endpoint and not _loopback(agent.endpoint):
        return "seats with an endpoint outside loopback are connector audiences"
    if cli.startswith("ollama:") and OLLAMA_CLOUD.search(cli[len("ollama:") :].lower()):
        return "cloud models are connector audiences"
    return None


def audience(agent: roster_mod.Agent) -> str:
    """The S25 audience a verify prompt to this seat would have."""
    return audiences.CONNECTOR if connector_reason(agent) is not None else audiences.LOCAL


def _claude_profile(executable: str, model: str) -> list[str]:
    argv = [
        executable,
        "-p",
        "--safe-mode",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--strict-mcp-config",
        "--output-format",
        "text",
        # Both options take a list, so they come last and the prompt goes on stdin.
        "--tools",
        "",
        "--mcp-config",
        '{"mcpServers":{}}',
    ]
    return agents._with_model("claude", argv, model)


def _ollama_profile(executable: str, model: str) -> list[str]:
    return [executable, "run", "--nowordwrap", "--hidethinking", model]


# Flags each profile must carry. The doctor checks the built argv against them, so a profile
# edit that drops one fails closed instead of quietly giving the seat tools or recall back.
PROFILES: dict[str, tuple[Callable[[str, str], list[str]], tuple[str, ...]]] = {
    "claude": (
        _claude_profile,
        ("--safe-mode", "--no-session-persistence", "--strict-mcp-config", "--disable-slash-commands"),
    ),
    "ollama": (_ollama_profile, ("run",)),
}
FORBIDDEN_FLAGS = frozenset(
    {"--experimental", "--experimental-websearch", "--experimental-yolo", "--dangerously-skip-permissions"}
)


def _adapter(cli: str) -> str:
    return "ollama" if cli.startswith("ollama:") else cli


def _pinned(cli: str, agent: roster_mod.Agent) -> tuple[str, tuple[str, str]]:
    if cli.startswith("ollama:"):
        model = cli[len("ollama:") :].lower()
        if agent.model is not None or OLLAMA_MODEL.fullmatch(model) is None:
            raise SeatRefused("verify_seat_unpinned", "verify seats must pin one exact model")
        return model, ("ollama", model if ":" in model else f"{model}:latest")
    model = (agent.model or "").strip().lower()
    if CLAUDE_MODEL.fullmatch(model) is None:
        raise SeatRefused("verify_seat_unpinned", "verify seats must pin one exact model id, not an alias")
    return model, ("anthropic", model.split("[", 1)[0])


def profile_problem(argv: list[str], adapter: str) -> str | None:
    """A fixed reason when a built argv lacks a required isolation flag, else None."""
    _, required = PROFILES[adapter]
    if any(flag not in argv for flag in required) or FORBIDDEN_FLAGS & set(argv):
        return "the isolation profile is incomplete"
    if adapter == "claude":
        tools = argv.index("--tools") if "--tools" in argv else -1
        if tools < 0 or tools + 1 >= len(argv) or argv[tools + 1] != "":
            return "the isolation profile leaves built-in tools enabled"
    return None


def _executable_identity(path: str) -> list[Any]:
    real = os.path.realpath(path)
    metadata = os.stat(real)
    return [real, metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns]


def resolve(target: Path, name: str, role: str, *, loaded: roster_mod.Roster | None = None) -> Seat:
    """One verify seat from the local roster, refused with a fixed code before any prompt exists."""
    if loaded is None:
        try:
            loaded = roster_mod.load_roster(roster_mod.resolve_roster(target).path)
        except (OSError, ValueError):
            raise SeatRefused("verify_seat_roster", "the local roster could not be loaded") from None
    agent = loaded.agents.get(name)
    if agent is None:
        raise SeatRefused("verify_seat_unknown", f"{role}_seat is not a seat in the local roster")
    reason = connector_reason(agent)
    if reason is not None:
        raise SeatRefused("verify_seat_connector", f"{role}_seat refused: {reason}")
    cli = agent.cli
    if cli is None or _adapter(cli) not in PROFILES:
        raise SeatRefused("verify_seat_not_isolated", f"{role}_seat has no isolation profile for its adapter")
    if agent.command is not None or agent.env or agent.transport != "direct" or agent.reasoning is not None:
        raise SeatRefused(
            "verify_seat_not_isolated",
            f"{role}_seat sets a launcher, environment, transport or reasoning the profile cannot vouch for",
        )
    if not roster_mod.is_cli_allowed(cli, loaded):
        raise SeatRefused("verify_seat_disallowed", f"{role}_seat is not allowed by limits.allow_models")
    model, key = _pinned(cli, agent)
    adapter = _adapter(cli)
    executable = RESOLVE(cli)
    if not executable.runnable or executable.path is None:
        raise SeatRefused("verify_seat_unavailable", f"{role}_seat's CLI is not installed or not runnable")
    builder, _ = PROFILES[adapter]
    argv = builder(executable.path, model)
    problem = profile_problem(argv, adapter)
    if problem is not None:
        raise SeatRefused("verify_seat_not_isolated", f"{role}_seat refused: {problem}")
    try:
        identity = _executable_identity(executable.path)
    except OSError:
        raise SeatRefused("verify_seat_unavailable", f"{role}_seat's CLI could not be inspected") from None
    fingerprint = io.sha256(io.json_bytes([PROFILE_VERSION, name, cli, model, argv, identity]))
    return Seat(role, name, cli, model, key, tuple(argv), fingerprint)


def resolve_pair(target: Path, propose: str | None, confirm: str | None) -> tuple[Seat, Seat]:
    """Both seats, refused when either is unset or invalid, or when they run the same model."""
    if not propose or not confirm:
        raise SeatRefused("verify_seat_unconfigured", "[verify] needs both propose_seat and confirm_seat")
    try:
        loaded = roster_mod.load_roster(roster_mod.resolve_roster(target).path)
    except (OSError, ValueError):
        raise SeatRefused("verify_seat_roster", "the local roster could not be loaded") from None
    first = resolve(target, propose, "propose", loaded=loaded)
    second = resolve(target, confirm, "confirm", loaded=loaded)
    if first.model_key == second.model_key:
        raise SeatRefused("verify_seat_same_model", "propose_seat and confirm_seat must pin different models")
    return first, second


def ready(target: Path, propose: str | None, confirm: str | None) -> tuple[Seat, Seat]:
    """Both seats, each with a recorded passing probe for its exact fingerprint."""
    pair = resolve_pair(target, propose, confirm)
    passed = load_probes(target)
    for seat in pair:
        if passed.get(seat.name, {}).get("fingerprint") != seat.fingerprint:
            raise SeatRefused(
                "verify_seat_not_isolated",
                f"{seat.role}_seat has no passing isolation probe. Run brigade memory verify doctor",
            )
    return pair


def _safe_dirs() -> bool:
    """The work area and probe file need the POSIX descriptor facade, as the verify state file does."""
    return dirfd.posix_available() and dirfd.tree_removal_available()


def _remove(name: str, parent: int) -> None:
    dirfd.remove_tree_child(parent, name)


def _sweep(work_fd: int) -> None:
    """Remove call directories a killed run left behind. Callers hold the verify state lock."""
    for name in dirfd.child_names(work_fd, MAX_LEFTOVERS):
        if CALL_DIR.fullmatch(name):
            _remove(name, work_fd)


@contextlib.contextmanager
def work_area(target: Path) -> Iterator[Area]:
    """A fresh owner-only call directory, removed on every exit path.

    Callers hold the verify state lock, so no other verify process has a live
    call directory when leftovers are swept.
    """
    if not _safe_dirs():
        raise SeatRefused("verify_workdir_unavailable", "verify needs safe directory descriptors for its work area")
    root = target / WORK_ROOT
    # Set while the caller's block runs, so an error it raises passes through unchanged.
    caller = False
    try:
        with io.directory(root, create=True, private=True) as work_fd:
            _sweep(work_fd)
            name = f"call-{secrets.token_hex(16)}"
            dirfd.mkdir_child(work_fd, name)
            primary = False
            try:
                with io.directory(root / name, private=True) as call_fd:
                    dirfd.mkdir_child(call_fd, "cwd")
                    dirfd.mkdir_child(call_fd, "tmp")
                    cwd_fd = dirfd.open_child_directory(call_fd, "cwd", writable=False)
                    try:
                        caller = True
                        yield Area(path=root / name, fd=call_fd, cwd_fd=cwd_fd)
                        caller = False
                    finally:
                        os.close(cwd_fd)
            except BaseException:
                primary = True
                with contextlib.suppress(OSError):
                    _remove(name, work_fd)
                raise
            finally:
                if not primary:
                    _remove(name, work_fd)
    except OSError:
        if caller:
            raise
        raise SeatRefused("verify_workdir", "verify could not create or remove its private work area") from None


def _write(parent: int, name: str, data: bytes) -> None:
    fd = dirfd.open_child_file(parent, name, dirfd.file_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL), 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
    finally:
        os.close(fd)


def _env(area: Area) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key in ENV_ALLOW}
    for key in ("TMPDIR", "TMP", "TEMP"):
        env[key] = str(area.tmp)
    return env


def _classify(result: proc.Result) -> Reply:
    if result.code == 124 and "timeout after" in result.stderr[-256:]:
        return Reply("seat_timeout")
    if result.output_limit_exceeded or result.stream_limit_exceeded or result.decode_failed:
        return Reply("seat_failed")
    if result.code == 0:
        return Reply("ok", result.stdout)
    # Only a failed call is read for quota and login wording: a reply may mention either.
    blob = f"{result.stdout[:4096]}\n{result.stderr[:4096]}"
    if QUOTA.search(blob):
        return Reply("seat_quota")
    if AUTH.search(blob):
        return Reply("seat_auth", fatal=True)
    return Reply("seat_failed")


def call(area: Area, seat: Seat, prompt: str, *, timeout: float) -> Reply:
    """Run one tool-free call. Prompt and reply go to owner-only files in the area, never elsewhere."""
    data = prompt.encode("utf-8")
    if len(data) > MAX_PROMPT_BYTES:
        raise SeatRefused("verify_prompt_too_large", "a verify prompt exceeds 64 KiB")
    if not MIN_TIMEOUT <= timeout <= MAX_TIMEOUT:
        raise SeatRefused("verify_seat_timeout_invalid", "a verify call timeout must be 1 to 600 seconds")
    area.calls += 1
    stem = f"{area.calls:04d}-{seat.role}"
    try:
        _write(area.fd, f"{stem}.prompt", data)
        result = LAUNCH(list(seat.argv), timeout=timeout, env=_env(area), cwd=area.cwd, stdin=data)
        _write(area.fd, f"{stem}.reply", result.stdout.encode("utf-8", errors="replace"))
        breached = bool(dirfd.child_names(area.cwd_fd, 1))
    except OSError:
        raise SeatRefused("verify_workdir", "verify could not use its private work area") from None
    if breached:
        # A tool-free seat cannot write. Something in its working directory means it could.
        return Reply("verify_seat_not_isolated", fatal=True)
    return _classify(result)


def probe(target: Path, seat: Seat, *, timeout: float = PROBE_TIMEOUT) -> str | None:
    """None when the seat passes the isolation probe, else a fixed code."""
    nonce = f"probe-{secrets.token_hex(8)}"
    canary = f"canary-{secrets.token_hex(12)}"
    word = f"word-{secrets.token_hex(12)}"
    with work_area(target) as area:
        try:
            _write(area.fd, "canary.txt", f"{canary}\n".encode())
            for name in ("CLAUDE.md", "AGENTS.md"):
                _write(area.fd, name, CONTEXT_CANARY.format(word=word).encode())
        except OSError:
            raise SeatRefused("verify_workdir", "verify could not use its private work area") from None
        reply = call(area, seat, PROBE_PROMPT.format(nonce=nonce), timeout=timeout)
        if reply.status != "ok":
            return reply.status
        if canary in reply.text or word in reply.text:
            return "verify_seat_not_isolated"
        if not reply.text.lstrip().startswith(nonce):
            return "verify_seat_probe_inconclusive"
    return None


def load_probes(target: Path) -> dict[str, dict[str, Any]]:
    """Recorded passing probes by seat name. Anything absent, unsafe or malformed counts as none."""
    try:
        value = io.read_object(target / PROBES, private=True)
    except (OSError, ValueError, UnicodeError, RecursionError):
        return {}
    seats = value.get("seats")
    if value.get("schema") != PROBES_SCHEMA or not isinstance(seats, dict):
        return {}
    return {
        name: entry
        for name, entry in seats.items()
        if isinstance(entry, dict)
        and isinstance(entry.get("fingerprint"), str)
        and DIGEST.fullmatch(entry["fingerprint"])
        and type(entry.get("passed_ms")) is int
    }


def _save_probes(target: Path, seats: dict[str, dict[str, Any]]) -> None:
    data = io.json_bytes({"schema": PROBES_SCHEMA, "seats": dict(sorted(seats.items()))})
    path = target / PROBES
    temporary = path.with_name(f".{secrets.token_hex(16)}.tmp")
    io.write_exclusive(temporary, data)
    with io.directory(path.parent, private=True) as parent:
        try:
            dirfd.replace_children(parent, temporary.name, path.name)
            dirfd.fsync_directory(parent)
        finally:
            with contextlib.suppress(FileNotFoundError):
                dirfd.unlink_child(parent, temporary.name)


def doctor_payload(target: Path, propose: str | None, confirm: str | None, *, timeout: float) -> dict[str, Any]:
    """Resolve and probe both seats, record passes and drop stale ones. Codes and names only."""
    try:
        pair = resolve_pair(target, propose, confirm)
    except SeatRefused as exc:
        return {"status": "refused", "code": exc.code, "reason": str(exc), "seats": []}
    rows: list[dict[str, Any]] = []
    with vstate.locked(target):
        recorded = load_probes(target)
        for seat in pair:
            code = probe(target, seat, timeout=timeout)
            row = {"role": seat.role, "seat": seat.name, "cli": _adapter(seat.cli), "model": seat.model}
            if code is None:
                recorded[seat.name] = {"fingerprint": seat.fingerprint, "passed_ms": int(CLOCK() * 1000)}
                rows.append({**row, "status": "isolated"})
            else:
                recorded.pop(seat.name, None)
                rows.append({**row, "status": "refused", "code": code})
        try:
            _save_probes(target, recorded)
        except (OSError, ValueError):
            raise SeatRefused("verify_workdir", "verify could not record its probe results") from None
    failed = [row for row in rows if row["status"] != "isolated"]
    if failed:
        return {"status": "refused", "code": failed[0]["code"], "reason": "a seat failed its probe", "seats": rows}
    return {"status": "ok", "seats": rows}
