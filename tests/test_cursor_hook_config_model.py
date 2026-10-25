"""Table-driven checks for the Cursor hook config model (issue #1898, PR #1938).

Every Codex finding on the PR maps to at least one row below; the `finding`
field names it by review comment id.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from brigade import cli, cursor_user_cmd, fleet_client, localio
from tests._home import set_home

EVENTS = cursor_user_cmd._MANAGED_HOOK_EVENTS
PROJECT = cursor_user_cmd._hook_entry(scope="project")
LEGACY = cursor_user_cmd._hook_entry(scope="user")
FOREIGN = {"command": "keep-foreign-hook"}
FOREIGN_2 = {"command": "keep-other-foreign-hook"}
LIMIT = cursor_user_cmd._MAX_HOOKS_JSON_BYTES
# Independent contract from https://cursor.com/docs/hooks#hook-events,
# retrieved 2026-10-22. Keep this table independent of the validator's allowlist.
DOCUMENTED_EVENTS = (
    "sessionStart",
    "sessionEnd",
    "preToolUse",
    "postToolUse",
    "postToolUseFailure",
    "subagentStart",
    "subagentStop",
    "beforeShellExecution",
    "afterShellExecution",
    "beforeMCPExecution",
    "afterMCPExecution",
    "beforeReadFile",
    "afterFileEdit",
    "beforeSubmitPrompt",
    "preCompact",
    "stop",
    "afterAgentResponse",
    "afterAgentThought",
    "beforeTabFileRead",
    "afterTabFileEdit",
    "workspaceOpen",
)


def _all_events(entry: dict[str, str]) -> dict[str, list[dict[str, str]]]:
    return {event: [entry] for event in EVENTS}


def _padded(size: int, *, managed: bool = True) -> str:
    """Valid hooks JSON that is exactly `size` bytes, with a foreign hook ahead of any managed ones."""
    doc = {
        "version": 1,
        "hooks": {
            "beforeSubmitPrompt": [{"command": "keep", "padding": ""}],
            **(_all_events(PROJECT) if managed else {}),
        },
    }
    base = len(json.dumps(doc))
    doc["hooks"]["beforeSubmitPrompt"][0]["padding"] = "x" * (size - base)
    text = json.dumps(doc)
    assert len(text.encode()) == size
    return text


def _wire(path: Path, harnesses: list[str]) -> Path:
    (path / ".brigade").mkdir(parents=True, exist_ok=True)
    (path / ".brigade" / "config.json").write_text(
        json.dumps({"version": 1, "depth": "repo", "harnesses": harnesses, "owner": harnesses[0]}),
        encoding="utf-8",
    )
    return path


def _seed(workspace: Path, content: str | dict[str, Any] | None) -> Path:
    path = workspace / ".cursor" / "hooks.json"
    if content is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
    return path


# (id, finding, initial file, expected hooks after install or None when refused)
INSTALL_ROWS = [
    pytest.param(
        "empty-repo",
        "r4344139090 P1: fresh config needs version",
        None,
        _all_events(PROJECT),
        id="empty-repo-writes-version-1",
    ),
    pytest.param(
        "empty-object",
        "r4344139090 P1: fresh config needs version",
        {},
        _all_events(PROJECT),
        id="empty-object-is-a-fresh-document",
    ),
    pytest.param(
        "user-authored",
        "brief: preserve user-authored entries and order",
        {
            "version": 1,
            "hooks": {"beforeSubmitPrompt": [FOREIGN], "sessionStart": [FOREIGN, FOREIGN_2], "sessionEnd": [FOREIGN]},
        },
        {
            "beforeSubmitPrompt": [FOREIGN],
            "sessionStart": [FOREIGN, FOREIGN_2, PROJECT],
            "postToolUse": [PROJECT],
            "sessionEnd": [FOREIGN, PROJECT],
        },
        id="user-authored-hooks-preserved",
    ),
    pytest.param(
        "legacy",
        "r4343977825 P2: legacy project entry migrates in place",
        {"version": 1, "hooks": {"sessionStart": [FOREIGN, LEGACY], "postToolUse": [LEGACY], "sessionEnd": [LEGACY]}},
        {"sessionStart": [FOREIGN, PROJECT], "postToolUse": [PROJECT], "sessionEnd": [PROJECT]},
        id="legacy-managed-hooks-migrate",
    ),
    pytest.param(
        "mixed-and-duplicates",
        "r4343977836 P2: mixed legacy and new collapse to one entry",
        {"version": 1, "hooks": {"sessionStart": [LEGACY, FOREIGN, PROJECT, LEGACY, PROJECT]}},
        {"sessionStart": [PROJECT, FOREIGN], "postToolUse": [PROJECT], "sessionEnd": [PROJECT]},
        id="mixed-legacy-and-new-with-duplicates",
    ),
    pytest.param(
        "already-current",
        "idempotent re-run",
        {"version": 3, "hooks": _all_events(PROJECT)},
        _all_events(PROJECT),
        id="current-config-with-newer-version-is-untouched",
    ),
    pytest.param(
        "future-event",
        "future schema events are preserved while installing project hooks",
        {"version": 2, "hooks": {"sessionStart": [FOREIGN], "futureEvent": []}},
        {**_all_events(PROJECT), "sessionStart": [FOREIGN, PROJECT], "futureEvent": []},
        id="future-version-unknown-event-is-preserved",
    ),
    pytest.param(
        "at-limit",
        "r4344044551 P2: the size limit is inclusive",
        _padded(LIMIT),
        None,
        id="config-at-the-limit-is-accepted",
    ),
    *[
        pytest.param(
            event,
            "issue #1961: every documented event is preserved during installation",
            {"version": 1, "hooks": {event: [FOREIGN]}},
            {**_all_events(PROJECT), event: [FOREIGN, PROJECT] if event in EVENTS else [FOREIGN]},
            id=f"documented-{event}",
        )
        for event in DOCUMENTED_EVENTS
    ],
]


@pytest.mark.parametrize(("name", "finding", "initial", "expected"), INSTALL_ROWS)
def test_project_install_model(tmp_path, name, finding, initial, expected):
    path = _seed(tmp_path, initial)

    assert cursor_user_cmd.install_project_presence_hooks(tmp_path) is None
    first = path.read_text()
    doc = json.loads(first)
    assert isinstance(doc["version"], int) and doc["version"] >= 1
    if isinstance(initial, dict) and initial.get("version"):
        assert doc["version"] == initial["version"]
    elif initial is None or initial == {}:
        assert doc["version"] == 1
    if expected is not None:
        assert doc["hooks"] == expected
    for event in EVENTS:
        assert doc["hooks"][event].count(PROJECT) == 1
        assert LEGACY not in doc["hooks"][event]
        assert cursor_user_cmd.project_presence_hook_installed(tmp_path, event)

    assert cursor_user_cmd.install_project_presence_hooks(tmp_path) is None
    assert path.read_text() == first


# (id, finding, initial file, error substring)
REFUSED_ROWS = [
    pytest.param(
        "above-limit",
        "r4344044551/r4344099017: oversized configs are never edited",
        _padded(LIMIT + 1),
        "1 MiB",
        id="config-above-the-limit",
    ),
    pytest.param(
        "output-above-limit",
        "r4344099017: never write a document the loader would reject",
        _padded(LIMIT - 10, managed=False),
        "1 MiB",
        id="rewrite-would-cross-the-limit",
    ),
    pytest.param(
        "malformed", "r4344139095 P2: whole document must parse", '{"version": 1, "hooks": {', "", id="malformed"
    ),
    pytest.param("empty-file", "r4344139095 P2", "", "", id="empty-file"),
    pytest.param("not-an-object", "invalid document", "[]", "object", id="array-root"),
    pytest.param(
        "no-version", "r4344139090 P1: existing config without version", {"hooks": {}}, "version", id="missing-version"
    ),
    pytest.param("zero-version", "r4344139090 P1", {"version": 0, "hooks": {}}, "version", id="version-zero"),
    pytest.param("string-version", "r4344139090 P1", {"version": "1", "hooks": {}}, "version", id="version-string"),
    pytest.param("bool-version", "r4344139090 P1", {"version": True, "hooks": {}}, "version", id="version-bool"),
    pytest.param("hooks-not-object", "invalid document", {"version": 1, "hooks": []}, "object", id="hooks-not-object"),
    pytest.param("hooks-null", "r4344688457 P2", {"version": 1, "hooks": None}, "object", id="hooks-null"),
    pytest.param(
        "bad-definition",
        "r4344258655 P2: every hook definition has a valid shape",
        {"version": 1, "hooks": {"beforeSubmitPrompt": [{}], "sessionStart": [PROJECT]}},
        "command or prompt",
        id="empty-hook-definition",
    ),
    pytest.param(
        "bad-command",
        "r4344258655 P2",
        {"version": 1, "hooks": {"stop": [{"command": 1}]}},
        "command or prompt",
        id="non-string-command",
    ),
    pytest.param(
        "not-a-dict",
        "r4344258655 P2",
        {"version": 1, "hooks": {"stop": ["run-it"]}},
        "command or prompt",
        id="string-definition",
    ),
    pytest.param(
        "event-not-list",
        "invalid document",
        {"version": 1, "hooks": {"sessionEnd": {}}},
        "must be a list",
        id="event-not-list",
    ),
    pytest.param(
        "future-event-not-list",
        "invalid document",
        {"version": 2, "hooks": {"futureEvent": {}}},
        "must be a list",
        id="future-version-event-not-list",
    ),
    pytest.param(
        "future-event-bad-definition",
        "r4344258655 P2",
        {"version": 2, "hooks": {"futureEvent": [{}]}},
        "command or prompt",
        id="future-version-event-bad-definition",
    ),
    *[
        pytest.param(
            event,
            "issue #1961: unknown events invalidate the whole document",
            {"version": 1, "hooks": {"sessionStart": [PROJECT], event: entries}},
            "unknown event",
            id=f"unknown-{event}",
        )
        for event, entries in [("sessionEnded", [FOREIGN]), ("unknownEvent", [])]
    ],
]


@pytest.mark.parametrize(("name", "finding", "initial", "error"), REFUSED_ROWS)
def test_project_install_refuses_an_invalid_document_and_leaves_it_alone(tmp_path, name, finding, initial, error):
    path = _seed(tmp_path, initial)
    before = path.read_bytes()

    result = cursor_user_cmd.install_project_presence_hooks(tmp_path)

    assert result and error in result
    assert path.read_bytes() == before
    assert not any(cursor_user_cmd.project_presence_hook_installed(tmp_path, event) for event in EVENTS)


def test_project_install_write_failures_return_text_and_keep_the_original(tmp_path, monkeypatch):
    """r4343906080 P2 (OSError becomes a message) and r4343949439 P2 (atomic replace)."""
    original = json.dumps({"version": 1, "hooks": {"beforeSubmitPrompt": [FOREIGN]}})
    path = _seed(tmp_path, original)
    real_replace = localio.os.replace

    def fail_replace(source, destination):
        if Path(destination) == path:
            raise OSError("disk full")
        return real_replace(source, destination)

    monkeypatch.setattr(localio.os, "replace", fail_replace)

    assert cursor_user_cmd.install_project_presence_hooks(tmp_path) == "disk full"
    assert path.read_text() == original
    assert list(path.parent.glob(".hooks.json.*.tmp")) == []


# (id, finding, file content, expected owned events)
OWNERSHIP_ROWS = [
    pytest.param(
        "valid", "project owns the event", {"version": 1, "hooks": _all_events(PROJECT)}, set(EVENTS), id="valid"
    ),
    pytest.param(
        "future-version",
        "future schema event names do not invalidate known project ownership",
        {"version": 2, "hooks": {"sessionStart": [PROJECT], "futureEvent": []}},
        {"sessionStart"},
        id="future-version-preserves-project-ownership",
    ),
    pytest.param(
        "one-event",
        "ownership is per event",
        {"version": 1, "hooks": {"sessionStart": [PROJECT], "postToolUse": [LEGACY]}},
        {"sessionStart"},
        id="owns-only-the-event-it-lists",
    ),
    pytest.param(
        "legacy",
        "r4343977825 P2: the legacy command is not distinguishable as project scope",
        {"version": 1, "hooks": _all_events(LEGACY)},
        set(),
        id="legacy-entry-does-not-own",
    ),
    pytest.param(
        "managed-command-before-bad-suffix",
        "r4344139095 P2: no ownership from a prefix of a malformed document",
        '{"version": 1, "hooks": {"sessionStart": [{"command": "' + PROJECT["command"] + '"}]}, "oops": ',
        set(),
        id="malformed-suffix",
    ),
    pytest.param(
        "no-version",
        "r4344139090 P1: a config Cursor cannot load owns nothing",
        {"hooks": _all_events(PROJECT)},
        set(),
        id="missing-version",
    ),
    pytest.param(
        "wrong-event-type",
        "invalid document",
        {"version": 1, "hooks": {"sessionStart": [PROJECT], "sessionEnd": "nope"}},
        set(),
        id="sibling-event-wrong-type",
    ),
    pytest.param(
        "bad-sibling-definition",
        "r4344258655 P2: another event's malformed definition invalidates the document",
        {"version": 1, "hooks": {"sessionStart": [PROJECT], "stop": [{"command": 1}]}},
        set(),
        id="sibling-event-malformed-definition",
    ),
    pytest.param(
        "valid-prompt-definition",
        "r4344258655 P2: prompt hooks are valid definitions",
        {"version": 1, "hooks": {"sessionStart": [PROJECT], "stop": [{"type": "prompt", "prompt": "check"}]}},
        {"sessionStart"},
        id="prompt-definition-is-valid",
    ),
    pytest.param(
        "unsupported-type",
        "r4344601010 P2: a definition type Cursor rejects invalidates the document",
        {"version": 1, "hooks": {"sessionStart": [PROJECT], "stop": [{"type": "bogus", "command": "x"}]}},
        set(),
        id="unsupported-hook-type",
    ),
    pytest.param(
        "explicit-command-type",
        "r4344601010 P2: type command is a supported definition",
        {"version": 1, "hooks": {"sessionStart": [PROJECT], "stop": [{"type": "command", "command": "x"}]}},
        {"sessionStart"},
        id="explicit-command-type-is-valid",
    ),
    pytest.param(
        "explicit-project-command-type",
        "issue #1968: command type defaults to command for project ownership",
        {"version": 1, "hooks": {"sessionStart": [{"type": "command", **PROJECT}]}},
        {"sessionStart"},
        id="explicit-command-type-project-ownership",
    ),
    pytest.param("at-limit", "r4344099017: limit is inclusive", _padded(LIMIT), set(EVENTS), id="owns-at-the-limit"),
    pytest.param(
        "grew-past-limit",
        "r4344099017 P2: oversized config fails open",
        _padded(LIMIT + 1),
        set(),
        id="above-the-limit-fails-open",
    ),
]


@pytest.mark.parametrize(("name", "finding", "initial", "owned"), OWNERSHIP_ROWS)
def test_project_ownership_requires_a_whole_valid_document(tmp_path, name, finding, initial, owned):
    _seed(tmp_path, initial)

    assert {e for e in EVENTS if cursor_user_cmd.project_presence_hook_installed(tmp_path, e)} == owned


def test_init_deduplicates_explicit_command_type_entries_and_preserves_options(tmp_path):
    hooks_path = _seed(
        tmp_path,
        {
            "version": 1,
            "hooks": {
                "sessionStart": [
                    {"type": "command", **PROJECT},
                    {"type": "command", **PROJECT},
                    {"type": "command", "command": LEGACY["command"]},
                    {"type": "command", "command": LEGACY["command"]},
                    {"type": "command", **PROJECT, "timeout": 12},
                ]
            },
        },
    )
    owned_before_init = cursor_user_cmd.project_presence_hook_installed(tmp_path, "sessionStart")

    command = ["init", "--target", str(tmp_path), "--depth", "repo", "--harnesses", "cursor"]
    assert cli.main(command) == 0
    first = hooks_path.read_bytes()
    hooks = json.loads(first)["hooks"]
    assert owned_before_init
    assert hooks["sessionStart"] == [PROJECT, {"type": "command", **PROJECT, "timeout": 12}]
    for event in EVENTS:
        assert sum(entry in (PROJECT, {"type": "command", **PROJECT}) for entry in hooks[event]) == 1
    assert cli.main(command) == 0
    assert hooks_path.read_bytes() == first


# Scope resolution through the real hook entry point. `callbacks` lists the
# scopes Cursor would run for a sessionStart in that configuration.
SCOPE_ROWS = [
    pytest.param(
        ["codex"],
        None,
        ["user"],
        0,
        "issue #1898: user hook skips a project selected for another harness",
        id="other-harness",
    ),
    pytest.param(["cursor"], None, ["user"], 1, "user-only", id="user-only"),
    pytest.param(
        ["cursor"], {"version": 1, "hooks": _all_events(PROJECT)}, ["project"], 1, "project-only", id="project-only"
    ),
    pytest.param(
        ["cursor"],
        {"version": 1, "hooks": _all_events(PROJECT)},
        ["project", "user"],
        1,
        "r4343833587 P2: both scopes publish once",
        id="both-scopes-publish-once",
    ),
    pytest.param(
        ["cursor"],
        {"version": 1, "hooks": _all_events(LEGACY)},
        ["user", "user"],
        2,
        "r4343977825 P2: legacy project entry stays active until init migrates it",
        id="legacy-project-entry",
    ),
    pytest.param(
        ["cursor"],
        '{"version": 1, "hooks": {"sessionStart": [{"command": "' + PROJECT["command"] + '"}]}, "oops": ',
        ["user"],
        1,
        "r4344139095 P2: malformed project config does not suppress the user hook",
        id="malformed-project-config",
    ),
    pytest.param(
        ["cursor"],
        {"hooks": _all_events(PROJECT)},
        ["user"],
        1,
        "r4344139090 P1: unversioned project config does not suppress the user hook",
        id="unversioned-project-config",
    ),
    pytest.param(
        ["cursor"],
        {"version": 1, "hooks": {**_all_events(PROJECT), "stop": [{"command": 1}]}},
        ["user"],
        1,
        "r4344258655 P2: a malformed definition anywhere in the project config does not suppress the user hook",
        id="malformed-definition-in-project-config",
    ),
    pytest.param(
        ["cursor"],
        {"version": 1, "hooks": {**_all_events(PROJECT), "stop": [{"type": "bogus", "command": "x"}]}},
        ["user"],
        1,
        "r4344601010 P2: an unsupported definition type in the project config does not suppress the user hook",
        id="unsupported-type-in-project-config",
    ),
    pytest.param(
        ["cursor"],
        _padded(LIMIT + 1),
        ["user"],
        1,
        "r4344099017 P2: oversized project config fails open",
        id="oversized-project-config",
    ),
    *[
        pytest.param(
            ["cursor"],
            {"version": 1, "hooks": {"sessionStart": [PROJECT], event: entries}},
            ["user"],
            1,
            "issue #1961: unknown events invalidate project ownership without suppressing the user hook",
            id=f"unknown-{event}-in-project-config",
        )
        for event, entries in [("sessionEnded", [FOREIGN]), ("unknownEvent", [])]
    ],
]


@pytest.mark.parametrize(("harnesses", "project_file", "callbacks", "published", "finding"), SCOPE_ROWS)
def test_scope_resolution_publishes_the_expected_number_of_times(
    tmp_path, monkeypatch, capsys, harnesses, project_file, callbacks, published, finding
):
    workspace = _wire(tmp_path, harnesses)
    _seed(workspace, project_file)
    calls: list[Any] = []
    monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "http://127.0.0.1:3774")
    monkeypatch.setattr(fleet_client, "upsert_session", lambda snap: calls.append(snap) or True)
    payload = json.dumps(
        {"conversation_id": "conv-1", "hook_event_name": "sessionStart", "workspace_roots": [str(workspace)]}
    )

    for scope in callbacks:
        monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
        context = "cursor-work-loop-project" if scope == "project" else "cursor-work-loop"
        assert cli.main(["work", "presence-hook", "--harness", "cursor", "--stdin", "--context", context]) == 0
        capsys.readouterr()

    assert len(calls) == published


def test_fresh_user_scope_hooks_carry_a_version(tmp_path, monkeypatch, capsys):
    """r4344139090 P1 at user scope: a new ~/.cursor/hooks.json is also a versioned document."""
    monkeypatch.setattr(cursor_user_cmd, "_home_dir", lambda: tmp_path)

    assert cursor_user_cmd.install(write=True, json_output=True) == 0
    capsys.readouterr()

    doc = json.loads((tmp_path / ".cursor" / "hooks.json").read_text())
    assert doc["version"] == 1
    assert all(LEGACY in doc["hooks"][event] for event in EVENTS)


# User-scope documents follow the same validator, except that a versionless
# document is repaired: sync is an explicit edit of the user's own file and
# older releases wrote it without `version`.
USER_ROWS = [
    *[
        pytest.param(
            {"version": 1, "hooks": {"sessionStart": [LEGACY], event: entries}},
            "conflict",
            id=f"unknown-{event}",
        )
        for event, entries in [("sessionEnded", [FOREIGN]), ("unknownEvent", [])]
    ],
    *[
        pytest.param(
            {"hooks": {"sessionStart": [LEGACY], event: entries}},
            "conflict",
            id=f"versionless-unknown-{event}",
        )
        for event, entries in [("sessionEnded", [FOREIGN]), ("unknownEvent", [])]
    ],
    pytest.param(
        {"version": 2, "hooks": {"sessionStart": [FOREIGN, LEGACY], "futureEvent": []}},
        "repaired",
        id="future-version-unknown-event-is-preserved",
    ),
    pytest.param({"hooks": {"sessionStart": [FOREIGN, LEGACY]}}, "repaired", id="older-release-document-is-repaired"),
    pytest.param({"hooks": {"beforeSubmitPrompt": [FOREIGN]}}, "repaired", id="foreign-document-without-version"),
    pytest.param({"version": "1", "hooks": {}}, "conflict", id="string-version"),
    pytest.param({"version": 1, "hooks": {"sessionStart": "nope"}}, "conflict", id="event-not-list"),
    pytest.param({"version": 1, "hooks": None}, "conflict", id="hooks-null"),
    pytest.param({"hooks": None}, "conflict", id="versionless-hooks-null"),
    pytest.param('{"version": 1, "hooks": {', "conflict", id="malformed"),
]


@pytest.mark.parametrize("route", ["install", "sync"])
@pytest.mark.parametrize(("initial", "outcome"), USER_ROWS)
def test_user_scope_hook_document_validation(tmp_path, monkeypatch, capsys, route, initial, outcome):
    """r4344204939 P1: existing user hook documents are validated and migrated, not extended in place."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    set_home(monkeypatch, str(home))
    monkeypatch.setenv("BRIGADE_NO_UPDATE_CHECK", "1")
    path = _seed(home, initial)
    path = home / ".cursor" / "hooks.json"
    before = path.read_bytes()
    workspace = _wire(tmp_path / "workspace", ["cursor"])
    if route == "install":
        command = ["harness", "install", "cursor", "--scope", "user", "--write", "--json"]
    else:
        command = ["harness", "sync", "--target", "cursor", "--scope", "user", "--workspace", str(workspace)]
        command += ["--write", "--adopt", "--json"]

    rc = cli.main(command)
    capsys.readouterr()

    if outcome == "conflict":
        assert rc == 1
        assert path.read_bytes() == before
        return
    assert rc == 0
    doc = json.loads(path.read_text())
    assert doc["version"] == (initial.get("version", 1) if isinstance(initial, dict) else 1)
    for event, entries in initial["hooks"].items():
        assert doc["hooks"][event][: len(entries)] == entries
    assert all(LEGACY in doc["hooks"][event] for event in EVENTS)
