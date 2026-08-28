"""Local, SSH, service, and incident probe tests."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from brigade.grokbot_fleet.contracts import FleetError
from brigade.grokbot_fleet.exec import PrivateExecResult
from brigade.grokbot_fleet.probes import (
    LINUX_SSH_REMOTE_COMMAND,
    LOCAL_FAILED_SERVICE_ARGS,
    fleet_ssh_argv,
    observe_linux_ssh_host,
    observe_local_host,
    observe_service,
)

TARGET = {
    "alias": "control-plane",
    "tier": "infrastructure",
    "adapter": "local",
    "probe_ids": ["linux-host-summary-v1"],
    "timeout_ms": 12_000,
    "freshness_seconds": 60,
}
LINUX_TARGET = {**TARGET, "alias": "hypervisor", "adapter": "linux"}
SERVICE = {"service_id": "research-bridge", "target_alias": "control-plane", "probe_id": "linux-host-summary-v1"}


def test_local_host_probe_uses_fixed_systemctl_argv_and_cwd():
    recorded = {}

    def runner(request):
        recorded.update(request.__dict__)
        return PrivateExecResult(stdout="sshd.service loaded failed\n")

    observation = observe_local_host(
        TARGET,
        "linux-host-summary-v1",
        systemctl_file="/usr/bin/systemctl",
        env={"PATH": "/usr/bin"},
        now=lambda: datetime(2026, 8, 27, tzinfo=timezone.utc),
        uptime=lambda: 100.2,
        statvfs=lambda path: SimpleNamespace(f_blocks=100, f_bavail=20),
        exists=lambda path: path == "/run/reboot-required",
        runner=runner,
    )
    assert recorded["file"] == "/usr/bin/systemctl"
    assert recorded["args"] == LOCAL_FAILED_SERVICE_ARGS
    assert recorded["cwd"] == "/"
    assert observation["failed_services"] == 1
    assert observation["reboot_pending"] is True
    assert observation["storage_percent"] == 80.0


def test_ssh_probe_uses_fixed_argv_and_parses_summary():
    recorded = {}

    def runner(request):
        recorded.update(request.__dict__)
        return PrivateExecResult(stdout="86400\n80\n2\n1\n")

    observation = observe_linux_ssh_host(
        LINUX_TARGET,
        "linux-host-summary-v1",
        ssh_file="/usr/bin/ssh",
        ssh_alias="lab-hypervisor",
        env={"PATH": "/usr/bin"},
        now=lambda: datetime(2026, 8, 27, tzinfo=timezone.utc),
        runner=runner,
    )
    assert recorded["file"] == "/usr/bin/ssh"
    assert recorded["args"] == tuple(fleet_ssh_argv("lab-hypervisor", [LINUX_SSH_REMOTE_COMMAND]))
    assert observation["failed_services"] == 2
    assert observation["reboot_pending"] is True


def test_service_probe_maps_active_states():
    mapping = {"unit": "research-bridge.service", "manager": "user"}

    def runner_for(state: str):
        def runner(request):
            assert request.args == ("--user", "is-active", "research-bridge.service")
            return PrivateExecResult(stdout=state + "\n")

        return runner

    healthy = observe_service(
        TARGET,
        SERVICE,
        transport={"kind": "local", "systemctl_file": "/usr/bin/systemctl"},
        mapping=mapping,
        env={},
        now=lambda: datetime(2026, 8, 27, tzinfo=timezone.utc),
        runner=runner_for("active"),
    )
    assert healthy["health_class"] == "healthy"
    degraded = observe_service(
        TARGET,
        SERVICE,
        transport={"kind": "local", "systemctl_file": "/usr/bin/systemctl"},
        mapping=mapping,
        env={},
        now=lambda: datetime(2026, 8, 27, tzinfo=timezone.utc),
        runner=runner_for("activating"),
    )
    assert degraded["health_class"] == "degraded"
    unhealthy = observe_service(
        TARGET,
        SERVICE,
        transport={"kind": "local", "systemctl_file": "/usr/bin/systemctl"},
        mapping=mapping,
        env={},
        now=lambda: datetime(2026, 8, 27, tzinfo=timezone.utc),
        runner=runner_for("failed"),
    )
    assert unhealthy["health_class"] == "unhealthy"
    unknown = observe_service(
        TARGET,
        SERVICE,
        transport={"kind": "local", "systemctl_file": "/usr/bin/systemctl"},
        mapping=mapping,
        env={},
        now=lambda: datetime(2026, 8, 27, tzinfo=timezone.utc),
        runner=runner_for("maintenance"),
    )
    assert unknown["health_class"] == "unknown"


def test_service_probe_rejects_unknown_unit_without_leaking_it():
    with pytest.raises(FleetError) as caught:
        observe_service(
            TARGET,
            SERVICE,
            transport={"kind": "local", "systemctl_file": "/usr/bin/systemctl"},
            mapping={"unit": "../evil.service", "manager": "user"},
            env={},
            now=lambda: datetime(2026, 8, 27, tzinfo=timezone.utc),
            runner=lambda request: PrivateExecResult(stdout="active\n"),
        )
    assert caught.value.code == "invalid_request"
    assert "../evil.service" not in str(caught.value)
