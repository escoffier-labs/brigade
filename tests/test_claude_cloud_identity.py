"""Offline regressions for explicitly supplied subscription cloud identities."""

from dataclasses import FrozenInstanceError

import pytest

from brigade.claude_cloud_identity import normalize_claude_cloud_identity


@pytest.mark.parametrize("session_id", ["session_fixture_A-1", "cse_fixture_B-2"])
def test_bare_id_and_url_normalize_to_one_immutable_identity(session_id):
    identity = normalize_claude_cloud_identity(session_id)
    assert identity.session_id == session_id
    assert identity.url == f"https://claude.ai/code/{session_id}"
    assert identity == normalize_claude_cloud_identity(identity.url)
    assert identity == normalize_claude_cloud_identity(f"claude.ai/code/{session_id}")
    assert identity == normalize_claude_cloud_identity(identity.url + "?from=cli&m=0#fixture")
    assert identity == normalize_claude_cloud_identity(identity.url + "#fixture?from=cli")
    with pytest.raises(FrozenInstanceError):
        identity.session_id = "cse_changed_fixture"


@pytest.mark.parametrize("prefix", ["session_", "cse_"])
@pytest.mark.parametrize("suffix", ["A", "a" * 128, "_-0aZ"])
def test_accepts_validator_suffix_boundaries(prefix, suffix):
    session_id = prefix + suffix
    assert normalize_claude_cloud_identity(session_id).session_id == session_id
    assert normalize_claude_cloud_identity(f"https://claude.ai/code/{session_id}").session_id == session_id


@pytest.mark.parametrize(
    "value",
    [
        "",
        "session_",
        "cse_",
        "session_fixture.id",
        "session_fixture/id",
        "session_fixture%2Fid",
        "Session_fixture",
        "sess_fixture",
        "ccpool_fixture",
        "session_" + "a" * 129,
        "cse_" + "a" * 129,
        "session_fixture?from=cli",
        "session_fixture#fragment",
        "http://claude.ai/code/cse_fixture",
        "ftp://claude.ai/code/cse_fixture",
        "//claude.ai/code/cse_fixture",
        "https://claude.ai.example/code/cse_fixture",
        "https://example.invalid/code/cse_fixture",
        "https://claude.ai./code/cse_fixture",
        "https://CLAUDE.AI/code/cse_fixture",
        "https://claude.ai:443/code/cse_fixture",
        "https://claude.ai:/code/cse_fixture",
        # Synthetic URL userinfo, not contact addresses or live credentials.
        "https://fixture@claude.ai/code/cse_fixture",  # content-guard: allow email
        "https://fixture:password@claude.ai/code/cse_fixture",  # content-guard: allow email
        "https://claude.ai@evil.invalid/code/cse_fixture",  # content-guard: allow example-email-reserved
        "https://claude.ai\\@evil.invalid/code/cse_fixture",
        "https://[claude.ai/code/cse_fixture",
        "https://claude.ai",
        "https://claude.ai/code/",
        "https://claude.ai/code/cse_",
        "https://claude.ai/code/cse_fixture/extra",
        "https://claude.ai/code/cse_fixture/",
        "https://claude.ai//code/cse_fixture",
        "https://claude.ai/code/../code/cse_fixture",
        "https://claude.ai/code/%63se_fixture",
        "https://claude.ai/code/cse_fixture%2Fextra",
        "https://claude.ai/%63ode/cse_fixture",
        "https://claude.ai/code/cse_fixture;extra",
        "https://claude.ai/code/cse_fixture\\extra",
        "https://claude.ai/code/cse_" + "a" * 129,
        "https://claude.ai/code/cse_fixture?tracking=" + "x" * 4096,
    ],
)
def test_rejects_unsafe_or_malformed_identity_without_echoing_input(value):
    with pytest.raises(ValueError) as error:
        normalize_claude_cloud_identity(value)
    assert str(error.value) == "Invalid Claude cloud session identity"
    assert error.value.__cause__ is None


@pytest.mark.parametrize("character", [" ", "\t", "\n", "\r", "\0", "\x1f", "\x7f", "\x85", "\xa0", "\u200b", "\u202e"])
@pytest.mark.parametrize(
    "template",
    [
        "{}cse_fixture",
        "cse_fixture{}",
        "https://claude{}.ai/code/cse_fixture",
        "https://claude.ai/code/cse_fixture?tracking={}",
        "https://claude.ai/code/cse_fixture#{}",
    ],
)
def test_rejects_whitespace_and_controls_before_url_parser_can_strip_them(character, template):
    with pytest.raises(ValueError, match="^Invalid Claude cloud session identity$"):
        normalize_claude_cloud_identity(template.format(character))


@pytest.mark.parametrize("value", [None, 42, b"cse_fixture", ["cse_fixture"], {"id": "cse_fixture"}])
def test_rejects_non_string_inputs_with_safe_error(value):
    with pytest.raises(ValueError, match="^Invalid Claude cloud session identity$"):
        normalize_claude_cloud_identity(value)
