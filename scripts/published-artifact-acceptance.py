#!/usr/bin/env python3
"""Accept a published Brigade artifact on Unix GitHub-hosted runners."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


COMPONENT_IDS = ("graphtrail", "graphtrail-mcp", "miseledger", "sessionfind")
Runner = Callable[..., subprocess.CompletedProcess[str]]


class AcceptanceError(RuntimeError):
    """A published-artifact acceptance requirement was not met."""


def run_checked(
    argv: Sequence[str | Path],
    *,
    runner: Runner = subprocess.run,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [str(value) for value in argv]
    try:
        completed = runner(command, text=True, input=input_text, capture_output=True, env=env, check=False)
    except OSError as exc:
        raise AcceptanceError(f"could not run {' '.join(command)}: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "no command output").strip()
        raise AcceptanceError(f"{' '.join(command)} failed with exit {completed.returncode}: {detail}")
    return completed


def validate_component_report(report: Any, managed_bin: Path) -> dict[str, Path]:
    if not isinstance(report, dict) or not isinstance(report.get("components"), list):
        raise AcceptanceError("component report did not contain a components list")
    components = report["components"]
    if len(components) != len(COMPONENT_IDS):
        raise AcceptanceError(f"expected exactly 4 components, got {len(components)}")

    root = managed_bin.resolve()
    managed_paths: dict[str, Path] = {}
    for component in components:
        if not isinstance(component, dict):
            raise AcceptanceError("component report entry was not an object")
        component_id = component.get("component_id")
        if component_id not in COMPONENT_IDS:
            raise AcceptanceError(f"unexpected component {component_id!r}")
        if component_id in managed_paths:
            raise AcceptanceError(f"component report repeated {component_id}")
        if component.get("status") != "healthy":
            raise AcceptanceError(
                f"component {component_id} is {component.get('status')}: {component.get('detail', '')}"
            )
        executable = component.get("recorded_executable") or component.get("managed_executable_path")
        if not isinstance(executable, str) or not executable:
            raise AcceptanceError(f"component {component_id} has no managed executable path")
        raw_path = Path(executable)
        if not raw_path.is_absolute():
            raise AcceptanceError(f"component {component_id} executable must be absolute: {executable!r}")
        resolved = raw_path.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise AcceptanceError(f"component {component_id} executable is outside managed root: {resolved}") from exc
        if not resolved.is_file():
            raise AcceptanceError(f"component {component_id} managed executable missing: {resolved}")
        managed_paths[component_id] = resolved

    if set(managed_paths) != set(COMPONENT_IDS):
        missing = ", ".join(sorted(set(COMPONENT_IDS) - set(managed_paths)))
        raise AcceptanceError(f"component report missing required components: {missing}")
    return managed_paths


def smoke_managed_components(
    managed_paths: Mapping[str, Path], *, runner: Runner = subprocess.run, env: Mapping[str, str] | None = None
) -> None:
    graphtrail = run_checked([managed_paths["graphtrail"], "--version"], runner=runner, env=env)
    if not graphtrail.stdout.strip():
        raise AcceptanceError("graphtrail smoke produced no version output")

    mcp = run_checked(
        [managed_paths["graphtrail-mcp"]],
        runner=runner,
        env=env,
        input_text='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n',
    )
    try:
        response = json.loads(mcp.stdout)
    except json.JSONDecodeError as exc:
        raise AcceptanceError("graphtrail-mcp smoke returned malformed JSON-RPC") from exc
    if response.get("jsonrpc") != "2.0" or response.get("id") != 1 or "result" not in response:
        raise AcceptanceError("graphtrail-mcp smoke returned an invalid JSON-RPC response")

    run_checked([managed_paths["miseledger"], "version"], runner=runner, env=env)
    sessionfind = run_checked([managed_paths["sessionfind"], "--help"], runner=runner, env=env)
    if "usage" not in f"{sessionfind.stdout}{sessionfind.stderr}".lower():
        raise AcceptanceError("sessionfind smoke produced no help text")


def assert_no_poison_invocation(marker: Path) -> None:
    if marker.exists() and marker.read_text().strip():
        raise AcceptanceError(f"poison component binary was invoked: {marker.read_text().strip()}")


def _write_poison_binaries(poison_dir: Path, marker: Path) -> None:
    poison_dir.mkdir(parents=True)
    marker_literal = shlex.quote(str(marker))
    for component_id in COMPONENT_IDS:
        poison = poison_dir / component_id
        poison.write_text(f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(component_id)} >> {marker_literal}\nexit 97\n")
        poison.chmod(poison.stat().st_mode | stat.S_IXUSR)


def run_acceptance(version: str, *, runner: Runner = subprocess.run) -> None:
    if not version or version.startswith("v"):
        raise AcceptanceError("--brigade-version must be an exact PyPI version without a v prefix")

    runner_temp = os.environ.get("RUNNER_TEMP")
    with tempfile.TemporaryDirectory(prefix="brigade-published-acceptance-", dir=runner_temp) as temporary:
        root = Path(temporary)
        profile = root / "profile"
        data_home = root / "xdg-data"
        pipx_home = root / "pipx-home"
        pipx_bin = root / "pipx-bin"
        poison_dir = root / "poison-bin"
        marker = root / "poison-invoked"
        for directory in (profile, data_home, pipx_home, pipx_bin, root / "xdg-config", root / "xdg-cache"):
            directory.mkdir(parents=True)
        _write_poison_binaries(poison_dir, marker)

        env = os.environ.copy()
        env.update(
            {
                "HOME": str(profile),
                "XDG_CONFIG_HOME": str(root / "xdg-config"),
                "XDG_DATA_HOME": str(data_home),
                "XDG_CACHE_HOME": str(root / "xdg-cache"),
                "PIPX_HOME": str(pipx_home),
                "PIPX_BIN_DIR": str(pipx_bin),
                "PATH": os.pathsep.join((str(poison_dir), env.get("PATH", ""))),
            }
        )
        managed_brigade = pipx_bin / "brigade"
        try:
            run_checked([sys.executable, "-m", "pip", "install", "--upgrade", "pip", "pipx"], runner=runner, env=env)
            run_checked([sys.executable, "-m", "pipx", "install", f"brigade-cli=={version}"], runner=runner, env=env)
            if not managed_brigade.is_file():
                raise AcceptanceError(f"pipx did not install brigade at {managed_brigade}")

            version_output = run_checked([managed_brigade, "--version"], runner=runner, env=env).stdout.strip()
            if version_output != f"brigade {version}":
                raise AcceptanceError(f"installed brigade version mismatch: expected {version}, got {version_output}")
            run_checked([managed_brigade, "setup"], runner=runner, env=env)
            run_checked([managed_brigade, "setup", "--offline"], runner=runner, env=env)
            report_output = run_checked([managed_brigade, "version", "--components", "--json"], runner=runner, env=env)
            try:
                report = json.loads(report_output.stdout)
            except json.JSONDecodeError as exc:
                raise AcceptanceError("brigade version --components --json returned malformed JSON") from exc
            managed_paths = validate_component_report(report, data_home / "brigade" / "bin")
            smoke_managed_components(managed_paths, runner=runner, env=env)
        finally:
            assert_no_poison_invocation(marker)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--brigade-version", required=True)
    args = parser.parse_args(argv)
    try:
        run_acceptance(args.brigade_version)
    except AcceptanceError as exc:
        print(f"published artifact acceptance failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
