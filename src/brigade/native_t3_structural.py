"""Closed structural classification for one governed native T3 target.

A structural gap means T3 Code cannot run the work at all. It is decided from
the seat's resolved target declaration alone, so it is stable at submission and
never depends on policy, quota, capacity, authentication, entitlement, a held
task, or parent-reported capability evidence. Those stay refusals, and none of
them may be reported as a structural gap.

The classifier is pure over its input. It grants nothing and writes nothing.
"""

from __future__ import annotations

from .native_t3_contract import INHERIT_MODE, INTERACTION_MODES, RUNTIME_MODES, NativeTarget

UNSUPPORTED = "t3-unsupported"

DRIVER_UNSUPPORTED = "driver-unsupported"
TARGET_REMOTE_UNSUPPORTED = "target-remote-unsupported"
TARGET_MODE_INVALID = "target-mode-invalid"

# The only tokens this module can return. Every other refusal keeps its own
# code, so admittance can never be reshaped into an unsupported declaration.
STRUCTURAL_GAPS = frozenset({DRIVER_UNSUPPORTED, TARGET_REMOTE_UNSUPPORTED, TARGET_MODE_INVALID})

# The closed set of T3 driver literals, mapped onto the Brigade adapter whose
# provider and model policy apply. A driver outside this table is not a T3
# driver today, so no target using it can run.
DRIVER_ADAPTERS = {"codex": "codex", "claudeAgent": "claude"}


def structural_gap(target: NativeTarget) -> str | None:
    """Return the closed structural-gap token for one resolved target, or None."""
    runtime_modes = RUNTIME_MODES | {INHERIT_MODE}
    interaction_modes = INTERACTION_MODES | {INHERIT_MODE}
    if target.runtime_mode not in runtime_modes or target.interaction_mode not in interaction_modes:
        return TARGET_MODE_INVALID
    if target.target_environment != target.origin_environment:
        return TARGET_REMOTE_UNSUPPORTED
    if target.driver not in DRIVER_ADAPTERS:
        return DRIVER_UNSUPPORTED
    return None
