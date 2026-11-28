"""The timer publisher can be exercised without contacting the fleet."""

import json
import runpy
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "ops/fleet-telemetry/fleet-telemetry-publish"


@pytest.fixture
def publisher():
    return runpy.run_path(str(SCRIPT))["main"].__globals__


def test_cpu_sampling_excludes_io_wait_and_does_not_double_count_guest(publisher, tmp_path, monkeypatch):
    stat = tmp_path / "stat"
    stat.write_text("cpu  100 10 40 500 50 1 2 0 25 5\ncpu0 100 10 40 500 50 1 2 0 25 5\n")

    def advance(seconds):
        assert seconds == 1
        stat.write_text("cpu  120 10 50 540 80 1 2 0 35 5\n")

    monkeypatch.setattr(publisher["time"], "sleep", advance)
    assert publisher["_cpu_util"](stat) == pytest.approx(0.30)


@pytest.mark.parametrize(("total", "free", "expected"), [(100, 25, 0.75), (0, 0, 0.0)])
def test_memory_sampling(publisher, tmp_path, total, free, expected):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(f"MemTotal: 1000 kB\nMemAvailable: 200 kB\nSwapTotal: {total} kB\nSwapFree: {free} kB\n")
    assert publisher["_memory_and_swap"](meminfo) == pytest.approx((0.8, expected))


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
def test_status_rules(publisher, monkeypatch, cpu, memory, swap, reasons):
    monkeypatch.setitem(publisher, "_load", lambda: 1.0)
    monkeypatch.setitem(publisher, "_cpu_util", lambda _: cpu)
    monkeypatch.setitem(publisher, "_memory_and_swap", lambda _: (memory, swap))
    body = publisher["_load_fields"](Path("/proc"))
    assert body["status"] == ("busy" if reasons else "available")
    assert body["busy_reasons"] == reasons
    assert body["load"] == cpu


def test_missing_proc_retains_legacy_fields(publisher, tmp_path, monkeypatch):
    monkeypatch.setitem(publisher, "_load", lambda: 0.96)
    assert publisher["_load_fields"](tmp_path) == {"load": 0.96, "status": "busy"}


def test_dry_run_prints_publish_body_without_hooks(publisher, monkeypatch, capsys):
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
        "load_avg_ratio": 0.99,
        "memory_pressure": 0.60,
        "swap_pressure": 0.50,
        "busy_reasons": [],
        "running": {"session_ids": [], "run_ids": []},
        "active_claims": [],
        "usable_seats": [],
        "credential_state": "ok",
    }
