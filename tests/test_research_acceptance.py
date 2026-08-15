"""Hermetic black-box CLI acceptance for first-class multi-lane research.

Exercises real Oracle/Codex adapter argv and stdin through executable stubs on
PATH. Does not monkeypatch research_cmd, SeatInvoker, or agents.run_agent.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from brigade import localio

_ORACLE_STUB = r"""
import json, os, re, sys, time
if "-p" not in sys.argv:
    print("oracle stub requires -p prompt", file=sys.stderr)
    raise SystemExit(2)
prompt = sys.argv[sys.argv.index("-p") + 1]
mode = os.environ.get("ORACLE_TEST_MODE", "success")
if mode == "expired-cookie":
    print(
        "cookie=oracle-expired-cookie-secret profile=/home/oracle-user/.config/browser browser session expired",
        file=sys.stderr,
    )
    raise SystemExit(1)
if mode == "block":
    time.sleep(30)
if "Return ONLY a JSON array" in prompt:
    print(
        json.dumps(
            [
                {
                    "url": "https://example.test/current",
                    "title": "Current",
                    "snippet": "Verified fact",
                }
            ]
        )
    )
else:
    source = re.search(r"\[source:(src-[a-f0-9]{16})\]", prompt)
    print("Oracle report " + (source.group(0) if source else ""))
"""

_CODEX_STUB = r"""
import json, re, sys
prompt = sys.stdin.read()
source = re.search(r"\[source:(src-[a-f0-9]{16})\]", prompt)
if "research strategist" in prompt:
    print(
        json.dumps(
            {
                "sub_questions": ["fact"],
                "key_topics": ["fact"],
                "success_criteria": "cite",
            }
        )
    )
elif "extract only the information" in prompt:
    print(json.dumps({"summary": "Verified fact", "evidence": "Verified fact"}))
elif "independent research reviewer" in prompt:
    print(
        json.dumps(
            {
                "accepted": True,
                "detail": "citations resolve",
                "rejected_claims": [],
            }
        )
    )
else:
    print("Luna report " + (source.group(0) if source else ""))
"""


def _write_executable(path: Path, source: str) -> None:
    path.write_text(f"#!{sys.executable}\n{source}", encoding="utf-8")
    path.chmod(0o755)


def acceptance_workspace(
    tmp_path: Path,
    *,
    oracle_mode: str,
    allow_fallback: bool = True,
    include_source: bool = True,
) -> Path:
    workspace = tmp_path / "workspace"
    bin_dir = tmp_path / "bin"
    workspace.mkdir()
    bin_dir.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(workspace)], check=True)
    (workspace / ".brigade").mkdir()
    (workspace / "docs").mkdir()
    if include_source:
        (workspace / "docs" / "fact.md").write_text("Verified fact\n", encoding="utf-8")
    # Roster uses the live string orchestrator contract, not the plan's table form.
    (workspace / ".brigade" / "roster.toml").write_text(
        """
orchestrator = "chef"

[agents.chef]
cli = "codex"
role = "orchestrator"

[agents.luna]
cli = "codex"
role = "researcher"
model = "gpt-5.6-luna"
reasoning = "medium"
capabilities = ["research.plan", "research.extract", "research.synthesize", "research.review"]

[agents.gemini_browser]
cli = "oracle"
role = "browser researcher"
model = "gemini-3.1-pro"
capabilities = ["research.synthesize", "research.browser-discover"]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (workspace / ".brigade" / "research.toml").write_text(
        (
            "[research]\n"
            'default_profile = "grounded"\n\n'
            "[profiles.grounded]\n"
            'discovery = ["brigade"]\n'
            'planner = ["luna"]\n'
            'extractor = ["luna"]\n'
            'synthesizer = ["gemini_browser", "luna"]\n'
            'reviewer = ["luna"]\n'
            f"allow_synthesis_fallback = {str(allow_fallback).lower()}\n"
            "browser_ai_research = false\n\n"
            "[profiles.browser-ai]\n"
            'discovery = ["browser-ai"]\n'
            'planner = ["luna"]\n'
            'extractor = ["luna"]\n'
            'synthesizer = ["gemini_browser", "luna"]\n'
            'reviewer = ["luna"]\n'
            "allow_synthesis_fallback = true\n"
            "browser_ai_research = true\n"
        ),
        encoding="utf-8",
    )
    _write_executable(bin_dir / "oracle", _ORACLE_STUB)
    _write_executable(bin_dir / "codex", _CODEX_STUB)
    localio.write_json(
        workspace / ".acceptance-env.json",
        {
            "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            "ORACLE_TEST_MODE": oracle_mode,
        },
    )
    return workspace


def _acceptance_env(workspace: Path) -> dict[str, str]:
    return {
        **os.environ,
        **json.loads((workspace / ".acceptance-env.json").read_text(encoding="utf-8")),
    }


def run_brigade(workspace: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "brigade", *args, "--target", str(workspace)],
        cwd=workspace,
        env=_acceptance_env(workspace),
        text=True,
        capture_output=True,
        timeout=20,
    )


def start_brigade(workspace: Path, *args: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-m", "brigade", *args, "--target", str(workspace)],
        cwd=workspace,
        env=_acceptance_env(workspace),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )


def _terminate_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def wait_for_phase(workspace: Path, phase: str, *, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    runs_root = workspace / ".brigade" / "runs"
    while time.monotonic() < deadline:
        if runs_root.is_dir():
            for run_dir in runs_root.glob("*"):
                sidecar = run_dir / "research.json"
                if not sidecar.is_file():
                    continue
                try:
                    payload = json.loads(sidecar.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if payload.get("current_phase") == phase:
                    return run_dir.name
        time.sleep(0.02)
    raise AssertionError(f"research run did not reach {phase}")


def test_grounded_cli_creates_auditable_standard_run(tmp_path: Path) -> None:
    workspace = acceptance_workspace(tmp_path, oracle_mode="success")

    result = run_brigade(
        workspace,
        "research",
        "run",
        "What changed?",
        "--source",
        "docs/*.md",
        "--profile",
        "grounded",
        "--json",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    run_dir = workspace / ".brigade" / "runs" / payload["run_id"]
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    research = json.loads((run_dir / "research.json").read_text(encoding="utf-8"))
    audit = json.loads((run_dir / "citation-audit.json").read_text(encoding="utf-8"))
    assert run["kind"] == "research"
    assert run["status"] == "completed"
    # Standard run journal lives at events/lifecycle.jsonl in the current kernel.
    assert (run_dir / "events" / "lifecycle.jsonl").is_file()
    assert any((run_dir / "workers").iterdir())
    assert research["resolved_lanes"]["synthesis"]["primary"] == "gemini_browser"
    assert research["resolved_lanes"]["review"]["primary"] == "luna"
    assert audit["accepted"] is True
    assert audit["unresolved"] == []


def test_expired_cookie_fails_truthfully_without_report(tmp_path: Path) -> None:
    workspace = acceptance_workspace(tmp_path, oracle_mode="expired-cookie", allow_fallback=False)

    result = run_brigade(
        workspace,
        "research",
        "run",
        "What changed?",
        "--source",
        "docs/*.md",
        "--profile",
        "grounded",
        "--json",
    )

    assert result.returncode == 1, result.stderr
    payload = json.loads(result.stdout)
    run_dir = workspace / ".brigade" / "runs" / payload["run_id"]
    assert payload["failure_kind"] == "browser-auth"
    assert payload["failure_phase"] == "synthesis"
    assert not (run_dir / "report.md").exists()
    assert "cookie" not in result.stderr.lower()
    assert "oracle-expired-cookie-secret" not in result.stderr
    assert "oracle-expired-cookie-secret" not in result.stdout
    assert "/home/oracle-user" not in result.stderr
    assert "/home/oracle-user" not in result.stdout
    assert "/home/oracle-user/.config/browser" not in json.dumps(payload)


def test_oracle_failure_uses_luna_fallback(tmp_path: Path) -> None:
    workspace = acceptance_workspace(tmp_path, oracle_mode="expired-cookie", allow_fallback=True)

    result = run_brigade(
        workspace,
        "research",
        "run",
        "What changed?",
        "--source",
        "docs/*.md",
        "--profile",
        "grounded",
        "--json",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    research = json.loads(
        (workspace / ".brigade" / "runs" / payload["run_id"] / "research.json").read_text(encoding="utf-8")
    )
    assert research["synthesis"]["seat"] == "luna"
    assert research["fallbacks"][0]["from_seat"] == "gemini_browser"
    assert research["fallbacks"][0]["failure_kind"] == "browser-auth"


def test_browser_ai_discovery_requires_explicit_mode(tmp_path: Path) -> None:
    workspace = acceptance_workspace(tmp_path, oracle_mode="success", include_source=False)

    denied = run_brigade(
        workspace,
        "research",
        "run",
        "Find browser agents",
        "--profile",
        "grounded",
        "--json",
    )
    allowed = run_brigade(
        workspace,
        "research",
        "run",
        "Find browser agents",
        "--profile",
        "browser-ai",
        "--browser-ai-research",
        "--json",
    )

    assert denied.returncode == 2, denied.stderr
    assert json.loads(denied.stdout)["failure_kind"] == "no-source-route"
    assert allowed.returncode == 0, allowed.stderr
    allowed_payload = json.loads(allowed.stdout)
    research = json.loads(
        (workspace / ".brigade" / "runs" / allowed_payload["run_id"] / "research.json").read_text(encoding="utf-8")
    )
    assert research["discovery_mode"] == "browser-ai"
    assert research["source_counts"]["browser-ai"] == 1


def test_cancel_active_synthesis_exits_130(tmp_path: Path) -> None:
    workspace = acceptance_workspace(tmp_path, oracle_mode="block")
    process = start_brigade(
        workspace,
        "research",
        "run",
        "What changed?",
        "--source",
        "docs/*.md",
        "--profile",
        "grounded",
        "--json",
    )
    try:
        run_id = wait_for_phase(workspace, "synthesis", timeout=8.0)
        cancelled = run_brigade(workspace, "research", "cancel", run_id, "--json")
        stdout, stderr = process.communicate(timeout=10)
    finally:
        _terminate_tree(process)

    assert cancelled.returncode == 0, cancelled.stderr
    assert process.returncode == 130, stderr
    assert json.loads(stdout)["status"] == "cancelled"
