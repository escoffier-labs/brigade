"""Host contract for governed native child delegation.

Governed native delegation lets an external parent session run a child task
under Brigade's admission, capacity, journal and custody rules. Everything in
the ``native_child_*`` modules is host neutral. Whatever differs between hosts
is expressed here as one frozen description that a host adapter supplies, so a
second host can plug in without touching the core.

The adapter owns the persisted spellings that name the host (schema strings,
digest domains, the run prefix and run kind), the closed structural
classification of a retained target, and the roster declaration accessor.
The core never imports an adapter module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from . import roster as roster_mod


class NativeChildError(RuntimeError):
    """A bounded governed-verb refusal published before any output."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class NativeChildCapacityError(RuntimeError):
    """A bounded capacity-gate refusal, safe for a fixed diagnostic."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class NativeChildCustodyError(RuntimeError):
    """A bounded enrollment or custody refusal published before any native invocation."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class TargetUnresolved(Exception):
    """A host could not resolve the current declaration for a seat.

    ``missing`` is true when the seat has no declaration at all, and false when
    a declaration exists but is not valid.
    """

    def __init__(self, *, missing: bool) -> None:
        self.missing = missing
        super().__init__("seat target unresolved")


@dataclass(frozen=True)
class NativeChildHost:
    """Everything a host adapter supplies to the host-neutral core."""

    # Persisted run identity. Changing either breaks every existing run.
    run_kind: str
    run_prefix: str
    # How a refusal names a run of this host, e.g. "the run is not a <run_noun>".
    run_noun: str
    # Persisted digest domains and report schemas, passed through verbatim.
    roster_digest_domain: str
    idempotency_domain: str
    seat_digest_domain: str
    ack_schema: str
    finalize_schema: str
    status_schema: str
    # The roster digest keeps the declaration table under this key.
    declarations_digest_key: str
    # Refusal code for a target the host structurally cannot run.
    unsupported_code: str
    # Event the status report reads as "cancel requested".
    cancel_event: str
    # The closed set of structural-gap tokens the host classifier can return.
    structural_gaps: frozenset[str]
    declarations: Callable[[roster_mod.Roster], Mapping[str, Mapping[str, Any]]]
    structural_gap: Callable[[Any], str | None]
    # The Brigade adapter whose provider and model policy apply to a retained
    # target, or ``None`` when the target names a driver the host cannot run.
    driver_adapter: Callable[[Any], str | None]
    # Resolve the current declaration for a seat, raising ``TargetUnresolved``.
    resolve_target: Callable[[roster_mod.Roster, str, str], Any]
