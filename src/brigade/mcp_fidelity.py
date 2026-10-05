"""Value-free assessment of MCP output semantics, separate from write success.

Only Codex and OpenCode have complete contracts here. Other adapters still enforce
known security losses, but their overall fidelity is explicitly unevaluated.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable

from .mcp_adapters import CanonicalServer, codex_auth_header_names

STATUSES = ("preserved", "transformed", "unsupported", "intentionally_omitted")
_CODEX = {"codex", "codex-user"}
_OPENCODE = {"opencode", "opencode-user"}
_AUTH = ("http_headers", "env_http_headers", "bearer_token_env_var")
_ERROR_REASONS = {
    "native_auth_collision",
    "auth_interpolation_unsupported",
    "native_auth_unrepresentable",
    "native_timeout_flat_unsupported",
    "native_config_malformed",
    "native_layout_duplicate",
    "native_setting_collision",
    "projection_invalid",
    "native_config_unreadable",
}


_MALFORMED_ERRORS = {
    "existing JSON configuration is invalid; refusing to overwrite",
    "existing JSON configuration must be an object; refusing to overwrite",
    "existing OpenCode config is malformed",
    "existing OpenCode mcp.servers is malformed",
    "existing OpenCode mcp.servers entry is malformed",
    *(
        f"existing {key} section must be an object; refusing to overwrite"
        for key in ("mcpServers", "mcp.servers", "servers")
    ),
}
_SAFE_PROJECTION_ERRORS = _MALFORMED_ERRORS | {
    "duplicate OpenCode server locations; refusing mutation",
    "OpenCode server would overwrite an existing setting/container",
    "OpenCode timeout cannot be preserved in flat layout",
    "OpenCode native metadata requires an object",
    "OpenCode enabled requires a boolean",
    "OpenCode disabled requires a boolean",
    "OpenCode timeout has unsupported fields",
    "OpenCode timeout requires positive integer milliseconds",
    "OpenCode oauth requires false or an object",
    "OpenCode oauth fields require strings",
    "unsupported native TOML option type; refusing to overwrite",
    "Codex bearer_token_env_var requires an environment variable name",
    "Codex http_headers requires a string map",
    "Codex env_http_headers requires a string map",
    "Codex env_http_headers requires environment variable names; interpolation is unsupported",
    "Codex header ref requires an environment variable name; interpolation is unsupported",
    "Codex headers contain unsupported interpolation; use native header or bearer environment references",
    "cannot preserve Codex native header semantics in this harness; scope the server to Codex",
}


def projection_error_code(error: ValueError) -> str:
    """Categorize known serializer refusals without emitting exception input."""
    if str(error) in _MALFORMED_ERRORS:
        return "native_config_malformed"
    message = str(error).lower()
    if "timeout" in message and "flat" in message:
        return "native_timeout_flat_unsupported"
    if "interpolation" in message or "header ref requires" in message:
        return "auth_interpolation_unsupported"
    if "native header semantics" in message:
        return "native_auth_unrepresentable"
    if str(error) == "OpenCode server would overwrite an existing setting/container":
        return "native_setting_collision"
    return "projection_invalid"


def projection_error_message(error: ValueError, *, fallback: str | None = None) -> str:
    """Retain fixed actionable refusals, never unknown exception input."""
    message = str(error)
    return message if message in _SAFE_PROJECTION_ERRORS else fallback or projection_error_code(error)


def _row(field: str, status: str, reason: str, *, blocking: bool = False, **metadata: str) -> dict[str, Any]:
    return {"field": field, "status": status, "reason": reason, "blocking": blocking, **metadata}


def _security_fields(server: CanonicalServer, harness: str) -> list[dict[str, Any]]:
    fields = []
    if not server.is_remote:
        for key in ("headers", *_AUTH):
            if getattr(server, key):
                fields.append(_row(key, "unsupported", "auth_transport_incompatible", blocking=True))
    native = server.opencode_native
    if "oauth" in native and (not server.is_remote or harness not in _OPENCODE):
        reason = "auth_transport_incompatible" if not server.is_remote else "oauth_unsupported"
        fields.append(_row("opencode_native.oauth", "unsupported", reason, blocking=True))
    if harness not in _OPENCODE:
        # Cross-family projections have no native activation equivalent. Flat
        # source precedence is enabled first, matching the default serializer.
        disabled = not native["enabled"] if "enabled" in native else native.get("disabled", False)
        for key in ("enabled", "disabled"):
            if key in native:
                fields.append(
                    _row(
                        f"opencode_native.{key}",
                        "unsupported" if disabled else "intentionally_omitted",
                        "disabled_activation_unsupported" if disabled else "native_activation_not_applicable",
                        blocking=disabled,
                    )
                )
        if "timeout" in native:
            fields.append(_row("opencode_native.timeout", "unsupported", "native_timeout_unsupported"))
    return fields


def _auth_fields(
    server: CanonicalServer,
    harness: str,
    projected: dict[str, Any],
    live: dict[str, Any],
    native_keys: dict[str, Any],
) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    if not server.is_remote:
        for key in (*_AUTH, "headers", "oauth"):
            if key in live and key not in projected:
                fields.append(_row(f"native.{key}", "intentionally_omitted", "transport_cleanup"))
        return fields
    explicit = codex_auth_header_names(vars(server))
    retained: set[str] = set()
    for key in (*_AUTH, "headers", "oauth"):
        if key not in live:
            continue
        names = native_keys.get(key)
        output_key = key if harness in _CODEX else "headers" if key in _AUTH else key
        if names and output_key in projected:
            if isinstance(names, list) and key in (*_AUTH, "headers"):
                retained.update(name.lower() for name in names)
            if key == "bearer_token_env_var":
                retained.add("authorization")
            fields.append(_row(f"native.{key}", "preserved", "retained_native_auth"))
        live_names = (
            {name.lower() for name in live[key]}
            if key == "headers" and isinstance(live[key], dict)
            else codex_auth_header_names({key: live[key]})
            if key in _AUTH
            else set()
        )
        replacement_names = explicit | (set(name.lower() for name in server.headers) if harness in _OPENCODE else set())
        if live_names & replacement_names or (key == "oauth" and "oauth" in server.opencode_native):
            fields.append(_row(f"native.{key}", "intentionally_omitted", "explicit_canonical_replacement"))
    shadowed = {name for name in server.headers if name.lower() in explicit | retained}
    if shadowed:
        fields.append(_row("headers.shadowed", "intentionally_omitted", "native_auth_precedence"))
    generic_names = {name.lower() for name in set(server.headers) - shadowed}
    if generic_names:
        if harness in _CODEX:
            destinations = {
                key
                for key in ("http_headers", "env_http_headers")
                if isinstance(projected.get(key), dict) and generic_names & {name.lower() for name in projected[key]}
            }
            if "authorization" in generic_names and projected.get("bearer_token_env_var"):
                destinations.add("bearer_token_env_var")
            reasons = {
                "http_headers": "generic_static_to_http_headers",
                "env_http_headers": "complete_header_reference",
                "bearer_token_env_var": "bearer_token_reference",
            }
            fields.extend(_row("headers", "transformed", reasons[key], destination=key) for key in sorted(destinations))
            if not destinations:
                fields.append(_row("headers", "unsupported", "auth_not_emitted", blocking=True))
        else:
            emitted = bool(projected.get("headers"))
            fields.append(
                _row(
                    "headers",
                    "transformed" if emitted else "unsupported",
                    "header_reference_format" if emitted else "auth_not_emitted",
                    blocking=not emitted,
                    destination="headers",
                )
            )
    for key in _AUTH:
        if getattr(server, key):
            emitted = key in projected if harness in _CODEX else "headers" in projected
            fields.append(
                _row(
                    key,
                    ("preserved" if harness in _CODEX else "transformed") if emitted else "unsupported",
                    "native_auth_preserved"
                    if emitted and harness in _CODEX
                    else "native_auth_to_headers"
                    if emitted
                    else "auth_not_emitted",
                    blocking=not emitted,
                    destination=key if harness in _CODEX else "headers",
                )
            )
    return fields


def _opencode_fields(
    server: CanonicalServer,
    location: str,
    projected: dict[str, Any],
    live: dict[str, Any],
    native_keys: dict[str, Any],
) -> list[dict[str, Any]]:
    fields = []
    native = server.opencode_native
    activation = "disabled" if location == "nested" else "enabled"
    other = "enabled" if activation == "disabled" else "disabled"
    for key in ("enabled", "disabled"):
        if key in native:
            if key == other and activation in native:
                fields.append(_row(f"opencode_native.{key}", "intentionally_omitted", "native_activation_precedence"))
            else:
                fields.append(
                    _row(
                        f"opencode_native.{key}",
                        "preserved" if key == activation else "transformed",
                        "native_activation_preserved" if key == activation else "native_activation_inverted",
                        destination=activation,
                    )
                )
    if not any(key in native for key in ("enabled", "disabled")) and any(
        native_keys.get(key) for key in ("enabled", "disabled")
    ):
        fields.append(_row("native.activation", "transformed", "retained_native_activation", destination=activation))
    if "oauth" in native and server.is_remote:
        fields.append(_row("opencode_native.oauth", "preserved", "native_oauth_preserved", destination="oauth"))
    if "timeout" in native:
        value = native["timeout"]
        if server.timeout is not None:
            reason, status = "canonical_timeout_precedence", "intentionally_omitted"
        elif location == "nested" and isinstance(value, int):
            reason, status = "native_timeout_to_nested", "transformed"
        elif location == "flat" and isinstance(value, dict):
            reason, status = "native_timeout_to_flat", "transformed"
        else:
            reason, status = "native_timeout_preserved", "preserved"
        fields.append(_row("opencode_native.timeout", status, reason, destination="timeout", units="milliseconds"))
    retained_timeout = live.get("timeout") if native_keys.get("timeout") else None
    startup_source = native.get("timeout", retained_timeout)
    if isinstance(startup_source, dict) and "startup" in startup_source:
        emitted = isinstance(projected.get("timeout"), dict) and "startup" in projected["timeout"]
        fields.append(
            _row(
                "native.timeout.startup",
                "preserved" if emitted else "unsupported",
                "native_startup_preserved" if emitted else "native_startup_flat_unsupported",
                units="milliseconds",
            )
        )
    if retained_timeout is not None:
        reason, status = "retained_native_timeout", "preserved"
        if server.timeout is not None:
            reason, status = "canonical_timeout_precedence", "transformed"
        elif location == "nested" and isinstance(retained_timeout, int):
            reason, status = "native_timeout_to_nested", "transformed"
        elif location == "flat" and isinstance(retained_timeout, dict):
            reason, status = "native_timeout_to_flat", "transformed"
        fields.append(_row("native.timeout", status, reason, units="milliseconds"))
    return fields


def evaluate(
    server: CanonicalServer | None,
    harness: str,
    *,
    scope: str,
    location: str | None = None,
    projected: dict[str, Any] | None = None,
    live: dict[str, Any] | None = None,
    native_keys: dict[str, Any] | None = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    """Assess actual emitted fields. Context is inspected but never serialized."""
    evaluated = harness in _CODEX | _OPENCODE and server is not None
    if error_code in {"native_config_malformed", "native_config_unreadable", "native_layout_duplicate"}:
        evaluated = False
    row: dict[str, Any] = {
        "harness": harness,
        "server": server.name if server else "*",
        "scope": scope,
        "evaluated": evaluated,
        "faithful": None,
        "fields": [],
    }
    if harness in _OPENCODE and location is not None:
        row["layout"] = location
    fields: list[dict[str, Any]] = row["fields"]
    if server is not None:
        fields.extend(_security_fields(server, harness))
    if error_code:
        reason = error_code if error_code in _ERROR_REASONS else "projection_invalid"
        fields.append(_row("projection", "unsupported", reason, blocking=True))
    elif not evaluated:
        fields.append(_row("projection", "intentionally_omitted", "adapter_unevaluated"))
    elif projected is None:
        fields.append(_row("projection", "unsupported", "projection_invalid", blocking=True))
    else:
        assert server is not None
        opencode = harness in _OPENCODE
        fields.append(_row("enabled", "intentionally_omitted", "catalog_membership"))
        if server.targets is not None:
            fields.append(_row("targets", "intentionally_omitted", "routing_only"))
        if server.description:
            fields.append(_row("description", "intentionally_omitted", "catalog_only"))
        sse_loss = opencode and server.transport == "sse"
        fields.append(
            _row(
                "transport",
                "unsupported" if sse_loss else "transformed" if opencode else "preserved",
                "sse_distinction_unsupported"
                if sse_loss
                else "transport_to_native_type"
                if opencode
                else "transport_preserved",
            )
        )
        for key in ("command", "args", "env", "url"):
            if not getattr(server, key):
                continue
            applicable = server.is_remote if key == "url" else not server.is_remote
            dest = "command" if opencode and key == "args" else "environment" if opencode and key == "env" else key
            emitted = applicable and dest in projected
            transformed = (opencode and key in ("command", "args")) or (key == "env" and opencode)
            fields.append(
                _row(
                    key,
                    "transformed" if emitted and transformed else "preserved" if emitted else "intentionally_omitted",
                    "command_array"
                    if emitted and opencode and key in ("command", "args")
                    else "environment_reference_format"
                    if emitted and transformed
                    else "field_emitted"
                    if emitted
                    else "transport_not_applicable",
                    destination=dest,
                )
            )
        if server.timeout is not None:
            emitted = "timeout" in projected
            fields.append(
                _row(
                    "timeout",
                    "transformed" if emitted and opencode else "preserved" if emitted else "unsupported",
                    "seconds_to_milliseconds"
                    if emitted and opencode
                    else "emitted_timeout_seconds"
                    if emitted
                    else "remote_timeout_unsupported",
                    destination="timeout",
                    units="milliseconds" if opencode else "seconds",
                )
            )
        fields.extend(_auth_fields(server, harness, projected, live or {}, native_keys or {}))
        if opencode:
            fields.extend(_opencode_fields(server, location or "flat", projected, live or {}, native_keys or {}))
    row["fields"] = sorted(fields, key=lambda f: (f["field"], f["reason"]))
    if evaluated:
        row["faithful"] = not any(f["status"] == "unsupported" for f in fields)
    return row


def build_report(
    server_reports: Iterable[dict[str, Any]],
    excluded: Iterable[dict[str, Any]],
    *,
    harnesses: Iterable[str] = (),
) -> dict[str, Any]:
    """Counts and target states derive from field rows, independently of writes."""
    servers = sorted(server_reports, key=lambda r: (r["harness"], r["server"], r["scope"]))
    counts = Counter(f["status"] for row in servers for f in row["fields"])
    summary = {status: counts[status] for status in STATUSES}
    summary["blocking"] = sum(bool(f["blocking"]) for row in servers for f in row["fields"])
    targets = []
    for harness in sorted(set(harnesses) | {r["harness"] for r in servers}):
        rows = [r for r in servers if r["harness"] == harness]
        blocking = sum(bool(f["blocking"]) for row in rows for f in row["fields"])
        state = (
            "blocked"
            if blocking
            else "unevaluated"
            if (harness not in _CODEX | _OPENCODE or any(not r["evaluated"] for r in rows))
            else "partial"
            if any(r["faithful"] is False for r in rows)
            else "ok"
        )
        targets.append({"harness": harness, "state": state, "blocking": blocking})
    return {
        "version": 1,
        "summary": summary,
        "targets": targets,
        "servers": servers,
        "excluded": sorted(excluded, key=lambda r: (r["harness"], r["server"], r["reason"])),
    }


def render_lines(report: dict[str, Any]) -> list[str]:
    lines = [f"fidelity {t['harness']}: {t['state']} (blocking={t['blocking']})" for t in report["targets"]]
    for row in report["servers"]:
        for field in row["fields"]:
            if field["status"] != "preserved":
                lines.append(
                    f"fidelity {row['harness']}/{row['server']} [{row['scope']}] {field['field']}: "
                    f"{field['status']} ({field['reason']})" + (" blocking" if field["blocking"] else "")
                )
    lines.extend(f"fidelity {r['harness']}/{r['server']}: excluded ({r['reason']})" for r in report["excluded"])
    return lines
