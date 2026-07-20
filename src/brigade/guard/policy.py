from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from importlib import resources
import sys

if sys.version_info >= (3, 11):
    from importlib.resources.abc import Traversable
else:  # pragma: no cover - 3.10 fallback
    from importlib.abc import Traversable
from os import PathLike
from pathlib import Path
from typing import Any

from .rules import DEFAULT_RULES
from .types import Action, Rule

VALID_ACTIONS: set[str] = {"allow", "warn", "redact", "block"}

_AGENT_COAUTHOR_EMAIL = (
    r"(?:noreply@anthropic\.com|codex@openai\.com|cursoragent@cursor\.com|"
    r"[0-9]+@users\.noreply\.github\.com)"
)
_ALLOWED_AGENT_COAUTHOR_TRAILER = re.compile(
    rf"^Co-authored-by:\s*[^<\r\n]+<(?P<email>{_AGENT_COAUTHOR_EMAIL})>\s*$", re.IGNORECASE
)
_UNAPPROVED_COAUTHOR_TRAILER = rf"^Co-authored-by:\s*(?![^\r\n]*<{_AGENT_COAUTHOR_EMAIL}>\s*\r?$).+(?:\r?\n)?"


# Per-rule action defaults that override category-level defaults. Each entry
# represents "this rule should default to X unless the user's policy.rules map
# explicitly overrides it."
#
# Why these exist:
#   - example-* rules are illustrative-only patterns (NANPA 555 phones, RFC 2606
#     example.{com,org,net} emails). They live in the PII category but are
#     almost never real PII. Defaulting them to WARN avoids blocking valid
#     documentation under a "pii: block" policy.
#   - known-host is a synthetic rule generated from policy.known_hosts. It
#     represents user-declared real internal IPs/hostnames. It must BLOCK
#     even under permissive "infrastructure: warn" policies.
_RULE_DEFAULTS: dict[str, Action] = {
    "example-phone-555": "warn",
    "example-email-reserved": "warn",
    "known-host": "block",
}


@dataclass
class OpfBackendConfig:
    enabled: bool = False
    device: str = "cpu"
    bin: str | None = None


@dataclass
class Policy:
    name: str = "default"
    defaults: dict[str, Action] = field(
        default_factory=lambda: {
            "infrastructure": "block",
            "secret": "block",
            "pii": "warn",
            "personal": "block",
            "business": "warn",
            "attribution": "block",
        }
    )
    rules: dict[str, Action] = field(default_factory=dict)
    custom_rules: list[Rule] = field(default_factory=list)
    known_hosts: list[str] = field(default_factory=list)
    # Literal strings that are known-public and safe. A finding whose matched
    # text equals one of these exactly is forced to action "allow" regardless
    # of rule or category. Unlike inline `content-guard: allow` comments, this
    # applies everywhere including history scans of old commit diffs, where no
    # inline marker can exist. Keep personal or environment-specific values in
    # a private policy file, never in a shipped public default policy.
    allow_values: list[str] = field(default_factory=list)
    # In-house commit and repository scans may retain only the explicitly
    # approved agent attribution addresses. Outbound prose policies leave this
    # disabled, so every co-author trailer is still removed.
    allow_agent_coauthors: bool = False
    opf_backend: OpfBackendConfig = field(default_factory=OpfBackendConfig)

    def action_for(self, rule: Rule) -> Action:
        # Per-rule defaults that DEFEAT category-level defaults unless the
        # user explicitly overrides the rule in policy.rules. This is the
        # mechanism for the example-* rules to remain WARN even when a policy
        # blocks the entire PII category, and for known-host to BLOCK even
        # when a policy warns the entire infrastructure category.
        if rule.id not in self.rules and rule.id in _RULE_DEFAULTS:
            return _RULE_DEFAULTS[rule.id]
        return self.rules.get(rule.id) or self.defaults.get(rule.category, "warn")

    def all_rules(self) -> list[Rule]:
        # Synthetic known-host rule runs FIRST so it claims spans before more
        # general patterns (private-ipv4, etc.). Always BLOCK regardless of
        # category default, since these are explicit user-defined real hosts.
        known_rule: list[Rule] = []
        if self.known_hosts:
            pattern = r"\b(?:" + "|".join(re.escape(h) for h in self.known_hosts) + r")\b"
            known_rule.append(
                Rule(
                    id="known-host",
                    category="infrastructure",
                    pattern=pattern,
                    replacement="[redacted-known-host]",
                    description="Known internal host or IP (policy-defined).",
                )
            )
        default_rules = list(DEFAULT_RULES)
        if self.allow_agent_coauthors:
            default_rules = [
                Rule(
                    id=rule.id,
                    category=rule.category,
                    pattern=_UNAPPROVED_COAUTHOR_TRAILER,
                    replacement=rule.replacement,
                    description=rule.description,
                    flags=rule.flags,
                )
                if rule.id == "coauthored-by-trailer"
                else rule
                for rule in default_rules
            ]
        coauthor_rules = [rule for rule in default_rules if rule.id == "coauthored-by-trailer"]
        default_rules = [rule for rule in default_rules if rule.id != "coauthored-by-trailer"]
        default_rules = [*coauthor_rules, *default_rules]
        return [*known_rule, *default_rules, *self.custom_rules]

    def allows_agent_coauthor_email(self, email: str, line: str) -> bool:
        """Allow only the approved bracketed address in an approved trailer."""
        trailer = _ALLOWED_AGENT_COAUTHOR_TRAILER.fullmatch(line.rstrip("\r\n"))
        return bool(self.allow_agent_coauthors and trailer and trailer.group("email").casefold() == email.casefold())


def _as_action(value: Any, where: str) -> Action:
    if not isinstance(value, str) or value not in VALID_ACTIONS:
        raise ValueError(f"{where} must be one of: {', '.join(sorted(VALID_ACTIONS))}")
    return value  # type: ignore[return-value]


def default_policy(name: str) -> Path | Traversable:
    repo_path = Path(__file__).resolve().parents[2] / "policies" / name
    if repo_path.is_file():
        return repo_path

    packaged_path = resources.files("brigade.guard").joinpath("policies", name)
    if packaged_path.is_file():
        return packaged_path

    return repo_path


def load_policy(path: str | PathLike[str] | Traversable | None) -> Policy:
    if path is None:
        return Policy()

    if isinstance(path, (str, PathLike)):
        policy_path = Path(path)
        raw_text = policy_path.read_text()
        fallback_name = policy_path.stem
    else:
        raw_text = path.read_text()
        fallback_name = Path(path.name).stem

    raw = json.loads(raw_text)
    if not isinstance(raw, dict):
        raise ValueError("policy root must be an object")

    defaults = Policy().defaults
    for category, action in raw.get("defaults", {}).items():
        defaults[str(category)] = _as_action(action, f"defaults.{category}")

    rules: dict[str, Action] = {}
    for rule_id, action in raw.get("rules", {}).items():
        rules[str(rule_id)] = _as_action(action, f"rules.{rule_id}")

    custom_rules = [_parse_custom_rule(item, i) for i, item in enumerate(raw.get("custom_rules", []))]
    known_hosts = _parse_known_hosts(raw.get("known_hosts"))
    allow_values = _parse_allow_values(raw.get("allow_values"))
    allow_agent_coauthors = _parse_allow_agent_coauthors(raw.get("allow_agent_coauthors"))
    opf_backend = _parse_opf_backend(
        raw.get("backends", {}).get("opf") if isinstance(raw.get("backends"), dict) else None
    )
    if opf_backend.action:
        rules["opf-pii"] = opf_backend.action

    return Policy(
        name=str(raw.get("name") or fallback_name),
        defaults=defaults,
        rules=rules,
        custom_rules=custom_rules,
        known_hosts=known_hosts,
        allow_values=allow_values,
        allow_agent_coauthors=allow_agent_coauthors,
        opf_backend=OpfBackendConfig(
            enabled=opf_backend.enabled,
            device=opf_backend.device,
            bin=opf_backend.bin,
        ),
    )


def _parse_known_hosts(raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("known_hosts must be a list of host strings")
    hosts: list[str] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError(f"known_hosts[{i}] must be a non-empty string")
        hosts.append(entry.strip())
    return hosts


def _parse_allow_values(raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("allow_values must be a list of literal strings")
    values: list[str] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError(f"allow_values[{i}] must be a non-empty string")
        values.append(entry)
    return values


def _parse_allow_agent_coauthors(raw: Any) -> bool:
    if raw is None:
        return False
    if not isinstance(raw, bool):
        raise ValueError("allow_agent_coauthors must be a boolean")
    return raw


def _parse_custom_rule(item: Any, index: int) -> Rule:
    if not isinstance(item, dict):
        raise ValueError(f"custom_rules[{index}] must be an object")

    try:
        rule_id = str(item["id"])
        category = str(item["category"])
        pattern = str(item["pattern"])
    except KeyError as exc:
        raise ValueError(f"custom_rules[{index}] missing required field {exc.args[0]!r}") from exc

    replacement = str(item.get("replacement", f"[redacted-{category}]"))
    description = str(item.get("description", ""))
    flags = 0
    for flag in item.get("flags", []):
        if flag == "ignorecase":
            flags |= re.IGNORECASE
        elif flag == "multiline":
            flags |= re.MULTILINE
        elif flag == "dotall":
            flags |= re.DOTALL
        else:
            raise ValueError(f"custom_rules[{index}].flags has unsupported flag {flag!r}")

    re.compile(pattern, flags)
    return Rule(
        id=rule_id,
        category=category,
        pattern=pattern,
        replacement=replacement,
        description=description,
        flags=flags,
    )


@dataclass
class _ParsedOpfBackend:
    enabled: bool = False
    device: str = "cpu"
    bin: str | None = None
    action: Action | None = None


def _parse_opf_backend(raw: Any) -> _ParsedOpfBackend:
    if raw is None:
        return _ParsedOpfBackend()
    if not isinstance(raw, dict):
        raise ValueError("backends.opf must be an object")

    enabled = bool(raw.get("enabled", False))
    device = str(raw.get("device", "cpu"))
    opf_bin = raw.get("bin")
    action = raw.get("action")

    return _ParsedOpfBackend(
        enabled=enabled,
        device=device,
        bin=str(opf_bin) if opf_bin else None,
        action=_as_action(action, "backends.opf.action") if action is not None else None,
    )
