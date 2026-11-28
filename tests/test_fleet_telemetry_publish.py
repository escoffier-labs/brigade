"""The timer publisher can be exercised without contacting the fleet."""

import json
import runpy
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "ops/fleet-telemetry/fleet-telemetry-publish"


@pytest.fixture
def publisher():
    return runpy.run_path(str(SCRIPT))["main"].__globals__


def test_cpu_sampling_excludes_io_wait_and_guest_and_counts_steal(publisher, tmp_path, monkeypatch):
    stat = tmp_path / "stat"
    stat.write_text("cpu  100 10 40 500 50 1 2 0 25 5\ncpu0 100 10 40 500 50 1 2 0 25 5\n")

    def advance(seconds):
        assert seconds == 1
        stat.write_text("cpu  120 10 50 540 80 1 2 3 35 5\n")

    monkeypatch.setattr(publisher["time"], "sleep", advance)
    assert publisher["_cpu_util"](stat) == 0.320


@pytest.mark.parametrize(
    ("meminfo", "expected"),
    [
        ("MemTotal: 1000\nMemAvailable: 200\nSwapTotal: 100\nSwapFree: 25", (0.8, 0.75)),
        ("MemTotal: 1000\nMemAvailable: 200\nSwapTotal: 0", (0.8, 0.0)),
        ("MemTotal: 1000\nSwapTotal: 100\nSwapFree: 25", (None, 0.75)),
        ("MemTotal: 0\nMemAvailable: 0\nSwapTotal: 100\nSwapFree: 25", (None, 0.75)),
        ("MemTotal: 1000\nMemAvailable: 200\nSwapTotal: 100", (0.8, None)),
    ],
)
def test_partial_meminfo(publisher, tmp_path, monkeypatch, meminfo, expected):
    (tmp_path / "meminfo").write_text(meminfo)
    monkeypatch.setitem(publisher, "_cpu_util", lambda _: 0.123)
    body = publisher["_load_fields"](tmp_path)
    for field, value in zip(("memory_pressure", "swap_pressure"), expected, strict=True):
        if value is None:
            assert field not in body
        else:
            assert body[field] == pytest.approx(value)
    assert body["load"] == 0.123


def test_short_cpu_line_falls_back_without_losing_memory(publisher, tmp_path, monkeypatch):
    (tmp_path / "stat").write_text("cpu 100 10 40 500\n")
    (tmp_path / "meminfo").write_text("MemTotal: 1000\nMemAvailable: 100\nSwapTotal: 0")
    monkeypatch.setitem(publisher, "_load", lambda: 0.96)
    body = publisher["_load_fields"](tmp_path)
    assert body["load"] == 0.96
    assert "cpu_util" not in body
    assert body["memory_pressure"] == 0.9 and body["swap_pressure"] == 0
    assert body["status"] == "busy"
    assert body["busy_reasons"] == ["load", "memory_pressure"]


@pytest.mark.parametrize(
    ("cpu", "memory", "swap", "reasons"),
    [
        (0.95, 0, 0, ["cpu_util"]),
        (0.94, 0.90, 0, ["memory_pressure"]),
        (0.94, 0.80, 0.50, ["swap_pressure"]),
        (0.94, 0.79, 0.99, []),
        (0.94, 0.80, 0.49, []),
        (0.95, 0.90, 0.50, ["cpu_util", "memory_pressure", "swap_pressure"]),
    ],
)
def test_status_rules(publisher, monkeypatch, tmp_path, cpu, memory, swap, reasons):
    (tmp_path / "stat").touch()
    monkeypatch.setitem(publisher, "_load", lambda: 1.0)
    monkeypatch.setitem(publisher, "_cpu_util", lambda _: cpu)
    monkeypatch.setitem(publisher, "_memory_and_swap", lambda _: (memory, swap))
    body = publisher["_load_fields"](tmp_path)
    assert body["status"] == ("busy" if reasons else "available")
    assert body["busy_reasons"] == reasons
    assert body["load"] == cpu


def test_missing_proc_retains_legacy_fields(publisher, tmp_path, monkeypatch):
    monkeypatch.setitem(publisher, "_load", lambda: 0.96)
    assert publisher["_load_fields"](tmp_path) == {"load": 0.96, "status": "busy"}


def test_dry_run_prints_publish_body_without_hooks(publisher, monkeypatch, capsys, tmp_path):
    (tmp_path / "stat").touch()
    monkeypatch.setitem(publisher, "Path", lambda _: tmp_path)

    def read_policy(args):
        assert args == ["fleet", "policy", "show", "--json"]
        return '{"machines": {"worker": {"node_id": "node-fixture"}}}'

    def unexpected_hook():
        pytest.fail("dry-run executed the observer hook")

    monkeypatch.setitem(publisher, "_node_id", lambda: "node-fixture")
    monkeypatch.setitem(publisher, "_run", read_policy)
    monkeypatch.setitem(publisher, "_observe_t3", unexpected_hook)
    monkeypatch.setitem(publisher, "_load", lambda: 0.99)
    monkeypatch.setitem(publisher, "_cpu_util", lambda _: 0.30)
    monkeypatch.setitem(publisher, "_memory_and_swap", lambda _: (0.60, 0.5))
    assert publisher["main"](["--dry-run"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body.pop("observed_at").endswith("Z")
    assert body == {
        "ttl_seconds": 300,
        "status": "available",
        "load": 0.30,
        "cpu_util": 0.30,
        "memory_pressure": 0.60,
        "swap_pressure": 0.50,
        "busy_reasons": [],
        "running": {"session_ids": [], "run_ids": []},
        "active_claims": [],
        "usable_seats": [],
        "credential_state": "ok",
    }
