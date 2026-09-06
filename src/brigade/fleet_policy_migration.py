"""Explicit preview and CAS activation from legacy roster/preference onto fleet policy.

This slice does not auto-migrate. Preview is read-only. Activation writes one
new policy revision plus a one-row activation marker in the same transaction.
After activation, roster and run-preference *reads* project the current policy
document; legacy writers refuse with ``authority_owned``.

Activation that publishes ``bindings.brigade.model`` requires a consumer that
accepts that optional launch-model leaf. Older signed-roster readers that
require exact ``{"cli"}`` fail closed instead of launching the canonical slug.
"""

from __future__ import annotations

import copy
import hashlib
import sqlite3
from typing import Any, Mapping

from . import fleet_hub, fleet_hub_policy, fleet_model_roster, fleet_policy, run_preference
from .fleet_hub import FleetHubConflict, FleetHubError
from .fleet_model_roster import canonical_json
from .fleet_policy import FleetPolicyError

MIGRATION_SCHEMA = "brigade.fleet_policy_migration.preview.v1"
TABLE = "fleet_policy_migration"
AUTHORITY_OWNED = "authority_owned"
AUTHORITY_OWNED_MESSAGE = "authority_owned: fleet policy owns model roster and run preference writes after migration"
COMPAT_LEGACY = "legacy-writable"
COMPAT_AUTHORITY = "authority-owned"
CLASSIFICATION_FIELDS = ("pinned", "training_allowed")
ANNOTATION_SEAT_FIELDS = (
    "provider",
    "model",
    "effort",
    "eligible_machines",
    "concurrency",
    "timeout_seconds",
    "fallback",
    "training_allowed",
    "retention",
    "quota_pool",
    "enabled",
    "pinned",
    "bindings",
    "notes",
)
_TABLE_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    activated INTEGER NOT NULL CHECK (activated IN (0, 1)),
    activated_at TEXT,
    activated_by TEXT,
    reason TEXT,
    preview_digest TEXT NOT NULL,
    policy_revision INTEGER NOT NULL,
    roster_revision INTEGER NOT NULL,
    preference_digest TEXT,
    candidate_digest TEXT
);
"""
_GUARD_TABLES = (
    ("model_policy", ("INSERT", "UPDATE", "DELETE")),
    ("model_consumer_defaults", ("INSERT", "UPDATE", "DELETE")),
    ("model_roster_meta", ("INSERT", "UPDATE", "DELETE")),
    ("run_preference", ("INSERT", "UPDATE", "DELETE")),
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the additive activation marker and write-guard triggers. No commit."""
    conn.execute(_TABLE_SCHEMA)
    for table, events in _GUARD_TABLES:
        for event in events:
            name = f"fleet_policy_guard_{table}_{event.lower()}"
            conn.execute(
                f"CREATE TRIGGER IF NOT EXISTS {name} BEFORE {event} ON {table} "
                f"WHEN EXISTS (SELECT 1 FROM {TABLE} WHERE singleton=1 AND activated=1) "
                f"BEGIN SELECT RAISE(ABORT, {AUTHORITY_OWNED_MESSAGE!r}); END;"
            )


def _table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone()
    return row is not None


def is_activated(conn: sqlite3.Connection) -> bool:
    """True only after a committed activation marker. Never creates schema."""
    if not _table_exists(conn):
        return False
    row = conn.execute(f"SELECT activated FROM {TABLE} WHERE singleton=1").fetchone()
    return bool(row and int(row[0]) == 1)


def refuse_legacy_write(conn: sqlite3.Connection) -> None:
    """Raise when legacy roster/preference mutation is no longer independent."""
    if is_activated(conn):
        raise FleetHubError(AUTHORITY_OWNED_MESSAGE)


def _sha(payload: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(payload).encode("ascii")).hexdigest()


def _raw_roster_module():
    from . import fleet_hub_model_roster

    return fleet_hub_model_roster


def _raw_preference_module():
    from . import fleet_hub_preference

    return fleet_hub_preference


def _sources(conn: sqlite3.Connection) -> dict[str, Any]:
    roster_mod = _raw_roster_module()
    pref_mod = _raw_preference_module()
    policy = fleet_hub_policy.current_policy(conn)
    seats = roster_mod.raw_seats(conn)
    defaults = roster_mod.raw_consumer_defaults(conn)
    retired = roster_mod.raw_retired_rows(conn)
    revision = roster_mod.raw_revision(conn)
    digest_payload = {
        "schema": fleet_model_roster.ROSTER_SCHEMA,
        "revision": revision,
        "seats": seats,
        "consumer_defaults": defaults,
        "retired_models": retired,
    }
    preference = pref_mod.raw_run_preference(conn)
    return {
        "policy_revision": policy["revision"],
        "policy_digest": policy["digest"],
        "roster_revision": revision,
        "roster_digest": fleet_model_roster.roster_digest(digest_payload),
        "preference": preference,
        "preference_digest": _sha(preference),
    }


def _schema_status(conn: sqlite3.Connection) -> dict[str, Any]:
    present = _table_exists(conn)
    return {"table": TABLE, "present": present, "ready": True, "activated": is_activated(conn)}


def migration_status(conn: sqlite3.Connection) -> dict[str, Any]:
    """Read-only snapshot of schema, source revisions, and compatibility mode."""
    activated = is_activated(conn)
    return {
        "schema": _schema_status(conn),
        "activated": activated,
        "sources": _sources(conn),
        "compatibility_behavior": COMPAT_AUTHORITY if activated else COMPAT_LEGACY,
        "affected": {"consumers": [], "repositories": [], "sessions": []},
    }


def _error(code: str, **fields: Any) -> dict[str, Any]:
    return {"code": code, **fields}


def _parse_annotations(raw: Any) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    if raw is None:
        return {}, []
    if not isinstance(raw, Mapping):
        return {}, [_error("invalid_annotations", detail="migration annotations must be a JSON object")]
    unknown = sorted(set(raw).difference({"seats"}))
    if unknown:
        return {}, [
            _error("invalid_annotations", detail=f"unknown migration annotation field(s): {', '.join(unknown)}")
        ]
    seats_raw = raw.get("seats") or {}
    if not isinstance(seats_raw, Mapping):
        return {}, [_error("invalid_annotations", detail="migration annotation field 'seats' must be a JSON object")]
    parsed: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []
    for name, body in seats_raw.items():
        if not isinstance(name, str) or not name:
            errors.append(_error("invalid_annotations", detail="annotation seat names must be strings"))
            continue
        if not isinstance(body, Mapping):
            errors.append(_error("invalid_annotations", seat=name, detail="annotation seat body must be a JSON object"))
            continue
        extra = sorted(set(body).difference(ANNOTATION_SEAT_FIELDS))
        if extra:
            errors.append(
                _error("invalid_annotations", seat=name, detail=f"unknown annotation seat field(s): {', '.join(extra)}")
            )
            continue
        parsed[str(name)] = dict(body)
    return parsed, errors


def _as_mapping(raw: Any) -> Mapping[str, Any]:
    return raw if isinstance(raw, Mapping) else {}


def _legacy_bindings(seat: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    bindings = _as_mapping(seat.get("bindings"))
    brigade = _as_mapping(bindings.get("brigade"))
    t3_fleet = _as_mapping(bindings.get("t3_fleet"))
    cli = brigade.get("cli") or None
    instance_id = t3_fleet.get("instance_id") or None
    service_tier = t3_fleet.get("service_tier") or None
    return {
        "brigade": {"cli": cli},
        "t3_fleet": {"instance_id": instance_id, "service_tier": service_tier},
    }


def _map_legacy_seat(seat: Mapping[str, Any]) -> dict[str, Any]:
    limit = seat.get("limit")
    mapped = {
        "provider": seat["provider"],
        "model": seat["model"],
        "effort": seat.get("reasoning") or "none",
        "concurrency": 1 if limit is None else limit,
        "enabled": bool(seat.get("enabled")),
        "bindings": _legacy_bindings(seat),
        "pinned": False,
        "training_allowed": False,
    }
    return mapped


def _same_provider_model(mapped: Mapping[str, Any], current: Mapping[str, Any] | None) -> bool:
    if current is None:
        return False
    return current.get("provider") == mapped.get("provider") and current.get("model") == mapped.get("model")


_LEGACY_IDENTITY_FIELDS = frozenset({"provider", "model", "concurrency", "enabled", "bindings", "seat"})


def _overlay_current_seat(mapped: dict[str, Any], current: Mapping[str, Any] | None) -> dict[str, Any]:
    if current is None:
        return mapped
    merged = dict(mapped)
    for field, value in current.items():
        if field in _LEGACY_IDENTITY_FIELDS or field in CLASSIFICATION_FIELDS:
            continue
        merged[field] = copy.deepcopy(value)
    same_identity = _same_provider_model(mapped, current)
    if same_identity:
        for field in CLASSIFICATION_FIELDS:
            if field in current:
                merged[field] = copy.deepcopy(current[field])
    current_bindings = _as_mapping(current.get("bindings"))
    bindings = dict(merged.get("bindings") or {})
    native = current_bindings.get("native")
    if isinstance(native, Mapping):
        native_copy = dict(native)
        if not same_identity:
            native_copy.pop("model", None)
        if native_copy:
            bindings["native"] = copy.deepcopy(native_copy)
    brigade_current = current_bindings.get("brigade")
    if same_identity and isinstance(brigade_current, Mapping):
        launch_model = brigade_current.get("model")
        if isinstance(launch_model, str) and launch_model:
            brigade = dict(bindings.get("brigade") or {})
            brigade["model"] = launch_model
            bindings["brigade"] = brigade
    merged["bindings"] = bindings
    return merged


def _apply_annotation(merged: dict[str, Any], annotation: Mapping[str, Any] | None) -> dict[str, Any]:
    if not annotation:
        return merged
    result = dict(merged)
    for key, value in annotation.items():
        if key == "bindings" and isinstance(value, Mapping):
            bindings = dict(result.get("bindings") or {})
            for group, fields in value.items():
                if not isinstance(fields, Mapping):
                    bindings[group] = fields
                    continue
                target = dict(bindings.get(group) or {})
                target.update(dict(fields))
                bindings[str(group)] = target
            result["bindings"] = bindings
            continue
        result[key] = copy.deepcopy(value)
    return result


def _consumer_record(current: Mapping[str, Any] | None, admission_default: str | None) -> dict[str, Any]:
    record: dict[str, Any] = {}
    if current:
        record = copy.deepcopy(dict(current))
    patches = dict(record.get("default_patches") or {})
    roles = dict(patches.get("roles") or {})
    if admission_default:
        roles["admission_default"] = admission_default
    elif "admission_default" in roles:
        del roles["admission_default"]
    if roles:
        patches["roles"] = roles
    elif "roles" in patches:
        del patches["roles"]
    if patches:
        record["default_patches"] = patches
    elif "default_patches" in record:
        del record["default_patches"]
    if "coverage" not in record:
        record["coverage"] = "unverified"
    return record


def _build_candidate(
    conn: sqlite3.Connection,
    *,
    annotations: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]]]:
    roster_mod = _raw_roster_module()
    pref_mod = _raw_preference_module()
    current = fleet_hub_policy.current_policy(conn)
    current_doc = copy.deepcopy(current["document"])
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    legacy_seats = {item["seat"]: item for item in roster_mod.raw_seats(conn)}
    defaults = roster_mod.raw_consumer_defaults(conn)
    preference = pref_mod.raw_run_preference(conn)
    current_seats = dict(current_doc.get("seats") or {})
    mapped_seats: dict[str, Any] = dict(current_seats)

    for name, legacy in legacy_seats.items():
        current_seat = current_seats.get(name)
        annotation = annotations.get(name)
        mapped = _overlay_current_seat(_map_legacy_seat(legacy), current_seat)
        if not _same_provider_model(mapped, current_seat):
            if annotation is None or any(field not in annotation for field in CLASSIFICATION_FIELDS):
                errors.append(_error("missing_classification", seat=name))
                warnings.append(_error("unclassified-imported-seat", seat=name))
        if legacy.get("limit") is None and (annotation is None or "concurrency" not in annotation):
            warnings.append(_error("unbounded-limit-mapped", seat=name, before=None, after=mapped.get("concurrency")))
        mapped = _apply_annotation(mapped, annotation)
        if current_seat and current_seat.get("pinned") and mapped.get("pinned") is False:
            errors.append(_error("pin-weakened", seat=name))
        retired = fleet_model_roster.retired_reason(
            str(legacy["provider"]), str(legacy["model"]), roster_mod.raw_retired_rows(conn)
        )
        if retired:
            warnings.append(_error("retired-model-seat", seat=name, reason=retired))
        mapped_seats[name] = mapped

    for consumer, seat_name in defaults.items():
        if seat_name and seat_name not in mapped_seats:
            errors.append(_error("consumer_default_missing_seat", consumer=consumer, seat=seat_name))

    roles = dict((current_doc.get("defaults") or {}).get("roles") or {})
    for field in run_preference.ROLE_FIELDS:
        value = preference.get(field)
        if isinstance(value, str) and value:
            roles[field] = value
    defaults_tree = dict(current_doc.get("defaults") or {})
    if roles:
        defaults_tree["roles"] = roles
    if preference.get("notes"):
        warnings.append(_error("preference-notes-not-imported"))

    consumers = dict(current_doc.get("consumers") or {})
    for name in sorted(fleet_model_roster.CONSUMERS):
        consumers[name] = _consumer_record(consumers.get(name), defaults.get(name))

    candidate_raw = copy.deepcopy(current_doc)
    candidate_raw["schema"] = fleet_policy.POLICY_SCHEMA
    candidate_raw["defaults"] = defaults_tree
    candidate_raw["seats"] = mapped_seats
    candidate_raw["consumers"] = consumers
    try:
        candidate = fleet_policy.parse_document(candidate_raw)
    except FleetPolicyError as exc:
        errors.append(_error("invalid_document", detail=str(exc)))
        return candidate_raw, errors, warnings

    for violation in fleet_policy.pin_violations(current_doc, candidate):
        errors.append(_error(str(violation["reason"]), seat=violation["seat"], changed=violation.get("changed") or []))
    return candidate, errors, warnings


def _affected(current: Mapping[str, Any], proposed: Mapping[str, Any] | None) -> dict[str, list[str]]:
    if proposed is None:
        return {"consumers": [], "repositories": [], "sessions": []}
    names = sorted(
        set(current.get("consumers") or {}) | set(proposed.get("consumers") or {}) | set(fleet_model_roster.CONSUMERS)
    )
    changed = []
    for consumer in names:
        before = (current.get("consumers") or {}).get(consumer)
        after = (proposed.get("consumers") or {}).get(consumer)
        if before != after:
            changed.append(consumer)
            continue
        try:
            if (
                fleet_policy.resolve_policy(current, consumer, None)["effective"]
                != fleet_policy.resolve_policy(proposed, consumer, None)["effective"]
            ):
                changed.append(consumer)
        except FleetPolicyError:
            changed.append(consumer)
    return {"consumers": changed, "repositories": [], "sessions": []}


def _candidate_digest(candidate: Mapping[str, Any] | None, errors: list[dict[str, Any]]) -> str | None:
    if candidate is None:
        return None
    if any(error["code"] == "invalid_document" for error in errors):
        return _sha(candidate)
    try:
        return fleet_policy.document_digest(candidate)
    except FleetPolicyError:
        return _sha(candidate)


def _preview_digest(
    sources: Mapping[str, Any],
    candidate: Mapping[str, Any] | None,
    annotations: Any,
    errors: list[dict[str, Any]],
) -> str:
    material = {
        "schema": MIGRATION_SCHEMA,
        "policy_revision": sources["policy_revision"],
        "policy_digest": sources["policy_digest"],
        "roster_revision": sources["roster_revision"],
        "roster_digest": sources["roster_digest"],
        "preference_digest": sources["preference_digest"],
        "candidate_digest": _candidate_digest(candidate, errors),
        "annotations": annotations if annotations is not None else {},
    }
    return _sha(material)


def preview_migration(
    conn: sqlite3.Connection,
    *,
    expected_policy_version: int,
    expected_roster_revision: int,
    annotations: Any = None,
) -> dict[str, Any]:
    """Dry-run the migration. Writes nothing and never activates."""
    sources = _sources(conn)
    errors: list[dict[str, Any]] = []
    if type(expected_policy_version) is not int:
        raise FleetHubError("fleet policy migration field 'expected_policy_version' must be an integer")
    if type(expected_roster_revision) is not int:
        raise FleetHubError("fleet policy migration field 'expected_roster_revision' must be an integer")
    if expected_policy_version != sources["policy_revision"]:
        errors.append(
            _error(
                "policy_revision_conflict",
                expected_version=expected_policy_version,
                current_revision=sources["policy_revision"],
            )
        )
    if expected_roster_revision != sources["roster_revision"]:
        errors.append(
            _error(
                "roster_revision_conflict",
                expected_revision=expected_roster_revision,
                current_revision=sources["roster_revision"],
            )
        )
    parsed_annotations, annotation_errors = _parse_annotations(annotations)
    errors.extend(annotation_errors)
    candidate, build_errors, warnings = _build_candidate(conn, annotations=parsed_annotations)
    errors.extend(build_errors)
    activated = is_activated(conn)
    if activated:
        errors.append(_error("already_activated"))
    digest = _preview_digest(sources, candidate, annotations, errors)
    current_doc = fleet_hub_policy.current_policy(conn)["document"]
    parsed_candidate = None
    try:
        if candidate is not None and not any(item["code"] == "invalid_document" for item in errors):
            parsed_candidate = fleet_policy.parse_document(candidate)
    except FleetPolicyError:
        parsed_candidate = candidate
    return {
        "ok": not errors,
        "activated": activated,
        "schema": _schema_status(conn),
        "sources": sources,
        "candidate": parsed_candidate if parsed_candidate is not None else candidate,
        "preview_digest": digest,
        "annotations": annotations,
        "warnings": warnings,
        "errors": errors,
        "affected": _affected(current_doc, parsed_candidate if isinstance(parsed_candidate, dict) else candidate),
        "compatibility_behavior": COMPAT_AUTHORITY if activated else COMPAT_LEGACY,
    }


def activate_migration(
    conn: sqlite3.Connection,
    *,
    expected_policy_version: int,
    expected_roster_revision: int,
    preview_digest: str,
    actor: str,
    reason: str,
    annotations: Any = None,
) -> dict[str, Any]:
    """CAS-activate the previewed candidate. Commits only if this call opened the txn."""
    if type(expected_policy_version) is not int:
        raise FleetHubError("fleet policy migration field 'expected_policy_version' must be an integer")
    if type(expected_roster_revision) is not int:
        raise FleetHubError("fleet policy migration field 'expected_roster_revision' must be an integer")
    if not isinstance(preview_digest, str) or not preview_digest:
        raise FleetHubError("fleet policy migration field 'preview_digest' is required")
    opened = False
    if conn.in_transaction is False:
        conn.execute("BEGIN IMMEDIATE")
        opened = True
    try:
        if is_activated(conn):
            raise FleetHubError("already_activated: fleet policy already owns the legacy roster")
        sources = _sources(conn)
        if sources["policy_revision"] != expected_policy_version:
            raise FleetHubConflict(
                f"policy_revision_conflict: expected {expected_policy_version}, current {sources['policy_revision']}"
            )
        if sources["roster_revision"] != expected_roster_revision:
            raise FleetHubConflict(
                f"roster_revision_conflict: expected {expected_roster_revision}, current {sources['roster_revision']}"
            )
        preview = preview_migration(
            conn,
            expected_policy_version=expected_policy_version,
            expected_roster_revision=expected_roster_revision,
            annotations=annotations,
        )
        if preview["preview_digest"] != preview_digest:
            raise FleetHubConflict("preview_digest_conflict: sources changed since preview")
        if preview["errors"]:
            detail = ", ".join(error["code"] for error in preview["errors"])
            raise FleetHubError(f"fleet policy migration refused ({detail})")
        candidate = preview["candidate"]
        saved = fleet_hub_policy.save_policy(
            conn,
            candidate,
            expected_version=expected_policy_version,
            actor=actor,
            reason=reason,
        )
        ensure_schema(conn)
        now = fleet_hub._utc_now()
        conn.execute(
            f"INSERT INTO {TABLE} (singleton, activated, activated_at, activated_by, reason, preview_digest, "
            "policy_revision, roster_revision, preference_digest, candidate_digest) "
            "VALUES (1, 1, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                now,
                actor,
                reason,
                preview_digest,
                saved["revision"],
                expected_roster_revision,
                sources["preference_digest"],
                saved["digest"],
            ),
        )
        if opened:
            conn.commit()
        return {
            "ok": True,
            "activated": True,
            "schema": _schema_status(conn),
            "sources": sources,
            "revision": saved["revision"],
            "digest": saved["digest"],
            "document": saved["document"],
            "preview_digest": preview_digest,
            "compatibility_behavior": COMPAT_AUTHORITY,
            "affected": preview["affected"],
            "warnings": preview["warnings"],
            "errors": [],
        }
    except BaseException:
        if opened:
            conn.rollback()
        raise


def projected_roster_meta(conn: sqlite3.Connection) -> tuple[int, str]:
    current = fleet_hub_policy.current_policy(conn)
    return int(current["revision"]), str(current["created_at"])


def projected_authority_metadata(conn: sqlite3.Connection) -> dict[str, Any]:
    """Signed fleet-policy authority metadata for an activated projected roster."""
    current = fleet_hub_policy.current_policy(conn)
    return {
        "active": True,
        "version": int(current["revision"]),
        "digest": str(current["digest"]),
    }


def projected_seats(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Legacy roster seat rows projected from the current policy document."""
    document = fleet_hub_policy.current_policy(conn)["document"]
    rows: list[dict[str, Any]] = []
    for name in sorted(document.get("seats") or {}):
        seat = document["seats"][name]
        bindings = _as_mapping(seat.get("bindings"))
        brigade = _as_mapping(bindings.get("brigade"))
        t3_fleet = _as_mapping(bindings.get("t3_fleet"))
        brigade_binding: dict[str, Any] = {"cli": brigade.get("cli") or ""}
        launch_model = brigade.get("model")
        if isinstance(launch_model, str) and launch_model:
            brigade_binding["model"] = launch_model
        rows.append(
            {
                "seat": name,
                "enabled": bool(seat.get("enabled")),
                "provider": seat["provider"],
                "model": seat["model"],
                "reasoning": seat.get("effort") or "none",
                "limit": seat.get("concurrency"),
                "bindings": {
                    "brigade": brigade_binding,
                    "t3_fleet": {
                        "instance_id": t3_fleet.get("instance_id") or "",
                        "service_tier": t3_fleet.get("service_tier") or None,
                    },
                },
                "notes": seat.get("notes"),
            }
        )
    return rows


def projected_consumer_defaults(conn: sqlite3.Connection) -> dict[str, str | None]:
    document = fleet_hub_policy.current_policy(conn)["document"]
    consumers = document.get("consumers") or {}
    defaults: dict[str, str | None] = {}
    for name in sorted(fleet_model_roster.CONSUMERS):
        record = consumers.get(name) or {}
        patches = record.get("default_patches") if isinstance(record, Mapping) else {}
        roles = patches.get("roles") if isinstance(patches, Mapping) else {}
        value = roles.get("admission_default") if isinstance(roles, Mapping) else None
        defaults[name] = value if isinstance(value, str) and value else None
    return defaults


def projected_run_preference(conn: sqlite3.Connection) -> dict[str, Any]:
    document = fleet_hub_policy.current_policy(conn)["document"]
    roles = (document.get("defaults") or {}).get("roles") or {}
    payload: dict[str, Any] = {field: None for field in run_preference.ALLOWED_FIELDS}
    for field in run_preference.ROLE_FIELDS:
        value = roles.get(field)
        payload[field] = value if isinstance(value, str) and value else None
    return payload


def projected_models(conn: sqlite3.Connection, seats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    providers = [row for row in fleet_hub.list_model_policy(conn) if row.get("seat") is None]
    projected = [
        {
            "seat": item["seat"],
            "provider": item["provider"],
            "model": item["model"],
            "enabled": item["enabled"],
            "limit": item["limit"],
            "notes": item.get("notes"),
        }
        for item in seats
    ]
    return providers + projected
