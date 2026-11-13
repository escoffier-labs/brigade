"""Capability gates, byte-preserving upgrades and downgrade coverage."""

import json
from pathlib import Path

import pytest

from brigade import cursor_hook_compat as compat, cursor_user_cmd, observed_checks, work_mode
from tests._home import set_home
from tests.test_work_mode import _wired, _set_mode
from tests.test_observe_harness_hooks import payload, run


@pytest.mark.parametrize(
    "version,expected",
    [
        ("2026.09.18-9a7762b", True),
        ("2026.10.01-e373342", True),
        ("2026.09.17-abcdef0", False),
        (None, False),
        ("unparsable", False),
        ("2026.99.99-abcdef0", False),
    ],
)
def test_version_floor(version, expected):
    assert compat.supports_failure_hook(version) is expected


@pytest.mark.parametrize(
    "version,expected",
    [("2026.09.18-9a7762b", True), ("2026.09.17-abcdef0", False), (None, False), ("unparsable", False)],
)
def test_install_capture_and_doctor(tmp_path, monkeypatch, capsys, version, expected):
    set_home(monkeypatch, tmp_path / "home")
    monkeypatch.setattr(compat, "detect_version", lambda version=version: version)
    monkeypatch.setattr(observed_checks, "_capture_clock", lambda: 0.0)
    target = _wired(tmp_path, harnesses=("cursor",))
    _set_mode(target, "observe", capsys)
    doc = json.loads((target / ".cursor/hooks.json").read_text())
    assert ("postToolUseFailure" in doc["hooks"]) is expected
    assert all(event in doc["hooks"] for event in ("sessionStart", "postToolUse", "sessionEnd"))
    # Detection must never happen in a hook, even when recording is disabled.
    monkeypatch.setattr(compat, "detect_version", lambda: pytest.fail("hook detected version"))
    run(monkeypatch, "cursor", payload(target, "cursor"))
    assert len(observed_checks.read_checks(target)[0]) == expected
    warnings = [detail for status, name, detail in work_mode.doctor_checks(target) if name == "Cursor check recording"]
    assert bool(warnings) is not expected
    if warnings:
        assert warnings == [
            f"Cursor is too old or undetected for check recording. Install Cursor Agent {compat.CURSOR_FAILURE_HOOK_MIN_VERSION} or newer and reinstall Brigade hooks."
        ]


@pytest.mark.parametrize("scope", ["project", "user", "profile"])
def test_upgrade_downgrade_preserves_entry_bytes(tmp_path, monkeypatch, scope):
    set_home(monkeypatch, tmp_path / "home")
    (tmp_path / "workspace").mkdir()
    root = tmp_path / "repo" if scope == "project" else Path.home()
    path = root / ".cursor/hooks.json"
    path.parent.mkdir(parents=True)
    foreign = '{ "command" : "user-owned", "nested" : {"keep":  true} }'
    managed = json.dumps(cursor_user_cmd._hook_entry(scope="project" if scope == "project" else "user"))
    # Prior installed document, with deliberately different user formatting.
    path.write_text(
        '{"version":1,"hooks":{"sessionStart":['
        + foreign
        + ","
        + managed
        + '],"postToolUse":['
        + managed
        + '],"sessionEnd":['
        + managed
        + "]}}"
    )

    def install():
        if scope == "project":
            assert cursor_user_cmd.install_project_presence_hooks(root) is None
        elif scope == "user":
            assert cursor_user_cmd.install(write=True, json_output=True) == 0
        else:
            from brigade import cli

            assert (
                cli.main(
                    [
                        "harness",
                        "sync",
                        "--target",
                        "cursor",
                        "--scope",
                        "user",
                        "--workspace",
                        str(tmp_path / "workspace"),
                        "--write",
                        "--adopt",
                        "--json",
                    ]
                )
                == 0
            )

    for version, expected in [
        ("2026.09.17-abcdef0", False),
        ("2026.10.01-e373342", True),
        ("2026.09.17-abcdef0", False),
    ]:
        monkeypatch.setattr(compat, "detect_version", lambda version=version: version)
        before = json.loads(path.read_text())
        install()
        text = path.read_text()
        assert foreign in text
        doc = json.loads(text)
        assert ("postToolUseFailure" in doc["hooks"]) is expected
        for event in ("sessionStart", "postToolUse", "sessionEnd"):
            assert doc["hooks"][event] == before["hooks"][event]
        install()
        assert path.read_text() == text


@pytest.mark.parametrize("scope", ["user", "profile"])
@pytest.mark.parametrize("version", ["2026.10.01-e373342", "2026.09.17-abcdef0", None])
def test_uninstall_removes_all_owned_events_at_any_version(tmp_path, monkeypatch, capsys, scope, version):
    from brigade import cli

    set_home(monkeypatch, tmp_path / "home")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(compat, "detect_version", lambda: "2026.10.01-e373342")
    if scope == "user":
        assert cursor_user_cmd.install(write=True, json_output=True) == 0
    else:
        assert (
            cli.main(
                [
                    "harness",
                    "sync",
                    "--target",
                    "cursor",
                    "--scope",
                    "user",
                    "--workspace",
                    str(workspace),
                    "--write",
                    "--json",
                ]
            )
            == 0
        )
    capsys.readouterr()
    path = Path.home() / ".cursor/hooks.json"
    doc = json.loads(path.read_text())
    assert set(doc["hooks"]) == set(cursor_user_cmd._MANAGED_HOOK_EVENTS)
    foreign = '{ "command" : "user-owned" }'
    for entries in doc["hooks"].values():
        entries.append(json.loads(foreign))
    path.write_text(json.dumps(doc).replace(json.dumps(json.loads(foreign)), foreign))
    monkeypatch.setattr(compat, "detect_version", lambda: version)
    if scope == "user":
        assert cursor_user_cmd.uninstall(write=True, json_output=True) == 0
    else:
        assert (
            cli.main(
                [
                    "harness",
                    "uninstall",
                    "--target",
                    "cursor",
                    "--scope",
                    "user",
                    "--workspace",
                    str(workspace),
                    "--write",
                    "--json",
                ]
            )
            == 0
        )
    text = path.read_text()
    assert text.count(foreign) == 4
    assert all(entries == [json.loads(foreign)] for entries in json.loads(text)["hooks"].values())


def test_version_detection_isolates_binary_side_effects(tmp_path, monkeypatch):
    from types import SimpleNamespace

    set_home(monkeypatch, tmp_path / "home")
    profiles = []

    def binary(*args, **kwargs):
        root = Path(kwargs["env"]["HOME"])
        profiles.append(root)
        assert root != Path.home()
        (root / "probe-config").write_text("fixture")
        assert kwargs["env"]["NODE_DISABLE_COMPILE_CACHE"] == "1"
        return SimpleNamespace(returncode=0, stdout="2026.10.01-e373342\n")

    monkeypatch.setattr(compat.subprocess, "run", binary)
    assert compat.detect_version() == "2026.10.01-e373342"
    assert not Path.home().exists()
    assert not profiles[0].exists()


def test_mode_show_keeps_invalid_list_config_fail_safe(tmp_path, capsys):
    from brigade import cli

    (tmp_path / ".brigade").mkdir()
    (tmp_path / ".brigade/config.json").write_text("[]")
    assert cli.main(["work", "mode", "show", "--target", str(tmp_path), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "strict"
    assert report["observe_hooks"] == {"codex": False}
