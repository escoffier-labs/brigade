"""Managed tools: external CLIs Brigade can install, wire, and health-check.

Each tool attaches to a station. The core never imports these tools; it shells
out via brigade.proc. Absent tools are reported as MANUAL (a hint to install),
never as a hard failure.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from . import proc
from .doctor import OK, WARN, FAIL, MANUAL
from .station import CheckResult, DoctorContext


@dataclass(frozen=True)
class ManagedTool:
    name: str            # e.g. "memory-doctor"
    station: str         # "memory" | "guard" | "tokens"
    command: str         # the binary name to detect on PATH
    summary: str
    install_args: List[str]                      # argv to install (pipx/npm/pip)
    wire: Callable[[DoctorContext], List[CheckResult]]   # lay config; returns notes
    doctor: Callable[[DoctorContext], List[CheckResult]] # health via proc

    def detect(self) -> bool:
        return proc.which(self.command) is not None


# ---- adapters -------------------------------------------------------------

def _noop_wire(ctx: DoctorContext) -> List[CheckResult]:
    return []


def _memory_doctor_doctor(ctx: DoctorContext) -> List[CheckResult]:
    r = proc.run(["memory-doctor", "status", "--json"])
    if r.code == 2:
        return [(WARN, "memory-doctor", "installed but unwired (memory/handoffs dir missing)")]
    data = r.json()
    if data is None:
        return [(WARN, "memory-doctor", f"unexpected output (exit {r.code})")]
    dead = data.get("dead_links", 0)
    status = WARN if dead else OK
    return [(status, "memory-doctor", f"cards={data.get('cards')}, dead_links={dead}, pending={data.get('pending_handoffs')}")]


def _bootstrap_doctor_doctor(ctx: DoctorContext) -> List[CheckResult]:
    r = proc.run(["bootstrap-doctor", "status", "--json"])
    data = r.json()
    if data is None:
        return [(WARN, "bootstrap-doctor", f"installed but unwired or errored (exit {r.code})")]
    rows = data.get("rows", [])
    bad = [row for row in rows if row.get("severity") in ("hard", "missing", "unreadable")]
    soft = [row for row in rows if row.get("severity") == "soft"]
    if bad:
        return [(FAIL, "bootstrap-doctor", f"{len(bad)} file(s) over hard limit / missing")]
    if soft:
        return [(WARN, "bootstrap-doctor", f"{len(soft)} file(s) in soft band")]
    return [(OK, "bootstrap-doctor", f"{len(rows)} bootstrap file(s) within limits")]


def _content_guard_doctor(ctx: DoctorContext) -> List[CheckResult]:
    # A "tool present + policy loads" check: scan this plan's own clean string.
    r = proc.run(["content-guard", "scan", "--policy", "public-repo", "--json"], env=None)
    data = r.json()
    if data is None and r.code not in (0, 1):
        return [(WARN, "content-guard", f"installed but not runnable (exit {r.code})")]
    return [(OK, "content-guard", "installed; public-repo policy loads")]


def _content_guard_wire(ctx: DoctorContext) -> List[CheckResult]:
    # content-guard ships bundled policies; nothing to lay down for the default.
    return [(OK, "content-guard: policy", "using bundled public-repo policy")]


def _tokenjuice_doctor(ctx: DoctorContext) -> List[CheckResult]:
    r = proc.run(["tokenjuice", "doctor", "hooks", "--format", "json"])
    data = r.json()
    if data is None:
        return [(WARN, "tokenjuice", f"installed but doctor output unreadable (exit {r.code})")]
    status = data.get("status", "unknown")
    mapping = {"ok": OK, "warn": WARN, "disabled": MANUAL, "broken": FAIL}
    return [(mapping.get(status, WARN), "tokenjuice", f"hook status: {status}")]


def _tokenjuice_wire(ctx: DoctorContext) -> List[CheckResult]:
    # Wiring installs a host hook; which host depends on the workspace's harnesses.
    hosts = [h for h in ctx.harnesses if h in ("claude", "codex", "cursor")]
    if not hosts:
        return [(MANUAL, "tokenjuice: wire", "no hookable harness selected; run `tokenjuice install <host>` manually")]
    notes: List[CheckResult] = []
    for h in hosts:
        host = "claude-code" if h == "claude" else h
        r = proc.run(["tokenjuice", "install", host])
        notes.append((OK if r.code == 0 else WARN, f"tokenjuice: install {host}", r.stderr.strip()[:80] or "installed"))
    return notes


# ---- registry -------------------------------------------------------------

_TOOLS: Tuple[ManagedTool, ...] = (
    ManagedTool(
        name="memory-doctor", station="memory", command="memory-doctor",
        summary="memory index health, dead-link lint, handoff counts",
        install_args=["pipx", "install", "git+https://github.com/solomonneas/memory-doctor"],
        wire=_noop_wire, doctor=_memory_doctor_doctor,
    ),
    ManagedTool(
        name="bootstrap-doctor", station="memory", command="bootstrap-doctor",
        summary="bootstrap-file size/limit audit",
        install_args=["pipx", "install", "git+https://github.com/solomonneas/bootstrap-doctor"],
        wire=_noop_wire, doctor=_bootstrap_doctor_doctor,
    ),
    ManagedTool(
        name="content-guard", station="guard", command="content-guard",
        summary="policy-driven content scanning",
        install_args=["pipx", "install", "git+https://github.com/solomonneas/content-guard"],
        wire=_content_guard_wire, doctor=_content_guard_doctor,
    ),
    ManagedTool(
        name="tokenjuice", station="tokens", command="tokenjuice",
        summary="output compaction via host hooks",
        install_args=["npm", "install", "-g", "tokenjuice"],
        wire=_tokenjuice_wire, doctor=_tokenjuice_doctor,
    ),
)


def all_tools() -> Tuple[ManagedTool, ...]:
    return _TOOLS


def for_station(station: str) -> Tuple[ManagedTool, ...]:
    return tuple(t for t in _TOOLS if t.station == station)


def resolve(name: str) -> Optional[ManagedTool]:
    for t in _TOOLS:
        if t.name == name:
            return t
    return None
