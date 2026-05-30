import json
import subprocess

from brigade import cli
from brigade import repos_cmd


def _init_git_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.email", "dev@example.invalid"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Dev"], cwd=path, check=True)


def test_repos_init_list_show_scan_doctor_json(tmp_path, capsys):
    _init_git_repo(tmp_path)
    (tmp_path / "AGENTS.md").write_text("local guidance\n")
    (tmp_path / "README.md").write_text("readme\n")
    (tmp_path / "CHANGELOG.md").write_text("changes\n")
    (tmp_path / "ROADMAP.md").write_text("roadmap\n")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = \"demo\"\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / ".claude" / "memory-handoffs").mkdir(parents=True)

    assert repos_cmd.init(target=tmp_path, json_output=True) == 0
    init_payload = json.loads(capsys.readouterr().out)
    assert init_payload["repo_count"] == 1

    assert repos_cmd.list_repos(target=tmp_path, json_output=True) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["repos"][0]["has_agents"] is True

    assert repos_cmd.show(target=tmp_path, repo_id="current", json_output=True) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["repo"]["guidance_source"] == "AGENTS.md"

    assert repos_cmd.scan(target=tmp_path, json_output=True) == 0
    scanned = json.loads(capsys.readouterr().out)
    assert scanned["repos"][0]["test_hints"]

    assert repos_cmd.doctor(target=tmp_path, json_output=True) == 0
    doctored = json.loads(capsys.readouterr().out)
    assert doctored["issue_count"] == 0


def test_repos_claude_fallback_detection_does_not_copy_contents(tmp_path, capsys):
    _init_git_repo(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("private setup detail should stay local\n")
    (tmp_path / ".brigade").mkdir()
    (tmp_path / ".claude" / "memory-handoffs").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text("[project]\nname = \"demo\"\n")
    assert repos_cmd.init(target=tmp_path) == 0
    capsys.readouterr()

    assert repos_cmd.scan(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["repos"][0]["guidance_source"] == "CLAUDE.md"
    rendered = json.dumps(payload)
    assert "private setup detail" not in rendered
    assert any(check["name"] == "repo_claude_fallback" for check in payload["issues"])


def test_repos_import_issues_dedupe_and_dismissed_until_changed(tmp_path, capsys):
    _init_git_repo(tmp_path)
    (tmp_path / ".brigade").mkdir()
    assert repos_cmd.init(target=tmp_path) == 0
    capsys.readouterr()

    assert repos_cmd.import_issues(target=tmp_path, json_output=True) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["created"] >= 1

    assert repos_cmd.import_issues(target=tmp_path, json_output=True) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["created"] == 0
    assert second["skipped"] >= 1

    imports_path = tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl"
    imports = [json.loads(line) for line in imports_path.read_text().splitlines()]
    imports[0]["status"] = "dismissed"
    imports_path.write_text("".join(json.dumps(item, sort_keys=True) + "\n" for item in imports))

    assert repos_cmd.import_issues(target=tmp_path, json_output=True) == 0
    third = json.loads(capsys.readouterr().out)
    assert third["created"] == 0
    assert third["dismissed"] >= 1


def test_repos_cli_dispatch(tmp_path, monkeypatch):
    seen = []

    def record(name):
        def _fake(**kwargs):
            seen.append((name, kwargs))
            return 0
        return _fake

    monkeypatch.setattr(repos_cmd, "init", record("init"))
    monkeypatch.setattr(repos_cmd, "list_repos", record("list"))
    monkeypatch.setattr(repos_cmd, "show", record("show"))
    monkeypatch.setattr(repos_cmd, "scan", record("scan"))
    monkeypatch.setattr(repos_cmd, "doctor", record("doctor"))
    monkeypatch.setattr(repos_cmd, "import_issues", record("import-issues"))

    assert cli.main(["repos", "init", "--target", str(tmp_path), "--force", "--no-gitignore", "--json"]) == 0
    assert cli.main(["repos", "list", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["repos", "show", "current", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["repos", "scan", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["repos", "doctor", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["repos", "import-issues", "--target", str(tmp_path), "--dry-run", "--json"]) == 0

    assert seen == [
        ("init", {"target": tmp_path, "force": True, "update_gitignore": False, "json_output": True}),
        ("list", {"target": tmp_path, "json_output": True}),
        ("show", {"target": tmp_path, "repo_id": "current", "json_output": True}),
        ("scan", {"target": tmp_path, "json_output": True}),
        ("doctor", {"target": tmp_path, "json_output": True}),
        ("import-issues", {"target": tmp_path, "dry_run": True, "json_output": True}),
    ]
