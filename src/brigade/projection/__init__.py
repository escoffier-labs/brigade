"""Internal projection transaction kernel (issue #910).

Standard library only. A projector supplies an immutable plan; the kernel
either commits every destination mutation or restores every destination to its
captured before-image. Production command modules do not belong here.
"""

from __future__ import annotations

from . import kernel
from .kernel import (
    ABSENT,
    DEPENDENCY_DECISION,
    SCHEMA_VERSION,
    DestinationChangedError,
    DriftError,
    FailureInjector,
    OverlapBlockedError,
    PlanError,
    ProjectionError,
    Receipt,
    build_plan,
    execute,
    mutation,
    operation_dir,
    recover,
    unfinished_operations,
)

__all__ = [
    "ABSENT",
    "DEPENDENCY_DECISION",
    "SCHEMA_VERSION",
    "DestinationChangedError",
    "DriftError",
    "FailureInjector",
    "OverlapBlockedError",
    "PlanError",
    "ProjectionError",
    "Receipt",
    "build_plan",
    "execute",
    "kernel",
    "mutation",
    "operation_dir",
    "recover",
    "unfinished_operations",
]
