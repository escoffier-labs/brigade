"""Code-shaped hostname lookalikes and username-less home paths (token-glace false positives)."""

import pytest

from brigade.guard.engine import scan_text

HOST = "args" + ".local"
ENV_HOST = "process.env" + ".HOME"
LITERAL = "db" + ".local"
REAL = "db01" + ".internal"


def rule_ids(text: str) -> list[str]:
    return [finding.rule_id for finding in scan_text(text).findings]


@pytest.mark.parametrize(
    "text",
    [
        "    local: " + HOST + ",",
        "\tlocal:" + HOST + ",\n",
        "    delete " + ENV_HOST + ";",
        "  await " + HOST + ";",
        "typeof " + HOST + ";",
        "    yield await " + HOST + ";",
        "  else delete " + ENV_HOST + ";",
    ],
)
def test_code_shaped_members_are_not_hostnames(text: str) -> None:
    assert "private-hostname" not in rule_ids(text)


@pytest.mark.parametrize(
    "text",
    [
        "Database: " + LITERAL + ", then continue.",
        "    host: " + LITERAL,
        "    local: " + LITERAL + ", then continue.",
        "local: " + LITERAL + ",",
        "Please delete " + LITERAL + " entries.",
        "Then delete " + LITERAL,
        '    local: "' + LITERAL + '",',
        "services:\n  host: " + LITERAL + ",\n",
        "  server_url: " + LITERAL + ",",
        "Please delete " + LITERAL + ";",
        "services: {\n  primary: " + REAL + ",\n}\n",
        "  primary: " + REAL + ",",
        "do delete " + REAL + ";",
        "delete " + REAL + ";",
        "    delete " + REAL + ";",
        "delete context" + ".internal;",
        "  primary: input" + ".lan,",
        "  primary: ctx" + ".corp,",
    ],
)
def test_prose_config_and_quoted_hosts_stay_detected(text: str) -> None:
    assert "private-hostname" in rule_ids(text)


@pytest.mark.parametrize(
    "text",
    [
        "<SANDBOX>/home/" + ".claude/settings.json",
        "/home/" + ".config/opencode/plugins/plugin.js",
        "/Users/" + ".config/app/state.json",
    ],
)
def test_username_less_dot_paths_are_not_home_paths(text: str) -> None:
    assert "home-path" not in rule_ids(text)


@pytest.mark.parametrize(
    "text",
    [
        "/home/" + "alice/.claude/settings.json",
        "/Users/" + "jane/Library",
        "/home/" + "_svc/data",
        "/home/" + "a.b/notes",
        "artifact=/home/" + "./alice/build/report.txt",
        "HOME=/home/" + ".svc",
        "/Users/" + ".Trash/old",
        "/home/" + ".claudette/notes",
    ],
)
def test_user_home_paths_stay_detected(text: str) -> None:
    assert "home-path" in rule_ids(text)
