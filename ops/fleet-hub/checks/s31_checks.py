#!/usr/bin/env python3
"""S31 host acceptance checks for the dogfood hub, one subcommand per acceptance item.

Run these only after the matching HOST-ACTIONS.md steps are approved and done.
Each check prints PASS, FAIL, PENDING or INFO lines and appends one JSON record
to --record (default ./s31-results.jsonl), so repeated runs (three reboots, three
daemon restarts) are counted by `summary`. Checks that change state say so in
their help text and never run implicitly. Stdlib only; run with the host's python3.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import http.client
import json
import os
import posixpath
import secrets
import socket
import sqlite3
import ssl
import stat
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RECIPE = Path(__file__).resolve().parents[1]
REPO = RECIPE.parents[1]
# control-brigade.py finds the repository root at parents[3], so the drive is
# mounted at the same depth it has in the checkout.
DRIVE = "/repo/registry/skills/verify-brigade"
CGROUP = Path("/sys/fs/cgroup")
HUB_PORT = 3774
ECHO_PORT = 3775
ECHO_PATH = "/s31-echo"
FORGED_LOGIN = "forged-user@example.test"
FORGED_FOR = "203.0.113.9"


class Result:
    def __init__(self, check: str) -> None:
        self.check = check
        self.items: list[dict[str, Any]] = []

    def add(self, status: str, name: str, detail: Any = None) -> None:
        self.items.append({"status": status, "name": name, "detail": detail})
        suffix = f": {detail}" if detail not in (None, "") else ""
        print(f"{status:<8}{name}{suffix}")

    def ok(self, condition: bool, name: str, detail: Any = None) -> bool:
        self.add("PASS" if condition else "FAIL", name, detail)
        return condition

    @property
    def status(self) -> str:
        states = {item["status"] for item in self.items}
        if "FAIL" in states:
            return "fail"
        if "PENDING" in states:
            return "pending"
        return "pass"


# ------------------------------------------------------------------ helpers


def run(argv: list[str], *, timeout: float = 120, check: bool = False) -> subprocess.CompletedProcess[str]:
    done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    if check and done.returncode != 0:
        raise RuntimeError(f"{' '.join(argv)} exited {done.returncode}: {done.stderr.strip()[:400]}")
    return done


def compose(*args: str, project: Path = RECIPE) -> list[str]:
    return ["docker", "compose", "--project-directory", str(project), *args]


def container_id(service: str, project: Path = RECIPE) -> str:
    return run(compose("ps", "-q", service, project=project)).stdout.strip()


def inspect(container: str, template: str) -> str:
    return run(["docker", "inspect", "-f", template, container]).stdout.strip()


def env_file(project: Path = RECIPE) -> dict[str, str]:
    values = {}
    path = project / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    return values


def parse_cpus(text: str) -> set[int]:
    cpus: set[int] = set()
    for part in text.strip().split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            low, high = part.split("-", 1)
            cpus.update(range(int(low), int(high) + 1))
        else:
            cpus.add(int(part))
    return cpus


def percentile(values: list[float], share: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return float("nan")
    index = min(len(ordered) - 1, max(0, int(round(share * (len(ordered) - 1)))))
    return ordered[index]


def listening_ports(proc_net_tcp: str, inodes: set[str] | None = None) -> set[int]:
    """Ports in LISTEN state (0A) from /proc/net/tcp or tcp6 text.

    With `inodes`, only sockets whose inode (the tenth column) is in the set.
    """
    ports = set()
    for line in proc_net_tcp.splitlines()[1:]:
        fields = line.split()
        if len(fields) > 3 and fields[3] == "0A":
            if inodes is not None and (len(fields) < 10 or fields[9] not in inodes):
                continue
            ports.add(int(fields[1].rsplit(":", 1)[1], 16))
    return ports


# Run inside the hub container by `access`. The holder's namespace is shared
# with the front, so its socket tables also list the front's tailscaled peerapi
# and Docker's embedded DNS. The container's own PID namespace shows only the
# hub's processes, and their socket inodes pick out the hub's listeners.
HUB_SOCKETS = (
    "cat /proc/net/tcp /proc/net/tcp6 2>/dev/null; echo '#inodes'; "
    'for f in /proc/[0-9]*/fd/*; do readlink "$f"; done 2>/dev/null'
    " | sed -n 's/^socket:\\[\\([0-9]*\\)\\]$/\\1/p'"
)


def split_hub_sockets(text: str) -> tuple[str, set[str]]:
    """The socket tables and the hub processes' socket inodes from HUB_SOCKETS output."""
    tables, _, inodes = text.partition("#inodes")
    return tables, {line.strip() for line in inodes.splitlines() if line.strip()}


def publishes_any_port(holder_bindings: str) -> bool:
    """Whether the holder's HostConfig.PortBindings JSON maps any port to the host (unreadable counts as yes).

    Any binding refuses every loopback exemption: a container port published
    to a host loopback port can sit behind any host port number.
    """
    try:
        bindings = json.loads(holder_bindings or "null") or {}
    except ValueError:
        return True
    return not isinstance(bindings, dict) or any(bindings.values())


def connect_refused(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return False
    except OSError:
        return True


# A reachable listener on host loopback is a FAIL unless the operator exempts
# its port with --allow-loopback-listener PORT=EXE_REALPATH//REASON and every
# process holding the socket runs exactly that executable. The exemption trusts
# that program not to forward to the hub. No HTTP answer proves a listener is not a proxy
# to the hub, so the probe below is evidence for the record, never a pass.
PROC = Path("/proc")
PROBE_DEADLINE = 5.0
PROBE_CAP = 65536
EVIDENCE_PROBES = (("/health", {}), ("/status", {"Tailscale-User-Login": FORGED_LOGIN}))
LOOPBACK_HEX = {
    "0100007F",  # 127.0.0.1
    "00000000",  # 0.0.0.0
    "00000000000000000000000001000000",  # ::1
    "00000000000000000000000000000000",  # ::
    "0000000000000000FFFF00000100007F",  # ::ffff:127.0.0.1
}


EXEMPTION_SEPARATOR = "//"
DELETED = " (deleted)"


def parse_exemptions(values: list[str]) -> dict[int, tuple[str, str]]:
    """PORT=EXE_REALPATH//REASON triples from --allow-loopback-listener.

    PORT is digits, so the first "=" ends it. EXE_REALPATH must be the
    canonical absolute path `readlink -f /proc/<pid>/exe` prints. A canonical
    path never contains "//", so the first "//" after it ends the path and the
    reason may contain anything. A reason is required.
    """
    exemptions: dict[int, tuple[str, str]] = {}
    for value in values:
        port, _, rest = value.partition("=")
        exe, separator, reason = rest.partition(EXEMPTION_SEPARATOR)
        canonical = (
            exe.startswith("/")
            and exe != "/"
            and posixpath.normpath(exe) == exe
            and not exe.startswith("//")
            and not exe.endswith(DELETED)
        )
        if not port.isdigit() or not separator or not canonical or not reason.strip():
            raise ValueError(f"--allow-loopback-listener wants PORT=EXE_REALPATH//REASON, got {value!r}")
        exemptions[int(port)] = (exe, reason.strip())
    return exemptions


def probe_evidence(host: str, port: int, deadline: float | None = None) -> dict[str, Any]:
    """Status line and headers each evidence probe gets, within one total deadline per listener.

    Reads raw bytes up to PROBE_CAP per probe, so a reset after the headers
    keeps what already arrived, and a peer that trickles bytes is cut off at
    the deadline. Bodies are never kept.
    """
    stop = time.monotonic() + (deadline or PROBE_DEADLINE)
    evidence: dict[str, Any] = {}
    for path, headers in EVIDENCE_PROBES:
        data, ended = b"", "eof"
        remaining = stop - time.monotonic()
        if remaining <= 0:
            evidence[path] = {"ended": "deadline before the probe"}
            continue
        try:
            with socket.create_connection((host, port), timeout=remaining) as conn:
                # The connect spent part of the budget: recompute before the send.
                remaining = stop - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("deadline reached after connect")
                conn.settimeout(remaining)
                lines = [f"GET {path} HTTP/1.0", f"Host: {host}:{port}"]
                lines += [f"{name}: {value}" for name, value in headers.items()]
                conn.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
                while len(data) < PROBE_CAP:
                    remaining = stop - time.monotonic()
                    if remaining <= 0:
                        ended = "deadline"
                        break
                    conn.settimeout(remaining)
                    chunk = conn.recv(min(4096, PROBE_CAP - len(data)))
                    if not chunk:
                        break
                    data += chunk
                else:
                    ended = "cap"
        except ConnectionResetError:
            ended = "reset"
        except TimeoutError:
            ended = "deadline"
        except OSError as exc:
            ended = f"error: {type(exc).__name__}"
        head = data.split(b"\r\n\r\n", 1)[0].decode("latin-1").split("\r\n")
        evidence[path] = {
            "status_line": head[0][:200] if data else None,
            "headers": {
                name.strip().lower(): value.strip()[:200]
                for name, sep, value in (line.partition(":") for line in head[1:])
                if sep
            }
            if data
            else {},
            "bytes": len(data),
            "ended": ended,
        }
    return evidence


def loopback_holders(port: int, proc: Path | None = None) -> list[dict[str, Any]] | None:
    """The processes holding host LISTEN sockets on `port` that loopback reaches, or None.

    Reads the host's /proc/net/tcp and tcp6 for LISTEN sockets on loopback or
    the wildcard address, then maps each socket inode to the processes whose
    fds hold it. None when no socket is found or any socket has no readable
    holder, as for another account's process.
    """
    root = proc or PROC
    inodes: set[str] = set()
    for name in ("tcp", "tcp6"):
        try:
            table = (root / "net" / name).read_text(encoding="utf-8")
        except OSError:
            continue
        for line in table.splitlines()[1:]:
            fields = line.split()
            if len(fields) < 10 or fields[3] != "0A":
                continue
            address, _, hex_port = fields[1].rpartition(":")
            if int(hex_port, 16) == port and address in LOOPBACK_HEX:
                inodes.add(fields[9])
    if not inodes:
        return None
    found: dict[str, list[dict[str, Any]]] = {inode: [] for inode in inodes}
    for entry in root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            links = [os.readlink(fd) for fd in (entry / "fd").iterdir()]
        except OSError:
            continue
        held = {link[8:-1] for link in links if link.startswith("socket:[")} & inodes
        if not held:
            continue
        try:
            holder = {
                "pid": int(entry.name),
                "exe": os.path.realpath(entry / "exe"),
                "comm": (entry / "comm").read_text(encoding="utf-8").strip(),
                "cgroup": (entry / "cgroup").read_text(encoding="utf-8").strip(),
            }
        except OSError:
            continue
        for inode in held:
            found[inode].append(holder)
    if not all(found.values()):
        return None
    unique = {holder["pid"]: holder for holders in found.values() for holder in holders}
    return [unique[pid] for pid in sorted(unique)]


def hub_image_holder(holder: dict[str, Any], hub_containers: list[str]) -> bool:
    """Whether a holder runs the hub's binary or sits in a container of the hub's image."""
    name = Path(holder["exe"].removesuffix(DELETED)).name
    return name == "brigade-hub" or any(cid and cid in holder["cgroup"] for cid in hub_containers)


def front_url() -> str:
    url = os.environ.get("BRIGADE_S31_FRONT_URL")
    if not url:
        raise SystemExit(
            "set BRIGADE_S31_FRONT_URL to the front's HTTPS URL (from `tailscale serve status` in the front)"
        )
    return url.rstrip("/")


def get(url: str, *, headers: dict[str, str] | None = None, timeout: float = 5) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=ssl.create_default_context()) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def wait_for_front(limit: float) -> float | None:
    """Seconds until GET /health through the real front answers 200, or None past the limit."""
    started = time.monotonic()
    while time.monotonic() - started < limit:
        try:
            if get(front_url() + "/health", timeout=3)[0] == 200:
                return time.monotonic() - started
        except OSError:
            pass
        time.sleep(1)
    return None


def namespace_marks(services: tuple[str, ...] = ("netns", "hub", "front")) -> dict[str, str]:
    """Each container's view of its non-loopback interfaces' MAC addresses. Equal marks mean one namespace."""
    marks = {}
    for service in services:
        cid = container_id(service)
        if not cid:
            marks[service] = "not running"
            continue
        script = 'for i in /sys/class/net/*; do [ "${i##*/}" = lo ] || cat "$i/address"; done'
        done = run(["docker", "exec", cid, "sh", "-c", script])
        marks[service] = ",".join(sorted(done.stdout.split())) if done.returncode == 0 else "exec failed"
    return marks


def one_namespace(result: Result) -> None:
    marks = namespace_marks()
    holder = marks.get("netns", "")
    result.ok(bool(holder) and holder not in ("not running", "exec failed"), "holder has a network interface", holder)
    result.ok(marks.get("hub") == holder, "hub is in the holder's namespace", marks.get("hub"))
    result.ok(marks.get("front") == holder, "front is in the holder's namespace", marks.get("front"))


HUB_STARTED = "brigade fleet hub listening on"
HUB_STOPPED = "brigade fleet hub stopped cleanly"


def clean_exit_logged(service: str = "hub", container: str | None = None, *, before_current_run: bool = False) -> bool:
    """Whether the hub run under test ended with its clean-exit line.

    The log holds every run of the container, so a clean stop earlier (say from
    `restart --case stop`) must not vouch for a later forced kill. The run under
    test is the latest one in the log, or with `before_current_run` the one that
    ended before the container's current start (a host shutdown or reboot). It
    passed only if its clean-exit line follows its own listening line.
    """
    # A stopped container is missing from plain `compose ps`, so look it up with -a.
    target = container or run(compose("ps", "-a", "-q", service)).stdout.strip() or service
    argv = ["docker", "logs"]
    if before_current_run:
        started = inspect(target, "{{.State.StartedAt}}")
        if not started:
            return False
        argv += ["--until", started]
    # Both lines go to stdout, so their order there is the order they were written.
    lines = run([*argv, target]).stdout.splitlines()
    starts = [index for index, line in enumerate(lines) if HUB_STARTED in line]
    stops = [index for index, line in enumerate(lines) if HUB_STOPPED in line]
    return bool(starts) and bool(stops) and stops[-1] > starts[-1]


# ------------------------------------------------------------------- checks


def check_ups(args: argparse.Namespace, result: Result) -> None:
    """UPS monitor and the simulated low-battery shutdown. `--phase before`, then `sudo upsmon -c fsd`, then `--phase after`."""
    if args.phase == "before":
        done = run(["upsc", args.ups, "ups.status"])
        result.ok(
            done.returncode == 0 and "OL" in done.stdout.split(), "UPS online through the monitor", done.stdout.strip()
        )
        result.ok(bool(container_id("hub")), "hub container present")
        result.add("INFO", "next", "run `sudo upsmon -c fsd`; the host shuts down. After it boots, run --phase after")
        return
    boots = run(["journalctl", "-b", "-1", "--no-pager", "-o", "cat", "-t", "upsmon", "-t", "upssched"])
    text = boots.stdout.lower()
    result.ok("forced shutdown" in text or "fsd" in text, "previous boot ended in the UPS monitor's forced shutdown")
    boot = run(["uptime", "-s"]).stdout.strip()
    result.ok(
        bool(boot) and clean_exit_logged(before_current_run=True),
        "hub run before this boot logged a clean exit",
        f"current boot {boot}",
    )
    result.ok(wait_for_front(120) is not None, "hub answers through the front after the boot")


def check_gpu(args: argparse.Namespace, result: Result) -> None:
    """GPU container toolkit: the embedder sees the GPU and the hub container has none."""
    runtimes = run(["docker", "info", "--format", "{{json .Runtimes}}"]).stdout
    result.ok("nvidia" in runtimes, "GPU container runtime registered with Docker")
    embedder = container_id("embedder")
    if not embedder:
        result.add(
            "FAIL",
            "embedder container running under the gpu profile",
            "start it: docker compose --profile gpu up -d embedder",
        )
    else:
        done = run(["docker", "exec", embedder, "nvidia-smi", "-L"])
        result.ok(done.returncode == 0 and "GPU" in done.stdout, "embedder sees the GPU", done.stdout.strip())
    for service in ("hub", "netns", "front"):
        cid = container_id(service)
        requests = inspect(cid, "{{json .HostConfig.DeviceRequests}}") if cid else "missing"
        nodes = run(["docker", "exec", cid, "sh", "-c", "ls /dev/nvidia* 2>/dev/null"]).stdout.strip() if cid else ""
        result.ok(requests in ("null", "[]") and not nodes, f"{service} container has no GPU", requests)


def check_cgroups(args: argparse.Namespace, result: Result) -> None:
    """Reservations read back from the cgroup files (effective sets, never the configured value alone)."""
    hub, embed = parse_cpus(args.hub_cpus), parse_cpus(args.embed_cpus)
    reserved = hub | embed

    def read(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            return f"unreadable: {exc}"

    hub_slice = CGROUP / "brigade.slice" / "brigade-hub.slice"
    embed_slice = CGROUP / "brigade.slice" / "brigade-embed.slice"
    effective = read(hub_slice / "cpuset.cpus.effective")
    result.ok(
        not effective.startswith("unreadable") and parse_cpus(effective) == hub,
        "hub slice cpuset.cpus.effective",
        effective,
    )
    effective = read(embed_slice / "cpuset.cpus.effective")
    result.ok(
        not effective.startswith("unreadable") and parse_cpus(effective) == embed,
        "embedder slice cpuset.cpus.effective",
        effective,
    )
    agents = [CGROUP / "user.slice"] + [CGROUP / "system.slice" / name for name in args.service]
    for path in agents:
        effective = read(path / "cpuset.cpus.effective")
        good = not effective.startswith("unreadable") and not (parse_cpus(effective) & reserved)
        result.ok(good, f"{path.relative_to(CGROUP)} excludes the reserved cores", effective)
    for path, floor, ceiling in ((hub_slice, 1 << 30, 3 << 30), (embed_slice, 2 << 30, 4 << 30)):
        low, high = read(path / "memory.min"), read(path / "memory.max")
        result.ok(
            low == str(floor) and high == str(ceiling), f"{path.name} memory.min and memory.max", f"{low} / {high}"
        )
    result.ok(
        read(CGROUP / "brigade.slice" / "memory.min") == str(3 << 30), "brigade.slice memory.min covers both children"
    )
    result.ok(read(hub_slice / "cpu.weight") == "10000", "hub slice cpu.weight", read(hub_slice / "cpu.weight"))
    hub_cid = container_id("hub")
    if hub_cid:
        pid = inspect(hub_cid, "{{.State.Pid}}")
        cgroup = read(Path(f"/proc/{pid}/cgroup"))
        result.ok("brigade-hub.slice" in cgroup, "hub container runs under the hub slice", cgroup)
    result.ok(
        run(["docker", "info", "-f", "{{.CgroupDriver}}"]).stdout.strip() == "systemd",
        "Docker uses the systemd cgroup driver",
    )


def check_io(args: argparse.Namespace, result: Result) -> None:
    """Whether the I/O weight can act on the database device, or only the latency target."""
    major_minor = args.device_id
    qos = (CGROUP / "io.cost.qos").read_text(encoding="utf-8") if (CGROUP / "io.cost.qos").exists() else ""
    enabled = any(line.startswith(major_minor) and "enable=1" in line for line in qos.splitlines())
    # The kernel applies io.latency only between peers, so the target that
    # protects the hub from agent writers sits on brigade.slice, the peer of
    # user.slice and system.slice. The hub slice's own target guards it from
    # its embedder sibling.
    for name, path in (
        ("brigade slice", CGROUP / "brigade.slice" / "io.latency"),
        ("hub slice", CGROUP / "brigade.slice" / "brigade-hub.slice" / "io.latency"),
    ):
        target = path.read_text(encoding="utf-8").strip() if path.exists() else ""
        result.ok(major_minor in target, f"{name} io.latency names the database device", target or "absent")
    if enabled:
        result.add(
            "INFO", "io.cost.qos enabled for the device: IOWeight can act", "record a fdatasync run with --writer"
        )
    else:
        result.add(
            "INFO", "no cost model on the device: only the latency target is in force", "record this in the S31 PR"
        )


def check_fdatasync(args: argparse.Namespace, result: Result) -> None:
    """The fdatasync probe: 4 KiB writes, each followed by fdatasync, for --seconds, --runs times.

    With --slice, only the probe runs in that slice (through `sudo systemd-run
    --scope`). The --writer process stays in this invocation's own cgroup, the
    caller's user session, so it competes as agent traffic outside the slice.
    Run it as the compose account, not under systemd-run.
    """
    folder = Path(args.path)
    writer = None
    if args.writer:
        # A synthetic agent writer in the caller's cgroup, outside the measured slice.
        writer = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import os,sys,time\np=sys.argv[1]\nb=os.urandom(1<<20)\nwhile True:\n"
                " f=open(p,'wb')\n for _ in range(256): f.write(b)\n f.flush(); os.fsync(f.fileno()); f.close()",
                str(Path(args.writer) / "s31-writer.bin"),
            ]
        )
    try:
        if args.slice:
            inner = [
                "sudo",
                "systemd-run",
                "--scope",
                "--quiet",
                f"--slice={args.slice}",
                sys.executable,
                str(Path(__file__).resolve()),
                "--record",
                str(Path(args.record).resolve()),
                "fdatasync",
                "--path",
                str(folder),
                "--seconds",
                str(args.seconds),
                "--runs",
                str(args.runs),
            ]
            done = run(inner, timeout=args.runs * (args.seconds + 30) + 60)
            print(done.stdout, end="")
            result.ok(
                done.returncode == 0,
                f"probe ran in {args.slice} with the writer outside it",
                done.stderr.strip()[-200:],
            )
            result.add("INFO", "writer", "on" if writer else "off")
            return
        for index in range(args.runs):
            samples = []
            fd, name = tempfile.mkstemp(prefix="s31-fdatasync-", dir=folder)
            block = os.urandom(4096)
            try:
                deadline = time.monotonic() + args.seconds
                while time.monotonic() < deadline:
                    started = time.perf_counter()
                    os.write(fd, block)
                    os.fdatasync(fd)
                    samples.append((time.perf_counter() - started) * 1000)
            finally:
                os.close(fd)
                os.unlink(name)
            load = os.getloadavg()[0]
            detail = {
                "run": index + 1,
                "writes": len(samples),
                "load": round(load, 1),
                "median_ms": round(statistics.median(samples), 2),
                "p99_ms": round(percentile(samples, 0.99), 2),
                "max_ms": round(max(samples), 2),
            }
            result.add("INFO", f"fdatasync on {folder}", detail)
    finally:
        if writer is not None:
            writer.kill()
            writer.wait()
            Path(args.writer, "s31-writer.bin").unlink(missing_ok=True)


def check_setup(args: argparse.Namespace, result: Result) -> None:
    """Setup on throwaway project copies: the paths this host offers, and that BM25 never creates an embedder."""
    import shutil

    with tempfile.TemporaryDirectory(prefix="s31-setup-") as scratch:
        for name, extra in (("gpu", []), ("remote", ["--no-gpu", *args.remote]), ("neither", ["--no-gpu"])):
            if name == "remote" and not args.remote:
                result.add("PENDING", "remote path", "pass --remote --remote-url URL --remote-token-file FILE")
                continue
            project = Path(scratch) / name / "ops" / "fleet-hub"
            shutil.copytree(
                RECIPE, project, ignore=shutil.ignore_patterns(".env", "config", "secrets", "compose.override.yaml")
            )
            done = run(
                [
                    sys.executable,
                    str(project / "bin" / "hub-setup.py"),
                    "--project-dir",
                    str(project),
                    "init",
                    "--project-name",
                    f"s31-setup-{name}",
                    "--backup-dir",
                    str(Path(scratch) / name / "backups"),
                    *extra,
                ]
            )
            toml = (project / "config" / "hub.toml").read_text(encoding="utf-8") if done.returncode == 0 else ""
            result.add("INFO", f"setup ({name})", done.stdout.strip())
            if name == "gpu":
                hybrid = 'backend = "local"' in toml
                with_reason = 'retrieval = "lexical"' in toml and "R1" in done.stdout
                result.ok(
                    hybrid or with_reason,
                    "GPU runtime: gpu profile, local backend and an R1 model, or BM25 with the reason",
                )
            elif name == "remote":
                result.ok(
                    'backend = "remote"' in toml
                    and "COMPOSE_PROFILES=\n" in (project / ".env").read_text(encoding="utf-8"),
                    "remote: probed endpoint, remote backend, no gpu profile",
                )
            else:
                result.ok(
                    'retrieval = "lexical"' in toml and "did not pass" in done.stdout,
                    "neither: BM25 mode and the failed checks",
                )
            leftovers = run(
                ["docker", "ps", "-a", "-q", "--filter", f"label=com.docker.compose.project=s31-setup-{name}"]
            ).stdout
            result.ok(not leftovers.strip(), f"setup ({name}) created no container")
    result.add(
        "PENDING",
        "status fields (mode, effective, state) per Retrieval modes",
        "needs the memory status route from S15",
    )


def check_access(args: argparse.Namespace, result: Result) -> None:
    """A host process outside the hub's namespace cannot reach the hub port or the upstream on any address."""
    hub = container_id("hub")
    holder = container_id("netns")
    if not hub or not holder:
        result.add("FAIL", "hub and holder running")
        return
    tables, inodes = split_hub_sockets(run(["docker", "exec", hub, "sh", "-c", HUB_SOCKETS]).stdout)
    every = listening_ports(tables)
    ports = listening_ports(tables, inodes) if inodes else every
    if not inodes:
        result.add("INFO", "hub socket inodes unreadable", "probing every listener in the namespace")
    result.ok(HUB_PORT in ports, "hub listens on its port inside the namespace", sorted(ports))
    if every - ports:
        result.add(
            "INFO",
            "listeners of other containers in the namespace (front's peerapi, Docker DNS)",
            sorted(every - ports),
        )
    addresses = ["127.0.0.1", "::1"] + inspect(
        holder, "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}"
    ).split()
    # Any reachable listener is a FAIL, as on any address. On host loopback an
    # operator exemption (--allow-loopback-listener PORT=EXE_REALPATH//REASON)
    # turns it into INFO only when the netns container is not on host
    # networking and publishes no port, every socket holder can be named, each
    # holder's /proc/<pid>/exe realpath equals EXE_REALPATH exactly (a deleted
    # binary reads "... (deleted)" and never matches), and no holder is the
    # hub's binary or in a container of the hub's image. The probe
    # evidence goes into the record either way and never decides the result.
    exemptions = parse_exemptions(getattr(args, "allow_loopback_listener", None) or [])
    own_namespace = inspect(holder, "{{.HostConfig.NetworkMode}}") != "host"
    bindings = inspect(holder, "{{json .HostConfig.PortBindings}}")
    image = inspect(hub, "{{.Image}}")
    hub_containers = [hub] + (
        run(["docker", "ps", "-a", "-q", "--no-trunc", "--filter", f"ancestor={image}"]).stdout.split() if image else []
    )
    used: set[int] = set()
    for address in addresses:
        for port in sorted(ports):
            name = f"host cannot connect to {address}:{port}"
            if connect_refused(address, port):
                result.add("PASS", name)
                continue
            detail: dict[str, Any] = {"evidence": probe_evidence(address, port)}
            if address not in ("127.0.0.1", "::1") or port not in exemptions:
                result.add("FAIL", name, detail)
                continue
            used.add(port)
            expected_exe, reason = exemptions[port]
            detail["exemption"] = {"exe": expected_exe, "reason": reason}
            holders = loopback_holders(port)
            detail["holders"] = [{key: h[key] for key in ("pid", "exe", "comm")} for h in holders or []]
            if not own_namespace or publishes_any_port(bindings):
                detail["exemption_applied"] = "no: the netns container uses host networking or publishes a port"
            elif holders is None:
                detail["exemption_applied"] = "no: the socket's holder process could not be identified"
            elif any(h["exe"] != expected_exe for h in holders):
                detail["exemption_applied"] = "no: a holder's executable is not the exempted path (or was deleted)"
            elif any(hub_image_holder(h, hub_containers) for h in holders):
                detail["exemption_applied"] = "no: a holder runs the hub's binary or image"
            else:
                detail["exemption_applied"] = "yes"
                result.add("INFO", name, detail)
                continue
            result.add("FAIL", name, detail)
    for port in sorted(set(exemptions) - used):
        result.add(
            "INFO", f"exemption for port {port} not used", {"exe": exemptions[port][0], "reason": exemptions[port][1]}
        )
    status: int | str = 0
    try:
        status = get(front_url() + "/status", headers={"Tailscale-User-Login": FORGED_LOGIN})[0]
    except OSError as exc:
        status = str(exc)
    result.ok(status in (401, 403), "a forged identity header without a token gains nothing through the front", status)


ECHO_SERVER = (
    "import http.server, json\n"
    "class H(http.server.BaseHTTPRequestHandler):\n"
    "    def do_GET(self):\n"
    "        body = json.dumps(dict(self.headers.items())).encode()\n"
    "        self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers()\n"
    "        self.wfile.write(body)\n"
    "    def log_message(self, *a): pass\n"
    f"http.server.HTTPServer(('127.0.0.1', {ECHO_PORT}), H).serve_forever()\n"
)


def evaluate_echo(headers: dict[str, str]) -> dict[str, Any]:
    lowered = {key.lower(): value for key, value in headers.items()}
    login = lowered.get("tailscale-user-login")
    forwarded = lowered.get("x-forwarded-for", "")
    return {
        "login_forged": login == FORGED_LOGIN,
        "login_seen": login,
        "forwarded_for": forwarded,
        "client_value_kept": FORGED_FOR in forwarded,
    }


def check_front(args: argparse.Namespace, result: Result) -> None:
    """Forged identity header through the real front, the front's SSH server, and its forwarded-for handling.

    Changes state for the test only: starts a header echo on 127.0.0.1:3775 in the
    hub's namespace and a serve path to it, and removes both afterwards.
    """
    front, holder = container_id("front"), container_id("netns")
    image = env_file().get("BRIGADE_HUB_IMAGE", "localhost/brigade-fleet-hub:dogfood")
    prefs = run(["docker", "exec", front, "tailscale", "debug", "prefs"]).stdout
    try:
        run_ssh = json.loads(prefs).get("RunSSH")
    except ValueError:
        run_ssh = None
    result.ok(run_ssh is False, "front's node runs with its SSH server off", run_ssh)
    echo = run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            "s31-header-echo",
            "--network",
            f"container:{holder}",
            "--entrypoint",
            "python",
            image,
            "-c",
            ECHO_SERVER,
        ]
    )
    if echo.returncode != 0:
        result.add("FAIL", "header echo started", echo.stderr.strip())
        return
    try:
        run(
            [
                "docker",
                "exec",
                front,
                "tailscale",
                "serve",
                "--bg",
                "--set-path",
                ECHO_PATH,
                f"http://127.0.0.1:{ECHO_PORT}",
            ],
            check=True,
        )
        time.sleep(2)
        status, body = get(
            front_url() + ECHO_PATH, headers={"Tailscale-User-Login": FORGED_LOGIN, "X-Forwarded-For": FORGED_FOR}
        )
        seen = evaluate_echo(json.loads(body)) if status == 200 else {}
        result.ok(status == 200 and not seen["login_forged"], "forged Tailscale-User-Login replaced or removed", seen)
        result.add("INFO", "front's handling of a client X-Forwarded-For", seen.get("forwarded_for"))
    finally:
        run(["docker", "exec", front, "tailscale", "serve", "--set-path", ECHO_PATH, "off"])
        run(["docker", "rm", "-f", "s31-header-echo"])


def outcome_name(exc: BaseException) -> str:
    """Name a failed request. urlopen wraps a refused connect in URLError, so unwrap it."""
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, ConnectionRefusedError):
        return "refused"
    return type(reason).__name__ if isinstance(reason, BaseException) else type(exc).__name__


def bench_summary(stdout: str) -> Any:
    """The load harness report without its per-attempt records, which would crowd out
    the summary and conditions. Falls back to the raw tail if no report parses."""
    text = stdout.strip()
    decoder = json.JSONDecoder()
    starts = [0] + [i + 1 for i, ch in enumerate(text) if ch == "\n"]
    for start in reversed(starts):
        if not text.startswith("{", start):
            continue
        try:
            report, _ = decoder.raw_decode(text, start)
        except ValueError:
            continue
        if isinstance(report, dict):
            attempts = report.pop("attempts", None)
            if isinstance(attempts, list):
                report["attempt_count"] = len(attempts)
            return report
    return text[-2000:]


def claim_floor(db: Path) -> int:
    """The claim recovery floor a restore wrote into `db` (0 when none).

    The same hub_meta row fleet_hub_claims.recovery_floor reads; this check is
    stdlib only, so it reads the table directly.
    """
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT value FROM hub_meta WHERE key = 'recovery'").fetchone()
    finally:
        conn.close()
    if row is None:
        return 0
    value = json.loads(row[0])["claim_floor"]
    if type(value) is not int or value < 0:
        raise ValueError(f"corrupt claim recovery floor in {db}: {value!r}")
    return value


def restore_drive_script(operator: str, floor: int) -> str:
    """Serve the restored file and run the contract drive against it. The drive
    enrolls an operator and creates questions, which the hub refuses unless it
    has the same fresh operator recipient in its environment. A restore raised
    the claim recovery floor, so the drive expects new claims at floor + 1."""
    return (
        f"BRIGADE_FLEET_OPERATOR_RECIPIENT={operator} "
        "brigade fleet serve --host 127.0.0.1 --port 3774 --db /data/fleet-hub.db --token-file /data/token & "
        "sleep 5; BRIGADE_HUB_CONTRACT_ADMIN_TOKEN=$(cat /data/token) "
        f"python {DRIVE}/control-brigade.py hub-contract --hub-url http://127.0.0.1:3774 "
        f"--operator-recipient {operator} --claim-floor {int(floor)}"
    )


def check_load(args: argparse.Namespace, result: Result) -> None:
    """Concurrent GET /health through the real front: refused connections and latency at the fleet's concurrency."""
    url = front_url() + "/health"

    def one(_: int) -> tuple[str, float]:
        started = time.perf_counter()
        try:
            status, _ = get(url, timeout=10)
            return ("ok" if status == 200 else f"http-{status}"), (time.perf_counter() - started) * 1000
        except (OSError, http.client.HTTPException) as exc:
            return outcome_name(exc), 0.0

    outcomes: dict[str, int] = {}
    latencies = []
    deadline = time.monotonic() + args.seconds
    with concurrent.futures.ThreadPoolExecutor(args.concurrency) as pool:
        while time.monotonic() < deadline:
            for outcome, ms in pool.map(one, range(args.concurrency)):
                outcomes[outcome] = outcomes.get(outcome, 0) + 1
                if outcome == "ok":
                    latencies.append(ms)
    detail = {
        "concurrency": args.concurrency,
        "outcomes": outcomes,
        "p50_ms": round(percentile(latencies, 0.5), 1),
        "p99_ms": round(percentile(latencies, 0.99), 1),
    }
    result.add("INFO", "load through the front", detail)
    result.add("INFO", "refused connections", outcomes.get("refused", 0))


# Run inside the hub container by `restart --case hub-kill`. `docker kill`
# marks a container manually stopped, so the restart policy ignores it and the
# watchdog skips it. A crash is the hub's own process dying instead: the Rust
# front, `brigade-hub`, which owns the port and supervises the Python hub.
# docker-init is PID 1 (compose `init: true`), and a namespace's init ignores
# SIGKILL sent from inside the namespace, so this signals the front, two levels
# down under the recipe's entrypoint. docker exec runs as the image's USER
# 10001, the front's own uid, so the kill needs no CAP_KILL (compose drops
# every capability). The entrypoint then exits non-zero, docker-init exits with
# it, and `restart: unless-stopped` starts the container again.
KILL_HUB = """
import os, signal
me = os.getpid()
for name in os.listdir("/proc"):
    if not name.isdigit() or int(name) in (1, me):
        continue
    try:
        with open(f"/proc/{name}/cmdline", "rb") as handle:
            argv = handle.read().split(b"\\0")
    except OSError:
        continue
    if os.path.basename(argv[0]) == b"brigade-hub":
        os.kill(int(name), signal.SIGKILL)
        print(name)
        break
else:
    raise SystemExit("no brigade-hub process in the container")
"""


def restart_count(container: str) -> int:
    text = inspect(container, "{{.RestartCount}}")
    return int(text) if text.isdigit() else -1


def hub_kill(result: Result, limit: float = 60) -> None:
    """SIGKILL the hub's Rust front, then expect the restart policy and the front to bring it back."""
    hub = container_id("hub")
    before = restart_count(hub)
    killed = run(["docker", "exec", hub, "python", "-c", KILL_HUB])
    started = time.monotonic()
    result.ok(
        killed.returncode == 0,
        "SIGKILL to the hub's brigade-hub process",
        (killed.stdout + killed.stderr).strip()[:200],
    )
    after = before
    while time.monotonic() - started < limit:
        after = restart_count(hub)
        if after > before:
            break
        time.sleep(1)
    restarted = result.ok(
        after > before, "restart policy restarted the hub container", f"RestartCount {before} -> {after}"
    )
    took = None
    if restarted:
        rest = wait_for_front(max(0.0, limit - (time.monotonic() - started)))
        took = None if rest is None else round(time.monotonic() - started, 1)
    result.ok(took is not None, "request through the front within 60 s of killing the hub process", took)
    if took is None:
        # Leave the host serving: start the hub, and the front if it exited too.
        services = ["hub"]
        front = container_id("front")
        if not front or inspect(front, "{{.State.Running}}") != "true":
            services.append("front")
        up = run(compose("up", "-d", "--no-build", *services), timeout=300)
        result.add(
            "INFO", "recovery failed; service restored with compose up", {"services": services, "rc": up.returncode}
        )


def check_restart(args: argparse.Namespace, result: Result) -> None:
    """Restart cases. Every case but `after-reboot` changes state: it stops, kills or restarts containers or the daemon."""
    case = args.case
    if case == "stop":
        hub = container_id("hub")
        started = time.monotonic()
        run(compose("stop", "hub"), timeout=120)
        result.ok(
            inspect(hub, "{{.State.ExitCode}}") == "0",
            "docker stop ends the hub with exit 0",
            f"{time.monotonic() - started:.1f}s",
        )
        result.ok(clean_exit_logged(container=hub), "hub logged a clean exit")
        run(compose("up", "-d", "--no-build"), timeout=300)
    elif case == "hub-kill":
        hub_kill(result)
    elif case == "holder-restart":
        run(["docker", "restart", container_id("netns")])
        took = wait_for_front(90)
        result.ok(took is not None, "request through the front within 90 s of the holder restart", took)
    elif case == "daemon-restart":
        run(["sudo", "systemctl", "restart", "docker"], timeout=300)
        took = wait_for_front(90)
        result.ok(took is not None, "request through the front within 90 s of the daemon being up", took)
    elif case == "after-reboot":
        active = run(["systemctl", "show", "docker", "-p", "ActiveEnterTimestampMonotonic", "--value"]).stdout.strip()
        boot_ok = run(["systemctl", "is-active", "brigade-hub-compose.service"]).stdout.strip() == "active"
        result.ok(boot_ok, "boot unit ran after the Docker service")
        starts = {}
        for service in ("netns", "hub", "front"):
            cid = container_id(service)
            starts[service] = inspect(cid, "{{.State.StartedAt}}") if cid else "not running"
        result.add("INFO", "container start times", {"docker_active_monotonic_us": active, **starts})
        result.ok(wait_for_front(90) is not None, "request through the front answers")
        result.ok(clean_exit_logged(before_current_run=True), "hub run before the reboot logged a clean exit")
        flush = run(["brigade", "fleet", "flush"])
        result.add("INFO", "dogfood node spool flush", (flush.stdout + flush.stderr).strip()[:400])
    one_namespace(result)


# Run inside the hub container by `memory`, on loopback in the hub's own
# namespace, so the admin token never leaves the container and never reaches
# this process's output. Prints one JSON line without the response body.
MEMORY_STATUS = """
import json, os, urllib.error, urllib.request
with open("/run/secrets/admin_token", encoding="utf-8") as handle:
    token = handle.read().strip()
request = urllib.request.Request(
    "http://127.0.0.1:3774/memory",
    data=json.dumps({"action": "status"}).encode("utf-8"),
    method="POST",
    headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
)
try:
    with urllib.request.urlopen(request, timeout=10) as response:
        status, body = response.status, response.read()
except urllib.error.HTTPError as exc:
    status, body = exc.code, exc.read()
try:
    parsed = json.loads(body)
except ValueError:
    parsed = None
parsed = parsed if isinstance(parsed, dict) else {}
print(json.dumps({
    "status": status,
    "error": parsed.get("error"),
    "reason": parsed.get("reason"),
    "log_epoch": bool(parsed.get("log_epoch")),
    "memory_file": os.path.isfile("/var/lib/brigade/fleet-hub-memory.db"),
}))
"""


def check_memory(args: argparse.Namespace, result: Result) -> None:
    """Hub memory: POST /memory with the admin token and the `status` action answers 200."""
    hub = container_id("hub")
    if not result.ok(bool(hub), "hub container running"):
        return
    done = run(["docker", "exec", hub, "python", "-c", MEMORY_STATUS])
    try:
        report = json.loads(done.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        result.add("FAIL", "memory status probe ran", (done.stdout + done.stderr).strip()[:200])
        return
    detail: dict[str, Any] = {key: report.get(key) for key in ("status", "error", "reason")}
    if report.get("status") == 404:
        detail["hint"] = "404 comes from the Python upstream: is --route-mode memory=native in the hub's command?"
    result.ok(report.get("status") == 200, "POST /memory status answers 200 with the admin token", detail)
    result.ok(report.get("log_epoch") is True, "status reports the memory file's log_epoch")
    result.ok(report.get("memory_file") is True, "fleet-hub-memory.db sits beside fleet-hub.db on the state volume")


MEMORY_FILE_PRESENT = 'import os; print(os.path.isfile("/var/lib/brigade/fleet-hub-memory.db"))'


def memory_file_present() -> bool | None:
    """Whether fleet-hub-memory.db is on the hub's state volume; None when the hub is not running to ask."""
    hub = container_id("hub")
    if not hub:
        return None
    done = run(["docker", "exec", hub, "python", "-c", MEMORY_FILE_PRESENT])
    answer = done.stdout.strip()
    return {"True": True, "False": False}.get(answer) if done.returncode == 0 else None


def verify_copy(copy: Path) -> dict[str, Any]:
    manifest = json.loads((copy / "manifest.json").read_text(encoding="utf-8"))
    files = {}
    for entry in manifest["files"]:
        conn = sqlite3.connect(f"file:{copy / entry['name']}?mode=ro", uri=True)
        try:
            files[entry["name"]] = {
                "bytes": entry["bytes"],
                "integrity_check": conn.execute("PRAGMA integrity_check").fetchone()[0],
            }
        finally:
            conn.close()
    return files


def completed_copies(folder: Path) -> list[Path]:
    """Copy directories that have a manifest, oldest first."""
    if not folder.is_dir():
        return []
    return sorted(path for path in folder.iterdir() if path.is_dir() and (path / "manifest.json").exists())


def check_backups(args: argparse.Namespace, result: Result) -> None:
    """Hourly copies, their integrity, the directory's owner and mode, the off-host include set and retention.

    The job copies fleet-hub.db into the backup directory and, in a separate
    call, fleet-hub-memory.db into its `memory` subdirectory, so the hub
    file's copies are checked on their own. No memory copies fail the check
    only while the memory file exists on the state volume: then the memory
    call has failed on every run. An absent memory file fails nothing here.
    """
    folder = Path(args.backup_dir or env_file().get("BRIGADE_BACKUP_DIR", "")).expanduser()
    info = folder.stat()
    result.ok(stat.S_IMODE(info.st_mode) == 0o700, "backup directory mode 0700", oct(stat.S_IMODE(info.st_mode)))
    owner = env_file().get("BRIGADE_BACKUP_OWNER", f"{os.getuid()}:{os.getgid()}")
    result.ok(str(info.st_uid) == owner.split(":")[0], "backup directory belongs to the compose account", info.st_uid)
    copies = completed_copies(folder)
    result.ok(bool(copies), "at least one copy exists", len(copies))
    for copy in copies[-args.last :]:
        files = verify_copy(copy)
        result.ok(
            all(item["integrity_check"] == "ok" for item in files.values()),
            f"{copy.name} passes integrity_check",
            files,
        )
    memory_copies = completed_copies(folder / "memory")
    if not memory_copies:
        present = memory_file_present()
        if present is None:
            result.add(
                "PENDING", "memory copies", "none, and the hub is not running to say whether the memory file exists"
            )
        elif present:
            result.add(
                "FAIL",
                "memory copies",
                "none, but fleet-hub-memory.db exists on the state volume: the memory call fails on every run",
            )
        else:
            result.add("INFO", "memory copies", "none: the memory file is absent from the state volume")
    for copy in memory_copies[-args.last :]:
        files = verify_copy(copy)
        result.ok(
            set(files) == {"fleet-hub-memory.db"} and all(item["integrity_check"] == "ok" for item in files.values()),
            f"memory/{copy.name} passes integrity_check",
            files,
        )
    stamps = [copy.name for copy in copies]
    hours = sorted({stamp[:11] for stamp in stamps})
    result.add(
        "INFO", "copies kept", {"count": len(stamps), "distinct_hours": len(hours), "newest": stamps[-1:] or None}
    )
    if args.include_file:
        text = Path(args.include_file).read_text(encoding="utf-8")
        result.ok(str(folder) in text, "backup directory is in the off-host job's include set", args.include_file)
    else:
        result.add("PENDING", "off-host include set", "pass --include-file with the off-host job's include list")
    if args.refusal:
        done = run(
            compose(
                "--profile",
                "backup",
                "run",
                "--rm",
                "--no-deps",
                "backup",
                "--dest",
                "/backups",
                "--owner",
                env_file()["BRIGADE_BACKUP_OWNER"],
                "--min-free-gib",
                "1000000",
                "--json",
            ),
            timeout=300,
        )
        after = [path.name for path in completed_copies(folder)]
        memory_after = [path.name for path in completed_copies(folder / "memory")]
        result.ok(
            done.returncode != 0
            and "refusing copy" in done.stderr
            and after == [c.name for c in copies]
            and memory_after == [c.name for c in memory_copies],
            "a copy past the free-space floor is refused and removes nothing",
        )


def check_restore(args: argparse.Namespace, result: Result) -> None:
    """Restore tool on a real copy, twice, into a throwaway directory: every counter strictly rises.

    Then the full drive against the restored copy in a throwaway container with no network.
    """
    folder = Path(args.backup_dir or env_file().get("BRIGADE_BACKUP_DIR", "")).expanduser()
    copies = sorted(path for path in folder.iterdir() if path.is_dir() and (path / "manifest.json").exists())
    copy = copies[-1] / "fleet-hub.db"
    with tempfile.TemporaryDirectory(prefix="s31-restore-") as scratch:
        ledger = Path(scratch) / "recovery-ledger.jsonl"
        # The tool counts only a ledger file that exists and never creates one
        # without --assert-no-prior-restore, the way setup creates the real one.
        os.close(os.open(ledger, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        reports = []
        for index in (1, 2):
            target = Path(scratch) / f"restored-{index}.db"
            done = run(
                [
                    "brigade",
                    "fleet",
                    "restore",
                    "--copy",
                    str(copy),
                    "--db",
                    str(target),
                    "--ledger",
                    str(ledger),
                    "--json",
                ],
                timeout=300,
            )
            result.ok(done.returncode == 0, f"restore {index} from {copy.parent.name}", done.stderr.strip()[:200])
            if done.returncode != 0:
                return
            reports.append(json.loads(done.stdout))
        for field in ("message_sequence", "claim_floor", "roster_revision", "policy_revision"):
            result.ok(
                reports[1][field] > reports[0][field],
                f"second restore's {field} is higher",
                (reports[0][field], reports[1][field]),
            )
        result.ok(reports[0]["claims_ended"] >= 0, "claims ended", reports[0]["claims_ended"])
        result.ok(len(ledger.read_text(encoding="utf-8").splitlines()) == 2, "ledger holds one record per restore")
        no_ledger = run(
            [
                "brigade",
                "fleet",
                "restore",
                "--copy",
                str(copy),
                "--db",
                str(Path(scratch) / "x.db"),
                "--ledger",
                str(Path(scratch) / "missing" / "dir" / "ledger.jsonl"),
            ]
        )
        result.ok(
            no_ledger.returncode == 1 and "no recovery ledger" in no_ledger.stderr,
            "with no reachable ledger and no flag, the tool refuses",
        )
        if args.drive:
            image = env_file().get("BRIGADE_HUB_IMAGE", "localhost/brigade-fleet-hub:dogfood")
            data = Path(scratch) / "data"
            data.mkdir()
            (Path(scratch) / "restored-2.db").rename(data / "fleet-hub.db")
            (data / "token").write_text("fake-restore-drive-token\n", encoding="utf-8")
            # The restored file belongs to this account and is mode 0600, so the
            # throwaway hub runs as this account rather than the image's UID.
            floor = claim_floor(data / "fleet-hub.db")
            if not result.ok(
                floor == reports[1]["claim_floor"],
                "restored copy holds the floor the restore reported",
                (floor, reports[1]["claim_floor"]),
            ):
                return
            script = restore_drive_script(f"agent:restore-drive-{secrets.token_hex(4)}", floor)
            done = run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--user",
                    f"{os.getuid()}:{os.getgid()}",
                    "-e",
                    "HOME=/data",
                    "-v",
                    f"{data}:/data",
                    "-v",
                    f"{REPO / 'registry' / 'skills' / 'verify-brigade'}:{DRIVE}:ro",
                    "--entrypoint",
                    "sh",
                    image,
                    "-c",
                    script,
                ],
                timeout=1800,
            )
            result.ok(done.returncode == 0, "full drive passes against the restored copy", done.stdout.strip()[-400:])
        else:
            result.add("PENDING", "full drive against a restored copy", "rerun with --drive")


def check_commit(args: argparse.Namespace, result: Result) -> None:
    """FULL commit latency of single-event writes on a throwaway Python hub inside the hub slice.

    Uses the S6a load harness against a disposable database, never the live one.
    With --fanout it holds N busy processes outside the slice while it measures.
    """
    image = env_file().get("BRIGADE_HUB_IMAGE", "localhost/brigade-fleet-hub:dogfood")
    busy = []
    for _ in range(args.fanout):
        busy.append(subprocess.Popen([sys.executable, "-c", "while True: pass"]))
    try:
        script = (
            "printf 'fake-bench-token\\n' > /tmp/token; "
            "brigade fleet serve --host 127.0.0.1 --port 3774 --db /tmp/bench.db --token-file /tmp/token "
            "--allow-admin-writes & sleep 4; "
            "BRIGADE_BENCH_TOKEN=fake-bench-token "
            f"python /bench/load.py --url http://127.0.0.1:3774 --output /tmp/out.json --target python "
            f"--database /tmp/bench.db --seconds {args.seconds} --clients {args.clients} --mode closed "
            "&& cat /tmp/out.json"
        )
        done = run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--cgroup-parent",
                env_file().get("BRIGADE_HUB_SLICE", "brigade-hub.slice"),
                "-v",
                f"{REPO / 'engines' / 'hub' / 'bench'}:/bench:ro",
                "--entrypoint",
                "sh",
                image,
                "-c",
                script,
            ],
            timeout=args.seconds + 300,
        )
        result.ok(done.returncode == 0, "throwaway hub load run", done.stderr.strip()[-300:])
        result.add("INFO", "load average during the run", round(os.getloadavg()[0], 1))
        result.add("INFO", "load harness report", bench_summary(done.stdout))
    finally:
        for process in busy:
            process.kill()
            process.wait()


def check_probe(args: argparse.Namespace, result: Result) -> None:
    """The S29 read-only probe against the dogfood hub, from a container in its namespace, with latency.

    Needs BRIGADE_HUB_PROBE_NODE_TOKEN and BRIGADE_HUB_PROBE_PRINCIPAL_TOKEN in the
    environment: a node token and the probe principal's token on the dogfood hub.
    """
    holder = container_id("netns")
    image = env_file().get("BRIGADE_HUB_IMAGE", "localhost/brigade-fleet-hub:dogfood")
    timings = []
    for _ in range(args.runs):
        started = time.perf_counter()
        done = run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                f"container:{holder}",
                "-v",
                f"{REPO / 'registry' / 'skills' / 'verify-brigade'}:{DRIVE}:ro",
                "-e",
                "BRIGADE_HUB_PROBE_NODE_TOKEN",
                "-e",
                "BRIGADE_HUB_PROBE_PRINCIPAL_TOKEN",
                "--entrypoint",
                "python",
                image,
                f"{DRIVE}/control-brigade.py",
                "hub-contract",
                "--profile",
                "prod-readonly",
                "--hub-url",
                f"http://127.0.0.1:{HUB_PORT}",
            ],
            timeout=600,
        )
        timings.append(time.perf_counter() - started)
        result.ok(done.returncode == 0, "S29 read-only probe passes", done.stdout.strip()[-200:])
    result.add(
        "INFO", "probe wall time (s)", {"median": round(statistics.median(timings), 2), "max": round(max(timings), 2)}
    )


def check_hardening(args: argparse.Namespace, result: Result) -> None:
    """Run as the throwaway unprivileged account: every privileged path must fail."""
    result.ok(run(["sudo", "-n", "true"]).returncode != 0, "sudo -n true fails")
    result.ok(run(["docker", "ps"]).returncode != 0, "docker ps fails")
    result.ok(
        run(["nsenter", "-t", str(args.hub_pid), "-n", "true"]).returncode != 0, "entering the hub's namespace fails"
    )
    for path in (args.volume, args.backup_dir):
        try:
            os.listdir(path)
            readable = True
        except OSError:
            readable = False
        result.ok(not readable, f"{path} is unreadable")


def check_summary(args: argparse.Namespace, result: Result) -> None:
    """Count recorded runs per check, for the three-times items."""
    path = Path(args.record)
    counts: dict[str, dict[str, int]] = {}
    for line in path.read_text(encoding="utf-8").splitlines() if path.exists() else []:
        record = json.loads(line)
        key = record["check"] + (f":{record['case']}" if record.get("case") else "")
        counts.setdefault(key, {}).setdefault(record["status"], 0)
        counts[key][record["status"]] += 1
    for key in sorted(counts):
        result.add("INFO", key, counts[key])
    for key in ("restart:after-reboot", "restart:daemon-restart"):
        passes = counts.get(key, {}).get("pass", 0)
        result.ok(passes >= 3, f"{key} passed at least 3 times", passes)


CHECKS = {
    "ups": check_ups,
    "gpu": check_gpu,
    "cgroups": check_cgroups,
    "io": check_io,
    "fdatasync": check_fdatasync,
    "setup": check_setup,
    "access": check_access,
    "front": check_front,
    "load": check_load,
    "restart": check_restart,
    "memory": check_memory,
    "backups": check_backups,
    "restore": check_restore,
    "commit": check_commit,
    "probe": check_probe,
    "hardening": check_hardening,
    "summary": check_summary,
}


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--record", default="s31-results.jsonl", help="JSON-lines file each run appends to.")
    sub = p.add_subparsers(dest="check", required=True)
    for name, func in CHECKS.items():
        q = sub.add_parser(name, help=(func.__doc__ or "").splitlines()[0])
        if name == "ups":
            q.add_argument("--phase", choices=("before", "after"), required=True)
            q.add_argument("--ups", default="workstation@localhost")
        elif name == "cgroups":
            q.add_argument("--hub-cpus", required=True)
            q.add_argument("--embed-cpus", required=True)
            q.add_argument("--service", action="append", default=[], help="Agent or model service unit; repeat.")
        elif name == "io":
            q.add_argument("--device-id", required=True, help="MAJOR:MINOR of the database device (lsblk -o MAJ:MIN).")
        elif name == "fdatasync":
            q.add_argument("--path", required=True, help="Directory on the hub's data volume.")
            q.add_argument("--seconds", type=float, default=6)
            q.add_argument("--runs", type=int, default=2)
            q.add_argument("--writer", default=None, help="Directory for a synthetic agent writer during the runs.")
            q.add_argument("--slice", default=None, help="Run only the probe in this slice, e.g. brigade-hub.slice.")
        elif name == "setup":
            q.add_argument(
                "--remote", nargs=argparse.REMAINDER, default=[], help="--remote-url URL --remote-token-file FILE"
            )
        elif name == "load":
            q.add_argument("--concurrency", type=int, default=64)
            q.add_argument("--seconds", type=float, default=30)
        elif name == "restart":
            q.add_argument(
                "--case",
                choices=("stop", "hub-kill", "holder-restart", "daemon-restart", "after-reboot"),
                required=True,
            )
        elif name == "backups":
            q.add_argument("--backup-dir", default=None)
            q.add_argument("--last", type=int, default=48)
            q.add_argument("--include-file", default=None)
            q.add_argument(
                "--refusal", action="store_true", help="Also run a copy that the free-space floor must refuse."
            )
        elif name == "restore":
            q.add_argument("--backup-dir", default=None)
            q.add_argument("--drive", action="store_true")
        elif name == "commit":
            q.add_argument("--seconds", type=float, default=60)
            q.add_argument("--clients", type=int, default=16)
            q.add_argument(
                "--fanout", type=int, default=0, help="Busy processes outside the slice (aim for load near 170)."
            )
        elif name == "probe":
            q.add_argument("--runs", type=int, default=5)
        elif name == "access":
            q.add_argument(
                "--allow-loopback-listener",
                action="append",
                default=[],
                metavar="PORT=EXE_REALPATH//REASON",
                help="Exempt a host loopback listener on PORT held only by the program at EXE_REALPATH; repeat.",
            )
        elif name == "hardening":
            q.add_argument("--volume", required=True)
            q.add_argument("--backup-dir", required=True)
            q.add_argument("--hub-pid", type=int, required=True)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    result = Result(args.check)
    try:
        CHECKS[args.check](args, result)
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        result.add("FAIL", "check could not complete", str(exc))
    record = {
        "check": args.check,
        "case": getattr(args, "case", None),
        "status": result.status,
        "at": datetime.now(timezone.utc).isoformat(),
        "items": result.items,
    }
    if args.check != "summary":
        with open(args.record, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    print(f"{args.check}: {result.status.upper()}")
    return 0 if result.status != "fail" else 1


if __name__ == "__main__":
    sys.exit(main())
