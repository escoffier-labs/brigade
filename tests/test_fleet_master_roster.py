from __future__ import annotations

import hashlib
import json
import subprocess
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
                "purpose": "implementation",
                "caveats": ["focused-verification"],
                "fallbacks": ["reviewer"],
                "enabled": True,
            },
            {
                "name": "reviewer",
                "cli": "claude",
                "model": "opus",
                "purpose": "review",
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


def test_parse_rejects_duplicate_fallbacks() -> None:
    document = _document()
    document["seats"][0]["fallbacks"] = ["reviewer", "reviewer"]  # type: ignore[index]

    with pytest.raises(fleet_master_roster.MasterRosterError, match="duplicate fallback"):
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


def test_parse_rejects_oversized_mapping_revision() -> None:
    document = _document()
    document["revision"] = 10**5000

    with pytest.raises(fleet_master_roster.MasterRosterError, match="revision"):
        fleet_master_roster.parse(document)


def test_parse_rejects_json_integer_above_revision_limit() -> None:
    raw = b'{"schema":"brigade.fleet_master_roster.v1","revision":' + b"9" * 5000 + b',"seats":[]}'

    with pytest.raises(fleet_master_roster.MasterRosterError, match="valid UTF-8 JSON"):
        fleet_master_roster.parse(raw)


@pytest.mark.parametrize("as_json", [False, True])
def test_parse_rejects_revision_above_supported_limit(as_json: bool) -> None:
    document = _document()
    document["revision"] = fleet_master_roster.MAX_REVISION + 1
    raw = json.dumps(document).encode("utf-8") if as_json else document

    with pytest.raises(fleet_master_roster.MasterRosterError, match="revision"):
        fleet_master_roster.parse(raw)


def test_parse_accepts_maximum_supported_revision() -> None:
    document = _document()
    document["revision"] = fleet_master_roster.MAX_REVISION

    assert fleet_master_roster.parse(document)["revision"] == fleet_master_roster.MAX_REVISION


def test_fleet_roster_validate_cli_reports_document_digest(tmp_path, capsys) -> None:
    path = tmp_path / "roster.json"
    path.write_text(json.dumps(_document()), encoding="utf-8")

    assert cli.main(["fleet", "roster", "validate", "--file", str(path), "--json"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["revision"] == 7
    assert output["seats"] == 2
    assert output["sha256"].startswith("sha256:")


def test_cli_parser_defers_roster_validator_import() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from brigade.cli import _build_parser; _build_parser(); "
            "import sys; assert 'brigade.fleet_master_roster' not in sys.modules",
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr


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


@pytest.mark.parametrize("field", ["purpose", "caveats"])
@pytest.mark.parametrize(
    "text",
    [
        "Authentication uses profile production-admin.",
        "The checkout is private-area/config-prod",
        "password hunter2",
    ],
)
def test_parse_rejects_disclosures_outside_allowed_vocabulary(field: str, text: str) -> None:
    document = _document()
    document["seats"][0][field] = [text] if field == "caveats" else text  # type: ignore[index]

    with pytest.raises(fleet_master_roster.MasterRosterError):
        fleet_master_roster.parse(document)


@pytest.mark.parametrize(
    "role", ["implementation", "review", "research", "planning", "triage", "documentation", "security-review"]
)
def test_parse_accepts_known_purposes(role: str) -> None:
    document = _document()
    document["seats"][0]["purpose"] = role
    assert fleet_master_roster.parse(document)["seats"][0]["purpose"] == role


def test_parse_accepts_known_caveats_and_preserves_digest_and_copy() -> None:
    document = _document()
    caveats = ["focused-verification", "authentication-required", "read-only", "manual-approval"]
    document["seats"][0]["caveats"] = caveats
    parsed = fleet_master_roster.parse(json.dumps(document).encode("utf-8"))
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    assert parsed["sha256"] == "sha256:" + hashlib.sha256(canonical).hexdigest()
    assert parsed["seats"][0]["caveats"] == caveats
    parsed["seats"][0]["caveats"].clear()
    assert document["seats"][0]["caveats"] == caveats


@pytest.mark.parametrize("field", ["purpose", "caveats"])
@pytest.mark.parametrize(
    "text",
    [
        "unknown",
        "Implementation",
        " implementation",
        "implementation ",
        "Support café users.",
        "Use the repository's focused verification gate.",
    ],
)
def test_parse_rejects_unknown_descriptive_values(field: str, text: str) -> None:
    document = _document()
    document["seats"][0][field] = [text] if field == "caveats" else text
    with pytest.raises(fleet_master_roster.MasterRosterError, match="allowed value"):
        fleet_master_roster.parse(document)


@pytest.mark.parametrize("field", ["name", "cli", "model", "purpose", "caveats", "fallbacks"])
@pytest.mark.parametrize("character", list("/\\~=: @$}{\"'`\n\t") + ["\u202e", "\ud800", "界"])
def test_parse_rejects_disallowed_characters(field: str, character: str) -> None:
    document = _document()
    # Slash is permitted only between model identifier components.
    text = character + "implementation"
    document["seats"][0][field] = [text] if field in {"caveats", "fallbacks"} else text
    with pytest.raises(fleet_master_roster.MasterRosterError):
        fleet_master_roster.parse(document)


@pytest.mark.parametrize("field", ["name", "cli", "model", "purpose", "caveats", "fallbacks"])
def test_parse_rejects_overlength_strings(field: str) -> None:
    document = _document()
    text = "a" * (fleet_master_roster.MAX_TEXT + 1)
    document["seats"][0][field] = [text] if field in {"caveats", "fallbacks"} else text
    with pytest.raises(fleet_master_roster.MasterRosterError, match="at most"):
        fleet_master_roster.parse(document)


@pytest.mark.parametrize(
    "model",
    [
        "gpt-6.1-sol",
        "claude-opus-5-5[1m]",
        "anthropic/claude-opus-5-5[200k]",
        "openai/gpt-6.1-sol",
        "provider/family/model",
        "model.preview",
        "a" * 64 + "/" + "b" * 64 + "/" + "c" * 30,
    ],
)
def test_parse_accepts_bounded_model_identifiers(model: str) -> None:
    document = _document()
    document["seats"][0]["model"] = model
    assert fleet_master_roster.parse(document)["seats"][0]["model"] == model


@pytest.mark.parametrize(
    "model",
    [
        ".ssh/config",
        "a/../b",
        "a//b",
        "/model",
        "model/",
        "a/b/c/d/e",
        "a" * 65,
        "a/" + "b" * 65,
        "openrouter/model:free",
        "https://example.com/model",
        "model[1M]",
        "model[12345m]",
        "model[]",
        "model[1m][2m]",
        "model[1m]/other",
    ],
)
def test_parse_rejects_invalid_model_identifiers(model: str) -> None:
    document = _document()
    document["seats"][0]["model"] = model
    with pytest.raises(fleet_master_roster.MasterRosterError, match="model identifier"):
        fleet_master_roster.parse(document)


def test_parse_rejects_adversarial_text_quickly() -> None:
    script = """
import json, sys, time
from brigade import fleet_master_roster as roster
start = time.perf_counter()
for value in ["token has " + "really " * 70 + "x", ("token has " + "really " * 30)[:160], "a/" * 79 + "a!", "a" * 159 + "[", "/" * 160]:
    for field in ["name", "cli", "model", "purpose", "caveats", "fallbacks"]:
        document = json.loads(sys.argv[1])
        document["seats"][0][field] = [value] if field in {"caveats", "fallbacks"} else value
        try:
            roster.parse(document)
        except roster.MasterRosterError:
            pass
        else:
            raise AssertionError((field, value))
print(time.perf_counter() - start)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, json.dumps(_document())], capture_output=True, text=True, timeout=10, check=True
    )
    assert float(result.stdout) < 0.5


@pytest.mark.parametrize(
    "field,value",
    [
        ("revision", True),
        ("revision", 1.0),
        ("revision", 0),
        ("revision", -1),
        ("seats", {}),
        ("seats", [None]),
        ("seats", [dict()]),
        ("unexpected", True),
    ],
)
def test_parse_rejects_malformed_document_fields(field: str, value: object) -> None:
    document = _document()
    document[field] = value
    with pytest.raises(fleet_master_roster.MasterRosterError):
        fleet_master_roster.parse(document)


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", "1invalid"),
        ("cli", "codex --unsafe"),
        ("enabled", 1),
        ("enabled", "true"),
        ("caveats", "read-only"),
        ("caveats", ["read-only"] * 17),
        ("caveats", ["read-only"] * 2),
        ("fallbacks", "reviewer"),
        ("fallbacks", ["missing"]),
        ("fallbacks", ["implementer"]),
        ("fallbacks", ["reviewer"] * 129),
        ("purpose", None),
    ],
)
def test_parse_rejects_malformed_seat_fields(field: str, value: object) -> None:
    document = _document()
    document["seats"][0][field] = value
    with pytest.raises(fleet_master_roster.MasterRosterError):
        fleet_master_roster.parse(document)


@pytest.mark.parametrize("as_json", [False, True])
def test_parse_rejects_too_many_seats(as_json: bool) -> None:
    document = _document()
    document["seats"] = [
        dict(document["seats"][0], name=f"worker-{i}", fallbacks=[]) for i in range(fleet_master_roster.MAX_SEATS + 1)
    ]
    with pytest.raises(fleet_master_roster.MasterRosterError, match="at most 128"):
        fleet_master_roster.parse(json.dumps(document) if as_json else document)


def test_parse_accepts_maximum_seat_count() -> None:
    document = _document()
    document["seats"] = [
        dict(document["seats"][0], name=f"worker-{i}", fallbacks=[]) for i in range(fleet_master_roster.MAX_SEATS)
    ]
    assert len(fleet_master_roster.parse(document)["seats"]) == fleet_master_roster.MAX_SEATS


@pytest.mark.parametrize("as_bytes", [False, True])
def test_parse_rejects_oversized_raw_documents(as_bytes: bool) -> None:
    text = " " * (fleet_master_roster.MAX_DOCUMENT_BYTES + 1)
    with pytest.raises(fleet_master_roster.MasterRosterError, match="at most"):
        fleet_master_roster.parse(text.encode() if as_bytes else text)


def test_parse_enforces_canonical_document_limit_for_mappings(monkeypatch) -> None:
    monkeypatch.setattr(fleet_master_roster, "MAX_DOCUMENT_BYTES", 100)
    with pytest.raises(fleet_master_roster.MasterRosterError, match="at most 100 bytes"):
        fleet_master_roster.parse(_document())


@pytest.mark.parametrize("raw", [b"{", b"[]", b"null", 3, []])
def test_parse_rejects_invalid_document_roots(raw: object) -> None:
    with pytest.raises(fleet_master_roster.MasterRosterError):
        fleet_master_roster.parse(raw)


def test_fleet_roster_validate_does_not_echo_invalid_fallback(tmp_path, capsys) -> None:
    document = _document()
    document["seats"][0]["fallbacks"] = ["password hunter2"]
    path = tmp_path / "roster.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    assert cli.main(["fleet", "roster", "validate", "--file", str(path)]) == 1
    assert "hunter2" not in capsys.readouterr().err


class _Impostor(str):
    """Compares equal to an allowed value while carrying different text."""

    def __eq__(self, other: object) -> bool:
        return True

    def __hash__(self) -> int:
        return hash("implementation")


@pytest.mark.parametrize("field", ["purpose", "cli", "name"])
def test_parse_rejects_string_subclass_values(field: str) -> None:
    document = _document()
    document["seats"][0][field] = _Impostor("private-area/config-prod")  # type: ignore[index]

    with pytest.raises(fleet_master_roster.MasterRosterError, match="non-empty string"):
        fleet_master_roster.parse(document)


def test_parse_rejects_string_subclass_keys() -> None:
    document = _document()
    seat = document["seats"][0]  # type: ignore[index]
    seat[_Impostor("enabled")] = seat.pop("enabled")  # type: ignore[index]

    with pytest.raises(fleet_master_roster.MasterRosterError, match="keys must be strings"):
        fleet_master_roster.parse(document)
