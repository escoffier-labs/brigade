"""Safe local operator bootstrap commands."""

from __future__ import annotations

from .. import scrub
from .adoption import (
    adoption_capture,
    adoption_capture_payload,
    adoption_import_issues,
    adoption_plan,
    adoption_plan_payload,
)
from .guide import (
    PROFILES,
    guide,
    guide_payload,
    plan,
    plan_payload,
)
from .health import (
    doctor,
    doctor_payload,
    status,
    status_payload,
    verify_harness,
    verify_harness_payload,
)
from .lifecycle import (
    bootstrap_portable,
    checkup,
    checkup_payload,
    init,
    quickstart,
    sync_tools,
)
from .migration import (
    migration_consolidate,
    migration_doctor,
    migration_import_issues,
    migration_status,
    migration_status_payload,
)
from .surfaces import (
    SURFACE_REVIEW_STATUSES,
    _run_read_only_command,
    surfaces_capture,
    surfaces_capture_payload,
    surfaces_doctor,
    surfaces_doctor_payload,
    surfaces_import_issues,
    surfaces_list,
    surfaces_review,
    surfaces_reviews,
)

__all__ = [
    "PROFILES",
    "SURFACE_REVIEW_STATUSES",
    "_run_read_only_command",
    "adoption_capture",
    "adoption_capture_payload",
    "adoption_import_issues",
    "adoption_plan",
    "adoption_plan_payload",
    "bootstrap_portable",
    "checkup",
    "checkup_payload",
    "doctor",
    "doctor_payload",
    "guide",
    "guide_payload",
    "init",
    "migration_consolidate",
    "migration_doctor",
    "migration_import_issues",
    "migration_status",
    "migration_status_payload",
    "plan",
    "plan_payload",
    "quickstart",
    "scrub",
    "status",
    "status_payload",
    "surfaces_capture",
    "surfaces_capture_payload",
    "surfaces_doctor",
    "surfaces_doctor_payload",
    "surfaces_import_issues",
    "surfaces_list",
    "surfaces_review",
    "surfaces_reviews",
    "sync_tools",
    "verify_harness",
    "verify_harness_payload",
]
