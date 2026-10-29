"""Ownership and explicit activation regressions at the station boundary."""

import json

import pytest

from brigade import managed, tokens_cmd


@pytest.mark.parametrize("owner", ["tokenjuice", "token-glace", "unrelated", "missing", "fork", "malformed"])
def test_tokens_discovery_requires_upstream_package(monkeypatch, tmp_path, owner):
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


def test_add_preserves_upstream_development_owner_on_repeat(monkeypatch, tmp_path):
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
