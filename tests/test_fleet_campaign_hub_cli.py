"""Hub-backed campaign CLI: create, campaigns and show, with a fake Worklore client."""

import json
import subprocess
from pathlib import Path

import pytest
from brigade import cli, worklore_client


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


def _repo(root: Path, name: str, origin: str | None) -> None:
    path = root / name
    path.mkdir()
    _git(path, "init", "-q")
    if origin:
        _git(path, "remote", "add", "origin", origin)


@pytest.fixture
def workspace(tmp_path):
    _repo(tmp_path, "one", "https://github.com/acme/one.git")
    _repo(tmp_path, "two", "https://github.com/acme/two.git")
    (tmp_path / ".brigade").mkdir()
    (tmp_path / ".brigade/repos.toml").write_text(
        '[[repo]]\nid="repo-one"\npath="one"\n[[repo]]\nid="repo-two"\npath="two"\n'
    )
    return tmp_path


class FakeHub:
    def __init__(self):
        self.posted = []
        self.reads = []
        self.shown = {"campaign_missing": True}
        self.settings = {"hub_url": "https://hub.invalid"}

    def create(self, body):
        self.posted.append(body)
        return {
            "campaign": {},
            "created": True,
            "added": [m["repo"] for m in body["members"]],
            "existing": [],
            "override_conflicts": [],
        }

    def get(self, key):
        self.reads.append(key)
        if self.shown.get("campaign_missing"):
            raise worklore_client.WorkloreClientError("fleet hub work failed: HTTP 404: not found", code="not-found")
        return self.shown


@pytest.fixture
def hub(monkeypatch):
    fake = FakeHub()
    monkeypatch.setattr(worklore_client, "create_campaign", fake.create)
    monkeypatch.setattr(worklore_client, "get_campaign", fake.get)
    monkeypatch.setattr(worklore_client, "load_fleet_settings", lambda: fake.settings)
    return fake


def _create(workspace, *extra):
    return cli.main(
        [
            "fleet",
            "campaign",
            "create",
            "--target",
            str(workspace),
            "--campaign-id",
            "camp-1",
            "--title",
            "Sweep",
            "--acceptance",
            "Done",
            *extra,
        ]
    )


def test_create_posts_canonical_identities(workspace, hub, capsys):
    (workspace / "desc.md").write_text("Shared spec.")
    (workspace / "ov.json").write_text(json.dumps({"repo-two": "special"}))
    code = _create(
        workspace, "--description-file", str(workspace / "desc.md"), "--overrides", str(workspace / "ov.json")
    )
    assert code == 0
    assert hub.posted == [
        {
            "campaign_key": "camp-1",
            "title": "Sweep",
            "acceptance": ["Done"],
            "description": "Shared spec.",
            "members": [{"repo": "acme/one"}, {"repo": "acme/two", "override": "special"}],
        }
    ]
    out = capsys.readouterr().out
    assert "created campaign camp-1: 2 added, 0 existing" in out and "added: acme/one" in out


def test_create_json_and_repos_subset(workspace, hub, capsys):
    assert _create(workspace, "--repos", "repo-two", "--json") == 0
    assert hub.posted[0]["members"] == [{"repo": "acme/two"}]
    assert json.loads(capsys.readouterr().out)["added"] == ["acme/two"]


def test_dry_run_never_posts_and_marks_existing(workspace, hub, capsys):
    hub.shown = {
        "campaign": {"spec_fingerprint": "0" * 64},
        "members": [{"repo": "acme/one"}],
    }
    assert _create(workspace, "--dry-run", "--json") == 0
    assert hub.posted == [] and hub.reads == ["camp-1"]
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True and payload["request"]["campaign_key"] == "camp-1"
    assert payload["members"] == [{"repo": "acme/one", "exists": True}, {"repo": "acme/two", "exists": False}]
    assert payload["hub"]["checked"] is True and payload["hub"]["spec_changed"] is True


def test_dry_run_reports_hub_failure_without_raising(workspace, hub, monkeypatch, capsys):
    def boom(key):
        raise worklore_client.FleetClientError("hub-unavailable\x1b[31m")

    monkeypatch.setattr(worklore_client, "get_campaign", boom)
    assert _create(workspace, "--dry-run") == 0
    out = capsys.readouterr().out
    assert "hub not checked: hub-unavailable" in out and "\x1b" not in out and hub.posted == []


def test_dry_run_without_hub_skips_the_read(workspace, hub, capsys):
    hub.settings = {}
    assert _create(workspace, "--dry-run", "--json") == 0
    assert hub.reads == []
    assert json.loads(capsys.readouterr().out)["hub"]["checked"] is False


def test_identity_collision_is_refused(workspace, hub, capsys):
    _repo(workspace, "twin", "https://github.com/acme/one.git")
    (workspace / ".brigade/repos.toml").write_text(
        '[[repo]]\nid="repo-one"\npath="one"\n[[repo]]\nid="repo-twin"\npath="twin"\n'
    )
    assert _create(workspace) == 2
    assert "repository_identity_collision" in capsys.readouterr().err
    assert hub.posted == []


def test_worktree_suffix_is_stripped_and_missing_identity_refused(workspace, hub, capsys):
    _git(
        workspace / "one", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "--allow-empty", "-qm", "x"
    )
    _git(workspace / "one", "worktree", "add", "-q", str(workspace / "linked"))
    (workspace / ".brigade/repos.toml").write_text('[[repo]]\nid="linked"\npath="linked"\n')
    assert _create(workspace) == 0
    assert hub.posted[0]["members"] == [{"repo": "acme/one"}]
    plain = workspace / "plain"
    plain.mkdir()
    (workspace / ".brigade/repos.toml").write_text('[[repo]]\nid="plain"\npath="plain"\n')
    assert _create(workspace) == 2
    assert "repository_identity_unresolved" in capsys.readouterr().err


def test_identity_without_an_owner_is_refused(workspace, hub, capsys):
    _repo(workspace, "solo", None)
    (workspace / ".brigade/repos.toml").write_text('[[repo]]\nid="solo"\npath="solo"\n')
    assert _create(workspace) == 2
    assert "repository_identity_unresolved" in capsys.readouterr().err
    assert hub.posted == []


def test_identity_is_lowercased(workspace, hub):
    _repo(workspace, "mixed", "https://github.com/Acme/Mixed.git")
    (workspace / ".brigade/repos.toml").write_text('[[repo]]\nid="mixed"\npath="mixed"\n')
    assert _create(workspace) == 0
    assert hub.posted[0]["members"] == [{"repo": "acme/mixed"}]


def test_unknown_override_and_bad_selection_are_refused(workspace, hub, capsys):
    (workspace / "ov.json").write_text(json.dumps({"nope": "x"}))
    assert _create(workspace, "--overrides", str(workspace / "ov.json")) == 2
    assert "override_for_unknown_repository" in capsys.readouterr().err
    assert _create(workspace, "--repos", "missing") == 2
    assert hub.posted == []


def test_hub_refusal_exits_nonzero_with_code(workspace, hub, monkeypatch, capsys):
    def refuse(body):
        raise worklore_client.WorkloreClientError("fleet hub work failed: HTTP 409: spec", code="campaign-spec-changed")

    monkeypatch.setattr(worklore_client, "create_campaign", refuse)
    assert _create(workspace) == 1
    assert "campaign-spec-changed" in capsys.readouterr().err
    assert _create(workspace, "--json") == 1
    assert json.loads(capsys.readouterr().err)["code"] == "campaign-spec-changed"


SUMMARY = {
    "total": 2,
    "unowned": 1,
    "in_flight": 1,
    "awaiting_gate": 0,
    "blocked": 3,
    "completed": 0,
    "canceled": 4,
    "archived": 5,
    "attempt_limited": 0,
    "drifted": 1,
    "done": False,
}


def test_campaigns_table_strips_controls(monkeypatch, capsys):
    payload = {"campaigns": [{"campaign_key": "camp-1", "title": "Sweep\x1b[31m red", "summary": SUMMARY}]}
    monkeypatch.setattr(worklore_client, "list_campaigns", lambda limit=None: payload)
    assert cli.main(["fleet", "campaigns", "--limit", "5"]) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out and "camp-1" in out and "Sweep" in out
    header = out.splitlines()[0]
    assert header.split()[:3] == ["KEY", "TOTAL", "UNOWNED"] and "BLOCKED" in header
    assert cli.main(["fleet", "campaigns", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == payload


def test_show_prints_summary_and_member_rows(monkeypatch, capsys):
    payload = {
        "campaign": {"campaign_key": "camp-1", "title": "Sweep", "summary": SUMMARY},
        "members": [
            {
                "repo": "acme/one",
                "bucket": "in_flight",
                "status": "running",
                "attempt_count": 1,
                "drifted": True,
                "owner_node": "node-a",
                "brigade": {"display_ref": "run-9\x1b"},
                "github": {"display_ref": "acme/one#4", "external_state": "open"},
            },
            {"repo": "acme/two", "bucket": "unowned", "status": "captured", "attempt_count": 0, "drifted": False},
        ],
    }
    monkeypatch.setattr(worklore_client, "get_campaign", lambda key: payload)
    assert cli.main(["fleet", "campaign", "show", "camp-1"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("camp-1: Sweep | total 2, unowned 1, in flight 1")
    assert "blocked 3," in lines[0] and "canceled 4," in lines[0] and "archived 5," in lines[0]
    assert (
        "acme/one  in_flight  running  attempts 1  owner node-a  run run-9  pr acme/one#4 (open)  drifted" in lines[1]
    )
    assert "owner -  run -  pr -  in-spec" in lines[2]
    assert "\x1b" not in "".join(lines)


def test_show_failure_exits_nonzero(monkeypatch, capsys):
    def missing(key):
        raise worklore_client.WorkloreClientError(
            "fleet hub work failed: HTTP 404: campaign not found", code="not-found"
        )

    monkeypatch.setattr(worklore_client, "get_campaign", missing)
    assert cli.main(["fleet", "campaign", "show", "nope"]) == 1
    assert "not-found" in capsys.readouterr().err


def test_show_and_list_survive_malformed_hub_values(monkeypatch, capsys):
    payload = {
        "campaign": {"campaign_key": "camp-1", "title": "T", "summary": ["bad"]},
        "members": [{"repo": "acme/one", "attempt_count": "9\x1b[31m", "bucket": "unowned"}, "junk"],
    }
    monkeypatch.setattr(worklore_client, "get_campaign", lambda key: payload)
    assert cli.main(["fleet", "campaign", "show", "camp-1"]) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out and "total 0," in out and "attempts 9 [31m" in out
    bad_list = {"campaigns": [{"campaign_key": "k", "title": "t", "summary": {"total": "5\x1b[1m", "unowned": True}}]}
    monkeypatch.setattr(worklore_client, "list_campaigns", lambda limit=None: bad_list)
    assert cli.main(["fleet", "campaigns"]) == 0
    assert "\x1b" not in capsys.readouterr().out
    monkeypatch.setattr(worklore_client, "get_campaign", lambda key: {"campaign": "x", "members": "y"})
    assert cli.main(["fleet", "campaign", "show", "camp-1"]) == 0
