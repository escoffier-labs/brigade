from brigade import agents
from brigade import cli
from brigade import roster_cmd


def test_roster_init_writes_default_roster(tmp_target, capsys):
    rc = roster_cmd.init(tmp_target)
    out = capsys.readouterr().out
    path = tmp_target / ".brigade" / "roster.toml"
    assert rc == 0
    assert path.is_file()
    text = path.read_text()
    assert 'orchestrator = "chef"' in text
    assert 'cli = "codex"' in text
    assert 'cli = "ollama:llama3.3"' in text
    assert "timeout_seconds = 600" in text
    assert str(path) in out


def test_roster_template_documents_model_pinning(tmp_target):
    assert roster_cmd.init(tmp_target) == 0
    text = (tmp_target / ".brigade" / "roster.toml").read_text()
    assert '# model = "claude-fable-5"' in text
    assert '# model = "gpt-5.5"' in text


def test_roster_init_refuses_overwrite_without_force(tmp_target, capsys):
    assert roster_cmd.init(tmp_target) == 0
    assert roster_cmd.init(tmp_target) == 2
    assert "already exists" in capsys.readouterr().err


def test_roster_init_force_overwrites_with_options(tmp_target):
    assert roster_cmd.init(tmp_target) == 0
    assert roster_cmd.init(tmp_target, force=True, ollama_model="mistral", max_workers=2) == 0
    text = (tmp_target / ".brigade" / "roster.toml").read_text()
    assert 'cli = "ollama:mistral"' in text
    assert "max_workers = 2" in text


def test_roster_doctor_missing_file_fails(tmp_target, capsys):
    rc = roster_cmd.doctor(tmp_target)
    out = capsys.readouterr().out
    assert rc == 1
    assert "[fail]" in out
    assert "brigade roster init" in out


def test_roster_doctor_validates_agents(monkeypatch, tmp_target, capsys):
    roster_cmd.init(tmp_target)
    monkeypatch.setattr(agents.proc, "which", lambda cmd: "/x/" + cmd if cmd == "codex" else None)
    rc = roster_cmd.doctor(tmp_target)
    out = capsys.readouterr().out
    assert rc == 0
    assert "roster: orchestrator" in out
    assert "roster: timeout_seconds" in out
    assert "agent: chef" in out
    assert "timeout=600s" in out
    assert "agent: local_researcher" in out
    assert "ollama" in out
    assert "[warn]" in out


def test_roster_doctor_claude_missing_is_optional_warning(monkeypatch, tmp_target, capsys):
    path = tmp_target / ".brigade" / "roster.toml"
    path.parent.mkdir(parents=True)
    path.write_text(
        """
orchestrator = "chef"

[agents.chef]
cli = "claude"
role = "plan"
"""
    )
    monkeypatch.setattr(agents.proc, "which", lambda cmd: None)
    rc = roster_cmd.doctor(tmp_target)
    out = capsys.readouterr().out
    assert rc == 0
    assert "Claude is optional" in out


def test_roster_doctor_invalid_roster_fails(tmp_target, capsys):
    path = tmp_target / ".brigade" / "roster.toml"
    path.parent.mkdir(parents=True)
    path.write_text("orchestrator = ")
    rc = roster_cmd.doctor(tmp_target)
    out = capsys.readouterr().out
    assert rc == 1
    assert "invalid" in out


def test_roster_cli_init_and_doctor(monkeypatch, tmp_target, capsys):
    assert cli.main(["roster", "init", "--target", str(tmp_target), "--ollama-model", "mistral"]) == 0
    monkeypatch.setattr(agents.proc, "which", lambda cmd: "/x/" + cmd)
    assert cli.main(["roster", "doctor", "--target", str(tmp_target)]) == 0
    out = capsys.readouterr().out
    assert "ollama:mistral" in out
