#!/usr/bin/env python3
"""Safely collect portable harness-contract.v1 availability evidence.

Default mode resolves bare executable availability only. Pass ``--run-version`` to
execute ``<binary> --version`` in an isolated temporary cwd and temporary HOME.
Even with those guards, executing a third-party binary can still read ambient
environment data, perform network I/O, or run vendor-defined side effects.
Treat ``--run-version`` as an explicit operator opt-in.

Fixture-declared deep probes are not executed unless ``--run-deep-probes`` is
passed. Without that flag their declarations are echoed in probe output for
harness-specific follow-up work.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from draft7_validator import validate as validate_against_schema

CAPABILITY_IDS = {
    "instructions",
    "skills",
    "hooks",
    "mcp",
    "workspace",
    "session",
    "verification",
    "handoff",
    "reload",
    "telemetry",
    "platform",
}
BARE_COMMAND = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
DISCOVERY_ARGS_ALLOWLIST = frozenset({("--help",), ("-h",)})
DEEP_PROBE_IDS = (
    "instruction",
    "skill",
    "hook",
    "mcp",
    "workspace",
    "session",
    "verification",
    "handoff",
    "reload",
    "telemetry",
    "platform",
)
CREDENTIAL_NAME = (
    r"(?:api[_-]?key|token|secret|password|credential|private[_-]?key|"
    r"secret[_-]?access[_-]?key|access[_-]?key|(?<![A-Za-z0-9_])key(?![A-Za-z0-9_]))"
)
ASSIGNMENT_SECRET = re.compile(
    rf"(?i)((?:{CREDENTIAL_NAME})\s*[:=]\s*)(?!\[REDACTED\])(?:\"(?:\\.|[^\"\\])*(?:\"|\Z)|'(?:\\.|[^'\\])*(?:'|\Z)|[^\s]+)"
)
AUTHORIZATION_HEADER = re.compile(r"(?i)(Authorization\s*:\s*).+")
BEARER_TOKEN = re.compile(r"(?i)\bBearer\s+([A-Za-z0-9._~+/=-]+)\b")
JSON_QUOTED_CREDENTIAL = re.compile(rf'(?i)("[^"\r\n]*(?:{CREDENTIAL_NAME})[^"\r\n]*"\s*:\s*")((?:\\.|[^"\\])*)(")')
JSON_UNTERMINATED_CREDENTIAL = re.compile(
    rf'(?i)("[^"\r\n]*(?:{CREDENTIAL_NAME})[^"\r\n]*"\s*:\s*")((?:\\.|[^"\\])*)\\?\Z'
)
WINDOWS_HOME_PATH = re.compile(r"(?i)\b[A-Z]:(?:[\\/]+Users[\\/]+)[^\\/\"'\r\n]+")
REDACTION_CONTEXT = re.compile(rb"(?i)(?:[A-Z_][A-Z0-9_-]*\s*[:=]\s*[\"']?)$")
VERSION_LINE = re.compile(r"\d+\.\d+(?:\.\d+)?(?:[-+~][0-9A-Za-z][0-9A-Za-z.-]*)?")
OUTPUT_CAP_BYTES = 65536
REDACTION_CONTEXT_TAIL_BYTES = 256
DEFAULT_TIMEOUT_SECONDS = 10.0


def _positive_finite_timeout(timeout_seconds: float) -> float:
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout must be a finite positive number of seconds")
    return timeout_seconds


def _home_redaction_targets(*extra_homes: str | None) -> list[str]:
    homes: list[str] = []
    for candidate in (str(Path.home()), os.environ.get("HOME"), os.environ.get("USERPROFILE"), *extra_homes):
        if candidate and candidate not in homes:
            homes.append(candidate)
    return homes


def redact_text(value: str, *home_dirs: str | None) -> str:
    """Remove home paths, PATH home segments, and credential material from output."""
    redacted = value
    homes = _home_redaction_targets(*home_dirs)
    if os.environ.get("PATH"):
        for segment in os.environ["PATH"].split(os.pathsep):
            if segment and any(home in segment for home in homes):
                redacted = redacted.replace(segment, "[PATH]")
    for home in homes:
        redacted = redacted.replace(home, "[HOME]")
    redacted = WINDOWS_HOME_PATH.sub("[HOME]", redacted)
    redacted = ASSIGNMENT_SECRET.sub(r"\1[REDACTED]", redacted)
    redacted = AUTHORIZATION_HEADER.sub(r"\1[REDACTED]", redacted)
    redacted = BEARER_TOKEN.sub("Bearer [REDACTED]", redacted)
    redacted = JSON_QUOTED_CREDENTIAL.sub(r"\1[REDACTED]\3", redacted)
    redacted = JSON_UNTERMINATED_CREDENTIAL.sub(r'\1[REDACTED]"', redacted)
    return redacted


def _redact_bounded_output(output_bytes: bytes, *private_paths: str | None) -> str:
    output = redact_text(output_bytes.decode("utf-8", errors="replace"), *private_paths)
    encoded = output.encode("utf-8")
    if len(encoded) <= OUTPUT_CAP_BYTES:
        return output
    marker = b"[REDACTED]"
    marker_start = encoded.rfind(marker)
    if marker_start >= 0:
        line_start = encoded.rfind(b"\n", 0, marker_start) + 1
        context_start = max(line_start, marker_start - REDACTION_CONTEXT_TAIL_BYTES)
        context = REDACTION_CONTEXT.search(encoded[context_start:marker_start])
        suffix = encoded[context_start + context.start() if context else marker_start :]
        prefix_end = OUTPUT_CAP_BYTES - len(suffix)
        if prefix_end >= 0:
            return (encoded[:prefix_end] + suffix).decode("utf-8", errors="ignore")
    return encoded[:OUTPUT_CAP_BYTES].decode("utf-8", errors="ignore")


def _extract_version_line(output: str) -> str | None:
    """Return the first output line carrying a dotted version number.

    Vendors sometimes print warnings or banners before the version on
    ``--version``; a bare first-nonblank-line parse would report that banner as
    the version. Requiring a dotted numeric component skips banner lines while
    still accepting common formats (``0.3.1``, ``1.4.0-rc.1``, ``2.0+build5``).
    """
    for line in output.splitlines():
        stripped = line.strip()
        if stripped and VERSION_LINE.search(stripped):
            return stripped
    return None


def load_schema(schema_path: Path) -> dict[str, Any]:
    with schema_path.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_fixtures(fixtures_dir: Path) -> list[dict[str, Any]]:
    fixtures: list[dict[str, Any]] = []
    for path in sorted(fixtures_dir.glob("*.json")):
        with path.open(encoding="utf-8") as handle:
            fixtures.append(json.load(handle))
    return fixtures


def validate_fixture(fixture: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    """Validate a fixture against the shipped schema and contract invariants."""
    errors = [error.replace("$", "fixture") for error in validate_against_schema(fixture, schema)]
    cells = fixture.get("capabilities")
    if isinstance(cells, list):
        identifiers = [cell.get("id") for cell in cells if isinstance(cell, dict)]
        if len(identifiers) != len(set(identifiers)):
            errors.append("fixture.capabilities: duplicate capability ids are not allowed")
        unknown = set(identifiers) - CAPABILITY_IDS
        if unknown:
            errors.append(f"fixture.capabilities: unknown capability ids {sorted(unknown)!r}")
    deep_probes = fixture.get("deep_probes")
    if isinstance(deep_probes, dict):
        for probe_id in DEEP_PROBE_IDS:
            if probe_id in deep_probes:
                errors.extend(_validate_deep_probe_entry(probe_id, deep_probes[probe_id]))
    return errors


def _validate_deep_probe_entry(probe_id: str, entry: Any) -> list[str]:
    """Mirror the schema's deep-probe ``oneOf``/``$ref`` contract locally."""
    path = f"fixture.deep_probes.{probe_id}"
    if entry == "declared":
        return []
    if not isinstance(entry, dict):
        return [f"{path}: expected 'declared' or a declared probe object"]

    errors: list[str] = []
    for key in entry:
        if key not in {"state", "discovery"}:
            errors.append(f"{path}.{key}: additional property is not allowed")
    if "state" not in entry:
        errors.append(f"{path}: missing required property 'state'")
    elif entry["state"] != "declared":
        errors.append(f"{path}.state: expected const 'declared'")

    if "discovery" not in entry:
        return errors
    discovery = entry["discovery"]
    discovery_path = f"{path}.discovery"
    if not isinstance(discovery, dict):
        return [*errors, f"{discovery_path}: expected type 'object'"]
    for key in discovery:
        if key not in {"command", "args"}:
            errors.append(f"{discovery_path}.{key}: additional property is not allowed")
    for key in ("command", "args"):
        if key not in discovery:
            errors.append(f"{discovery_path}: missing required property '{key}'")

    if "command" in discovery:
        command = discovery["command"]
        if not isinstance(command, str):
            errors.append(f"{discovery_path}.command: expected type 'string'")
        elif not _is_safe_command_name(command):
            errors.append(f"{discovery_path}.command: string does not match bare-command pattern")
    if "args" in discovery:
        args = discovery["args"]
        if not isinstance(args, list):
            errors.append(f"{discovery_path}.args: expected type 'array'")
        else:
            for index, arg in enumerate(args):
                if not isinstance(arg, str):
                    errors.append(f"{discovery_path}.args[{index}]: expected type 'string'")
    return errors


def _is_safe_command_name(command: str) -> bool:
    return BARE_COMMAND.fullmatch(command) is not None


def _is_safe_discovery_args(args: list[str]) -> bool:
    return tuple(args) in DISCOVERY_ARGS_ALLOWLIST


def _normalize_deep_probe_entry(value: Any) -> dict[str, Any]:
    if value == "declared":
        return {"state": "declared"}
    if isinstance(value, dict) and value.get("state") == "declared":
        return value
    return {"state": "unknown"}


def _availability_blocks_deep_probe_execution(availability: dict[str, Any]) -> bool:
    return availability.get("state") in {"externally_blocked", "external_only", "not_executable"}


def _declared_only_receipt(probe_id: str, *, reason: str) -> dict[str, Any]:
    return {
        "probe_id": probe_id,
        "state": "declared_only",
        "reason": reason,
        "platform": sys.platform,
    }


def _redact_fixture_value(value: Any, *, secret_value: bool = False) -> Any:
    if isinstance(value, str):
        return "[REDACTED]" if secret_value else redact_text(value)
    if isinstance(value, list):
        return [_redact_fixture_value(item, secret_value=secret_value) for item in value]
    if isinstance(value, dict):
        return {
            redact_text(key) if isinstance(key, str) else key: _redact_fixture_value(
                item,
                secret_value=secret_value
                or (isinstance(key, str) and re.search(CREDENTIAL_NAME, key, flags=re.IGNORECASE) is not None),
            )
            for key, item in value.items()
        }
    return value


def _sanitize_probe_payload(value: Any, *, validation_error: bool = False) -> Any:
    """Apply the final redaction boundary without changing payload keys."""
    if isinstance(value, str):
        redacted = redact_text(value)
        if validation_error:
            return re.sub(CREDENTIAL_NAME, "[REDACTED]", redacted, flags=re.IGNORECASE)
        return redacted
    if isinstance(value, list):
        return [_sanitize_probe_payload(item, validation_error=validation_error) for item in value]
    if isinstance(value, dict):
        return {
            key: _sanitize_probe_payload(item, validation_error=validation_error or key == "validation_errors")
            for key, item in value.items()
        }
    return value


def _discovery_refusal_reason(discovery: dict[str, Any]) -> str | None:
    command = discovery.get("command")
    if not isinstance(command, str) or not _is_safe_command_name(command):
        return "unsafe_command_name"
    args = discovery.get("args")
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        return "unsafe_discovery_arguments"
    if not _is_safe_discovery_args(args):
        return "unsafe_discovery_arguments"
    return None


def _refused_discovery_receipt(probe_id: str, command: Any, reason: str) -> dict[str, Any]:
    return {
        "probe_id": probe_id,
        "state": "refused",
        "reason": reason,
        "command": _redact_fixture_value(command),
        "platform": sys.platform,
    }


def _invalid_fixture_unsafe_discovery_receipt(probe_id: str, entry: dict[str, Any]) -> dict[str, Any] | None:
    """Refuse unsafe declared discovery without executing an invalid fixture."""
    discovery = entry.get("discovery")
    if not isinstance(discovery, dict):
        return None
    reason = _discovery_refusal_reason(discovery)
    if reason is None:
        return None
    return _refused_discovery_receipt(probe_id, discovery.get("command"), reason)


def run_deep_probe_discovery(
    probe_id: str,
    discovery: dict[str, Any],
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    timeout_seconds = _positive_finite_timeout(timeout_seconds)
    command = discovery.get("command")
    refusal_reason = _discovery_refusal_reason(discovery)
    if refusal_reason is not None:
        return _refused_discovery_receipt(probe_id, command, refusal_reason)
    args = discovery["args"]

    executable = shutil.which(command)
    if executable is None:
        return {
            "probe_id": probe_id,
            "state": "externally_blocked",
            "reason": "binary_not_found",
            "command": command,
            "platform": sys.platform,
        }

    with tempfile.TemporaryDirectory(prefix="harness-probe-") as tmpdir:
        home_dir = Path(tmpdir) / "home"
        home_dir.mkdir()
        (home_dir / ".config").mkdir(parents=True, exist_ok=True)
        environment = _minimal_environment(home_dir, executable)
        process: subprocess.Popen[bytes] | None = None
        output_bytes = b""
        overflow = False
        failure_reason: str | None = None
        return_code: int | None = None
        try:
            try:
                process = _popen_probe_process(
                    [executable, *args],
                    cwd=tmpdir,
                    env=environment,
                )
            except OSError as error:
                return {
                    "probe_id": probe_id,
                    "state": "externally_blocked",
                    "reason": "OSError",
                    "command": command,
                    "exit_code": None,
                    "platform": sys.platform,
                    "output": _redact_bounded_output(str(error).encode(), str(home_dir), tmpdir),
                }
            output_bytes, overflow, failure_reason = _collect_bounded_output(
                process,
                cap_bytes=OUTPUT_CAP_BYTES,
                timeout_seconds=timeout_seconds,
            )
            return_code = process.poll()
            if return_code is None:
                try:
                    return_code = process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    _terminate_process_group(process)
                    return_code = process.wait(timeout=1)
        finally:
            if process is not None and process.poll() is None:
                _terminate_process_group(process)
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass

        output = _redact_bounded_output(output_bytes, str(home_dir), tmpdir)
        if failure_reason == "TimeoutExpired":
            return {
                "probe_id": probe_id,
                "state": "externally_blocked",
                "reason": "TimeoutExpired",
                "command": command,
                "exit_code": return_code,
                "platform": sys.platform,
                "output": output,
            }
        if overflow or failure_reason == "output_overflow":
            return {
                "probe_id": probe_id,
                "state": "externally_blocked",
                "reason": "output_overflow",
                "command": command,
                "exit_code": return_code,
                "platform": sys.platform,
                "output": output,
            }
        if return_code == 0:
            return {
                "probe_id": probe_id,
                "state": "observed",
                "command": command,
                "exit_code": return_code,
                "platform": sys.platform,
                "output": output,
            }
        return {
            "probe_id": probe_id,
            "state": "nonzero_exit",
            "reason": "nonzero_exit",
            "command": command,
            "exit_code": return_code,
            "platform": sys.platform,
            "output": output,
        }


def collect_deep_probe_receipts(
    fixture: dict[str, Any],
    availability: dict[str, Any],
    *,
    timeout_seconds: float,
) -> dict[str, dict[str, Any]]:
    timeout_seconds = _positive_finite_timeout(timeout_seconds)
    blocked = _availability_blocks_deep_probe_execution(availability)
    invalid_fixture = availability.get("state") == "not_executable" and availability.get("reason") == "invalid_fixture"
    blocked_reason = "invalid_fixture" if invalid_fixture else "externally_blocked_fixture"
    declared_probes = fixture.get("deep_probes", {})
    deep_probes = declared_probes if isinstance(declared_probes, dict) else {}
    receipts: dict[str, dict[str, Any]] = {}
    for probe_id in DEEP_PROBE_IDS:
        raw_entry = deep_probes.get(probe_id)
        entry = _normalize_deep_probe_entry(raw_entry)
        if blocked:
            if invalid_fixture and isinstance(raw_entry, dict):
                receipt = _invalid_fixture_unsafe_discovery_receipt(probe_id, raw_entry)
                if receipt is not None:
                    receipts[probe_id] = receipt
                    continue
            receipts[probe_id] = _declared_only_receipt(probe_id, reason=blocked_reason)
            continue
        discovery = entry.get("discovery")
        if not isinstance(discovery, dict):
            receipts[probe_id] = _declared_only_receipt(probe_id, reason="no_discovery_spec")
            continue
        receipts[probe_id] = run_deep_probe_discovery(probe_id, discovery, timeout_seconds=timeout_seconds)
    return receipts


def _command_candidates(fixture: dict[str, Any]) -> list[str]:
    harness = fixture.get("harness", {})
    surface = harness.get("surface")
    if surface == "cli":
        binary = fixture.get("binary", {})
        command = binary.get("command")
        return [command] if isinstance(command, str) else []
    availability = fixture.get("availability", {})
    if not isinstance(availability, dict):
        return []
    if availability.get("external_only") is True:
        return []
    candidates = availability.get("command_candidates", [])
    if isinstance(candidates, list):
        return [item for item in candidates if isinstance(item, str)]
    return []


def _command_availability(candidates: list[str]) -> dict[str, bool]:
    return {command: shutil.which(command) is not None for command in candidates}


def check_availability(fixture: dict[str, Any]) -> dict[str, Any]:
    """Resolve bare executable availability without executing vendor commands."""
    harness = fixture.get("harness", {})
    harness_id = harness.get("id", "unknown")
    surface = harness.get("surface")
    availability = fixture.get("availability", {})
    if surface in {"desktop", "gui"} and isinstance(availability, dict) and availability.get("external_only"):
        return {
            "harness_id": harness_id,
            "surface": surface,
            "state": "external_only",
            "reason": "desktop_or_gui_surface",
        }

    candidates = _command_candidates(fixture)
    if not candidates:
        return {
            "harness_id": harness_id,
            "surface": surface,
            "state": "externally_blocked",
            "reason": "no_command_candidates",
        }

    unsafe = [command for command in candidates if not _is_safe_command_name(command)]
    if unsafe:
        return {
            "harness_id": harness_id,
            "surface": surface,
            "state": "externally_blocked",
            "reason": "unsafe_command_name",
            "commands": candidates,
        }

    command_available = _command_availability(candidates)
    found = [command for command, available in command_available.items() if available]
    if not found:
        return {
            "harness_id": harness_id,
            "surface": surface,
            "state": "externally_blocked",
            "reason": "binary_not_found",
            "commands": candidates,
            "command_available": command_available,
        }
    return {
        "harness_id": harness_id,
        "surface": surface,
        "state": "available",
        "commands": candidates,
        "command_available": command_available,
        "available_commands": found,
    }


def _platform_default_path_segments() -> list[str]:
    if os.name == "nt":
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        return [
            str(Path(system_root) / "System32"),
            str(Path(system_root)),
        ]
    return ["/usr/bin", "/bin"]


def _minimal_environment(home_dir: Path, executable: str | None = None) -> dict[str, str]:
    path_parts = _platform_default_path_segments()
    if executable:
        path_parts.insert(0, str(Path(executable).parent))
    environment: dict[str, str] = {"PATH": os.pathsep.join(path_parts)}
    environment["HOME"] = str(home_dir)
    environment["USERPROFILE"] = str(home_dir)
    environment["XDG_CONFIG_HOME"] = str(home_dir / ".config")
    environment["XDG_CACHE_HOME"] = str(home_dir / ".cache")
    environment["XDG_DATA_HOME"] = str(home_dir / ".local" / "share")
    if os.name == "nt" and os.environ.get("SystemRoot"):
        environment["SystemRoot"] = os.environ["SystemRoot"]
    if os.name == "nt" and os.environ.get("TEMP"):
        environment["TEMP"] = os.environ["TEMP"]
        environment["TMP"] = os.environ.get("TMP", os.environ["TEMP"])
    return environment


def _popen_probe_process(
    argv: list[str],
    *,
    cwd: str,
    env: dict[str, str],
) -> subprocess.Popen[bytes]:
    popen_kwargs: dict[str, Any] = {
        "cwd": cwd,
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "bufsize": 0,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    else:
        popen_kwargs["start_new_session"] = True
    return subprocess.Popen(argv, **popen_kwargs)


def _terminate_process_group(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if process.poll() is None:
            process.kill()
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        process.kill()
    except AttributeError:
        process.kill()


class _BoundedStreamCollector:
    """Thread-safe bounded byte collector with no unbounded queue."""

    def __init__(self, cap_bytes: int) -> None:
        self._cap_bytes = cap_bytes
        self._lock = threading.Lock()
        self._chunks: list[bytes] = []
        self._total = 0
        self._overflow = False

    def append(self, data: bytes) -> bool:
        if not data:
            return False
        with self._lock:
            if self._total >= self._cap_bytes:
                self._overflow = True
                return True
            remaining = self._cap_bytes - self._total
            if len(data) > remaining:
                self._chunks.append(data[:remaining])
                self._total = self._cap_bytes
                self._overflow = True
                return True
            self._chunks.append(data)
            self._total += len(data)
            return False

    def snapshot(self) -> tuple[bytes, bool]:
        with self._lock:
            return b"".join(self._chunks), self._overflow


def _reader_loop(
    stream: Any,
    collector: _BoundedStreamCollector,
    stop_event: threading.Event,
) -> None:
    try:
        while not stop_event.is_set():
            data = stream.read(4096)
            if not data:
                break
            if collector.append(data):
                stop_event.set()
                break
    finally:
        stream.close()


def _collect_bounded_output(
    process: subprocess.Popen[bytes],
    *,
    cap_bytes: int,
    timeout_seconds: float,
) -> tuple[bytes, bool, str | None]:
    timeout_seconds = _positive_finite_timeout(timeout_seconds)
    stdout = process.stdout
    stderr = process.stderr
    assert stdout is not None
    assert stderr is not None
    collector = _BoundedStreamCollector(cap_bytes)
    stop_event = threading.Event()
    threads = [
        threading.Thread(target=_reader_loop, args=(stdout, collector, stop_event), daemon=True),
        threading.Thread(target=_reader_loop, args=(stderr, collector, stop_event), daemon=True),
    ]
    for thread in threads:
        thread.start()

    reason: str | None = None
    deadline = time.monotonic() + timeout_seconds
    try:
        while True:
            output, overflow = collector.snapshot()
            if overflow:
                reason = "output_overflow"
                _terminate_process_group(process)
                break
            if process.poll() is not None:
                break
            if time.monotonic() > deadline:
                reason = "TimeoutExpired"
                _terminate_process_group(process)
                break
            time.sleep(0.05)
        for thread in threads:
            thread.join(timeout=0.5)
        output, overflow = collector.snapshot()
        if overflow and reason is None:
            reason = "output_overflow"
            _terminate_process_group(process)
        if reason is None and process.poll() is None:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                reason = "TimeoutExpired"
                _terminate_process_group(process)
    finally:
        stop_event.set()
        for thread in threads:
            thread.join(timeout=0.5)

    output, overflow = collector.snapshot()
    return output, overflow, reason


def run_version_probe(fixture: dict[str, Any], *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Execute only ``<bare-command> --version`` inside an isolated sandbox.

    CLI surfaces execute their declared ``binary.command``. Desktop/GUI surfaces
    may declare a CLI-companion ``binary`` block (for example Antigravity's
    ``agy``); when present, the same sandboxed version probe runs so runtime
    conformance comes from direct vendor-binary evidence rather than an
    installation proxy. Non-CLI fixtures without a ``binary`` block stay
    limited to availability-only evidence.
    """
    timeout_seconds = _positive_finite_timeout(timeout_seconds)
    harness = fixture.get("harness", {})
    harness_id = harness.get("id", "unknown")
    surface = harness.get("surface")
    availability = fixture.get("availability", {})
    if surface in {"desktop", "gui"} and isinstance(availability, dict) and availability.get("external_only"):
        return {
            "harness_id": harness_id,
            "state": "external_only",
            "reason": "desktop_or_gui_surface",
        }

    binary = fixture.get("binary", {})
    if not isinstance(binary, dict):
        binary = {}
    command = binary.get("command")
    version_args = binary.get("version_args")
    if surface != "cli" and not isinstance(command, str):
        return {
            "harness_id": harness_id,
            "state": "externally_blocked",
            "reason": "version_execution_limited_to_cli_surface",
        }

    if not isinstance(command, str) or not _is_safe_command_name(command):
        return {
            "harness_id": harness_id,
            "state": "externally_blocked",
            "reason": "unsafe_command_name",
            "command": command,
        }
    if version_args != ["--version"]:
        return {
            "harness_id": harness_id,
            "state": "externally_blocked",
            "reason": "unsafe_version_arguments",
            "command": command,
        }

    executable = shutil.which(command)
    probed_command = command
    if executable is None and surface != "cli":
        # A desktop/GUI install may ship its CLI companion under one of the
        # declared availability candidates instead of the primary binary
        # command. Probe whichever candidate is present so the availability
        # signal and the version receipt never disagree.
        for candidate in _command_candidates(fixture):
            if candidate == command or not _is_safe_command_name(candidate):
                continue
            resolved = shutil.which(candidate)
            if resolved is not None:
                executable = resolved
                probed_command = candidate
                break
    if executable is None:
        return {
            "harness_id": harness_id,
            "state": "externally_blocked",
            "reason": "binary_not_found",
            "command": command,
        }

    with tempfile.TemporaryDirectory(prefix="harness-probe-") as tmpdir:
        home_dir = Path(tmpdir) / "home"
        home_dir.mkdir()
        config_dir = home_dir / ".config"
        config_dir.mkdir(parents=True, exist_ok=True)
        environment = _minimal_environment(home_dir, executable)
        process: subprocess.Popen[bytes] | None = None
        output_bytes = b""
        overflow = False
        failure_reason: str | None = None
        return_code: int | None = None
        try:
            process = _popen_probe_process(
                [executable, "--version"],
                cwd=tmpdir,
                env=environment,
            )
            output_bytes, overflow, failure_reason = _collect_bounded_output(
                process,
                cap_bytes=OUTPUT_CAP_BYTES,
                timeout_seconds=timeout_seconds,
            )
            return_code = process.poll()
            if return_code is None:
                try:
                    return_code = process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    _terminate_process_group(process)
                    return_code = process.wait(timeout=1)
        finally:
            if process is not None and process.poll() is None:
                _terminate_process_group(process)
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass

        if failure_reason == "TimeoutExpired":
            return {
                "harness_id": harness_id,
                "state": "externally_blocked",
                "reason": "TimeoutExpired",
                "command": probed_command,
            }
        if overflow:
            return {
                "harness_id": harness_id,
                "state": "externally_blocked",
                "reason": "output_overflow",
                "command": probed_command,
            }

        output = _redact_bounded_output(output_bytes, str(home_dir), tmpdir)
        if return_code == 0:
            return {
                "harness_id": harness_id,
                "state": "observed",
                "command": probed_command,
                "exit_code": return_code,
                "platform": sys.platform,
                "version": _extract_version_line(output),
                "output": output,
            }
        return {
            "harness_id": harness_id,
            "state": "nonzero_exit",
            "command": probed_command,
            "exit_code": return_code,
            "platform": sys.platform,
            "output": output,
        }


def probe_fixture(
    fixture: dict[str, Any],
    schema: dict[str, Any],
    *,
    run_version: bool,
    run_deep_probes: bool = False,
    timeout_seconds: float,
) -> dict[str, Any]:
    timeout_seconds = _positive_finite_timeout(timeout_seconds)
    errors = validate_fixture(fixture, schema)
    harness = fixture.get("harness", {})
    harness_id = harness.get("id", "unknown")
    payload: dict[str, Any] = {
        "harness_id": harness_id,
        "validation_errors": errors,
        "deep_probes": _redact_fixture_value(fixture.get("deep_probes", {})),
    }
    if errors:
        payload["availability"] = {
            "state": "not_executable",
            "reason": "invalid_fixture",
        }
        payload["version_probe"] = {
            "state": "not_executable",
            "reason": "invalid_fixture",
        }
        if run_deep_probes:
            payload["deep_probe_receipts"] = collect_deep_probe_receipts(
                fixture,
                payload["availability"],
                timeout_seconds=timeout_seconds,
            )
        return _sanitize_probe_payload(payload)

    payload["availability"] = check_availability(fixture)
    if run_version:
        payload["version_probe"] = run_version_probe(fixture, timeout_seconds=timeout_seconds)
    else:
        payload["version_probe"] = {
            "state": "skipped",
            "reason": "availability_only_unless_run_version",
        }
    if run_deep_probes and not errors:
        payload["deep_probe_receipts"] = collect_deep_probe_receipts(
            fixture,
            payload["availability"],
            timeout_seconds=timeout_seconds,
        )
    return _sanitize_probe_payload(payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures-dir", type=Path, required=True)
    parser.add_argument(
        "--schema",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "docs" / "proposals" / "harness-contract.v1.schema.json",
    )
    parser.add_argument(
        "--run-version",
        action="store_true",
        help="Execute validated CLI fixtures with --version in an isolated sandbox.",
    )
    parser.add_argument(
        "--run-deep-probes",
        action="store_true",
        help="Execute fixture-declared deep probe discovery commands in an isolated sandbox.",
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)

    schema = load_schema(args.schema)
    fixtures = load_fixtures(args.fixtures_dir)
    timeout_seconds = _positive_finite_timeout(args.timeout)
    results = [
        probe_fixture(
            fixture,
            schema,
            run_version=args.run_version,
            run_deep_probes=args.run_deep_probes,
            timeout_seconds=timeout_seconds,
        )
        for fixture in fixtures
    ]
    payload = {
        "schema": "harness-conformance-probe.v1",
        "fixtures": [fixture["harness"]["id"] for fixture in fixtures],
        "run_version": args.run_version,
        "run_deep_probes": args.run_deep_probes,
        "deep_probe_policy": ("executed_when_specified" if args.run_deep_probes else "declared_only_not_executed"),
        "results": results,
    }
    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 1 if any(result["validation_errors"] for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
