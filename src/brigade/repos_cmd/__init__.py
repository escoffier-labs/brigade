"""Repository fleet command package facade.

Re-exports the flat ``repos_cmd.X`` surface while implementation lives in
family submodules. The module setattr bridge keeps legacy facade-level
monkeypatches working by forwarding patched symbols to owning submodules.
"""

# ruff: noqa: F401
from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

from . import (
    adoption,
    actions_dispatch,
    constants,
    fleet,
    fleet_health,
    friction_fleet,
    release_ops,
    release_train,
    sweeps,
)

from .adoption import adoption_payload, adoption_repair, adoption_report

from .constants import (
    BACKLOG_STALE_DAYS,
    RELEASE_EVIDENCE_STATUSES,
    RELEASE_EVIDENCE_STEPS,
    RELEASE_WAIVER_SCOPES,
    REQUIRED_RELEASE_EVIDENCE_STEPS,
    RepoEntry,
    SweepCommand,
    _now,
    config_path,
)

from .fleet import (
    _repo_summaries,
    _repo_summary,
    discover_plan,
    doctor,
    ingest_fleet,
    init,
    list_repos,
    rearm,
    scan,
    show,
)

from .sweeps import (
    health_commands,
    import_issues,
    report_archive,
    report_build,
    report_closeout,
    report_list,
    report_plan,
    report_show,
    sweep_closeout,
    sweep_plan,
    sweep_run,
    sweep_runs,
    sweep_show,
)

from .actions_dispatch import (
    _read_actions,
    _write_actions,
    actions_context_build,
    actions_context_plan,
    actions_dispatch_apply,
    actions_dispatch_plan,
    actions_dispatch_report,
    actions_reconcile,
)

from .release_train import (
    _read_release_actions,
    latest_release_train,
    release_actions_build,
    release_actions_defer,
    release_actions_done,
    release_actions_list,
    release_actions_plan,
    release_actions_show,
    release_actions_start,
    release_archive,
    release_build,
    release_closeout,
    release_compare,
    release_list,
    release_plan,
    release_show,
)

from .release_ops import (
    release_actions_archive_completed,
    release_activity,
    release_audit,
    release_checklist,
    release_evidence_list,
    release_evidence_plan,
    release_evidence_record,
    release_evidence_show,
    release_hygiene,
    release_import_issues,
    release_manifest,
    release_matrix,
    release_ready,
    release_reconcile,
    release_report,
    release_summary,
    release_waiver_doctor,
    release_waiver_import_issues,
    release_waiver_list,
    release_waiver_record,
    release_waiver_renew,
    release_waiver_revoke,
    release_waiver_show,
    release_waiver_templates,
)

from .fleet_health import (
    _find_action,
    actions_archive_completed,
    actions_build,
    actions_defer,
    actions_done,
    actions_health,
    actions_list,
    actions_plan,
    actions_show,
    actions_start,
    daily_use_health,
    first_run_plan,
    health,
    release_train_health,
)

from .friction_fleet import friction_scan, friction_show

_MODULES = (
    constants,
    fleet,
    adoption,
    sweeps,
    actions_dispatch,
    release_train,
    release_ops,
    fleet_health,
    friction_fleet,
)


class _ReposFacade(ModuleType):
    def __setattr__(self, name: str, value: Any) -> None:
        super().__setattr__(name, value)
        for module in _MODULES:
            if hasattr(module, name):
                setattr(module, name, value)


sys.modules[__name__].__class__ = _ReposFacade

for _module in _MODULES:
    for _name in getattr(_module, "__all__", ()):
        if not _name.startswith("__"):
            globals()[_name] = getattr(_module, _name)

__all__ = tuple(name for name in globals() if not name.startswith("__"))
