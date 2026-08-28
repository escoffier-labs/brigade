"""Bounded Wazuh alert normalization, redaction, and fingerprints."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping, Sequence

from .. import grokbot_findings
from .contracts import MAX_RULE_ID, PRODUCER, SEVERITIES, omit_undefined, parse_identifier

MAX_REDACTED_BYTES = 4_096
_PID_RE = re.compile(r"\bpid=\d+\b", re.IGNORECASE)
_URL_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s]+")
_CREDENTIAL_URI_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)([^/\s@]+)@")
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:\[(?:[0-9A-Fa-f]{0,4}:){2,}[0-9A-Fa-f:]{0,4}(?:%[A-Za-z0-9._-]+)?\]|"
    r"(?:[0-9A-Fa-f]{0,4}:){2,}[0-9A-Fa-f:]{0,4}(?:%[A-Za-z0-9._-]+)?)(?![A-Za-z0-9])"
)
_HOSTNAME_RE = re.compile(r"\b[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+\b")
_QUOTED_PATH_RE = re.compile(r"(['\"])(?:[A-Za-z]:[\\/]|/)[^\"'\r\n]*\1")
_BARE_PATH_RE = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|/)[^|;\r\n]*")
_TOKEN_RE = re.compile(r"\b(?:ghp|gho|github_pat|sk|Bearer)[_-][A-Za-z0-9._-]{8,}\b", re.IGNORECASE)
_AUTHORIZATION_BEARER_RE = re.compile(r"(?i)\bAuthorization\s*:\s*Bearer\s+\S+")
_BEARER_VALUE_RE = re.compile(r"(?i)\bBearer\s+\S+")
_SENSITIVE_KEY = r"api[_-]?key|secret|credential|private[_-]?key|password|token"
_SENSITIVE_EQUALS_RE = re.compile(rf"(?i)\b({_SENSITIVE_KEY})\s*=\s*\S+")
_SENSITIVE_COLON_RE = re.compile(rf"(?i)\b({_SENSITIVE_KEY})\s*:\s*\S+")
_SENSITIVE_JSON_RE = re.compile(
    rf'(?i)(?P<q>["\'])(?P<key>{_SENSITIVE_KEY})(?P=q)\s*:\s*(?P<vq>["\'])(?:(?!(?P=vq)).)*(?P=vq)'
)
RULE_ID_CLASSES = {
    "501": "agent-disconnected",
    "503": "agent-disconnected",
    "504": "agent-disconnected",
    "510": "fim-change",
    "530": "critical-storage",
    "533": "service-failure",
    "550": "port-change",
    "5501": "windows-logon",
    "5502": "windows-logon",
    "5503": "auth-failure",
    "5710": "auth-brute-force",
    "5712": "auth-brute-force",
    "18104": "windows-installer",
    "18107": "windows-installer",
    "19001": "sca-repeat",
    "19002": "sca-repeat",
    "80790": "sca-repeat",
}
GROUP_CLASSES = {
    "agent_disconnected": "agent-disconnected",
    "authentication_failed": "auth-failure",
    "authentication_success": "windows-logon",
    "sca": "sca-repeat",
    "service_availability": "service-failure",
    "syslog_sshd": "auth-brute-force",
    "syscheck": "fim-change",
}
ESCALATE_CLASSES = frozenset(
    {
        "agent-disconnected",
        "auth-brute-force",
        "critical-storage",
        "service-failure",
    }
)
NOISE_CLASSES = frozenset(
    {
        "lxc-pseudo-file",
        "sca-repeat",
        "windows-installer",
        "windows-logon",
    }
)
WATCH_CLASSES = frozenset({"fim-change", "port-change", "unknown"})


def utf8_byte_length(value: str) -> int:
    return len(value.encode("utf-8"))


def truncate_utf8(text: str, max_bytes: int) -> str:
    result: list[str] = []
    used = 0
    for char in text:
        next_size = utf8_byte_length(char)
        if used + next_size > max_bytes:
            break
        result.append(char)
        used += next_size
    return "".join(result)


def sanitize_detail(value: str, secrets: Sequence[str] = ()) -> str:
    try:
        sanitized = value
        ordered = sorted({secret for secret in secrets if secret}, key=len, reverse=True)
        for secret in ordered:
            bounded = re.compile(rf"(?<![A-Za-z0-9]){re.escape(secret)}(?![A-Za-z0-9])")
            sanitized = bounded.sub("[redacted]", sanitized)
        sanitized = _AUTHORIZATION_BEARER_RE.sub("[redacted]", sanitized)
        sanitized = _BEARER_VALUE_RE.sub("[redacted]", sanitized)
        sanitized = _SENSITIVE_JSON_RE.sub(r"\g<q>\g<key>\g<q>: \g<vq>[redacted]\g<vq>", sanitized)
        sanitized = _SENSITIVE_EQUALS_RE.sub(r"\1=[redacted]", sanitized)
        sanitized = _SENSITIVE_COLON_RE.sub(r"\1:[redacted]", sanitized)
        sanitized = _TOKEN_RE.sub("[redacted]", sanitized)
        sanitized = _PID_RE.sub("pid=[redacted]", sanitized)
        sanitized = _URL_RE.sub("[redacted]", sanitized)
        sanitized = _CREDENTIAL_URI_RE.sub(r"\1[redacted]@", sanitized)
        sanitized = _IPV4_RE.sub("[redacted]", sanitized)
        sanitized = _IPV6_RE.sub("[redacted]", sanitized)
        sanitized = _HOSTNAME_RE.sub("[redacted]", sanitized)
        sanitized = _QUOTED_PATH_RE.sub(r"\1[redacted]\1", sanitized)
        sanitized = _BARE_PATH_RE.sub("[redacted]", sanitized)
        return truncate_utf8(sanitized, MAX_REDACTED_BYTES)
    except Exception:
        return "[redacted]"


def rule_class_for(alert: Mapping[str, Any]) -> str:
    mapped = RULE_ID_CLASSES.get(str(alert.get("rule_id")))
    if mapped == "fim-change" and _is_lxc_pseudo_file(alert):
        return "lxc-pseudo-file"
    if mapped is not None:
        return mapped
    for group in alert.get("rule_groups") or ():
        if group in GROUP_CLASSES:
            if GROUP_CLASSES[group] == "fim-change" and _is_lxc_pseudo_file(alert):
                return "lxc-pseudo-file"
            return GROUP_CLASSES[group]
    return "unknown"


def severity_for_level(level: int) -> str:
    if level >= 15:
        return "critical"
    if level >= 12:
        return "high"
    if level >= 8:
        return "warning"
    if level >= 4:
        return "medium"
    if level >= 1:
        return "info"
    return "unknown"


def fingerprint_for(alert: Mapping[str, Any], rule_class: str) -> str:
    payload = {
        "agent_id": alert["agent_id"],
        "producer": PRODUCER,
        "rule_class": rule_class,
        "rule_id": alert["rule_id"],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def semantic_revision(
    *,
    severity: str,
    title: str,
    body: str,
    source_digest: str,
    content_digest: str,
) -> str:
    payload = {
        "body": body,
        "content_digest": content_digest,
        "severity": severity,
        "source_digest": source_digest,
        "title": title,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def normalize_alert(alert: Mapping[str, Any], secrets: Sequence[str] = ()) -> dict[str, Any]:
    rule_class = rule_class_for(alert)
    try:
        agent_id = parse_identifier(alert["agent_id"])
    except Exception:
        agent_id = "unknown"
        rule_class = "unknown"
    raw_title = str(alert.get("rule_description") or rule_class).strip() or rule_class
    title = sanitize_detail(raw_title, secrets)[: grokbot_findings.MAX_TITLE_CHARS].strip() or rule_class
    body = sanitize_detail(str(alert.get("detail") or "[redacted]"), secrets)
    if not body.strip():
        body = "[redacted]"
    try:
        rule_id = parse_identifier(alert.get("rule_id") or "unknown", maximum=MAX_RULE_ID)
    except Exception:
        rule_id = "unknown"
        rule_class = "unknown"
    finding_id = f"{agent_id}:{rule_class}:{rule_id}"
    observed_at = str(alert.get("timestamp") or "")
    severity = severity_for_level(int(alert["rule_level"])) if type(alert.get("rule_level")) is int else "unknown"
    if severity not in SEVERITIES:
        severity = "unknown"
    source_ref = f"{PRODUCER}:{rule_class}"
    digest = fingerprint_for(alert, rule_class)
    source_digest = f"sha256:{digest}"
    digest_title = title[: grokbot_findings.MAX_TITLE_CHARS]
    content = grokbot_findings.content_digest(digest_title, body)
    record = {
        "producer": PRODUCER,
        "finding_id": finding_id,
        "revision": semantic_revision(
            severity=severity,
            title=digest_title,
            body=body,
            source_digest=source_digest,
            content_digest=content,
        ),
        "observed_at": observed_at,
        "severity": severity,
        "title": digest_title,
        "body": body,
        "source_ref": source_ref,
        "source_digest": source_digest,
        "content_digest": content,
        "fingerprint": digest,
        "rule_class": rule_class,
        "agent_id": agent_id,
        "rule_id": rule_id,
    }
    return omit_undefined(record)


def _is_lxc_pseudo_file(alert: Mapping[str, Any]) -> bool:
    decoder = str(alert.get("decoder") or "")
    groups = {str(item) for item in (alert.get("rule_groups") or ())}
    return decoder == "lxc-syscheck" or "lxc" in groups or "container" in groups
