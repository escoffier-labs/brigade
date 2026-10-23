"""Roster doctor must bound hanging health probes (issue #1481).

A probe subprocess that never exits (a wedged agent CLI behind a .ps1 shim
or an npm wrapper on Windows) must not stall ``brigade roster doctor`` and
must not be orphaned: every probe runs through ``brigade.proc`` under an
explicit timeout, the timeout surfaces as a distinct ``timeout`` health
value, and the whole probe process tree is terminated.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest
from pathlib import Path

from brigade import model_inventory, roster_cmd


def _hanging_command(pid_file: Path) -> list[str]:
    # A small Python child that sleeps, plus a grandchild that sleeps longer:
    # killing only the direct child would orphan the grandchild, which is the
    # original Windows-fleet failure. argv[0] identifies the probe child so
    # the test can assert the exact process is gone after doctor returns.
    code = (
        "import subprocess, sys, time; "
        "grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)']); "
        f"open({str(pid_file)!r}, 'w').write(str(grandchild.pid)); "
        "time.sleep(120)"
    )
    return [sys.executable, "-c", code]


def _write_hanging_cursor_roster(tmp_target: Path, pid_file: Path) -> None:
    command = _hanging_command(pid_file)
    command_toml = "[" + ", ".join(json.dumps(part) for part in command) + "]"
    path = tmp_target / ".brigade" / "roster.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        'orchestrator = "chef"\n'
        "[agents.chef]\n"
        'cli = "cursor"\n'
        'model = "composer-2.5"\n'
        'role = "plan"\n'
        f"command = {command_toml}\n"
    )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def test_roster_doctor_bounds_hanging_probe_and_reaps_its_tree(monkeypatch, tmp_target, tmp_path, capsys):
    # Keep the developer machine's real user roster out of the probe path.
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "empty-home")
    # Exercise the real timeout and tree reap without waiting the inventory default.
    monkeypatch.setattr(model_inventory, "_INVENTORY_TIMEOUT_SECONDS", 1.0)
    pid_file = tmp_path / "grandchild.pid"
    _write_hanging_cursor_roster(tmp_target, pid_file)
    # The suite pins proc.which to a bare host; resolve only the injected
    # hanging probe command so detection sees a runnable executable.
    from brigade import agents

    monkeypatch.setattr(agents.proc, "which", lambda cmd: cmd if cmd == sys.executable else None)

    spawned: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    def spy_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", spy_popen)

    started = time.monotonic()
    rc = roster_cmd.doctor(tmp_target)
    elapsed = time.monotonic() - started
    out = capsys.readouterr().out

    # The hung inventory probe is advisory: doctor lists every seat and exits
    # fine, well within the 60-second acceptance bound for a hanging agent CLI,
    # reporting the distinct timeout state rather than blocking or raising.
    assert rc == 0
    assert elapsed < 60
    assert "agent: chef model inventory" in out
    assert "model inventory  timeout:" in out

    probe_children = [process for process in spawned if process.args and list(process.args)[0] == sys.executable]
    assert probe_children, "expected the hanging probe command to be spawned"
    for process in probe_children:
        assert process.poll() is not None, "hanging probe child must be reaped"
        if os.name == "posix":
            assert not _pid_alive(process.pid), "hanging probe child must not survive"
    if os.name == "posix":
        # The grandchild outlives a direct-child-only kill: the probe tree
        # kill must reap it too, or it lingers like the fleet orphans.
        grandchild_pid = int(pid_file.read_text().strip())
        assert not _pid_alive(grandchild_pid), "probe grandchild must not be orphaned"


def test_hung_grok_inventory_surfaces_distinct_timeout_state(monkeypatch):
    from brigade import agents

    monkeypatch.setattr(
        model_inventory.proc,
        "run",
        lambda argv, **kwargs: agents.proc.Result(124, "", "timeout after 15.0s"),
    )

    result = model_inventory.ModelInventoryInspector().inspect("grok", "grok-4.5")

    assert result is not None
    assert result.state == "timeout"
    assert "timed out" in result.detail


def test_ready_inspects_only_selected_seat_without_inference_or_persistent_writes(tmp_path, monkeypatch, capsys):
    import json

    from brigade import agents, seat_health

    path = tmp_path / ".brigade" / "roster.toml"
    path.parent.mkdir()
    path.write_text(
        'orchestrator = "chef"\n'
        '[agents.chef]\ncli = "codex"\nrole = "plan"\n'
        '[agents.worker]\ncli = "codex"\nrole = "implement"\nmodel = "example-model"\n'
        '[agents.unused]\ncli = "codex"\nrole = "research"\n'
    )
    before = {file.relative_to(tmp_path): file.read_bytes() for file in tmp_path.rglob("*") if file.is_file()}
    probed = []
    original = seat_health.SeatHealthProbe.probe

    def recording_probe(self, seat, roster, **kwargs):
        probed.append(seat.name)
        return original(self, seat, roster, **kwargs)

    def forbidden_inference(*args, **kwargs):
        raise AssertionError("readiness inspection attempted inference")

    monkeypatch.setattr(seat_health.SeatHealthProbe, "probe", recording_probe)
    monkeypatch.setattr(agents, "run_agent", forbidden_inference)
    rc = roster_cmd.ready(tmp_path, workers=("worker",), json_output=True)
    result = json.loads(capsys.readouterr().out)
    assert rc == 2  # bare-host fixture has no installed worker executable
    assert probed == ["worker"]
    assert result["roster"]["source"] == "workspace"
    assert result["admitted_capacity"] is None
    assert any(item["category"] == "model-reachability" and item["status"] == "unknown" for item in result["evidence"])
    assert {file.relative_to(tmp_path): file.read_bytes() for file in tmp_path.rglob("*") if file.is_file()} == before


@pytest.mark.parametrize("target_kind", ["missing", "file"])
@pytest.mark.parametrize("roster_source", ["explicit", "user-fallback"])
@pytest.mark.parametrize("output_format", ["json", "text"])
def test_ready_cli_rejects_unusable_target_before_roster_or_provider_probe(
    tmp_path, monkeypatch, capsys, target_kind, roster_source, output_format
):
    from brigade import cli, roster, seat_health

    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    user_roster = home / ".brigade" / "roster.toml"
    user_roster.parent.mkdir(parents=True)
    roster_text = (
        'orchestrator = "chef"\n[agents.chef]\ncli = "codex"\nrole = "plan"\n'
        '[agents.coder]\ncli = "codex"\nrole = "implement"\n'
    )
    user_roster.write_text(roster_text)

    explicit_roster = tmp_path / "explicit-roster.toml"
    explicit_roster.write_text(roster_text)
    if target_kind == "missing":
        target = tmp_path / "missing-workspace"
        assert not target.exists()
    else:
        target = tmp_path / "workspace-file"
        target.write_text("not a workspace directory")

    calls = []
    resolve_roster = roster.resolve_roster

    def recording_resolve(target_arg, explicit=None):
        calls.append("resolve_roster")
        return resolve_roster(target_arg, explicit)

    def recording_probe(*args, **kwargs):
        calls.append("provider_health_probe")
        raise AssertionError("provider health probe ran for an unusable workspace")

    monkeypatch.setattr(roster, "resolve_roster", recording_resolve)
    monkeypatch.setattr(seat_health.SeatHealthProbe, "probe_roster", recording_probe)
    args = ["roster", "ready", "--target", str(target), "--worker", "coder"]
    if output_format == "json":
        args.append("--json")
    if roster_source == "explicit":
        args.extend(["--roster", str(explicit_roster)])

    assert cli.main(args) == 2
    output = capsys.readouterr().out
    if output_format == "json":
        report = json.loads(output)
        assert report["status"] == "blocked"
        assert report["exit_code"] == 2
        assert report["limiting_reason"] == "target-unusable"
        assert report["evidence"][0]["category"] == "target"
        assert report["evidence"][0]["reason"] == "target-not-directory"
        assert str(target) in report["evidence"][0]["detail"]
    else:
        assert "readiness: blocked (exit 2)" in output
        assert "target: failed freshness=current target-not-directory" in output
        assert str(target) in output
        assert "roster-unavailable" not in output
    assert calls == []
    assert not target.is_dir()


@pytest.mark.parametrize(
    "worker,expected,role",
    [
        ("worker", 2, "orchestrator"),
        ("chef", 3, "worker"),
    ],
)
def test_ready_uses_cached_preference_without_refresh(tmp_path, monkeypatch, capsys, worker, expected, role):
    from brigade import fleet_client, roster, run_preference, seat_health

    path = tmp_path / "roster.toml"
    path.write_text(
        'orchestrator = "chef"\n[agents.chef]\ncli = "codex"\nrole = "plan"\n'
        '[agents.worker]\ncli = "codex"\nrole = "implement"\n'
    )
    run_preference.write_cached(run_preference.RunPreference(chef="worker"))

    def forbidden(*args, **kwargs):
        raise AssertionError("readiness attempted a network refresh")

    class HealthyProbe:
        def probe_roster(self, selected, **kwargs):
            return [
                seat_health.SeatHealthResult(
                    "test",
                    name,
                    seat_health.seat_fingerprint(agent, selected),
                    "healthy",
                    {},
                    tuple(seat_health.SeatHealthCheck(check, "passed") for check in seat_health.CHECK_NAMES),
                    0,
                    0,
                    time.time(),
                    time.time(),
                )
                for name, agent in selected.agents.items()
            ]

    monkeypatch.setattr(run_preference, "refresh_cache", forbidden)
    monkeypatch.setattr(fleet_client, "load_model_policy_snapshot", forbidden)
    before = path.read_bytes()
    assert (
        roster_cmd.ready(tmp_path, workers=(worker,), roster_path=path, health_probe=HealthyProbe(), json_output=True)
        == expected
    )
    report = json.loads(capsys.readouterr().out)
    assert report["selected_seats"][0]["selection_role"] == role
    assert path.read_bytes() == before
    assert roster.load_roster(path).orchestrator == "chef"


@pytest.mark.parametrize("hub_source,expected", [("none", 2), ("env", 3), ("file", 3)])
def test_ready_missing_local_worker_respects_hub_discovery_uncertainty(
    tmp_path, monkeypatch, capsys, hub_source, expected
):
    from brigade import fleet_client, seat_health

    path = tmp_path / "roster.toml"
    path.write_text('orchestrator = "chef"\n[agents.chef]\ncli = "codex"\nrole = "plan"\n')
    if hub_source == "env":
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "https://hub.example.invalid")
    elif hub_source == "file":
        fleet_path = fleet_client.brigade_home() / "fleet.toml"
        fleet_path.parent.mkdir(parents=True, exist_ok=True)
        fleet_path.write_text('[fleet]\nhub_url = "https://hub.example.invalid"\n')

    def forbidden(*args, **kwargs):
        raise AssertionError("unresolved local selection attempted provider or Hub discovery")

    monkeypatch.setattr(fleet_client, "load_model_policy_snapshot", forbidden)
    monkeypatch.setattr(seat_health, "SeatHealthProbe", forbidden)
    assert roster_cmd.ready(tmp_path, workers=("remote",), roster_path=path, json_output=True) == expected
    report = json.loads(capsys.readouterr().out)
    selection = next(item for item in report["evidence"] if item["category"] == "selection")
    assert selection["reason"] == ("worker-missing" if hub_source == "none" else "worker-discovery-unverified")
    assert report["roster"]["discovery"] == "local-cache-only"
    assert report["roster"]["local_authoritative"] == (hub_source == "none")
