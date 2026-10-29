"""PR rules section of the fleet policy document (``pr_rules``).

A rule is a human-readable, human-editable statement about review and CI for
pull requests ("doc-only PRs skip CodeRabbit and CI"). Agents read the same
structured rule. Phase 1 only stores, displays, edits, and prints rules: nothing
here touches GitHub or enforces anything.

The section rides the versioned policy document, so it inherits revisions,
compare-and-swap saves, preview, history, and rollback. It is omitted from the
document when empty so every earlier revision keeps its stored digest.

Unknown conditions, unknown actions, and unknown fields are rejected, never
ignored. This module imports ``fleet_policy`` lazily, inside the parse
functions, because ``fleet_policy`` imports this module to validate the section.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

MAX_RULES = 50
MAX_TITLE = 120
MAX_WHY = 300
MAX_NOTE = 200
MAX_THEN = 10
MAX_PATHS = 20
MAX_PATH = 120
MAX_REPOS = 20
MAX_LIMIT_COUNT = 100

REVIEWERS = ("coderabbit", "greptile", "codex")
REVIEWER_LABELS = {"coderabbit": "CodeRabbit", "greptile": "Greptile", "codex": "Codex"}
PERIODS = ("day", "week")
DEFAULT_DOCS_PATHS = ("docs/**", "*.md")

# Conditions and the parameters each one accepts. Anything else is rejected.
CONDITIONS: dict[str, tuple[str, ...]] = {
    "docs_only": ("paths",),
    "review_quota_exhausted": ("reviewer",),
    "label_present": ("label",),
    "repository": ("repos",),
    "always": (),
}
CONDITION_LABELS = {
    "docs_only": "A PR only changes docs",
    "review_quota_exhausted": "A reviewer is out of reviews",
    "label_present": "A PR has a label",
    "repository": "A PR is in certain repositories",
    "always": "Every PR",
}
# Actions take no argument, one argument, or (limit_reviewer) a reviewer and a rate.
ACTION_KINDS = (
    "skip_label",
    "require_label",
    "skip_ci",
    "use_reviewer",
    "limit_reviewer",
    "no_paid_credits",
    "note",
)
ACTION_LABELS = {
    "skip_label": "Do not add a label",
    "require_label": "Add a label",
    "skip_ci": "Skip CI",
    "use_reviewer": "Use a reviewer",
    "limit_reviewer": "Limit a reviewer",
    "no_paid_credits": "Never spend paid credits",
    "note": "Leave a note",
}

RULE_FIELDS = ("id", "title", "when", "then", "scope", "why", "enabled")
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
LABEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}$")
REPO_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9._-]{1,100}$")
PATH_PATTERN = re.compile(r"^[A-Za-z0-9._/*?-]{1,%d}$" % MAX_PATH)
LIMIT_PATTERN = re.compile(r"^([1-9][0-9]{0,2})_per_(day|week)$")


def _error(where: str, message: str) -> Exception:
    from . import fleet_policy as policy

    return policy.FleetPolicyError(f"fleet policy {where} {message}")


def _no_control(value: str, where: str) -> None:
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise _error(where, "must not contain control characters")


def _string(raw: Any, where: str, *, limit: int, optional: bool = False) -> str | None:
    if raw is None:
        if optional:
            return None
        raise _error(where, "is required")
    if not isinstance(raw, str):
        raise _error(where, "must be a string")
    if len(raw) > limit:
        raise _error(where, f"must be at most {limit} characters")
    _no_control(raw, where)
    return raw


def _repos(raw: Any, where: str, *, required: bool) -> list[str]:
    if raw is None:
        if required:
            raise _error(where, "is required")
        return []
    if not isinstance(raw, list):
        raise _error(where, "must be a list")
    if required and not raw:
        raise _error(where, "must hold at least one repository")
    if len(raw) > MAX_REPOS:
        raise _error(where, f"must hold at most {MAX_REPOS} repositories")
    repos: list[str] = []
    for index, item in enumerate(raw):
        if not isinstance(item, str) or not REPO_PATTERN.match(item):
            raise _error(f"{where}[{index}]", "must be an owner/name repository")
        if item in repos:
            raise _error(f"{where}[{index}]", f"repeats repository {item!r}")
        repos.append(item)
    return repos


def _reviewer(raw: Any, where: str) -> str:
    if not isinstance(raw, str) or raw not in REVIEWERS:
        raise _error(where, f"must be one of: {', '.join(REVIEWERS)}")
    return raw


def _label(raw: Any, where: str) -> str:
    if not isinstance(raw, str) or not LABEL_PATTERN.match(raw):
        raise _error(where, f"must match {LABEL_PATTERN.pattern}")
    return raw


def _parse_when(raw: Any, where: str) -> dict[str, Any]:
    from . import fleet_policy as policy

    body = policy._mapping(raw, where)
    condition = body.get("condition")
    if not isinstance(condition, str) or condition not in CONDITIONS:
        raise _error(f"{where} field 'condition'", f"must be one of: {', '.join(CONDITIONS)}")
    policy._reject_unknown(body, ("condition", *CONDITIONS[condition]), f"{where} ({condition})")
    when: dict[str, Any] = {"condition": condition}
    if condition == "docs_only":
        paths = body.get("paths")
        if paths is None:
            when["paths"] = list(DEFAULT_DOCS_PATHS)
        else:
            if not isinstance(paths, list) or not paths:
                raise _error(f"{where} field 'paths'", "must be a non-empty list")
            if len(paths) > MAX_PATHS:
                raise _error(f"{where} field 'paths'", f"must hold at most {MAX_PATHS} patterns")
            for index, item in enumerate(paths):
                if not isinstance(item, str) or not PATH_PATTERN.match(item):
                    raise _error(f"{where} field 'paths'[{index}]", f"must match {PATH_PATTERN.pattern}")
            if len(set(paths)) != len(paths):
                raise _error(f"{where} field 'paths'", "must not repeat a pattern")
            when["paths"] = list(paths)
    elif condition == "review_quota_exhausted":
        when["reviewer"] = _reviewer(body.get("reviewer"), f"{where} field 'reviewer'")
    elif condition == "label_present":
        when["label"] = _label(body.get("label"), f"{where} field 'label'")
    elif condition == "repository":
        when["repos"] = _repos(body.get("repos"), f"{where} field 'repos'", required=True)
    return when


def parse_action(raw: Any, where: str) -> str:
    """Validate one action string and return its canonical form."""
    if not isinstance(raw, str):
        raise _error(where, "must be a string")
    _no_control(raw, where)
    kind, separator, argument = raw.partition(":")
    if kind not in ACTION_KINDS:
        raise _error(where, f"has unknown action {kind!r}; use one of: {', '.join(ACTION_KINDS)}")
    if kind == "skip_ci":
        if separator:
            raise _error(where, "skip_ci takes no argument")
        return kind
    if not separator:
        raise _error(where, f"{kind} needs an argument")
    if kind in ("skip_label", "require_label"):
        return f"{kind}:{_label(argument, where)}"
    if kind in ("use_reviewer", "no_paid_credits"):
        return f"{kind}:{_reviewer(argument, where)}"
    if kind == "limit_reviewer":
        reviewer, _, rate = argument.partition(":")
        if LIMIT_PATTERN.match(rate) is None or int(rate.split("_", 1)[0]) > MAX_LIMIT_COUNT:
            raise _error(where, "limit_reviewer needs <reviewer>:<n>_per_<day|week> with n from 1 to 100")
        return f"limit_reviewer:{_reviewer(reviewer, where)}:{rate}"
    text = _string(argument, where, limit=MAX_NOTE)
    assert text is not None
    if not text.strip():
        raise _error(where, "note must not be empty")
    return f"note:{text}"


def _parse_then(raw: Any, where: str) -> list[str]:
    if not isinstance(raw, list) or not raw:
        raise _error(where, "must be a non-empty list of actions")
    if len(raw) > MAX_THEN:
        raise _error(where, f"must hold at most {MAX_THEN} actions")
    actions = [parse_action(item, f"{where}[{index}]") for index, item in enumerate(raw)]
    if len(set(actions)) != len(actions):
        raise _error(where, "must not repeat an action")
    skipped = {a.split(":", 1)[1] for a in actions if a.startswith("skip_label:")}
    required = {a.split(":", 1)[1] for a in actions if a.startswith("require_label:")}
    clash = sorted(skipped & required)
    if clash:
        raise _error(where, f"both skips and requires label {clash[0]!r}")
    return actions


def _parse_scope(raw: Any, where: str) -> dict[str, list[str]]:
    from . import fleet_policy as policy

    if raw is None:
        return {"repos": []}
    body = policy._mapping(raw, where)
    policy._reject_unknown(body, ("repos",), where)
    return {"repos": _repos(body.get("repos"), f"{where} field 'repos'", required=False)}


def _parse_rule(raw: Any, index: int) -> dict[str, Any]:
    from . import fleet_policy as policy

    where = f"pr_rules[{index}]"
    body = policy._mapping(raw, where)
    policy._reject_unknown(body, RULE_FIELDS, where)
    rule_id = body.get("id")
    if not isinstance(rule_id, str) or not ID_PATTERN.match(rule_id):
        raise _error(f"{where} field 'id'", f"must be a lowercase slug matching {ID_PATTERN.pattern}")
    where = f"pr_rules '{rule_id}'"
    title = _string(body.get("title"), f"{where} field 'title'", limit=MAX_TITLE)
    assert title is not None
    if not title.strip():
        raise _error(f"{where} field 'title'", "must not be empty")
    why = _string(body.get("why"), f"{where} field 'why'", limit=MAX_WHY, optional=True)
    enabled = body.get("enabled")
    if enabled is not None and type(enabled) is not bool:
        raise _error(f"{where} field 'enabled'", "must be a boolean")
    return {
        "id": rule_id,
        "title": title,
        "when": _parse_when(body.get("when"), f"{where} when"),
        "then": _parse_then(body.get("then"), f"{where} field 'then'"),
        "scope": _parse_scope(body.get("scope"), f"{where} scope"),
        "why": why or None,
        "enabled": True if enabled is None else enabled,
    }


def parse_pr_rules(raw: Any) -> list[dict[str, Any]]:
    """Validate and normalize the ``pr_rules`` list. ``None`` means no rules. Idempotent."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise _error("pr_rules", "must be a list")
    if len(raw) > MAX_RULES:
        raise _error("pr_rules", f"must hold at most {MAX_RULES} rules")
    rules = [_parse_rule(item, index) for index, item in enumerate(raw)]
    seen: set[str] = set()
    for rule in rules:
        if rule["id"] in seen:
            raise _error("pr_rules", f"repeats rule id {rule['id']!r}")
        seen.add(rule["id"])
    return rules


def by_id(rules: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Rules keyed by id, so a diff reports one changed rule instead of one big list."""
    return {str(rule["id"]): dict(rule) for rule in rules}


# --- plain language and agent output ------------------------------------------


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def when_text(when: Mapping[str, Any]) -> str:
    condition = when["condition"]
    if condition == "docs_only":
        return f"When a PR only changes docs ({', '.join(when['paths'])})"
    if condition == "review_quota_exhausted":
        return f"When {REVIEWER_LABELS[when['reviewer']]} is out of reviews"
    if condition == "label_present":
        return f"When a PR has the label {when['label']}"
    if condition == "repository":
        return f"When a PR is in {', '.join(when['repos'])}"
    return "For every PR"


def action_text(action: str) -> str:
    kind, _, argument = action.partition(":")
    if kind == "skip_label":
        return f"do not add the {argument} label"
    if kind == "require_label":
        return f"add the {argument} label"
    if kind == "skip_ci":
        return "skip CI"
    if kind == "use_reviewer":
        return f"use {REVIEWER_LABELS[argument]} for review"
    if kind == "limit_reviewer":
        reviewer, _, rate = argument.partition(":")
        count, _, period = rate.partition("_per_")
        return f"use {REVIEWER_LABELS[reviewer]} at most {_plural(int(count), 'time')} per {period}"
    if kind == "no_paid_credits":
        return f"never spend paid {REVIEWER_LABELS[argument]} credits"
    return argument if argument.rstrip().endswith((".", "!", "?")) else argument + "."


def sentence(rule: Mapping[str, Any]) -> str:
    """The rule in plain language, generated from its structured fields only."""
    parts: list[str] = []
    for index, action in enumerate(rule["then"]):
        text = action_text(action)
        text = text[0].upper() + text[1:] if index else text
        parts.append(text if text.endswith((".", "!", "?")) else text + ".")
    return f"{when_text(rule['when'])}: {' '.join(parts)}"


def scope_text(rule: Mapping[str, Any]) -> str:
    repos = rule["scope"]["repos"]
    return "All repos" if not repos else _plural(len(repos), "repo")


def applies_to(rule: Mapping[str, Any], repo: str | None) -> bool:
    """Whether an enabled rule can apply to ``repo``. ``None`` asks about the whole fleet."""
    if not rule["enabled"]:
        return False
    if repo is None:
        return True
    # GitHub treats owner and repository names case-insensitively.
    wanted = repo.casefold()
    scoped = [item.casefold() for item in rule["scope"]["repos"]]
    if scoped and wanted not in scoped:
        return False
    when = rule["when"]
    return not (when["condition"] == "repository" and wanted not in [item.casefold() for item in when["repos"]])


def rules_for(rules: Sequence[Mapping[str, Any]], repo: str | None) -> list[Mapping[str, Any]]:
    return [rule for rule in rules if applies_to(rule, repo)]


def when_compact(when: Mapping[str, Any]) -> str:
    condition = when["condition"]
    if condition == "docs_only":
        return f"docs_only({','.join(when['paths'])})"
    if condition == "review_quota_exhausted":
        return f"review_quota_exhausted({when['reviewer']})"
    if condition == "label_present":
        return f"label_present({when['label']})"
    if condition == "repository":
        return f"repository({','.join(when['repos'])})"
    return "always"


def compact_lines(rules: Sequence[Mapping[str, Any]], *, revision: object, repo: str | None) -> list[str]:
    """Stable, quotable lines for agents. Order is the stored rule order."""
    selected = rules_for(rules, repo)
    lines = [f"pr-rules revision={revision} repo={repo or 'all'} rules={len(selected)}"]
    for rule in selected:
        lines.append(str(rule["id"]))
        lines.append(f"  when {when_compact(rule['when'])}")
        lines.extend(f"  then {action}" for action in rule["then"])
    return lines
