"""Bounded, read-only selected-project MCP configuration diagnosis."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import stat
from pathlib import Path
from typing import Any

from . import __version__, dirfd, mcp_adapters, mcp_cmd
from .bug_redaction import Redactor, ReportBlocked, assert_clean, assert_terminal_safe

_CAP = 64 * 1024
_LIMIT = 100
_OBSERVED = {
    "missing": "Catalog entry is absent from the selected native configuration.",
    "current": "Selected configuration matches the catalog projection.",
    "conflicted": "Managed native entry was edited outside Brigade.",
    "stale": "Managed native entry is unchanged, but its catalog projection has changed.",
    "foreign": "An unowned native entry differs from the catalog projection.",
    "orphan": "Previously managed entry is no longer selected by the catalog.",
    "orphan-edited": "Previously managed entry is no longer selected and was externally edited.",
    "invalid": "Selected configuration cannot faithfully represent the catalog entry.",
    "excluded": "Catalog entry is disabled or excludes the selected harness.",
    "absent": "No selected catalog entry or managed native entry was found.",
}


def _read(root: int, relative: str) -> bytes | None:
    """Read one fixed relative path under a held project descriptor, without links."""
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or any(p in {"..", "."} for p in parts):
        raise ValueError("unsafe path")
    parent = os.dup(root)
    try:
        for part in parts[:-1]:
            child = dirfd.open_child_directory(parent, part, writable=False)
            os.close(parent)
            parent = child
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        descriptor = dirfd.open_child_file(parent, parts[-1], flags, share_write=False)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > _CAP:
                raise ValueError("unsafe file")
            chunks = bytearray()
            while len(chunks) <= _CAP:
                chunk = os.read(descriptor, min(8192, _CAP + 1 - len(chunks)))
                if not chunk:
                    break
                chunks.extend(chunk)
            after = os.fstat(descriptor)
            if len(chunks) > _CAP or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise ValueError("unstable file")
            if after.st_nlink != 1:
                raise ValueError("linked file")
            return bytes(chunks)
        finally:
            os.close(descriptor)
    except FileNotFoundError:
        return None
    finally:
        os.close(parent)


def _object(raw: bytes | None) -> dict[str, Any]:
    if raw is None:
        raise ValueError("missing input")
    doc = json.loads(raw.decode("utf-8"))
    if not isinstance(doc, dict):
        raise ValueError("invalid object")
    return doc


def _string_mapping(value: Any) -> bool:
    return isinstance(value, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in value.items())


def _validate_fields(raw: dict[str, Any], *, native_harness: str | None = None) -> None:
    """Reject malformed modeled fields before permissive adapter normalization."""
    native = native_harness is not None
    opencode = native_harness == "opencode"
    for key in ("url", "description", "transport", "type", "serverUrl", "cwd", "oauth_resource"):
        if key in raw and not isinstance(raw[key], str):
            raise ValueError("invalid string field")
    if "transport" in raw and raw["transport"] not in mcp_adapters.TRANSPORTS:
        raise ValueError("invalid transport")
    if "type" in raw and raw["type"] not in (("local", "remote") if opencode else mcp_adapters.TRANSPORTS):
        raise ValueError("invalid transport")
    if "command" in raw:
        command = raw["command"]
        if opencode:
            if not isinstance(command, list) or any(not isinstance(v, str) for v in command):
                raise ValueError("invalid command")
        elif not isinstance(command, str):
            raise ValueError("invalid command")
    for key in ("args", "env_vars", "targets"):
        if key == "targets" and not native and raw.get(key) is None:
            continue
        if key in raw and (not isinstance(raw[key], list) or any(not isinstance(v, str) for v in raw[key])):
            raise ValueError("invalid string list")
    for key in ("env", "headers", "environment", "http_headers", "env_http_headers"):
        if key not in raw:
            continue
        mapping = raw[key]
        if not native and key in {"env", "headers"}:
            if not isinstance(mapping, dict):
                raise ValueError("invalid canonical mapping")
            for value in mapping.values():
                if isinstance(value, str):
                    continue  # Canonical paste convenience: a warned literal.
                if (
                    not isinstance(value, dict)
                    or set(value) not in ({"ref"}, {"literal"})
                    or not isinstance(next(iter(value.values())), str)
                ):
                    raise ValueError("invalid canonical mapping")
        elif not _string_mapping(mapping):
            raise ValueError("invalid native mapping")
    if "bearer_token_env_var" in raw and (
        not isinstance(raw["bearer_token_env_var"], str)
        or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", raw["bearer_token_env_var"]) is None
    ):
        raise ValueError("invalid token reference")
    for key in ("enabled", "disabled"):
        if key in raw and not isinstance(raw[key], bool):
            raise ValueError("invalid activation")
    if opencode:
        mcp_adapters.opencode_native(raw)
    else:
        timeout = raw.get("timeout")
        if timeout is not None and (type(timeout) is not int or timeout <= 0):
            raise ValueError("invalid timeout")
    if not native and "opencode_native" in raw:
        mcp_adapters.opencode_native(raw["opencode_native"])
    mcp_adapters.codex_native_auth(raw)


def _inputs(target: Path, harness: str) -> tuple[dict[str, Any], dict[str, Any], str | None]:
    adapter = mcp_adapters.ADAPTERS[harness]
    root = dirfd.open_directory_chain(target, writable=False)
    try:
        blobs = [_read(root, p) for p in (mcp_cmd.CANONICAL_REL, mcp_cmd.STATE_REL, adapter.path)]
    finally:
        os.close(root)
    catalog = _object(blobs[0])
    raw_servers = catalog.get("servers")
    if not isinstance(raw_servers, dict) or len(raw_servers) > _LIMIT:
        raise ValueError("invalid catalog")
    servers = {}
    for name, raw in raw_servers.items():
        if not isinstance(raw, dict) or not name:
            raise ValueError("invalid server")
        _validate_fields(raw)
        # Strict validation above rejects malformed values. Normalization may
        # still warn about documented bare-string literals, which remain valid.
        server, _ = mcp_adapters.server_from_dict(name, raw)
        if any(level == "error" for level, _ in mcp_adapters.validate_server(server)):
            raise ValueError("invalid server")
        servers[name] = server
    state = _object(blobs[1]) if blobs[1] is not None else {"version": 1, "ownership": {}}
    ownership = state.get("ownership")
    if not isinstance(ownership, dict):
        raise ValueError("invalid ownership")
    # Validate the complete sidecar shape without interpreting another harness.
    for harness_state in ownership.values():
        if not isinstance(harness_state, dict):
            raise ValueError("invalid ownership")
        for records in harness_state.values():
            if not isinstance(records, dict) or len(records) > _LIMIT:
                raise ValueError("invalid ownership")
            for record in records.values():
                if not isinstance(record, dict) or any(
                    not isinstance(record.get(key), str) for key in ("canonical_fingerprint", "projected_fingerprint")
                ):
                    raise ValueError("invalid ownership")
                for key in ("native_auth_keys", "opencode_native_keys"):
                    keys = record.get(key, {})
                    if not isinstance(keys, dict) or any(
                        value is not True and not (isinstance(value, list) and all(isinstance(v, str) for v in value))
                        for value in keys.values()
                    ):
                        raise ValueError("invalid ownership")
    text = blobs[2].decode("utf-8") if blobs[2] is not None else None
    if text is not None and adapter.fmt == "json" and not text.strip():
        raise ValueError("empty native JSON")
    mcp_cmd._validate_native_config(harness, text)
    live = adapter.read_file(text)
    for entry in live.values():
        _validate_fields(entry, native_harness=harness)
    owned = ownership.get(harness, {}).get(adapter.path, {})
    if len(set(servers) | set(live) | set(owned)) > _LIMIT:
        raise ValueError("too many servers")
    return servers, state, text


def _safe(redactor: Redactor, text: str) -> str:
    text = redactor.text(text)
    text = re.sub(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s<>]+", "<url>", text)
    text = re.sub(r"(?<!\w)(?:/[\w./~%-]+|[A-Za-z]:[\\/][^\s]+)", "<path>", text)
    return text[:2000]


def _compare(args: argparse.Namespace) -> dict[str, Any]:
    adapter = mcp_adapters.ADAPTERS.get(args.harness)
    if adapter is None or adapter.user_scope:
        raise ValueError("project adapter required")
    # Do not resolve: the descriptor walk must refuse symlinked ancestors too.
    target = args.target.absolute()
    servers, state, native = _inputs(target, args.harness)
    items = mcp_cmd._plan_from_inputs(target, args.harness, servers, state, native, name_filter=args.name)
    if any(i["status"] == "invalid" and i["server"] == "*" for i in items):
        raise ValueError("invalid evidence")
    redactor = Redactor(target)
    symptom = _safe(redactor, args.summary)
    findings = []
    excluded = set()
    for name, server in servers.items():
        if (args.name is None or args.name == name) and (
            not server.enabled or not mcp_cmd._server_targets_harness(server, args.harness)
        ):
            excluded.add(name)
    for item in items:
        if item["server"] in excluded:
            continue
        status = item["status"]
        findings.append(
            {
                "server": _safe(redactor, item["server"]),
                "status": status,
                "expected": "Selected catalog projection.",
                "observed": _OBSERVED[status],
            }
        )
    for name in sorted(excluded):
        findings.append(
            {
                "server": _safe(redactor, name),
                "status": "excluded",
                "expected": "No selected projection.",
                "observed": _OBSERVED["excluded"],
            }
        )
    if not findings:
        findings.append(
            {
                "server": _safe(redactor, args.name or "(none)"),
                "status": "absent",
                "expected": "Selected catalog projection, if present.",
                "observed": _OBSERVED["absent"],
            }
        )
    # Export only a digest of fields already disclosed in the redacted result.
    # A raw-input digest would allow guessing secret-bearing configuration.
    projection = {"harness": args.harness, "findings": findings}
    digest = hashlib.sha256(json.dumps(projection, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    # Run from the selected project. Scrub before quoting so placeholders remain
    # literal arguments, even when redaction introduces shell metacharacters.
    command = "brigade mcp plan --target . --harness " + shlex.quote(args.harness)
    if args.name is not None:
        command += " --name " + shlex.quote(_safe(redactor, args.name))
    next_check = "From the selected project: " + command
    uncertainty = (
        "Configuration evidence only; runtime availability unknown. A difference does not prove a Brigade defect."
    )
    text = f"Symptom: {symptom}\nHarness: {args.harness}\nEvidence digest: {digest}\n"
    text += "Expected: Selected catalog projection; excluded entries have no projection.\n"
    text += "\n".join(f"Server: {f['server'][:80]} | {f['status']}" for f in findings)
    text += "\n" + "\n".join(
        f"Observed {status}: {_OBSERVED[status]}" for status in sorted({f["status"] for f in findings})
    )
    text += f"\nUncertainty: {uncertainty}\nNext check (preview only): {next_check}\nverification: not-run"
    result = {
        "status": "available",
        "symptom": symptom,
        "harness": args.harness,
        "findings": findings,
        "uncertainty": uncertainty,
        "next_check": next_check,
        "verification": "not-run",
        "runtime": "unknown",
        "text": text,
    }
    assert_terminal_safe(result)
    assert_clean(json.dumps(result, ensure_ascii=False))
    return result


def _export(path: Path, text: str) -> None:
    """Create exactly one new finding; refuse existing files and linked ancestors."""
    path = path.absolute()
    parent = dirfd.open_directory_chain(path.parent, writable=True)
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = dirfd.open_child_file(parent, path.name, flags)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps({"kind": "finding", "source": "assisted-diagnosis", "text": text}) + "\n")
    finally:
        os.close(parent)


def diagnose(args: argparse.Namespace) -> int:
    try:
        result = _compare(args)
    except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
        result = {
            "status": "unavailable",
            "verification": "not-run",
            "runtime": "unknown",
            "text": "Diagnosis unavailable: selected project evidence is unsupported, unsafe, malformed or exceeds limits. Review the selected configuration locally; no runtime check was run.",
        }
        print(json.dumps(result) if args.json else result["text"])
        return 2
    try:
        if args.save_finding:
            _export(args.save_finding, result["text"])
        if args.prepare_report:
            from .bug_cmd import _prepare_collected, report

            # Only the selected diagnosis crosses this boundary. Installation
            # and runtime metadata remain unknown instead of probing ambient
            # state, recent receipts, diagnostics or the global error record.
            collected: dict[str, Any] = {
                "schema": "brigade.bug-report.v1",
                "brigade": {"version": __version__, "channel": "unknown", "distribution": "brigade-cli"},
                "runtime": {key: "unknown" for key in ("python", "os", "release", "arch")},
                "bug_diagnostics": {"target": "<target>", "checks": [], "summary": {"failed": 0, "warn": 0}},
                "recent_failures": {"runs": [], "verify": []},
                "last_error": None,
                "user": {
                    "summary": result["text"],
                    "title": "MCP configuration diagnosis",
                    "kind": "bug",
                    "output_tail": "",
                },
            }
            directory, _, _, _ = _prepare_collected(args.target.absolute(), collected)

            return report(
                argparse.Namespace(
                    target=args.target,
                    title=None,
                    summary=None,
                    summary_file=None,
                    include_output=None,
                    kind="bug",
                    via="file",
                    comment_on=None,
                    json=args.json,
                    dry_run=True,
                    yes=False,
                    not_security=False,
                    report_id=directory.name,
                )
            )
    except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError) as exc:
        # Output errors do not invalidate the already compared evidence. Keep
        # exception data private, including destination paths and scan details.
        message = (
            "Bug report blocked: review the diagnosis locally. Report security concerns privately using SECURITY.md."
            if isinstance(exc, ReportBlocked)
            else "Output unavailable: the destination exists, is unsafe or could not be written. Review the diagnosis without output options, then choose a new destination or check local permissions."
        )
        result = {
            "status": "unavailable",
            "verification": "not-run",
            "runtime": "unknown",
            "text": message,
        }
        print(json.dumps(result) if args.json else result["text"])
        return 2
    print(json.dumps(result, ensure_ascii=False) if args.json else result["text"])
    return 0
