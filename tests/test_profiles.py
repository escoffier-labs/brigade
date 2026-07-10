import json

from brigade import profiles, profiles_cmd, stations_cmd


def test_repo_profile_selects_priority_stations():
    profile = profiles.resolve("repo")
    assert profile is not None
    assert profile is profiles.resolve("default")
    assert {
        "core",
        "skills",
        "memory",
        "guard",
        "security",
        "tokens",
        "evidence",
        "search",
    } <= set(profile.selected_stations)
    assert {"mcp", "pantry", "notifications"} <= set(profile.optional_stations)


def test_profiles_list_json_is_structured(capsys):
    rc = profiles_cmd.list_profiles(json_output=True)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    names = {row["name"] for row in payload["profiles"]}
    assert {"repo", "workspace", "fleet-operator"} <= names
    repo = next(row for row in payload["profiles"] if row["name"] == "repo")
    assert repo["missing_stations"] == []


def test_profiles_show_unknown_returns_cli_error(capsys):
    rc = profiles_cmd.show_profile("nope")
    assert rc == 2
    assert "unknown profile: nope" in capsys.readouterr().out


def test_stations_list_marks_selected_and_optional_for_repo(capsys):
    rc = stations_cmd.list_stations(profile_name="repo", json_output=True)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    rows = {row["station"]: row for row in payload["stations"]}
    assert rows["skills"]["selection"] == "selected"
    assert rows["tokens"]["selection"] == "selected"
    assert rows["evidence"]["selection"] == "selected"
    assert rows["mcp"]["selection"] == "optional"
    assert rows["pantry"]["selection"] == "optional"
    assert set(rows["skills"]["built_in_skills"]) == {"brigade-work", "ultra-work-scout"}
    token_tools = {tool["name"]: tool for tool in rows["tokens"]["tools"]}
    assert set(token_tools) == {"token-glace", "usage-tracker"}
    token_surfaces = {surface["kind"] for surface in token_tools["token-glace"]["surfaces"]}
    assert {"doctor-json", "summary-json", "verify-exit"} <= token_surfaces
    usage_surfaces = {surface["kind"] for surface in token_tools["usage-tracker"]["surfaces"]}
    assert usage_surfaces == {"summary-json"}


def test_stations_list_unknown_profile_returns_cli_error(capsys):
    rc = stations_cmd.list_stations(profile_name="nope")
    assert rc == 2
    assert "unknown profile: nope" in capsys.readouterr().out
