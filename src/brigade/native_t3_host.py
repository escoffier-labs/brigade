"""The T3 binding of the host-neutral native child core.

This is the one place the persisted T3 spellings are supplied to the core:
the ``native-t3`` run kind, the ``t3-`` run prefix, the ``brigade.native-t3.*``
and ``brigade.t3.*`` digest domains and the ``brigade.t3_*.v1`` report schemas.
Nothing here changes a byte that an existing run or digest already holds.

The Orca adapter (workstream 2 of the native child epic) plugs in the same way:
it supplies its own ``NativeChildHost`` and its own verbs, and reuses the core.
"""

from __future__ import annotations

from typing import Any, Mapping

from . import roster as roster_mod
from .native_child_host import NativeChildHost, TargetUnresolved
from .native_t3_structural import DRIVER_ADAPTERS, STRUCTURAL_GAPS, UNSUPPORTED, structural_gap

CANCEL_EVENT = "task.native-cancel-requested"


def _declarations(roster: roster_mod.Roster) -> Mapping[str, Mapping[str, Any]]:
    return roster.t3_targets


def _driver_adapter(target: Any) -> str | None:
    return DRIVER_ADAPTERS.get(target.driver)


def _resolve_target(roster: roster_mod.Roster, seat: str, source_revision: str) -> Any:
    # Imported here because enrollment itself binds to this host description.
    from . import native_t3_enrollment as enrollment

    try:
        return enrollment._declared_target(roster, seat, source_revision=source_revision)
    except enrollment.EnrollmentError as exc:
        raise TargetUnresolved(missing=exc.code == "seat-unbound") from None


HOST = NativeChildHost(
    run_kind="native-t3",
    run_prefix="t3-",
    run_noun="governed native T3 run",
    roster_digest_domain="brigade.native-t3.roster-digest.v1",
    idempotency_domain="brigade.native-t3.idempotency.v1",
    seat_digest_domain="brigade.t3.seat.v1",
    ack_schema="brigade.t3_ack.v1",
    finalize_schema="brigade.t3_finalize.v1",
    status_schema="brigade.t3_status.v1",
    declarations_digest_key="t3_targets",
    unsupported_code=UNSUPPORTED,
    cancel_event=CANCEL_EVENT,
    structural_gaps=STRUCTURAL_GAPS,
    declarations=_declarations,
    structural_gap=structural_gap,
    driver_adapter=_driver_adapter,
    resolve_target=_resolve_target,
)
