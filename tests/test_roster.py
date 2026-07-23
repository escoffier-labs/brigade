import json
from pathlib import Path

import pytest

from brigade import agents
from brigade import cli
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
    assert r.sandbox is None
    assert r.timeout_seconds == 300.0
    assert r.agents["coder"].cli == "ollama:llama3.3"
    assert r.agents["coder"].timeout_seconds == 120.0
    assert roster_mod.timeout_for(r.agents["chef"], r) == 300.0
    assert roster_mod.timeout_for(r.agents["coder"], r) == 120.0
    assert roster_mod.is_cli_allowed("codex", r)
    assert roster_mod.is_cli_allowed("ollama:anything", r)
    assert not roster_mod.is_cli_allowed("claude", r)


def test_read_only_capability_defaults_true_and_accepts_boolean(tmp_path):
    loaded = roster_mod.load_roster(_write(tmp_path, VALID))
    assert loaded.agents["coder"].read_only_capable is True

    incapable = VALID.replace(
        'role = "write code"',
        'role = "write code"\nread_only_capable = false',
    )
    loaded = roster_mod.load_roster(_write(tmp_path, incapable))
    assert loaded.agents["coder"].read_only_capable is False


@pytest.mark.parametrize("value", ['"false"', "0", "[]"])
def test_read_only_capability_rejects_non_booleans(tmp_path, value):
    invalid = VALID.replace(
        'role = "write code"',
        f'role = "write code"\nread_only_capable = {value}',
    )

    with pytest.raises(ValueError, match=r"agents\.coder\.read_only_capable must be a boolean"):
        roster_mod.load_roster(_write(tmp_path, invalid))


def test_load_roster_without_resolution_does_not_invent_source(tmp_path):
    loaded = roster_mod.load_roster(_write(tmp_path, VALID))

    assert loaded.resolution is None


def test_load_roster_fallback_parser(monkeypatch, tmp_path):
    # Force the pure-Python TOML fallback (the Python 3.10 path, where tomllib
    # is absent) and confirm a real roster still loads through it.
    monkeypatch.setattr(roster_mod.toml_compat, "_stdlib_tomllib", None)
    r = roster_mod.load_roster(_write(tmp_path, VALID))
    assert r.orchestrator == "chef"
    assert r.allow_models == ("codex", "ollama:*")


def test_resolve_roster_path_prefers_workspace(monkeypatch, tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    local = workspace / ".brigade" / "roster.toml"
    user = home / ".brigade" / "roster.toml"
    local.parent.mkdir(parents=True)
    user.parent.mkdir(parents=True)
    local.write_text(VALID)
    user.write_text(VALID)
    monkeypatch.setattr(Path, "home", lambda: home)
    assert roster_mod.resolve_roster_path(workspace) == local.resolve()


def test_resolve_roster_reports_workspace_shadowing_user_roster(monkeypatch, tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    local = workspace / ".brigade" / "roster.toml"
    user = home / ".brigade" / "roster.toml"
    local.parent.mkdir(parents=True)
    user.parent.mkdir(parents=True)
    local.write_text(VALID)
    user.write_text(VALID)
    monkeypatch.setattr(Path, "home", lambda: home)

    resolution = roster_mod.resolve_roster(workspace)

    assert resolution.path == local.resolve()
    assert resolution.source == "workspace"
    assert resolution.shadowed == (user.resolve(),)


def test_resolve_roster_does_not_shadow_same_file_through_workspace_symlink(monkeypatch, tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    user = home / ".brigade" / "roster.toml"
    user.parent.mkdir(parents=True)
    user.write_text(VALID)
    workspace.mkdir()
    (workspace / ".brigade").symlink_to(user.parent, target_is_directory=True)
    monkeypatch.setattr(Path, "home", lambda: home)

    resolution = roster_mod.resolve_roster(workspace)

    assert resolution.path == user.resolve()
    assert resolution.source == "workspace"
    assert resolution.shadowed == ()


def test_resolve_roster_explicit_choice_has_no_implicit_shadow(monkeypatch, tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    local = workspace / ".brigade" / "roster.toml"
    user = home / ".brigade" / "roster.toml"
    explicit = tmp_path / "chosen.toml"
    local.parent.mkdir(parents=True)
    user.parent.mkdir(parents=True)
    local.write_text(VALID)
    user.write_text(VALID)
    explicit.write_text(VALID)
    monkeypatch.setattr(Path, "home", lambda: home)

    resolution = roster_mod.resolve_roster(workspace, explicit)

    assert resolution.path == explicit.resolve()
    assert resolution.source == "explicit"
    assert resolution.shadowed == ()


def test_resolve_roster_path_uses_user_fallback(monkeypatch, tmp_path):
    home = tmp_path / "home"
    user = home / ".brigade" / "roster.toml"
    user.parent.mkdir(parents=True)
    user.write_text(VALID)
    monkeypatch.setattr(Path, "home", lambda: home)
    assert roster_mod.resolve_roster_path(tmp_path / "workspace") == user.resolve()


def test_resolve_roster_reports_user_fallback_source(monkeypatch, tmp_path):
    home = tmp_path / "home"
    user = home / ".brigade" / "roster.toml"
    user.parent.mkdir(parents=True)
    user.write_text(VALID)
    monkeypatch.setattr(Path, "home", lambda: home)

    resolution = roster_mod.resolve_roster(tmp_path / "workspace")

    assert resolution.path == user.resolve()
    assert resolution.source == "user"
    assert resolution.shadowed == ()


def test_resolve_roster_path_explicit_never_falls_back(monkeypatch, tmp_path):
    home = tmp_path / "home"
    user = home / ".brigade" / "roster.toml"
    user.parent.mkdir(parents=True)
    user.write_text(VALID)
    missing = tmp_path / "missing.toml"
    monkeypatch.setattr(Path, "home", lambda: home)
    with pytest.raises(FileNotFoundError, match=str(missing)):
        roster_mod.resolve_roster_path(tmp_path / "workspace", missing)


def test_resolve_roster_path_missing_names_both_candidates(monkeypatch, tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    monkeypatch.setattr(Path, "home", lambda: home)
    with pytest.raises(FileNotFoundError) as exc:
        roster_mod.resolve_roster_path(workspace)
    message = str(exc.value)
    assert str(workspace / ".brigade" / "roster.toml") in message
    assert str(home / ".brigade" / "roster.toml") in message


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


def test_load_accepts_valid_sandbox_limits(tmp_path):
    for sandbox in ("read-only", "workspace-write", "danger-full-access"):
        text = VALID.replace("[limits]\n", f'[limits]\nsandbox = "{sandbox}"\n')
        assert roster_mod.load_roster(_write(tmp_path, text)).sandbox == sandbox


def test_load_rejects_invalid_sandbox_limit(tmp_path):
    text = VALID.replace("[limits]\n", '[limits]\nsandbox = "none"\n')
    with pytest.raises(ValueError) as exc:
        roster_mod.load_roster(_write(tmp_path, text))

    message = str(exc.value)
    assert "limits.sandbox" in message
    assert "read-only" in message
    assert "workspace-write" in message
    assert "danger-full-access" in message


def test_roster_doctor_prints_sandbox_info(tmp_path, capsys):
    text = VALID.replace("[limits]\n", '[limits]\nsandbox = "workspace-write"\n')
    _write(tmp_path, text)

    assert cli.main(["roster", "doctor", "--roster", str(tmp_path / "roster.toml")]) == 0
    out = capsys.readouterr().out
    assert "[info]" in out
    assert "roster: sandbox" in out
    assert "workspace-write" in out


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


def test_researcher_headers_via_fallback_parser(monkeypatch, tmp_path):
    # Regression: the Python 3.10 fallback must parse the inline-table headers
    # value. Force the fallback even on 3.11+ so this is guarded everywhere.
    monkeypatch.setattr(roster_mod.toml_compat, "_stdlib_tomllib", None)
    text = (
        'orchestrator = "chef"\n'
        '[agents.chef]\ncli = "codex"\nrole = "plan"\n'
        '[agents.api]\nrole = "researcher"\nendpoint = "http://x/v1"\nmodel = "m"\n'
        'headers = {"Authorization" = "Bearer t"}\n'
    )
    loaded = roster_mod.load_roster(_write(tmp_path, text))
    assert loaded.find_role("researcher").headers == {"Authorization": "Bearer t"}


def test_cli_agent_accepts_model_pin(tmp_path):
    text = (
        'orchestrator = "architect"\n'
        '[agents.architect]\ncli = "claude"\nmodel = "claude-fable-5"\nrole = "plan"\n'
        '[agents.builder]\ncli = "codex"\nmodel = "gpt-5.5-codex"\nrole = "build"\n'
    )
    loaded = roster_mod.load_roster(_write(tmp_path, text))
    assert loaded.agents["architect"].cli == "claude"
    assert loaded.agents["architect"].model == "claude-fable-5"
    assert loaded.agents["builder"].model == "gpt-5.5-codex"


def test_cli_agent_accepts_reasoning_pin(tmp_path):
    text = (
        'orchestrator = "chef"\n'
        '[agents.chef]\ncli = "opencode"\nmodel = "openai/gpt-5.6"\nreasoning = "max"\nrole = "plan"\n'
    )
    loaded = roster_mod.load_roster(_write(tmp_path, text))
    assert loaded.agents["chef"].reasoning == "max"


def test_cursor_worker_accepts_pinned_acpx_transport(tmp_path):
    text = (
        'orchestrator = "chef"\n'
        '[agents.chef]\ncli = "codex"\nrole = "plan"\n'
        '[agents.composer]\ncli = "cursor"\nmodel = "composer-2.5"\n'
        'transport = "acpx"\ntransport_version = "0.12.0"\nrole = "build"\n'
    )
    loaded = roster_mod.load_roster(_write(tmp_path, text))
    assert loaded.agents["composer"].transport == "acpx"
    assert loaded.agents["composer"].transport_version == "0.12.0"


def test_acpx_transport_rejects_wrong_cli_or_unreviewed_version(tmp_path):
    wrong_cli = (
        'orchestrator = "chef"\n'
        '[agents.chef]\ncli = "codex"\nrole = "plan"\n'
        '[agents.worker]\ncli = "grok"\nmodel = "grok-4.5"\n'
        'transport = "acpx"\ntransport_version = "0.12.0"\nrole = "build"\n'
    )
    with pytest.raises(ValueError, match="cursor"):
        roster_mod.load_roster(_write(tmp_path, wrong_cli))

    wrong_version = wrong_cli.replace('cli = "grok"', 'cli = "cursor"').replace("0.12.0", "0.13.0")
    with pytest.raises(ValueError, match="reviewed version"):
        roster_mod.load_roster(_write(tmp_path, wrong_version))


def test_cli_agent_still_requires_cli_or_endpoint(tmp_path):
    text = 'orchestrator = "chef"\n[agents.chef]\nrole = "plan"\n'
    with pytest.raises(ValueError):
        roster_mod.load_roster(_write(tmp_path, text))


def test_codex_transport_defaults_to_exec(tmp_path):
    p = tmp_path / "roster.toml"
    p.write_text('orchestrator = "chef"\n\n[agents.chef]\ncli = "codex"\nrole = "plan"\n')
    assert roster_mod.load_roster(p).codex_transport == "exec"


def test_codex_transport_accepts_app_server(tmp_path):
    p = tmp_path / "roster.toml"
    p.write_text(
        'orchestrator = "chef"\ncodex_transport = "app-server"\n\n[agents.chef]\ncli = "codex"\nrole = "plan"\n'
    )
    assert roster_mod.load_roster(p).codex_transport == "app-server"


def test_codex_transport_rejects_unknown(tmp_path):
    p = tmp_path / "roster.toml"
    p.write_text('orchestrator = "chef"\ncodex_transport = "daemon"\n\n[agents.chef]\ncli = "codex"\nrole = "plan"\n')
    with pytest.raises(ValueError, match="codex_transport"):
        roster_mod.load_roster(p)


ENV_SEAT = """
orchestrator = "chef"

[agents.chef]
cli = "codex"
role = "plan and synthesize"

[agents.k3]
cli = "claude"
model = "kimi-k3"
role = "open-weight worker"
env = { ANTHROPIC_BASE_URL = "https://api.example.com/anthropic", ANTHROPIC_AUTH_TOKEN_REF = "KIMI_API_KEY", CLAUDE_CONFIG_DIR = "/tmp/claudex-config" }

[limits]
allow_models = ["codex", "claude"]
"""


def test_env_table_parses_onto_agent(tmp_path):
    r = roster_mod.load_roster(_write(tmp_path, ENV_SEAT))
    assert r.agents["k3"].env == {
        "ANTHROPIC_BASE_URL": "https://api.example.com/anthropic",
        "ANTHROPIC_AUTH_TOKEN_REF": "KIMI_API_KEY",
        "CLAUDE_CONFIG_DIR": "/tmp/claudex-config",
    }
    assert r.agents["chef"].env is None


def test_env_rejects_non_table(tmp_path):
    bad = ENV_SEAT.replace(
        'env = { ANTHROPIC_BASE_URL = "https://api.example.com/anthropic", ANTHROPIC_AUTH_TOKEN_REF = "KIMI_API_KEY", CLAUDE_CONFIG_DIR = "/tmp/claudex-config" }',
        'env = "ANTHROPIC_BASE_URL=https://api.example.com"',
    )
    with pytest.raises(ValueError, match="agents.k3.env must be a TOML table"):
        roster_mod.load_roster(_write(tmp_path, bad))


def test_env_rejects_non_string_values(tmp_path):
    bad = ENV_SEAT.replace(
        '"/tmp/claudex-config" }',
        '"/tmp/claudex-config", RETRIES = 3 }',
    )
    with pytest.raises(ValueError, match="agents.k3.env.RETRIES must be a string"):
        roster_mod.load_roster(_write(tmp_path, bad))


def test_env_rejects_invalid_variable_names(tmp_path):
    bad = ENV_SEAT.replace("CLAUDE_CONFIG_DIR", "claude-config-dir")
    with pytest.raises(ValueError, match="not a valid environment variable name"):
        roster_mod.load_roster(_write(tmp_path, bad))


def test_env_rejects_inline_secret_values(tmp_path):
    bad = ENV_SEAT.replace('ANTHROPIC_AUTH_TOKEN_REF = "KIMI_API_KEY"', 'ANTHROPIC_AUTH_TOKEN = "sk-live-secret"')
    with pytest.raises(ValueError, match="looks like a secret; pass it by reference"):
        roster_mod.load_roster(_write(tmp_path, bad))


def test_env_ref_value_must_be_variable_name(tmp_path):
    bad = ENV_SEAT.replace('ANTHROPIC_AUTH_TOKEN_REF = "KIMI_API_KEY"', 'ANTHROPIC_AUTH_TOKEN_REF = "sk-live-inline"')
    with pytest.raises(ValueError, match="must name an environment variable"):
        roster_mod.load_roster(_write(tmp_path, bad))


def test_env_ref_accepts_absolute_environment_file_reference(tmp_path):
    environment_file = tmp_path / "runtime.env"
    roster = ENV_SEAT.replace(
        'ANTHROPIC_AUTH_TOKEN_REF = "KIMI_API_KEY"',
        f'ANTHROPIC_AUTH_TOKEN_REF = "env-file:{environment_file}#CLIPROXY_API_KEY"',
    )
    assert roster_mod.load_roster(_write(tmp_path, roster)).agents["k3"].env is not None


@pytest.mark.parametrize(
    "reference",
    (
        "env-file:relative.env#CLIPROXY_API_KEY",
        "env-file:/runtime.env#",
        "env-file:/runtime.env#not_a_variable",
    ),
)
def test_env_ref_rejects_malformed_environment_file_reference(tmp_path, reference):
    bad = ENV_SEAT.replace('ANTHROPIC_AUTH_TOKEN_REF = "KIMI_API_KEY"', f'ANTHROPIC_AUTH_TOKEN_REF = "{reference}"')
    with pytest.raises(ValueError, match="env-file:/absolute/path#VARIABLE"):
        roster_mod.load_roster(_write(tmp_path, bad))


def test_env_rejected_on_acpx_transport(tmp_path):
    bad = ENV_SEAT.replace(
        'cli = "claude"\nmodel = "kimi-k3"',
        'cli = "cursor"\nmodel = "kimi-k3"\ntransport = "acpx"\ntransport_version = "0.12.0"',
    ).replace('allow_models = ["codex", "claude"]', 'allow_models = ["codex", "cursor"]')
    with pytest.raises(ValueError, match="env overrides support direct CLI seats only"):
        roster_mod.load_roster(_write(tmp_path, bad))


def test_env_rejected_on_codex_cloud_seat(tmp_path):
    bad = ENV_SEAT.replace('cli = "claude"\nmodel = "kimi-k3"\n', 'cli = "codex-cloud:brigade"\n').replace(
        'allow_models = ["codex", "claude"]', 'allow_models = ["codex", "codex-cloud:*"]'
    )
    with pytest.raises(ValueError, match="env overrides support direct CLI seats only"):
        roster_mod.load_roster(_write(tmp_path, bad))


def test_env_rejects_ref_and_inline_collision(tmp_path):
    bad = ENV_SEAT.replace(
        'CLAUDE_CONFIG_DIR = "/tmp/claudex-config"',
        'CLAUDE_CONFIG_DIR = "/tmp/claudex-config", ANTHROPIC_BASE_URL_REF = "OTHER_VAR"',
    )
    with pytest.raises(ValueError, match="collides with"):
        roster_mod.load_roster(_write(tmp_path, bad))


def test_env_secret_hints_cover_common_credential_names(tmp_path):
    for name in ("GH_PAT", "DB_PASSWD", "SESSION_COOKIE", "ANTHROPIC_AUTH", "MY_CREDENTIAL", "API_BEARER"):
        bad = ENV_SEAT.replace("CLAUDE_CONFIG_DIR", name)
        with pytest.raises(ValueError, match="looks like a secret"):
            roster_mod.load_roster(_write(tmp_path, bad))


def test_env_rejects_secret_shaped_inline_values(tmp_path):
    bad = ENV_SEAT.replace('"/tmp/claudex-config"', '"sk-live-abc123"')
    with pytest.raises(ValueError, match="looks like a secret value"):
        roster_mod.load_roster(_write(tmp_path, bad))


def test_env_allows_path_and_nonsecret_names(tmp_path):
    ok = ENV_SEAT.replace("CLAUDE_CONFIG_DIR", "PATH")
    r = roster_mod.load_roster(_write(tmp_path, ok))
    assert r.agents["k3"].env["PATH"] == "/tmp/claudex-config"


def test_env_rejected_on_codex_seat_under_appserver_transport(tmp_path):
    bad = ENV_SEAT.replace('cli = "claude"\nmodel = "kimi-k3"', 'cli = "codex"').replace(
        'orchestrator = "chef"', 'orchestrator = "chef"\ncodex_transport = "app-server"'
    )
    with pytest.raises(ValueError, match="codex_transport"):
        roster_mod.load_roster(_write(tmp_path, bad))


def test_env_rejected_on_endpoint_only_seat(tmp_path):
    bad = ENV_SEAT.replace(
        'cli = "claude"\nmodel = "kimi-k3"',
        'endpoint = "https://api.example.com"\nmodel = "kimi-k3"',
    )
    with pytest.raises(ValueError, match="direct CLI seats only"):
        roster_mod.load_roster(_write(tmp_path, bad))


def test_env_empty_table_loads_as_none(tmp_path):
    bad = ENV_SEAT.replace(
        'env = { ANTHROPIC_BASE_URL = "https://api.example.com/anthropic", ANTHROPIC_AUTH_TOKEN_REF = "KIMI_API_KEY", CLAUDE_CONFIG_DIR = "/tmp/claudex-config" }',
        "env = { }",
    )
    r = roster_mod.load_roster(_write(tmp_path, bad))
    assert r.agents["k3"].env is None


GROK_FALLBACK_ROSTER = """
orchestrator = "chef"

[agents.chef]
cli = "codex"
role = "plan"

[agents.grok-review]
cli = "grok"
model = "grok-4.5"
reasoning = "high"
role = "review"
invalid_final_fallback = "cursor-grok"

[agents.cursor-grok]
cli = "cursor"
model = "grok-4.5"
transport = "acpx"
transport_version = "0.12.0"
role = "fallback review"
"""


def test_grok_invalid_final_fallback_names_reviewed_acpx_seat(tmp_path):
    loaded = roster_mod.load_roster(_write(tmp_path, GROK_FALLBACK_ROSTER))

    assert loaded.agents["grok-review"].invalid_final_fallback == "cursor-grok"


@pytest.mark.parametrize(
    ("roster_text", "match"),
    [
        (
            GROK_FALLBACK_ROSTER.replace(
                'invalid_final_fallback = "cursor-grok"', 'invalid_final_fallback = "missing"'
            ),
            "is not defined",
        ),
        (
            GROK_FALLBACK_ROSTER.replace('cli = "grok"\nmodel = "grok-4.5"', 'cli = "codex"\nmodel = "gpt-5.6"'),
            "direct grok seat",
        ),
        (
            GROK_FALLBACK_ROSTER.replace('transport = "acpx"\ntransport_version = "0.12.0"\n', ""),
            "reviewed cursor-grok acpx seat",
        ),
        (
            GROK_FALLBACK_ROSTER.replace(
                'model = "grok-4.5"\ntransport = "acpx"', 'model = "composer-2.5"\ntransport = "acpx"'
            ),
            "grok model",
        ),
        (
            GROK_FALLBACK_ROSTER.replace('transport_version = "0.12.0"', 'transport_version = "0.11.0"'),
            "reviewed version is 0.12.0",
        ),
    ],
)
def test_grok_invalid_final_fallback_rejects_unreviewed_routes(tmp_path, roster_text, match):
    with pytest.raises(ValueError, match=match):
        roster_mod.load_roster(_write(tmp_path, roster_text))


METADATA_ROSTER = """
orchestrator = "chef"

[agents.chef]
cli = "codex"
role = "plan"
purpose = "Orchestrate the run."
requires = { cli = "codex" }
fallback = ["coder"]
stats = { speed = "fast", source = "author-receipts-2026-07" }
caveats = ["Needs a Codex subscription."]

[agents.coder]
cli = "ollama:llama3.2:3b"
role = "build"
purpose = "Fallback builder."
requires = { cli = "ollama" }
stats = { speed = "slow", source = "author-receipts-2026-07" }
caveats = []
"""


def test_legacy_roster_gets_empty_metadata_defaults(tmp_path):
    loaded = roster_mod.load_roster(_write(tmp_path, VALID))

    agent = loaded.agents["chef"]
    assert agent.purpose is None
    assert agent.requires is None
    assert agent.fallback == ()
    assert agent.stats is None
    assert agent.caveats == ()


def test_metadata_roster_parses_preset_fields(tmp_path):
    loaded = roster_mod.load_roster(_write(tmp_path, METADATA_ROSTER))
    chef = loaded.agents["chef"]

    assert chef.purpose == "Orchestrate the run."
    assert chef.requires == {"cli": "codex"}
    assert chef.fallback == ("coder",)
    assert chef.stats == {"speed": "fast", "source": "author-receipts-2026-07"}
    assert chef.caveats == ("Needs a Codex subscription.",)


def test_metadata_roster_rejects_unknown_requirement_keys(tmp_path):
    invalid = METADATA_ROSTER.replace('requires = { cli = "codex" }', 'requires = { cli = "codex", lane = "fast" }')
    with pytest.raises(ValueError, match=r"agents\.chef\.requires"):
        roster_mod.load_roster(_write(tmp_path, invalid))


def test_metadata_roster_rejects_unknown_auth_requirement(tmp_path):
    invalid = METADATA_ROSTER.replace(
        'requires = { cli = "codex" }',
        'requires = { cli = "codex", auth = "api-key" }',
    )
    with pytest.raises(ValueError, match=r'auth must be "logged-in"'):
        roster_mod.load_roster(_write(tmp_path, invalid))


def test_metadata_roster_rejects_missing_fallback_target(tmp_path):
    invalid = METADATA_ROSTER.replace('fallback = ["coder"]', 'fallback = ["missing"]')
    with pytest.raises(ValueError, match="missing"):
        roster_mod.load_roster(_write(tmp_path, invalid))


def test_metadata_roster_rejects_fallback_cycle(tmp_path):
    invalid = METADATA_ROSTER.replace(
        'requires = { cli = "ollama" }\nstats',
        'requires = { cli = "ollama" }\nfallback = ["coder"]\nstats',
    )

    with pytest.raises(ValueError, match="must not define its own fallback"):
        roster_mod.load_roster(_write(tmp_path, invalid))


def test_metadata_roster_rejects_nested_fallback_chain(tmp_path):
    invalid = METADATA_ROSTER.replace(
        'requires = { cli = "ollama" }\nstats',
        'requires = { cli = "ollama" }\nfallback = ["local"]\nstats',
    )
    invalid += """

[agents.local]
cli = "ollama:llama3.2:3b"
role = "last resort"
requires = { cli = "ollama" }
"""

    with pytest.raises(ValueError, match="must not define its own fallback"):
        roster_mod.load_roster(_write(tmp_path, invalid))


def test_grok_invalid_final_fallback_still_validates_with_metadata_fields(tmp_path):
    text = GROK_FALLBACK_ROSTER.replace(
        'role = "review"',
        'role = "review"\npurpose = "Review changes."\n'
        'requires = { cli = "grok" }\n'
        'stats = { speed = "fast", source = "author-receipts-2026-07" }\n'
        "caveats = []\n",
    )
    loaded = roster_mod.load_roster(_write(tmp_path, text))
    assert loaded.agents["grok-review"].invalid_final_fallback == "cursor-grok"


class FakeProbe:
    def __init__(self, capabilities: dict[str, roster_mod.Capability]):
        self._capabilities = capabilities

    def lookup(self, cli_ref: str) -> roster_mod.Capability:
        return self._capabilities.get(
            cli_ref,
            roster_mod.Capability(installed=False, authenticated=None, detail=f"{cli_ref} missing"),
        )


def _metadata_roster_from_text(text: str, tmp_path) -> roster_mod.Roster:
    return roster_mod.load_roster(_write(tmp_path, text))


def test_resolve_capabilities_selects_self_when_requirements_pass(tmp_path):
    roster = _metadata_roster_from_text(METADATA_ROSTER, tmp_path)
    probe = FakeProbe(
        {
            "codex": roster_mod.Capability(installed=True),
            "ollama": roster_mod.Capability(installed=True),
        }
    )

    result = roster_mod.resolve_capabilities(roster, probe=probe)

    assert "chef" in result.roster.agents
    assert result.roster.agents["chef"].cli == "codex"
    assert result.roster.agents["chef"].fallback == ()
    assert len(result.report) == 1
    chef_report = next(item for item in result.report if item.requested == "chef")
    assert chef_report.outcome == "self"
    assert chef_report.resolved == "chef"


def test_resolve_capabilities_uses_first_satisfiable_fallback(tmp_path):
    roster = _metadata_roster_from_text(METADATA_ROSTER, tmp_path)
    probe = FakeProbe(
        {
            "codex": roster_mod.Capability(installed=False, detail="codex missing"),
            "ollama": roster_mod.Capability(installed=True),
        }
    )

    result = roster_mod.resolve_capabilities(roster, probe=probe)

    chef = result.roster.agents["chef"]
    assert chef.cli == "ollama:llama3.2:3b"
    assert chef.role == "plan"
    assert chef.purpose == "Orchestrate the run."
    assert chef.requires == {"cli": "ollama"}
    assert chef.stats == {"speed": "slow", "source": "author-receipts-2026-07"}
    assert chef.caveats == ()
    assert chef.fallback == ()
    chef_report = next(item for item in result.report if item.requested == "chef")
    assert chef_report.outcome == "fallback"
    assert chef_report.resolved == "coder"
    assert "codex missing" in chef_report.reason


def test_resolve_capabilities_drops_unsatisfied_seat_with_reason(tmp_path):
    roster = _metadata_roster_from_text(METADATA_ROSTER, tmp_path)
    probe = FakeProbe(
        {
            "codex": roster_mod.Capability(installed=False, detail="codex missing"),
            "ollama": roster_mod.Capability(installed=False, detail="ollama missing"),
        }
    )

    result = roster_mod.resolve_capabilities(roster, probe=probe)

    assert "chef" not in result.roster.agents
    chef_report = next(item for item in result.report if item.requested == "chef")
    assert chef_report.outcome == "dropped"
    assert chef_report.resolved is None
    assert "codex missing" in chef_report.reason
    assert "ollama missing" in chef_report.reason


def test_resolve_capabilities_emits_one_report_entry_per_requested_root(tmp_path):
    roster = _metadata_roster_from_text(METADATA_ROSTER, tmp_path)
    probe = FakeProbe({"codex": roster_mod.Capability(installed=True), "ollama": roster_mod.Capability(installed=True)})

    result = roster_mod.resolve_capabilities(roster, probe=probe)

    assert len(result.report) == 1
    assert {item.requested for item in result.report} == {"chef"}


def test_resolve_capabilities_keeps_no_requirement_endpoint_seats(tmp_path):
    text = (
        'orchestrator = "chef"\n'
        '[agents.chef]\ncli = "codex"\nrole = "plan"\n'
        '[agents.api]\nrole = "researcher"\nendpoint = "http://example.test/v1"\nmodel = "m"\n'
    )
    roster = _metadata_roster_from_text(text, tmp_path)
    probe = FakeProbe({"codex": roster_mod.Capability(installed=False)})

    result = roster_mod.resolve_capabilities(roster, probe=probe)

    assert "api" in result.roster.agents
    api_report = next(item for item in result.report if item.requested == "api")
    assert api_report.outcome == "self"


TWO_FALLBACK_ROSTER = """
orchestrator = "chef"

[agents.chef]
cli = "codex"
role = "plan"
purpose = "Primary orchestrator."
requires = { cli = "codex" }
fallback = ["fallback_a", "fallback_b"]
stats = { speed = "fast", source = "author-receipts-2026-07" }
caveats = ["Primary seat caveat."]

[agents.fallback_a]
cli = "grok"
model = "grok-4.5"
role = "first fallback"
purpose = "First fallback seat."
requires = { cli = "grok" }
fallback = []
stats = { speed = "medium", source = "author-receipts-2026-07" }
caveats = ["First fallback caveat."]

[agents.fallback_b]
cli = "ollama:llama3.2:3b"
role = "second fallback"
purpose = "Second fallback seat."
requires = { cli = "ollama" }
fallback = []
stats = { speed = "slow", source = "author-receipts-2026-07" }
caveats = ["Second fallback caveat."]
"""


def test_resolve_capabilities_selects_first_of_two_satisfiable_fallbacks(tmp_path):
    roster = _metadata_roster_from_text(TWO_FALLBACK_ROSTER, tmp_path)
    probe = FakeProbe(
        {
            "codex": roster_mod.Capability(installed=False, detail="codex missing"),
            "grok": roster_mod.Capability(installed=True),
            "ollama": roster_mod.Capability(installed=True),
        }
    )

    result = roster_mod.resolve_capabilities(roster, probe=probe)

    chef = result.roster.agents["chef"]
    assert chef.cli == "grok"
    assert chef.stats == {"speed": "medium", "source": "author-receipts-2026-07"}
    assert chef.caveats == ("First fallback caveat.",)
    assert chef.fallback == ()
    chef_report = next(item for item in result.report if item.requested == "chef")
    assert chef_report.outcome == "fallback"
    assert chef_report.resolved == "fallback_a"
    assert "codex missing" in chef_report.reason
    assert "fallback_a" in chef_report.reason


def test_resolve_capabilities_skips_failed_fallback_for_later_satisfiable_one(tmp_path):
    roster = _metadata_roster_from_text(TWO_FALLBACK_ROSTER, tmp_path)
    probe = FakeProbe(
        {
            "codex": roster_mod.Capability(installed=False, detail="codex missing"),
            "grok": roster_mod.Capability(installed=False, detail="grok missing"),
            "ollama": roster_mod.Capability(installed=True),
        }
    )

    result = roster_mod.resolve_capabilities(roster, probe=probe)

    chef = result.roster.agents["chef"]
    assert chef.cli == "ollama:llama3.2:3b"
    chef_report = next(item for item in result.report if item.requested == "chef")
    assert chef_report.outcome == "fallback"
    assert chef_report.resolved == "fallback_b"
    assert "grok missing" in chef_report.reason


AUTH_FALLBACK_ROSTER = """
orchestrator = "chef"

[agents.chef]
cli = "cursor"
model = "composer-2.5"
role = "plan"
purpose = "Primary orchestrator."
requires = { cli = "cursor", auth = "logged-in" }
fallback = ["chef_codex"]
stats = { speed = "fast", source = "author-receipts-2026-07" }
caveats = []

[agents.chef_codex]
cli = "codex"
role = "codex fallback"
purpose = "Codex fallback orchestrator."
requires = { cli = "codex" }
fallback = []
stats = { speed = "medium", source = "author-receipts-2026-07" }
caveats = ["Codex fallback caveat."]
"""


def test_resolve_capabilities_auth_failure_falls_back_with_detailed_reason(tmp_path):
    roster = _metadata_roster_from_text(AUTH_FALLBACK_ROSTER, tmp_path)
    probe = FakeProbe(
        {
            "cursor": roster_mod.Capability(
                installed=True,
                authenticated=False,
                detail="cursor-agent CLI is not logged in; run cursor-agent login",
            ),
            "codex": roster_mod.Capability(installed=True, authenticated=True),
        }
    )

    result = roster_mod.resolve_capabilities(roster, probe=probe)

    chef = result.roster.agents["chef"]
    assert chef.cli == "codex"
    assert chef.caveats == ("Codex fallback caveat.",)
    assert chef.fallback == ()
    chef_report = next(item for item in result.report if item.requested == "chef")
    assert chef_report.outcome == "fallback"
    assert "cursor-agent CLI is not logged in" in chef_report.reason
    assert "chef_codex" in chef_report.reason


def test_resolve_capabilities_fallback_definitions_are_not_independent_report_entries(tmp_path):
    roster = _metadata_roster_from_text(METADATA_ROSTER, tmp_path)
    probe = FakeProbe(
        {
            "codex": roster_mod.Capability(installed=True),
            "ollama": roster_mod.Capability(installed=True),
        }
    )

    result = roster_mod.resolve_capabilities(roster, probe=probe)

    assert {item.requested for item in result.report} == {"chef"}
    assert "coder" not in result.roster.agents
    assert "coder" not in {item.requested for item in result.report}


def test_resolve_capabilities_self_selected_roots_clear_fallback_chain(tmp_path):
    roster = _metadata_roster_from_text(METADATA_ROSTER, tmp_path)
    probe = FakeProbe(
        {
            "codex": roster_mod.Capability(installed=True),
            "ollama": roster_mod.Capability(installed=True),
        }
    )

    result = roster_mod.resolve_capabilities(roster, probe=probe)

    assert result.roster.agents["chef"].fallback == ()


def test_resolve_capabilities_legacy_roster_is_unchanged_with_self_reports(tmp_path):
    roster = roster_mod.load_roster(_write(tmp_path, VALID))
    probe = FakeProbe({})

    result = roster_mod.resolve_capabilities(roster, probe=probe)

    assert result.roster == roster
    assert result.usable is True
    assert len(result.report) == len(roster.agents)
    assert all(item.outcome == "self" for item in result.report)
    assert {item.requested for item in result.report} == set(roster.agents)


def test_resolve_capabilities_dropped_orchestrator_sets_usable_false(tmp_path):
    roster = _metadata_roster_from_text(METADATA_ROSTER, tmp_path)
    probe = FakeProbe(
        {
            "codex": roster_mod.Capability(installed=False, detail="codex missing"),
            "ollama": roster_mod.Capability(installed=False, detail="ollama missing"),
        }
    )

    result = roster_mod.resolve_capabilities(roster, probe=probe)

    assert result.usable is False
    assert "chef" not in result.roster.agents
    chef_report = next(item for item in result.report if item.requested == "chef")
    assert chef_report.outcome == "dropped"


def test_load_rejects_orchestrator_fallback_to_acpx_worker(tmp_path):
    text = (
        'orchestrator = "chef"\n'
        '[agents.chef]\ncli = "codex"\nrole = "plan"\n'
        'requires = { cli = "codex" }\nfallback = ["cursor_worker"]\n'
        '[agents.cursor_worker]\ncli = "cursor"\nmodel = "composer-2.5"\n'
        'transport = "acpx"\ntransport_version = "0.12.0"\nrole = "worker"\n'
        'requires = { cli = "cursor" }\n'
    )
    with pytest.raises(ValueError, match="acpx"):
        roster_mod.load_roster(_write(tmp_path, text))


@pytest.mark.parametrize(
    "fallback_definition",
    [
        (
            '[agents.cloud_worker]\ncli = "codex-cloud:env"\nrole = "worker"\n'
            'requires = { cli = "codex" }\n'
            '[limits]\nallow_models = ["claude", "codex-cloud:*"]\n'
        ),
        ('[agents.endpoint_worker]\nrole = "worker"\nendpoint = "http://example.test/v1"\nmodel = "m"\n'),
    ],
)
def test_load_rejects_orchestrator_fallback_to_other_worker_only_seats(tmp_path, fallback_definition):
    fallback_name = "cloud_worker" if "cloud_worker" in fallback_definition else "endpoint_worker"
    text = (
        'orchestrator = "chef"\n'
        '[agents.chef]\ncli = "claude"\nrole = "plan"\n'
        'requires = { cli = "claude" }\n'
        f'fallback = ["{fallback_name}"]\n'
        f"{fallback_definition}"
    )

    with pytest.raises(ValueError, match="worker-only"):
        roster_mod.load_roster(_write(tmp_path, text))


def test_load_rejects_non_orchestrator_fallback_naming_orchestrator(tmp_path):
    text = (
        'orchestrator = "chef"\n'
        '[agents.chef]\ncli = "codex"\nrole = "plan"\n'
        'requires = { cli = "codex" }\n'
        '[agents.reviewer]\ncli = "grok"\nmodel = "grok-4.5"\nrole = "review"\n'
        'requires = { cli = "grok" }\nfallback = ["chef"]\n'
    )
    with pytest.raises(ValueError, match="orchestrator"):
        roster_mod.load_roster(_write(tmp_path, text))


@pytest.mark.parametrize(
    ("requires_cli", "seat_cli", "should_load"),
    [
        ("ollama", "ollama:llama3.2:3b", True),
        ("codex", "codex-cloud:brigade", True),
        ("claude", "claude", True),
        ("codex", "claude", False),
    ],
)
def test_load_validates_requires_cli_against_seat_adapter(tmp_path, requires_cli, seat_cli, should_load):
    text = (
        'orchestrator = "chef"\n'
        f'[agents.chef]\ncli = "{seat_cli}"\nrole = "plan"\n'
        f'requires = {{ cli = "{requires_cli}" }}\n'
    )
    if seat_cli.startswith("codex-cloud"):
        text += '\n[limits]\nallow_models = ["codex-cloud:*"]\n'
    if should_load:
        loaded = roster_mod.load_roster(_write(tmp_path, text))
        assert loaded.agents["chef"].requires == {"cli": requires_cli}
    else:
        with pytest.raises(ValueError, match=r"requires\.cli"):
            roster_mod.load_roster(_write(tmp_path, text))


def test_load_rejects_requires_cli_on_endpoint_seat(tmp_path):
    text = (
        'orchestrator = "chef"\n'
        '[agents.chef]\ncli = "codex"\nrole = "plan"\n'
        '[agents.api]\nrole = "researcher"\nendpoint = "http://example.test/v1"\nmodel = "m"\n'
        'requires = { cli = "codex" }\n'
    )
    with pytest.raises(ValueError, match=r"requires\.cli"):
        roster_mod.load_roster(_write(tmp_path, text))


INVALID_FINAL_RESOLUTION_ROSTER = """
orchestrator = "chef"

[agents.chef]
cli = "codex"
role = "plan"
purpose = "Orchestrator."

[agents.grok-review]
cli = "grok"
model = "grok-4.5"
role = "review"
purpose = "Review lane."
requires = { cli = "grok" }
invalid_final_fallback = "cursor-grok"
stats = { speed = "fast", source = "author-receipts-2026-07" }
caveats = []

[agents.cursor-grok]
cli = "cursor"
model = "grok-4.5"
transport = "acpx"
transport_version = "0.12.0"
role = "fallback review"
purpose = "ACP fallback."
requires = { cli = "cursor", auth = "logged-in" }
stats = { speed = "fast", source = "author-receipts-2026-07" }
caveats = []
"""


def test_resolve_capabilities_includes_invalid_final_fallback_dependency(tmp_path):
    roster = _metadata_roster_from_text(INVALID_FINAL_RESOLUTION_ROSTER, tmp_path)
    probe = FakeProbe(
        {
            "codex": roster_mod.Capability(installed=True),
            "grok": roster_mod.Capability(installed=True),
            "cursor": roster_mod.Capability(installed=True, authenticated=True),
        }
    )

    result = roster_mod.resolve_capabilities(roster, probe=probe)

    assert "grok-review" in result.roster.agents
    assert result.roster.agents["grok-review"].invalid_final_fallback == "cursor-grok"
    assert "cursor-grok" in result.roster.agents


def test_resolve_capabilities_clears_invalid_final_fallback_when_dependency_unsatisfied(tmp_path):
    roster = _metadata_roster_from_text(INVALID_FINAL_RESOLUTION_ROSTER, tmp_path)
    probe = FakeProbe(
        {
            "codex": roster_mod.Capability(installed=True),
            "grok": roster_mod.Capability(installed=True),
            "cursor": roster_mod.Capability(installed=False, detail="cursor missing"),
        }
    )

    result = roster_mod.resolve_capabilities(roster, probe=probe)

    grok = result.roster.agents["grok-review"]
    assert grok.invalid_final_fallback is None
    assert "cursor-grok" not in result.roster.agents


def test_resolve_capabilities_clears_invalid_final_fallback_when_target_resolves_to_incompatible_seat(tmp_path):
    text = INVALID_FINAL_RESOLUTION_ROSTER.replace(
        'requires = { cli = "cursor", auth = "logged-in" }\nstats',
        'requires = { cli = "cursor", auth = "logged-in" }\nfallback = ["cursor-grok-codex"]\nstats',
    )
    text += """

[agents.cursor-grok-codex]
cli = "codex"
model = "gpt-5.6-terra"
role = "fallback review"
requires = { cli = "codex" }
"""
    roster = _metadata_roster_from_text(text, tmp_path)
    probe = FakeProbe(
        {
            "codex": roster_mod.Capability(installed=True),
            "grok": roster_mod.Capability(installed=True),
            "cursor": roster_mod.Capability(installed=False, detail="cursor missing"),
        }
    )

    result = roster_mod.resolve_capabilities(roster, probe=probe)

    assert result.roster.agents["cursor-grok"].cli == "codex"
    assert result.roster.agents["grok-review"].invalid_final_fallback is None


def test_host_capability_probe_uses_detect_and_command_for(monkeypatch):
    calls: list[tuple[str, str]] = []

    def fake_command_for(cli_ref: str) -> str:
        calls.append(("command_for", cli_ref))
        return f"bin-{cli_ref}"

    def fake_detect(cli_ref: str) -> bool:
        calls.append(("detect", cli_ref))
        return cli_ref == "codex"

    monkeypatch.setattr(agents, "command_for", fake_command_for)
    monkeypatch.setattr(agents, "detect", fake_detect)

    capability = roster_mod.HostCapabilityProbe().lookup("codex")

    assert ("command_for", "codex") in calls
    assert ("detect", "codex") in calls
    assert capability.installed is True
    assert capability.detail == "codex via bin-codex"


def test_host_capability_probe_missing_cli_matches_doctor_style(monkeypatch):
    monkeypatch.setattr(agents, "command_for", lambda cli_ref: "missing-bin")
    monkeypatch.setattr(agents, "detect", lambda cli_ref: False)

    capability = roster_mod.HostCapabilityProbe().lookup("codex")

    assert capability.installed is False
    assert capability.detail == "codex needs `missing-bin` on PATH"


def test_host_capability_probe_cursor_auth_states(monkeypatch):
    from brigade import acpx_adapter

    monkeypatch.setattr(agents, "command_for", lambda cli_ref: "cursor-agent")
    monkeypatch.setattr(agents, "detect", lambda cli_ref: True)
    auth_calls: list[str] = []

    def fake_auth_status():
        auth_calls.append("called")
        return acpx_adapter.CursorAuthStatus("authenticated", "logged in", "", "", 0)

    monkeypatch.setattr(acpx_adapter, "cursor_auth_status", fake_auth_status)
    authenticated = roster_mod.HostCapabilityProbe().lookup("cursor")
    assert authenticated.authenticated is True
    assert auth_calls == ["called"]

    monkeypatch.setattr(
        acpx_adapter,
        "cursor_auth_status",
        lambda: acpx_adapter.CursorAuthStatus("unauthenticated", "not logged in", "", "", 1),
    )
    unauthenticated = roster_mod.HostCapabilityProbe().lookup("cursor")
    assert unauthenticated.authenticated is False

    monkeypatch.setattr(
        acpx_adapter,
        "cursor_auth_status",
        lambda: acpx_adapter.CursorAuthStatus("unavailable", "cursor-agent is not installed", "", "", 127),
    )
    unavailable = roster_mod.HostCapabilityProbe().lookup("cursor")
    assert unavailable.authenticated is None
    assert unavailable.detail == "cursor-agent is not installed"
    assert unavailable.auth_detail == "cursor-agent is not installed"

    monkeypatch.setattr(
        acpx_adapter,
        "cursor_auth_status",
        lambda: acpx_adapter.CursorAuthStatus("unrecognized", "unexpected status payload", "", "", 0),
    )
    unrecognized = roster_mod.HostCapabilityProbe().lookup("cursor")
    assert unrecognized.authenticated is None
    assert unrecognized.detail == "unexpected status payload"
    assert unrecognized.auth_detail == "unexpected status payload"


def test_host_capability_probe_skips_cursor_auth_when_cursor_not_installed(monkeypatch):
    from brigade import acpx_adapter

    monkeypatch.setattr(agents, "command_for", lambda cli_ref: "cursor-agent")
    monkeypatch.setattr(agents, "detect", lambda cli_ref: False)

    def fail_auth():
        raise AssertionError("cursor_auth_status must not run when cursor is not installed")

    monkeypatch.setattr(acpx_adapter, "cursor_auth_status", fail_auth)

    capability = roster_mod.HostCapabilityProbe().lookup("cursor")

    assert capability.installed is False
    assert capability.authenticated is None


def test_resolve_capabilities_fake_probe_never_calls_host_functions(monkeypatch, tmp_path):
    roster = _metadata_roster_from_text(METADATA_ROSTER, tmp_path)
    probe = FakeProbe({"codex": roster_mod.Capability(installed=True), "ollama": roster_mod.Capability(installed=True)})

    def fail_detect(cli_ref: str) -> bool:
        raise AssertionError("detect must not run when a probe is injected")

    monkeypatch.setattr(agents, "detect", fail_detect)

    roster_mod.resolve_capabilities(roster, probe=probe)


def test_requirements_satisfied_skips_redundant_auth_when_cli_missing():
    probe = FakeProbe({"cursor": roster_mod.Capability(installed=False, detail="cursor missing")})
    agent = roster_mod.Agent(
        name="worker",
        cli="cursor",
        role="build",
        requires={"cli": "cursor", "auth": "logged-in"},
    )

    ok, reason = roster_mod._requirements_satisfied(agent, probe)

    assert ok is False
    assert reason == "cursor missing"
    assert "authenticated" not in reason.lower()


def test_requirements_satisfied_reports_unknown_auth_separately_from_installation():
    probe = FakeProbe({"codex": roster_mod.Capability(installed=True, detail="codex via codex")})
    agent = roster_mod.Agent(
        name="worker",
        cli="codex",
        role="build",
        requires={"cli": "codex", "auth": "logged-in"},
    )

    ok, reason = roster_mod._requirements_satisfied(agent, probe)

    assert ok is False
    assert reason == "codex authentication is unknown"


def _write_worker_results(run_dir: Path, results: list[dict]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "results": results,
        "ground_truth": {
            "available": False,
            "cwd": "/repo",
            "diffstat": "",
            "changed_files": [],
            "untracked_files": [],
            "patch_ref": None,
        },
    }
    (run_dir / "worker-results.json").write_text(json.dumps(payload, indent=2) + "\n")


def test_collect_seat_receipt_stats_computes_median_duration_and_failure_rate(tmp_path):
    runs = tmp_path / ".brigade" / "runs"
    _write_worker_results(
        runs / "run-a",
        [
            {"worker": "coder", "task": "a", "ok": True, "detail": "", "text": "ok", "duration_seconds": 10.0},
            {"worker": "coder", "task": "b", "ok": False, "detail": "err", "text": "", "duration_seconds": 30.0},
        ],
    )
    _write_worker_results(
        runs / "run-b",
        [
            {"worker": "coder", "task": "c", "ok": True, "detail": "", "text": "ok", "duration_seconds": 20.0},
            {"worker": "reviewer", "task": "d", "ok": True, "detail": "", "text": "ok", "duration_seconds": 5.0},
        ],
    )

    stats = roster_mod.collect_seat_receipt_stats(runs)

    coder = stats["coder"]
    assert coder.sample_count == 3
    assert coder.median_duration_seconds == pytest.approx(20.0)
    assert coder.failure_rate == pytest.approx(1 / 3)
    reviewer = stats["reviewer"]
    assert reviewer.sample_count == 1
    assert reviewer.median_duration_seconds == pytest.approx(5.0)
    assert reviewer.failure_rate == pytest.approx(0.0)


def test_collect_seat_receipt_stats_ignores_missing_worker_results(tmp_path):
    runs = tmp_path / ".brigade" / "runs"
    (runs / "empty-run").mkdir(parents=True)

    stats = roster_mod.collect_seat_receipt_stats(runs)

    assert stats == {}


def test_collect_seat_receipt_stats_counts_failures_without_durations(tmp_path):
    runs = tmp_path / ".brigade" / "runs"
    _write_worker_results(
        runs / "run-a",
        [
            {"worker": "coder", "ok": True, "duration_seconds": 10.0},
            {"worker": "coder", "ok": False},
        ],
    )

    coder = roster_mod.collect_seat_receipt_stats(runs)["coder"]

    assert coder.sample_count == 2
    assert coder.median_duration_seconds == pytest.approx(10.0)
    assert coder.failure_rate == pytest.approx(0.5)
