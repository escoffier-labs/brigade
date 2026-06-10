"""Dogfood Brigade against a trusted workspace with a safe default roster."""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import aboyeur
from . import agents
from . import runs_cmd
from .roster import Agent, Roster
from .selection import WRITER_INBOXES

DEFAULT_TASK = "Review this repo and recommend the next implementation slice."
DEFAULT_TIMEOUT_SECONDS = 600.0
CONFIG_REL_PATH = ".brigade/dogfood.toml"
DEFAULT_AGENT_CLI = "codex"
DEFAULT_HANDOFF_INBOX_REL_PATH = ".codex/memory-handoffs"
NEXT_LABELS = (
    "next practical slice",
    "smallest follow-up slice",
    "next implementation slice",
    "recommended next slice",
    "next step",
)


@dataclass(frozen=True)
class DogfoodConfig:
    target: Path | None = None
    artifacts_dir: Path | None = None
    agent_cli: str = DEFAULT_AGENT_CLI
    handoff: bool = True
    handoff_inbox: Path | None = None
    inspect: bool = True
    native_read_only_sandbox: bool = False
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS


def _dogfood_roster(timeout_seconds: float, agent_cli: str = DEFAULT_AGENT_CLI) -> Roster:
    return Roster(
        orchestrator="chef",
        agents={
            "chef": Agent(
                "chef",
                agent_cli,
                "Plan one small read-only review task and synthesize a concise final answer.",
                timeout_seconds=timeout_seconds,
            ),
            "reviewer": Agent(
                "reviewer",
                agent_cli,
                "Inspect the target repo in read-only mode and recommend the next practical implementation slice.",
                timeout_seconds=timeout_seconds,
            ),
        },
        max_workers=1,
        allow_models=(agent_cli,),
        timeout_seconds=timeout_seconds,
    )


def _format_toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return f"{value:g}"
    return repr(str(value))


def _parse_toml_value(raw: str) -> object:
    value = raw.strip()
    if value == "true":
        return True
    if value == "false":
        return False
    try:
        return ast.literal_eval(value)
    except (SyntaxError, ValueError):
        try:
            return float(value)
        except ValueError:
            return value


def _read_toml_object(path: Path) -> dict[str, object]:
    data: dict[str, object] = {}
    for line_number, raw_line in enumerate(path.read_text().splitlines(), start=1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if "=" not in line:
            raise ValueError(f"invalid dogfood config line {line_number}: expected key = value")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"invalid dogfood config line {line_number}: empty key")
        data[key] = _parse_toml_value(raw_value)
    return data


def _as_path(value: object, field: str, base: Path) -> Path | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string path")
    path = Path(value).expanduser()
    return path if path.is_absolute() else base / path


def _as_bool(value: object, field: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError(f"{field} must be true or false")


def _as_positive_float(value: object, field: str) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return float(value)
    raise ValueError(f"{field} must be a positive number")


def _as_agent_cli(value: object, field: str = "agent_cli") -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    cli_ref = value.strip()
    if not agents.is_known(cli_ref):
        raise ValueError(f"{field} is unknown: {cli_ref!r}")
    return cli_ref


def config_path(target: Path) -> Path:
    return target / CONFIG_REL_PATH


def default_handoff_inbox(target: Path, agent_cli: str = DEFAULT_AGENT_CLI) -> Path:
    return target / WRITER_INBOXES.get(agent_cli, DEFAULT_HANDOFF_INBOX_REL_PATH)


def load_config(target: Path) -> DogfoodConfig | None:
    target = target.expanduser().resolve()
    path = config_path(target)
    if not path.is_file():
        return None

    data = _read_toml_object(path)
    return DogfoodConfig(
        target=_as_path(data.get("target"), "target", target),
        artifacts_dir=_as_path(data.get("artifacts_dir"), "artifacts_dir", target),
        agent_cli=_as_agent_cli(data.get("agent_cli", DEFAULT_AGENT_CLI)),
        handoff=_as_bool(data.get("handoff", True), "handoff"),
        handoff_inbox=_as_path(data.get("handoff_inbox"), "handoff_inbox", target),
        inspect=_as_bool(data.get("inspect", True), "inspect"),
        native_read_only_sandbox=_as_bool(
            data.get("native_read_only_sandbox", False),
            "native_read_only_sandbox",
        ),
        timeout_seconds=_as_positive_float(data.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS), "timeout_seconds"),
    )


def _check_git_ignored(repo: Path, path: Path) -> str:
    try:
        relative = path.expanduser().resolve().relative_to(repo)
    except ValueError:
        return "outside-target"
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "check-ignore", "-q", str(relative)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return "unknown"
    if result.returncode == 0:
        return "yes"
    if result.returncode == 1:
        return "no"
    return "unknown"


def _latest_run(runs_dir: Path) -> tuple[Path, dict[str, Any]] | None:
    if not runs_dir.is_dir():
        return None
    latest: tuple[Path, dict[str, Any]] | None = None
    latest_key = ""
    for child in runs_dir.iterdir():
        if not child.is_dir():
            continue
        try:
            payload = json.loads((child / "run.json").read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        key = str(payload.get("started_at") or child.name)
        if latest is None or key > latest_key:
            latest = (child, payload)
            latest_key = key
    return latest


def _setting_line(label: str, value: object) -> None:
    print(f"{label}: {value}")


def _load_effective_paths(target: Path) -> tuple[Path, Path, DogfoodConfig | None]:
    target = target.expanduser().resolve()
    if not target.is_dir():
        raise FileNotFoundError(f"--target is not a directory: {target}")
    cfg = load_config(target)
    effective_target = cfg.target if cfg and cfg.target is not None else target
    effective_target = effective_target.expanduser().resolve()
    if not effective_target.is_dir():
        raise FileNotFoundError(f"configured target is not a directory: {effective_target}")
    artifacts_dir = (
        cfg.artifacts_dir if cfg and cfg.artifacts_dir is not None else effective_target / ".brigade" / "runs"
    )
    return effective_target, artifacts_dir, cfg


def _read_run_json(run_dir: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads((run_dir / "run.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _read_final(run_dir: Path) -> str:
    try:
        return (run_dir / "final.txt").read_text().strip()
    except OSError:
        return ""


def _read_summary(run_dir: Path) -> str:
    try:
        return (run_dir / "summary.md").read_text().strip()
    except OSError:
        return ""


def _clean_next_candidate(line: str) -> str:
    cleaned = line.strip()
    cleaned = re.sub(r"^\s{0,3}#{1,6}\s+", "", cleaned)
    cleaned = re.sub(r"^\s{0,3}[-*+]\s+", "", cleaned)
    cleaned = cleaned.strip().strip("*").strip()
    return cleaned


def _collect_next_block(lines: list[str], start: int) -> str | None:
    collected: list[str] = []
    for follow in lines[start:]:
        follow_stripped = follow.strip()
        if not follow_stripped:
            if collected:
                break
            continue
        if collected and re.match(r"^\s{0,3}#{1,6}\s+", follow_stripped):
            break
        if collected and follow_stripped.endswith(":") and not follow_stripped.startswith(("-", "*", "+")):
            break
        collected.append(follow_stripped)
    rendered = "\n".join(item for item in collected if item).strip()
    if _clean_next_candidate(rendered).lower() in {"(none extracted)", "none", "n/a"}:
        return None
    return rendered or None


def extract_next_step(final_text: str) -> str | None:
    lines = final_text.splitlines()
    for index, line in enumerate(lines):
        stripped = _clean_next_candidate(line)
        lowered = stripped.lower()
        if lowered == "next":
            return _collect_next_block(lines, index + 1)
        for label in NEXT_LABELS:
            if not lowered.startswith(label):
                continue
            _, _, after = stripped.partition(":")
            if after.strip():
                return after.strip()
            return _collect_next_block(lines, index + 1)
    return None


def extract_next_step_from_run(run_dir: Path) -> tuple[str | None, str | None]:
    final_next = extract_next_step(_read_final(run_dir))
    if final_next:
        return final_next, "final"
    summary_next = extract_next_step(_read_summary(run_dir))
    if summary_next:
        return summary_next, "summary"
    return None, None


def _write_summary(run_dir: Path) -> None:
    run_meta = _read_run_json(run_dir)
    if run_meta is None:
        return
    final_text = _read_final(run_dir)
    next_step = extract_next_step(final_text)
    lines = [
        "# Brigade Dogfood Run Summary",
        "",
        f"- Task: {run_meta.get('task', '')}",
        f"- Status: {run_meta.get('status', 'unknown')}",
        f"- Started: {run_meta.get('started_at', '')}",
        f"- Duration: {run_meta.get('duration_seconds', '')}",
        f"- Artifacts: {run_meta.get('artifacts') or run_dir}",
    ]
    if run_meta.get("handoff"):
        lines.append(f"- Handoff: {run_meta['handoff']}")
    lines.extend(["", "## Next", "", next_step or "(none extracted)", "", "## Final", "", final_text or "(empty)", ""])
    (run_dir / "summary.md").write_text("\n".join(lines))


def latest(*, target: Path) -> int:
    try:
        _, artifacts_dir, _ = _load_effective_paths(target)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return runs_cmd.show_latest(cwd=target, runs_dir=artifacts_dir)


def next_step(*, target: Path) -> int:
    try:
        _, artifacts_dir, _ = _load_effective_paths(target)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    latest_run = _latest_run(artifacts_dir)
    if latest_run is None:
        print(f"error: no runs found in {artifacts_dir}", file=sys.stderr)
        return 1
    run_dir, _ = latest_run
    extracted, _ = extract_next_step_from_run(run_dir)
    if not extracted:
        print(f"error: no next step found in {run_dir / 'final.txt'}", file=sys.stderr)
        return 1
    print(extracted)
    return 0


def status(*, target: Path) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2

    path = config_path(target)
    try:
        cfg = load_config(target)
    except ValueError as exc:
        print(f"error: invalid dogfood config: {exc}", file=sys.stderr)
        return 2

    effective_target = cfg.target if cfg and cfg.target is not None else target
    effective_target = effective_target.expanduser().resolve()
    artifacts_dir = (
        cfg.artifacts_dir if cfg and cfg.artifacts_dir is not None else effective_target / ".brigade" / "runs"
    )
    agent_cli = cfg.agent_cli if cfg else DEFAULT_AGENT_CLI
    handoff = cfg.handoff if cfg else True
    handoff_inbox = (
        cfg.handoff_inbox
        if cfg and cfg.handoff_inbox is not None
        else default_handoff_inbox(effective_target, agent_cli)
    )
    inspect = cfg.inspect if cfg else True
    native = cfg.native_read_only_sandbox if cfg else False
    timeout = cfg.timeout_seconds if cfg else DEFAULT_TIMEOUT_SECONDS

    agent_command = agents.command_for(agent_cli)
    agent_path = shutil.which(agent_command)
    brigade_path = shutil.which("brigade")
    blockers: list[str] = []
    warnings: list[str] = []
    if cfg is None:
        warnings.append(f"config missing: run `brigade dogfood init --target {target}`")
    if not effective_target.is_dir():
        blockers.append(f"configured target is not a directory: {effective_target}")
    if agent_path is None:
        blockers.append(f"{agent_command} CLI not found on PATH")
    if brigade_path is None:
        warnings.append("brigade CLI not found on PATH; use the venv command or install the package")

    ready = not blockers
    print(f"dogfood: {'ready' if ready else 'not ready'}")
    _setting_line("config", path if path.exists() else f"{path} (missing)")
    _setting_line("target", effective_target)
    _setting_line("artifacts_dir", artifacts_dir)
    _setting_line("agent_cli", agent_cli)
    _setting_line("handoff", "enabled" if handoff else "disabled")
    if handoff:
        _setting_line("handoff_inbox", handoff_inbox)
    _setting_line("inspect", "enabled" if inspect else "disabled")
    _setting_line(
        "sandbox",
        "native read-only" if native else "prompt read-only + trusted-workspace execution",
    )
    _setting_line("timeout_seconds", f"{timeout:g}")
    _setting_line(agent_command, agent_path or "missing")
    _setting_line("brigade", brigade_path or "missing")
    _setting_line("config_ignored", _check_git_ignored(effective_target, path))
    _setting_line("artifacts_ignored", _check_git_ignored(effective_target, artifacts_dir))
    latest = _latest_run(artifacts_dir)
    if latest is not None:
        latest_path, latest_meta = latest
        task = " ".join(str(latest_meta.get("task") or "").split())
        if len(task) > 80:
            task = task[:77].rstrip() + "..."
        _setting_line(
            "latest_run",
            f"{latest_meta.get('started_at', latest_path.name)} [{latest_meta.get('status', 'unknown')}] {latest_path}",
        )
        if task:
            _setting_line("latest_task", task)
        next_step_text, _ = extract_next_step_from_run(latest_path)
        if next_step_text:
            _setting_line("latest_next", " ".join(next_step_text.split()))
    else:
        _setting_line("latest_run", "none")

    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    for blocker in blockers:
        print(f"error: {blocker}", file=sys.stderr)
    return 0 if ready else 1


def init(
    *,
    target: Path,
    artifacts_dir: Path | None = None,
    handoff_inbox: Path | None = None,
    agent_cli: str = DEFAULT_AGENT_CLI,
    force: bool = False,
    handoff: bool = True,
    inspect: bool = True,
    native_read_only_sandbox: bool = False,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> int:
    if timeout_seconds <= 0:
        print("error: --timeout-seconds must be positive", file=sys.stderr)
        return 2
    try:
        chosen_agent_cli = _as_agent_cli(agent_cli)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2

    path = config_path(target)
    if path.exists() and not force:
        print(f"error: dogfood config already exists at {path}; pass --force to overwrite", file=sys.stderr)
        return 2

    chosen_artifacts_dir = artifacts_dir.expanduser() if artifacts_dir is not None else target / ".brigade" / "runs"
    chosen_handoff_inbox = (
        handoff_inbox.expanduser() if handoff_inbox is not None else default_handoff_inbox(target, chosen_agent_cli)
    )
    payload = {
        "target": str(target),
        "artifacts_dir": str(chosen_artifacts_dir),
        "agent_cli": chosen_agent_cli,
        "handoff": handoff,
        "handoff_inbox": str(chosen_handoff_inbox),
        "inspect": inspect,
        "native_read_only_sandbox": native_read_only_sandbox,
        "timeout_seconds": timeout_seconds,
    }
    body = "\n".join(f"{key} = {_format_toml_value(value)}" for key, value in payload.items()) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    print(f"wrote {path}")
    return 0


def run(
    task: str | None,
    *,
    target: Path,
    output_dir: Path | None = None,
    handoff: bool = True,
    handoff_inbox: Path | None = None,
    agent_cli: str | None = None,
    inspect: bool = True,
    native_read_only_sandbox: bool = False,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    try:
        cfg = load_config(target)
    except ValueError as exc:
        print(f"error: invalid dogfood config: {exc}", file=sys.stderr)
        return 2

    effective_target = cfg.target if cfg and cfg.target is not None else target
    effective_target = effective_target.expanduser().resolve()
    if not effective_target.is_dir():
        print(f"error: configured target is not a directory: {effective_target}", file=sys.stderr)
        return 2

    effective_handoff = cfg.handoff if cfg else handoff
    effective_inspect = cfg.inspect if cfg else inspect
    effective_native = cfg.native_read_only_sandbox if cfg else native_read_only_sandbox
    effective_timeout = cfg.timeout_seconds if cfg else timeout_seconds
    effective_agent_cli = cfg.agent_cli if cfg else DEFAULT_AGENT_CLI
    if agent_cli is not None:
        try:
            effective_agent_cli = _as_agent_cli(agent_cli)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if timeout_seconds != DEFAULT_TIMEOUT_SECONDS:
        effective_timeout = timeout_seconds
    if timeout_seconds <= 0 or effective_timeout <= 0:
        print("error: --timeout-seconds must be positive", file=sys.stderr)
        return 2

    artifacts_dir = (
        cfg.artifacts_dir if cfg and cfg.artifacts_dir is not None else effective_target / ".brigade" / "runs"
    )
    chosen_output_dir = output_dir.expanduser() if output_dir is not None else aboyeur.make_run_dir(artifacts_dir)
    chosen_handoff_inbox = None
    if handoff is False:
        effective_handoff = False
    if inspect is False:
        effective_inspect = False
    if native_read_only_sandbox:
        effective_native = True
    if effective_handoff:
        chosen_handoff_inbox = (
            handoff_inbox.expanduser()
            if handoff_inbox is not None
            else cfg.handoff_inbox
            if cfg and cfg.handoff_inbox is not None
            else default_handoff_inbox(effective_target, effective_agent_cli)
        )

    rc = aboyeur.run(
        task or DEFAULT_TASK,
        _dogfood_roster(effective_timeout, effective_agent_cli),
        show_plan=True,
        cwd=effective_target,
        output_dir=chosen_output_dir,
        handoff_inbox=chosen_handoff_inbox,
        read_only=True,
        sandbox="read-only" if effective_native else None,
    )
    if chosen_output_dir.is_dir():
        _write_summary(chosen_output_dir)
    print(f"artifacts: {chosen_output_dir}", file=sys.stderr)
    if effective_inspect:
        runs_cmd.show(chosen_output_dir)
    return rc
