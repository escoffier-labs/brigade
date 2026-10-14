from __future__ import annotations

import json
import sys

import pytest

from brigade import cli, fleet_master_roster


def _document() -> dict[str, object]:
    return {
        "schema": fleet_master_roster.SCHEMA,
        "revision": 7,
        "seats": [
            {
                "name": "implementer",
                "cli": "codex",
                "model": "gpt-5.6",
                "purpose": "Implement and review repository changes.",
                "caveats": ["Use the repository's focused verification gate."],
                "fallbacks": ["reviewer"],
                "enabled": True,
            },
            {
                "name": "reviewer",
                "cli": "claude",
                "model": "opus",
                "purpose": "Review changes independently.",
                "caveats": [],
                "fallbacks": [],
                "enabled": True,
            },
        ],
    }


def test_parse_master_roster_returns_canonical_digest_and_copy() -> None:
    document = _document()

    parsed = fleet_master_roster.parse(document)

    assert parsed["revision"] == 7
    assert parsed["seats"][0]["fallbacks"] == ["reviewer"]
    assert parsed["sha256"].startswith("sha256:")
    assert "sha256" not in document


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("api_token", "placeholder-token"),
        ("env", {"OPENAI_API_KEY": "placeholder-secret"}),
        ("home", "~/.config/credential"),
        ("command", "codex --api-key=placeholder"),
    ],
)
def test_parse_rejects_forbidden_seat_material(key: str, value: object) -> None:
    document = _document()
    document["seats"][0][key] = value  # type: ignore[index]

    with pytest.raises(fleet_master_roster.MasterRosterError):
        fleet_master_roster.parse(document)


@pytest.mark.parametrize(
    "text",
    [
        "Use token sk-ant-abcdefghijklmnopqrstuvwxyz0123456789 for auth.",
        "Authorization: Basic dXNlcjpwYXNz",
        "Use Slack token " + "xoxb-" + "A" * 20,
        "Use Slack app token " + "xapp-1-" + "A" * 24,
        "Use Hugging Face token " + "hf_" + "A" * 24,
        "Use AWS temporary access key " + "ASIA" + "A" * 16,
        "Authorization: Bearer " + "A" * 32,
        "Use Stripe key " + "sk_live_" + "A" * 24,
        "Use Stripe test key " + "sk_test_" + "A" * 24,
        "Use Stripe restricted key " + "rk_live_" + "A" * 24,
        "Use Stripe restricted test key " + "rk_test_" + "A" * 24,
        "Use GitLab token " + "glpat-" + "A" * 20,
        "OPENAI_API_KEY=placeholder-secret",
        "CI=production",
        "X=private-value",
        "_X=private-value",
        "__COMPAT_LAYER=RunAsInvoker",
        "Load config from C:\\Users\\operator\\.config\\secret.toml",
        r"Read $env:USERPROFILE\Vendor\Product",
        r"Read $env:APPDATA\Vendor\Product",
        r"Read $PWD/foo",
        r"Read $XDG_CONFIG_HOME/tool",
        r"Read ${XDG_CONFIG_HOME}",
        "Use %APPDATA%",
        "Use %PATH%",
        "Use %HOME%",
        "Read ${HOME:-fallback}",
        "Use ${XDG_CONFIG_HOME:+configured}",
        "Password is provided as hunter2",
        "Password provided as hunter2",
        "API key configured with hunter2",
        "The API key for this seat is shortkey",
        "The API key for this seat = shortkey",
        "The API key for implementer is shortkey",
        "The API key for the implementer is shortkey",
        "The API key assigned to this seat is shortkey",
        "The API key used by this seat is shortkey",
        "The API key used by implementer is shortkey",
        "API key value is shortkey",
        "Password value = hunter2",
        "Password: stored as hunter2",
        "Password is generated as hunter2",
        "Password stored with hunter2",
        "API key generated with shortkey",
        "The directory is build",
        "Path for this seat is foo/bar",
        "File: LICENSE",
        "Folder set to artifacts",
        "Use C:Users\\alice",
        "Read D:projects\\acme",
        "Read /etc/credentials.",
        "Write ./private/output.json.",
        "Read ../private/credentials.toml.",
        "Write private/output.json.",
        "Filename: config.json",
        "The filename is config.json",
        "File name: config.json",
        "The file name is config.json",
        "File name: foo/bar",
        r"Write private\output.json.",
        "Read .ssh/config.",
        "Read .env.",
        "Load credentials.json.",
        "Inspect id_rsa.",
        "Read private/credentials.",
        "Read secrets/token.",
        "Use ~/.config/provider/credentials.json",
        "See (~/.ssh/config).",
        "Use ~alice/work/repo",
        r"Read ~bob\work\repo",
        "Use auth profile production-admin.",
        "Use the auth profile production-admin.",
        "Activate the auth profile production-admin.",
        'Use auth profile "production-admin".',
        "Use auth profile 'production-admin'.",
        "Use auth profile `production-admin`.",
        "Use auth profile [production-admin].",
        "Auth profile (production-admin).",
        "Authentication profile equals production-admin.",
        "Authentication profile was production-admin.",
        "Authentication profile will be production-admin.",
        "Authentication profile has been production-admin.",
        "Authentication profile set to production-admin.",
        "Authentication profile configured as production-admin.",
        "password is required.hunter2",
        "token: optional.token",
        "Use auth profiles: production-admin.",
        "Authentication profile provided as production-admin",
        "Authentication profile is provided as production-admin",
        "Connect through auth profiles (production-admin).",
        "Connect using auth profile production-admin.",
        "Connect using production-admin auth profile.",
        "Authentication profile named production-admin.",
        "Authentication profile called production-admin.",
        "Call /Applications/Agent/Binary.",
        "Invoke /Volumes/Data/Build.",
        "Run foo/main.c",
        "Open your tools/build script.",
        "Auth profile is production-admin.",
        "Use auth profile: production-admin",
        "Use auth-profile production-admin",
        "Use auth_profile production-admin",
        "Use production-admin auth profile.",
        "Use `production-admin` auth profile.",
        "Use the production-admin auth profile.",
        "Authentication profile for this seat is production-admin",
        "The auth profile assigned to this seat is production-admin",
        "Auth profile value: production-admin",
        "password: actual-secret",
        "password equals hunter2",
        "API key equals shortkey",
        "password is actual-secret",
        "Password was hunter2",
        "Password will be hunter2",
        "Password has been hunter2",
        "API key is abcdefghijklmnop",
        "password is p@ssw0rd!",
        "API key is abcd.efgh.ijkl.mnop",
        "password is hunter2!",
        "token is abcdefg.",
        'password is "abcdefghijklmnop"',
        'token is "hunter2!"',
        "password is “abcdefghijklmnop”",
        "token is ‘hunter2!’",
        "password is hunter2",
        'passphrase is "correct horse battery staple"',
        'password is "hunter 2"',
        'password is "abc123"',
        "password is [hunter 2]",
        "password is required-secret",
        "Password is configured as hunter2",
        "Password is provided with hunter2",
        "Password provided with hunter2",
        "Password is configured with hunter2",
        "Password is configured to hunter2",
        "token: optional-token",
        "password is x",
        "Password set to hunter2",
        "Set password to hunter2",
        "Set the password to hunter2",
        "Set a password to hunter2",
        "Set an API key to shortkey",
        "Set your API key to shortkey",
        "Set API key to shortkey",
        "Use API key shortkey",
        "Use the password hunter2",
        "Password is set to hunter2",
        "Password was set to hunter2",
        "Token is set to abcdefgh",
        "token is xy",
        "password is !",
        "token: hunter2",
        "Credential: hunter2",
        "Credentials are hunter2",
        "API keys are shortkey",
        "Tokens are abcdefgh",
        "Passwords are hunter2",
        "Passwords were hunter2",
        "Credentials were hunter2",
        "API keys have been shortkey",
        "Passphrases are correct-horse",
        "passwords: hunter2",
        "Authentication profiles are production-admin",
        "Auth profiles are production-admin",
        "Authentication profiles were production-admin",
        "Auth profiles have been production-admin",
        "API key has the value shortkey",
        "Password has a value of hunter2",
        "Authentication profile has the value production-admin",
        "Environment variable SERVICE_MODE has the value production",
        "Environment variable SERVICE_MODE has a value of production",
        "Environment variable SERVICE_MODE currently is production",
        "Environment variable SERVICE_MODE now equals production",
        "auth token: hunter2",
        "token is abcdefgh",
        "api key is shortkey",
        "See https://example.com/callback?token=hunter2",
        "See https://example.com/callback?%74oken=hunter2",
        "See https://example.com/config?env=SERVICE_MODE%3Dproduction",
        "See https://example.com/config?SERVICE_MODE=production",
        "See https://example.com/config#SERVICE_MODE=production",
        "See https://example.com/SERVICE_MODE%3Dproduction",
        "See https://example.com/#/callback?path=/srv/private",
        "Call /api?path=%252Fetc%252Fpasswd",
        "See https://example.com/callback?api%5Fkey=hunter2",
        "See https://example.com/password%3Dhunter2",
        "See https://example.com/password%253Dhunter2",
        "See https://example.com/SERVICE_MODE%253Dproduction",
        "password is [hunter2]",
        'The "password" is hunter2',
        "The `password` is hunter2",
        "token is (abcdefg)",
        "Configure a password as hunter2",
        "Store the password as hunter2",
        "Password value: hunter2",
        "The API key for this seat: shortkey",
        "Provide an API key as shortkey",
        "npm_" + "A" * 36,
        "token is abcdefghijklmnop",
        "auth token is abcdefghijklmnop",
        "export service_mode=production",
        "Set environment variable service_mode=production",
        "Set environment variable SERVICE_MODE to production",
        "env service_mode=production",
        "Set env service_mode=production",
        "Environment variable service_mode=production",
        "Environment variable SERVICE_MODE is production",
        "The environment variable named SERVICE_MODE is production",
        'Environment variable "SERVICE_MODE" is production',
        "Environment variable `SERVICE_MODE` is production",
        "The environment variable SERVICE_MODE value is production",
        "Environment variable SERVICE_MODE set to production",
        "Environment variable SERVICE_MODE configured as production",
        "Environment variable SERVICE_MODE configured to production",
        "Environment variable SERVICE_MODE configured with production",
        "Configure environment variable SERVICE_MODE as production",
        "Use the environment variable SERVICE_MODE as production",
        "Environment variable SERVICE_MODE was production",
        "Environment variable SERVICE_MODE will be production",
        "Environment variable SERVICE_MODE has been production",
        "Environment variable SERVICE_MODE equals production",
        "Environment variable SERVICE_MODE value: production",
        "Environment variable SERVICE_MODE value = production",
        "Environment variable SERVICE_MODE: production",
        "Use environment variable service_mode=production",
        "Home directory: ~",
        "The path is /",
        "The path is /秘密",
        "The path is .",
        "The path is ..",
        "Start at /",
        "Operate from ~",
        "Move to ..",
        "Move to foo/bar",
        "Copy foo/bar",
        "Work from foo/bar",
        "Run from /",
        "Execute from ~",
        "Open ..",
        "Navigate to ..",
        "Navigate to foo/bar",
        "Navigate to foo/bar target",
        "Navigate into foo/bar artifacts",
        "Move to foo/bar target",
        "Navigate into foo/bar data",
        "Read foo/bar content",
        "Save foo/bar output",
        "Change directory to /",
        "cd /",
        "cd ~",
        "cd ..",
        "cd foo/bar",
        "Work from /",
    ],
)
def test_parse_rejects_secret_or_home_path_text(text: str) -> None:
    document = _document()
    document["seats"][0]["caveats"] = [text]  # type: ignore[index]

    with pytest.raises(fleet_master_roster.MasterRosterError):
        fleet_master_roster.parse(document)


@pytest.mark.parametrize("field", ["purpose", "caveats"])
@pytest.mark.parametrize(
    "text",
    [
        "Auth profile for implementer is production-admin",
        "Authentication profile for the implementer is production-admin",
        "Auth profile for reviewer: production-admin",
        "Auth profile used by this seat is production-admin",
        "Auth profile used by implementer: production-admin",
        "This seat uses auth profile production-admin",
        "Reviewer uses auth profile production-admin",
        "Reviewer had auth profile production-admin",
        "Reviewer is using auth profile production-admin",
        "Reviewer used auth profile production-admin",
        "Reviewer was using the authentication profile production-admin",
        "Reviewer will be using auth profile production-admin",
        "Reviewers are using auth profile production-admin",
        "This seat is using the authentication profile production-admin",
        "Reviewer has auth profile production-admin",
        "This seat has an auth profile production-admin",
        "Auth profile had been production-admin",
        "Auth profile will have been production-admin",
        "Auth profile is 生产环境",
        "Use auth profile 生产环境",
        "Reviewer uses auth profile 生产环境",
        "Auth profile of this seat is production-admin",
        "Auth profile of implementer is production-admin",
        'Auth profile is "production admin"',
        "Auth profile is “production admin”",
        "Auth profile is ‘production admin’",
        'Use auth profile "production admin"',
        "Use auth profile “production admin”",
        "Use auth profile ‘production admin’",
        'Authentication profile named "production admin"',
        "Authentication profile named “production admin”",
        "Authentication profile named ‘production admin’",
    ],
)
def test_parse_rejects_named_seat_auth_profile_qualifiers(field: str, text: str) -> None:
    document = _document()
    document["seats"][0][field] = [text] if field == "caveats" else text  # type: ignore[index]

    with pytest.raises(fleet_master_roster.MasterRosterError):
        fleet_master_roster.parse(document)


@pytest.mark.parametrize("field", ["purpose", "caveats"])
@pytest.mark.parametrize("text", ["Auth profile prod-admin.", "Authentication profile production-admin."])
def test_parse_rejects_bare_auth_profile_labels(field: str, text: str) -> None:
    document = _document()
    document["seats"][0][field] = [text] if field == "caveats" else text  # type: ignore[index]

    with pytest.raises(fleet_master_roster.MasterRosterError):
        fleet_master_roster.parse(document)


@pytest.mark.parametrize("field", ["purpose", "caveats"])
@pytest.mark.parametrize(
    "text",
    [
        "Authentication profile production-admin is active.",
        "Environment variable SERVICE_MODE has value production.",
        "API key has value shortkey",
        "Password has value hunter2",
        "Path has value foo/bar",
    ],
)
def test_parse_rejects_subject_profile_and_has_value_disclosures(field: str, text: str) -> None:
    document = _document()
    document["seats"][0][field] = [text] if field == "caveats" else text  # type: ignore[index]

    with pytest.raises(fleet_master_roster.MasterRosterError):
        fleet_master_roster.parse(document)


@pytest.mark.parametrize("field", ["purpose", "caveats"])
@pytest.mark.parametrize(
    "text",
    [
        "Password value=hunter2",
        "The API key for this seat=shortkey",
        "The API key used by implementer=shortkey",
        "The API key of this seat is shortkey",
        "The API key of implementer is shortkey",
        "The API key for seat implementer is shortkey",
        "The API key for the reviewer seat is shortkey",
        "The API key used on this seat is shortkey",
        "The API key used for this seat is shortkey",
        "The API key used for implementer is shortkey",
        "The API key for 审查员 is shortkey",
        "Password assigned to 审查员 is hunter2",
        "Auth profile on 审查员 is production-admin",
        "The API key for primary reviewer is shortkey",
        "The API key for 审查 团队 is shortkey",
    ],
)
def test_parse_rejects_qualified_compact_credential_assignments(field: str, text: str) -> None:
    document = _document()
    document["seats"][0][field] = [text] if field == "caveats" else text  # type: ignore[index]

    with pytest.raises(fleet_master_roster.MasterRosterError):
        fleet_master_roster.parse(document)


@pytest.mark.parametrize("field", ["purpose", "caveats"])
@pytest.mark.parametrize(
    "text",
    [
        "The API key on this seat is shortkey",
        "Password on this service is hunter2",
        "The auth profile on reviewer is production-admin",
    ],
)
def test_parse_rejects_on_qualified_protected_assignments(field: str, text: str) -> None:
    document = _document()
    document["seats"][0][field] = [text] if field == "caveats" else text  # type: ignore[index]

    with pytest.raises(fleet_master_roster.MasterRosterError):
        fleet_master_roster.parse(document)


@pytest.mark.parametrize("field", ["purpose", "caveats"])
@pytest.mark.parametrize(
    "text",
    [
        "Reviewer should be using API key shortkey",
        "Reviewer must be using password hunter2",
    ],
)
def test_parse_rejects_modal_progressive_credential_disclosures(field: str, text: str) -> None:
    document = _document()
    document["seats"][0][field] = [text] if field == "caveats" else text  # type: ignore[index]

    with pytest.raises(fleet_master_roster.MasterRosterError):
        fleet_master_roster.parse(document)


@pytest.mark.parametrize("field", ["purpose", "caveats"])
@pytest.mark.parametrize(
    "text",
    [
        "Path on this seat is foo/bar",
        "Repository on this service should be private/repo",
    ],
)
def test_parse_rejects_on_qualified_paths(field: str, text: str) -> None:
    document = _document()
    document["seats"][0][field] = [text] if field == "caveats" else text  # type: ignore[index]

    with pytest.raises(fleet_master_roster.MasterRosterError):
        fleet_master_roster.parse(document)


@pytest.mark.parametrize("field", ["purpose", "caveats"])
@pytest.mark.parametrize(
    "text",
    [
        "This seat uses API key shortkey",
        "Reviewer uses the password hunter2",
        "Reviewer is using API key shortkey",
        "This seat is using the password hunter2",
        "Reviewer used API key shortkey",
        "Reviewer was using the password hunter2",
        "Reviewer will be using API key shortkey",
        "Reviewer has API key shortkey",
        "Reviewer has the password hunter2",
        "Reviewer had API key shortkey",
        "This seat had the password hunter2",
        "Password had been hunter2",
        "Password will have been hunter2",
        "API key will have been shortkey",
        "API key had been shortkey",
        "Reviewers have API key shortkey",
        "Agents have the password hunter2",
        "审查员 uses API key shortkey",
    ],
)
def test_parse_rejects_subject_first_credential_disclosures(field: str, text: str) -> None:
    document = _document()
    document["seats"][0][field] = [text] if field == "caveats" else text  # type: ignore[index]

    with pytest.raises(fleet_master_roster.MasterRosterError):
        fleet_master_roster.parse(document)


@pytest.mark.parametrize("field", ["purpose", "caveats"])
@pytest.mark.parametrize("text", ["password%3Dhunter2", "API%20key%20is%20shortkey"])
def test_parse_rejects_percent_encoded_credential_disclosures(field: str, text: str) -> None:
    document = _document()
    document["seats"][0][field] = [text] if field == "caveats" else text  # type: ignore[index]

    with pytest.raises(fleet_master_roster.MasterRosterError):
        fleet_master_roster.parse(document)


@pytest.mark.parametrize("field", ["purpose", "caveats"])
@pytest.mark.parametrize(
    "text",
    [
        "API key would be shortkey",
        "API key must be shortkey",
        "Password could be hunter2",
    ],
)
def test_parse_rejects_modal_credential_assignments(field: str, text: str) -> None:
    document = _document()
    document["seats"][0][field] = [text] if field == "caveats" else text  # type: ignore[index]

    with pytest.raises(fleet_master_roster.MasterRosterError):
        fleet_master_roster.parse(document)


@pytest.mark.parametrize("field", ["purpose", "caveats"])
@pytest.mark.parametrize(
    "text",
    [
        "Environment variable SERVICE_MODE should be production",
        "Environment variable SERVICE_MODE must be production",
        "Path should be foo/bar",
        "Path must be foo/bar",
        "Call%20/api%3Fpath%3D%2Fetc%2Fpasswd",
        "Call%20/api%3FSERVICE_MODE%3Dproduction",
    ],
)
def test_parse_rejects_modal_declarations_and_encoded_api_routes(field: str, text: str) -> None:
    document = _document()
    document["seats"][0][field] = [text] if field == "caveats" else text  # type: ignore[index]

    with pytest.raises(fleet_master_roster.MasterRosterError):
        fleet_master_roster.parse(document)


@pytest.mark.parametrize("field", ["purpose", "caveats"])
@pytest.mark.parametrize(
    "text",
    [
        "API+key%3Dshortkey",
        "Password+is+hunter2",
        "Auth+profile%3Dproduction-admin",
        "See https://example.com/foo;path=/etc/passwd",
        "See https://example.com/foo;%2e%2e/etc/passwd",
    ],
)
def test_parse_rejects_form_encoded_secrets_and_embedded_url_paths(field: str, text: str) -> None:
    document = _document()
    document["seats"][0][field] = [text] if field == "caveats" else text  # type: ignore[index]

    with pytest.raises(fleet_master_roster.MasterRosterError):
        fleet_master_roster.parse(document)


@pytest.mark.parametrize("field", ["purpose", "caveats"])
@pytest.mark.parametrize(
    "text",
    [
        "See https:///tmp/example/file",
        "See http:///etc/hosts",
        "The SERVICE_MODE environment variable is production",
        "SERVICE_MODE environment variable is production",
    ],
)
def test_parse_rejects_authorityless_url_paths_and_variable_first_assignments(field: str, text: str) -> None:
    document = _document()
    document["seats"][0][field] = [text] if field == "caveats" else text  # type: ignore[index]

    with pytest.raises(fleet_master_roster.MasterRosterError):
        fleet_master_roster.parse(document)


@pytest.mark.parametrize("field", ["purpose", "caveats"])
@pytest.mark.parametrize(
    "text",
    [
        "See https%3A%2F%2Fexample.com%2F%3Fpath%3D%2Fetc%2Fhosts",
        "See https%3A%2F%2Fexample.com%2F%3FSERVICE_MODE%3Dproduction",
    ],
)
def test_parse_rejects_encoded_url_schemes_with_sensitive_values(field: str, text: str) -> None:
    document = _document()
    document["seats"][0][field] = [text] if field == "caveats" else text  # type: ignore[index]

    with pytest.raises(fleet_master_roster.MasterRosterError):
        fleet_master_roster.parse(document)


@pytest.mark.parametrize("field", ["purpose", "caveats"])
@pytest.mark.parametrize(
    "text",
    [
        'The "auth profile" is production-admin',
        '"Environment variable" SERVICE_MODE is production',
        'The "path" is foo/bar',
    ],
)
def test_parse_rejects_quoted_protected_labels(field: str, text: str) -> None:
    document = _document()
    document["seats"][0][field] = [text] if field == "caveats" else text  # type: ignore[index]

    with pytest.raises(fleet_master_roster.MasterRosterError):
        fleet_master_roster.parse(document)


@pytest.mark.parametrize("field", ["purpose", "caveats"])
@pytest.mark.parametrize(
    "text",
    [
        "%22API%20key%22%3Dshortkey",
        "%22auth%20profile%22%3Dproduction-admin",
        "%E2%80%9Cpassword%E2%80%9D%3Dhunter2",
    ],
)
def test_parse_rejects_encoded_quoted_protected_labels(field: str, text: str) -> None:
    document = _document()
    document["seats"][0][field] = [text] if field == "caveats" else text  # type: ignore[index]

    with pytest.raises(fleet_master_roster.MasterRosterError):
        fleet_master_roster.parse(document)


@pytest.mark.parametrize(
    "text",
    [
        "mask_live_data",
        "task-abcdefghijkl",
        "Run CI/CD workflows",
        "Read input/output formats",
        "Read e.g., the guide",
        "See https://example.com/docs?lang=en",
        "Support Bearer authentication for API clients",
        "Read Node.js documentation",
        "Open VS.Code to review",
        "Basic code review",
        "Use basic mode",
        "Plan A: review changes",
        "Use option B: fast mode",
        "Token: optional.",
        "Authentication token: managed by Vault.",
        "Password: required.",
        "API key: configured.",
        "Use API key authentication for request signing.",
        "This seat uses API key authentication for request signing.",
        "Client secret: stored securely.",
        "Password is required.",
        "Auth profiles are required.",
        "Password is confidential.",
        "Password is managed by Vault.",
        "Auth profile is required.",
        "Environment variable SERVICE_MODE is required.",
    ],
)
def test_parse_accepts_text_containing_non_token_secret_prefixes(text: str) -> None:
    document = _document()
    document["seats"][0]["caveats"] = [text]  # type: ignore[index]

    assert fleet_master_roster.parse(document)["seats"][0]["caveats"] == [text]


@pytest.mark.parametrize(
    "text",
    [
        "No auth profile is required.",
        "Authentication profiles require rotation.",
        "Auth profile: required.",
        "Authentication profile: configured.",
        "Support auth profile selection.",
        "Support auth profiles as needed.",
        "Configure auth profile selection.",
        "Rotate auth profiles regularly.",
        "Support auth profile rotation.",
        "Configure authentication profile management.",
        "Use the auth profile for this service.",
        "Authentication profile names must remain local.",
        "Authentication profile policies require rotation.",
        "Authentication profile values are prohibited.",
    ],
)
@pytest.mark.parametrize("field", ["purpose", "caveats"])
def test_parse_accepts_prose_that_mentions_auth_profiles(field: str, text: str) -> None:
    document = _document()
    document["seats"][0][field] = [text] if field == "caveats" else text  # type: ignore[index]

    parsed = fleet_master_roster.parse(document)["seats"][0]
    assert parsed[field] == ([text] if field == "caveats" else text)


@pytest.mark.parametrize("field", ["purpose", "caveats"])
@pytest.mark.parametrize(
    "text",
    [
        "Serve the /models endpoint.",
        "Call /preference for routing.",
        "Invoke /health for readiness.",
        "Call /v1/chat/completions endpoint.",
        "Call /search?lang=en endpoint.",
        "Support HTTP/2.0 requests.",
        "Target Python/3.12 runtimes.",
        "Handle TLS/1.3 connections.",
        "GET /health endpoint",
        "GET /.well-known/openid-configuration",
        "Call /api/.well-known/info",
        "POST /v1/jobs",
        "Call /api/v1/%6aobs",
        "GET /users",
        "POST /applications",
        "DELETE /config/settings.json",
        "Call /api/run",
        "Call /api/cache",
        "Call /api/project",
        "Call /api/v1/docs",
        "GET /api/v1/logs",
        "Invoke /api/v1/tests",
        "Call /applications/123",
        "Invoke /applications/123",
        "Request /applications/123",
        "Call /docs",
        "GET /logs",
        "Invoke /tests",
    ],
)
def test_parse_accepts_known_api_routes_in_descriptive_text(field: str, text: str) -> None:
    document = _document()
    if field == "purpose":
        document["seats"][0][field] = text  # type: ignore[index]
    else:
        document["seats"][0][field] = [text]  # type: ignore[index]

    parsed = fleet_master_roster.parse(document)

    assert parsed["seats"][0][field] == (text if field == "purpose" else [text])


@pytest.mark.parametrize("field", ["purpose", "caveats"])
@pytest.mark.parametrize(
    "text",
    ["Treat mode == fast as enabled.", "Use x==y predicates.", "Compare version >= 2.0 before launch."],
)
def test_parse_accepts_comparison_operators_in_descriptive_text(field: str, text: str) -> None:
    document = _document()
    if field == "purpose":
        document["seats"][0][field] = text  # type: ignore[index]
    else:
        document["seats"][0][field] = [text]  # type: ignore[index]

    parsed = fleet_master_roster.parse(document)

    assert parsed["seats"][0][field] == (text if field == "purpose" else [text])


@pytest.mark.parametrize("text", [".env", "credentials.json", "id_rsa"])
def test_parse_rejects_standalone_sensitive_file_names(text: str) -> None:
    document = _document()
    document["seats"][0]["caveats"] = [text]  # type: ignore[index]

    with pytest.raises(fleet_master_roster.MasterRosterError):
        fleet_master_roster.parse(document)


@pytest.mark.parametrize("field", ["model", "purpose", "caveats"])
@pytest.mark.parametrize("filename", ["video.mp4", "archive.7z", "font.woff2"])
def test_parse_rejects_digit_bearing_filename_extensions(field: str, filename: str) -> None:
    document = _document()
    text = f"Load {filename}"
    document["seats"][0][field] = [text] if field == "caveats" else text  # type: ignore[index]

    with pytest.raises(fleet_master_roster.MasterRosterError):
        fleet_master_roster.parse(document)


@pytest.mark.parametrize("field", ["purpose", "caveats"])
@pytest.mark.parametrize("text", ["Filename: 秘密.txt", "Open 秘密.txt"])
def test_parse_rejects_unicode_filenames_in_descriptive_text(field: str, text: str) -> None:
    document = _document()
    document["seats"][0][field] = [text] if field == "caveats" else text  # type: ignore[index]

    with pytest.raises(fleet_master_roster.MasterRosterError):
        fleet_master_roster.parse(document)


@pytest.mark.parametrize("field", ["purpose", "caveats"])
@pytest.mark.parametrize("text", ["Open README.md for instructions", "Load video.mp4 before launch"])
def test_parse_rejects_filenames_followed_by_prose(field: str, text: str) -> None:
    document = _document()
    document["seats"][0][field] = [text] if field == "caveats" else text  # type: ignore[index]

    with pytest.raises(fleet_master_roster.MasterRosterError):
        fleet_master_roster.parse(document)


def test_parse_accepts_dotted_seat_names_and_fallbacks() -> None:
    document = _document()
    seats = document["seats"]
    assert isinstance(seats, list)
    seats[0]["name"] = "builder.prod"
    seats[0]["fallbacks"] = ["reviewer.prod"]
    seats[1]["name"] = "reviewer.prod"

    parsed = fleet_master_roster.parse(document)

    assert parsed["seats"][0]["name"] == "builder.prod"
    assert parsed["seats"][0]["fallbacks"] == ["reviewer.prod"]


@pytest.mark.parametrize("cli_name", ["agent.exe", "aider.chat"])
def test_parse_accepts_dotted_cli_executable_names(cli_name: str) -> None:
    document = _document()
    document["seats"][0]["cli"] = cli_name  # type: ignore[index]

    assert fleet_master_roster.parse(document)["seats"][0]["cli"] == cli_name


def test_parse_rejects_fine_grained_github_pat() -> None:
    document = _document()
    document["seats"][0]["caveats"] = ["placeholder=" + "github_pat_" + "A" * 24]  # type: ignore[index]

    with pytest.raises(fleet_master_roster.MasterRosterError):
        fleet_master_roster.parse(document)


@pytest.mark.parametrize(
    "model",
    [
        "openai/gpt-5.6",
        "opencode-go/glm-5.2",
        "openrouter/x-ai/grok",
        "openrouter/x-ai/grok.preview",
        "anthropic/claude",
        "openrouter/model:free",
        "openrouter/model.preview",
    ],
)
def test_parse_accepts_provider_qualified_model_identifiers(model: str) -> None:
    document = _document()
    document["seats"][0]["model"] = model  # type: ignore[index]

    assert fleet_master_roster.parse(document)["seats"][0]["model"] == model


@pytest.mark.parametrize("model", ["model.2024", "release.10", "model.preview"])
def test_parse_accepts_unqualified_inventory_model_identifiers(model: str) -> None:
    document = _document()
    document["seats"][0]["model"] = model  # type: ignore[index]

    assert fleet_master_roster.parse(document)["seats"][0]["model"] == model


@pytest.mark.parametrize("model", ["gpt-4 fast", "gpt --fast", "model🚀", "https://example.com/model"])
def test_parse_rejects_invalid_model_identifiers(model: str) -> None:
    document = _document()
    document["seats"][0]["model"] = model  # type: ignore[index]

    with pytest.raises(fleet_master_roster.MasterRosterError):
        fleet_master_roster.parse(document)


@pytest.mark.parametrize(
    "model",
    [
        ".ssh/config",
        "private/credentials",
        "etc/passwd",
        "private/output.json",
        "usr/bin/python",
        "var/log/messages",
        "home/alice",
        "package/pyproject.toml",
        "foo/bar.py",
        "assets/logo.png",
        "package/Gemfile.lock",
        "assets/logo.webp",
        "package/foo.rb",
        "app/main.php",
        "web/client.mjs",
    ],
)
def test_parse_rejects_relative_paths_as_models(model: str) -> None:
    document = _document()
    document["seats"][0]["model"] = model  # type: ignore[index]

    with pytest.raises(fleet_master_roster.MasterRosterError):
        fleet_master_roster.parse(document)


@pytest.mark.parametrize("field", ["model", "purpose", "caveats"])
@pytest.mark.parametrize(
    "path",
    [
        "workspace/Dockerfile",
        "project/Makefile",
        "docs/README",
        "src/main",
        "scripts/verify",
        "tests/unit",
    ],
)
def test_parse_rejects_common_extensionless_paths(field: str, path: str) -> None:
    document = _document()
    if field == "caveats":
        document["seats"][0][field] = [path]  # type: ignore[index]
    else:
        document["seats"][0][field] = path  # type: ignore[index]

    with pytest.raises(fleet_master_roster.MasterRosterError):
        fleet_master_roster.parse(document)


@pytest.mark.parametrize("field", ["purpose", "caveats"])
@pytest.mark.parametrize(
    "text",
    [
        "Run automation/verify.",
        "Delete credentials.json after use.",
        "Edit config.toml before running.",
        "Remove foo/bar after cleanup.",
        "Inspect checks/unit.",
        "Run tools/build",
        'Open "tools/build" for instructions',
        'Open "My Folder/file" for instructions',
        'Open "Shared Folder/file" for instructions',
        "Run `foo/bar`",
        'The path is "foo/bar"',
        "Inspect foo/bar",
        "Open 目录/秘密",
        "Run the tools/build script",
        "Execute the foo/bar helper",
        "Read from tools/build",
        "Use the file at tools/build",
        "The path is foo/bar.",
        "Location: foo/bar",
        "The location is foo/bar",
        "File names: config.json",
        "Filenames: config.json",
        "The file names are config.json",
        "The file names are /",
        "The file names have been /",
        "Files: config.json",
        "Directories: build",
        "Paths: foo/bar",
        "Path: %2Fetc%2Fpasswd",
        "Path: %2Fhome%2Fexample",
        "Repository path: foo/bar",
        "Path configured as foo/bar",
        "The path configured to foo/bar",
        "Home directory configured as /",
        "Repository: foo/bar",
        "The repository is foo/bar",
        "Repository: build",
        "The path equals foo/bar",
        "The path was foo/bar",
        "The path will be foo/bar",
        "The path has been foo/bar",
        "Path has the value foo/bar",
        "Path has a value of foo/bar",
        "Path currently is foo/bar",
        "Path = foo/bar",
        "Path value: foo/bar",
        "The path value is foo/bar",
        "Path set to foo/bar",
        "The path is set to foo/bar",
        "The path is Shared Folder/file",
        "Open Shared Folder/file for instructions",
        "GET /etc/passwd",
        "GET /users/../etc/passwd",
        "GET /v1/../etc/passwd",
        "Call /api/../../home/example",
        "Call /api?path=/etc/passwd",
        "Call /api?SERVICE_MODE=production",
        "foo/bar",
        "tools/build",
    ],
)
def test_parse_rejects_unenumerated_paths_in_descriptive_text(field: str, text: str) -> None:
    document = _document()
    document["seats"][0][field] = [text] if field == "caveats" else text  # type: ignore[index]

    with pytest.raises(fleet_master_roster.MasterRosterError):
        fleet_master_roster.parse(document)


@pytest.mark.parametrize(
    "text",
    [
        "and/or",
        "input/output",
        "read/write",
        "input / output",
        "planner / reviewer",
        "Write in key/value format.",
        "Read in client/server mode.",
    ],
)
def test_parse_accepts_slash_separated_descriptive_prose(text: str) -> None:
    document = _document()
    document["seats"][0]["purpose"] = f"Support {text} workflows"  # type: ignore[index]
    document["seats"][0]["caveats"] = [f"Review {text} details"]  # type: ignore[index]

    assert fleet_master_roster.parse(document)["seats"][0]["purpose"] == f"Support {text} workflows"


@pytest.mark.parametrize("text", ["CI/CD", "planner/reviewer", "24/7", "yes/no", "review/approval"])
def test_parse_accepts_ordinary_slash_delimited_descriptive_prose(text: str) -> None:
    document = _document()
    document["seats"][0]["purpose"] = f"Support {text} workflows"  # type: ignore[index]

    assert fleet_master_roster.parse(document)["seats"][0]["purpose"] == f"Support {text} workflows"


@pytest.mark.parametrize("field", ["purpose", "caveats"])
@pytest.mark.parametrize(
    "text",
    [
        "24/7",
        "yes/no",
        "review/approval",
        "Work / life balance guidance.",
        "We work / collaborate asynchronously.",
        "Save time/effort on reviews",
        "Write clear/concise summaries",
        "Open source/proprietary model analysis",
        "Read XML/JSON documents.",
        "Write request/response examples.",
        "Inspect source/target mappings.",
        "Read request/response data.",
        "Inspect source/target mapping.",
        "支持 输入/输出 工作流。",
    ],
)
def test_parse_accepts_standalone_slash_prose(field: str, text: str) -> None:
    document = _document()
    document["seats"][0][field] = [text] if field == "caveats" else text  # type: ignore[index]

    parsed = fleet_master_roster.parse(document)

    assert parsed["seats"][0][field] == ([text] if field == "caveats" else text)


@pytest.mark.parametrize("field", ["purpose", "caveats"])
@pytest.mark.parametrize("url", ["https://account%3Avalue@example.com/path"])
def test_parse_rejects_url_userinfo(field: str, url: str) -> None:
    document = _document()
    text = f"See {url} for the guide."
    document["seats"][0][field] = [text] if field == "caveats" else text  # type: ignore[index]

    with pytest.raises(fleet_master_roster.MasterRosterError):
        fleet_master_roster.parse(document)


@pytest.mark.parametrize("field", ["purpose", "caveats"])
@pytest.mark.parametrize("url", ["https://example.com/docs?page=2", "https://example.com/download?file=report.pdf"])
def test_parse_accepts_https_urls_in_descriptive_text(field: str, url: str) -> None:
    document = _document()
    text = f"See {url} for the guide."
    if field == "purpose":
        document["seats"][0][field] = text  # type: ignore[index]
    else:
        document["seats"][0][field] = [text]  # type: ignore[index]

    parsed = fleet_master_roster.parse(document)

    assert parsed["seats"][0][field] == (text if field == "purpose" else [text])


@pytest.mark.parametrize("field", ["purpose", "caveats"])
@pytest.mark.parametrize(
    "text",
    [
        "Use query parameter page=2.",
        "Accept format=json responses.",
        "Document x=2 arithmetic.",
    ],
)
def test_parse_accepts_lowercase_key_value_prose(field: str, text: str) -> None:
    document = _document()
    document["seats"][0][field] = [text] if field == "caveats" else text  # type: ignore[index]

    assert fleet_master_roster.parse(document)["seats"][0][field] == ([text] if field == "caveats" else text)


@pytest.mark.parametrize("field", ["purpose", "caveats"])
@pytest.mark.parametrize(
    "text",
    [
        "See https://example.com/applications/123",
        "See https://example.com/home/page",
        "See https://example.com/etc/status",
    ],
)
def test_parse_accepts_url_routes_with_filesystem_named_components(field: str, text: str) -> None:
    document = _document()
    document["seats"][0][field] = [text] if field == "caveats" else text  # type: ignore[index]

    assert fleet_master_roster.parse(document)["seats"][0][field] == ([text] if field == "caveats" else text)


@pytest.mark.parametrize("field", ["purpose", "caveats"])
@pytest.mark.parametrize(
    "text",
    [
        "See https://example.com/download?path=/opt/example.",
        "See https://example.com/config?path=~.",
        "See https://example.com/config?path=/.",
        "See https://example.com/config?path=.",
        "See https://example.com/config?path=..",
        "See https://example.com/download?file=~example/.ssh/config.",
        "See https://example.com/callback#path=/home/" + "example.",
        "See https://example.com/api?output=%2Fetc%2Fpasswd",
        "See https://example.com/path%3Foutput%3D%2Fetc%2Fhosts",
        "See https://example.com/?output%3D%2Fetc%2Fhosts",
        "See https://example.com/#output%3D%2Fetc%2Fhosts",
        "https://example.com/callback?password[]=hunter2",
        "https://example.com/callback?api_key[value]=shortkey",
        "Call /api?token%5B%5D=hunter2",
        "See https://example.com/?%2Fhome%2Fexample=x",
        "See https://example.com/foo;path=foo/bar",
        "See https://example.com/foo;path=~example/work/repo",
        "See https://example.com/foo;repository=~example/work/repo",
        "See https://example.com/foo;path[]=foo/bar",
        "See https://example.com/foo;repository[]=~example/work/repo",
        "https://example.com/%2Fetc/passwd",
        "https://example.com/%7Eexample/.ssh/config",
        "Call /api?output=/etc/passwd",
        "Call /api#path%3D%2Fetc%2Fhosts",
    ],
)
def test_parse_rejects_filesystem_paths_in_url_parameters(field: str, text: str) -> None:
    document = _document()
    document["seats"][0][field] = [text] if field == "caveats" else text  # type: ignore[index]

    with pytest.raises(fleet_master_roster.MasterRosterError):
        fleet_master_roster.parse(document)


@pytest.mark.parametrize("field", ["purpose", "caveats"])
@pytest.mark.parametrize(
    "text",
    [
        "https://example.com/callback?password.value=hunter2",
        "Call /api?api_key.value=shortkey",
    ],
)
def test_parse_rejects_dotted_credential_query_keys(field: str, text: str) -> None:
    document = _document()
    document["seats"][0][field] = [text] if field == "caveats" else text  # type: ignore[index]

    with pytest.raises(fleet_master_roster.MasterRosterError):
        fleet_master_roster.parse(document)


@pytest.mark.parametrize("field", ["purpose", "caveats"])
@pytest.mark.parametrize(
    "text",
    [
        "https://example.com/login?user_password=hunter2",
        "Call /api?db_password=hunter2",
        "user_password=hunter2",
        "https://example.com/config?env[SERVICE_MODE]=production",
        "Call /api?SERVICE_MODE[]=production",
        "API key currently is shortkey",
        "API key now equals shortkey",
        "Password remains hunter2",
        "https://example.com/?userPassword=hunter2",
        "Call /api?dbPassword=hunter2",
        "openaiApiKey=shortkey",
    ],
)
def test_parse_rejects_compound_assignments_and_adverb_qualified_credentials(field: str, text: str) -> None:
    document = _document()
    document["seats"][0][field] = [text] if field == "caveats" else text  # type: ignore[index]

    with pytest.raises(fleet_master_roster.MasterRosterError):
        fleet_master_roster.parse(document)


@pytest.mark.parametrize("field", ["purpose", "caveats"])
@pytest.mark.parametrize(
    "text",
    [
        "See https://[invalid]/?path=/etc/passwd",
        "See https://[invalid]/?SERVICE_MODE=production",
    ],
)
def test_parse_rejects_sensitive_values_in_malformed_urls(field: str, text: str) -> None:
    document = _document()
    document["seats"][0][field] = [text] if field == "caveats" else text  # type: ignore[index]

    with pytest.raises(fleet_master_roster.MasterRosterError):
        fleet_master_roster.parse(document)


@pytest.mark.parametrize("field", ["purpose", "caveats"])
@pytest.mark.parametrize(
    "text",
    [
        "Call /api/password%3Dhunter2",
        "Call /api/SERVICE_MODE%3Dproduction",
        "Call /api/SERVICE_MODE=production",
        "Call /api/%3Fpath%3D%2Fetc%2Fhosts",
        "Call /api/%3Foutput%3D%2Fhome%2Fexample",
        "Call /%65tc/passwd",
        "Call /api/%2e%2e/etc/passwd",
    ],
)
def test_parse_rejects_encoded_assignments_in_api_route_paths(field: str, text: str) -> None:
    document = _document()
    document["seats"][0][field] = [text] if field == "caveats" else text  # type: ignore[index]

    with pytest.raises(fleet_master_roster.MasterRosterError):
        fleet_master_roster.parse(document)


@pytest.mark.parametrize("field", ["purpose", "caveats"])
@pytest.mark.parametrize(
    "text",
    [
        "Call /api/%2Fetc/passwd",
        "Call /api/%2Fetc/hosts",
        "Call /api/%2Fhome/operator/file",
        "Call /api/%252Fetc/hosts",
        "Call /api/%252Fhome/operator/file",
        "Call /api/%5CUsers%5Cexample%5Cfile",
        "Call /api/%7Eexample/work/repo",
        "Call /api/~example/work/repo",
        "Call /api/foo/%65tc/passwd",
        "Call /api/%7E/.ssh/id_rsa",
        "Call /api?%2Fetc%2Fhosts=x",
        "GET /api?%2Fhome%2Fexample=x",
        "Call /api/etc/passwd",
        "Call /api/etc/passwd.",
        "Call /api/foo/etc/passwd",
        "GET /users/.ssh/id_rsa",
    ],
)
def test_parse_rejects_filesystem_paths_in_api_routes(field: str, text: str) -> None:
    document = _document()
    document["seats"][0][field] = [text] if field == "caveats" else text  # type: ignore[index]

    with pytest.raises(fleet_master_roster.MasterRosterError):
        fleet_master_roster.parse(document)


@pytest.mark.parametrize("field", ["purpose", "caveats"])
@pytest.mark.parametrize(
    "text",
    [
        "Environment variable SERVICE_MODE for this seat is production",
        "Environment variable SERVICE_MODE used by this seat is production",
        "Environment variable SERVICE_MODE used on this seat is production",
        "Environment variable SERVICE_MODE used on implementer is production",
    ],
)
def test_parse_rejects_ownership_qualified_environment_assignments(field: str, text: str) -> None:
    document = _document()
    document["seats"][0][field] = [text] if field == "caveats" else text  # type: ignore[index]

    with pytest.raises(fleet_master_roster.MasterRosterError):
        fleet_master_roster.parse(document)


@pytest.mark.parametrize("field", ["purpose", "caveats"])
@pytest.mark.parametrize(
    "text",
    [
        "Environment variable SERVICE_MODE will have been production",
        "Environment variable SERVICE_MODE had been production",
        "Path had been foo/bar",
        "Path will have been foo/bar",
    ],
)
def test_parse_rejects_perfect_tense_disclosures(field: str, text: str) -> None:
    document = _document()
    document["seats"][0][field] = [text] if field == "caveats" else text  # type: ignore[index]

    with pytest.raises(fleet_master_roster.MasterRosterError):
        fleet_master_roster.parse(document)


@pytest.mark.parametrize("field", ["purpose", "caveats"])
@pytest.mark.parametrize(
    "text",
    [
        "Navigate to foo/bar directories",
        "Open %2Fetc%2Fpasswd",
        "Read %2Fhome%2Fexample",
        "Copy foo/bar files",
        "Read foo/bar files before launch",
        'Navigate to "Shared Folder/file"',
        'Copy "Shared Folder/file"',
        'Work from "Shared Folder/file"',
        'The path is "Shared Folder/file"',
        "The directory is “Shared Folder/file”",
        "Path assigned to this seat is foo/bar",
        "Path used by reviewer is foo/bar",
        "Path used on this seat is foo/bar",
        "Path used on reviewer is foo/bar",
        "Path: ~example",
        "Home directory is ~example",
        "Open ~example",
        "%2Fetc%2Fpasswd",
        "%252Fetc%252Fpasswd",
        "See %2Fhome%2Fexample",
    ],
)
def test_parse_rejects_paths_in_navigation_and_quoted_declarations(field: str, text: str) -> None:
    document = _document()
    document["seats"][0][field] = [text] if field == "caveats" else text  # type: ignore[index]

    with pytest.raises(fleet_master_roster.MasterRosterError):
        fleet_master_roster.parse(document)


def test_parse_rejects_duplicate_seat_names() -> None:
    document = _document()
    document["seats"].append(document["seats"][0])  # type: ignore[union-attr,index]

    with pytest.raises(fleet_master_roster.MasterRosterError, match="duplicate"):
        fleet_master_roster.parse(document)


def test_parse_rejects_fallback_targets_that_define_fallbacks() -> None:
    document = _document()
    document["seats"][1]["fallbacks"] = ["implementer"]  # type: ignore[index]

    with pytest.raises(fleet_master_roster.MasterRosterError, match="fallback target.*must not define"):
        fleet_master_roster.parse(document)


def test_parse_rejects_non_utf8_json_bytes() -> None:
    encoded = json.dumps(_document()).encode("utf-16")

    with pytest.raises(fleet_master_roster.MasterRosterError, match="UTF-8"):
        fleet_master_roster.parse(encoded)


def test_parse_rejects_duplicate_json_keys() -> None:
    raw = b'{"schema":"brigade.fleet_master_roster.v1","revision":1,"revision":2,"seats":[]}'

    with pytest.raises(fleet_master_roster.MasterRosterError, match="duplicate JSON key"):
        fleet_master_roster.parse(raw)


def test_fleet_roster_validate_does_not_echo_control_duplicate_key(tmp_path, capsys) -> None:
    path = tmp_path / "duplicate-key.json"
    path.write_bytes(
        b'{"schema":"brigade.fleet_master_roster.v1","revision":1,"seats":[],"\\u001b[2J":1,"\\u001b[2J":2}'
    )

    assert cli.main(["fleet", "roster", "validate", "--file", str(path)]) == 1

    error = capsys.readouterr().err
    assert "duplicate JSON key" in error
    assert "\x1b" not in error


def test_parse_rejects_non_string_mapping_keys() -> None:
    document = _document()
    document[1] = "unexpected"  # type: ignore[index]

    with pytest.raises(fleet_master_roster.MasterRosterError, match="keys must be strings"):
        fleet_master_roster.parse(document)


def test_parse_rejects_control_characters_in_json_property_names() -> None:
    raw = b'{"schema":"brigade.fleet_master_roster.v1","revision":1,"seats":[],"\\u001b[2Jforged error":true}'

    with pytest.raises(fleet_master_roster.MasterRosterError, match="keys must not contain controls"):
        fleet_master_roster.parse(raw)


def test_parse_rejects_unicode_format_controls_in_json_property_names() -> None:
    raw = b'{"schema":"brigade.fleet_master_roster.v1","revision":1,"seats":[],"\\u202eforged":true}'

    with pytest.raises(fleet_master_roster.MasterRosterError, match="keys must not contain controls"):
        fleet_master_roster.parse(raw)


def test_parse_does_not_echo_secret_unknown_property_names() -> None:
    secret_name = "github_pat_" + "A" * 24
    document = _document()
    document["seats"] = []
    document[secret_name] = True

    with pytest.raises(fleet_master_roster.MasterRosterError) as exc_info:
        fleet_master_roster.parse(json.dumps(document).encode("utf-8"))

    assert secret_name not in str(exc_info.value)


def test_parse_rejects_lone_surrogate_json_property_names() -> None:
    raw = b'{"schema":"brigade.fleet_master_roster.v1","revision":1,"seats":[],"\\ud800":true}'

    with pytest.raises(fleet_master_roster.MasterRosterError, match="valid Unicode scalar values") as exc_info:
        fleet_master_roster.parse(raw)

    str(exc_info.value).encode("utf-8")


@pytest.mark.parametrize("separator", ["\u2028", "\u2029"])
def test_parse_rejects_unicode_separators_in_json_property_names(separator: str) -> None:
    document = _document()
    document["seats"] = []
    document[f"forged{separator}key"] = True
    raw = json.dumps(document, ensure_ascii=True).encode("utf-8")

    with pytest.raises(fleet_master_roster.MasterRosterError, match="keys must not contain controls"):
        fleet_master_roster.parse(raw)


@pytest.mark.parametrize("field", ["model", "purpose", "caveats"])
def test_parse_rejects_unicode_format_controls_in_roster_text(field: str) -> None:
    document = _document()
    value = "safe\u202etext"
    document["seats"][0][field] = [value] if field == "caveats" else value  # type: ignore[index]

    with pytest.raises(fleet_master_roster.MasterRosterError, match="without controls"):
        fleet_master_roster.parse(json.dumps(document, ensure_ascii=True).encode("utf-8"))


@pytest.mark.parametrize("field", ["model", "purpose", "caveats"])
@pytest.mark.parametrize("separator", ["\u2028", "\u2029"])
def test_parse_rejects_unicode_separators_in_roster_text(field: str, separator: str) -> None:
    document = _document()
    value = f"safe{separator}text"
    document["seats"][0][field] = [value] if field == "caveats" else value  # type: ignore[index]

    with pytest.raises(fleet_master_roster.MasterRosterError, match="without controls"):
        fleet_master_roster.parse(json.dumps(document, ensure_ascii=True).encode("utf-8"))


def test_parse_normalizes_unpaired_surrogate_string_input() -> None:
    with pytest.raises(fleet_master_roster.MasterRosterError, match="valid UTF-8 JSON"):
        fleet_master_roster.parse("\ud800")


def test_parse_rejects_escaped_lone_surrogate_in_json_text() -> None:
    raw = (
        b'{"schema":"brigade.fleet_master_roster.v1","revision":1,"seats":['
        b'{"name":"worker","cli":"codex","model":"gpt","purpose":"\\ud800",'
        b'"enabled":true}]}'
    )

    with pytest.raises(fleet_master_roster.MasterRosterError, match="valid Unicode"):
        fleet_master_roster.parse(raw)


@pytest.mark.parametrize("field", ["model", "purpose"])
def test_parse_rejects_whitespace_only_required_text(field: str) -> None:
    document = _document()
    document["seats"][0][field] = " \t "  # type: ignore[index]

    with pytest.raises(fleet_master_roster.MasterRosterError, match="non-empty"):
        fleet_master_roster.parse(document)


@pytest.mark.skipif(sys.version_info < (3, 10, 7), reason="requires Python's integer-string digit limit")
def test_parse_normalizes_oversized_mapping_revision() -> None:
    document = _document()
    document["revision"] = 10**5000

    with pytest.raises(fleet_master_roster.MasterRosterError, match="JSON"):
        fleet_master_roster.parse(document)


@pytest.mark.skipif(sys.version_info < (3, 10, 7), reason="requires Python's integer-string digit limit")
def test_parse_normalizes_json_integer_digit_limit() -> None:
    raw = b'{"schema":"brigade.fleet_master_roster.v1","revision":' + b"9" * 5000 + b',"seats":[]}'

    with pytest.raises(fleet_master_roster.MasterRosterError, match="valid UTF-8 JSON"):
        fleet_master_roster.parse(raw)


def test_fleet_roster_validate_cli_reports_document_digest(tmp_path, capsys) -> None:
    path = tmp_path / "roster.json"
    path.write_text(json.dumps(_document()), encoding="utf-8")

    assert cli.main(["fleet", "roster", "validate", "--file", str(path), "--json"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["revision"] == 7
    assert output["seats"] == 2
    assert output["sha256"].startswith("sha256:")


def test_fleet_roster_validate_cli_refuses_forbidden_document(tmp_path, capsys) -> None:
    path = tmp_path / "roster.json"
    path.write_text(json.dumps({**_document(), "api_token": "placeholder"}), encoding="utf-8")

    assert cli.main(["fleet", "roster", "validate", "--file", str(path)]) == 1
    assert "unknown document field" in capsys.readouterr().err


def test_fleet_roster_validate_cli_reads_only_document_limit_plus_one(tmp_path, monkeypatch, capsys) -> None:
    path = tmp_path / "oversized.json"
    path.write_bytes(b" " * (fleet_master_roster.MAX_DOCUMENT_BYTES + 1))
    original_open = type(path).open
    requested_sizes: list[int] = []

    class TrackingFile:
        def __init__(self, file):
            self.file = file

        def __enter__(self):
            self.file.__enter__()
            return self

        def __exit__(self, *args):
            return self.file.__exit__(*args)

        def read(self, size=-1):
            requested_sizes.append(size)
            return self.file.read(size)

    def tracking_open(self, *args, **kwargs):
        return TrackingFile(original_open(self, *args, **kwargs))

    monkeypatch.setattr(type(path), "open", tracking_open)

    assert cli.main(["fleet", "roster", "validate", "--file", str(path)]) == 1
    assert requested_sizes == [fleet_master_roster.MAX_DOCUMENT_BYTES + 1]
    assert "at most" in capsys.readouterr().err
