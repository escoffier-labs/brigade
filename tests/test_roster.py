import pytest

from brigade import roster as roster_mod

VALID = """
orchestrator = "chef"

[agents.chef]
cli = "codex"
role = "plan and synthesize"

[agents.coder]
cli = "ollama:llama3.3"
role = "write code"
timeout_seconds = 120

[limits]
max_workers = 4
timeout_seconds = 300
allow_models = ["codex", "ollama:*"]
"""


def _write(tmp_path, text):
    path = tmp_path / "roster.toml"
    path.write_text(text)
    return path


def test_load_valid_roster(tmp_path):
    r = roster_mod.load_roster(_write(tmp_path, VALID))
    assert r.orchestrator == "chef"
    assert set(r.agents) == {"chef", "coder"}
    assert r.max_workers == 4
    assert r.timeout_seconds == 300.0
    assert r.agents["coder"].cli == "ollama:llama3.3"
    assert r.agents["coder"].timeout_seconds == 120.0
    assert roster_mod.timeout_for(r.agents["chef"], r) == 300.0
    assert roster_mod.timeout_for(r.agents["coder"], r) == 120.0
    assert roster_mod.is_cli_allowed("codex", r)
    assert roster_mod.is_cli_allowed("ollama:anything", r)
    assert not roster_mod.is_cli_allowed("claude", r)


def test_load_roster_fallback_parser(monkeypatch, tmp_path):
    monkeypatch.setattr(roster_mod, "tomllib", None)
    r = roster_mod.load_roster(_write(tmp_path, VALID))
    assert r.orchestrator == "chef"
    assert r.allow_models == ("codex", "ollama:*")


def test_workers_excludes_orchestrator(tmp_path):
    r = roster_mod.load_roster(_write(tmp_path, VALID))
    assert [agent.name for agent in roster_mod.workers(r)] == ["coder"]


def test_load_rejects_unknown_orchestrator(tmp_path):
    text = VALID.replace('orchestrator = "chef"', 'orchestrator = "missing"')
    with pytest.raises(ValueError, match="orchestrator"):
        roster_mod.load_roster(_write(tmp_path, text))


def test_load_rejects_unknown_cli(tmp_path):
    text = VALID.replace('cli = "ollama:llama3.3"', 'cli = "nope"')
    with pytest.raises(ValueError, match="unknown"):
        roster_mod.load_roster(_write(tmp_path, text))


def test_load_rejects_bad_limits(tmp_path):
    text = VALID.replace("max_workers = 4", "max_workers = 0")
    with pytest.raises(ValueError, match="positive"):
        roster_mod.load_roster(_write(tmp_path, text))


def test_load_rejects_bad_timeout(tmp_path):
    text = VALID.replace("timeout_seconds = 300", "timeout_seconds = 0")
    with pytest.raises(ValueError, match="timeout_seconds"):
        roster_mod.load_roster(_write(tmp_path, text))


def test_load_rejects_bad_agent_timeout(tmp_path):
    text = VALID.replace("timeout_seconds = 120", "timeout_seconds = -1")
    with pytest.raises(ValueError, match="agents.coder.timeout_seconds"):
        roster_mod.load_roster(_write(tmp_path, text))


def test_load_rejects_disallowed_model(tmp_path):
    text = VALID.replace('allow_models = ["codex", "ollama:*"]', 'allow_models = ["codex"]')
    with pytest.raises(ValueError, match="not allowed"):
        roster_mod.load_roster(_write(tmp_path, text))


def test_find_role_returns_matching_agent(tmp_path):
    r = roster_mod.load_roster(_write(tmp_path, VALID))
    assert r.find_role("write code").name == "coder"
    assert r.find_role("nope") is None


def test_researcher_agent_accepts_endpoint(tmp_path):
    text = (
        'orchestrator = "chef"\n'
        '[agents.chef]\ncli = "codex"\nrole = "plan"\n'
        '[agents.api]\nrole = "researcher"\nendpoint = "http://x/v1"\nmodel = "m"\n'
    )
    loaded = roster_mod.load_roster(_write(tmp_path, text))
    a = loaded.find_role("researcher")
    assert a is not None and a.endpoint == "http://x/v1" and a.model == "m"
    assert a.cli is None


def test_researcher_agent_accepts_headers(tmp_path):
    text = (
        'orchestrator = "chef"\n'
        '[agents.chef]\ncli = "codex"\nrole = "plan"\n'
        '[agents.api]\nrole = "researcher"\nendpoint = "http://x/v1"\nmodel = "m"\n'
        'headers = {"Authorization" = "Bearer t"}\n'
    )
    loaded = roster_mod.load_roster(_write(tmp_path, text))
    a = loaded.find_role("researcher")
    assert a.headers == {"Authorization": "Bearer t"}


def test_cli_agent_still_requires_cli_or_endpoint(tmp_path):
    text = (
        'orchestrator = "chef"\n'
        '[agents.chef]\nrole = "plan"\n'
    )
    with pytest.raises(ValueError):
        roster_mod.load_roster(_write(tmp_path, text))
