"""Fleet policy form parsing and scoped document construction.

Split out of ``fleet_policy_page`` so the page module stays under the
module-size ceiling. ``fleet_policy_page`` re-exports every public name here,
so callers and tests keep addressing them through the page module.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs

from . import fleet_policy, fleet_policy_pr_rules

_MAX_FIELDS = 1024
SCOPES = ("defaults", "seat", "consumer", "repository", "pr_rules", "rollback")
ACTIONS = ("preview", "save")
RUNTIME_ROLES: tuple[str, ...] = ("impl", "review", "research", "scout", "security", "chef")
ADMISSION_ROLE = "admission_default"
POLICY_ROLES: tuple[str, ...] = (*RUNTIME_ROLES, ADMISSION_ROLE)
SEAT_TEXT_FIELDS = ("provider", "model", "effort", "cost_class", "retention", "quota_pool", "notes")
SEAT_INT_FIELDS = ("concurrency", "timeout_seconds")
SEAT_LIST_FIELDS = ("eligible_machines", "fallback")
SEAT_BOOL_FIELDS = ("training_allowed", "enabled", "pinned")
# A seat dropdown whose stored value is retired renders this instead of the
# seat name: it means "leave the stored leaf alone". It cannot be a seat name
# (names start with an alphanumeric), so it never selects a retired seat anew.
KEEP_CURRENT = "@keep-current"
_TRUE = frozenset({"1", "yes", "true", "on"})
_FALSE = frozenset({"0", "no", "false"})


class FormError(ValueError):
    """The submitted form is malformed (not a policy failure)."""


class DocumentError(ValueError):
    """A scoped edit could not be turned into a document at all."""


@dataclass(frozen=True)
class Submission:
    scope: str
    action: str
    expected_version: int
    csrf: str
    reason: str
    target: str
    fields: Mapping[str, str]
    review: str = ""

    def value(self, name: str) -> str | None:
        raw = self.fields.get(name)
        return None if raw is None else raw.strip()


def parse_form(raw: bytes) -> Submission:
    """Decode a form body. Only ``field.*`` keys reach the document builders.

    Anything else in the body - notably a caller-supplied inventory claim - is
    dropped here, so a form can never vouch for a model's availability.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FormError("form body is not UTF-8") from exc
    try:
        parsed = parse_qs(text, keep_blank_values=True, max_num_fields=_MAX_FIELDS)
    except ValueError as exc:
        raise FormError("too many form fields") from exc
    first = {key: values[0] for key, values in parsed.items() if values}
    scope = first.get("scope", "").strip()
    if scope not in SCOPES:
        raise FormError(f"unknown form scope {scope!r}")
    action = first.get("action", "").strip()
    if action not in ACTIONS:
        raise FormError(f"unknown form action {action!r}")
    try:
        expected = int(first.get("expected_version", ""))
    except ValueError as exc:
        raise FormError("expected_version must be an integer") from exc
    fields = {key[len("field.") :]: value for key, value in first.items() if key.startswith("field.")}
    return Submission(
        scope=scope,
        action=action,
        expected_version=expected,
        csrf=first.get("csrf", ""),
        reason=first.get("reason", "").strip(),
        target=first.get("target", "").strip(),
        fields=fields,
        review=first.get("review", "").strip(),
    )


def _bool_field(raw: str | None) -> bool:
    return raw is not None and raw.strip().lower() in _TRUE


def _tristate(raw: str | None) -> Any:
    """``None`` means the leaf was not submitted; ``""`` means inherit."""
    if raw is None:
        return None
    value = raw.strip().lower()
    if not value:
        return ""
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    return raw.strip()


def _int_field(raw: str) -> Any:
    """Best-effort int. A non-numeric value is handed to the core validator verbatim."""
    try:
        return int(raw)
    except ValueError:
        return raw


def _list_field(raw: str) -> list[str]:
    return [item for item in (part.strip() for part in raw.replace(",", " ").split()) if item]


def _json_field(raw: str, where: str) -> Any:
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DocumentError(f"{where} is not valid JSON: {exc.msg}") from exc


def _prune(settings: Mapping[str, Any]) -> dict[str, Any]:
    """Drop empty sections so a cleared patch is absent, not a null-filled shell."""
    return {section: dict(body) for section, body in settings.items() if body}


def _patch_from(fields: Mapping[str, str], prefix: str) -> dict[str, Any] | None:
    """Build a sparse settings tree from ``<prefix>_<section>_<key>`` form fields.

    A submitted-but-blank leaf clears that leaf (inherit). A leaf that was not
    submitted at all is left untouched by the caller.
    """
    touched = False
    tree: dict[str, dict[str, Any]] = {"roles": {}, "data": {}, "execution": {}}
    for role in POLICY_ROLES:
        raw = fields.get(f"{prefix}role_{role}")
        if raw is None or raw.strip() == KEEP_CURRENT:
            continue
        touched = True
        value = raw.strip()
        if value:
            tree["roles"][role] = value
    for name in ("allow_training", "retention", "allow_free"):
        raw = fields.get(f"{prefix}data_{name}")
        if raw is None:
            continue
        touched = True
        value = _tristate(raw)
        if value != "":
            tree["data"][name] = value
    for name in ("machine", "concurrency", "timeout_seconds"):
        raw = fields.get(f"{prefix}execution_{name}")
        if raw is None:
            continue
        touched = True
        value = raw.strip()
        if not value:
            continue
        tree["execution"][name] = _int_field(value) if name != "machine" else value
    return _prune(tree) if touched else None


def _merge_patch(existing: Mapping[str, Any], fields: Mapping[str, str], prefix: str) -> dict[str, Any]:
    """Apply the submitted leaves onto an existing sparse tree, leaf by leaf."""
    submitted = _patch_from(fields, prefix)
    if submitted is None:
        return {section: dict(body) for section, body in existing.items()}
    merged: dict[str, dict[str, Any]] = {section: dict(body) for section, body in existing.items()}
    for section in ("roles", "data", "execution"):
        keys = _submitted_keys(fields, prefix, section)
        if not keys:
            continue
        body = dict(merged.get(section, {}))
        for key in keys:
            body.pop(key, None)
        body.update(submitted.get(section, {}))
        merged[section] = body
    return _prune(merged)


def _submitted_keys(fields: Mapping[str, str], prefix: str, section: str) -> list[str]:
    if section == "roles":
        return [
            role
            for role in POLICY_ROLES
            if f"{prefix}role_{role}" in fields and fields[f"{prefix}role_{role}"].strip() != KEEP_CURRENT
        ]
    if section == "data":
        return [name for name in ("allow_training", "retention", "allow_free") if f"{prefix}data_{name}" in fields]
    return [name for name in ("machine", "concurrency", "timeout_seconds") if f"{prefix}execution_{name}" in fields]


def _seat_record(existing: Mapping[str, Any] | None, submission: Submission) -> dict[str, Any]:
    record: dict[str, Any] = dict(existing or {})
    for name in SEAT_TEXT_FIELDS:
        raw = submission.value(name)
        if raw is None:
            continue
        record[name] = raw or None
    for name in SEAT_INT_FIELDS:
        raw = submission.value(name)
        if raw is None:
            continue
        record[name] = _int_field(raw) if raw else None
    for name in SEAT_LIST_FIELDS:
        raw = submission.value(name)
        if raw is None:
            continue
        record[name] = _list_field(raw)
    # Checkbox semantics: the rendered form always carries all three boxes, so
    # an absent key means the operator cleared it.
    for name in SEAT_BOOL_FIELDS:
        record[name] = _bool_field(submission.fields.get(name))
    bindings_raw = submission.value("bindings")
    if bindings_raw is not None:
        record["bindings"] = _json_field(bindings_raw, "seat bindings")
    if record.get("provider") is None:
        raise DocumentError("a seat needs a provider")
    if record.get("model") is None:
        raise DocumentError("a seat needs an exact model id")
    return {
        key: value for key, value in record.items() if value is not None or key in SEAT_TEXT_FIELDS + SEAT_INT_FIELDS
    }


NEW_TARGET = "@new"


def _split_items(raw: str) -> list[str]:
    return [item for item in (part.strip() for part in raw.replace(",", " ").split()) if item]


def _rule_when(fields: Mapping[str, str]) -> dict[str, Any]:
    condition = (fields.get("condition") or "").strip()
    when: dict[str, Any] = {"condition": condition}
    if condition == "docs_only":
        paths = _split_items(fields.get("when_paths") or "")
        if paths:
            when["paths"] = paths
    elif condition == "review_quota_exhausted":
        when["reviewer"] = (fields.get("when_reviewer") or "").strip()
    elif condition == "label_present":
        when["label"] = (fields.get("when_label") or "").strip()
    elif condition == "repository":
        when["repos"] = _split_items(fields.get("when_repos") or "")
    return when


def _rule_actions(fields: Mapping[str, str]) -> list[str]:
    """One action per filled row. Unused row inputs are ignored, an unknown kind is not."""
    actions: list[str] = []
    for index in range(fleet_policy_pr_rules.MAX_THEN):
        kind = (fields.get(f"act_kind_{index}") or "").strip()
        if not kind:
            continue
        reviewer = (fields.get(f"act_reviewer_{index}") or "").strip()
        value = (fields.get(f"act_value_{index}") or "").strip()
        if kind in ("skip_label", "require_label"):
            actions.append(f"{kind}:{value}")
        elif kind in ("use_reviewer", "no_paid_credits"):
            actions.append(f"{kind}:{reviewer}")
        elif kind == "limit_reviewer":
            count = (fields.get(f"act_count_{index}") or "").strip()
            period = (fields.get(f"act_period_{index}") or "").strip()
            actions.append(f"limit_reviewer:{reviewer}:{count}_per_{period}")
        elif kind == "note":
            actions.append(f"note:{(fields.get(f'act_value_{index}') or '').strip()}")
        else:
            actions.append(kind)
    return actions


def _pr_rules_document(document: dict[str, Any], submission: Submission) -> dict[str, Any]:
    """Add, edit, toggle, or delete exactly one PR rule. Other rules are untouched."""
    rules: list[dict[str, Any]] = [dict(rule) for rule in document.get("pr_rules") or []]
    target = submission.target
    fields = submission.fields
    is_new = target == NEW_TARGET
    rule_id = (fields.get("id") or "").strip() if is_new else target
    if not rule_id:
        raise DocumentError("a PR rule needs an id")
    index = next((i for i, rule in enumerate(rules) if rule["id"] == rule_id), None)
    if is_new and index is not None:
        raise DocumentError(f"PR rule {rule_id!r} already exists")
    if not is_new and index is None:
        raise DocumentError(f"PR rule {rule_id!r} does not exist")
    if _bool_field(fields.get("delete")):
        if index is not None:
            rules.pop(index)
    elif (toggle := (fields.get("toggle") or "").strip()) and index is not None:
        if toggle not in ("enable", "disable"):
            raise DocumentError("toggle must be enable or disable")
        rules[index]["enabled"] = toggle == "enable"
    else:
        rule = {
            "id": rule_id,
            "title": fields.get("title", "").strip(),
            "when": _rule_when(fields),
            "then": _rule_actions(fields),
            "scope": {"repos": _split_items(fields.get("scope_repos") or "")},
            "why": (fields.get("why") or "").strip() or None,
            "enabled": _bool_field(fields.get("enabled")),
        }
        if index is None:
            rules.append(rule)
        else:
            rules[index] = rule
    if rules:
        document["pr_rules"] = rules
    else:
        document.pop("pr_rules", None)
    return document


def build_document(current: Mapping[str, Any], submission: Submission) -> dict[str, Any]:
    """One scoped edit applied to the current document. Never a wholesale copy-in."""
    document = json.loads(json.dumps(fleet_policy.parse_document(current)))
    if submission.scope == "defaults":
        document["defaults"] = _merge_patch(document["defaults"], submission.fields, "")
        return document
    if submission.scope == "pr_rules":
        return _pr_rules_document(document, submission)
    target = submission.target
    if not target:
        raise DocumentError(f"a {submission.scope} edit needs a target")
    delete = _bool_field(submission.fields.get("delete"))
    if submission.scope == "seat":
        if delete:
            document["seats"].pop(target, None)
            return document
        document["seats"][target] = _seat_record(document["seats"].get(target), submission)
        return document
    if submission.scope == "consumer":
        if delete:
            document["consumers"].pop(target, None)
            return document
        record = dict(document["consumers"].get(target) or {})
        for name in ("reload", "coverage", "adapter_version", "notes"):
            raw = submission.value(name)
            if raw is not None:
                record[name] = raw or None
        if "legacy_bootstrap" in submission.fields:
            record["legacy_bootstrap"] = _bool_field(submission.fields.get("legacy_bootstrap"))
        record["default_patches"] = _merge_patch(record.get("default_patches") or {}, submission.fields, "")
        bindings_raw = submission.value("seat_bindings")
        if bindings_raw is not None:
            record["seat_bindings"] = _json_field(bindings_raw, "consumer seat bindings")
        document["consumers"][target] = {key: value for key, value in record.items() if value is not None}
        return document
    if submission.scope == "repository":
        if delete:
            document["repositories"].pop(target, None)
            return document
        record = dict(document["repositories"].get(target) or {})
        for name in ("privacy", "owner", "notes"):
            raw = submission.value(name)
            if raw is not None:
                record[name] = raw or None
        raw_machines = submission.value("eligible_machines")
        if raw_machines is not None:
            record["eligible_machines"] = _list_field(raw_machines)
        record["patches"] = _merge_patch(record.get("patches") or {}, submission.fields, "patch_")
        document["repositories"][target] = {key: value for key, value in record.items() if value is not None}
        return document
    raise DocumentError(f"scope {submission.scope!r} does not build a document")


# --- planning and apply -------------------------------------------------------
