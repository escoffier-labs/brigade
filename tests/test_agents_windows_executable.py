from __future__ import annotations

import sys

import pytest

from brigade import agents, proc


def _fake_windows_which(mapping: dict[str, str]):
    def _which(command: str) -> str | None:
        return mapping.get(command)

    return _which


@pytest.mark.parametrize(
    ("path", "expected_kind"),
    [
        (r"C:\npm\codex.exe", "exe"),
        (r"C:\npm\codex.cmd", "cmd"),
        (r"C:\npm\codex.bat", "bat"),
        (r"C:\npm\codex", "npm-shim"),
    ],
)
def test_resolve_executable_classifies_windows_shim_kinds(monkeypatch, path, expected_kind):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(proc, "which", _fake_windows_which({"codex": path}))
    monkeypatch.setattr(proc, "_looks_like_windows_pe_executable", lambda _path: False)

    identity = proc.resolve_executable("codex")

    assert identity.path == path
    assert identity.kind == expected_kind
    assert identity.runnable is (expected_kind == "exe")
    assert "codex" in identity.detail
    assert path not in identity.detail
    if expected_kind != "exe":
        assert "native codex executable directory" in identity.detail


def test_resolve_executable_accepts_extensionless_windows_pe(monkeypatch, tmp_path):
    pe_offset = 0x80
    native = tmp_path / "codex"
    native.write_bytes(
        b"MZ" + b"\0" * (0x3C - 2) + pe_offset.to_bytes(4, "little") + b"\0" * (pe_offset - 0x40) + b"PE\0\0"
    )
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(proc, "which", _fake_windows_which({"codex": str(native)}))

    identity = proc.resolve_executable("codex")

    assert identity.kind == "native"
    assert identity.runnable is True
    assert "native executable" in identity.detail
    assert str(native) not in identity.detail


def test_resolve_executable_rejects_unsupported_windows_ps1_shim(monkeypatch):
    path = r"C:\npm\codex.ps1"
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(proc, "which", _fake_windows_which({"codex": path}))

    identity = proc.resolve_executable("codex")

    assert identity.kind == "unsupported.ps1"
    assert identity.runnable is False
    assert "unsupported Windows PowerShell script" in identity.detail
    assert "native codex executable directory" in identity.detail
    assert path not in identity.detail


def test_run_agent_launches_resolved_windows_exe(monkeypatch):
    resolved = r"C:\native\codex.exe"
    launched: list[list[str]] = []
    stdin_payloads: list[bytes | None] = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(proc, "which", _fake_windows_which({"codex": resolved}))
    monkeypatch.setattr(
        agents.proc,
        "run",
        lambda argv, **kwargs: (
            launched.append(argv) or stdin_payloads.append(kwargs.get("stdin")) or agents.proc.Result(0, "ok", "")
        ),
    )

    result = agents.run_agent("codex", "do it")

    assert result.ok is True
    assert launched == [[resolved, "exec", "-"]]
    assert stdin_payloads == [b"do it"]


def test_run_agent_resolves_once_from_child_path(monkeypatch):
    parent = r"C:\\parent\\codex.exe"
    child = r"C:\\child\\codex.exe"
    resolutions: list[tuple[str, str | None]] = []
    launched: list[list[str]] = []

    def which(command: str, path: str | None = None) -> str | None:
        resolutions.append((command, path))
        return child if path == r"C:\\child" else parent

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(proc, "which", which)
    monkeypatch.setattr(
        agents.proc,
        "run",
        lambda argv, **kwargs: launched.append(argv) or agents.proc.Result(0, "ok", ""),
    )

    result = agents.run_agent("codex", "do it", env={"PATH": r"C:\\child"})

    assert result.ok is True
    assert resolutions == [("codex", r"C:\\child")]
    assert launched == [[child, "exec", "-"]]


@pytest.mark.parametrize(
    "resolved",
    [
        r"C:\npm\codex.ps1",
        r"C:\npm\codex.cmd",
        r"C:\npm\codex.bat",
        r"C:\npm\codex",
    ],
)
def test_run_agent_rejects_unsupported_windows_shim_before_inference(monkeypatch, resolved):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(proc, "which", _fake_windows_which({"codex": resolved}))
    monkeypatch.setattr(proc, "_looks_like_windows_pe_executable", lambda _path: False)
    launched: list[list[str]] = []

    def _run(argv, **kwargs):
        launched.append(argv)
        return agents.proc.Result(0, "", "")

    monkeypatch.setattr(agents.proc, "run", _run)

    result = agents.run_agent("codex", "do it")

    assert result.ok is False
    assert launched == []
    assert result.failure_phase == "dispatch"
    assert result.failure_kind == "unsupported-command-shim"
    assert "native codex executable directory" in result.detail


def test_detect_and_run_agent_share_resolved_executable_identity(monkeypatch):
    resolved = r"C:\native\codex.exe"
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(proc, "which", _fake_windows_which({"codex": resolved}))
    launched: list[list[str]] = []

    monkeypatch.setattr(
        agents.proc,
        "run",
        lambda argv, **kwargs: launched.append(argv) or agents.proc.Result(0, "ok", ""),
    )

    doctor_identity = agents.resolve_agent_executable("codex")
    assert agents.detect("codex") is True
    assert doctor_identity.path == resolved
    assert doctor_identity.kind == "exe"

    result = agents.run_agent("codex", "probe")

    assert result.ok is True
    assert launched[0][0] == doctor_identity.path


def test_run_agent_classifies_provider_preflight_separately_from_command_resolution(monkeypatch):
    resolved = r"C:\native\codex.exe"
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(proc, "which", _fake_windows_which({"codex": resolved}))
    monkeypatch.setattr(
        agents.proc,
        "run",
        lambda argv, **kwargs: agents.proc.Result(
            1,
            "",
            "error: workspace is not trusted; run from a Git repository",
        ),
    )

    result = agents.run_agent("codex", "do it")

    assert result.ok is False
    assert result.failure_phase == "provider-preflight"
    assert result.failure_kind == "workspace-trust"
    assert "preflight" in result.detail
    assert "command not found" not in result.detail.lower()


def test_run_agent_maps_process_creation_failure_without_leaking_paths(monkeypatch):
    resolved = r"C:\native\codex.exe"
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(proc, "which", _fake_windows_which({"codex": resolved}))
    monkeypatch.setattr(
        agents.proc,
        "run",
        lambda argv, **kwargs: agents.proc.Result(127, "", "command not found: codex"),
    )

    result = agents.run_agent("codex", "do it")

    assert result.ok is False
    assert result.failure_phase == "dispatch"
    assert result.failure_kind == "command-not-found"
    assert resolved not in result.detail
    assert "native codex executable directory" not in result.detail


def test_resolve_executable_keeps_linux_behavior_unchanged(monkeypatch):
    if sys.platform == "win32":
        pytest.skip("POSIX-only regression")
    monkeypatch.setattr(proc, "which", lambda command: f"/usr/bin/{command}" if command == "codex" else None)

    identity = proc.resolve_executable("codex")

    assert identity.path == "/usr/bin/codex"
    assert identity.kind == "native"
    assert identity.runnable is True
