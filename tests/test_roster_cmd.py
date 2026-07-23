import json
import re
from pathlib import Path

import pytest

from brigade import agents
from brigade import cli
from brigade import model_inventory
from brigade import roster
from brigade import roster_cmd
from brigade import toml_compat


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


EXPECTED_PRESET_NAMES = (
    "budget-open-weight.toml",
    "full-multi-lane.toml",
    "minimal-single-cli.toml",
    "review-heavy.toml",
)


def test_preset_roster_paths_returns_four_packaged_presets():
    paths = roster_cmd.preset_roster_paths()
    assert tuple(path.name for path in paths) == EXPECTED_PRESET_NAMES
    assert all(path.is_file() for path in paths)


@pytest.mark.parametrize("preset_name", EXPECTED_PRESET_NAMES)
def test_packaged_presets_parse(preset_name):
    path = next(item for item in roster_cmd.preset_roster_paths() if item.name == preset_name)
    raw = toml_compat.loads(path.read_text())
    loaded = roster.load_roster(path)
    assert loaded.orchestrator in loaded.agents
    for name, agent in loaded.agents.items():
        assert agent.purpose
        assert agent.requires is not None
        assert agent.stats is not None
        assert "speed" in agent.stats
        assert "source" in agent.stats
        assert "caveats" in raw["agents"][name]


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
        model_inventory.proc,
        "run",
        lambda argv, **kwargs: agents.proc.Result(
            0,
            "NAME ID SIZE MODIFIED\nother:latest abcdef123456 2.0 GB 2 days ago\n",
            "",
        ),
    )
    rc = roster_cmd.doctor(tmp_target)
    out = capsys.readouterr().out
    assert rc == 0
    assert "[warn]" in out
    assert "not listed locally" in out
    assert "never auto-pulls" in out


def test_roster_doctor_ok_when_ollama_model_pulled(monkeypatch, tmp_target, capsys):
    roster_cmd.init(tmp_target)
    monkeypatch.setattr(agents.proc, "which", lambda cmd: "/x/" + cmd)
    monkeypatch.setattr(
        model_inventory.proc,
        "run",
        lambda argv, **kwargs: agents.proc.Result(
            0,
            f"NAME ID SIZE MODIFIED\n{roster_cmd.DEFAULT_OLLAMA_MODEL} abcdef123456 2.0 GB 2 days ago\n",
            "",
        ),
    )
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


def _write_grok_inventory_roster(tmp_target, model: str) -> None:
    _write_roster(
        tmp_target,
        f'orchestrator = "chef"\n[agents.chef]\ncli = "grok"\nmodel = "{model}"\nrole = "plan"\n',
    )


def test_roster_doctor_reports_exact_live_model_inventory(monkeypatch, tmp_target, capsys):
    _write_grok_inventory_roster(tmp_target, "grok-4.5")
    monkeypatch.setattr(agents.proc, "which", lambda cmd: "/x/" + cmd)
    monkeypatch.setattr(
        model_inventory.proc,
        "run",
        lambda argv, **kwargs: agents.proc.Result(0, "Available models:\n  * grok-4.5 (default)\n", ""),
    )

    rc = roster_cmd.doctor(tmp_target)
    out = capsys.readouterr().out

    assert rc == 0
    assert "agent: chef model inventory" in out
    assert "exact:" in out


def test_roster_doctor_warns_on_fuzzy_resolved_model(monkeypatch, tmp_target, capsys):
    _write_grok_inventory_roster(tmp_target, "grok-4.5-xhigh")
    monkeypatch.setattr(agents.proc, "which", lambda cmd: "/x/" + cmd)
    monkeypatch.setattr(
        model_inventory.proc,
        "run",
        lambda argv, **kwargs: agents.proc.Result(0, "Available models:\n  * grok-4.5 (default)\n", ""),
    )

    rc = roster_cmd.doctor(tmp_target)
    out = capsys.readouterr().out

    assert rc == 0
    assert "[warn] agent: chef model inventory" in out
    assert "fuzzy-resolved:" in out
    assert "grok-4.5" in out


def test_roster_doctor_warns_on_missing_live_model(monkeypatch, tmp_target, capsys):
    _write_grok_inventory_roster(tmp_target, "grok-4.6")
    monkeypatch.setattr(agents.proc, "which", lambda cmd: "/x/" + cmd)
    monkeypatch.setattr(
        model_inventory.proc,
        "run",
        lambda argv, **kwargs: agents.proc.Result(0, "Available models:\n  * grok-4.5 (default)\n", ""),
    )

    rc = roster_cmd.doctor(tmp_target)
    out = capsys.readouterr().out

    assert rc == 0
    assert "[warn] agent: chef model inventory" in out
    assert "missing:" in out
    assert "absent" in out


def test_roster_doctor_warns_when_live_inventory_is_unavailable(monkeypatch, tmp_target, capsys):
    _write_grok_inventory_roster(tmp_target, "grok-4.5")
    monkeypatch.setattr(agents.proc, "which", lambda cmd: "/x/" + cmd)
    monkeypatch.setattr(
        model_inventory.proc,
        "run",
        lambda argv, **kwargs: agents.proc.Result(1, "", "inventory network error"),
    )

    rc = roster_cmd.doctor(tmp_target)
    out = capsys.readouterr().out

    assert rc == 0
    assert "[warn] agent: chef model inventory" in out
    assert "unavailable:" in out
    assert "inventory network error" in out


def test_roster_doctor_warns_on_retired_ollama_cloud_model(monkeypatch, tmp_target, capsys):
    _write_roster(
        tmp_target,
        'orchestrator = "chef"\n[agents.chef]\ncli = "ollama:glm-5:cloud"\nrole = "plan"\n',
    )
    monkeypatch.setattr(agents.proc, "which", lambda cmd: "/x/" + cmd)

    def fake_run(argv, **kwargs):
        if argv == ["ollama", "list"]:
            return agents.proc.Result(
                0,
                "NAME ID SIZE MODIFIED\nglm-5:cloud abcdef123456 - 2 days ago\n",
                "",
            )
        assert argv == ["ollama", "show", "glm-5:cloud"]
        return agents.proc.Result(1, "", "Error: glm-5 was retired at 2026-07-15")

    monkeypatch.setattr(model_inventory.proc, "run", fake_run)

    rc = roster_cmd.doctor(tmp_target)
    out = capsys.readouterr().out

    assert rc == 0
    assert "[warn] agent: chef model inventory" in out
    assert "missing:" in out
    assert "retired" in out


def test_roster_doctor_reuses_inventory_for_repeated_harness_seats(monkeypatch, tmp_target, capsys):
    _write_roster(
        tmp_target,
        'orchestrator = "chef"\n'
        '[agents.chef]\ncli = "cursor"\nmodel = "composer-2.5"\nrole = "plan"\n'
        '[agents.reviewer]\ncli = "cursor"\nmodel = "gpt-5.5-high"\nrole = "review"\n',
    )
    monkeypatch.setattr(agents.proc, "which", lambda cmd: "/x/" + cmd)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return agents.proc.Result(
            0,
            "Available models\n\ncomposer-2.5 - Composer 2.5\ngpt-5.5-high - GPT-5.5 High\n",
            "",
        )

    monkeypatch.setattr(model_inventory.proc, "run", fake_run)

    assert roster_cmd.doctor(tmp_target) == 0
    assert capsys.readouterr().out.count("model inventory") == 2
    assert calls == [["cursor-agent", "models"]]


def test_roster_doctor_does_not_apply_direct_cursor_inventory_to_acpx_seat(monkeypatch, tmp_target, capsys):
    from brigade import acpx_adapter

    _write_roster(
        tmp_target,
        'orchestrator = "chef"\n'
        '[agents.chef]\ncli = "codex"\nrole = "plan"\n'
        '[agents.reviewer]\ncli = "cursor"\nmodel = "grok-4.5"\nrole = "review"\n'
        'transport = "acpx"\ntransport_version = "0.12.0"\n',
    )
    monkeypatch.setattr(agents.proc, "which", lambda cmd: "/x/" + cmd)
    monkeypatch.setattr(acpx_adapter, "installed_version", lambda: ("0.12.0", ""))
    monkeypatch.setattr(
        acpx_adapter,
        "cursor_auth_status",
        lambda: acpx_adapter.CursorAuthStatus("authenticated", "authenticated", "", "", 0),
    )
    monkeypatch.setattr(
        model_inventory.proc,
        "run",
        lambda argv, **kwargs: (_ for _ in ()).throw(AssertionError(f"unexpected direct inventory: {argv}")),
    )

    assert roster_cmd.doctor(tmp_target) == 0
    out = capsys.readouterr().out
    assert "agent: reviewer model inventory" not in out
    assert "agent: reviewer acpx" in out


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
    monkeypatch.setattr(
        model_inventory.proc,
        "run",
        lambda argv, **kwargs: agents.proc.Result(
            0,
            "NAME ID SIZE MODIFIED\nllama3.3 abcdef123456 43 GB 2 days ago\n",
            "",
        ),
    )
    rc = roster_cmd.doctor(tmp_target)
    out = capsys.readouterr().out
    assert rc == 1
    assert "ollama names its model in the cli ref" in out


_CF_MODEL_ROUTE = "cloudflare-ai-gateway/openai/gpt-5.3-codex"
_FAKE_CF_ACCOUNT = "fake-account-id-for-test"
_FAKE_CF_GATEWAY = "fake-gateway-id-for-test"


def _write_cloudflare_gateway_roster(tmp_target) -> None:
    _write_roster(
        tmp_target,
        'orchestrator = "chef"\n'
        '[agents.chef]\ncli = "codex"\nmodel = "gpt-5.5"\nrole = "plan"\n'
        f'[agents.cf_worker]\ncli = "codex"\nmodel = "{_CF_MODEL_ROUTE}"\nrole = "worker"\n',
    )


def _clear_cloudflare_gateway_env(monkeypatch) -> None:
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("CLOUDFLARE_GATEWAY_ID", raising=False)


def test_roster_doctor_ok_for_cloudflare_gateway_when_env_present(monkeypatch, tmp_target, capsys):
    _write_cloudflare_gateway_roster(tmp_target)
    _clear_cloudflare_gateway_env(monkeypatch)
    monkeypatch.setattr(agents.proc, "which", lambda cmd: "/x/" + cmd)
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", _FAKE_CF_ACCOUNT)
    monkeypatch.setenv("CLOUDFLARE_GATEWAY_ID", _FAKE_CF_GATEWAY)

    rc = roster_cmd.doctor(tmp_target)
    out = capsys.readouterr().out

    assert rc == 0
    assert "[ok]   agent: cf_worker cloudflare gateway" in out
    assert "required env vars are set" in out
    assert _FAKE_CF_ACCOUNT not in out
    assert _FAKE_CF_GATEWAY not in out


@pytest.mark.parametrize(
    ("env", "missing_vars"),
    [
        ({}, ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_GATEWAY_ID")),
        ({"CLOUDFLARE_ACCOUNT_ID": _FAKE_CF_ACCOUNT}, ("CLOUDFLARE_GATEWAY_ID",)),
        ({"CLOUDFLARE_GATEWAY_ID": _FAKE_CF_GATEWAY}, ("CLOUDFLARE_ACCOUNT_ID",)),
    ],
)
def test_roster_doctor_fails_cloudflare_gateway_when_env_missing(monkeypatch, tmp_target, capsys, env, missing_vars):
    _write_cloudflare_gateway_roster(tmp_target)
    _clear_cloudflare_gateway_env(monkeypatch)
    monkeypatch.setattr(agents.proc, "which", lambda cmd: "/x/" + cmd)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    rc = roster_cmd.doctor(tmp_target)
    out = capsys.readouterr().out

    assert rc == 1
    assert "[fail] agent: cf_worker cloudflare gateway" in out
    for var in missing_vars:
        assert var in out
    assert _FAKE_CF_ACCOUNT not in out
    assert _FAKE_CF_GATEWAY not in out


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


class FakeProbe:
    def __init__(self, capabilities: dict[str, roster.Capability]):
        self._capabilities = capabilities

    def lookup(self, cli_ref: str) -> roster.Capability:
        return self._capabilities.get(
            cli_ref,
            roster.Capability(installed=False, authenticated=None, detail=f"{cli_ref} missing"),
        )


def _preset_path(preset_name: str) -> Path:
    return next(path for path in roster_cmd.preset_roster_paths() if path.name == preset_name)


def _installed_probe(*cli_refs: str, authenticated: bool = True) -> FakeProbe:
    return FakeProbe(
        {
            ref: roster.Capability(installed=True, authenticated=authenticated, detail=f"{ref} available")
            for ref in cli_refs
        }
    )


def _write_worker_run(
    runs_root: Path,
    run_id: str,
    *,
    results: list[dict],
) -> Path:
    run_dir = runs_root / run_id
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
    return run_dir


def _runs_root(target: Path) -> Path:
    root = target / ".brigade" / "runs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _seat_resolution_fields_present(out: str, seat: str) -> None:
    pattern = rf"requested={re.escape(seat)}\b.*outcome=\w+.*resolved=.*reason="
    assert re.search(pattern, out, flags=re.DOTALL), out


def test_roster_suggest_prints_every_seat_resolution_when_roots_satisfiable(tmp_target, capsys):
    preset = _preset_path("review-heavy.toml")
    probe = _installed_probe("codex", "grok", "antigravity")

    assert roster_cmd.suggest(tmp_target, preset=preset, probe=probe) == 0
    out = capsys.readouterr().out

    for seat in ("chef", "reviewer", "reviewer_flash"):
        _seat_resolution_fields_present(out, seat)
        assert f"resolved={seat}" in out
    assert out.count("outcome=self") == 3
    assert "orchestrator =" in out
    assert "adoptable roster" in out.lower()
    emitted = out.split("# Adoptable roster\n", maxsplit=1)[1]
    resolved_path = tmp_target / "resolved-roster.toml"
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_path.write_text(emitted)
    assert roster.load_roster(resolved_path).orchestrator == "chef"


def test_roster_cli_registers_suggest_and_stats_commands(tmp_target):
    parser = cli._build_parser()

    suggest_args = parser.parse_args(
        ["roster", "suggest", "--preset", "minimal-single-cli", "--target", str(tmp_target)]
    )
    stats_args = parser.parse_args(["roster", "stats", "--target", str(tmp_target)])

    assert suggest_args.roster_command == "suggest"
    assert suggest_args.preset == "minimal-single-cli"
    assert suggest_args.target == tmp_target
    assert stats_args.roster_command == "stats"
    assert stats_args.target == tmp_target


def test_roster_suggest_reports_first_satisfiable_fallback_when_primary_cli_missing(tmp_target, capsys):
    preset = _preset_path("budget-open-weight.toml")
    probe = FakeProbe(
        {
            "codex": roster.Capability(installed=False, detail="codex missing"),
            "ollama": roster.Capability(installed=True, detail="ollama available"),
        }
    )

    assert roster_cmd.suggest(tmp_target, preset=preset, probe=probe) == 0
    out = capsys.readouterr().out

    _seat_resolution_fields_present(out, "chef")
    assert "outcome=fallback" in out
    assert "resolved=chef_oss" in out
    assert "codex missing" in out
    _seat_resolution_fields_present(out, "coder")
    assert "resolved=coder" in out
    assert "outcome=self" in out


def test_roster_suggest_reports_dropped_seat_when_no_fallback_satisfies(tmp_target, capsys):
    preset = _preset_path("budget-open-weight.toml")
    probe = FakeProbe(
        {
            "codex": roster.Capability(installed=False, detail="codex missing"),
            "ollama": roster.Capability(installed=False, detail="ollama missing"),
        }
    )

    rc = roster_cmd.suggest(tmp_target, preset=preset, probe=probe)
    out = capsys.readouterr().out

    _seat_resolution_fields_present(out, "chef")
    assert "outcome=dropped" in out
    assert "resolved=-" in out or "resolved=none" in out.lower()
    assert "codex missing" in out
    assert "ollama missing" in out
    assert rc != 0 or "not adoptable" in out.lower()


def test_roster_suggest_refuses_adoptable_roster_when_orchestrator_dropped(tmp_target, capsys):
    preset = _preset_path("budget-open-weight.toml")
    probe = FakeProbe({})

    rc = roster_cmd.suggest(tmp_target, preset=preset, probe=probe)
    out = capsys.readouterr().out

    assert "not adoptable" in out.lower()
    assert "[agents.chef]" not in out
    assert rc != 0


def test_roster_doctor_warns_when_declared_requirements_lapse(monkeypatch, tmp_target, capsys):
    _write_roster(
        tmp_target,
        'orchestrator = "chef"\n'
        "[agents.chef]\n"
        'cli = "codex"\n'
        'role = "plan"\n'
        'requires = { cli = "codex" }\n'
        'fallback = ["chef_oss"]\n'
        "[agents.chef_oss]\n"
        'cli = "ollama:llama3.2:3b"\n'
        'role = "local plan"\n'
        'requires = { cli = "ollama" }\n',
    )
    probe = FakeProbe(
        {
            "codex": roster.Capability(installed=False, detail="codex missing"),
            "ollama": roster.Capability(installed=True),
        }
    )
    monkeypatch.setattr(agents.proc, "which", lambda cmd: "/bin/" + cmd)

    rc = roster_cmd.doctor(tmp_target, probe=probe)
    out = capsys.readouterr().out

    assert rc == 0
    assert "[warn]" in out
    assert "chef" in out
    assert "codex missing" in out


def test_roster_doctor_warns_when_declared_seat_is_dropped(monkeypatch, tmp_target, capsys):
    _write_roster(
        tmp_target,
        'orchestrator = "chef"\n[agents.chef]\ncli = "codex"\nrole = "plan"\nrequires = { cli = "codex" }\n',
    )
    monkeypatch.setattr(agents.proc, "which", lambda cmd: "/bin/" + cmd)

    rc = roster_cmd.doctor(tmp_target, probe=FakeProbe({}))
    out = capsys.readouterr().out

    assert rc == 0
    assert "[warn] roster: capability chef" in out
    assert "codex missing" in out


def test_roster_stats_reports_per_seat_median_and_failure_rate(tmp_target, capsys):
    runs = _runs_root(tmp_target)
    _write_worker_run(
        runs,
        "run-a",
        results=[
            {"worker": "coder", "task": "a", "ok": True, "detail": "", "text": "ok", "duration_seconds": 10.0},
            {"worker": "coder", "task": "b", "ok": False, "detail": "err", "text": "", "duration_seconds": 30.0},
        ],
    )
    _write_worker_run(
        runs,
        "run-b",
        results=[
            {"worker": "coder", "task": "c", "ok": True, "detail": "", "text": "ok", "duration_seconds": 20.0},
        ],
    )

    assert roster_cmd.stats(tmp_target) == 0
    out = capsys.readouterr().out

    assert "coder" in out
    assert "median_duration" in out
    assert "failure_rate" in out
    assert "0.333" in out or "33.3" in out or "1/3" in out


def test_roster_suggest_labels_author_default_stats_without_local_receipts(tmp_target, capsys):
    preset = _preset_path("minimal-single-cli.toml")
    probe = _installed_probe("codex")

    assert roster_cmd.suggest(tmp_target, preset=preset, probe=probe) == 0
    out = capsys.readouterr().out

    assert "source=author-default" in out
    assert "source=local-receipts" not in out


def test_roster_suggest_overlays_local_receipt_stats(tmp_target, capsys):
    preset = _preset_path("minimal-single-cli.toml")
    probe = _installed_probe("codex")
    runs = _runs_root(tmp_target)
    _write_worker_run(
        runs,
        "run-a",
        results=[
            {"worker": "coder", "task": "a", "ok": True, "detail": "", "text": "ok", "duration_seconds": 12.0},
        ],
    )

    assert roster_cmd.suggest(tmp_target, preset=preset, probe=probe) == 0
    out = capsys.readouterr().out

    assert "source=local-receipts" in out
    assert "coder" in out
    assert "median_duration" in out


def test_roster_suggest_does_not_relabel_author_stats_as_local(tmp_target, capsys):
    preset = _preset_path("minimal-single-cli.toml")
    probe = _installed_probe("codex")
    runs = _runs_root(tmp_target)
    _write_worker_run(
        runs,
        "run-a",
        results=[
            {"worker": "coder", "ok": True, "duration_seconds": 12.0},
        ],
    )

    assert roster_cmd.suggest(tmp_target, preset=preset, probe=probe) == 0
    out = capsys.readouterr().out
    emitted = out.split("# Adoptable roster\n", maxsplit=1)[1]
    resolved_path = tmp_target / "resolved-roster.toml"
    resolved_path.write_text(emitted)

    resolved = roster.load_roster(resolved_path)

    assert resolved.agents["coder"].stats == {
        "source": "local-receipts",
        "median_duration_seconds": "12",
        "failure_rate": "0.000",
        "sample_count": "1",
    }


def test_roster_doctor_labels_author_default_stats_without_local_receipts(tmp_target, capsys):
    _write_roster(
        tmp_target,
        'orchestrator = "chef"\n'
        "[agents.chef]\n"
        'cli = "codex"\n'
        'role = "plan"\n'
        'stats = { speed = "medium", source = "author-receipts-2026-07" }\n',
    )

    assert roster_cmd.doctor(tmp_target) == 0
    out = capsys.readouterr().out

    assert "source=author-default" in out
    assert "source=local-receipts" not in out


def test_roster_doctor_overlays_local_receipt_stats(tmp_target, capsys):
    _write_roster(
        tmp_target,
        'orchestrator = "chef"\n'
        "[agents.chef]\n"
        'cli = "codex"\n'
        'role = "plan"\n'
        'stats = { speed = "medium", source = "author-receipts-2026-07" }\n',
    )
    runs = _runs_root(tmp_target)
    _write_worker_run(
        runs,
        "run-a",
        results=[
            {"worker": "chef", "task": "a", "ok": True, "detail": "", "text": "ok", "duration_seconds": 8.0},
        ],
    )

    assert roster_cmd.doctor(tmp_target) == 0
    out = capsys.readouterr().out

    assert "source=local-receipts" in out
    assert "chef" in out
    assert "median_duration" in out


@pytest.mark.parametrize("preset_name", EXPECTED_PRESET_NAMES)
def test_packaged_presets_remain_byte_identical_after_suggest_and_stats(tmp_target, preset_name):
    preset = _preset_path(preset_name)
    original = preset.read_bytes()
    probe = _installed_probe("codex", "claude", "cursor", "grok", "ollama", "antigravity")

    roster_cmd.suggest(tmp_target, preset=preset, probe=probe)
    roster_cmd.stats(tmp_target)

    assert preset.read_bytes() == original
