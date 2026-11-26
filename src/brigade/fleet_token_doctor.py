"""Doctor checks for configured fleet token files."""

from __future__ import annotations

import os

from .fleet_home_guard import FleetTokenFileError, _open_configured_token, token_path, user_fleet_config
from .station import DoctorContext


def checks(_ctx: DoctorContext) -> list[tuple[str, str, str]]:
    """Report each configured token path without reading its contents."""
    config_path, section = user_fleet_config()
    if config_path is None:
        return []
    checks = []
    for key, env in (("token_file", "BRIGADE_FLEET_TOKEN"), ("node_token_file", "BRIGADE_FLEET_NODE_TOKEN")):
        raw = section.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        name = f"fleet token file: {key}"
        if os.environ.get(env, "").strip():
            checks.append(("WARN", name, f"{raw.strip()}: overridden by environment ({env})."))
            continue
        try:
            opened = _open_configured_token(raw, config_path)
        except FleetTokenFileError as exc:
            checks.append(("FAIL", name, str(exc)))
            continue
        if opened is None:
            path = token_path(raw, config_path)
            checks.append(("WARN", name, f"{path or raw}: missing. Supply the token file or update {key}."))
        else:
            fd, detail = opened
            os.close(fd)
            checks.append(("OK", name, detail))
    return checks
