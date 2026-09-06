"""Fleet control-plane policy contract and resolver (brigade.fleet_policy.v1)."""

from __future__ import annotations

import copy

import pytest

from brigade import fleet_policy


def _document() -> dict:
    return {
        "schema": fleet_policy.POLICY_SCHEMA,
        "defaults": {
            "roles": {"impl": "seat-alpha", "review": "seat-beta"},
            "data": {"allow_training": False},
            "execution": {"machine": "worker-linux-1", "concurrency": 2},
        },
        "machines": {
            "worker-linux-1": {
                "os": "linux",
                "capabilities": ["general"],
                "preferred_workloads": ["general"],
                "discouraged_workloads": ["gui"],
                "prohibited_workloads": ["windows-native"],
                "priority": 200,
                "concurrency": 4,
                "fallback": ["worker-linux-2"],
            },
            "worker-linux-2": {"os": "linux", "priority": 100},
        },
        "seats": {
            "seat-alpha": {
                "provider": "provider-a",
                "model": "model-a-1",
                "effort": "high",
                "eligible_machines": ["worker-linux-1"],
                "concurrency": 2,
                "timeout_seconds": 900,
                "fallback": ["seat-beta"],
                "quota_pool": "pool-a",
            },
            "seat-beta": {"provider": "provider-b", "model": "model-b-1"},
            "seat-free": {
                "provider": "provider-c",
                "model": "model-c-free",
                "training_allowed": True,
                "retention": "provider-trains-on-prompts",
            },
            "seat-off": {"provider": "provider-a", "model": "model-a-2", "enabled": False},
        },
        "consumers": {
            "consumer-one": {
                "default_patches": {"roles": {"impl": "seat-beta"}},
                "reload": "refreshable",
                "coverage": "verified",
            },
            "consumer-two": {"reload": "restart-required"},
        },
        "repositories": {
            "repo/public": {"privacy": "public", "patches": {"data": {"allow_training": True}}},
            "repo/private": {"privacy": "private", "patches": {"roles": {"impl": "seat-alpha"}}},
            "repo/inherits": {"privacy": "public", "patches": {"roles": {"impl": None}}},
        },
    }


def test_empty_document_parses_and_is_generic():
    parsed = fleet_policy.parse_document(fleet_policy.empty_document())
    assert parsed["schema"] == fleet_policy.POLICY_SCHEMA
    assert parsed["machines"] == {}
    assert parsed["seats"] == {}
    assert parsed["consumers"] == {}
    assert parsed["repositories"] == {}
    resolved = fleet_policy.resolve_policy(parsed, "consumer-one", "repo/unknown")
    assert resolved["effective"]["data"]["allow_training"] is False
    assert "unregistered-consumer" in resolved["warnings"]


def test_parse_document_fills_defaults_and_rejects_unknown_fields():
    parsed = fleet_policy.parse_document(_document())
    assert parsed["seats"]["seat-beta"]["enabled"] is True
    assert parsed["seats"]["seat-beta"]["pinned"] is False
    assert parsed["seats"]["seat-beta"]["training_allowed"] is False
    assert parsed["machines"]["worker-linux-2"]["concurrency"] == 1
    assert parsed["consumers"]["consumer-two"]["coverage"] == "unverified"
    assert parsed["repositories"]["repo/private"]["privacy"] == "private"

    broken = _document()
    broken["seats"]["seat-beta"]["speed"] = "fast"
    with pytest.raises(fleet_policy.FleetPolicyError) as excinfo:
        fleet_policy.parse_document(broken)
    assert "speed" in str(excinfo.value)


@pytest.mark.parametrize(
    "mutate, fragment",
    [
        (lambda doc: doc.update(schema="brigade.fleet_policy.v0"), "schema"),
        (lambda doc: doc["seats"]["seat-beta"].pop("model"), "model"),
        (lambda doc: doc["seats"]["seat-beta"].update(concurrency=-1), "concurrency"),
        (lambda doc: doc["defaults"].update(weather={"sunny": True}), "weather"),
        (lambda doc: doc["defaults"]["data"].update(unknown_leaf=1), "unknown_leaf"),
        (lambda doc: doc["repositories"]["repo/public"].update(privacy="sort-of-private"), "privacy"),
        (lambda doc: doc["consumers"]["consumer-one"].update(reload="maybe"), "reload"),
        (lambda doc: doc["defaults"]["roles"].update(impl={"nested": "value"}), "impl"),
    ],
)
def test_parse_document_rejects_malformed_input(mutate, fragment):
    doc = _document()
    mutate(doc)
    with pytest.raises(fleet_policy.FleetPolicyError) as excinfo:
        fleet_policy.parse_document(doc)
    assert fragment in str(excinfo.value)


def test_parse_document_rejects_oversized_documents():
    doc = _document()
    doc["seats"] = {
        f"seat-{index}": {"provider": "provider-a", "model": "model-a-1"}
        for index in range(fleet_policy.MAX_RECORDS + 1)
    }
    with pytest.raises(fleet_policy.FleetPolicyError) as excinfo:
        fleet_policy.parse_document(doc)
    assert "seats" in str(excinfo.value)


def test_document_digest_is_stable_and_order_independent():
    first = fleet_policy.parse_document(_document())
    reordered = _document()
    reordered["seats"] = dict(reversed(list(reordered["seats"].items())))
    second = fleet_policy.parse_document(reordered)
    assert fleet_policy.document_digest(first) == fleet_policy.document_digest(second)
    assert fleet_policy.document_digest(first).startswith("sha256:")

    changed = copy.deepcopy(first)
    changed["seats"]["seat-beta"]["model"] = "model-b-2"
    assert fleet_policy.document_digest(changed) != fleet_policy.document_digest(first)


def test_precedence_is_fleet_then_consumer_then_repo_then_session():
    doc = fleet_policy.parse_document(_document())

    fleet_only = fleet_policy.resolve_policy(doc, "consumer-two", None)
    assert fleet_only["effective"]["roles"]["impl"] == "seat-alpha"
    assert fleet_only["sources"]["roles.impl"]["layer"] == "fleet-defaults"

    consumer = fleet_policy.resolve_policy(doc, "consumer-one", None)
    assert consumer["effective"]["roles"]["impl"] == "seat-beta"
    assert consumer["sources"]["roles.impl"]["layer"] == "consumer:consumer-one"

    repo = fleet_policy.resolve_policy(doc, "consumer-one", "repo/private")
    assert repo["effective"]["roles"]["impl"] == "seat-alpha"
    assert repo["sources"]["roles.impl"]["layer"] == "repo:repo/private"

    session = fleet_policy.resolve_policy(
        doc,
        "consumer-one",
        "repo/private",
        overrides={"roles": {"impl": "seat-beta"}},
        override_reason="operator asked for the review seat",
    )
    assert session["effective"]["roles"]["impl"] == "seat-beta"
    assert session["sources"]["roles.impl"]["layer"] == "session"
    assert session["override_reason"] == "operator asked for the review seat"


def test_source_chain_reports_every_layer_including_inherited():
    doc = fleet_policy.parse_document(_document())
    resolved = fleet_policy.resolve_policy(doc, "consumer-one", "repo/private")
    chain = resolved["sources"]["roles.impl"]["chain"]
    assert [entry["layer"] for entry in chain] == [
        "fleet-defaults",
        "consumer:consumer-one",
        "repo:repo/private",
        "session",
    ]
    assert [entry["action"] for entry in chain] == ["set", "set", "set", "unset"]
    assert chain[0]["value"] == "seat-alpha"
    assert chain[3]["value"] is None


def test_null_repo_override_restores_inheritance():
    doc = fleet_policy.parse_document(_document())
    resolved = fleet_policy.resolve_policy(doc, "consumer-one", "repo/inherits")
    assert resolved["effective"]["roles"]["impl"] == "seat-beta"
    assert resolved["sources"]["roles.impl"]["layer"] == "consumer:consumer-one"
    repo_entry = next(
        entry for entry in resolved["sources"]["roles.impl"]["chain"] if entry["layer"] == "repo:repo/inherits"
    )
    assert repo_entry["action"] == "inherit"


def test_hard_privacy_constraint_applies_after_preference_overrides():
    doc = fleet_policy.parse_document(_document())
    resolved = fleet_policy.resolve_policy(
        doc,
        "consumer-one",
        "repo/private",
        overrides={"data": {"allow_training": True}},
        override_reason="operator override",
    )
    assert resolved["effective"]["data"]["allow_training"] is False
    assert resolved["repository_privacy"] == "private"
    assert [item["path"] for item in resolved["denied_overrides"]] == ["data.allow_training"]
    assert resolved["denied_overrides"][0]["layer"] == "session"
    assert any(rule["rule"] == "repo-privacy-training" for rule in resolved["constraints"])


def test_unknown_repository_never_allows_training_by_override():
    doc = fleet_policy.parse_document(_document())
    resolved = fleet_policy.resolve_policy(
        doc,
        "consumer-one",
        "repo/not-registered",
        overrides={"data": {"allow_training": True}},
    )
    assert resolved["repository_privacy"] == "unknown"
    assert resolved["effective"]["data"]["allow_training"] is False
    assert resolved["denied_overrides"][0]["reason"] == "unknown-repository"
    assert "unknown-repository" in resolved["warnings"]


def test_public_repository_may_enable_training():
    doc = fleet_policy.parse_document(_document())
    resolved = fleet_policy.resolve_policy(doc, "consumer-one", "repo/public")
    assert resolved["effective"]["data"]["allow_training"] is True
    assert resolved["denied_overrides"] == []


def test_resolve_rejects_unknown_override_paths():
    doc = fleet_policy.parse_document(_document())
    with pytest.raises(fleet_policy.FleetPolicyError):
        fleet_policy.resolve_policy(doc, "consumer-one", "repo/public", overrides={"weather": {"sunny": True}})


def test_admissible_seat_reports_every_refusal_reason():
    doc = fleet_policy.parse_document(_document())
    public = fleet_policy.resolve_policy(doc, "consumer-one", "repo/public")
    private = fleet_policy.resolve_policy(doc, "consumer-one", "repo/private")

    assert fleet_policy.admissible_seat(doc, "seat-alpha", private)["admissible"] is True
    assert fleet_policy.admissible_seat(doc, "seat-free", public)["admissible"] is True

    training_denied = fleet_policy.admissible_seat(doc, "seat-free", private)
    assert training_denied["admissible"] is False
    assert "training-not-permitted" in training_denied["reasons"]

    disabled = fleet_policy.admissible_seat(doc, "seat-off", public)
    assert disabled["admissible"] is False
    assert "seat-disabled" in disabled["reasons"]

    missing = fleet_policy.admissible_seat(doc, "seat-nope", public)
    assert missing["admissible"] is False
    assert "unknown-seat" in missing["reasons"]


def test_validate_inventory_separates_unavailable_from_missing():
    doc = fleet_policy.parse_document(_document())
    inventory = {
        "provider-a": {"state": "fresh", "available": ["model-a-1"], "retired": ["model-a-2"]},
        "provider-b": {"state": "unavailable", "reason": "authentication-required"},
        "provider-c": {"state": "fresh", "available": [], "blocked": ["model-c-free"]},
    }
    report = fleet_policy.validate_inventory(doc, inventory)
    states = {row["seat"]: row["state"] for row in report["seats"]}
    assert states["seat-alpha"] == "available"
    assert states["seat-off"] == "retired"
    assert states["seat-beta"] == "unavailable"
    assert states["seat-free"] == "policy-blocked"

    missing = fleet_policy.validate_inventory(
        doc, {**inventory, "provider-b": {"state": "fresh", "available": ["model-b-9"]}}
    )
    assert {row["seat"]: row["state"] for row in missing["seats"]}["seat-beta"] == "missing"


def test_validate_inventory_marks_uninventoried_providers_unavailable():
    doc = fleet_policy.parse_document(_document())
    report = fleet_policy.validate_inventory(doc, {})
    assert {row["state"] for row in report["seats"]} == {"unavailable"}
    assert all(row["reason"] == "provider-not-inventoried" for row in report["seats"])


def test_pin_violations_require_an_explicit_separate_unpin():
    current = fleet_policy.parse_document(_document())
    current["seats"]["seat-alpha"]["pinned"] = True

    changed = copy.deepcopy(current)
    changed["seats"]["seat-alpha"]["model"] = "model-a-9"
    violations = fleet_policy.pin_violations(current, changed)
    assert [item["reason"] for item in violations] == ["pinned-seat-change"]

    unpin_and_change = copy.deepcopy(current)
    unpin_and_change["seats"]["seat-alpha"]["pinned"] = False
    unpin_and_change["seats"]["seat-alpha"]["model"] = "model-a-9"
    assert [item["reason"] for item in fleet_policy.pin_violations(current, unpin_and_change)] == ["unpin-and-change"]

    unpin_only = copy.deepcopy(current)
    unpin_only["seats"]["seat-alpha"]["pinned"] = False
    assert fleet_policy.pin_violations(current, unpin_only) == []

    removed = copy.deepcopy(current)
    removed["seats"].pop("seat-alpha")
    assert [item["reason"] for item in fleet_policy.pin_violations(current, removed)] == ["pinned-seat-removed"]

    unrelated = copy.deepcopy(current)
    unrelated["seats"]["seat-beta"]["model"] = "model-b-2"
    assert fleet_policy.pin_violations(current, unrelated) == []


def test_preserve_pins_keeps_pinned_identity_on_bootstrap():
    current = fleet_policy.parse_document(_document())
    current["seats"]["seat-alpha"]["pinned"] = True

    incoming = fleet_policy.parse_document(_document())
    incoming["seats"]["seat-alpha"]["model"] = "model-a-9"
    incoming["seats"]["seat-alpha"]["effort"] = "low"
    incoming["seats"]["seat-beta"]["model"] = "model-b-2"

    merged = fleet_policy.preserve_pins(current, incoming)
    assert merged["seats"]["seat-alpha"]["model"] == "model-a-1"
    assert merged["seats"]["seat-alpha"]["effort"] == "high"
    assert merged["seats"]["seat-alpha"]["pinned"] is True
    assert merged["seats"]["seat-beta"]["model"] == "model-b-2"
    assert fleet_policy.pin_violations(current, merged) == []


def test_diff_documents_reports_added_removed_and_changed_leaves():
    current = fleet_policy.parse_document(_document())
    proposed = copy.deepcopy(current)
    proposed["seats"]["seat-beta"]["model"] = "model-b-2"
    proposed["seats"].pop("seat-off")
    proposed["seats"]["seat-new"] = fleet_policy.parse_document(
        {**fleet_policy.empty_document(), "seats": {"seat-new": {"provider": "provider-a", "model": "model-a-3"}}}
    )["seats"]["seat-new"]

    diff = fleet_policy.diff_documents(current, proposed)
    changed = {item["path"]: (item["from"], item["to"]) for item in diff["changed"]}
    assert changed["seats.seat-beta.model"] == ("model-b-1", "model-b-2")
    assert any(item["path"].startswith("seats.seat-off.") for item in diff["removed"])
    assert any(item["path"] == "seats.seat-new.model" for item in diff["added"])
    assert fleet_policy.diff_documents(current, current) == {"added": [], "removed": [], "changed": []}


def test_seat_bindings_and_machine_node_id_are_parsed_exactly():
    doc = _document()
    doc["machines"]["worker-linux-1"]["node_id"] = "11111111-1111-4111-8111-111111111111"
    doc["seats"]["seat-alpha"]["bindings"] = {
        "brigade": {"cli": "cursor-agent"},
        "t3_fleet": {"instance_id": "cursor", "service_tier": "standard"},
        "native": {"instance_id": "grok-native", "model": "model-a-1"},
    }
    parsed = fleet_policy.parse_document(doc)
    assert parsed["machines"]["worker-linux-1"]["node_id"] == "11111111-1111-4111-8111-111111111111"
    assert parsed["seats"]["seat-alpha"]["bindings"]["native"]["instance_id"] == "grok-native"
    assert parsed["seats"]["seat-beta"]["bindings"]["native"]["instance_id"] is None

    broken = _document()
    broken["seats"]["seat-alpha"]["bindings"] = {"native": {"instance_id": "x", "model": "y", "alias": "fuzzy"}}
    with pytest.raises(fleet_policy.FleetPolicyError):
        fleet_policy.parse_document(broken)


def test_matching_seats_require_an_exact_configured_binding():
    doc = _document()
    doc["seats"]["seat-alpha"]["bindings"] = {
        "native": {"instance_id": "grok-native", "model": "model-a-1"},
    }
    parsed = fleet_policy.parse_document(doc)
    assert fleet_policy.matching_seats(
        parsed, None, provider="provider-a", model="model-a-1", instance_id="grok-native"
    ) == ["seat-alpha"]
    assert (
        fleet_policy.matching_seats(parsed, None, provider="provider-a", model="model-a-1", instance_id="other-native")
        == []
    )
    unbound = fleet_policy.parse_document(_document())
    assert (
        fleet_policy.matching_seats(unbound, None, provider="provider-a", model="model-a-1", instance_id="grok-native")
        == []
    )


def test_matching_seats_brigade_run_uses_cli_and_optional_launch_model():
    doc = _document()
    doc["seats"]["seat-alpha"]["bindings"] = {
        "brigade": {"cli": "cli-alpha", "model": "provider-a/model-slash-id"},
    }
    parsed = fleet_policy.parse_document(doc)
    assert fleet_policy.matching_seats(
        parsed,
        "brigade-run",
        provider="provider-a",
        model="provider-a/model-slash-id",
        instance_id="cli-alpha",
    ) == ["seat-alpha"]
    assert (
        fleet_policy.matching_seats(
            parsed, "brigade-run", provider="provider-a", model="model-a-1", instance_id="cli-alpha"
        )
        == []
    )
    assert (
        fleet_policy.matching_seats(
            parsed,
            "brigade-run",
            provider="provider-a",
            model="provider-a/model-slash-id",
            instance_id="cli-other",
        )
        == []
    )
    canonical_only = _document()
    canonical_only["seats"]["seat-alpha"]["bindings"] = {"brigade": {"cli": "cli-alpha"}}
    parsed_canonical = fleet_policy.parse_document(canonical_only)
    assert fleet_policy.matching_seats(
        parsed_canonical, "brigade-run", provider="provider-a", model="model-a-1", instance_id="cli-alpha"
    ) == ["seat-alpha"]
    assert (
        fleet_policy.matching_seats(
            parsed_canonical,
            "brigade-run",
            provider="provider-a",
            model="model-a-1",
            instance_id="grok-native",
        )
        == []
    )


def test_pin_violations_include_model_bearing_binding_leaves():
    current = fleet_policy.parse_document(_document())
    current["seats"]["seat-alpha"]["pinned"] = True
    current["seats"]["seat-alpha"]["bindings"]["brigade"]["model"] = "provider-a/model-slash-id"
    current["seats"]["seat-alpha"]["bindings"]["native"]["model"] = "provider-a/model-slash-id"

    swapped = copy.deepcopy(current)
    swapped["seats"]["seat-alpha"]["bindings"]["brigade"]["model"] = "provider-contrib/model-contrib-train-1"
    violations = fleet_policy.pin_violations(current, swapped)
    assert [item["reason"] for item in violations] == ["pinned-seat-change"]
    assert "bindings.brigade.model" in violations[0]["changed"]

    native_swap = copy.deepcopy(current)
    native_swap["seats"]["seat-alpha"]["bindings"]["native"]["model"] = "provider-contrib/model-contrib-train-1"
    native_violations = fleet_policy.pin_violations(current, native_swap)
    assert [item["reason"] for item in native_violations] == ["pinned-seat-change"]
    assert "bindings.native.model" in native_violations[0]["changed"]

    instance_move = copy.deepcopy(current)
    instance_move["seats"]["seat-alpha"]["bindings"]["brigade"]["cli"] = "cli-moved"
    instance_move["seats"]["seat-alpha"]["bindings"]["native"]["instance_id"] = "native-moved"
    instance_move["seats"]["seat-alpha"]["bindings"]["t3_fleet"]["instance_id"] = "t3-moved"
    assert fleet_policy.pin_violations(current, instance_move) == []


def test_inventory_collectors_parse_exact_tuple_and_default_empty():
    parsed = fleet_policy.parse_document(fleet_policy.empty_document())
    assert parsed["routing"]["inventory_collectors"] == {}

    doc = _document()
    doc["routing"] = {
        "enabled": True,
        "inventory_collectors": {
            "agy-google": {
                "node_id": "11111111-1111-4111-8111-111111111111",
                "source": "cli:agy",
                "provider": "google",
                "harness": "agy",
                "account_id": "acct-generic",
            }
        },
    }
    parsed = fleet_policy.parse_document(doc)
    collector = parsed["routing"]["inventory_collectors"]["agy-google"]
    assert collector == {
        "node_id": "11111111-1111-4111-8111-111111111111",
        "source": "cli:agy",
        "provider": "google",
        "harness": "agy",
        "account_id": "acct-generic",
    }


def test_inventory_collectors_reject_unknown_fields_and_unknown_sources():
    doc = _document()
    doc["routing"] = {
        "inventory_collectors": {
            "agy-google": {
                "node_id": "11111111-1111-4111-8111-111111111111",
                "source": "cli:agy",
                "provider": "google",
                "harness": "agy",
                "account_id": "acct-generic",
                "favorite": True,
            }
        }
    }
    with pytest.raises(fleet_policy.FleetPolicyError, match="favorite"):
        fleet_policy.parse_document(doc)

    doc["routing"]["inventory_collectors"]["agy-google"] = {
        "node_id": "11111111-1111-4111-8111-111111111111",
        "source": "cli:secret-probe",
        "provider": "google",
        "harness": "agy",
        "account_id": "acct-generic",
    }
    with pytest.raises(fleet_policy.FleetPolicyError, match="source"):
        fleet_policy.parse_document(doc)


def test_inventory_collector_bindings_omit_ambiguous_providers():
    document = {
        "routing": {
            "inventory_collectors": {
                "one": {
                    "node_id": "n1",
                    "source": "cli:agy",
                    "provider": "google",
                    "harness": "agy",
                    "account_id": "acct-a",
                },
                "two": {
                    "node_id": "n2",
                    "source": "cli:agy",
                    "provider": "google",
                    "harness": "agy",
                    "account_id": "acct-b",
                },
                "three": {
                    "node_id": "n1",
                    "source": "cli:grok",
                    "provider": "xai",
                    "harness": "grok",
                    "account_id": "acct-x",
                },
            }
        }
    }
    bindings = fleet_policy.inventory_collector_bindings(document)
    assert "google" not in bindings
    assert bindings["xai"] == {"harness": "grok", "account_id": "acct-x"}
