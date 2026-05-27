"""Read-only security scanner for agent workspaces."""
from __future__ import annotations

import ast
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import work_cmd

SEVERITY_ORDER = {
    "info": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}
CONFIG_REL_PATH = ".brigade/security.toml"
ARTIFACTS_REL_PATH = ".brigade/security/latest"
POLICIES = {
    "personal": {
        "fail_on": "critical",
        "include_templates": False,
    },
    "public-repo": {
        "fail_on": "high",
        "include_templates": False,
    },
    "strict": {
        "fail_on": "medium",
        "include_templates": True,
    },
}

SKIP_DIRS = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "node_modules",
}

SKIP_PREFIXES = (
    (".brigade", "runs"),
    (".brigade", "security"),
    (".brigade", "work"),
    (".claude", "memory-handoffs"),
    (".codex", "memory-handoffs"),
)

TEXT_SUFFIXES = {
    "",
    ".bash",
    ".cfg",
    ".conf",
    ".env",
    ".ini",
    ".json",
    ".jsonl",
    ".md",
    ".mjs",
    ".py",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}

SECRET_VALUE_RE = re.compile(
    r"(?i)\b(api[_-]?key|secret|token|password|passwd|pwd)\b\s*[:=]\s*['\"]?([A-Za-z0-9_./+=:-]{16,})"
)
PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
ENV_ASSIGNMENT_RE = re.compile(r"(?i)\b[A-Z0-9_]*(TOKEN|SECRET|PASSWORD|API_KEY)\s*=\s*[A-Za-z0-9_./+=:-]{16,}")
REMOTE_SHELL_RE = re.compile(r"\b(curl|wget)\b[^\n|;]*(\||;)\s*(sh|bash)\b")
DESTRUCTIVE_RE = re.compile(r"\b(rm\s+-rf|git\s+reset\s+--hard|git\s+clean\s+-fdx|chmod\s+777)\b")
UNPINNED_NPX_RE = re.compile(r"\bnpx\s+(?:-y\s+)?([a-zA-Z0-9_.-]+)(?:\s|$)")
ENV_DUMP_RE = re.compile(r"\b(env|printenv|set)\b.*(>\s*\S+|\|\s*(curl|nc|netcat|tee))")
UNPINNED_ACTION_RE = re.compile(r"uses:\s*['\"]?([^@\s'\":]+/[^@\s'\"]+|docker://[^@\s'\"]+)['\"]?\s*$")
PINNED_ACTION_RE = re.compile(r"uses:\s*['\"]?([^@\s'\"]+)@([^@\s'\"]+)")
PYTHON_URL_DEP_RE = re.compile(r"(?i)(https?://|git\+https?://|git\+ssh://)")
HTTP_MCP_RE = re.compile(r'"url"\s*:\s*"https?://')
AUTO_APPROVE_RE = re.compile(r"(?i)(auto[_-]?approve|always[_-]?allow|allow[_-]?all)")
PROMPT_INJECTION_RE = re.compile(
    r"(?i)(ignore (all )?(previous|prior) instructions|do not (tell|reveal)|hidden instruction|"
    r"send (all )?(secrets|tokens)|exfiltrat|disable safety|bypass safety)"
)
MCP_SENSITIVE_ARG_RE = re.compile(
    r"(^|/)(\.env|id_rsa|id_ed25519|credentials|known_hosts|passwd|shadow)$|"
    r"(\.ssh/|\.aws/|\.config/gh/|\.docker/|/etc/passwd|/etc/shadow)",
    re.IGNORECASE,
)
MCP_BROAD_PATHS = {"~", "$HOME", "/", "/home", "/Users"}
MCP_HIGH_RISK_COMMANDS = {"bash", "sh", "zsh", "fish", "powershell", "pwsh", "docker", "podman", "ssh", "scp", "rsync"}
MCP_SERVER_COUNT_WARN = 8
MCP_SHELL_META_RE = re.compile(r"[;&|`<>]|\$\(")
FINGERPRINT_RE = re.compile(r"^[a-f0-9]{16}$")
GITHUB_ACTION_FLOATING_REFS = {"main", "master", "latest", "dev", "develop", "trunk", "head"}


@dataclass(frozen=True)
class SecurityConfig:
    policy: str = "personal"
    fail_on: str | None = None
    include_templates: bool | None = None
    suppressions: tuple[str, ...] = ()
    suppression_reasons: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class EffectivePolicy:
    policy: str
    fail_on: str
    include_templates: bool
    suppressions: tuple[str, ...]
    config_path: Path
    config_loaded: bool


def config_path(target: Path) -> Path:
    return target / CONFIG_REL_PATH


def default_artifacts_dir(target: Path) -> Path:
    return target / ARTIFACTS_REL_PATH


def inspect_evidence_bundle(path: Path) -> dict[str, Any]:
    path = path.expanduser().resolve()
    json_path = path / "security-report.json"
    markdown_path = path / "security-report.md"
    if not path.is_dir():
        return {"ready": False, "path": str(path), "reason": "missing"}
    missing = [item.name for item in (json_path, markdown_path) if not item.is_file()]
    if missing:
        return {"ready": False, "path": str(path), "reason": f"missing {', '.join(missing)}"}
    try:
        payload = json.loads(json_path.read_text())
    except json.JSONDecodeError as exc:
        return {"ready": False, "path": str(path), "reason": f"invalid JSON: {exc}"}
    if not isinstance(payload, dict):
        return {"ready": False, "path": str(path), "reason": "security-report.json must contain an object"}
    return {
        "ready": True,
        "path": str(path),
        "generated_at": payload.get("generated_at"),
        "finding_count": payload.get("finding_count"),
        "policy": payload.get("policy"),
    }


def _parse_toml_value(raw: str) -> object:
    value = raw.strip()
    if value == "true":
        return True
    if value == "false":
        return False
    try:
        return ast.literal_eval(value)
    except (SyntaxError, ValueError):
        return value


def _read_toml_object(path: Path) -> dict[str, object]:
    data: dict[str, object] = {}
    current = data
    for line_number, raw_line in enumerate(path.read_text().splitlines(), start=1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            table = line[1:-1].strip()
            if table not in {"suppressions", "suppression_reasons"}:
                raise ValueError(f"invalid security config line {line_number}: unsupported table [{table}]")
            current = data.setdefault(table, {})
            if not isinstance(current, dict):
                raise ValueError(f"invalid security config line {line_number}: {table} must be a table")
            continue
        if "=" not in line:
            raise ValueError(f"invalid security config line {line_number}: expected key = value")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"invalid security config line {line_number}: empty key")
        current[key] = _parse_toml_value(raw_value)
    return data


def load_config(target: Path) -> SecurityConfig | None:
    path = config_path(target.expanduser().resolve())
    if not path.is_file():
        return None
    data = _read_toml_object(path)
    policy = data.get("policy", "personal")
    if not isinstance(policy, str) or policy not in POLICIES:
        raise ValueError("policy must be one of: personal, public-repo, strict")
    fail_on = data.get("fail_on")
    if fail_on is not None and (not isinstance(fail_on, str) or fail_on not in SEVERITY_ORDER and fail_on != "none"):
        raise ValueError("fail_on must be one of: none, low, medium, high, critical")
    include_templates = data.get("include_templates")
    if include_templates is not None and not isinstance(include_templates, bool):
        raise ValueError("include_templates must be true or false")
    suppressions: tuple[str, ...] = ()
    raw_suppressions = data.get("suppressions", {})
    if raw_suppressions:
        if not isinstance(raw_suppressions, dict):
            raise ValueError("suppressions must be a table")
        fingerprints = raw_suppressions.get("fingerprints", [])
        if not isinstance(fingerprints, list) or not all(isinstance(item, str) for item in fingerprints):
            raise ValueError("suppressions.fingerprints must be a list of strings")
        suppressions = tuple(item.strip() for item in fingerprints if item.strip())
    suppression_reasons: dict[str, str] = {}
    raw_reasons = data.get("suppression_reasons", {})
    if raw_reasons:
        if not isinstance(raw_reasons, dict):
            raise ValueError("suppression_reasons must be a table")
        for fingerprint, reason in raw_reasons.items():
            if not isinstance(fingerprint, str) or not isinstance(reason, str):
                raise ValueError("suppression_reasons entries must be string = string")
            if fingerprint.strip() and reason.strip():
                suppression_reasons[fingerprint.strip()] = reason.strip()
    return SecurityConfig(
        policy=policy,
        fail_on=fail_on,
        include_templates=include_templates,
        suppressions=suppressions,
        suppression_reasons=suppression_reasons,
    )


def _effective_policy(
    target: Path,
    *,
    policy: str | None,
    fail_on: str | None,
    include_templates: bool | None,
) -> EffectivePolicy:
    loaded = load_config(target)
    policy_name = policy or (loaded.policy if loaded is not None else "personal")
    if policy_name not in POLICIES:
        raise ValueError("policy must be one of: personal, public-repo, strict")
    preset = POLICIES[policy_name]
    effective_fail_on = fail_on or (loaded.fail_on if loaded and loaded.fail_on is not None else str(preset["fail_on"]))
    if include_templates is not None:
        effective_include_templates = include_templates
    elif loaded and loaded.include_templates is not None:
        effective_include_templates = loaded.include_templates
    else:
        effective_include_templates = bool(preset["include_templates"])
    if effective_fail_on not in SEVERITY_ORDER and effective_fail_on != "none":
        raise ValueError("fail_on must be one of: none, low, medium, high, critical")
    return EffectivePolicy(
        policy=policy_name,
        fail_on=effective_fail_on,
        include_templates=effective_include_templates,
        suppressions=loaded.suppressions if loaded is not None else (),
        config_path=config_path(target),
        config_loaded=loaded is not None,
    )


def write_default_config(target: Path, *, force: bool = False) -> Path:
    path = config_path(target.expanduser().resolve())
    if path.exists() and not force:
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                'policy = "personal"',
                'fail_on = "critical"',
                "include_templates = false",
                "",
                "[suppressions]",
                "fingerprints = []",
                "",
                "[suppression_reasons]",
                "",
            ]
        )
    )
    return path


def _toml_string(value: str) -> str:
    return json.dumps(value)


def write_config(target: Path, config: SecurityConfig) -> Path:
    path = config_path(target.expanduser().resolve())
    path.parent.mkdir(parents=True, exist_ok=True)
    fingerprints = ", ".join(_toml_string(item) for item in config.suppressions)
    lines = [
        f"policy = {_toml_string(config.policy)}",
        f"fail_on = {_toml_string(config.fail_on or POLICIES[config.policy]['fail_on'])}",
        f"include_templates = {str(config.include_templates if config.include_templates is not None else POLICIES[config.policy]['include_templates']).lower()}",
        "",
        "[suppressions]",
        f"fingerprints = [{fingerprints}]",
        "",
        "[suppression_reasons]",
    ]
    reasons = config.suppression_reasons
    for fingerprint in config.suppressions:
        reason = reasons.get(fingerprint)
        if reason:
            lines.append(f"{fingerprint} = {_toml_string(reason)}")
    lines.append("")
    path.write_text("\n".join(lines))
    return path


def _load_config_or_default(target: Path) -> SecurityConfig:
    loaded = load_config(target)
    if loaded is not None:
        return loaded
    return SecurityConfig()


def _clean_reason(reason: str) -> str:
    return " ".join(reason.replace("#", " ").split()).strip()


def _gitignore_selection(target: Path):
    from .config import load_config
    from .selection import Selection

    loaded = load_config(target)
    if loaded is not None:
        return loaded.selection
    return Selection(depth="repo", harnesses=[], owner="this-repo", includes=[])


def fix(*, target: Path, dry_run: bool = False) -> int:
    from . import dogfood_cmd
    from .install import apply_gitignore

    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    try:
        selection = _gitignore_selection(target)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"error: invalid Brigade config: {exc}", file=sys.stderr)
        return 2

    artifacts_root = default_artifacts_dir(target).parent
    print(f"security fix: {target}")
    if dry_run:
        print("dry_run: True")
        print(f"would_create: {artifacts_root}")
        print("would_update: .gitignore")
        return 0

    artifacts_root.mkdir(parents=True, exist_ok=True)
    result = apply_gitignore(target, selection)
    config_ignored = dogfood_cmd._check_git_ignored(target, config_path(target))
    artifacts_ignored = dogfood_cmd._check_git_ignored(target, artifacts_root)
    print(f"security_artifacts_dir: {artifacts_root}")
    print(f"gitignore: {result}")
    print(f"security_config_ignored: {config_ignored}")
    print(f"security_artifacts_ignored: {artifacts_ignored}")
    return 0


def _load_report(output_dir: Path) -> dict[str, Any]:
    path = output_dir.expanduser().resolve() / "security-report.json"
    if not path.is_file():
        raise FileNotFoundError(path)
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"security report must be a JSON object: {path}")
    return data


def _report_findings_for_review(target: Path, report: dict[str, Any]) -> list[dict[str, Any]]:
    config = load_config(target) or SecurityConfig()
    suppressed = set(config.suppressions)
    reasons = config.suppression_reasons
    records: list[dict[str, Any]] = []
    for finding in report.get("findings", []):
        if not isinstance(finding, dict):
            continue
        fingerprint = str(finding.get("fingerprint") or "")
        record = dict(finding)
        record["status"] = "suppressed" if fingerprint in suppressed else "open"
        if fingerprint in reasons:
            record["reason"] = reasons[fingerprint]
        records.append(record)
    for finding in report.get("suppressed_findings", []):
        if not isinstance(finding, dict):
            continue
        fingerprint = str(finding.get("fingerprint") or "")
        record = dict(finding)
        record["status"] = "suppressed"
        if fingerprint in reasons:
            record["reason"] = reasons[fingerprint]
        records.append(record)
    records.sort(
        key=lambda item: (
            -SEVERITY_ORDER.get(str(item.get("severity")), 0),
            str(item.get("category") or ""),
            str(item.get("path") or ""),
            int(item.get("line") or 0),
        )
    )
    return records


def review(*, target: Path, output_dir: Path | None = None, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    artifacts_dir = output_dir.expanduser().resolve() if output_dir is not None else default_artifacts_dir(target)
    try:
        report = _load_report(artifacts_dir)
        records = _report_findings_for_review(target, report)
    except FileNotFoundError as exc:
        print(f"error: security report not found: {exc}", file=sys.stderr)
        return 2
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"error: invalid security report: {exc}", file=sys.stderr)
        return 2

    payload = {
        "artifacts": str(artifacts_dir),
        "generated_at": report.get("generated_at"),
        "policy": report.get("policy"),
        "findings": records,
        "finding_count": len(records),
        "open_count": len([item for item in records if item.get("status") != "suppressed"]),
        "suppressed_count": len([item for item in records if item.get("status") == "suppressed"]),
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"security review: {artifacts_dir}")
    print(f"generated_at: {payload['generated_at']}")
    print(f"policy: {payload['policy']}")
    print(f"findings: {payload['finding_count']}")
    print(f"open: {payload['open_count']}")
    print(f"suppressed: {payload['suppressed_count']}")
    current_group: tuple[str, str] | None = None
    for finding in records:
        group = (str(finding.get("severity") or "unknown"), str(finding.get("category") or "unknown"))
        if group != current_group:
            current_group = group
            print(f"{group[0]} / {group[1]}:")
        print(
            f"- {finding.get('fingerprint')} [{finding.get('status')}] "
            f"{finding.get('path')}:{finding.get('line')} {finding.get('title')}"
        )
        if finding.get("reason"):
            print(f"  reason: {finding['reason']}")
        print(f"  suggestion: {finding.get('suggestion')}")
    return 0


def suppress(*, target: Path, fingerprint: str, reason: str) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    fingerprint = fingerprint.strip()
    cleaned_reason = _clean_reason(reason)
    if not FINGERPRINT_RE.match(fingerprint):
        print("error: fingerprint must be a 16-character lowercase hex value", file=sys.stderr)
        return 2
    if not cleaned_reason:
        print("error: --reason is required", file=sys.stderr)
        return 2
    try:
        config = _load_config_or_default(target)
    except ValueError as exc:
        print(f"error: invalid security config: {exc}", file=sys.stderr)
        return 2
    suppressions = list(config.suppressions)
    if fingerprint not in suppressions:
        suppressions.append(fingerprint)
    reasons = dict(config.suppression_reasons)
    reasons[fingerprint] = cleaned_reason
    path = write_config(
        target,
        SecurityConfig(
            policy=config.policy,
            fail_on=config.fail_on,
            include_templates=config.include_templates,
            suppressions=tuple(suppressions),
            suppression_reasons=reasons,
        ),
    )
    print(f"security_config: {path}")
    print(f"suppressed: {fingerprint}")
    print(f"reason: {cleaned_reason}")
    return 0


def unsuppress(*, target: Path, fingerprint: str) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    fingerprint = fingerprint.strip()
    if not FINGERPRINT_RE.match(fingerprint):
        print("error: fingerprint must be a 16-character lowercase hex value", file=sys.stderr)
        return 2
    try:
        config = _load_config_or_default(target)
    except ValueError as exc:
        print(f"error: invalid security config: {exc}", file=sys.stderr)
        return 2
    if fingerprint not in config.suppressions and fingerprint not in config.suppression_reasons:
        print(f"error: suppression not found: {fingerprint}", file=sys.stderr)
        return 1
    suppressions = tuple(item for item in config.suppressions if item != fingerprint)
    reasons = dict(config.suppression_reasons)
    reasons.pop(fingerprint, None)
    path = write_config(
        target,
        SecurityConfig(
            policy=config.policy,
            fail_on=config.fail_on,
            include_templates=config.include_templates,
            suppressions=suppressions,
            suppression_reasons=reasons,
        ),
    )
    print(f"security_config: {path}")
    print(f"unsuppressed: {fingerprint}")
    return 0


def suppression_health(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    config = load_config(target)
    if config is None:
        return {"suppression_count": 0, "missing_reasons": [], "stale": []}
    if not config.suppressions:
        return {"suppression_count": 0, "missing_reasons": [], "stale": []}
    effective = _effective_policy(target, policy=None, fail_on=None, include_templates=None)
    report = scan_target(target, include_templates=effective.include_templates, suppressions=())
    active = {str(item.get("fingerprint")) for item in report["findings"] if item.get("fingerprint")}
    stale = [fingerprint for fingerprint in config.suppressions if fingerprint not in active]
    missing_reasons = [fingerprint for fingerprint in config.suppressions if not config.suppression_reasons.get(fingerprint)]
    return {
        "suppression_count": len(config.suppressions),
        "missing_reasons": missing_reasons,
        "stale": stale,
    }


def _short(text: str, limit: int = 160) -> str:
    rendered = " ".join(text.split())
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 3].rstrip() + "..."


def _is_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(
        marker in lowered
        for marker in (
            "example",
            "placeholder",
            "changeme",
            "your_",
            "your-",
            "<",
            "{{",
            "xxxxx",
            "dummy",
        )
    )


def _redact_secret_evidence(line: str) -> str:
    if PRIVATE_KEY_RE.search(line):
        return PRIVATE_KEY_RE.sub("-----BEGIN REDACTED PRIVATE KEY-----", line)

    def redact_secret(match: re.Match[str]) -> str:
        return match.group(0).replace(match.group(2), "[REDACTED]")

    redacted = SECRET_VALUE_RE.sub(redact_secret, line)

    def redact_env(match: re.Match[str]) -> str:
        text = match.group(0)
        if "=" not in text:
            return "[REDACTED]"
        key, _ = text.split("=", 1)
        return f"{key}=[REDACTED]"

    return ENV_ASSIGNMENT_RE.sub(redact_env, redacted)


def _line_number_for(text: str, needle: str) -> int:
    if not needle:
        return 1
    for line_number, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            return line_number
    return 1


def _is_mcp_document(path: Path, text: str) -> bool:
    return "mcp" in path.name.lower() or '"mcpServers"' in text


def _server_timeout(server: dict[str, Any]) -> object:
    for key in ("timeout", "timeout_seconds", "timeoutSeconds", "startupTimeout", "startupTimeoutMs"):
        if key in server:
            return server[key]
    return None


def _scan_mcp_document(findings: list[dict[str, Any]], *, target: Path, path: Path, text: str) -> None:
    if not _is_mcp_document(path, text):
        return
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return
    if not isinstance(data, dict):
        return
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        return
    if len(servers) > MCP_SERVER_COUNT_WARN:
        _finding(
            findings,
            target=target,
            path=path,
            line=_line_number_for(text, "mcpServers"),
            severity="low",
            category="mcp",
            title="Large MCP server set",
            evidence=f"mcpServers: {len(servers)} configured",
            suggestion="Review whether every MCP server is still needed and disable stale or duplicate servers.",
        )
    for server_name, raw_server in servers.items():
        if not isinstance(server_name, str) or not isinstance(raw_server, dict):
            continue
        server = raw_server
        line_number = _line_number_for(text, server_name)
        command = server.get("command")
        args = server.get("args", [])
        command_name = Path(command).name if isinstance(command, str) else None
        if command_name in MCP_HIGH_RISK_COMMANDS:
            _finding(
                findings,
                target=target,
                path=path,
                line=line_number,
                severity="medium",
                category="mcp",
                title="MCP high-risk local command",
                evidence=f"{server_name}: command={command}",
                suggestion="Prefer purpose-built MCP binaries with narrow capabilities over direct shell, container, or remote-copy commands.",
            )
        if isinstance(command, str) and command == "npx" and isinstance(args, list):
            package = _first_npx_package(args)
            if package and "@" not in package:
                _finding(
                    findings,
                    target=target,
                    path=path,
                    line=line_number,
                    severity="medium",
                    category="mcp",
                    title="MCP unpinned npx package",
                    evidence=f"{server_name}: npx {package}",
                    suggestion="Pin MCP package versions or install through a reviewed lockfile.",
                )
        if isinstance(args, list):
            for arg in args:
                if not isinstance(arg, str):
                    continue
                if MCP_SHELL_META_RE.search(arg):
                    _finding(
                        findings,
                        target=target,
                        path=path,
                        line=line_number,
                        severity="high",
                        category="mcp",
                        title="MCP shell metacharacter in argument",
                        evidence=f"{server_name}: arg={arg}",
                        suggestion="Remove shell metacharacters from MCP args and pass structured arguments directly.",
                    )
                if arg in MCP_BROAD_PATHS:
                    _finding(
                        findings,
                        target=target,
                        path=path,
                        line=line_number,
                        severity="medium",
                        category="mcp",
                        title="MCP broad filesystem argument",
                        evidence=f"{server_name}: arg={arg}",
                        suggestion="Scope MCP filesystem access to explicit project directories instead of home or filesystem roots.",
                    )
                if MCP_SENSITIVE_ARG_RE.search(arg):
                    _finding(
                        findings,
                        target=target,
                        path=path,
                        line=line_number,
                        severity="medium",
                        category="mcp",
                        title="MCP sensitive file argument",
                        evidence=f"{server_name}: arg={arg}",
                        suggestion="Avoid passing broad sensitive file paths to MCP servers; scope access to explicit project files.",
                    )
        env = server.get("env")
        if isinstance(env, dict):
            for key, value in env.items():
                if not isinstance(key, str) or not isinstance(value, str):
                    continue
                if re.search(r"(?i)(TOKEN|SECRET|PASSWORD|API_KEY)", key) and not _is_placeholder(value):
                    _finding(
                        findings,
                        target=target,
                        path=path,
                        line=line_number,
                        severity="high",
                        category="mcp",
                        title="MCP hardcoded environment secret",
                        evidence=_redact_secret_evidence(f"{server_name}.env.{key}={value}"),
                        suggestion="Load MCP secrets from local environment or secret storage instead of checked-in config.",
                    )
        url = server.get("url")
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            _finding(
                findings,
                target=target,
                path=path,
                line=line_number,
                severity="medium",
                category="mcp",
                title="Remote MCP transport",
                evidence=f"{server_name}: url={url}",
                suggestion="Prefer local MCP servers, pin remote hosts, and document authentication boundaries.",
            )
        if _server_timeout(server) is None:
            _finding(
                findings,
                target=target,
                path=path,
                line=line_number,
                severity="low",
                category="mcp",
                title="MCP server missing timeout",
                evidence=f"{server_name}: timeout unset",
                suggestion="Set an explicit MCP startup or request timeout so hung servers fail predictably.",
            )


def _first_npx_package(args: list[object]) -> str | None:
    skip_next = False
    for arg in args:
        if not isinstance(arg, str):
            continue
        if skip_next:
            skip_next = False
            continue
        if arg in {"-y", "--yes", "--quiet"}:
            continue
        if arg in {"--package", "-p"}:
            skip_next = True
            continue
        if arg.startswith("-"):
            continue
        return arg
    return None


def _scan_package_json(findings: list[dict[str, Any]], *, target: Path, path: Path, text: str) -> None:
    if path.name != "package.json":
        return
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return
    if not isinstance(data, dict):
        return
    scripts = data.get("scripts", {})
    if not isinstance(scripts, dict):
        return
    for name, command in scripts.items():
        if not isinstance(name, str) or not isinstance(command, str):
            continue
        line_number = _line_number_for(text, f'"{name}"')
        evidence = f"scripts.{name}: {command}"
        if REMOTE_SHELL_RE.search(command):
            _finding(
                findings,
                target=target,
                path=path,
                line=line_number,
                severity="high",
                category="supply-chain",
                title="Package script pipes remote content into shell",
                evidence=evidence,
                suggestion="Replace curl-to-shell package scripts with checked-in, pinned, and reviewed installer steps.",
            )
        if DESTRUCTIVE_RE.search(command):
            _finding(
                findings,
                target=target,
                path=path,
                line=line_number,
                severity="medium",
                category="supply-chain",
                title="Package script contains destructive command",
                evidence=evidence,
                suggestion="Gate destructive package scripts behind explicit operator approval and document recovery steps.",
            )
        npx_match = UNPINNED_NPX_RE.search(command)
        if npx_match and "@" not in npx_match.group(1):
            _finding(
                findings,
                target=target,
                path=path,
                line=line_number,
                severity="medium",
                category="supply-chain",
                title="Package script uses unpinned npx",
                evidence=evidence,
                suggestion="Pin npx package versions or move execution behind a reviewed lockfile.",
            )
        if ENV_DUMP_RE.search(command):
            _finding(
                findings,
                target=target,
                path=path,
                line=line_number,
                severity="high",
                category="supply-chain",
                title="Package script may leak environment",
                evidence=evidence,
                suggestion="Avoid dumping environment variables in package scripts, especially near network or file redirection.",
            )


def _scan_github_actions(findings: list[dict[str, Any]], *, target: Path, path: Path, text: str) -> None:
    rel = path.relative_to(target)
    if len(rel.parts) < 3 or rel.parts[0] != ".github" or rel.parts[1] != "workflows":
        return
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("pull_request_target:") or stripped == "- pull_request_target":
            _finding(
                findings,
                target=target,
                path=path,
                line=line_number,
                severity="high",
                category="supply-chain",
                title="GitHub Actions uses pull_request_target",
                evidence=stripped,
                suggestion="Avoid pull_request_target for untrusted code paths or isolate it from checkout and secret access.",
            )
        if stripped.startswith("permissions: write-all"):
            _finding(
                findings,
                target=target,
                path=path,
                line=line_number,
                severity="high",
                category="supply-chain",
                title="GitHub Actions grants write-all permissions",
                evidence=stripped,
                suggestion="Use least-privilege workflow permissions instead of write-all.",
            )
        action_match = UNPINNED_ACTION_RE.search(stripped)
        if action_match:
            _finding(
                findings,
                target=target,
                path=path,
                line=line_number,
                severity="medium",
                category="supply-chain",
                title="GitHub Action missing pinned ref",
                evidence=stripped,
                suggestion="Pin actions to an immutable commit SHA or a reviewed release ref.",
            )
        pinned_match = PINNED_ACTION_RE.search(stripped)
        if pinned_match:
            ref = pinned_match.group(2)
            if ref.lower() in GITHUB_ACTION_FLOATING_REFS or (not ref.startswith("v") and not re.fullmatch(r"[a-fA-F0-9]{40}", ref)):
                _finding(
                    findings,
                    target=target,
                    path=path,
                    line=line_number,
                    severity="medium",
                    category="supply-chain",
                    title="GitHub Action uses floating ref",
                    evidence=stripped,
                    suggestion="Pin GitHub Actions to immutable commit SHAs for release-sensitive workflows.",
                )


def _scan_python_project(findings: list[dict[str, Any]], *, target: Path, path: Path, text: str) -> None:
    if path.name not in {"pyproject.toml", "setup.cfg", "requirements.txt"}:
        return
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if PYTHON_URL_DEP_RE.search(stripped):
            _finding(
                findings,
                target=target,
                path=path,
                line=line_number,
                severity="medium",
                category="supply-chain",
                title="Python dependency uses URL source",
                evidence=stripped,
                suggestion="Prefer pinned package versions or reviewed immutable commit URLs for Python dependencies.",
            )
        if "setup_requires" in stripped or "dependency_links" in stripped:
            _finding(
                findings,
                target=target,
                path=path,
                line=line_number,
                severity="medium",
                category="supply-chain",
                title="Python project uses legacy install hook",
                evidence=stripped,
                suggestion="Avoid legacy install-time dependency hooks and move dependencies into static project metadata.",
            )


def _iter_scan_files(target: Path) -> list[Path]:
    paths: list[Path] = []
    for path in target.rglob("*"):
        if path.is_dir():
            continue
        rel_parts = path.relative_to(target).parts
        if any(part in SKIP_DIRS for part in rel_parts):
            continue
        if any(rel_parts[: len(prefix)] == prefix for prefix in SKIP_PREFIXES):
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            if path.stat().st_size > 500_000:
                continue
        except OSError:
            continue
        paths.append(path)
    paths.sort()
    return paths


def _surface_for(path: Path, target: Path) -> str:
    rel = path.relative_to(target)
    parts = rel.parts
    if parts and parts[0] == ".brigade":
        return "brigade"
    if parts and parts[0] == ".codex":
        return "codex"
    if parts and parts[0] == ".claude":
        return "claude"
    if "mcp" in path.name.lower():
        return "mcp"
    if parts and parts[0] in {"hooks", "scripts"}:
        return "automation"
    if path.name in {"AGENTS.md", "CLAUDE.md", "SAFETY_RULES.md", "INSTALL_FOR_AGENTS.md"}:
        return "agent-instructions"
    return "repo"


def _confidence_for(path: Path, target: Path) -> str:
    rel = path.relative_to(target)
    parts = rel.parts
    if parts and parts[0] == "src" and "templates" in parts:
        return "template"
    if parts and parts[0] in {".brigade", ".claude", ".codex", "hooks", "scripts"}:
        return "runtime"
    if path.name in {"AGENTS.md", "CLAUDE.md", "SAFETY_RULES.md", "INSTALL_FOR_AGENTS.md"}:
        return "runtime"
    return "repo"


def _fingerprint(*, category: str, title: str, rel_path: Path, evidence: str) -> str:
    stable = "\n".join([category, title, str(rel_path), _short(evidence, limit=96)])
    return hashlib.sha256(stable.encode()).hexdigest()[:16]


def _finding(
    findings: list[dict[str, Any]],
    *,
    target: Path,
    path: Path,
    line: int,
    severity: str,
    category: str,
    title: str,
    evidence: str,
    suggestion: str,
) -> None:
    rel = path.relative_to(target)
    finding_id = f"security-{len(findings) + 1:04d}"
    fingerprint = _fingerprint(category=category, title=title, rel_path=rel, evidence=evidence)
    findings.append(
        {
            "id": finding_id,
            "fingerprint": fingerprint,
            "severity": severity,
            "category": category,
            "title": title,
            "path": str(rel),
            "line": line,
            "surface": _surface_for(path, target),
            "confidence": _confidence_for(path, target),
            "evidence": _short(evidence),
            "suggestion": suggestion,
        }
    )


def _scan_line(findings: list[dict[str, Any]], *, target: Path, path: Path, line_number: int, line: str) -> None:
    secret_match = SECRET_VALUE_RE.search(line)
    if secret_match and not _is_placeholder(secret_match.group(2)):
        _finding(
            findings,
            target=target,
            path=path,
            line=line_number,
            severity="high",
            category="secrets",
            title="Possible hardcoded credential",
            evidence=_redact_secret_evidence(line),
            suggestion="Move the value into local environment or secret storage and commit only a placeholder.",
        )
    if PRIVATE_KEY_RE.search(line) or (ENV_ASSIGNMENT_RE.search(line) and not _is_placeholder(line)):
        _finding(
            findings,
            target=target,
            path=path,
            line=line_number,
            severity="high",
            category="secrets",
            title="Possible sensitive secret material",
            evidence=_redact_secret_evidence(line),
            suggestion="Remove secret material from the repo and rotate the credential if it was real.",
        )
    if "danger-full-access" in line or "sandbox_permissions" in line and "require_escalated" in line:
        _finding(
            findings,
            target=target,
            path=path,
            line=line_number,
            severity="medium",
            category="permissions",
            title="Broad agent execution permission",
            evidence=line,
            suggestion="Prefer read-only or workspace-scoped execution unless this is an explicitly trusted local path.",
        )
    if REMOTE_SHELL_RE.search(line):
        _finding(
            findings,
            target=target,
            path=path,
            line=line_number,
            severity="high",
            category="automation",
            title="Remote script piped into shell",
            evidence=line,
            suggestion="Pin and verify downloaded scripts before execution, or replace with a checked-in script.",
        )
    if DESTRUCTIVE_RE.search(line):
        _finding(
            findings,
            target=target,
            path=path,
            line=line_number,
            severity="medium",
            category="automation",
            title="Destructive command pattern",
            evidence=line,
            suggestion="Gate destructive commands behind explicit operator approval and document recovery steps.",
        )
    npx_match = UNPINNED_NPX_RE.search(line)
    if npx_match and "@" not in npx_match.group(1):
        _finding(
            findings,
            target=target,
            path=path,
            line=line_number,
            severity="medium",
            category="supply-chain",
            title="Unpinned remote package execution",
            evidence=line,
            suggestion="Pin remote package versions or install through a reviewed lockfile.",
        )
    if "mcp" in path.name.lower() or '"mcpServers"' in line:
        if HTTP_MCP_RE.search(line):
            _finding(
                findings,
                target=target,
                path=path,
                line=line_number,
                severity="medium",
                category="mcp",
                title="Remote MCP transport",
                evidence=line,
                suggestion="Prefer local MCP servers, pin remote hosts, and document authentication boundaries.",
            )
        if AUTO_APPROVE_RE.search(line):
            _finding(
                findings,
                target=target,
                path=path,
                line=line_number,
                severity="medium",
                category="mcp",
                title="MCP auto-approval pattern",
                evidence=line,
                suggestion="Avoid blanket auto-approval and require review for mutable or networked tools.",
            )
    if _surface_for(path, target) in {"agent-instructions", "claude", "codex", "repo"} and PROMPT_INJECTION_RE.search(line):
        _finding(
            findings,
            target=target,
            path=path,
            line=line_number,
            severity="low",
            category="prompt-injection",
            title="Prompt-injection style instruction",
            evidence=line,
            suggestion="Keep hostile examples clearly labeled as examples and avoid executable language in trusted instructions.",
        )


def scan_target(target: Path, *, include_templates: bool = False, suppressions: tuple[str, ...] = ()) -> dict[str, Any]:
    target = target.expanduser().resolve()
    findings: list[dict[str, Any]] = []
    scanned_files: list[str] = []
    for path in _iter_scan_files(target):
        if not include_templates and _confidence_for(path, target) == "template":
            continue
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        scanned_files.append(str(path.relative_to(target)))
        for line_number, line in enumerate(text.splitlines(), start=1):
            _scan_line(findings, target=target, path=path, line_number=line_number, line=line)
        _scan_mcp_document(findings, target=target, path=path, text=text)
        _scan_package_json(findings, target=target, path=path, text=text)
        _scan_github_actions(findings, target=target, path=path, text=text)
        _scan_python_project(findings, target=target, path=path, text=text)
    suppressed = [finding for finding in findings if finding.get("fingerprint") in suppressions]
    findings = [finding for finding in findings if finding.get("fingerprint") not in suppressions]
    counts: dict[str, int] = {}
    for finding in findings:
        severity = str(finding["severity"])
        counts[severity] = counts.get(severity, 0) + 1
    return {
        "target": str(target),
        "scanned_files": scanned_files,
        "scanned_file_count": len(scanned_files),
        "finding_count": len(findings),
        "suppressed_count": len(suppressed),
        "severity_counts": dict(sorted(counts.items())),
        "findings": findings,
        "suppressed_findings": suppressed,
    }


def _should_fail(findings: list[dict[str, Any]], fail_on: str) -> bool:
    if fail_on == "none":
        return False
    threshold = SEVERITY_ORDER[fail_on]
    return any(SEVERITY_ORDER.get(str(item.get("severity")), 0) >= threshold for item in findings)


def _import_findings(target: Path, findings: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    existing_fingerprints = {
        str(item.get("metadata", {}).get("fingerprint"))
        for item in work_cmd._pending_imports(target)
        if isinstance(item, dict)
        and item.get("source") == "security-scan"
        and isinstance(item.get("metadata"), dict)
        and item.get("metadata", {}).get("fingerprint")
    }
    records = []
    skipped: list[dict[str, Any]] = []
    for finding in findings:
        fingerprint = str(finding.get("fingerprint") or "")
        if fingerprint and fingerprint in existing_fingerprints:
            skipped.append(finding)
            continue
        path = finding.get("path")
        line = finding.get("line")
        title = finding.get("title")
        severity = finding.get("severity")
        category = finding.get("category")
        records.append(
            {
                "text": f"Review security finding [{severity}] {category} in {path}:{line}: {title}",
                "kind": "incident",
                "source": "security-scan",
                "metadata": {
                    "finding_id": finding.get("id"),
                    "fingerprint": finding.get("fingerprint"),
                    "severity": severity,
                    "category": category,
                    "path": path,
                    "line": line,
                    "surface": finding.get("surface"),
                    "confidence": finding.get("confidence"),
                    "suggestion": finding.get("suggestion"),
                },
            }
        )
        if fingerprint:
            existing_fingerprints.add(fingerprint)
    imported, duplicate_records = work_cmd._append_import_records(target, records)
    skipped.extend(duplicate_records)
    return imported, skipped


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _render_markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Brigade Security Report",
        "",
        f"- target: `{report['target']}`",
        f"- generated_at: `{report['generated_at']}`",
        f"- policy: `{report['policy']}`",
        f"- fail_on: `{report['fail_on']}`",
        f"- include_templates: `{report['include_templates']}`",
        f"- scanned_files: `{report['scanned_file_count']}`",
        f"- findings: `{report['finding_count']}`",
        f"- suppressed: `{report['suppressed_count']}`",
        "",
        "## Severity Counts",
        "",
    ]
    if report["severity_counts"]:
        for severity, count in report["severity_counts"].items():
            lines.append(f"- {severity}: {count}")
    else:
        lines.append("- none: 0")
    lines.extend(["", "## Findings", ""])
    if not report["findings"]:
        lines.append("No unsuppressed findings.")
    for finding in report["findings"]:
        lines.extend(
            [
                f"### {finding['id']} - {finding['title']}",
                "",
                f"- fingerprint: `{finding['fingerprint']}`",
                f"- severity: `{finding['severity']}`",
                f"- category: `{finding['category']}`",
                f"- path: `{finding['path']}:{finding['line']}`",
                f"- surface: `{finding['surface']}`",
                f"- confidence: `{finding['confidence']}`",
                f"- evidence: `{finding['evidence']}`",
                f"- suggestion: {finding['suggestion']}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def write_evidence_bundle(report: dict[str, Any], output_dir: Path) -> Path:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    report = dict(report)
    report["artifacts"] = str(output_dir)
    (output_dir / "security-report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    (output_dir / "security-report.md").write_text(_render_markdown_report(report))
    return output_dir


def scan(
    *,
    target: Path,
    json_output: bool = False,
    policy: str | None = None,
    fail_on: str | None = None,
    include_templates: bool | None = None,
    import_findings: bool = False,
    output_dir: Path | None = None,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    try:
        effective = _effective_policy(
            target,
            policy=policy,
            fail_on=fail_on,
            include_templates=include_templates,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    report = scan_target(
        target,
        include_templates=effective.include_templates,
        suppressions=effective.suppressions,
    )
    report["policy"] = effective.policy
    report["fail_on"] = effective.fail_on
    report["include_templates"] = effective.include_templates
    report["config"] = str(effective.config_path)
    report["config_loaded"] = effective.config_loaded
    report["generated_at"] = _utc_iso()
    imported: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    if import_findings and report["findings"]:
        imported, skipped = _import_findings(target, report["findings"])
        report["imported_findings"] = len(imported)
        report["skipped_duplicate_imports"] = len(skipped)
    if output_dir is not None:
        artifacts_dir = write_evidence_bundle(report, output_dir)
        report["artifacts"] = str(artifacts_dir)

    if json_output:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"security scan: {target}")
        print(f"policy: {effective.policy}")
        print(f"fail_on: {effective.fail_on}")
        print(f"include_templates: {effective.include_templates}")
        print(f"scanned_files: {report['scanned_file_count']}")
        print(f"findings: {report['finding_count']}")
        print(f"suppressed: {report['suppressed_count']}")
        for severity, count in report["severity_counts"].items():
            print(f"{severity}: {count}")
        if import_findings:
            print(f"imported_findings: {len(imported)}")
            print(f"skipped_duplicate_imports: {len(skipped)}")
        if output_dir is not None:
            print(f"artifacts: {report['artifacts']}")
        for finding in report["findings"]:
            print(
                f"- [{finding['severity']}] {finding['category']} "
                f"{finding['path']}:{finding['line']} {finding['title']}"
            )
            print(f"  fingerprint: {finding['fingerprint']}")
            print(f"  evidence: {finding['evidence']}")
            print(f"  suggestion: {finding['suggestion']}")

    return 1 if _should_fail(report["findings"], effective.fail_on) else 0


def init(*, target: Path, force: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    try:
        path = write_default_config(target, force=force)
    except FileExistsError as exc:
        print(f"error: security config already exists: {exc.args[0]}", file=sys.stderr)
        return 1
    print(f"security_config: {path}")
    print("policy: personal")
    return 0
