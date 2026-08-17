"""Tests for brigade.provenance envelope builder, validator, and legacy synthesis."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brigade import provenance

FIXTURES = Path(__file__).resolve().parents[1] / "src" / "brigade" / "fixtures"
GOLDEN_PATH = FIXTURES / "provenance-envelope.v1.golden.json"
POLICY_PATH = FIXTURES / "trust-policy.v1.json"
LEGACY_LABELS = ("unknown", "untrusted", "reviewed", "verified", "quarantined")


def _golden_cases():
    return json.loads(GOLDEN_PATH.read_text())["cases"]


def _case(name):
    return next(c for c in _golden_cases() if c["name"] == name)


def _set(env, path, value):
    cur = env
    for key in path[:-1]:
        cur = cur[key]
    cur[path[-1]] = value


def _item_kwargs():
    env = _case("item_unicode_trailing_newline")["envelope"]
    text = _case("item_unicode_trailing_newline")["text"]
    return dict(
        source_system=env["source"]["system"],
        source_kind=env["source"]["kind"],
        source_producer=env["source"]["producer"],
        origin=env["origin"],
        repository_id=env["repository"]["id"],
        repository_revision=env["repository"]["revision"],
        session_id=env["session"]["id"],
        session_harness=env["session"]["harness"],
        collection_id=env["collection_id"],
        item_id=env["item_id"],
        locator_kind=env["locator"]["kind"],
        locator_value=env["locator"]["value"],
        attribution=env["attribution"],
        modality=env["modality"],
        trust_label=env["trust"]["label"],
        trust_assigned_by=env["trust"]["assigned_by"],
        trust_assigned_at=env["trust"]["assigned_at"],
        injection_status=env["trust"]["injection"]["status"],
        injection_count=env["trust"]["injection"]["count"],
        injection_rules=list(env["trust"]["injection"]["rules"]),
        text=text,
        raw_bytes=text.encode("utf-8"),
        content_scope=env["hashes"]["content_scope"],
        captured_at=env["captured_at"],
        ingested_at=env["ingested_at"],
    )


def _valid_envelope():
    return provenance.build_envelope(**_item_kwargs())


def test_sha256_bytes_and_content_sha256_match_golden():
    text = _case("item_unicode_trailing_newline")["text"]
    digest = _case("item_unicode_trailing_newline")["envelope"]["hashes"]["content"]
    assert provenance.content_sha256(text) == digest
    assert provenance.sha256_bytes(text.encode("utf-8")) == digest
    assert len(digest) == 64
    assert digest == digest.lower()


def test_build_envelope_matches_golden_item_case():
    assert _valid_envelope() == _case("item_unicode_trailing_newline")["envelope"]


def test_message_scope_distinct_bytes_from_item():
    item = _case("item_unicode_trailing_newline")["envelope"]["hashes"]["content"]
    msg = _case("message_distinct_bytes")["envelope"]["hashes"]["content"]
    assert provenance.content_sha256("worker-result: done") == msg
    assert item != msg


def test_build_envelope_raw_none_when_no_raw_bytes():
    kwargs = _item_kwargs()
    kwargs["raw_bytes"] = None
    env = provenance.build_envelope(**kwargs)
    assert env["hashes"]["raw"] is None


def test_validate_accepts_golden_envelopes():
    for case in _golden_cases():
        assert provenance.validate_envelope(case["envelope"]) == []


def test_trust_policy_fixture_shape():
    policy = json.loads(POLICY_PATH.read_text())
    assert policy["schema"] == "brigade.trust-policy.v1"
    assert policy["schema_version"] == 1
    assert set(policy["entitlements"]) == set(LEGACY_LABELS)
    assert policy["untrusted_caps"] == {"max_items": 2, "max_fraction": 0.5}


@pytest.mark.parametrize(
    ("name", "path", "bad"),
    [
        ("origin underscore", ("origin",), "operator_input"),
        ("origin wrong case", ("origin",), "AGENT-SESSION"),
        ("modality underscore", ("modality",), "model_generated"),
        ("attribution bogus", ("attribution",), "guess"),
        ("trust label trusted", ("trust", "label"), "trusted"),
        ("injection status ok", ("trust", "injection", "status"), "ok"),
        ("content scope v2", ("hashes", "content_scope"), "item.text.utf8.v2"),
        ("locator kind absolute", ("locator", "kind"), "absolute"),
    ],
)
def test_validate_rejects_invalid_closed_set_values(name, path, bad):
    env = _valid_envelope()
    _set(env, path, bad)
    errors = provenance.validate_envelope(env)
    assert errors, f"expected error for {name}"
    assert any("closed" in e or "enum" in e or path[-1] in e for e in errors)


@pytest.mark.parametrize(
    ("name", "mutate"),
    [
        ("uppercase", lambda e: _set(e, ("hashes", "content"), e["hashes"]["content"].upper())),
        ("too short", lambda e: _set(e, ("hashes", "content"), "abc")),
        ("non hex", lambda e: _set(e, ("hashes", "content"), "z" * 64)),
        ("sha256 prefix", lambda e: _set(e, ("hashes", "content"), "sha256:" + "a" * 64)),
        ("empty", lambda e: _set(e, ("hashes", "content"), "")),
        ("raw uppercase", lambda e: _set(e, ("hashes", "raw"), e["hashes"]["raw"].upper())),
    ],
)
def test_validate_rejects_bad_digest_forms(name, mutate):
    env = _valid_envelope()
    mutate(env)
    errors = provenance.validate_envelope(env)
    assert errors, f"expected error for {name}"
    assert any("digest" in e or "hash" in e for e in errors)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("posix absolute", "/etc/passwd"),
        ("home absolute", "/home/user/secret"),
        ("windows drive", "C:\\Users\\foo"),
        ("windows backslash UNC", "\\\\host\\share\\file"),
        ("file URI", "file:///home/user/secret"),
        ("parent traversal", "../secret"),
    ],
)
def test_validate_rejects_unsafe_absolute_locators(name, value):
    env = _valid_envelope()
    _set(env, ("locator", "value"), value)
    errors = provenance.validate_envelope(env)
    assert errors, f"expected error for {name}"
    assert any("absolute" in e or "locator" in e for e in errors)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("repository", "id"), "/private/repo"),
        (("repository", "id"), " ../private/repo"),
        (("repository", "id"), "C:\\private\\repo"),
        (("repository", "id"), " C:\\private\\repo"),
        (("session", "id"), "\\\\server\\share"),
        (("session", "harness"), "file:///private/harness"),
        (("session", "harness"), " file:///private/harness"),
        (("collection_id",), "collection/../private"),
        (("item_id",), "item\\private"),
        (("repository", "id"), "https://example.test/repository"),
        (("session", "id"), "https://example.test/session"),
        (("session", "harness"), "https://example.test/harness"),
        (("collection_id",), "https://example.test/collection"),
        (("item_id",), "https://example.test/item"),
    ],
)
def test_validate_rejects_path_looking_identity_labels(path, value):
    env = _valid_envelope()
    _set(env, path, value)
    errors = provenance.validate_envelope(env)
    assert errors
    assert any("identity" in error or ".id" in error or ".harness" in error for error in errors)


def test_validate_accepts_ordinary_identity_labels():
    env = _valid_envelope()
    env["repository"]["id"] = "owner/repo"
    env["session"] = {"id": "session-1", "harness": "codex"}
    env["collection_id"] = "collection:alpha"
    env["item_id"] = "item-42"
    assert provenance.validate_envelope(env) == []
    assert provenance.is_safe_identity_label("owner/repo")
    assert provenance.is_safe_identity_label("collection:alpha")


@pytest.mark.parametrize(
    "revision",
    [
        " abc123 ",
        "/private/revision",
        "C:\\private\\revision",
        "\\\\server\\share\\revision",
        "file:///private/revision",
        "https://example.test/revision",
        "release/../revision",
        "é" * 129,
    ],
)
def test_validate_rejects_unsafe_repository_revisions(revision):
    env = _valid_envelope()
    env["repository"]["revision"] = revision

    errors = provenance.validate_envelope(env)

    assert errors
    assert "repository.revision must be a safe revision label" in errors


@pytest.mark.parametrize(
    "revision", ["urn:brigade:release", "mailto:ops@example.test", "ssh:repo", "https://example.test/repo"]
)
def test_validate_rejects_all_uri_scheme_repository_revisions(revision):
    env = _valid_envelope()
    env["repository"]["revision"] = revision

    assert "repository.revision must be a safe revision label" in provenance.validate_envelope(env)


@pytest.mark.parametrize(
    ("name", "inbound_adapter", "label", "proof_assigned_by", "proof_label", "want_valid"),
    [
        ("inbound reviewed no proof", True, "reviewed", None, None, False),
        ("inbound reviewed proof match", True, "reviewed", "verifier:demo", "reviewed", True),
        ("inbound reviewed assigned_by mismatch", True, "reviewed", "verifier:other", "reviewed", False),
        ("inbound reviewed label mismatch", True, "reviewed", "verifier:demo", "verified", False),
        ("inbound verified proof match", True, "verified", "verifier:demo", "verified", True),
        ("inbound untrusted no proof", True, "untrusted", None, None, True),
        ("not inbound reviewed no proof", False, "reviewed", None, None, True),
    ],
)
def test_validate_authority_for_inbound_adapter(
    name, inbound_adapter, label, proof_assigned_by, proof_label, want_valid
):
    env = _valid_envelope()
    _set(env, ("trust", "label"), label)
    _set(env, ("trust", "assigned_by"), "verifier:demo")
    proof = None
    if proof_assigned_by is not None:
        proof = {"assigned_by": proof_assigned_by, "label": proof_label}
    errors = provenance.validate_envelope(env, inbound_adapter=inbound_adapter, authority_proof=proof)
    assert (not errors) == want_valid, f"{name}: errors={errors}"


def test_verify_content_digest_detects_forged_hash():
    env = _valid_envelope()
    text = _case("item_unicode_trailing_newline")["text"]
    assert provenance.verify_content_digest(text, env) is True
    env["hashes"]["content"] = "a" * 64
    assert provenance.verify_content_digest(text, env) is False
    env["hashes"]["content"] = None
    assert provenance.verify_content_digest(text, env) is True


def test_apply_bundle_integrity_is_metadata_only_on_mismatch():
    env = _valid_envelope()
    env["hashes"]["content"] = "a" * 64
    bundle = {
        "results": [
            {
                "id": "item-1",
                "snippet": "unsafe leaked snippet",
                "text": "tampered body",
                "provenance": env,
                "artifacts": [{"id": "art-1", "text": "artifact body"}],
            }
        ]
    }

    out = provenance.apply_bundle_integrity(bundle)

    item = out["results"][0]
    assert item["integrity_mismatch"] is True
    assert item["snippet"] == ""
    assert "text" not in item
    assert "text" not in item["artifacts"][0]
    assert out["integrity_omitted"] == 1


def test_synthesize_legacy_provenance():
    env, display = provenance.synthesize_legacy_provenance()
    assert display == provenance.LEGACY_DISPLAY == "UNKNOWN PROVENANCE - legacy item"
    assert env["schema"] == "brigade.provenance-envelope.v1"
    assert env["schema_version"] == 1
    assert env["origin"] == "unknown"
    assert env["modality"] == "unknown"
    assert env["attribution"] == "inferred"
    assert env["trust"]["label"] == "unknown"
    assert env["hashes"]["content"] is None
    assert provenance.validate_envelope(env) == []


def test_validate_rejects_envelope_above_4096_compact_bytes():
    env = _valid_envelope()
    _set(env, ("item_id",), "x" * 5000)
    errors = provenance.validate_envelope(env)
    assert errors
    assert any("size" in e or "4096" in e for e in errors)


def test_validate_accepts_envelope_under_4096_compact_bytes():
    env = _valid_envelope()
    compact = len(json.dumps(env, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    assert compact < 4096
    assert provenance.validate_envelope(env) == []
