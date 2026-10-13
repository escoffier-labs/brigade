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
        "Use C:Users\\alice",
        "Read D:projects\\acme",
        "Read /etc/credentials.",
        "Write ./private/output.json.",
        "Read ../private/credentials.toml.",
        "Write private/output.json.",
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
        'Use auth profile "production-admin".',
        "Use auth profile 'production-admin'.",
        "Use auth profile `production-admin`.",
        "Use auth profile [production-admin].",
        "Auth profile (production-admin).",
        "Use auth profiles: production-admin.",
        "Connect through auth profiles (production-admin).",
        "Authentication profile named production-admin.",
        "Call /Applications/Agent/Binary.",
        "Invoke /Volumes/Data/Build.",
        "Run foo/main.c",
        "Auth profile is production-admin.",
        "Use auth profile: production-admin",
        "Use auth-profile production-admin",
        "Use auth_profile production-admin",
        "Use production-admin auth profile.",
        "Use `production-admin` auth profile.",
        "Use the production-admin auth profile.",
        "password: actual-secret",
        "password is actual-secret",
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
        "token is abcdefgh",
        "api key is shortkey",
        "See https://example.com/callback?token=hunter2",
        "password is [hunter2]",
        "token is (abcdefg)",
        "npm_" + "A" * 36,
        "token is abcdefghijklmnop",
        "auth token is abcdefghijklmnop",
    ],
)
def test_parse_rejects_secret_or_home_path_text(text: str) -> None:
    document = _document()
    document["seats"][0]["caveats"] = [text]  # type: ignore[index]

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
        "Password is required.",
        "Password is confidential.",
        "Password is managed by Vault.",
        "Auth profile is required.",
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
        "Support auth profile selection.",
        "Support auth profiles as needed.",
        "Configure auth profile selection.",
        "Rotate auth profiles regularly.",
        "Support auth profile rotation.",
        "Configure authentication profile management.",
        "Use the auth profile for this service.",
    ],
)
def test_parse_accepts_prose_that_mentions_auth_profiles(text: str) -> None:
    document = _document()
    document["seats"][0]["purpose"] = text  # type: ignore[index]

    assert fleet_master_roster.parse(document)["seats"][0]["purpose"] == text


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


@pytest.mark.parametrize("model", ["gpt-4 fast", "gpt --fast", "model🚀"])
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
        "Inspect checks/unit.",
        "Run tools/build",
        "Inspect foo/bar",
        "Run the tools/build script",
        "Execute the foo/bar helper",
    ],
)
def test_parse_rejects_unenumerated_paths_in_descriptive_text(field: str, text: str) -> None:
    document = _document()
    document["seats"][0][field] = [text] if field == "caveats" else text  # type: ignore[index]

    with pytest.raises(fleet_master_roster.MasterRosterError):
        fleet_master_roster.parse(document)


@pytest.mark.parametrize("text", ["and/or", "input/output", "read/write", "input / output", "planner / reviewer"])
def test_parse_accepts_slash_separated_descriptive_prose(text: str) -> None:
    document = _document()
    document["seats"][0]["purpose"] = f"Support {text} workflows"  # type: ignore[index]
    document["seats"][0]["caveats"] = [f"Review {text} details"]  # type: ignore[index]

    assert fleet_master_roster.parse(document)["seats"][0]["purpose"] == f"Support {text} workflows"


@pytest.mark.parametrize("text", ["CI/CD", "planner/reviewer"])
def test_parse_accepts_ordinary_slash_delimited_descriptive_prose(text: str) -> None:
    document = _document()
    document["seats"][0]["purpose"] = f"Support {text} workflows"  # type: ignore[index]

    assert fleet_master_roster.parse(document)["seats"][0]["purpose"] == f"Support {text} workflows"


@pytest.mark.parametrize("field", ["purpose", "caveats"])
def test_parse_accepts_https_urls_in_descriptive_text(field: str) -> None:
    document = _document()
    text = "See https://example.com/docs?page=2 for the guide."
    if field == "purpose":
        document["seats"][0][field] = text  # type: ignore[index]
    else:
        document["seats"][0][field] = [text]  # type: ignore[index]

    parsed = fleet_master_roster.parse(document)

    assert parsed["seats"][0][field] == (text if field == "purpose" else [text])


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
