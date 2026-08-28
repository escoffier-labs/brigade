"""Fleet Steward listener construction from environment or pack config."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from .contracts import FleetError
from .lifecycle import FleetListenerConfig, build_tools_from_config, run_listener


def start_fleet_app(
    *,
    env: Mapping[str, str] | None = None,
    bind_host: str | None = None,
    bind_port: int | None = None,
    bearer: str | None = None,
) -> None:
    from .runtime_config import load_fleet_runtime_env, read_secure_runtime_text

    source = os.environ if env is None else env
    runtime = load_fleet_runtime_env(source, dispatch_token=source.get("GROKBOT_DISPATCH_TOKEN"))
    config = FleetListenerConfig(
        target=Path("."),
        bind_host=bind_host or runtime["host"],
        bind_port=bind_port if bind_port is not None else runtime["port"],
        allowed_hosts=(),
        allowed_origins=(),
        bearer=bearer or runtime["token"],
        runtime_path=runtime["runtime_path"],
        ledger_path=runtime["ledger_path"],
        action_state_path=runtime["action_state_path"],
        approval_dir=runtime["approval_dir"],
    )
    try:
        json.loads(read_secure_runtime_text(config.runtime_path))
    except FleetError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FleetError("invalid_request", "Fleet runtime configuration is invalid") from exc
    tools = build_tools_from_config(config, env=env, now=lambda: datetime.now(timezone.utc))
    run_listener(config, tools)
