"""Bounded explicit Dot metadata adapter, reusable without a CLI or MCP server.

No provider discovery, filesystem inspection, transcript ingestion, or lifecycle
inference. Dry-run does not load authentication or call any client.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit

from .fleet_session_presence import SessionSnapshot, _parse_remote, validate_repo_identity
from .worklore_validate import WorkloreValidationError, safe_text

MAX_REPORT_BYTES = 16 * 1024
MAX_SUMMARY_CHARS = 400
MAX_EVIDENCE_REFS = 8
_OPAQUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_REPO = re.compile(r"[A-Za-z0-9.-]+/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_FIELDS = frozenset(
    {
        "version",
        "session_id",
        "agent_label",
        "parent_session_id",
        "repo_identity",
        "work_id",
        "source",
        "source_scope",
        "ownership_revision",
        "generation",
        "coverage",
        "observed_at",
        "sequence",
        "task",
        "progress",
        "blocker",
        "result",
        "evidence_refs",
    }
)
_CLOUD_FIELDS = frozenset(
    {
        "version",
        "provider",
        "agent_label",
        "parent_session_id",
        "work_id",
        "observed_at",
        "sequence",
        "source",
        "source_scope",
        "coverage",
    }
)


class DotReportError(ValueError):
    """A safe fixed refusal message, never including submitted text."""


def _text(raw: object, field: str, limit: int, *, https_reference: bool = False) -> str:
    try:
        value = safe_text(raw, field, max_len=limit)
        # Decode before checking private data and controls too. Query/fragment
        # metadata is unnecessary here and can carry short secrets validators
        # intentionally allow in ordinary Worklore prose.
        decoded = unquote(value)
        safe_text(decoded, field, max_len=limit)
        if (
            "%" in value
            or not value.strip()
            or any(char in decoded for char in ("?", "#", "@", "%", "\\", "\u2028", "\u2029"))
        ):
            raise DotReportError(f"unsafe {field}")
        if re.search(r"(?i)(?:token|secret|password|api[_-]?key)\s*[:=]", decoded):
            raise DotReportError(f"unsafe {field}")
        if re.search(
            r"(?i)\b(?:bearer|basic)\s+\S|\b(?:authorization|proxy-authorization|cookie|set-cookie)\s*:", decoded
        ):
            raise DotReportError(f"unsafe {field}")
        path_text = decoded
        if https_reference and path_text.startswith("https://"):
            # An evidence URL is separately host-validated below. Its path is
            # an HTTPS reference, never a local path. Reject backslashes.
            path_text = "" if "\\" not in path_text else path_text
        if re.search(r"(?<![\w./-])(?:/|~[/\\]|[A-Za-z]:[\\/]|\\\\)", path_text):
            raise DotReportError(f"unsafe {field}")
    except WorkloreValidationError:
        raise DotReportError(f"unsafe or oversized {field}") from None
    return value


def _public_evidence_host(host: str) -> None:
    """Syntactic public-host policy only. Never resolve DNS or claim reachability."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        labels = host.lower().split(".")
        if (
            len(labels) < 2
            or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part) for part in labels)
            or labels[-1] in {"localhost", "local", "internal", "test", "invalid", "home", "lan"}
            or labels[-1].isdigit()
        ):
            raise DotReportError("evidence URL requires a syntactically public host") from None
    else:
        if not address.is_global or address.is_multicast or address.is_unspecified:
            raise DotReportError("evidence URL requires a global unicast address")


def _opaque(raw: object, field: str) -> str:
    value = _text(raw, field, 128)
    if _OPAQUE.fullmatch(value) is None:
        raise DotReportError(f"invalid {field}")
    return value


def _integer(raw: object, field: str, *, minimum: int = 0) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or not minimum <= raw <= 2**53 - 1:
        raise DotReportError(f"invalid {field}")
    return raw


def validate_cloud_context(raw: object) -> dict[str, Any]:
    """Shared client/Hub validation for the versioned presence-only envelope."""
    if not isinstance(raw, Mapping) or set(raw) - _CLOUD_FIELDS:
        raise DotReportError("unsupported cloud context fields")
    if type(raw.get("version")) is not int or raw["version"] != 1 or raw.get("provider") != "dot":
        raise DotReportError("unsupported cloud context version or provider")
    context: dict[str, Any] = {
        "version": 1,
        "provider": "dot",
        "agent_label": _opaque(raw.get("agent_label"), "agent_label"),
        "sequence": _integer(raw.get("sequence"), "sequence"),
    }
    source = raw.get("source", "explicit-metadata")
    scope = raw.get("source_scope", "explicit-session")
    scopes = {
        "explicit-metadata": {"explicit-session"},
        "cloud_threads": {"caller-created-tasks", "authorized-visible-threads"},
    }
    if not isinstance(source, str) or source not in scopes or not isinstance(scope, str) or scope not in scopes[source]:
        raise DotReportError("unsupported metadata source or visibility scope")
    if raw.get("coverage", "explicitly-reported-sessions") != "explicitly-reported-sessions":
        raise DotReportError("unsupported inventory coverage")
    context.update(source=source, source_scope=scope, coverage="explicitly-reported-sessions")
    stamp = _text(raw.get("observed_at"), "observed_at", 64)
    try:
        observed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if observed.tzinfo is None:
            raise ValueError
        context["observed_at"] = observed.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError):
        raise DotReportError("observed_at requires a timezone-aware ISO timestamp") from None
    for field in ("parent_session_id", "work_id"):
        if field in raw:
            context[field] = _opaque(raw[field], field)
    return context


@dataclass(frozen=True)
class DotReport:
    """Validated metadata. No auth state or local checkout is carried."""

    snapshot: SessionSnapshot
    summaries: Mapping[str, str]
    evidence_refs: tuple[str, ...]
    ownership_revision: int | None
    generation: int | None


def parse_report(raw: object) -> DotReport:
    """Strict allowlist and bounds, before any side effect or authentication."""
    if not isinstance(raw, Mapping) or set(raw) - _FIELDS:
        raise DotReportError("unsupported report fields")
    try:
        if len(json.dumps(dict(raw), ensure_ascii=False).encode("utf-8")) > MAX_REPORT_BYTES:
            raise DotReportError("report exceeds byte bound")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        raise DotReportError("report must be bounded JSON metadata") from None
    session_id = _opaque(raw.get("session_id"), "session_id")
    context = validate_cloud_context({k: v for k, v in raw.items() if k in _CLOUD_FIELDS} | {"provider": "dot"})
    if context.get("parent_session_id") == session_id:
        raise DotReportError("session cannot parent itself")
    repo = ""
    if "repo_identity" in raw:
        repo = _text(raw["repo_identity"], "repo_identity", 512)
        if (
            _REPO.fullmatch(repo) is None
            or validate_repo_identity(repo) is None
            or _parse_remote("https://" + repo) != repo
        ):
            raise DotReportError("repo_identity requires canonical host/owner/repository")
        if any(part in {".", ".."} for part in repo.split("/")):
            raise DotReportError("invalid repo_identity")
    revision = generation = None
    if "work_id" in context:
        revision = _integer(raw.get("ownership_revision"), "ownership_revision")
        generation = _integer(raw.get("generation"), "generation", minimum=1)
        if revision > 9999999999 or generation > 9999999999:
            raise DotReportError("ownership counters exceed protocol bound")
    elif "ownership_revision" in raw or "generation" in raw:
        raise DotReportError("ownership revision/generation require an explicit work_id")
    summaries = {
        field: _text(raw[field], field, MAX_SUMMARY_CHARS)
        for field in ("task", "progress", "blocker", "result")
        if field in raw
    }
    evidence = raw.get("evidence_refs", [])
    if not isinstance(evidence, list) or len(evidence) > MAX_EVIDENCE_REFS:
        raise DotReportError("evidence_refs exceeds entry bound or is not an array")
    references: list[str] = []
    for entry in evidence:
        reference = _text(entry, "evidence_refs", 256, https_reference=True)
        if reference.startswith("https://"):
            try:
                parsed = urlsplit(reference)
                port = parsed.port
            except ValueError:
                raise DotReportError("invalid evidence reference") from None
            if not parsed.hostname or parsed.username or parsed.password or port not in {None, 443}:
                raise DotReportError("invalid evidence reference")
            _public_evidence_host(parsed.hostname)
        elif _OPAQUE.fullmatch(reference) is None:
            raise DotReportError("evidence reference requires opaque ID or public HTTPS URL")
        references.append(reference)
    return DotReport(
        snapshot=SessionSnapshot(
            harness="dot",
            session_id=session_id,
            repo_identity=repo,
            identity_scope="fleet" if repo else "node",
            repo_label=repo.rsplit("/", 1)[-1],
            checkout_path=None,
            branch=None,
            dirty_paths=(),
            dirty_truncated=False,
            cloud_context=context,
        ),
        summaries=summaries,
        evidence_refs=tuple(references),
        ownership_revision=revision,
        generation=generation,
    )


def read_report(data: bytes) -> object:
    """Decode one bounded document. Duplicate JSON fields are refused."""
    if len(data) > MAX_REPORT_BYTES:
        raise DotReportError("report exceeds byte bound")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise DotReportError("duplicate report field")
            result[key] = value
        return result

    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=unique)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise DotReportError("report requires valid bounded JSON") from None


def report_session(raw: object, *, publish: bool = False, holder_nonce: str | None = None) -> dict[str, Any]:
    """Validate/project, or explicitly publish through supported fleet clients."""
    from . import fleet_client

    report = parse_report(raw)
    if holder_nonce is not None:
        from .worklore_ownership import nonce_hash, _reject_nonce_echo

        try:
            nonce_hash(holder_nonce)
            _reject_nonce_echo(raw, "", holder_nonce)
        except WorkloreValidationError:
            raise DotReportError("invalid holder capability or secret in metadata") from None
    context = dict(report.snapshot.cloud_context or {})
    work_id = context.get("work_id")
    result: dict[str, Any] = {
        "mode": "publish" if publish else "dry-run",
        "inventory_coverage": "explicitly-reported-sessions",
        "provider_lifecycle": "unobserved",
        "evidence_verification": "reported-unverified",
        "presence_payload": fleet_client._session_request_body(report.snapshot, action="upsert"),
        "work_projection": ownership_request_projection(report, work_id) if isinstance(work_id, str) else None,
        "nonpersisted_metadata": None
        if work_id
        else {"summaries": dict(report.summaries), "reported_unverified_refs": list(report.evidence_refs)},
        "permissions": {
            "presence": "enrolled node token",
            "work": "enrolled current holder, generation, ownership If-Match and holder capability"
            if work_id
            else "none",
        },
        "summaries_persisted": False,
        "components": {"presence": "projected", "work": "projected" if work_id else "not-requested"},
    }
    if not publish:
        return result
    # A configured node token is required, even for presence-only publication.
    # Never silently use shared-admin credentials for this cloud adapter.
    settings = fleet_client.load_fleet_settings()
    node_id = fleet_client.resolve_node_id()
    if not settings.get("node_token") or not fleet_client._node_id_is_claimable(node_id):
        result["components"]["presence"] = "failed:node_auth_required"
        result["components"]["work"] = "not-attempted" if work_id else "not-requested"
        return result
    presence = fleet_client.publish_session(report.snapshot)
    reason = presence.reason or "unconfirmed"
    known_refusal = reason.startswith("http_status:") or reason in {
        "hub_unconfigured",
        "node_auth_required",
        "unknown_node_identity",
    }
    result["components"]["presence"] = (
        "published" if presence.ok else f"{'failed' if known_refusal else 'unknown'}:{reason}"
    )
    # Stale/replayed presence must not independently mutate Worklore.
    if not presence.ok:
        result["components"]["work"] = "not-attempted" if work_id else "not-requested"
        return result
    if isinstance(work_id, str):
        _publish_work(report, work_id, node_id, holder_nonce, result)
    return result


def ownership_report_envelope(report: DotReport) -> dict[str, Any]:
    """Presence sequence is independent from the Worklore ownership revision."""
    envelope = dict(report.snapshot.cloud_context or {})
    envelope.pop("work_id", None)
    envelope["session_id"] = report.snapshot.session_id
    if report.snapshot.repo_identity:
        envelope["repo_identity"] = report.snapshot.repo_identity
    envelope.update(report.summaries)
    envelope["evidence_refs"] = list(report.evidence_refs)
    return envelope


def validate_ownership_report(raw: object) -> dict[str, Any]:
    """Normalize the optional ownership report envelope using the same allowlist."""
    if not isinstance(raw, Mapping) or raw.get("provider") != "dot":
        raise DotReportError("unsupported report provider")
    if any(field in raw for field in ("work_id", "ownership_revision", "generation")):
        raise DotReportError("ownership report authority must be outside metadata")
    parsed = parse_report({k: v for k, v in raw.items() if k != "provider"})
    result = ownership_report_envelope(parsed)
    result["evidence_verification"] = "reported-unverified"
    result["provider_lifecycle"] = "unobserved"
    return result


def ownership_request_projection(report: DotReport, work_id: str) -> dict[str, Any]:
    """Exact non-secret request projection used by dry-run and publication."""
    envelope = ownership_report_envelope(report)
    key_data = {
        "work_id": work_id,
        "generation": report.generation,
        "session_id": report.snapshot.session_id,
        "sequence": envelope["sequence"],
    }
    key = "dot-report-" + hashlib.sha256(json.dumps(key_data, sort_keys=True).encode()).hexdigest()
    return {
        "work_id": work_id,
        "ownership_revision": report.ownership_revision,
        "idempotency_key": key,
        "body": {"action": "report", "generation": report.generation, "report": envelope},
    }


def _publish_work(
    report: DotReport, work_id: str, node_id: str, holder_nonce: str | None, result: dict[str, Any]
) -> None:
    from . import worklore_client

    if holder_nonce is None or report.ownership_revision is None or report.generation is None:
        result["components"]["work"] = "failed:holder-capability-required"
        return
    projection = ownership_request_projection(report, work_id)
    envelope = projection["body"]["report"]
    # Session/sequence identity stays fixed when metadata changes. The existing
    # ownership fingerprint binds the full content, holder and CAS revision.
    key = projection["idempotency_key"]
    try:
        published = worklore_client.report_ownership(
            work_id,
            envelope,
            generation=report.generation,
            if_match=report.ownership_revision,
            idempotency_key=key,
            holder_nonce=holder_nonce,
        )
        owner = published.get("ownership")
        expected_report = validate_ownership_report(envelope) | {"reporter_node": node_id}
        if (
            not isinstance(owner, dict)
            or owner.get("revision") != report.ownership_revision + 1
            or owner.get("generation") != report.generation
            or owner.get("owner_node") != node_id
            or owner.get("state") != "owned"
            or owner.get("last_report") != expected_report
        ):
            result["components"]["work"] = "unknown:invalid-report-response"
            return
        result["components"]["work"] = "reported"
        result["summaries_persisted"] = bool(report.summaries)
    except worklore_client.FleetClientError as exc:
        code = worklore_client._error_code(getattr(exc, "code", None))
        result["components"]["work"] = (
            f"failed:{code}" if code and code != "hub-unavailable" else "unknown:client-error"
        )
