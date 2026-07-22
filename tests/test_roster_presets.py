from __future__ import annotations

from dataclasses import dataclass

import pytest

from brigade import acpx_adapter
from brigade import roster as roster_mod
from brigade import roster_resolution as resolution_mod


VALID_LEGACY = """
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


def _write(tmp_path, text: str):
    path = tmp_path / "roster.toml"
    path.write_text(text)
    return path


def test_legacy_roster_loads_without_preset_metadata(tmp_path):
    loaded = roster_mod.load_roster(_write(tmp_path, VALID_LEGACY))
    coder = loaded.agents["coder"]
    assert coder.purpose is None
    assert coder.spec is None
    assert coder.requires is None
    assert coder.fallback == ()
    assert coder.stats is None
    assert coder.caveats == ()


@pytest.mark.parametrize(
    "requires_text,match",
    [
        ('requires = "codex"', r"agents\.coder\.requires must be a TOML table"),
        ("requires = {}", None),
        ('requires = { auth = "logged-in" }', r"requires\.auth requires requires\.cli"),
        ('requires = { cli = "codex", extra = true }', r"unknown keys: extra"),
        ('requires = { cli = "" }', r"requires\.cli must be a non-empty string"),
        ('requires = { cli = "codex", auth = "oauth" }', r"requires\.auth must be 'logged-in'"),
        ('requires = { cli = "codex", auth = "" }', r"requires\.auth must be a non-empty string"),
    ],
)
def test_malformed_requires_rejected(tmp_path, requires_text, match):
    text = VALID_LEGACY.replace(
        'role = "write code"',
        f'role = "write code"\n{requires_text}',
    )
    if match is None:
        loaded = roster_mod.load_roster(_write(tmp_path, text))
        assert loaded.agents["coder"].requires is None
        return
    with pytest.raises(ValueError, match=match):
        roster_mod.load_roster(_write(tmp_path, text))


def test_preset_path_rejects_unknown_and_traversal():
    with pytest.raises(ValueError, match="unknown preset: 'missing'"):
        roster_mod.preset_path("missing")
    with pytest.raises(ValueError, match="unknown preset: '../x'"):
        roster_mod.preset_path("../x")
    with pytest.raises(ValueError, match="unknown preset: 'minimal/nested'"):
        roster_mod.preset_path("minimal/nested")


def test_preset_path_returns_packaged_file():
    path = roster_mod.preset_path("minimal")
    assert path.name == "minimal.toml"
    assert path.is_file()


@pytest.mark.parametrize("preset_id", sorted(roster_mod.BUNDLED_PRESET_IDS))
def test_bundled_presets_load_and_carry_required_metadata(preset_id):
    loaded = roster_mod.load_preset(preset_id)
    assert loaded.orchestrator in loaded.agents
    for name, agent in loaded.agents.items():
        assert agent.purpose, f"{preset_id}:{name} missing purpose"
        assert agent.spec, f"{preset_id}:{name} missing spec"
        assert agent.requires is not None and agent.requires.cli, f"{preset_id}:{name} missing hard requirements"
        assert agent.stats is not None and agent.stats.speed and agent.stats.source, (
            f"{preset_id}:{name} missing stats.speed/source"
        )
        assert agent.caveats is not None, f"{preset_id}:{name} missing caveats field"


def test_minimal_preset_has_non_orchestrator_worker():
    loaded = roster_mod.load_preset("minimal")
    workers = roster_mod.workers(loaded)
    assert workers
    assert all(worker.name != loaded.orchestrator for worker in workers)
    assert any(worker.name == "implement" for worker in workers)


def test_full_multi_lane_preset_has_review_lane_with_ordered_fallbacks():
    loaded = roster_mod.load_preset("full-multi-lane")
    review = loaded.agents["review"]
    assert review.fallback == ("review_claude", "review_antigravity")
    assert "implement" in loaded.agents
    assert "scout" in loaded.agents


@dataclass(frozen=True)
class FakeLookup:
    installed: frozenset[str]
    authenticated: bool = True
    ollama_present: frozenset[str] = frozenset()

    def check_requirements(self, agent: roster_mod.Agent) -> tuple[bool, str]:
        req = agent.requires
        if req is None or req.cli is None:
            return True, "seat has no hard requirements"
        if req.cli == "ollama":
            model = (agent.cli or "")[len("ollama:") :]
            if model in self.ollama_present:
                return True, f"ollama model {model!r} is pulled locally"
            return False, f"ollama model {model!r} is not pulled locally"
        if req.cli == "cursor":
            if "cursor" not in self.installed:
                return False, "cursor CLI is not installed"
            if req.auth == "logged-in" and not self.authenticated:
                return False, "cursor CLI is not authenticated"
            if req.auth == "logged-in":
                return True, "cursor CLI is installed and authenticated"
            return True, "cursor CLI is installed"
        if req.cli in self.installed:
            if req.auth == "logged-in":
                return False, f"no authentication probe is available for {req.cli} CLI"
            return True, f"{req.cli} CLI is installed"
        return False, f"{req.cli} CLI is not installed"


def _preset_roster(text: str, tmp_path):
    return roster_mod.load_roster(_write(tmp_path, text))


def test_resolution_emits_one_entry_per_requested_name_in_order(tmp_path):
    roster = _preset_roster(
        """
orchestrator = "chef"
[agents.chef]
cli = "codex"
role = "plan"
[agents.alpha]
cli = "codex"
role = "a"
[agents.beta]
cli = "codex"
role = "b"
[limits]
allow_models = ["codex"]
""",
        tmp_path,
    )
    report = resolution_mod.resolve_seats(roster, seat_names=["beta", "alpha", "missing"])
    assert [entry.requested for entry in report.entries] == ["beta", "alpha", "missing"]
    assert report.entries[0].selected == "beta"
    assert report.entries[1].selected == "alpha"
    assert report.entries[2].status == "dropped"
    assert report.entries[2].reason


def test_resolution_keeps_seat_without_requires(tmp_path):
    roster = _preset_roster(
        """
orchestrator = "chef"
[agents.chef]
cli = "codex"
role = "plan"
[agents.plain]
cli = "codex"
role = "plain worker"
[limits]
allow_models = ["codex"]
""",
        tmp_path,
    )
    lookup = FakeLookup(installed=set())
    report = resolution_mod.resolve_seats(roster, seat_names=["plain"], lookup=lookup)
    entry = report.entries[0]
    assert entry.requested == "plain"
    assert entry.selected == "plain"
    assert entry.status == "resolved"
    assert entry.reason == "seat has no hard requirements"


def test_default_resolution_uses_roster_workers_excluding_orchestrator(tmp_path):
    roster = _preset_roster(
        """
orchestrator = "chef"
[agents.chef]
cli = "codex"
role = "plan"
[agents.worker]
cli = "codex"
role = "work"
[limits]
allow_models = ["codex"]
""",
        tmp_path,
    )
    report = resolution_mod.resolve_seats(roster)
    assert [entry.requested for entry in report.entries] == ["worker"]


def test_ollama_missing_model_falls_back_in_order(tmp_path):
    roster = _preset_roster(
        """
orchestrator = "chef"
[agents.chef]
cli = "codex"
role = "plan"
requires = { cli = "codex" }
[agents.implement_oss]
cli = "ollama:llama3.2:3b"
role = "oss"
requires = { cli = "ollama" }
fallback = ["implement_codex"]
[agents.implement_codex]
cli = "codex"
role = "codex"
requires = { cli = "codex" }
[limits]
allow_models = ["codex", "ollama:*"]
""",
        tmp_path,
    )
    lookup = FakeLookup(installed={"codex"}, ollama_present=set())
    report = resolution_mod.resolve_seats(roster, seat_names=["implement_oss"], lookup=lookup)
    entry = report.entries[0]
    assert entry.status == "resolved"
    assert entry.selected == "implement_codex"
    assert entry.fallback_reasons[0].startswith("ollama model")
    assert entry.reason == "codex CLI is installed"
    assert all(reason for reason in entry.fallback_reasons)


def test_ollama_present_model_self_resolves_without_host_calls(monkeypatch, tmp_path):
    roster = _preset_roster(
        """
orchestrator = "chef"
[agents.chef]
cli = "codex"
role = "plan"
[agents.implement_oss]
cli = "ollama:llama3.2:3b"
role = "oss"
requires = { cli = "ollama" }
fallback = ["implement_codex"]
[agents.implement_codex]
cli = "codex"
role = "codex"
requires = { cli = "codex" }
[limits]
allow_models = ["codex", "ollama:*"]
""",
        tmp_path,
    )

    calls: list[str] = []

    def fake_present(model: str) -> tuple[bool, str]:
        calls.append(model)
        return True, ""

    lookup = resolution_mod.DefaultCapabilityLookup(
        cli_detect=lambda cli: True,
        ollama_model_present=fake_present,
        cursor_auth_status=lambda: acpx_adapter.CursorAuthStatus(
            "authenticated",
            "cursor-agent CLI is authenticated",
            "",
            "",
            0,
        ),
    )
    report = resolution_mod.resolve_seats(roster, seat_names=["implement_oss"], lookup=lookup)
    entry = report.entries[0]
    assert entry.selected == "implement_oss"
    assert calls == ["llama3.2:3b"]
    assert "llama3.2:3b" in entry.reason


def test_cursor_auth_probe_uses_requires_cli(monkeypatch, tmp_path):
    roster = _preset_roster(
        """
orchestrator = "chef"
[agents.chef]
cli = "codex"
role = "plan"
[agents.implement]
cli = "cursor"
model = "composer-2.5"
role = "implement"
requires = { cli = "cursor", auth = "logged-in" }
[limits]
allow_models = ["codex", "cursor"]
""",
        tmp_path,
    )
    auth_calls: list[str] = []

    def fake_auth():
        auth_calls.append("called")
        return acpx_adapter.CursorAuthStatus(
            "unauthenticated",
            "cursor-agent CLI is not logged in",
            "",
            "",
            1,
        )

    lookup = resolution_mod.DefaultCapabilityLookup(
        cli_detect=lambda cli: cli == "cursor",
        ollama_model_present=lambda model: (True, ""),
        cursor_auth_status=fake_auth,
    )
    report = resolution_mod.resolve_seats(roster, seat_names=["implement"], lookup=lookup)
    entry = report.entries[0]
    assert auth_calls == ["called"]
    assert entry.status == "dropped"
    assert entry.reason == "no satisfiable fallback for seat 'implement'"
    assert "not logged in" in entry.fallback_reasons[0]


def test_dropped_seat_preserves_ordered_fallback_reasons(tmp_path):
    roster = _preset_roster(
        """
orchestrator = "chef"
[agents.chef]
cli = "codex"
role = "plan"
[agents.primary]
cli = "cursor"
model = "composer-2.5"
role = "primary"
requires = { cli = "cursor", auth = "logged-in" }
fallback = ["missing", "secondary"]
[agents.secondary]
cli = "codex"
role = "secondary"
requires = { cli = "codex" }
[limits]
allow_models = ["cursor", "codex"]
""",
        tmp_path,
    )
    lookup = FakeLookup(installed=set(), authenticated=False)
    report = resolution_mod.resolve_seats(roster, seat_names=["primary"], lookup=lookup)
    entry = report.entries[0]
    assert entry.status == "dropped"
    assert entry.reason
    assert entry.fallback_reasons == (
        "cursor CLI is not installed",
        "fallback 'missing' is not defined in the roster",
        "codex CLI is not installed",
    )


def test_non_cursor_logged_in_auth_drops_without_treating_installed_as_authenticated(tmp_path):
    roster = _preset_roster(
        """
orchestrator = "chef"
[agents.chef]
cli = "codex"
role = "plan"
[agents.primary]
cli = "codex"
role = "primary"
requires = { cli = "codex", auth = "logged-in" }
fallback = ["secondary"]
[agents.secondary]
cli = "claude"
role = "secondary"
requires = { cli = "claude" }
[limits]
allow_models = ["codex", "claude"]
""",
        tmp_path,
    )
    lookup = resolution_mod.DefaultCapabilityLookup(
        cli_detect=lambda cli: cli in {"codex", "claude"},
        ollama_model_present=lambda model: (True, ""),
        cursor_auth_status=lambda: acpx_adapter.CursorAuthStatus(
            "authenticated",
            "cursor-agent CLI is authenticated",
            "",
            "",
            0,
        ),
    )
    report = resolution_mod.resolve_seats(roster, seat_names=["primary"], lookup=lookup)
    entry = report.entries[0]
    assert entry.status == "resolved"
    assert entry.selected == "secondary"
    assert entry.fallback_reasons[0] == "no authentication probe is available for codex CLI"
    assert entry.reason == "claude CLI is installed"


def test_non_cursor_logged_in_auth_drops_when_no_fallback(tmp_path):
    roster = _preset_roster(
        """
orchestrator = "chef"
[agents.chef]
cli = "codex"
role = "plan"
[agents.primary]
cli = "codex"
role = "primary"
requires = { cli = "codex", auth = "logged-in" }
[limits]
allow_models = ["codex"]
""",
        tmp_path,
    )
    lookup = resolution_mod.DefaultCapabilityLookup(
        cli_detect=lambda cli: cli == "codex",
        ollama_model_present=lambda model: (True, ""),
        cursor_auth_status=lambda: acpx_adapter.CursorAuthStatus(
            "authenticated",
            "cursor-agent CLI is authenticated",
            "",
            "",
            0,
        ),
    )
    report = resolution_mod.resolve_seats(roster, seat_names=["primary"], lookup=lookup)
    entry = report.entries[0]
    assert entry.status == "dropped"
    assert entry.selected is None
    assert entry.fallback_reasons == ("no authentication probe is available for codex CLI",)
