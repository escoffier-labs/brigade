"""brigade projection recover command."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .projection.kernel import DestinationChangedError, ProjectionError, recover as recover_operation


def recover(operation_id: str, *, target: Path, force: bool, json_output: bool) -> int:
    try:
        receipt = recover_operation(operation_id, target=target, force=force)
    except (DestinationChangedError, ProjectionError) as exc:
        print(exc.diagnostic, file=sys.stderr)
        return 2
    payload = receipt.to_dict()
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        line = f"{payload['terminal_state']} {payload['operation_id']}"
        command = payload.get("recovery_command")
        if command:
            line = f"{line}\n{command}"
        print(line)
    return 0
