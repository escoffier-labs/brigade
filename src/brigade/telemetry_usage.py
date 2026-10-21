"""Closed-enum identity for the Python CLI's passive version request.

Tier 1 collection is not enabled here. Consent wording still requires the
operator sign-off specified in issue #1861.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path

from . import distribution

CHANNELS = frozenset({"pipx", "pip", "uvx", "npm", "source", "plugin"})


def install_channel(env: Mapping[str, str]) -> str:
    """Return a channel label, never a caller-supplied arbitrary value."""
    if distribution.is_npm():
        return "npm"
    override = env.get("BRIGADE_INSTALL_CHANNEL")
    if override in CHANNELS:
        return override
    prefix = Path(sys.prefix)
    if (prefix / "pipx_metadata.json").is_file():
        return "pipx"
    try:
        with (prefix / "pyvenv.cfg").open(encoding="utf-8") as config:
            for line in config.read(4096).splitlines():
                key, separator, value = line.partition("=")
                if key.strip() == "uv" and separator and value.strip():
                    return "uvx"
    except (OSError, UnicodeError):
        pass
    if {"site-packages", "dist-packages"}.intersection(Path(__file__).parts):
        return "pip"
    return "source"


def uncounted(env: Mapping[str, str]) -> bool:
    """Opt-outs preserve version retrieval with an uncounted generic agent."""
    dnt = env.get("DO_NOT_TRACK", "").strip().lower()
    return dnt not in {"", "0", "off", "false", "no"} or env.get("BRIGADE_TELEMETRY", "").strip().lower() in {
        "0",
        "off",
        "false",
    }
