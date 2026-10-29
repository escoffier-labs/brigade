"""Ownership and explicit activation regressions at the station boundary."""

import json
import re
import shlex
import sys

import pytest

from brigade import managed, tokens_cmd


@pytest.fixture
def posix_platform(monkeypatch):
    """Exercise POSIX ownership independently of the test runner's host OS."""
    monkeypatch.setattr(sys, "platform", "linux")


@pytest.mark.parametrize("owner", ["tokenjuice", "token-glace", "unrelated", "missing", "fork", "malformed"])
def test_tokens_discovery_requires_upstream_package(monkeypatch, tmp_path, owner, posix_platform):
    package = tmp_path / "package"
    executable = package / "dist" / "cli.js"
    executable.parent.mkdir(parents=True)
    executable.write_text("")
    if owner != "missing":
        (package / "package.json").write_text(
            json.dumps(
                {
                    "name": "tokenjuice" if owner == "fork" else owner,
                    "repository": {
                        "type": "git",
                        "url": "git+https://github.com/example/tokenjuice.git"
                        if owner == "fork"
                        else "git+https://github.com/vincentkoc/tokenjuice.git",
                    },
                }
            )
        )
    if owner == "malformed":
        (package / "package.json").write_text("{")
    alias = tmp_path / "tokenjuice"
    alias.symlink_to(executable)
    monkeypatch.setattr(tokens_cmd.proc, "which", lambda name: str(alias) if name == "tokenjuice" else None)
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return tokens_cmd.proc.Result(0, json.dumps({"status": "ok"}), "")

    monkeypatch.setattr(tokens_cmd.proc, "run", run)
    payload = tokens_cmd.status_payload(tmp_path)
    assert payload["tools"]["tokenjuice"]["installed"] is (owner == "tokenjuice")
    assert bool(calls) is (owner == "tokenjuice")
    assert all(args[1:] == ["doctor", "hooks", "--format", "json"] for args in calls)


def test_legacy_tool_request_resolves_upstream_without_activating(monkeypatch, tmp_path):
    tool = managed.resolve("token-glace")
    assert tool is not None
    assert tool.name == "tokenjuice"
    assert tool.install_command() == ["npm", "install", "-g", "tokenjuice@0.8.5"]
    monkeypatch.setattr(managed.proc, "run", lambda *a, **kw: pytest.fail("wire must not activate hooks"))
    from brigade.doctor import build_context

    assert tool.wire(build_context(tmp_path))


def test_add_preserves_upstream_development_owner_on_repeat(monkeypatch, tmp_path, posix_platform):
    from brigade import add

    package = tmp_path / "upstream"
    package.mkdir()
    executable = package / "tokenjuice"
    executable.write_text("")
    (package / "package.json").write_text(
        json.dumps(
            {
                "name": "tokenjuice",
                "version": "0.8.5-dev",
                "repository": "https://github.com/vincentkoc/tokenjuice",
            }
        )
    )
    monkeypatch.setattr(managed.proc, "which", lambda name: str(executable) if name == "tokenjuice" else None)
    monkeypatch.setattr(managed.proc, "run", lambda *a, **kw: pytest.fail("must preserve existing owner and hooks"))
    for _ in range(2):
        assert add.run(tmp_path, "tokenjuice") == 0
    assert not (tmp_path / ".brigade").exists()


def test_unverified_compactor_is_not_hidden_by_usage_tracker(monkeypatch, tmp_path):
    monkeypatch.setattr(managed, "tokenjuice_binary", lambda: None)
    monkeypatch.setattr(managed.proc, "which", lambda name: "/example/" + name)
    monkeypatch.setattr(managed.proc, "run", lambda *a, **kw: managed.proc.Result(0, "{}", ""))
    payload = tokens_cmd.status_payload(tmp_path)
    assert payload["health"] == "warn"
    assert "ownership unverified" in payload["summary"]


@pytest.mark.parametrize("occupied", ["tokenjuice", "token-glace", "both"])
def test_add_refuses_occupied_alias_before_install(monkeypatch, tmp_path, occupied, capsys, posix_platform):
    from brigade import add

    monkeypatch.setattr(managed, "tokenjuice_binary", lambda: None)
    names = {"tokenjuice", "token-glace"} if occupied == "both" else {occupied}
    monkeypatch.setattr(managed.proc, "which", lambda name: "/owned/" + name if name in names else None)
    monkeypatch.setattr(managed.proc, "run", lambda *a, **kw: pytest.fail("must not install over an existing owner"))
    assert add.run(tmp_path, "tokenjuice") == 1
    assert "migration" in capsys.readouterr().err


def test_add_requires_post_install_package_qualification(monkeypatch, tmp_path, capsys, posix_platform):
    from brigade import add

    monkeypatch.setattr(managed, "tokenjuice_binary", lambda: None)
    monkeypatch.setattr(managed.proc, "which", lambda name: None)
    calls = []
    monkeypatch.setattr(managed.proc, "run", lambda args, **kw: calls.append(args) or managed.proc.Result(0, "", ""))
    assert add.run(tmp_path, "tokenjuice") == 1
    assert calls == [["npm", "install", "-g", "tokenjuice@0.8.5"]]
    assert "qualification" in capsys.readouterr().err


def test_windows_station_requires_manual_qualification(monkeypatch, tmp_path, capsys):
    from brigade import add

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(managed.proc, "which", lambda name: None)
    monkeypatch.setattr(managed.proc, "run", lambda *a, **kw: pytest.fail("unqualified Windows install must not run"))
    assert add.run(tmp_path, "tokenjuice") == 1
    assert "Windows" in capsys.readouterr().err


def test_qualified_execution_binds_resolved_entrypoint(monkeypatch, tmp_path, posix_platform):
    package = tmp_path / "upstream"
    package.mkdir()
    executable = package / "cli.js"
    executable.write_text("")
    (package / "package.json").write_text(
        json.dumps({"name": "tokenjuice", "repository": "https://github.com/vincentkoc/tokenjuice"})
    )
    alias = tmp_path / "tokenjuice"
    alias.symlink_to(executable)
    monkeypatch.setattr(managed.proc, "which", lambda name: str(alias) if name == "tokenjuice" else None)
    selected = managed.tokenjuice_binary()
    alias.unlink()
    alias.symlink_to(tmp_path / "different-owner")
    assert selected == str(executable)


def test_tokenjuice_identity_label_is_metadata_only(monkeypatch, tmp_path):
    monkeypatch.setattr(managed, "tokenjuice_binary", lambda: "/qualified/cli.js")
    monkeypatch.setattr(managed.proc, "which", lambda name: "/alias/tokenjuice" if name == "tokenjuice" else None)
    monkeypatch.setattr(managed.proc, "run", lambda *a, **kw: managed.proc.Result(0, '{"status":"ok"}', ""))
    tool = tokens_cmd.status_payload(tmp_path)["tools"]["tokenjuice"]
    assert tool["identity"] == "package-metadata-match"
    assert tool["provenance"] == "unverified"


@pytest.mark.parametrize("qualified", [None, "/qualified/upstream cli.js"])
def test_wire_plan_binds_only_qualified_owner(monkeypatch, tmp_path, qualified):
    monkeypatch.setattr(managed, "tokenjuice_binary", lambda: qualified)
    monkeypatch.setattr(managed.proc, "which", lambda name: "/old/tokenjuice" if name == "tokenjuice" else None)
    plan = tokens_cmd.wire_plan_payload(target=tmp_path)
    installs = [argv for argv in plan["commands"] if "install" in argv]
    assert len(installs) == (2 if qualified else 0)
    assert all(argv[0] == qualified for argv in installs)
    assert all(argv[0] != "tokenjuice" for argv in plan["commands"])


def test_add_failed_install_never_wires(monkeypatch, tmp_path, capsys, posix_platform):
    from dataclasses import replace
    from brigade import add

    original = managed.resolve("tokenjuice")
    assert original is not None
    tool = replace(original, wire=lambda ctx: pytest.fail("failed install must not wire"))
    monkeypatch.setattr(managed, "resolve", lambda name: tool)
    monkeypatch.setattr(managed, "tokenjuice_binary", lambda: None)
    monkeypatch.setattr(managed.proc, "which", lambda name: None)
    monkeypatch.setattr(managed.proc, "run", lambda *a, **kw: managed.proc.Result(1, "", "synthetic install failure"))
    assert add.run(tmp_path, "tokenjuice") == 1
    assert "[fail]" in capsys.readouterr().err


@pytest.mark.parametrize("entrypoint", ["/qualified/upstream cli.js", "/qualified/space;$(echo unwanted)/cli.js"])
def test_rendered_wire_plan_preserves_argv(monkeypatch, tmp_path, entrypoint):
    monkeypatch.setattr(managed, "tokenjuice_binary", lambda: entrypoint)
    plan = tokens_cmd.wire_plan_payload(target=tmp_path)
    rendered = tokens_cmd.health.render_plan_md("tokens wire plan", plan)
    blocks = re.findall(r"```sh\n(.*?)\n```", rendered, re.DOTALL)
    assert [shlex.split(block) for block in blocks] == plan["commands"]


@pytest.mark.parametrize("legacy_only", [False, True])
def test_occupied_alias_guidance_requires_cutover(monkeypatch, tmp_path, legacy_only):
    monkeypatch.setattr(managed, "tokenjuice_binary", lambda: None)
    name = "token-glace" if legacy_only else "tokenjuice"
    monkeypatch.setattr(managed.proc, "which", lambda candidate: "/owned/" + name if candidate == name else None)
    for payload in [tokens_cmd.status_payload(tmp_path), tokens_cmd.wire_plan_payload(target=tmp_path)]:
        assert "brigade add tokens" not in payload["next_commands"]
        assert "cutover" in " ".join(payload["next_commands"]).lower()
