from __future__ import annotations

import json
from pathlib import Path

from brigade.research.doctor import doctor_payload
from brigade.roster import Agent, Roster


REQUIRED_LANE_FIELDS = {
    "capability",
    "configured",
    "seat",
    "cli",
    "executable",
    "version",
    "auth_status",
    "requested_model",
    "model_attestation",
    "read_only",
    "timeout_seconds",
    "concurrency",
    "last_live_smoke",
    "status",
    "detail",
}


def doctor_roster() -> Roster:
    return Roster(
        orchestrator="chef",
        agents={
            "chef": Agent(name="chef", cli="codex", role="orchestrator"),
            "luna": Agent(
                name="luna",
                cli="codex",
                role="researcher",
                model="gpt-5.6-luna",
                capabilities=(
                    "research.plan",
                    "research.extract",
                    "research.synthesize",
                    "research.review",
                ),
            ),
            "gemini_browser": Agent(
                name="gemini_browser",
                cli="oracle",
                role="browser researcher",
                model="gemini-3.1-pro",
                capabilities=("research.synthesize", "research.browser-discover"),
                read_only_capable=True,
            ),
        },
    )


def _assert_fixed_lanes(payload: dict) -> None:
    assert [lane["capability"] for lane in payload["lanes"]] == [
        "research.plan",
        "research.extract",
        "research.synthesize",
        "research.review",
        "research.browser-discover",
    ]
    for lane in payload["lanes"]:
        assert REQUIRED_LANE_FIELDS <= set(lane)


def test_doctor_reports_each_lane_without_profile_paths(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("brigade.research.doctor.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("brigade.research.llm.shutil.which", lambda name: f"/usr/bin/{name}")
    payload = doctor_payload(
        tmp_path,
        roster=doctor_roster(),
        probe=lambda _agent: {
            "auth_status": "authenticated",
            "detail": "profile=/home/alice/.config/browser secret-cookie-value Bearer abcdef",
        },
    )

    assert payload["schema"] == "brigade.research.doctor.v1"
    _assert_fixed_lanes(payload)
    gemini = next(lane for lane in payload["lanes"] if lane["capability"] == "research.browser-discover")
    assert gemini["concurrency"] == 1
    assert gemini["read_only"] is True
    assert "/home/" not in json.dumps(payload)
    assert "secret-cookie-value" not in json.dumps(payload)
    assert "Bearer " not in json.dumps(payload)


def test_doctor_fails_when_required_lane_missing(tmp_path: Path) -> None:
    roster = Roster(
        orchestrator="chef",
        agents={
            "chef": Agent(name="chef", cli="codex", role="orchestrator"),
            "luna": Agent(
                name="luna",
                cli="codex",
                role="researcher",
                model="gpt-5.6-luna",
                capabilities=("research.plan", "research.extract"),
            ),
        },
    )
    payload = doctor_payload(
        tmp_path,
        roster=roster,
        probe=lambda _agent: {"auth_status": "authenticated", "detail": "ok"},
    )
    assert payload["status"] == "fail"
    _assert_fixed_lanes(payload)


def test_doctor_preserves_browser_auth_classification(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("brigade.research.doctor.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("brigade.research.llm.shutil.which", lambda name: f"/usr/bin/{name}")
    payload = doctor_payload(
        tmp_path,
        roster=doctor_roster(),
        probe=lambda agent: (
            {
                "auth_status": "unauthenticated",
                "failure_kind": "browser-auth",
                "detail": "browser authentication failed",
            }
            if agent.cli == "oracle"
            else {"auth_status": "authenticated", "detail": "ok"}
        ),
    )
    gemini = next(lane for lane in payload["lanes"] if lane["capability"] == "research.browser-discover")
    assert gemini.get("failure_kind") == "browser-auth"
    synth = next(lane for lane in payload["lanes"] if lane["capability"] == "research.synthesize")
    # Oracle primary fails; Luna fallback is healthy under grounded -> warn
    assert synth["status"] == "warn"
    assert payload["status"] == "warn"


def test_doctor_grounded_synthesis_prefers_oracle_then_luna_fallback(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("brigade.research.doctor.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("brigade.research.llm.shutil.which", lambda name: f"/usr/bin/{name}")
    probed: list[str] = []

    def probe(agent: Agent) -> dict[str, object]:
        probed.append(agent.name)
        if agent.name == "gemini_browser":
            return {"auth_status": "missing", "detail": "oracle missing"}
        return {"auth_status": "authenticated", "detail": "ok", "executable": f"/usr/bin/{agent.cli}"}

    payload = doctor_payload(tmp_path, roster=doctor_roster(), profile_name="grounded", probe=probe)
    synth = next(lane for lane in payload["lanes"] if lane["capability"] == "research.synthesize")
    assert synth["seat"] == "gemini_browser"
    assert synth["status"] == "warn"
    assert synth.get("fallback_seat") == "luna" or "luna" in json.dumps(synth)
    assert payload["status"] == "warn"
    # Each selected primary/fallback probed at most once
    assert probed.count("gemini_browser") == 1
    assert probed.count("luna") == 1


def test_doctor_grounded_healthy_oracle_unhealthy_luna_fallback_is_warn(monkeypatch, tmp_path: Path) -> None:
    """Optional configured fallback unavailable still warns when primary is healthy."""
    monkeypatch.setattr("brigade.research.doctor.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("brigade.research.llm.shutil.which", lambda name: f"/usr/bin/{name}")
    # Keep plan/extract/review on a separate healthy seat so unhealthy Luna only
    # appears as the synthesis fallback (probe cache is per seat name).
    roster = Roster(
        orchestrator="chef",
        agents={
            "chef": Agent(
                name="chef",
                cli="codex",
                role="orchestrator",
                capabilities=(
                    "research.plan",
                    "research.extract",
                    "research.review",
                ),
            ),
            "luna": Agent(
                name="luna",
                cli="codex",
                role="researcher",
                model="gpt-5.6-luna",
                capabilities=("research.synthesize",),
            ),
            "gemini_browser": Agent(
                name="gemini_browser",
                cli="oracle",
                role="browser researcher",
                model="gemini-3.1-pro",
                capabilities=("research.synthesize", "research.browser-discover"),
                read_only_capable=True,
            ),
        },
    )

    def probe(agent: Agent) -> dict[str, object]:
        if agent.name == "luna":
            return {"auth_status": "missing", "detail": "luna unavailable"}
        return {
            "auth_status": "authenticated",
            "detail": "ok",
            "executable": f"/usr/bin/{agent.cli}",
        }

    payload = doctor_payload(
        tmp_path,
        roster=roster,
        profile_name="grounded",
        probe=probe,
    )
    synth = next(lane for lane in payload["lanes"] if lane["capability"] == "research.synthesize")
    assert synth["seat"] == "gemini_browser"
    assert synth["status"] == "warn"
    assert synth.get("fallback_seat") == "luna"
    assert synth.get("fallback_status") == "fail"
    assert payload["status"] == "warn"


def test_doctor_healthy_primary_without_resolved_fallback_stays_ok(monkeypatch, tmp_path: Path) -> None:
    """No warn for missing fallback when the resolved lane declares none."""
    monkeypatch.setattr("brigade.research.doctor.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("brigade.research.llm.shutil.which", lambda name: f"/usr/bin/{name}")
    roster = Roster(
        orchestrator="chef",
        agents={
            "chef": Agent(name="chef", cli="codex", role="orchestrator"),
            "luna": Agent(
                name="luna",
                cli="codex",
                role="researcher",
                model="gpt-5.6-luna",
                capabilities=(
                    "research.plan",
                    "research.extract",
                    "research.synthesize",
                    "research.review",
                ),
            ),
        },
    )
    payload = doctor_payload(
        tmp_path,
        roster=roster,
        profile_name="grounded",
        probe=lambda _agent: {
            "auth_status": "authenticated",
            "detail": "ok",
            "executable": "/usr/bin/codex",
        },
    )
    synth = next(lane for lane in payload["lanes"] if lane["capability"] == "research.synthesize")
    assert synth["seat"] == "luna"
    assert synth["status"] == "ok"
    assert "fallback_seat" not in synth
    assert payload["status"] == "warn"  # optional browser-discover missing


def test_doctor_disallowed_synthesis_fallback_fails_when_primary_unhealthy(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("brigade.research.doctor.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("brigade.research.llm.shutil.which", lambda name: f"/usr/bin/{name}")
    payload = doctor_payload(
        tmp_path,
        roster=doctor_roster(),
        profile_name="luna-only",
        probe=lambda agent: (
            {
                "auth_status": "missing",
                "detail": "down",
            }
            if agent.name == "gemini_browser"
            else {"auth_status": "authenticated", "detail": "ok", "executable": f"/usr/bin/{agent.cli}"}
        ),
    )
    synth = next(lane for lane in payload["lanes"] if lane["capability"] == "research.synthesize")
    assert synth["seat"] == "gemini_browser"
    assert synth["status"] == "fail"
    assert payload["status"] == "fail"


def test_doctor_uses_profile_candidate_order(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("brigade.research.doctor.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("brigade.research.llm.shutil.which", lambda name: f"/usr/bin/{name}")
    (tmp_path / ".brigade").mkdir()
    (tmp_path / ".brigade" / "research.toml").write_text(
        """
[research]
default_profile = "grounded"

[profiles.grounded]
planner = ["luna"]
extractor = ["luna"]
synthesizer = ["luna", "gemini_browser"]
reviewer = ["luna"]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    payload = doctor_payload(
        tmp_path,
        roster=doctor_roster(),
        profile_name="grounded",
        probe=lambda _agent: {
            "auth_status": "authenticated",
            "detail": "ok",
            "executable": "/usr/bin/codex",
        },
    )
    synth = next(lane for lane in payload["lanes"] if lane["capability"] == "research.synthesize")
    assert synth["seat"] == "luna"
    assert synth["status"] == "ok"


def test_doctor_browser_ai_requires_browser_discovery(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("brigade.research.doctor.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("brigade.research.llm.shutil.which", lambda name: f"/usr/bin/{name}")
    roster = Roster(
        orchestrator="chef",
        agents={
            "chef": Agent(name="chef", cli="codex", role="orchestrator"),
            "luna": Agent(
                name="luna",
                cli="codex",
                role="researcher",
                model="gpt-5.6-luna",
                capabilities=(
                    "research.plan",
                    "research.extract",
                    "research.synthesize",
                    "research.review",
                ),
            ),
        },
    )
    payload = doctor_payload(
        tmp_path,
        roster=roster,
        profile_name="browser-ai",
        probe=lambda _agent: {"auth_status": "authenticated", "detail": "ok", "executable": "/usr/bin/codex"},
    )
    browser = next(lane for lane in payload["lanes"] if lane["capability"] == "research.browser-discover")
    assert browser["status"] == "fail"
    assert payload["status"] == "fail"


def test_doctor_grounded_optional_browser_unavailable_is_warn(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("brigade.research.doctor.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("brigade.research.llm.shutil.which", lambda name: f"/usr/bin/{name}")
    roster = Roster(
        orchestrator="chef",
        agents={
            "chef": Agent(name="chef", cli="codex", role="orchestrator"),
            "luna": Agent(
                name="luna",
                cli="codex",
                role="researcher",
                model="gpt-5.6-luna",
                capabilities=(
                    "research.plan",
                    "research.extract",
                    "research.synthesize",
                    "research.review",
                ),
            ),
        },
    )
    payload = doctor_payload(
        tmp_path,
        roster=roster,
        profile_name="grounded",
        probe=lambda _agent: {"auth_status": "authenticated", "detail": "ok", "executable": "/usr/bin/codex"},
    )
    browser = next(lane for lane in payload["lanes"] if lane["capability"] == "research.browser-discover")
    assert browser["status"] == "warn"
    assert payload["status"] == "warn"
    assert payload["status"] != "fail"


def test_doctor_redacts_secret_keys_recursively(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("brigade.research.doctor.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("brigade.research.llm.shutil.which", lambda name: f"/usr/bin/{name}")
    payload = doctor_payload(
        tmp_path,
        roster=doctor_roster(),
        probe=lambda _agent: {
            "auth_status": "authenticated",
            "detail": {
                "note": "keep-me",
                "headers": {"Authorization": "Bearer nested-secret", "X-Safe": "ok"},
                "env": {"API_TOKEN": "env-secret"},
                "items": [
                    {"cookie": "cookie-secret", "label": "visible"},
                    {"token": "token-secret"},
                ],
                "browser_profile": "/home/alice/.mozilla/profiles/research",
                "api_key": "key-secret",
            },
            "executable": "/home/alice/bin/codex",
        },
    )
    dumped = json.dumps(payload)
    assert "nested-secret" not in dumped
    assert "env-secret" not in dumped
    assert "cookie-secret" not in dumped
    assert "token-secret" not in dumped
    assert "key-secret" not in dumped
    assert "/home/alice" not in dumped
    assert "keep-me" in dumped or "visible" in dumped
    _assert_fixed_lanes(payload)


def test_doctor_records_reason_when_discovery_unconfigured(monkeypatch, tmp_path: Path) -> None:
    from dataclasses import replace

    from brigade.research import doctor as doctor_mod
    from brigade.research.types import BUILTIN_PROFILES

    monkeypatch.setattr(doctor_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("brigade.research.llm.shutil.which", lambda name: f"/usr/bin/{name}")

    empty_discovery = replace(BUILTIN_PROFILES["grounded"], discovery=())

    class FakeResearchConfig:
        def profile(self, name: str | None = None) -> object:
            return empty_discovery

    monkeypatch.setattr(doctor_mod.rconfig, "load", lambda _target: FakeResearchConfig())

    payload = doctor_payload(
        tmp_path,
        roster=doctor_roster(),
        profile_name="grounded",
        probe=lambda _agent: {
            "auth_status": "authenticated",
            "detail": "ok",
            "executable": "/usr/bin/codex",
        },
    )

    assert payload["status"] == "fail"
    reasons = payload.get("reasons")
    assert isinstance(reasons, list) and reasons
    assert any("discovery" in reason.lower() for reason in reasons)


def test_probe_agent_is_read_only_and_marks_oracle_unauth(monkeypatch) -> None:
    from brigade.research import doctor as doctor_mod

    calls: list[object] = []

    class FakeCapability:
        authenticated = False
        detail = "oracle probe"
        auth_detail = "expired cookie"

    class FakeProbe:
        def lookup(self, cli_ref: str):
            calls.append(("lookup", cli_ref))
            return FakeCapability()

    monkeypatch.setattr(doctor_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(doctor_mod.roster_mod, "HostCapabilityProbe", FakeProbe)
    monkeypatch.setattr(
        doctor_mod.proc,
        "run",
        lambda *_args, **_kwargs: doctor_mod.proc.Result(0, "oracle 9.9.9\n", ""),
    )
    agent = Agent(name="gemini_browser", cli="oracle", role="browser researcher")
    result = doctor_mod.probe_agent(agent)
    assert result["auth_status"] == "unauthenticated"
    assert result["failure_kind"] == "browser-auth"
    assert result["version"] == "oracle 9.9.9"
    assert calls == [("lookup", "oracle")]


def test_probe_agent_reports_executable_version(monkeypatch) -> None:
    from brigade.research import doctor as doctor_mod

    class FakeCapability:
        authenticated = True
        detail = "codex ok"
        auth_detail = ""

    class FakeProbe:
        def lookup(self, cli_ref: str):
            return FakeCapability()

    runs: list[object] = []

    def fake_run(args, **kwargs):
        runs.append((list(args), kwargs.get("timeout")))
        return doctor_mod.proc.Result(0, "\n  codex-cli 1.2.3\nextra\n", "")

    monkeypatch.setattr(doctor_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(doctor_mod.roster_mod, "HostCapabilityProbe", FakeProbe)
    monkeypatch.setattr(doctor_mod.proc, "run", fake_run)
    agent = Agent(name="luna", cli="codex", role="researcher")
    result = doctor_mod.probe_agent(agent)
    assert result["auth_status"] == "authenticated"
    assert result["version"] == "codex-cli 1.2.3"
    assert runs == [(["/usr/bin/codex", "--version"], doctor_mod._VERSION_PROBE_TIMEOUT)]


def test_probe_agent_version_stays_none_when_probe_fails(monkeypatch) -> None:
    from brigade.research import doctor as doctor_mod

    class FakeCapability:
        authenticated = True
        detail = "codex ok"
        auth_detail = ""

    class FakeProbe:
        def lookup(self, cli_ref: str):
            return FakeCapability()

    monkeypatch.setattr(doctor_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(doctor_mod.roster_mod, "HostCapabilityProbe", FakeProbe)
    monkeypatch.setattr(
        doctor_mod.proc,
        "run",
        lambda *_args, **_kwargs: doctor_mod.proc.Result(124, "", "timeout after 2.0s"),
    )
    agent = Agent(name="luna", cli="codex", role="researcher")
    result = doctor_mod.probe_agent(agent)
    assert result["auth_status"] == "authenticated"
    assert result["version"] is None
    assert "failure_kind" not in result


def test_probe_agent_redacts_version_output(monkeypatch) -> None:
    from brigade.research import doctor as doctor_mod

    class FakeCapability:
        authenticated = False
        detail = "oracle probe"
        auth_detail = "expired cookie"

    class FakeProbe:
        def lookup(self, cli_ref: str):
            return FakeCapability()

    monkeypatch.setattr(doctor_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(doctor_mod.roster_mod, "HostCapabilityProbe", FakeProbe)
    monkeypatch.setattr(
        doctor_mod.proc,
        "run",
        lambda *_args, **_kwargs: doctor_mod.proc.Result(
            0,
            "oracle /home/alice/.config/browser Bearer abcdef token=sekrit\n",
            "",
        ),
    )
    agent = Agent(name="gemini_browser", cli="oracle", role="browser researcher")
    result = doctor_mod.probe_agent(agent)
    assert result["auth_status"] == "unauthenticated"
    assert result["failure_kind"] == "browser-auth"
    version = str(result["version"])
    assert "/home/" not in version
    assert "Bearer " not in version
    assert "sekrit" not in version
    assert "[redacted]" in version
