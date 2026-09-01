# Synthetic rejection fixtures retain private paths, emails, and a dummy JWT.
# content-guard: allow home-path file
# content-guard: allow email file
# content-guard: allow jwt-token file
from __future__ import annotations

import pytest
from brigade import worklore_validate as v


def test_create_payload_rejects_unknown_and_overlong_fields() -> None:
    parsed = v.parse_create({"title": "Rotate NAS restic password", "kind": "fleet"})
    assert parsed["title"] == "Rotate NAS restic password"
    assert parsed["kind"] == "fleet"
    assert parsed["status"] == "captured"
    assert parsed["burn_rank"] == 1000
    for raw, code in (
        ({"title": "x", "kind": "fleet", "extra": True}, "unknown-field"),
        ({"title": "", "kind": "fleet"}, "field-bound"),
        ({"title": "x" * 241, "kind": "fleet"}, "field-bound"),
        ({"title": "ok", "kind": "not-a-kind"}, "field-bound"),
        ({"title": "ok\x00", "kind": "fleet"}, "field-bound"),
    ):
        with pytest.raises(v.WorkloreValidationError) as excinfo:
            v.parse_create(raw)
        assert excinfo.value.code == code


def test_ready_requires_acceptance() -> None:
    item = {"acceptance": ["restic snapshots --latest 1 succeeds"]}
    v.assert_ready(item)
    item["acceptance"] = []
    with pytest.raises(v.WorkloreValidationError) as excinfo:
        v.assert_ready(item)
    assert excinfo.value.code == "acceptance-required"


def test_burn_eligibility_has_no_second_home_in_the_validator() -> None:
    """The queue rules live only in the store, so a duplicate helper cannot drift from them."""
    assert not hasattr(v, "burn_eligible")


def test_timestamps_are_validated_so_malformed_values_cannot_hide_work() -> None:
    parsed = v.parse_create(
        {
            "title": "Rotate NAS restic password",
            "kind": "fleet",
            "review_after": "2026-09-01T00:00:00Z",
            "spend_by": "2026-09-30T00:00:00+00:00",
        }
    )
    assert parsed["review_after"] == "2026-09-01T00:00:00Z"
    assert parsed["spend_by"] == "2026-09-30T00:00:00+00:00"
    assert v.parse_create({"title": "x", "kind": "fleet", "review_after": ""})["review_after"] is None
    assert v.parse_create({"title": "x", "kind": "fleet", "spend_by": None})["spend_by"] is None
    for field in ("review_after", "spend_by"):
        for bad in ("tomorrow", "2026-13-45", "2026-09-01T00:00:00Z\n", 17):
            with pytest.raises(v.WorkloreValidationError) as excinfo:
                v.parse_create({"title": "x", "kind": "fleet", field: bad})
            assert excinfo.value.code == "field-bound"


def test_as_datetime_normalizes_zulu_and_naive_input_to_utc() -> None:
    assert v.as_datetime("2026-09-01T00:00:00Z") == v.as_datetime("2026-09-01T00:00:00+00:00")
    assert v.as_datetime("2026-09-01T00:00:00") == v.as_datetime("2026-09-01T00:00:00+00:00")


def test_lifecycle_edges_are_closed() -> None:
    assert v.can_transition("captured", "ready") is True
    assert v.can_transition("ready", "claimed") is True
    assert v.can_transition("ready", "running") is False
    assert v.can_transition("completed", "archived") is True
    assert v.can_transition("archived", "ready") is False


# --- canonical private-data primitives (Daybreak finding 3) ------------------


@pytest.mark.parametrize(
    "value",
    [
        "/home/operator/notes",
        "/home",
        "see /Users/operator/notes",
        r"C:\Users\operator\notes",
        "C:/Users/operator/notes",
        "file:///home/operator/notes",
        'path="/root/.ssh/id_ed25519"',
        "%USERPROFILE%\\AppData",
    ],
)
def test_private_paths_are_detected(value: str) -> None:
    assert v.contains_private_path(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "escoffier-labs/homelab#4",
        "escoffier-labs/home#4",
        "https://github.com/users/octocat",
        "Homeroom rotation",
        "rootcause analysis",
        "brigade/rooted#12",
    ],
)
def test_ordinary_identities_are_not_private_paths(value: str) -> None:
    assert v.contains_private_path(value) is False


@pytest.mark.parametrize(
    "value",
    [
        "ghp_" + "a" * 36,
        "github_pat_" + "b" * 30,
        "xoxb-1234567890-abcdef",
        "sk-" + "c" * 32,
        "AKIAIOSFODNN7EXAMPLE",
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghij",
        "password=" + "s" * 20,
        "https://operator:" + "hunter2" + "@example.invalid/x",
    ],
)
def test_credential_shapes_are_detected(value: str) -> None:
    assert v.contains_credential(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "fix the token handling",
        "rotate the api key next quarter",
        "secret santa planning",
        "https://github.com/escoffier-labs/brigade/issues/1",
    ],
)
def test_ordinary_prose_is_not_a_credential(value: str) -> None:
    assert v.contains_credential(value) is False


def test_safe_text_separates_field_bound_from_private_data() -> None:
    assert v.safe_text("plain title", "title", max_len=20) == "plain title"
    for value, code in (
        (42, "field-bound"),
        ("bell \x07", "field-bound"),
        ("x" * 61, "field-bound"),
        ("/home/operator", "private-data"),
        ("ghp_" + "a" * 36, "private-data"),
    ):
        with pytest.raises(v.WorkloreValidationError) as excinfo:
            v.safe_text(value, "title", max_len=60)
        assert excinfo.value.code == code, value


def test_safe_https_url_rejects_userinfo_and_malformed_urls() -> None:
    assert v.safe_https_url("https://github.com/a/b/issues/1") == "https://github.com/a/b/issues/1"
    assert v.safe_https_url("https://example.invalid:443/path") == "https://example.invalid:443/path"
    assert v.safe_https_url("https://[2001:db8::1]:8443/path") == "https://[2001:db8::1]:8443/path"
    assert v.safe_https_url(None) is None
    for value, code in (
        ("http://github.com/a", "field-bound"),
        ("https:///a", "field-bound"),
        ("https://git hub.com/a", "field-bound"),
        ("ftp://github.com/a", "field-bound"),
        ("https://operator:token@github.com/a", "private-data"),
        ("https://example.invalid:abc/path", "field-bound"),
        ("https://2001:db8::1/path", "field-bound"),
        ("https://example.invalid:/path", "field-bound"),
        ("https://example.invalid:65536/path", "field-bound"),
        ("https://example.invalid/redirect?to=/home/operator/notes", "private-data"),
    ):
        with pytest.raises(v.WorkloreValidationError) as excinfo:
            v.safe_https_url(value)
        assert excinfo.value.code == code, value
