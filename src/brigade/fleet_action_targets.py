"""Short-lived, secret-free action catalog, owned by an enrolled broker."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from typing import Any

from . import fleet_action_auth as auth
from .fleet_action_auth import ActionError, canonical, opaque

TTL = 300
REQUIRED_FIELDS = {"target_id", "family", "label", "subjects", "models", "capability", "binding_digest"}
FIELDS = REQUIRED_FIELDS | {
    "principal",
    "host",
    "repository",
    "repo_identity",
    "source_revision",
    "agent_id",
    "session_key",
}


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS action_targets (target_id TEXT PRIMARY KEY, node_id TEXT NOT NULL, "
        "document TEXT NOT NULL, expires_at REAL NOT NULL)"
    )

    conn.execute(
        "CREATE TABLE IF NOT EXISTS action_target_identities (target_id TEXT PRIMARY KEY, node_id TEXT NOT NULL, "
        "identity TEXT NOT NULL)"
    )


def native_identity(target: dict[str, Any]) -> dict[str, str]:
    family = target.get("family")
    fields: tuple[str, ...]
    if family == "t3":
        fields = ("family", "host", "repository", "repo_identity")
    elif family == "openclaw":
        agent_id = target.get("agent_id")
        if not isinstance(agent_id, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", agent_id) is None:
            raise ActionError("invalid-openclaw-agent", 400)
        fields = ("family", "agent_id")
        if "session_key" in target:
            session_key = target["session_key"]
            if (
                not isinstance(session_key, str)
                or len(session_key) > 128
                or re.fullmatch(
                    r"agent:" + re.escape(agent_id) + r":clawchat:[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}",
                    session_key,
                )
                is None
            ):
                raise ActionError("invalid-openclaw-session-key", 400)
            fields += ("session_key",)
    elif family in {"grokbot", "dot"}:
        fields = ("family", "principal")
    else:
        raise ActionError("invalid-family", 400)
    if any(not isinstance(target.get(key), str) or not target[key] for key in fields):
        raise ActionError("native-destination-required", 400)
    return {key: target[key] for key in fields}


def _models(conn: sqlite3.Connection, target: dict[str, Any]) -> list[dict[str, str]]:
    if target["family"] != "t3":
        return []
    # Project the same authority as `fleet models list`, without admission side effects.
    from . import fleet_hub_model_roster, fleet_hub_policy, fleet_model_roster, fleet_policy
    from . import fleet_policy_migration

    try:
        roster = fleet_hub_model_roster.project_roster(conn)
        document = None
        resolution = None
        if fleet_policy_migration.is_activated(conn):
            document = fleet_hub_policy.current_policy(conn)["document"]
            resolution = fleet_policy.resolve_policy(
                document, consumer="t3-fleet", repo_identity=target.get("repo_identity")
            )
        requested = {model["id"] for model in target["models"]}
        result = []
        for seat in roster["seats"]:
            name = seat["seat"]
            if name not in requested or seat["enabled"] is not True:
                continue
            groups = fleet_model_roster.consumer_launch_groups(roster, "t3-fleet", name)
            binding = fleet_model_roster.adapter_plan_binding(
                "t3-fleet", groups if groups is not None else seat["bindings"], canonical_model=seat["model"]
            )
            if binding is None or not seat.get("reasoning"):
                continue
            if any(
                fleet_model_roster.retired_reason(seat["provider"], identity, roster["retired_models"])
                for identity in fleet_model_roster.binding_launch_models(seat, launch_groups=groups)
            ):
                continue
            if document is not None and resolution is not None:
                if not fleet_policy.admissible_seat(document, name, resolution)["admissible"]:
                    continue
            result.append({"id": name, "label": seat["model"]})
        return result
    except (ValueError, RuntimeError, KeyError):
        return []


def sync(conn: sqlite3.Connection, node_id: str, records: Any) -> None:
    if (
        not auth.active_node(conn, node_id)
        or conn.execute(
            "SELECT 1 FROM action_broker_grants WHERE node_id=? AND revoked_at IS NULL", (node_id,)
        ).fetchone()
        is None
    ):
        raise ActionError("broker-scope-refused")
    if not isinstance(records, list) or len(records) > 100:
        raise ActionError("invalid-targets", 400)
    clean = []
    for raw in records:
        if not isinstance(raw, dict) or set(raw) - FIELDS or not REQUIRED_FIELDS <= set(raw):
            raise ActionError("invalid-target", 400)
        item = dict(raw)
        opaque(item["target_id"])
        if item["family"] not in {"t3", "openclaw", "grokbot", "dot"}:
            raise ActionError("invalid-family", 400)
        if item["family"] == "t3":
            for key in ("host", "repository"):
                opaque(item.get(key))
            if (
                not isinstance(item.get("source_revision"), str)
                or re.fullmatch(r"[0-9a-f]{40}", item["source_revision"]) is None
            ):
                raise ActionError("full-source-revision-required", 400)
            from .fleet_session_presence import validate_repo_identity

            if validate_repo_identity(item.get("repo_identity", "")) is None:
                raise ActionError("invalid-repository", 400)
        native_identity(item)
        if item["family"] == "openclaw":
            opaque(item["agent_id"])
        if item["capability"] != ("inbox" if item["family"] in {"grokbot", "dot"} else "execution"):
            raise ActionError("invalid-capability", 400)
        if (
            not isinstance(item["label"], str)
            or not 1 <= len(item["label"]) <= 128
            or any(ord(c) < 32 for c in item["label"])
        ):
            raise ActionError("invalid-label", 400)
        try:
            item["label"].encode("utf-8")
        except UnicodeEncodeError:
            raise ActionError("invalid-label", 400) from None
        if not isinstance(item["binding_digest"], str) or re.fullmatch("[0-9a-f]{64}", item["binding_digest"]) is None:
            raise ActionError("invalid-binding", 400)
        if not isinstance(item["subjects"], list) or not 1 <= len(item["subjects"]) <= 100:
            raise ActionError("invalid-subjects", 400)
        item["subjects"] = [auth.selector(s) for s in item["subjects"]]
        grant = conn.execute(
            "SELECT targets, subjects FROM action_broker_grants WHERE node_id=? AND revoked_at IS NULL", (node_id,)
        ).fetchone()
        if not (
            grant
            and auth.active_node(conn, node_id)
            and item["target_id"] in json.loads(grant[0])
            and all(any(auth.selector_covers(g, s) for g in json.loads(grant[1])) for s in item["subjects"])
        ):
            raise ActionError("broker-scope-refused")
        if not isinstance(item["models"], list) or len(item["models"]) > 100:
            raise ActionError("invalid-models", 400)
        for model in item["models"]:
            if not isinstance(model, dict) or set(model) != {"id", "label"}:
                raise ActionError("invalid-model", 400)
            opaque(model["id"])
            if not isinstance(model["label"], str) or not 1 <= len(model["label"]) <= 128:
                raise ActionError("invalid-model", 400)
            try:
                model["label"].encode("utf-8")
            except UnicodeEncodeError:
                raise ActionError("invalid-model", 400) from None
        if item["family"] != "t3" and item["models"]:
            raise ActionError("model-override-unsupported", 400)
        if item["capability"] == "inbox":
            from .fleet_hub_messages import _active_principal

            if not _active_principal(conn, opaque(item.get("principal"))):
                raise ActionError("destination-inactive")
        clean.append(item)
    if len({t["target_id"] for t in clean}) != len(clean):
        raise ActionError("duplicate-target", 400)
    conn.execute("BEGIN IMMEDIATE")
    try:
        for item in clean:
            identity = canonical(native_identity(item))
            permanent = conn.execute(
                "SELECT node_id, identity FROM action_target_identities WHERE target_id=?", (item["target_id"],)
            ).fetchone()
            if permanent is not None and permanent != (node_id, identity):
                raise ActionError("target-identity-conflict", 409)
            conn.execute(
                "INSERT OR IGNORE INTO action_target_identities VALUES (?, ?, ?)",
                (item["target_id"], node_id, identity),
            )
        conn.execute("DELETE FROM action_targets WHERE node_id=?", (node_id,))
        for item in clean:
            conn.execute(
                "INSERT INTO action_targets VALUES (?, ?, ?, ?)",
                (item["target_id"], node_id, canonical(item), time.time() + TTL),
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def available(conn: sqlite3.Connection, *, source: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    result = []
    for node_id, document in conn.execute(
        "SELECT node_id, document FROM action_targets WHERE expires_at>? ORDER BY target_id", (time.time(),)
    ):
        item = json.loads(document)
        grant = conn.execute(
            "SELECT targets, subjects FROM action_broker_grants WHERE node_id=? AND revoked_at IS NULL", (node_id,)
        ).fetchone()
        if not grant or not auth.active_node(conn, node_id) or item["target_id"] not in json.loads(grant[0]):
            continue
        if source is not None:
            if not any(auth.selector_matches(conn, s, source) for s in item["subjects"]):
                continue
            if not auth.broker_allowed(conn, node_id, item["target_id"], source):
                continue
            if item["family"] == "t3":
                from .fleet_action_sources import resolve

                if (
                    source["kind"] != "run"
                    or resolve(conn, source)["evidence"].get("repo_identity") != item["repo_identity"]
                ):
                    continue
        if item["capability"] == "inbox":
            from .fleet_hub_messages import _active_principal

            if not _active_principal(conn, item["principal"]):
                continue
        item["models"] = _models(conn, item)
        if item["family"] == "t3" and not item["models"]:
            continue
        result.append({**item, "node_id": node_id})
    return result


def selected(conn: sqlite3.Connection, target_id: str, source: dict[str, Any], model_id: str) -> dict[str, Any]:
    target = next((t for t in available(conn, source=source) if t["target_id"] == target_id), None)
    if target is None or (
        model_id not in {m["id"] for m in target["models"]} if target["family"] == "t3" else bool(model_id)
    ):
        raise ActionError("target-unavailable", 409)
    return target


def linked_subjects(conn: sqlite3.Connection, source_refs: list[str]) -> frozenset[str]:
    """Match actual alert sources against selectors, including newly observed runs."""
    from .fleet_action_sources import resolve

    result = set()
    for ref in source_refs:
        if not ref:
            continue
        try:
            source = resolve(conn, json.loads(ref))["source"]
            if available(conn, source=source):
                result.add(canonical(source))
        except (ActionError, ValueError):
            continue
    return frozenset(result)
