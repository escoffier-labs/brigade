"""Read-only isolation and unit-only cleanup for retained retired instances."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from types import MappingProxyType

from brigade import grokbot_ops
from . import connector_files as files
from .contracts import DotError
from .runtime_config import CLOUD_AGENT_RUNTIME_SCHEMA, MAX_RUNTIME_BYTES, Runtime
from .safe_state import private_directory, private_file

# Historical schemas and paths only. These entries cannot start a listener.
RETIRED = MappingProxyType(
    {
        name: (f"brigade.{name}.runtime.v1", Path(".brigade/grokbot/packs") / f"{name}.json")
        for name in ("grokbot-memory", "grokbot-inbox")
    }
)


def remove_command(argv: list[str], name: str) -> int:
    from brigade.cli.removed_commands import removed_profile

    parser = argparse.ArgumentParser(prog="brigade retired connector remove")
    parser.add_argument("--id", required=True)
    parser.add_argument("--target", type=Path, default=Path("."))
    parser.add_argument("--unit-dir", "--out", type=Path)
    parser.add_argument("--apply", action="store_true")
    args, unknown = parser.parse_known_args(argv)
    # Anything but a cleanup of this profile's own units gets the removal message and stop instruction.
    if unknown or args.id != name or args.unit_dir is None:
        return removed_profile(name)
    try:
        result = remove_units(name, unit_dir=args.unit_dir, apply=args.apply)
    except (files.PackError, OSError):
        print("error: connector configuration or operation refused", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def remove_units(name: str, *, unit_dir: Path | None, apply: bool) -> dict[str, Any]:
    # Match the base registry's directory and no-follow regular-file ownership checks,
    # using the listener renderer's name rather than the queue-role renderer's name.
    if name not in RETIRED or unit_dir is None:
        raise files.PackError("outside-owned-path")
    unit = unit_dir / f"brigade-{name}.service"
    if unit_dir.is_symlink() or not unit_dir.is_dir() or unit.is_symlink() or not unit.is_file():
        raise files.PackError("outside-owned-path")
    paths = [
        unit_dir / filename
        for filename in (f"brigade-{name}.service", f"brigade-{name}-watch.service", f"brigade-{name}-watch.timer")
        if files._exists_or_link(unit_dir / filename)
    ]
    for path in paths:
        files._assert_removable(path, config_file=False)
    if apply:
        for path in paths:
            grokbot_ops.remove_regular_file(path)
    return dict(action="remove", apply=apply, pack_id=name, paths=[str(p) for p in paths])


def isolated_from_retired(target: Path, runtime: Runtime) -> bool:
    from .lifecycle import _same_location, _same_token

    for name, (schema, relative) in RETIRED.items():
        instance = target / relative
        if not os.path.lexists(instance):
            continue
        try:
            config = json.loads(grokbot_ops._read_regular_text(instance, max_bytes=MAX_RUNTIME_BYTES))
            if config["pack_id"] != name or config["schema"] != "brigade.grokbot.connector-instance.v1":
                raise ValueError
            path = Path(config["runtime_path"])
            if not path.is_absolute():
                raise ValueError
            parent = private_directory(path.parent)
            try:
                private_file(path.lstat())
                raw = json.loads(grokbot_ops._read_regular_text(path, max_bytes=MAX_RUNTIME_BYTES))
            finally:
                os.close(parent)
            if raw["schema"] != schema:
                raise ValueError
            principal = raw["hub"]["principal"]
            token = Path(raw["hub"]["message_token_file"])
            state = Path(raw["paths"]["state_dir"])
            audience, resource = raw["auth"]["audience"], raw["auth"]["resource"]
            if (
                not isinstance(principal, str)
                or not token.is_absolute()
                or not state.is_absolute()
                or not isinstance(audience, str)
                or not isinstance(resource, str)
            ):
                raise ValueError
        except (DotError, OSError, ValueError, KeyError, TypeError, RecursionError):
            raise DotError("not_isolated_from_dot", reason=f"cannot load the {name} instance") from None
        tokens = [runtime.hub.message_token_file]
        if runtime.auth.token_file is not None:
            tokens.append(runtime.auth.token_file)
        if (
            runtime.hub.principal == principal
            or any(_same_location(t, token) or _same_token(t, token) for t in tokens)
            or _same_location(runtime.paths.state_dir, state)
            or (
                (runtime.schema != CLOUD_AGENT_RUNTIME_SCHEMA or runtime.auth.mode == "access")
                and (runtime.auth.audience == audience or runtime.auth.resource == resource)
            )
        ):
            return False
    return True
