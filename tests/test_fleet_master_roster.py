from __future__ import annotations

import json

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
        "OPENAI_API_KEY=placeholder-secret",
        "Load config from C:\\Users\\operator\\.config\\secret.toml",
        "Use ~/.config/provider/credentials.json",
        "See (~/.ssh/config).",
        "Use auth profile production-admin.",
        "password: actual-secret",
    ],
)
def test_parse_rejects_secret_or_home_path_text(text: str) -> None:
    document = _document()
    document["seats"][0]["caveats"] = [text]  # type: ignore[index]

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


def test_parse_normalizes_oversized_mapping_revision() -> None:
    document = _document()
    document["revision"] = 10**5000

    with pytest.raises(fleet_master_roster.MasterRosterError, match="JSON"):
        fleet_master_roster.parse(document)


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
