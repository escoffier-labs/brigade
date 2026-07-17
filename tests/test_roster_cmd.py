from pathlib import Path

from brigade import agents
from brigade import cli
from brigade import roster
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
    assert 'cli = "ollama:llama3.2:3b"' in text
    assert "timeout_seconds = 600" in text
    assert str(path) in out


def test_default_roster_ollama_model_is_small_and_documented():
    # The starter roster must never name a model whose absence triggers a
    # multi-GB auto-pull (a 43GB llama3.3 default once filled a root disk).
    assert roster_cmd.DEFAULT_OLLAMA_MODEL == "llama3.2:3b"
    text = roster_cmd.default_roster_text()
    assert "llama3.3" not in text
    assert "never auto-pulls" in text


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


def test_roster_doctor_missing_file_fails(monkeypatch, tmp_target, tmp_path, capsys):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "empty-home")
    rc = roster_cmd.doctor(tmp_target)
    out = capsys.readouterr().out
    assert rc == 1
    assert "[fail]" in out
    assert "brigade roster init" in out


def test_roster_doctor_falls_back_to_home_roster(monkeypatch, tmp_target, tmp_path, capsys):
    home = tmp_path / "home"
    path = home / ".brigade" / "roster.toml"
    path.parent.mkdir(parents=True)
    path.write_text(
        'orchestrator = "chef"\n'
        "[agents.chef]\n"
        'endpoint = "https://example.test/v1/chat"\n'
        'model = "some-hosted-model"\n'
        'role = "plan"\n'
    )
    monkeypatch.setattr(Path, "home", lambda: home)

    assert roster_cmd.doctor(tmp_target) == 0
    assert str(path) in capsys.readouterr().out


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
    monkeypatch.setattr(agents, "ollama_model_present", lambda model: (True, ""))
    assert cli.main(["roster", "doctor", "--target", str(tmp_target)]) == 0
    out = capsys.readouterr().out
    assert "ollama:mistral" in out


def test_roster_cli_init_default_uses_small_ollama_model(tmp_target):
    assert cli.main(["roster", "init", "--target", str(tmp_target)]) == 0
    text = (tmp_target / ".brigade" / "roster.toml").read_text()
    assert f'cli = "ollama:{roster_cmd.DEFAULT_OLLAMA_MODEL}"' in text


def test_roster_doctor_warns_when_ollama_model_not_pulled(monkeypatch, tmp_target, capsys):
    roster_cmd.init(tmp_target)
    monkeypatch.setattr(agents.proc, "which", lambda cmd: "/x/" + cmd)
    monkeypatch.setattr(
        agents, "ollama_model_present", lambda model: (False, f"ollama model {model!r} is not pulled locally")
    )
    rc = roster_cmd.doctor(tmp_target)
    out = capsys.readouterr().out
    assert rc == 0
    assert "[warn]" in out
    assert "not pulled locally" in out


def test_roster_doctor_ok_when_ollama_model_pulled(monkeypatch, tmp_target, capsys):
    roster_cmd.init(tmp_target)
    monkeypatch.setattr(agents.proc, "which", lambda cmd: "/x/" + cmd)
    monkeypatch.setattr(agents, "ollama_model_present", lambda model: (True, ""))
    rc = roster_cmd.doctor(tmp_target)
    out = capsys.readouterr().out
    assert rc == 0
    assert "pulled locally" in out


def test_roster_doctor_fails_unauthenticated_acpx_cursor_with_recovery(monkeypatch, tmp_target, capsys):
    from brigade import acpx_adapter

    _write_roster(
        tmp_target,
        'orchestrator = "chef"\n'
        '[agents.chef]\ncli = "codex"\nrole = "plan"\n'
        '[agents.composer]\ncli = "cursor"\nmodel = "composer-2.5"\n'
        'transport = "acpx"\ntransport_version = "0.12.0"\nrole = "review"\n',
    )
    monkeypatch.setattr(agents.proc, "which", lambda cmd: f"/bin/{cmd}")
    monkeypatch.setattr(acpx_adapter, "installed_version", lambda: ("0.12.0", ""))
    diagnosis = (
        "cursor-agent CLI is not logged in; run `cursor-agent login` once, then verify with `cursor-agent status`"
    )
    monkeypatch.setattr(
        acpx_adapter,
        "cursor_auth_status",
        lambda: acpx_adapter.CursorAuthStatus("unauthenticated", diagnosis, "Not logged in", "", 0),
    )

    rc = roster_cmd.doctor(tmp_target)
    out = capsys.readouterr().out

    assert rc == 1
    assert "[fail]" in out
    assert "agent: composer cursor auth" in out
    assert diagnosis in out


def _write_roster(tmp_target, body: str) -> None:
    path = tmp_target / ".brigade" / "roster.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def test_roster_doctor_ok_for_supported_model_pin(monkeypatch, tmp_target, capsys):
    _write_roster(
        tmp_target,
        'orchestrator = "chef"\n[agents.chef]\ncli = "grok"\nmodel = "grok-composer-2.5-fast"\nrole = "plan"\n',
    )
    monkeypatch.setattr(agents.proc, "which", lambda cmd: "/x/" + cmd)
    rc = roster_cmd.doctor(tmp_target)
    out = capsys.readouterr().out
    assert rc == 0
    assert "grok-composer-2.5-fast via grok" in out


def test_roster_doctor_fails_pin_on_unsupported_cli(monkeypatch, tmp_target, capsys):
    _write_roster(
        tmp_target,
        'orchestrator = "chef"\n[agents.chef]\ncli = "goose"\nmodel = "whatever"\nrole = "plan"\n',
    )
    monkeypatch.setattr(agents.proc, "which", lambda cmd: "/x/" + cmd)
    rc = roster_cmd.doctor(tmp_target)
    out = capsys.readouterr().out
    assert rc == 1
    assert "[fail]" in out
    assert "does not support model pinning" in out


def test_roster_doctor_fails_pin_on_ollama_ref(monkeypatch, tmp_target, capsys):
    _write_roster(
        tmp_target,
        'orchestrator = "chef"\n[agents.chef]\ncli = "ollama:llama3.3"\nmodel = "mistral"\nrole = "plan"\n',
    )
    monkeypatch.setattr(agents.proc, "which", lambda cmd: "/x/" + cmd)
    monkeypatch.setattr(agents, "ollama_model_present", lambda model: (True, ""))
    rc = roster_cmd.doctor(tmp_target)
    out = capsys.readouterr().out
    assert rc == 1
    assert "ollama names its model in the cli ref" in out


def test_roster_doctor_endpoint_agent_skips_pin_check(tmp_target, capsys):
    _write_roster(
        tmp_target,
        'orchestrator = "chef"\n'
        "[agents.chef]\n"
        'endpoint = "https://example.test/v1/chat"\n'
        'model = "some-hosted-model"\n'
        'role = "plan"\n',
    )
    rc = roster_cmd.doctor(tmp_target)
    out = capsys.readouterr().out
    assert rc == 0
    assert "endpoint https://example.test/v1/chat" in out


def test_roster_init_review_model_adds_pinned_reviewer_seat(tmp_path):
    # Structural review independence (issue #125): the reviewer seat runs a
    # different model than the coder it checks.
    rc = roster_cmd.init(tmp_path, review_model="gpt-5.3-codex-spark")
    assert rc == 0
    text = (tmp_path / ".brigade" / "roster.toml").read_text()
    assert "[agents.reviewer]" in text
    assert 'model = "gpt-5.3-codex-spark"' in text
    # the generated roster stays loadable
    loaded = roster.load_roster(tmp_path / ".brigade" / "roster.toml")
    assert loaded.agents["reviewer"].model == "gpt-5.3-codex-spark"
    assert loaded.agents["reviewer"].cli == "codex"


def test_roster_init_without_review_model_has_no_reviewer_seat(tmp_path):
    assert roster_cmd.init(tmp_path) == 0
    assert "[agents.reviewer]" not in (tmp_path / ".brigade" / "roster.toml").read_text()


def test_roster_init_rejects_blank_review_model(tmp_path, capsys):
    assert roster_cmd.init(tmp_path, review_model="  ") == 2
    assert "review-model" in capsys.readouterr().err
