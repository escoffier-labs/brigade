"""Dogfood Brigade against a trusted workspace with a safe default roster."""
from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

from . import aboyeur
from . import runs_cmd
from .roster import Agent, Roster

DEFAULT_TASK = "Review this repo and recommend the next implementation slice."
DEFAULT_TIMEOUT_SECONDS = 600.0
CONFIG_REL_PATH = ".brigade/dogfood.toml"


@dataclass(frozen=True)
class DogfoodConfig:
    target: Path | None = None
    artifacts_dir: Path | None = None
    handoff: bool = True
    handoff_inbox: Path | None = None
    inspect: bool = True
    native_read_only_sandbox: bool = False
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS


def _dogfood_roster(timeout_seconds: float) -> Roster:
    return Roster(
        orchestrator="chef",
        agents={
            "chef": Agent(
                "chef",
                "codex",
                "Plan one small read-only review task and synthesize a concise final answer.",
                timeout_seconds=timeout_seconds,
            ),
            "reviewer": Agent(
                "reviewer",
                "codex",
                "Inspect the target repo in read-only mode and recommend the next practical implementation slice.",
                timeout_seconds=timeout_seconds,
            ),
        },
        max_workers=1,
        allow_models=("codex",),
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


def config_path(target: Path) -> Path:
    return target / CONFIG_REL_PATH


def load_config(target: Path) -> DogfoodConfig | None:
    target = target.expanduser().resolve()
    path = config_path(target)
    if not path.is_file():
        return None

    data = _read_toml_object(path)
    return DogfoodConfig(
        target=_as_path(data.get("target"), "target", target),
        artifacts_dir=_as_path(data.get("artifacts_dir"), "artifacts_dir", target),
        handoff=_as_bool(data.get("handoff", True), "handoff"),
        handoff_inbox=_as_path(data.get("handoff_inbox"), "handoff_inbox", target),
        inspect=_as_bool(data.get("inspect", True), "inspect"),
        native_read_only_sandbox=_as_bool(
            data.get("native_read_only_sandbox", False),
            "native_read_only_sandbox",
        ),
        timeout_seconds=_as_positive_float(data.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS), "timeout_seconds"),
    )


def init(
    *,
    target: Path,
    artifacts_dir: Path | None = None,
    handoff_inbox: Path | None = None,
    force: bool = False,
    handoff: bool = True,
    inspect: bool = True,
    native_read_only_sandbox: bool = False,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> int:
    if timeout_seconds <= 0:
        print("error: --timeout-seconds must be positive", file=sys.stderr)
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
        handoff_inbox.expanduser() if handoff_inbox is not None else target / ".claude" / "memory-handoffs"
    )
    payload = {
        "target": str(target),
        "artifacts_dir": str(chosen_artifacts_dir),
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
    if timeout_seconds != DEFAULT_TIMEOUT_SECONDS:
        effective_timeout = timeout_seconds
    if timeout_seconds <= 0 or effective_timeout <= 0:
        print("error: --timeout-seconds must be positive", file=sys.stderr)
        return 2

    artifacts_dir = cfg.artifacts_dir if cfg and cfg.artifacts_dir is not None else effective_target / ".brigade" / "runs"
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
            else effective_target / ".claude" / "memory-handoffs"
        )

    rc = aboyeur.run(
        task or DEFAULT_TASK,
        _dogfood_roster(effective_timeout),
        show_plan=True,
        cwd=effective_target,
        output_dir=chosen_output_dir,
        handoff_inbox=chosen_handoff_inbox,
        read_only=True,
        sandbox="read-only" if effective_native else "danger-full-access",
    )
    print(f"artifacts: {chosen_output_dir}", file=sys.stderr)
    if effective_inspect:
        runs_cmd.show(chosen_output_dir)
    return rc
