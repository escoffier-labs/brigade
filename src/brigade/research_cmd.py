# src/brigade/research_cmd.py
from __future__ import annotations

import hashlib
import json as _json
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Sequence
from threading import Event, Thread
from typing import Any, Callable, Dict, List, Mapping, Optional
from urllib.parse import parse_qsl, quote, urlparse, urlunparse

from .localio import parse_iso_datetime, utc_now_iso as _now
from .research import config as rconfig
from .research import handoff as handoffmod
from .research import registry
from .research import report as reportmod
from .research.engine import ResearchEngine
from .research.sources import cli as clisrc
from .research.sources import local as localsrc
from .research.sources.browser_ai import BrowserAiProvider
from .research.types import (
    Caps,
    CitationAudit,
    Finding,
    ResearchLanes,
    ResearchProfile,
    ResearchResult,
    ResearchRunError,
    ResumeState,
    SourceEnvelope,
)
from .roster import Agent
from .run_budget import BudgetCompatibilityError, BudgetCoordinator, BudgetPolicyError, RunBudgetDeclaration
from .selection import WRITER_INBOXES


class ResearchCommandFailure(Exception):
    """Typed safe boundary for receipt-write lifecycle failures.

    Raised instead of letting LifecycleJournalError / RetainRunLockError escape
    to the CLI as a traceback. Callers must not mutate authoritative run.json
    after the failed gate; diagnostics stay concise and path-safe.
    """

    def __init__(
        self,
        *,
        run_id: str,
        failure_kind: str,
        detail: str,
        failure_phase: str = "admission",
        status: str = "failed",
        exit_code: int = 1,
    ) -> None:
        super().__init__(detail)
        self.run_id = run_id
        self.failure_kind = failure_kind
        self.detail = detail
        self.failure_phase = failure_phase
        self.status = status
        self.exit_code = exit_code

    def diagnostic_payload(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "failure_kind": self.failure_kind,
            "failure_phase": self.failure_phase,
            "detail": self.detail,
            "blockers": [self.detail],
        }


def _dict_or_empty(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list_or_empty(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _require_cli_run_id(run_id: str) -> str:
    """Convert invalid run_id Values into normal CLI SystemExit errors."""
    try:
        return registry.validate_run_id(run_id)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None


def _emit_cli_run_id_error(exc: BaseException, *, json_output: bool) -> int:
    message = str(exc)
    if json_output:
        print(_json.dumps({"error": message, "failure_kind": "invalid-run-id"}, indent=2, sort_keys=True))
    else:
        print(message)
    return 2


def _emit_missing_run_error(run_id: str, *, json_output: bool) -> int:
    message = f"no such run: {run_id}"
    if json_output:
        print(
            _json.dumps(
                {
                    "error": message,
                    "run_id": run_id,
                    "failure_kind": "no-such-run",
                    "failure_phase": "admission",
                    "detail": message,
                    "status": "failed",
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(message)
    return 1


def _invalid_config_failure(*, detail: str, run_id: str = "") -> ResearchCommandFailure:
    return ResearchCommandFailure(
        run_id=run_id,
        failure_kind="invalid-config",
        failure_phase="admission",
        detail=_operator_safe_detail(detail) or "invalid research configuration",
        exit_code=2,
    )


def _invalid_sidecar_failure(*, run_id: str, detail: str) -> ResearchCommandFailure:
    return ResearchCommandFailure(
        run_id=run_id,
        failure_kind="invalid-sidecar",
        failure_phase="admission",
        detail=_operator_safe_detail(detail) or "research sidecar is missing or invalid",
        exit_code=2,
    )


def _build_validated_caps(**overrides: Any) -> Caps:
    cap_fields = Caps.__dataclass_fields__
    caps = Caps.build(**{name: value for name, value in overrides.items() if name in cap_fields})
    for name, value in vars(caps).items():
        zero_allowed = name in {"min_rounds", "max_empty_rounds", "synthesis_window"}
        if isinstance(value, bool) or not isinstance(value, int) or (value < 0 if zero_allowed else value <= 0):
            requirement = "a non-negative integer" if zero_allowed else "a positive integer"
            raise ValueError(f"caps.{name} must be {requirement}")
    return caps


def _admission_failure(*, run_id: str, detail: str, failure_kind: str = "admission") -> ResearchCommandFailure:
    return ResearchCommandFailure(
        run_id=run_id,
        failure_kind=failure_kind,
        failure_phase="admission",
        detail=_operator_safe_detail(detail) or detail,
        exit_code=2,
    )


def _research_run_exists(target: Path, run_id: str) -> bool:
    return registry.standard_run_dir(target, run_id).is_dir() or registry.legacy_run_dir(target, run_id).is_dir()


def _unique_research_run_id(target: Path, question: str, preferred: str) -> str:
    """Return preferred when free; otherwise mint a collision-safe sibling id."""
    if not _research_run_exists(target, preferred):
        return preferred
    import secrets
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    base = registry.slug(question)
    for _ in range(64):
        candidate = f"{stamp}-{base}-{secrets.token_hex(3)}"
        if not _research_run_exists(target, candidate):
            return candidate
    raise ResearchCommandFailure(
        run_id=preferred,
        failure_kind="admission",
        detail="unable to allocate a unique research run id",
        exit_code=2,
    )


def _wall_clock_resume_refusal(target: Path, run_id: str) -> ResearchCommandFailure | None:
    """Refuse resume when persisted wall-clock budget is already exhausted.

    Must run before ``_reopen_standard_run`` mutates the receipt or journal.
    """
    from datetime import datetime, timezone

    from .run_budget import declaration_from_persisted_artifact

    run_directory = registry.standard_run_dir(target, run_id)
    run_path = run_directory / "run.json"
    if not run_path.is_file():
        return None
    try:
        receipt = _json.loads(run_path.read_text(encoding="utf-8"))
    except (OSError, _json.JSONDecodeError):
        return None
    if not isinstance(receipt, dict):
        return None
    budget = receipt.get("run_budget")
    if not isinstance(budget, dict):
        return None
    try:
        declaration = declaration_from_persisted_artifact(budget)
    except Exception:  # noqa: BLE001 - malformed budget is not a silent resume gate
        return None
    wall = declaration.wall_clock_seconds
    if wall is None:
        return None
    started_at = parse_iso_datetime(receipt.get("started_at"))
    if started_at is None:
        return None
    elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
    if elapsed < float(wall):
        return None
    return _admission_failure(
        run_id=run_id,
        failure_kind="budget-exhausted",
        detail="run wall-clock budget exhausted; resume refused",
    )


class _LocalIndexProvider:
    trust = "local"
    source_type = "local"
    source_id = "local"

    def __init__(self, index: Any) -> None:
        self._index = index
        self._pages: dict[str, dict[str, Any]] = {}

    def search(self, query: str, limit: int) -> list[dict[str, str]]:
        hits: list[dict[str, str]] = []
        for hit in self._index.search(query, limit=limit):
            url = str(hit.get("source") or "")
            if not url:
                continue
            title = str(hit.get("title") or url)
            self._pages[url] = {
                "success": True,
                "content": str(hit.get("text") or ""),
                "title": title,
            }
            hits.append({"url": url, "title": title, "trust": self.trust})
        return hits

    def fetch(self, url: str) -> dict[str, Any]:
        return dict(self._pages.get(url, {"success": False, "content": "", "title": ""}))


def _build_run_invoker(
    target: Path,
    *,
    run_id: str,
    process_registry: Any | None = None,
    budget_callbacks: ResearchBudgetCallbacks | None = None,
    receipt_admission: Event | None = None,
):
    from . import roster as roster_mod
    from .proc import ProcessRegistry
    from .run_seat import SeatInvoker

    roster = roster_mod.load_roster(roster_mod.resolve_roster_path(target))
    output_dir = registry.run_dir(target, run_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    registry_obj = process_registry if process_registry is not None else ProcessRegistry()
    callback_kwargs: dict[str, Any] = {}
    if budget_callbacks is not None:
        callback_kwargs = {
            "on_dispatch_requested": budget_callbacks.on_dispatch_requested,
            "on_dispatch_observed": budget_callbacks.on_dispatch_observed,
            "on_dispatch_completed": budget_callbacks.on_dispatch_completed,
            "on_dispatch_failed": budget_callbacks.on_dispatch_failed,
        }
    return SeatInvoker(
        roster=roster,
        cwd=target,
        run_id=run_id,
        process_registry=registry_obj,
        output_dir=output_dir,
        receipt_admission=receipt_admission,
        **callback_kwargs,
    )


@dataclass
class ResearchBudgetCallbacks:
    coordinator: BudgetCoordinator
    append_event: Callable[[str, dict[str, object], str], object]
    attempts: dict[str, int] = field(default_factory=dict)

    def _next_attempt(self, seat: str) -> int:
        attempt = self.attempts.get(seat, 0) + 1
        self.attempts[seat] = attempt
        return attempt

    def _record(self, event_type: str, seat: str, attempt: int) -> int:
        self.append_event(
            event_type,
            {"seat": seat, "attempt": attempt},
            f"{event_type}:{seat}:{attempt}",
        )
        return attempt

    def on_dispatch_requested(self, agent: Agent) -> int | None:
        return self.coordinator.reserve_and_record_dispatch(
            seat=agent.name,
            allocate_attempt=lambda: self._next_attempt(agent.name),
            record_requested=lambda attempt: self._record("run.dispatch.requested", agent.name, attempt),
        )

    def on_dispatch_observed(self, agent: Agent, attempt: int) -> None:
        self._record("run.dispatch.observed", agent.name, attempt)

    def on_dispatch_completed(self, agent: Agent, attempt: int) -> None:
        self._record("run.dispatch.completed", agent.name, attempt)

    def on_dispatch_failed(self, agent: Agent, attempt: int) -> None:
        self._record("run.dispatch.failed", agent.name, attempt)


class CancellationWatcher(Thread):
    def __init__(
        self,
        *,
        target: Path,
        run_id: str,
        cancelled: Event,
        process_registry: Any,
        poll_seconds: float = 0.25,
    ) -> None:
        super().__init__(name=f"research-cancel-{run_id}", daemon=True)
        self.target = target
        self.run_id = run_id
        self.cancelled = cancelled
        self.process_registry = process_registry
        self.poll_seconds = poll_seconds
        self.stopped = Event()
        self._cancel_fired = False

    def stop(self) -> None:
        self.stopped.set()

    def _poll_once(self) -> bool:
        try:
            sidecar = registry.read_research(self.target, self.run_id)
        except (OSError, ValueError):
            # A missing or momentarily unreadable sidecar must not kill the watcher.
            return False
        if sidecar.get("cancel_requested_at"):
            self.cancelled.set()
            if not self._cancel_fired:
                self._cancel_fired = True
                self.process_registry.cancel()
            return True
        return False

    def run(self) -> None:
        # Observe a cancel requested before watcher startup immediately.
        if self._poll_once():
            return
        while not self.stopped.wait(self.poll_seconds):
            if self._poll_once():
                return


def resume_state(target: Path, run_id: str) -> ResumeState:
    from .research.types import ReviewResult, SynthesisRecord

    sidecar = registry.read_research(target, run_id)
    phases = sidecar.get("phases") or {}
    planning = phases.get("planning") or {}
    discovery = phases.get("discovery") or {}
    extraction = phases.get("extraction") or {}
    synthesis = phases.get("synthesis") or {}
    review = phases.get("review") or {}
    sources = registry.read_verified_artifact(target, run_id, discovery.get("artifact"))
    findings = registry.read_verified_artifact(target, run_id, extraction.get("artifact"))
    report = registry.read_verified_artifact(target, run_id, synthesis.get("artifact"))
    audit = registry.read_verified_artifact(target, run_id, review.get("artifact"))
    review_meta = review.get("review") if isinstance(review.get("review"), dict) else None
    review_result = None
    if isinstance(review_meta, dict) and review.get("status") == "completed":
        try:
            review_result = ReviewResult(
                accepted=bool(review_meta["accepted"]),
                detail=str(review_meta.get("detail") or ""),
                rejected_claims=tuple(str(item) for item in (review_meta.get("rejected_claims") or ())),
                seat=str(review_meta["seat"]),
                attempt_id=str(review_meta["attempt_id"]),
            )
        except (KeyError, TypeError):
            review_result = None
    synthesis_record = None
    if synthesis.get("status") == "completed" and synthesis.get("seat") and synthesis.get("attempt_id"):
        synthesis_record = SynthesisRecord(
            seat=str(synthesis["seat"]),
            attempt_id=str(synthesis["attempt_id"]),
            requested_model=synthesis.get("requested_model")
            if isinstance(synthesis.get("requested_model"), str)
            else None,
            observed_model=str(synthesis.get("observed_model") or "resumed"),
        )

    # Transitive invalidation: a missing/bad upstream artifact drops all later
    # resume fields so stale completed phases cannot be reused alone.
    plan = planning.get("value") if planning.get("status") == "completed" else None
    sources_ok = discovery.get("status") == "completed" and sources is not None
    findings_ok = sources_ok and extraction.get("status") == "completed" and findings is not None
    report_ok = findings_ok and synthesis.get("status") == "completed" and report is not None
    audit_ok = report_ok and review.get("status") == "completed" and audit is not None
    review_ok = audit_ok and review_result is not None
    return ResumeState(
        plan=plan if isinstance(plan, str) else None,
        sources=sources if sources_ok else None,
        findings=findings if findings_ok else None,
        report=report if report_ok else None,
        audit=audit if audit_ok else None,
        review=review_result if review_ok else None,
        synthesis=synthesis_record if report_ok else None,
    )


def _load_roster(target: Path):
    from . import roster as roster_mod

    return roster_mod.load_roster(roster_mod.resolve_roster_path(target))


def _load_research_config(target: Path, profile: Optional[str] = None):
    try:
        cfg = rconfig.load(target)
        research_profile = cfg.profile(profile)
    except (ValueError, FileNotFoundError, OSError) as exc:
        raise _invalid_config_failure(detail=str(exc)) from exc
    return cfg, research_profile


def _budget_append_event(run_directory: Path, workspace: Path):
    from . import run_lifecycle

    def append_event(event_type: str, payload: dict[str, object], key: str) -> object:
        # Lifecycle journal is authoritative; never fall back to a side event stream.
        return run_lifecycle.record_lifecycle_event(
            run_directory,
            event_type=event_type,
            payload=dict(payload),
            idempotency_key=key,
            workspace=workspace,
        )

    return append_event


def _normalize_budget_failure(exc: BudgetPolicyError, *, phase: str) -> ResearchRunError:
    kind = "budget-exhausted" if exc.code == "budget_exhausted" else "budget-denied"
    return ResearchRunError(phase, kind, str(exc))


def _reopen_standard_run(target: Path, run_id: str) -> None:
    """Clear stale terminal receipt fields inside an acquired run lock.

    Goes through the authoritative aboyeur writer so research reopen emits the
    research-specific recovery transition (not approval-gated ``run.resumed``).
    """
    from . import aboyeur

    run_directory = registry.standard_run_dir(target, run_id)
    run_path = run_directory / "run.json"
    if not run_path.is_file():
        raise ResearchRunError("admission", "lifecycle-journal", "run.json missing during reopen")
    try:
        receipt = _json.loads(run_path.read_text(encoding="utf-8"))
    except (OSError, _json.JSONDecodeError) as exc:
        raise ResearchRunError(
            "admission",
            "lifecycle-journal",
            "run.json unreadable during reopen",
        ) from exc
    if not isinstance(receipt, dict):
        raise ResearchRunError("admission", "lifecycle-journal", "run.json is not an object during reopen")
    aboyeur.update_run_receipt(
        run_directory,
        status="running",
        finished_at=None,
        duration_seconds=None,
        failure=None,
        failure_kind=None,
        failure_phase=None,
        error=None,
    )
    registry.update_research(
        target,
        run_id,
        status="running",
        failure_kind=None,
        failure_phase=None,
        detail=None,
        blockers=[],
        cancel_requested_at=None,
        cancel_requested_by=None,
    )


def _persist_budget_projection(
    target: Path,
    run_id: str,
    *,
    declaration: RunBudgetDeclaration,
    projection: dict[str, Any],
) -> None:
    from . import aboyeur

    run_directory = registry.standard_run_dir(target, run_id)
    budget_payload = declaration.to_payload()
    registry.update_research(
        target,
        run_id,
        run_budget=budget_payload,
        run_budget_projection=projection,
    )
    run_path = run_directory / "run.json"
    if not run_path.is_file():
        raise ResearchRunError("admission", "lifecycle-journal", "run.json missing during budget projection")
    try:
        receipt = _json.loads(run_path.read_text(encoding="utf-8"))
    except (OSError, _json.JSONDecodeError) as exc:
        raise ResearchRunError(
            "admission",
            "lifecycle-journal",
            "run.json unreadable during budget projection",
        ) from exc
    if not isinstance(receipt, dict):
        raise ResearchRunError(
            "admission",
            "lifecycle-journal",
            "run.json is not an object during budget projection",
        )
    aboyeur.update_run_receipt(
        run_directory,
        run_budget=budget_payload,
        run_budget_projection=projection,
    )


def _terminate_standard_run(
    target: Path,
    run_id: str,
    *,
    status: str,
    failure_phase: str,
    failure_kind: str,
    detail: str,
    blockers: list[str] | None = None,
) -> None:
    from . import aboyeur

    run_directory = registry.standard_run_dir(target, run_id)
    run_path = run_directory / "run.json"
    if run_path.is_file():
        try:
            receipt = _json.loads(run_path.read_text(encoding="utf-8"))
        except (OSError, _json.JSONDecodeError) as exc:
            raise ResearchRunError(
                failure_phase,
                "lifecycle-journal",
                "run.json unreadable during termination",
            ) from exc
        if not isinstance(receipt, dict):
            raise ResearchRunError(
                failure_phase,
                "lifecycle-journal",
                "run.json is not an object during termination",
            )
        # Clear prior finished_at so record_run_termination can replace details.
        if receipt.get("finished_at"):
            aboyeur.update_run_receipt(
                run_directory,
                finished_at=None,
                duration_seconds=None,
            )
        aboyeur.record_run_termination(
            run_directory,
            status=status,
            failure_phase=failure_phase,
            failure_kind=failure_kind,
            detail=detail,
        )
        aboyeur.update_run_receipt(
            run_directory,
            failure_kind=failure_kind,
            failure_phase=failure_phase,
            error=detail,
        )
    registry.update_research(
        target,
        run_id,
        status=status,
        failure_kind=failure_kind,
        failure_phase=failure_phase,
        detail=detail,
        blockers=blockers or [],
    )


def _complete_standard_run(
    target: Path,
    run_id: str,
    *,
    stats: dict[str, Any],
    artifacts: dict[str, Any],
    blockers: list[str] | None = None,
    budget_projection: dict[str, Any] | None = None,
    artifact_refs: dict[str, Any] | None = None,
) -> None:
    from datetime import datetime, timezone

    from . import aboyeur

    run_directory = registry.standard_run_dir(target, run_id)
    finished = datetime.now(timezone.utc)
    finished_at = finished.isoformat()
    fields: dict[str, Any] = {
        "status": "completed",
        "stats": stats,
        "artifacts": artifacts,
        "blockers": blockers or [],
        "completed_at": finished_at,
        "current_phase": "publishing",
        "failure_kind": None,
        "failure_phase": None,
        "detail": None,
    }
    if budget_projection is not None:
        fields["run_budget_projection"] = budget_projection
    if artifact_refs is not None:
        fields["artifact_refs"] = artifact_refs
    registry.update_research(target, run_id, **fields)
    run_path = run_directory / "run.json"
    if not run_path.is_file():
        raise ResearchRunError("publishing", "lifecycle-journal", "run.json missing during completion")
    try:
        receipt = _json.loads(run_path.read_text(encoding="utf-8"))
    except (OSError, _json.JSONDecodeError) as exc:
        raise ResearchRunError(
            "publishing",
            "lifecycle-journal",
            "run.json unreadable during completion",
        ) from exc
    if not isinstance(receipt, dict):
        raise ResearchRunError(
            "publishing",
            "lifecycle-journal",
            "run.json is not an object during completion",
        )
    duration = None
    started = parse_iso_datetime(receipt.get("started_at"))
    if started is not None:
        duration = max(0.0, round((finished - started).total_seconds(), 3))
    update_fields: dict[str, Any] = {
        "status": "completed",
        "finished_at": finished_at,
        "duration_seconds": duration,
        "failure": None,
        "failure_kind": None,
        "failure_phase": None,
        "error": None,
        "artifacts": artifacts,
        "provenance": artifact_refs or artifacts,
    }
    if budget_projection is not None:
        update_fields["run_budget_projection"] = budget_projection
    aboyeur.update_run_receipt(run_directory, **update_fields)


def _publish_final_artifacts(
    target: Path,
    run_id: str,
    *,
    question: str,
    result: ResearchResult,
    cancelled: Event | None = None,
) -> dict[str, Any]:
    def _check() -> None:
        if cancelled is not None and cancelled.is_set():
            raise ResearchRunError("publishing", "cancelled", "research run cancelled")

    _check()
    findings = list(result.findings)
    sources_list = list(result.sources)
    finding_ref = registry.write_findings(target, run_id, findings)
    _check()
    # Citation audit was already persisted at review; refresh digest refs for finals.
    audit_ref = registry.write_citation_audit(target, run_id, result.citation_audit)
    _check()
    md = reportmod.render_markdown(
        question=question,
        markdown_report=result.report,
        findings=findings,
        sources=sources_list,
    )
    html = reportmod.render_html(
        question=question,
        markdown_report=result.report,
        findings=findings,
        sources=sources_list,
        stats=result.stats,
    )
    ho = handoffmod.render_handoff(
        question=question,
        markdown_report=result.report,
        findings=findings,
        sources=sources_list,
        stats=result.stats,
    )
    _check()
    report_md_ref = registry.write_text_artifact(target, run_id, registry.REPORT_MD_ARTIFACT, md)
    _check()
    report_html_ref = registry.write_text_artifact(target, run_id, registry.REPORT_HTML_ARTIFACT, html)
    _check()
    handoff_ref = registry.write_text_artifact(target, run_id, registry.HANDOFF_ARTIFACT, ho)
    _check()
    return {
        "report_md": report_md_ref,
        "report_html": report_html_ref,
        "handoff": handoff_ref,
        "findings": finding_ref,
        "citation_audit": audit_ref,
    }


def _phase_graph_update(
    target: Path,
    run_id: str,
    phase: str,
    *,
    status: str,
    artifact: Any | None = None,
    value: Any | None = None,
    extra: dict[str, Any] | None = None,
    reset_downstream: bool | Sequence[str] | None = None,
) -> None:
    registry.update_phase(
        target,
        run_id,
        phase,
        status=status,
        artifact=artifact,
        value=value,
        extra=extra,
        reset_downstream=reset_downstream,
    )


def _persist_category(target: Path, run_id: str, category: str | None) -> None:
    if category is None:
        return
    registry.update_research(target, run_id, category=category)
    # Mirror into run.json for legacy readers via the authoritative writer.
    from . import aboyeur

    run_directory = registry.standard_run_dir(target, run_id)
    if (run_directory / "run.json").is_file():
        aboyeur.update_run_receipt(run_directory, category=category)
    legacy_run = registry.legacy_run_dir(target, run_id) / "run.json"
    if legacy_run.is_file():
        # update_research may have created the standard dir for the sidecar;
        # still mirror category onto the legacy receipt when that is the home.
        registry._update_legacy(target, run_id, category=category)
    else:
        registry.update_run(target, run_id, category=category)


def _build_lanes_from_roster(
    target: Path,
    *,
    profile: ResearchProfile,
    synthesizer: str | None,
    reviewer: str | None,
    run_id: str,
    invoker: Any | None = None,
) -> ResearchLanes:
    from .research.llm import PhaseBackend, resolve_lane

    seat_invoker = invoker if invoker is not None else _build_run_invoker(target, run_id=run_id)
    roster = seat_invoker.roster
    synth_candidates = (synthesizer,) if synthesizer else profile.synthesizer
    review_candidates = (reviewer,) if reviewer else profile.reviewer
    planner = resolve_lane(roster, phase="research.plan", candidates=profile.planner)
    extractor = resolve_lane(roster, phase="research.extract", candidates=profile.extractor)
    synthesis = resolve_lane(roster, phase="research.synthesize", candidates=synth_candidates)
    review = resolve_lane(roster, phase="research.review", candidates=review_candidates)
    synthesizers = tuple(
        PhaseBackend(invoker=seat_invoker, seat=seat, phase="research.synthesize")
        for seat in (synthesis.primary, *synthesis.fallbacks)
    )
    if not profile.allow_synthesis_fallback:
        synthesizers = synthesizers[:1]
    return ResearchLanes(
        planner=PhaseBackend(invoker=seat_invoker, seat=planner.primary, phase="research.plan"),
        extractor=PhaseBackend(invoker=seat_invoker, seat=extractor.primary, phase="research.extract"),
        synthesizers=synthesizers,
        reviewer=PhaseBackend(invoker=seat_invoker, seat=review.primary, phase="research.review"),
        browser_discovery=None,
    )


def _resolve_browser_ai_provider(
    target: Path,
    *,
    run_id: str,
    invoker: Any | None = None,
) -> BrowserAiProvider:
    from .research.llm import PhaseBackend, resolve_lane

    seat_invoker = invoker if invoker is not None else _build_run_invoker(target, run_id=run_id)
    lane = resolve_lane(seat_invoker.roster, phase="research.browser-discover")
    backend = PhaseBackend(
        invoker=seat_invoker,
        seat=lane.primary,
        phase="research.browser-discover",
    )
    return BrowserAiProvider(backend=backend, lane=lane.primary)


def _resolve_lanes(
    target: Path,
    *,
    profile: ResearchProfile,
    synthesizer: str | None,
    reviewer: str | None,
    run_id: str,
    invoker: Any | None = None,
) -> ResearchLanes:
    # Research lanes always resolve through the roster/capability resolver.
    return _build_lanes_from_roster(
        target,
        profile=profile,
        synthesizer=synthesizer,
        reviewer=reviewer,
        run_id=run_id,
        invoker=invoker,
    )


def _operator_safe_detail(detail: str) -> str:
    from .worker_failure import safe_detail

    return _sanitize_projection_text(safe_detail(detail))


def _receipt_write_command_failure(
    exc: BaseException, *, run_id: str, phase: str = "admission"
) -> ResearchCommandFailure:
    """Convert a receipt-write lifecycle failure into a typed safe CLI boundary."""
    from . import run_lifecycle, runguard

    if isinstance(exc, runguard.RetainRunLockError):
        kind = "run-lock"
        detail = "research receipt write retained the run lock"
    elif isinstance(exc, run_lifecycle.LifecycleJournalError):
        kind = "lifecycle-journal"
        detail = "research receipt write failed the lifecycle journal gate"
    else:
        kind = "lifecycle-journal"
        detail = "research receipt write failed"
    raw = _operator_safe_detail(str(exc))
    if raw and raw not in detail:
        detail = f"{detail}: {raw}"
    return ResearchCommandFailure(
        run_id=run_id,
        failure_kind=kind,
        failure_phase=phase,
        detail=detail,
        exit_code=1,
    )


def _emit_command_failure(failure: ResearchCommandFailure, *, json_output: bool) -> int:
    payload = _sanitize_projection_value(failure.diagnostic_payload())
    if json_output:
        print(_json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"research command failed: {failure.run_id}")
        print(f"status: {failure.status}")
        print(f"failure_kind: {failure.failure_kind}")
        print(f"detail: {failure.detail}")
    return failure.exit_code


def _backend_seat(backend: Any) -> str:
    seat = getattr(backend, "seat", None)
    if isinstance(seat, str) and seat:
        return seat
    lane = getattr(backend, "lane", None)
    if isinstance(lane, str) and lane:
        return lane
    return str(backend)


def _lane_projection(primary: Any, fallbacks: tuple[Any, ...] = ()) -> dict[str, Any]:
    return {
        "primary": _backend_seat(primary),
        "fallbacks": [_backend_seat(item) for item in fallbacks],
    }


def _resolved_lanes_projection(lanes: ResearchLanes) -> dict[str, Any]:
    synthesizers = lanes.synthesizers
    if not synthesizers:
        raise ResearchRunError("admission", "no-synthesizer", "no synthesis seats configured")
    payload: dict[str, Any] = {
        "planning": _lane_projection(lanes.planner),
        "extraction": _lane_projection(lanes.extractor),
        "synthesis": _lane_projection(synthesizers[0], synthesizers[1:]),
        "review": _lane_projection(lanes.reviewer),
    }
    if lanes.browser_discovery is not None:
        payload["browser_discovery"] = _lane_projection(lanes.browser_discovery)
    return payload


def _persist_resolved_lanes(target: Path, run_id: str, lanes: ResearchLanes) -> None:
    registry.update_research(target, run_id, resolved_lanes=_resolved_lanes_projection(lanes))


def _source_counts(sources: tuple[SourceEnvelope, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for source in sources:
        key = str(source.trust or source.origin)
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _discovery_mode(*, browser_ai_enabled: bool, sources: tuple[SourceEnvelope, ...]) -> str:
    if any(source.trust == "browser-ai" or source.origin == "browser-ai" for source in sources):
        return "browser-ai"
    if not sources:
        return "browser-ai-empty" if browser_ai_enabled else "none"
    # Prefer the first source's trust for a stable single-mode label when mixed.
    return str(sources[0].trust or sources[0].origin)


def _outcome_projection(
    *,
    lanes: ResearchLanes,
    result: ResearchResult,
    browser_ai_enabled: bool,
) -> dict[str, Any]:
    selected = next(
        (backend for backend in lanes.synthesizers if _backend_seat(backend) == result.synthesis_seat),
        None,
    )
    if selected is None and lanes.synthesizers:
        selected = lanes.synthesizers[0]
    synthesis = {
        "seat": result.synthesis_seat,
        "attempt_id": result.synthesis_attempt_id,
        "requested_model": getattr(selected, "requested_model", None) if selected is not None else None,
        "observed_model": (
            str(getattr(selected, "observed_model", "unverified")) if selected is not None else "unverified"
        ),
    }
    fallbacks = [
        {
            "phase": record.phase,
            "from_seat": record.from_seat,
            "to_seat": record.to_seat,
            "failure_kind": record.failure_kind,
            "detail": _operator_safe_detail(record.detail),
        }
        for record in result.fallbacks
    ]
    return {
        "synthesis": synthesis,
        "fallbacks": fallbacks,
        "discovery_mode": _discovery_mode(
            browser_ai_enabled=browser_ai_enabled,
            sources=result.sources,
        ),
        "source_counts": _source_counts(result.sources),
    }


def _persist_outcome_projection(
    target: Path,
    run_id: str,
    *,
    lanes: ResearchLanes,
    result: ResearchResult,
    browser_ai_enabled: bool,
) -> None:
    registry.update_research(
        target,
        run_id,
        **_outcome_projection(
            lanes=lanes,
            result=result,
            browser_ai_enabled=browser_ai_enabled,
        ),
    )


def _resolve_sources(target: Path, corpus: Optional[str], sources: List[str]) -> List[str]:
    cfg = rconfig.load(target)
    paths = list(sources)
    if corpus:
        paths += cfg.corpus_paths(corpus)
    return paths


def _safe_source_adapters(adapters: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    safe: List[Dict[str, Any]] = []
    for item in adapters:
        adapter_type = str(item.get("type") or "").strip().lower()
        if not adapter_type:
            continue
        command = clisrc._command_parts(item.get("command") or item.get("argv"))
        safe_item: Dict[str, Any] = {
            "id": str(item.get("id") or item.get("name") or adapter_type).strip(),
            "type": adapter_type,
            "enabled": item.get("enabled", True) is not False,
            "trust": str(item.get("trust") or ("cli" if adapter_type in clisrc.CLI_SOURCE_TYPES else adapter_type)),
        }
        if command:
            safe_item["command"] = Path(command[0]).name
            safe_item["accepts_query"] = any("{query}" in part for part in command)
        if item.get("cwd"):
            safe_item["cwd"] = str(item.get("cwd"))
        safe.append(safe_item)
    return safe


def _manifest(
    *,
    target: Path,
    cfg: rconfig.ResearchConfig,
    corpus: Optional[str],
    sources: List[str],
    paths: List[str],
    web: bool,
    provider: Optional[str],
    cli_providers: List[Any],
    browser_ai_research: bool = False,
    browser_ai_provider: Any | None = None,
) -> Dict[str, Any]:
    web_provider = str(provider or cfg.search_settings().get("research_search_provider") or "playwright").strip()
    routes = [
        {"id": "local", "type": "local", "enabled": bool(paths), "trust": "local"},
        {"id": "configured-cli", "type": "cli", "enabled": bool(cli_providers), "trust": "cli"},
        {
            "id": web_provider,
            "type": "browser" if web_provider in {"playwright", "browser", ""} else "web",
            "enabled": bool(web),
            "trust": "browser" if web_provider in {"playwright", "browser", ""} else "web",
        },
        {
            "id": "browser-ai",
            "type": "browser-ai",
            "enabled": bool(browser_ai_provider),
            "trust": "browser-ai",
            "origin": "browser-ai",
        },
    ]
    return {
        "target": str(target),
        "corpus": corpus,
        "sources": list(sources),
        "local_paths": list(paths),
        "web_enabled": bool(web),
        "browser_ai_research": bool(browser_ai_research),
        "provider": web_provider,
        "source_adapters": _safe_source_adapters(cfg.source_adapters()),
        "cli_sources": [
            {
                "id": getattr(provider, "source_id", "cli-source"),
                "type": getattr(provider, "source_type", "cli"),
            }
            for provider in cli_providers
        ],
        "routes": routes,
    }


def _profile_allows_discovery(profile: ResearchProfile, kind: str) -> bool:
    """Whether a profile's discovery tuple admits a source route kind."""
    allowed = set(profile.discovery)
    if kind in {"local", "repository"}:
        return bool(allowed & {"local", "repository", "brigade"})
    if kind in {"cli", "indexed-cli"}:
        return bool(allowed & {"brigade", "cli", "indexed-cli"})
    if kind in {"web", "browser", "playwright"}:
        return bool(allowed & {"brigade", "web", "browser", "playwright"})
    if kind == "browser-ai":
        return "browser-ai" in allowed
    return False


def run(
    *,
    target: Path,
    question: str,
    sources: List[str],
    web: bool,
    overrides: Dict[str, Any],
    corpus: Optional[str] = None,
    provider: Optional[str] = None,
    run_id: Optional[str] = None,
    browser_ai_research: bool = False,
    profile: Optional[str] = None,
    synthesizer: Optional[str] = None,
    reviewer: Optional[str] = None,
    category: Optional[str] = None,
    resume: ResumeState | None = None,
) -> str:
    from . import run_lifecycle, runguard
    from .proc import ProcessRegistry

    cfg, research_profile = _load_research_config(target, profile)
    allows_local = _profile_allows_discovery(research_profile, "local")
    allows_cli = _profile_allows_discovery(research_profile, "cli")
    allows_web = _profile_allows_discovery(research_profile, "web")
    allows_browser_ai = _profile_allows_discovery(research_profile, "browser-ai")
    # Explicit CLI flag or browser-ai profile may activate browser-AI discovery when
    # the profile admits brigade or browser-ai routes. local-only never does.
    requested_browser_ai = bool(browser_ai_research) or research_profile.name == "browser-ai"
    browser_ai_enabled = requested_browser_ai and (allows_browser_ai or "brigade" in research_profile.discovery)
    caps_kwargs = {**cfg.caps_overrides(), **{k: v for k, v in overrides.items() if v is not None}}
    caps_kwargs.pop("_resume", None)
    try:
        caps = _build_validated_caps(**caps_kwargs)
    except ValueError as exc:
        raise _invalid_config_failure(detail=str(exc), run_id=run_id or "") from exc
    requested_run_id = run_id
    run_id = run_id or _new_run_id(question)
    if resume is None and requested_run_id is None:
        run_id = _unique_research_run_id(target, question, run_id)
    paths = _resolve_sources(target, corpus, sources) if allows_local else []
    cli_providers = clisrc.build_providers(cfg.source_adapters(), target=target) if allows_cli else []
    blockers: List[str] = []
    try:
        roster = _load_roster(target)
    except (FileNotFoundError, ValueError, OSError) as exc:
        raise _invalid_config_failure(detail=str(exc), run_id=run_id) from exc

    index = localsrc.build_index(paths) if paths else None

    web_provider = None
    if web and allows_web:
        from .research.sources import web as webmod

        try:
            web_provider = webmod.build_provider(provider, cfg.search_settings())
            # surface a missing-browser problem up front, not mid-loop
            if isinstance(web_provider, webmod.PlaywrightProvider) and webmod._import_playwright() is None:
                raise webmod.PlaywrightUnavailable(
                    "Playwright not installed. Run: pip install 'brigade[research]' && playwright install chromium"
                )
        except Exception as e:
            blockers.append(str(e))
            web_provider = None

    seat_invoker = None
    browser_provider = None

    providers: List[Any] = []
    if index is not None and index.num_chunks > 0:
        providers.append(_LocalIndexProvider(index))
    providers.extend(cli_providers)
    if web_provider is not None:
        providers.append(web_provider)

    manifest = _manifest(
        target=target,
        cfg=cfg,
        corpus=corpus,
        sources=sources,
        paths=paths,
        web=web and web_provider is not None,
        provider=provider,
        cli_providers=cli_providers,
        browser_ai_research=browser_ai_enabled,
        browser_ai_provider=True if browser_ai_enabled else None,
    )
    run_directory = registry.standard_run_dir(target, run_id)
    declaration = RunBudgetDeclaration(
        wall_clock_seconds=caps.max_time,
        worker_dispatch_count=caps.max_dispatches,
    )
    cancelled = Event()
    receipt_admission = Event()
    receipt_admission.set()
    process_registry = ProcessRegistry()

    def _close_receipts() -> None:
        if seat_invoker is None:
            receipt_admission.clear()
        else:
            seat_invoker.close_receipts()

    def _fail(status: str, phase: str, kind: str, detail: str) -> str:
        _close_receipts()
        process_registry.cancel()
        _terminate_standard_run(
            target,
            run_id,
            status=status,
            failure_phase=phase,
            failure_kind=kind,
            detail=detail,
            blockers=blockers + [detail],
        )
        return run_id

    try:
        with runguard.run_lock(target, run_dir=run_directory):
            existing = registry.show_run(target, run_id)
            if existing is None or existing.get("legacy"):
                registry.create_standard_run(
                    target,
                    run_id=run_id,
                    question=question,
                    profile=research_profile.name,
                    caps=caps.__dict__.copy(),
                    roster=roster,
                )

            if resume is not None or (existing is not None and existing.get("status") in {"failed", "cancelled"}):
                refusal = _wall_clock_resume_refusal(target, run_id)
                if refusal is not None:
                    raise refusal

            registry.update_research(
                target,
                run_id,
                profile=research_profile.name,
                browser_ai_research=browser_ai_enabled,
                manifest=manifest,
                caps=caps.__dict__.copy(),
            )
            _persist_category(target, run_id, category)

            can_skip_sources = resume is not None and (resume.sources is not None or resume.findings is not None)
            if not providers and not browser_ai_enabled and not can_skip_sources:
                detail = "no local, CLI, web, or browser-AI source route available"
                return _fail("failed", "admission", "no-source-route", detail)

            if resume is not None or (existing is not None and existing.get("status") in {"failed", "cancelled"}):
                try:
                    _reopen_standard_run(target, run_id)
                except ResearchRunError as e:
                    return _fail(
                        "failed",
                        e.failure_phase,
                        e.failure_kind,
                        _operator_safe_detail(e.detail),
                    )

            from . import run_events, run_journal, run_lifecycle
            from .run_budget import declaration_from_persisted_artifact

            append_event = _budget_append_event(run_directory, target)
            # Prefer the persisted declaration from the existing receipt when resuming.
            persisted_budget = None
            started_at = None
            try:
                receipt = _json.loads((run_directory / "run.json").read_text(encoding="utf-8"))
            except (OSError, _json.JSONDecodeError) as exc:
                return _fail(
                    "failed",
                    "admission",
                    "lifecycle-journal",
                    _operator_safe_detail(f"run.json unreadable: {exc}"),
                )
            if not isinstance(receipt, dict):
                return _fail(
                    "failed",
                    "admission",
                    "lifecycle-journal",
                    "run.json is not an object",
                )
            if isinstance(receipt.get("run_budget"), dict):
                persisted_budget = receipt["run_budget"]
            started_at = parse_iso_datetime(receipt.get("started_at"))
            run_lifecycle.prepare_lifecycle_journal(
                run_directory,
                workspace=target,
                incoming_snapshot=receipt,
            )
            if persisted_budget is not None:
                try:
                    declaration = declaration_from_persisted_artifact(persisted_budget)
                except BudgetCompatibilityError as exc:
                    return _fail("failed", "admission", "invalid-config", _operator_safe_detail(exc.diagnostic))
                # Advertise the same enforceable caps BudgetCoordinator will use.
                if declaration.wall_clock_seconds is not None:
                    caps.max_time = int(declaration.wall_clock_seconds)
                if declaration.worker_dispatch_count is not None:
                    caps.max_dispatches = int(declaration.worker_dispatch_count)
                registry.update_research(target, run_id, caps=caps.__dict__.copy())

            coordinator = BudgetCoordinator(declaration=declaration, append_event=append_event)
            if started_at is not None:
                coordinator.set_started_at(started_at)
            journal_path = run_directory / "events" / "lifecycle.jsonl"
            if journal_path.is_file():
                try:
                    report = run_journal.read_journal_bounded(journal_path)
                except (OSError, run_journal.RunJournalError, run_events.CanonicalizationError) as exc:
                    return _fail(
                        "failed",
                        "admission",
                        "lifecycle-journal",
                        _operator_safe_detail(str(exc)),
                    )
                if report.partial_tail is not None or report.chain_errors:
                    return _fail(
                        "failed",
                        "admission",
                        "lifecycle-journal",
                        "lifecycle journal is corrupt or has a partial tail",
                    )
                coordinator.reload(report.events)
            budget_callbacks = ResearchBudgetCallbacks(
                coordinator=coordinator,
                append_event=append_event,
            )
            _persist_budget_projection(
                target,
                run_id,
                declaration=declaration,
                projection=coordinator.projection.to_payload(),
            )

            try:
                seat_invoker = _build_run_invoker(
                    target,
                    run_id=run_id,
                    process_registry=process_registry,
                    budget_callbacks=budget_callbacks,
                    receipt_admission=receipt_admission,
                )
                lanes = _resolve_lanes(
                    target,
                    profile=research_profile,
                    synthesizer=synthesizer,
                    reviewer=reviewer,
                    run_id=run_id,
                    invoker=seat_invoker,
                )
            except Exception as e:
                return _fail("failed", "admission", "invalid-seat", _operator_safe_detail(str(e)))

            _persist_resolved_lanes(target, run_id, lanes)

            if browser_ai_enabled and not can_skip_sources:
                try:
                    browser_provider = _resolve_browser_ai_provider(target, run_id=run_id, invoker=seat_invoker)
                except Exception as e:
                    return _fail(
                        "failed",
                        "discovery",
                        "browser-ai-unavailable",
                        _operator_safe_detail(str(e)),
                    )
                providers.append(browser_provider)
                discovery_backend = getattr(browser_provider, "backend", browser_provider)
                lanes = ResearchLanes(
                    planner=lanes.planner,
                    extractor=lanes.extractor,
                    synthesizers=lanes.synthesizers,
                    reviewer=lanes.reviewer,
                    browser_discovery=discovery_backend,
                )
                _persist_resolved_lanes(target, run_id, lanes)
                manifest = _manifest(
                    target=target,
                    cfg=cfg,
                    corpus=corpus,
                    sources=sources,
                    paths=paths,
                    web=web and web_provider is not None,
                    provider=provider,
                    cli_providers=cli_providers,
                    browser_ai_research=True,
                    browser_ai_provider=browser_provider,
                )
                registry.update_research(target, run_id, manifest=manifest)

            if not providers and not can_skip_sources:
                detail = "no local, CLI, web, or browser-AI source route available"
                return _fail("failed", "admission", "no-source-route", detail)

            def on_phase_started(
                phase: str,
                *,
                reset_downstream: bool | Sequence[str] | None = None,
            ) -> None:
                _phase_graph_update(
                    target,
                    run_id,
                    phase,
                    status="running",
                    reset_downstream=reset_downstream,
                )
                registry.append_event(target, run_id, {"phase": phase, "status": "started"})
                _persist_budget_projection(
                    target,
                    run_id,
                    declaration=declaration,
                    projection=coordinator.projection.to_payload(),
                )

            def on_phase_completed(phase: str, detail: dict[str, Any]) -> None:
                artifact = detail.get("artifact")
                value = detail.get("value")
                _phase_graph_update(
                    target,
                    run_id,
                    phase,
                    status="completed",
                    artifact=artifact,
                    value=value,
                    extra={k: v for k, v in detail.items() if k not in {"artifact", "value"}},
                )
                registry.append_event(target, run_id, {"phase": phase, "status": "completed", **detail})
                _persist_budget_projection(
                    target,
                    run_id,
                    declaration=declaration,
                    projection=coordinator.projection.to_payload(),
                )

            def persist_sources(source_batch: tuple[SourceEnvelope, ...]) -> dict[str, str]:
                return registry.write_sources(target, run_id, source_batch)

            def persist_findings(finding_batch: tuple[Finding, ...]) -> dict[str, str]:
                return registry.write_findings(target, run_id, finding_batch)

            def persist_draft_report(text: str) -> dict[str, str]:
                return registry.write_text_artifact(target, run_id, registry.DRAFT_REPORT_ARTIFACT, text)

            def persist_citation_audit(audit: CitationAudit) -> dict[str, str]:
                return registry.write_citation_audit(target, run_id, audit)

            watcher = CancellationWatcher(
                target=target,
                run_id=run_id,
                cancelled=cancelled,
                process_registry=process_registry,
            )
            watcher.start()
            eng = ResearchEngine(
                lanes=lanes,
                sources=providers,
                caps=caps,
                cancelled=cancelled,
                on_phase_started=on_phase_started,
                on_phase_completed=on_phase_completed,
                persist_sources=persist_sources,
                persist_findings=persist_findings,
                persist_draft_report=persist_draft_report,
                persist_citation_audit=persist_citation_audit,
            )
            try:
                try:
                    result = eng.run(question, resume=resume)
                except BudgetPolicyError as e:
                    phase = eng.active_phase if eng.active_phase else "dispatch"
                    normalized = _normalize_budget_failure(e, phase=phase)
                    _phase_graph_update(
                        target,
                        run_id,
                        normalized.failure_phase,
                        status="failed",
                        extra={"failure_kind": normalized.failure_kind},
                    )
                    return _fail(
                        "failed",
                        normalized.failure_phase,
                        normalized.failure_kind,
                        _operator_safe_detail(normalized.detail),
                    )
                except run_lifecycle.LifecycleJournalError as e:
                    phase = eng.active_phase if eng.active_phase else "run"
                    _phase_graph_update(
                        target,
                        run_id,
                        phase,
                        status="failed",
                        extra={"failure_kind": "lifecycle-journal"},
                    )
                    return _fail(
                        "failed",
                        phase,
                        "lifecycle-journal",
                        _operator_safe_detail(str(e)),
                    )
                except ResearchRunError as e:
                    status = "cancelled" if e.failure_kind == "cancelled" else "failed"
                    kind = "operator-cancelled" if e.failure_kind == "cancelled" else e.failure_kind
                    if e.failure_kind in {"budget_exhausted", "budget-exhausted"}:
                        kind = "budget-exhausted"
                    _phase_graph_update(
                        target,
                        run_id,
                        e.failure_phase,
                        status=status,
                        extra={"failure_kind": kind},
                    )
                    return _fail(status, e.failure_phase, kind, _operator_safe_detail(e.detail))
                except Exception as e:
                    if cancelled.is_set():
                        return _fail(
                            "cancelled",
                            eng.active_phase or "research",
                            "operator-cancelled",
                            "research run cancelled",
                        )
                    return _fail("failed", "research", "worker-failed", _operator_safe_detail(str(e)))

                try:
                    if cancelled.is_set():
                        raise ResearchRunError("publishing", "cancelled", "research run cancelled")
                    _persist_outcome_projection(
                        target,
                        run_id,
                        lanes=lanes,
                        result=result,
                        browser_ai_enabled=browser_ai_enabled,
                    )
                    artifacts = _publish_final_artifacts(
                        target,
                        run_id,
                        question=question,
                        result=result,
                        cancelled=cancelled,
                    )
                    if cancelled.is_set():
                        raise ResearchRunError("publishing", "cancelled", "research run cancelled")
                    _phase_graph_update(
                        target,
                        run_id,
                        "publishing",
                        status="completed",
                        artifact=artifacts,
                    )
                    _complete_standard_run(
                        target,
                        run_id,
                        stats=result.stats,
                        artifacts={
                            "report_html": registry.REPORT_HTML_ARTIFACT,
                            "report_md": registry.REPORT_MD_ARTIFACT,
                            "handoff": registry.HANDOFF_ARTIFACT,
                            "findings": registry.FINDINGS_ARTIFACT,
                            "citation_audit": registry.CITATION_AUDIT_ARTIFACT,
                        },
                        blockers=blockers,
                        budget_projection=coordinator.projection.to_payload(),
                        artifact_refs=artifacts,
                    )
                except ResearchRunError as e:
                    status = "cancelled" if e.failure_kind == "cancelled" else "failed"
                    kind = "operator-cancelled" if e.failure_kind == "cancelled" else e.failure_kind
                    _phase_graph_update(
                        target,
                        run_id,
                        e.failure_phase,
                        status=status,
                        extra={"failure_kind": kind},
                    )
                    return _fail(status, e.failure_phase, kind, _operator_safe_detail(e.detail))
            finally:
                _close_receipts()
                watcher.stop()
                watcher.join(timeout=1)
                process_registry.cancel()
    except ResearchCommandFailure:
        raise
    except ResearchRunError as e:
        # A terminal-write failure must not escape as a traceback.
        raise ResearchCommandFailure(
            run_id=run_id,
            failure_kind=e.failure_kind,
            failure_phase=e.failure_phase,
            detail=_operator_safe_detail(e.detail),
            exit_code=1,
        ) from e
    except (run_lifecycle.LifecycleJournalError, runguard.RetainRunLockError) as e:
        # Receipt-write gates must not terminalize run.json after failing closed,
        # and must not escape as a Python traceback from the CLI boundary.
        raise _receipt_write_command_failure(e, run_id=run_id) from e
    except runguard.RunGuardError as e:
        raise ResearchCommandFailure(
            run_id=run_id,
            failure_kind="run-lock",
            failure_phase="admission",
            detail=_operator_safe_detail(str(e)),
            exit_code=2,
        ) from e
    finally:
        # Terminal exits (success or failure) must stop any still-registered workers.
        _close_receipts()
        process_registry.cancel()

    return run_id


def _resume_state_from_legacy_checkpoint(
    checkpoint: Mapping[str, Any],
    *,
    max_content_chars: int,
) -> ResumeState:
    """Convert a legacy checkpoint into bounded standard resume state."""
    from .research.types import Finding

    urls = [str(item) for item in (checkpoint.get("urls") or []) if item]
    queries = [str(item) for item in (checkpoint.get("queries") or []) if item]
    plan = None
    if queries:
        plan = _json.dumps({"sub_questions": queries, "key_topics": queries, "success_criteria": "cite"})
    sources: list[SourceEnvelope] = []
    for _index, url in enumerate(urls):
        content = registry.bound_text(f"legacy-source:{url}", max_content_chars)
        sources.append(
            SourceEnvelope.build(
                origin="web" if url.startswith("http") else "local",
                provider="legacy-checkpoint",
                uri=url,
                content=content,
                trust="web" if url.startswith("http") else "local",
                acquired_at="1970-01-01T00:00:00+00:00",
            )
        )
    findings_raw = checkpoint.get("findings") or []
    findings: list[Finding] = []
    for item in findings_raw:
        if not isinstance(item, Mapping):
            continue
        source_ids = item.get("source_ids") or item.get("sources") or ()
        if not source_ids and sources:
            source_ids = (sources[min(len(findings), len(sources) - 1)].source_id,)
        if not source_ids:
            continue
        evidence = registry.bound_text(str(item.get("evidence") or item.get("summary") or ""), max_content_chars)
        summary = registry.bound_text(str(item.get("summary") or evidence), max_content_chars)
        raw_provenance = item.get("provenance")
        findings.append(
            Finding(
                source_ids=tuple(str(sid) for sid in source_ids),
                title=str(item.get("title") or "legacy finding"),
                summary=summary,
                evidence=evidence,
                trust=item.get("trust") or ("web" if str(source_ids[0]).startswith("http") else "local"),  # type: ignore[arg-type]
                extraction_lane=str(item.get("extraction_lane") or "legacy"),
                extracted_at=str(item.get("extracted_at") or "1970-01-01T00:00:00+00:00"),
                parent_source_ids=tuple(str(p) for p in (item.get("parent_source_ids") or ())),
                provenance=dict(raw_provenance) if isinstance(raw_provenance, Mapping) else None,
            )
        )
    report = checkpoint.get("report")
    return ResumeState(
        plan=plan,
        sources=tuple(sources) if sources else None,
        findings=tuple(findings) if findings else None,
        report=str(report) if isinstance(report, str) and report else None,
    )


def resume_legacy(*, target: Path, run_id: str, overrides: Dict[str, Any]) -> str:
    rec = registry.show_run(target, run_id)
    if not rec:
        raise SystemExit(f"no such run: {run_id}")
    # Load from the legacy tree before create_standard_run hides it behind run_dir().
    legacy_cp = registry.load_checkpoint(target, run_id)
    if legacy_cp is None:
        legacy_path = registry.legacy_run_dir(target, run_id) / "checkpoint.json"
        if legacy_path.is_file():
            try:
                legacy_cp = _json.loads(legacy_path.read_text(encoding="utf-8"))
            except (OSError, _json.JSONDecodeError):
                legacy_cp = None
    caps_kwargs: Dict[str, Any] = {}
    if isinstance(rec.get("caps"), dict):
        caps_kwargs.update(rec["caps"])
    caps_kwargs.update({k: v for k, v in overrides.items() if v is not None})
    try:
        caps = _build_validated_caps(**{k: v for k, v in caps_kwargs.items() if k != "_resume"})
    except ValueError as exc:
        raise _invalid_config_failure(detail=str(exc), run_id=run_id) from exc
    resume = None
    if isinstance(legacy_cp, dict):
        resume = _resume_state_from_legacy_checkpoint(
            legacy_cp,
            max_content_chars=caps.max_content_chars,
        )
    registry.set_status(target, run_id, "running")
    manifest = _dict_or_empty(rec.get("manifest"))
    restored_overrides: Dict[str, Any] = dict(caps_kwargs)
    restored_overrides.pop("_resume", None)
    result = run(
        target=target,
        question=rec["question"],
        sources=list(manifest.get("sources") or []),
        web=bool(manifest.get("web_enabled")),
        corpus=manifest.get("corpus") if isinstance(manifest.get("corpus"), str) else None,
        provider=manifest.get("provider") if isinstance(manifest.get("provider"), str) else None,
        browser_ai_research=bool(manifest.get("browser_ai_research")),
        profile=rec.get("profile") if isinstance(rec.get("profile"), str) else None,
        category=rec.get("category") if isinstance(rec.get("category"), str) else None,
        overrides=restored_overrides,
        run_id=run_id,
        resume=resume,
    )
    if isinstance(legacy_cp, dict):
        registry.consume_checkpoint(target, run_id)
        # Also drop a leftover legacy-tree checkpoint if create_standard_run raced.
        leftover = registry.legacy_run_dir(target, run_id) / "checkpoint.json"
        if leftover.is_file():
            leftover.unlink()
    return result


def resume(
    *,
    target: Path,
    run_id: str,
    overrides: Dict[str, Any],
    refresh: bool = False,
) -> str:
    from . import runguard

    rec = registry.show_run(target, run_id)
    if not rec:
        raise SystemExit(f"no such run: {run_id}")
    if rec.get("legacy"):
        if refresh:
            raise SystemExit("cannot refresh a legacy research run")
        return resume_legacy(target=target, run_id=run_id, overrides=overrides)

    status = str(rec.get("status") or "")
    if status == "completed":
        raise _admission_failure(run_id=run_id, detail="cannot resume a completed research run")
    run_directory = registry.standard_run_dir(target, run_id)
    if runguard.has_active_run_owner(target, run_directory):
        raise _admission_failure(
            run_id=run_id,
            detail="cannot resume while a live owner holds the run lock",
        )
    resumable = status in {"failed", "cancelled", "running", "started", "error"} or not rec.get("finished_at")
    if not resumable:
        raise _admission_failure(
            run_id=run_id,
            detail=f"cannot resume run in status {status!r}",
        )

    refusal = _wall_clock_resume_refusal(target, run_id)
    if refusal is not None:
        raise refusal

    persisted_caps = _dict_or_empty(rec.get("caps"))
    for key in ("max_time", "max_dispatches"):
        if key in overrides and overrides[key] is not None and key in persisted_caps:
            if overrides[key] != persisted_caps[key]:
                raise SystemExit(f"cannot override {key} on resume")
    try:
        state = resume_state(target, run_id)
    except (OSError, ValueError) as exc:
        raise _invalid_sidecar_failure(
            run_id=run_id,
            detail=f"research sidecar is missing or invalid: {exc}",
        ) from exc
    if refresh:
        # Escape hatch: keep at most the durable plan; repeat discovery+.
        state = ResumeState(plan=state.plan)
    manifest = _dict_or_empty(rec.get("manifest"))
    restored_overrides: Dict[str, Any] = dict(persisted_caps)
    restored_overrides.update({k: v for k, v in overrides.items() if v is not None})
    # Enforce original wall-clock / dispatch caps even if a caller passed equals.
    if "max_time" in persisted_caps:
        restored_overrides["max_time"] = persisted_caps["max_time"]
    if "max_dispatches" in persisted_caps:
        restored_overrides["max_dispatches"] = persisted_caps["max_dispatches"]
    return run(
        target=target,
        question=str(rec.get("question") or ""),
        sources=list(manifest.get("sources") or []),
        web=bool(manifest.get("web_enabled")),
        corpus=manifest.get("corpus") if isinstance(manifest.get("corpus"), str) else None,
        provider=manifest.get("provider") if isinstance(manifest.get("provider"), str) else None,
        browser_ai_research=bool(manifest.get("browser_ai_research")),
        profile=rec.get("profile") if isinstance(rec.get("profile"), str) else None,
        category=rec.get("category") if isinstance(rec.get("category"), str) else None,
        overrides=restored_overrides,
        run_id=run_id,
        resume=state,
    )


def cancel(*, target: Path, run_id: str) -> None:
    rec = registry.show_run(target, run_id)
    if not rec:
        raise SystemExit(f"no such run: {run_id}")
    if rec.get("legacy"):
        registry.set_status(target, run_id, "cancelled")
        return
    registry.request_cancel(target, run_id, requested_by="operator")


def _fingerprint_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fingerprint_json(value: Any) -> str:
    return hashlib.sha256(_json.dumps(value, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _safe_filename(value: str) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", value.lower()).strip("-")
    return slug[:96] or "research-handoff"


def _resolve_handoff_destination(
    target: Path,
    *,
    inbox: str | None = None,
    handoff_inbox: Path | None = None,
) -> tuple[Path | None, str | None, list[str]]:
    blockers: list[str] = []
    if inbox and handoff_inbox is not None:
        blockers.append("choose either --inbox or --handoff-inbox, not both")
        return None, None, blockers
    if inbox:
        inbox_key = inbox.strip().lower()
        inbox_rel = WRITER_INBOXES.get(inbox_key)
        if inbox_rel is None:
            blockers.append(f"unsupported writer inbox: {inbox}")
            return None, inbox_key, blockers
        return target / inbox_rel, inbox_key, blockers
    if handoff_inbox is not None:
        path = handoff_inbox.expanduser()
        if not path.is_absolute():
            path = target / path
        return path, "custom", blockers
    blockers.append("missing export destination; pass --inbox or --handoff-inbox")
    return None, None, blockers


def _run_handoff_path(target: Path, rec: dict[str, Any]) -> Path | None:
    run_id = str(rec.get("run_id") or "")
    rel = rec.get("artifacts", {}).get("handoff") if isinstance(rec.get("artifacts"), dict) else None
    if not run_id or not isinstance(rel, str) or not rel:
        return None
    return registry.run_dir(target, run_id) / rel


def _export_source_payload(target: Path, rec: dict[str, Any], handoff_text: str) -> dict[str, Any]:
    return {
        "run_id": rec.get("run_id"),
        "status": rec.get("status"),
        "question": rec.get("question"),
        "caps": _dict_or_empty(rec.get("caps")),
        "manifest": _dict_or_empty(rec.get("manifest")),
        "stats": _dict_or_empty(rec.get("stats")),
        "handoff_artifact_fingerprint": _fingerprint_text(handoff_text),
    }


def _format_manifest_evidence(rec: dict[str, Any], source_fingerprint: str) -> list[str]:
    manifest = _dict_or_empty(rec.get("manifest"))
    caps = _dict_or_empty(rec.get("caps"))
    routes = _list_or_empty(manifest.get("routes"))
    route_labels = []
    for route in routes:
        if not isinstance(route, dict):
            continue
        route_labels.append(
            f"{route.get('id') or route.get('type')}:{route.get('trust')} enabled={bool(route.get('enabled'))}"
        )
    return [
        f"- research_run_id: {rec.get('run_id')}",
        f"- research_status: {rec.get('status')}",
        f"- research_source_fingerprint: {source_fingerprint}",
        f"- research_corpus: {manifest.get('corpus') or ''}",
        f"- research_web_enabled: {bool(manifest.get('web_enabled'))}",
        f"- research_provider: {manifest.get('provider') or ''}",
        f"- research_routes: {', '.join(route_labels) if route_labels else 'none'}",
        f"- research_caps: {_json.dumps(caps, sort_keys=True)}",
    ]


def _augment_handoff_for_export(handoff_text: str, rec: dict[str, Any], source_fingerprint: str) -> str:
    marker = "## Recommended memory action"
    evidence = _format_manifest_evidence(rec, source_fingerprint)
    if marker not in handoff_text:
        return handoff_text.rstrip() + "\n\n## Evidence\n\n" + "\n".join(evidence) + "\n"
    before, after = handoff_text.split(marker, 1)
    if "## Evidence" in before:
        before = before.rstrip() + "\n" + "\n".join(evidence) + "\n\n"
    else:
        before = before.rstrip() + "\n\n## Evidence\n\n" + "\n".join(evidence) + "\n\n"
    return before + marker + after


def export_handoff(
    *,
    target: Path,
    run_id: str,
    inbox: str | None = None,
    handoff_inbox: Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    target = target.expanduser().resolve()
    rec = registry.show_run(target, run_id)
    blockers: list[str] = []
    if rec is None:
        return {"status": "blocked", "run_id": run_id, "blockers": [f"research run not found: {run_id}"]}
    if rec.get("status") not in {"done", "completed"}:
        blockers.append(f"research run is not complete: {rec.get('status')}")
    artifact_path = _run_handoff_path(target, rec)
    if artifact_path is None:
        blockers.append("research run has no handoff artifact")
        handoff_text = ""
    elif not artifact_path.exists():
        blockers.append(f"handoff artifact missing: {artifact_path}")
        handoff_text = ""
    else:
        handoff_text = artifact_path.read_text(encoding="utf-8", errors="replace")
    destination, inbox_label, destination_blockers = _resolve_handoff_destination(
        target, inbox=inbox, handoff_inbox=handoff_inbox
    )
    blockers.extend(destination_blockers)
    if destination is not None and not destination.exists():
        blockers.append(f"handoff inbox missing: {destination}")
    if destination is not None and destination.exists() and not destination.is_dir():
        blockers.append(f"handoff inbox is not a directory: {destination}")
    if blockers:
        return {
            "status": "blocked",
            "run_id": run_id,
            "destination": str(destination) if destination else None,
            "inbox": inbox_label,
            "blockers": blockers,
        }

    assert destination is not None
    source_payload = _export_source_payload(target, rec, handoff_text)
    source_fingerprint = _fingerprint_json(source_payload)
    export_text = _augment_handoff_for_export(handoff_text, rec, source_fingerprint)
    filename = f"{_safe_filename(run_id)}-research-handoff.md"
    out_path = destination / filename
    if out_path.exists() and out_path.read_text(encoding="utf-8", errors="replace") != export_text and not force:
        return {
            "status": "blocked",
            "run_id": run_id,
            "destination": str(destination),
            "path": str(out_path),
            "inbox": inbox_label,
            "blockers": ["export path already exists with different content; leaving it unchanged"],
        }
    out_path.write_text(export_text, encoding="utf-8")

    from . import handoff_cmd

    lint_result = handoff_cmd.lint_file(out_path)
    if not lint_result.valid:
        try:
            out_path.unlink()
        except OSError:
            pass
        return {
            "status": "blocked",
            "run_id": run_id,
            "destination": str(destination),
            "path": str(out_path),
            "inbox": inbox_label,
            "blockers": list(lint_result.errors),
        }

    export = {
        "export_id": f"{_safe_filename(run_id)}-{inbox_label or 'custom'}",
        "run_id": run_id,
        "created_at": _now(),
        "inbox": inbox_label,
        "destination": str(destination),
        "path": str(out_path),
        "artifact_path": str(artifact_path),
        "source_fingerprint": source_fingerprint,
        "manifest_fingerprint": _fingerprint_json(rec.get("manifest") if isinstance(rec.get("manifest"), dict) else {}),
        "handoff_artifact_fingerprint": source_payload["handoff_artifact_fingerprint"],
        "lint": lint_result.as_dict(),
        "status": "exported",
    }
    exports = [item for item in rec.get("handoff_exports", []) if isinstance(item, dict)]
    replaced = False
    for index, item in enumerate(exports):
        if item.get("path") == str(out_path):
            exports[index] = export
            replaced = True
            break
    if not replaced:
        exports.append(export)
    registry.update_run(target, run_id, handoff_exports=exports)
    return export


def handoff_status_payload(*, target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    runs = registry.list_runs(target)
    items: list[dict[str, Any]] = []
    for rec in runs:
        run_id = str(rec.get("run_id") or "")
        if rec.get("status") not in {"done", "completed"}:
            continue
        artifact_path = _run_handoff_path(target, rec)
        if artifact_path is None:
            status = "missing-artifact"
            fingerprint = None
        elif not artifact_path.exists():
            status = "missing-artifact"
            fingerprint = None
        else:
            text = artifact_path.read_text(encoding="utf-8", errors="replace")
            fingerprint = _fingerprint_json(_export_source_payload(target, rec, text))
            exports = [item for item in rec.get("handoff_exports", []) if isinstance(item, dict)]
            if not exports:
                status = "missing-export"
            elif any(not Path(str(item.get("path") or "")).exists() for item in exports):
                status = "missing-export-path"
            elif any(item.get("source_fingerprint") != fingerprint for item in exports):
                status = "stale-export"
            else:
                status = "exported"
        exports = [item for item in rec.get("handoff_exports", []) if isinstance(item, dict)]
        items.append(
            {
                "run_id": run_id,
                "question": rec.get("question"),
                "status": status,
                "export_count": len(exports),
                "exports": exports,
                "source_fingerprint": fingerprint,
                "suggested_next_command": (
                    f"brigade research export-handoff {run_id} --inbox codex"
                    if status in {"missing-export", "stale-export", "missing-export-path"}
                    else None
                ),
            }
        )
    issue_items = [item for item in items if item.get("status") != "exported"]
    return {
        "target": str(target),
        "run_count": len(items),
        "issue_count": len(issue_items),
        "top_issue": issue_items[0] if issue_items else None,
        "runs": items,
    }


def health(target: Path) -> dict[str, Any]:
    return handoff_status_payload(target=target)


def _handoff_issue_record(item: dict[str, Any]) -> dict[str, Any]:
    run_id = str(item.get("run_id") or "unknown")
    status = str(item.get("status") or "unknown")
    question = str(item.get("question") or "research run")
    command = str(item.get("suggested_next_command") or f"brigade research show {run_id}")
    fingerprint = str(item.get("source_fingerprint") or _fingerprint_json(item))
    detail_by_status = {
        "missing-export": "completed research run has not been exported into a writer handoff inbox",
        "missing-export-path": "research handoff export path is missing",
        "stale-export": "research handoff export is stale compared with the run artifact",
        "missing-artifact": "completed research run is missing its handoff artifact",
    }
    detail = detail_by_status.get(status, "research handoff export needs review")
    return {
        "text": f"Research handoff export issue for {run_id}: {detail}",
        "kind": "research",
        "source": "research-handoff",
        "type": "docs",
        "priority": "normal",
        "acceptance": [
            f"Review research run {run_id} and confirm whether its handoff should be exported.",
            f"Run `{command}` or document why the research handoff should remain unexported.",
            "Confirm `brigade research handoffs doctor` no longer reports this issue or the issue is intentionally dismissed.",
        ],
        "metadata": {
            "research_run_id": run_id,
            "research_question": question,
            "research_handoff_status": status,
            "source_item_key": f"research-handoff:{run_id}:{status}",
            "source_fingerprint": fingerprint,
            "suggested_next_command": command,
            "export_count": item.get("export_count"),
        },
    }


def _handoff_issue_records(target: Path) -> list[dict[str, Any]]:
    payload = handoff_status_payload(target=target)
    records: list[dict[str, Any]] = []
    for item in payload.get("runs", []):
        if isinstance(item, dict) and item.get("status") != "exported":
            records.append(_handoff_issue_record(item))
    return records


def cli_handoffs_doctor(*, target: Path, json_output: bool = False) -> int:
    payload = handoff_status_payload(target=target)
    if json_output:
        print(_json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["issue_count"] == 0 else 1
    print(f"research handoffs doctor: {target}")
    print(f"runs: {payload['run_count']}")
    print(f"issues: {payload['issue_count']}")
    for item in payload.get("runs", []):
        if not isinstance(item, dict):
            continue
        status = item.get("status")
        marker = "ok" if status == "exported" else "warn"
        print(f"[{marker}] {item.get('run_id')}: {status} exports={item.get('export_count')}")
        if item.get("suggested_next_command"):
            print(f"  command: {item.get('suggested_next_command')}")
    return 0 if payload["issue_count"] == 0 else 1


def cli_handoffs_import_issues(*, target: Path, dry_run: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    records = _handoff_issue_records(target)
    from . import work_cmd

    imported, skipped, skipped_dismissed, _rejected = work_cmd._append_import_records(target, records, dry_run=dry_run)
    payload = {
        "target": str(target),
        "source": "research-handoff",
        "dry_run": dry_run,
        "candidate_count": len(records),
        "imported": len(imported),
        "skipped": len(skipped),
        "dismissed": len(skipped_dismissed),
        "imports": imported,
    }
    if json_output:
        print(_json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"research handoff issues: {target}")
    print(f"candidates: {len(records)}")
    print(f"imported: {len(imported)}")
    print(f"skipped: {len(skipped)}")
    print(f"dismissed: {len(skipped_dismissed)}")
    if records and not imported:
        print("status: no new imports")
    return 0


def cli_provenance_backfill(*, target: Path, json_output: bool = False) -> int:
    from .research.provenance import backfill_research_provenance

    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = backfill_research_provenance(target)
    if json_output:
        print(_json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"research provenance backfill: {target}")
    print(f"scanned_findings: {payload['scanned_findings']}")
    print(f"stamped: {payload['stamped']}")
    print(f"unchanged: {payload['unchanged']}")
    print(f"missing_envelope: {payload['missing_envelope']}")
    print(f"inferred: {payload['inferred']}")
    print("trusted: 0")
    return 0


def _new_run_id(question: str) -> str:
    # Caller passes run_id in tests for determinism; production stamps the time.
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + registry.slug(question)


def sources_payload(*, target: Path) -> Dict[str, Any]:
    cfg = rconfig.load(target)
    adapters = cfg.source_adapters()
    settings = cfg.search_settings()
    routes: List[Dict[str, Any]] = [
        {
            "id": "local",
            "type": "local",
            "status": "ok",
            "detail": "local corpus paths and --source globs are available",
            "trust": "local",
        }
    ]

    web_provider = str(settings.get("research_search_provider") or "playwright").strip()
    browser_ok = False
    try:
        from .research.sources import web as webmod

        browser_ok = webmod._import_playwright() is not None
    except Exception:
        browser_ok = False
    routes.append(
        {
            "id": "playwright",
            "type": "browser",
            "status": "ok" if browser_ok else "warn",
            "detail": "available for --web browser search"
            if browser_ok
            else "install brigade[research] and chromium for --web browser search",
            "trust": "browser",
        }
    )
    if web_provider == "searxng" or settings.get("searxng_url"):
        routes.append(
            {
                "id": "searxng",
                "type": "web",
                "status": "ok" if settings.get("searxng_url") else "fail",
                "detail": "configured web search endpoint"
                if settings.get("searxng_url")
                else "missing search.searxng_url",
                "trust": "web",
            }
        )
    if web_provider == "pageforge" or settings.get("pageforge_command"):
        command = clisrc._command_parts(settings.get("pageforge_command"))
        executable = command[0] if command else ""
        executable_path = Path(executable).expanduser()
        if "/" in executable and not executable_path.is_absolute():
            executable_path = target / executable_path
        exists = bool(executable) and (
            executable_path.exists() if "/" in executable else shutil.which(executable) is not None
        )
        if not command:
            status = "fail"
            detail = "missing search.pageforge_command"
        elif exists:
            status = "ok"
            detail = "configured PageForge web search with local cache"
        else:
            status = "fail"
            detail = "configured PageForge executable not found"
        routes.append(
            {
                "id": "pageforge",
                "type": "web",
                "status": status,
                "detail": detail,
                "trust": "web",
            }
        )

    # _safe_source_adapters drops typeless entries, so pair against the same filter.
    typed_adapters = [entry for entry in adapters if str(entry.get("type") or "").strip()]
    for raw, item in zip(typed_adapters, _safe_source_adapters(adapters), strict=True):
        if item.get("type") not in clisrc.CLI_SOURCE_TYPES:
            routes.append({**item, "status": "warn", "detail": "unsupported research source adapter type"})
            continue
        if item.get("enabled") is False:
            routes.append({**item, "status": "warn", "detail": "configured but disabled"})
            continue
        command = clisrc._command_parts(raw.get("command") or raw.get("argv"))
        executable = command[0] if command else ""
        executable_path = Path(executable).expanduser()
        if "/" in executable and not executable_path.is_absolute():
            executable_path = target / executable_path
        exists = bool(executable) and (
            executable_path.exists() if "/" in executable else shutil.which(executable) is not None
        )
        detail = "configured CLI source ready" if exists else "configured CLI executable not found"
        if item.get("type") == "antigravity" and not command:
            detail = "missing Antigravity CLI command; configure command or argv for agy"
        routes.append(
            {
                **item,
                "status": "ok" if exists else "fail",
                "detail": detail,
            }
        )

    statuses = [route["status"] for route in routes]
    return {
        "target": str(target),
        "status": "fail" if "fail" in statuses else ("warn" if "warn" in statuses else "ok"),
        "routes": routes,
    }


# --- CLI presentation helpers (return process exit codes) ---

_ADMISSION_FAILURE_KINDS = frozenset({"invalid-seat", "run-lock", "no-source-route"})
_RESEARCH_INIT_TEMPLATE = (
    '[research]\ndefault_profile = "grounded"\n\n[caps]\nmax_rounds = 6\nmax_time = 300\nmax_dispatches = 24\n'
)
_BROWSER_PROFILE_KEY_RE = re.compile(r"(?i)browser[_-]?profile")
_ABS_HOME_PATH_RE = re.compile(r"/(?:home|Users)/[^\s\"']+")
_CITATION_TOKEN_RE = re.compile(r"^\[source:src-[a-f0-9]{16}\]$")
_SECRET_KEY_RE = re.compile(
    r"(?i)("
    r"headers?|(?:^|[_-])env(?:$|[_-])|cookie|authorization|password|secret|api[_-]?key|"
    r"access[_-]?token|auth[_-]?token|api[_-]?token|refresh[_-]?token|id[_-]?token|"
    r"openai_api_key|browser[_-]?profile"
    r")"
)
_BARE_TOKEN_KEY_RE = re.compile(r"(?i)^token$")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_COOKIE_ASSIGN_RE = re.compile(r"(?i)\bcookie\s*[=:]\s*[^\s,;]+")
_URI_LIKE_RE = re.compile(r"(?i)^[a-z][a-z0-9+.-]*://")
_SECRET_QUERY_PARAM_RE = re.compile(
    r"(?i)^("
    r"api[_-]?key|access[_-]?token|auth[_-]?token|api[_-]?token|"
    r"refresh[_-]?token|id[_-]?token|token|key|secret|sig|signature|"
    r"password|authorization|cookie|openai_api_key"
    r")$"
)


def _pinned_research_exit_code(rec: Mapping[str, Any]) -> int:
    status = rec.get("status")
    if status == "cancelled":
        return 130
    if status in {"failed", "error"}:
        kind = rec.get("failure_kind")
        if kind in _ADMISSION_FAILURE_KINDS:
            return 2
        return 1
    return 0


def _is_projection_secret_key(key: object) -> bool:
    text = str(key)
    if _BARE_TOKEN_KEY_RE.fullmatch(text):
        return True
    return bool(_SECRET_KEY_RE.search(text))


def _sanitize_projection_text(value: str) -> str:
    text = _sanitize_projection_uri(value) if _URI_LIKE_RE.match(value.strip()) else value
    text = _ABS_HOME_PATH_RE.sub("[redacted]", text)
    text = _BEARER_RE.sub("[redacted]", text)
    text = _COOKIE_ASSIGN_RE.sub("cookie=[redacted]", text)
    return text


def _is_secret_query_param(name: str) -> bool:
    return bool(_SECRET_QUERY_PARAM_RE.fullmatch(name))


def _netloc_without_userinfo(parsed: Any) -> str:
    try:
        host = parsed.hostname
    except ValueError:
        host = None
    if host is None:
        netloc = parsed.netloc
        return netloc.rsplit("@", 1)[-1] if "@" in netloc else netloc
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parsed.port is not None:
        return f"{host}:{parsed.port}"
    return host


def _redact_secret_query(query: str) -> str:
    if not query:
        return query
    parts: list[str] = []
    for name, value in parse_qsl(query, keep_blank_values=True):
        encoded_name = quote(name, safe="")
        encoded_value = "[redacted]" if _is_secret_query_param(name) else quote(value, safe="")
        parts.append(f"{encoded_name}={encoded_value}")
    return "&".join(parts)


def _sanitize_projection_uri(value: str) -> str:
    """Strip URL userinfo and redact secret-bearing query parameter values."""
    text = value.strip()
    if not _URI_LIKE_RE.match(text):
        return value
    try:
        parsed = urlparse(text)
    except ValueError:
        return value
    if not parsed.scheme:
        return value
    return urlunparse(
        (
            parsed.scheme,
            _netloc_without_userinfo(parsed),
            parsed.path,
            parsed.params,
            _redact_secret_query(parsed.query),
            parsed.fragment,
        )
    )


def _sanitize_projection_value(value: object, *, key: str | None = None) -> object:
    if key is not None and _BROWSER_PROFILE_KEY_RE.search(key):
        return None
    if key is not None and _BARE_TOKEN_KEY_RE.fullmatch(key):
        if isinstance(value, str) and _CITATION_TOKEN_RE.fullmatch(value):
            return value
        return "[redacted]"
    if key is not None and _is_projection_secret_key(key):
        return "[redacted]"
    if isinstance(value, Mapping):
        cleaned: dict[str, object] = {}
        for item_key, item_value in value.items():
            key_text = str(item_key)
            if _BROWSER_PROFILE_KEY_RE.search(key_text):
                continue
            if _BARE_TOKEN_KEY_RE.fullmatch(key_text):
                if isinstance(item_value, str) and _CITATION_TOKEN_RE.fullmatch(item_value):
                    cleaned[key_text] = item_value
                else:
                    cleaned[key_text] = "[redacted]"
            elif _is_projection_secret_key(key_text):
                cleaned[key_text] = "[redacted]"
            else:
                cleaned[key_text] = _sanitize_projection_value(item_value, key=key_text)
        return cleaned
    if isinstance(value, list):
        return [_sanitize_projection_value(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_projection_value(item) for item in value]
    if isinstance(value, str):
        return _sanitize_projection_text(value)
    return value


def _status_run_projection(rec: Mapping[str, Any]) -> dict[str, object]:
    projected = dict(rec)
    projected.pop("browser_profile", None)
    return _sanitize_projection_value(projected)  # type: ignore[return-value]


def _status_payload(*, target: Path, runs: Sequence[Mapping[str, Any]]) -> dict[str, object]:
    return {
        "schema": "brigade.research.status.v1",
        "schema_version": 1,
        "runs": [_status_run_projection(run) for run in runs],
    }


def _load_json_artifact(target: Path, run_id: str, name: str) -> dict[str, Any] | None:
    path = registry.standard_run_dir(target, run_id) / name
    if not path.is_file():
        return None
    try:
        payload = _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, _json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


_SHOW_SOURCE_EXCERPT_MAX_CHARS = 640
_SHOW_REPORT_MAX_CHARS = 20000


def _show_artifact_refs(rec: Mapping[str, Any]) -> dict[str, Any]:
    """Merge legacy ``artifacts`` and digest-bearing ``artifact_refs`` maps."""
    refs: dict[str, Any] = {}
    artifacts = rec.get("artifacts")
    if isinstance(artifacts, Mapping):
        refs.update({str(name): value for name, value in artifacts.items()})
    artifact_refs = rec.get("artifact_refs")
    if isinstance(artifact_refs, Mapping):
        refs.update({str(name): value for name, value in artifact_refs.items()})
    return refs


def _source_record_projection(item: object) -> dict[str, object]:
    from dataclasses import asdict, is_dataclass

    if is_dataclass(item) and not isinstance(item, type):
        item = asdict(item)
    if not isinstance(item, Mapping):
        return {"malformed": True}
    content = item.get("content")
    content_text = content if isinstance(content, str) else ""
    excerpt = registry.bound_text(content_text, _SHOW_SOURCE_EXCERPT_MAX_CHARS)
    raw_uri = item.get("uri")
    record: dict[str, object] = {
        "source_id": item.get("source_id"),
        "origin": item.get("origin"),
        "provider": item.get("provider"),
        "uri": _sanitize_projection_uri(raw_uri) if isinstance(raw_uri, str) else raw_uri,
        "trust": item.get("trust"),
        "acquired_at": item.get("acquired_at"),
        "producing_lane": item.get("producing_lane"),
        "requested_model": item.get("requested_model"),
        "observed_model": item.get("observed_model"),
        "content_digest": item.get("content_digest"),
        "content_chars": len(content_text),
        "excerpt": excerpt,
        "excerpt_truncated": len(content_text) > len(excerpt),
    }
    return _sanitize_projection_value(record)  # type: ignore[return-value]


def _discovery_sources_ref(research: Mapping[str, Any] | None) -> Any | None:
    """Return the discovery-phase sources artifact ref, if one was recorded."""
    phases = (research or {}).get("phases") if isinstance(research, Mapping) else None
    if not isinstance(phases, Mapping):
        return None
    discovery = phases.get("discovery")
    if not isinstance(discovery, Mapping):
        return None
    candidate = discovery.get("artifact")
    return candidate if isinstance(candidate, Mapping) else None


def _show_sources_projection(
    target: Path,
    run_id: str,
    rec: Mapping[str, Any],
    research: Mapping[str, Any] | None,
) -> tuple[list[dict[str, object]] | None, str]:
    """Return (sanitized source records, verification state) for show.v2.

    Verification states: ``verified`` (digest-checked ref), ``unverified``
    (sources artifact readable but no matching digest ref), ``missing`` (no
    persisted sources), ``unavailable`` (legacy run), ``digest-mismatch``
    (a digest-bearing ref exists and the file on disk does not match).
    Content is withheld on every state except ``verified`` and ``unverified``.
    """
    if rec.get("legacy"):
        return None, "unavailable"
    ref = _discovery_sources_ref(research)
    if ref is not None:
        state = registry.artifact_verification_state(target, run_id, ref)
        if state == "verified":
            loaded = registry.read_verified_artifact(target, run_id, ref)
            if isinstance(loaded, tuple):
                return [_source_record_projection(item) for item in loaded], "verified"
            return None, "missing"
        if state == "digest-mismatch":
            return None, "digest-mismatch"
        if state == "missing":
            return None, "missing"
    payload = _load_json_artifact(target, run_id, registry.SOURCES_ARTIFACT)
    if payload is None:
        return None, "missing"
    raw_sources = payload.get("sources")
    if not isinstance(raw_sources, list):
        return None, "missing"
    return [_source_record_projection(item) for item in raw_sources], "unverified"


def _show_report_projection(target: Path, run_id: str, rec: Mapping[str, Any]) -> dict[str, object]:
    """Bounded, digest-verified report body for show.v2.

    Content is only included when the recorded digest matches the file on
    disk; every other verification state ships metadata alone.
    """
    report: dict[str, object] = {
        "artifact": registry.REPORT_MD_ARTIFACT,
        "digest": None,
        "verification": "unavailable",
        "content": None,
        "content_chars": 0,
        "truncated": False,
    }
    if rec.get("legacy"):
        return report
    ref = _show_artifact_refs(rec).get("report_md")
    if isinstance(ref, Mapping) and isinstance(ref.get("digest"), str):
        report["digest"] = ref.get("digest")
    report["verification"] = registry.artifact_verification_state(target, run_id, ref)
    if report["verification"] != "verified":
        return report
    text = registry.read_verified_artifact(target, run_id, ref)
    if not isinstance(text, str):
        report["verification"] = "missing"
        return report
    bounded = registry.bound_text(text, _SHOW_REPORT_MAX_CHARS)
    report["content"] = _sanitize_projection_text(bounded)
    report["content_chars"] = len(text)
    report["truncated"] = len(text) > len(bounded)
    return report


def _show_artifact_verification(
    target: Path,
    run_id: str,
    rec: Mapping[str, Any],
    research: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    if rec.get("legacy"):
        return {}
    refs = _show_artifact_refs(rec)
    sources_ref = _discovery_sources_ref(research)
    if sources_ref is not None and "sources" not in refs:
        refs["sources"] = sources_ref
    return {name: registry.artifact_verification_state(target, run_id, ref) for name, ref in sorted(refs.items())}


def _show_payload(*, target: Path, run_id: str, rec: Mapping[str, Any]) -> dict[str, object]:
    from dataclasses import asdict, is_dataclass

    research: dict[str, Any] | None = None
    findings: object | None = None
    citation_audit: object | None = None
    if not rec.get("legacy"):
        try:
            research = registry.read_research(target, run_id)
        except FileNotFoundError:
            research = None
        findings = _load_json_artifact(target, run_id, registry.FINDINGS_ARTIFACT)
        citation_audit = _load_json_artifact(target, run_id, registry.CITATION_AUDIT_ARTIFACT)
        artifacts = rec.get("artifacts") if isinstance(rec.get("artifacts"), Mapping) else {}
        artifact_refs = rec.get("artifact_refs") if isinstance(rec.get("artifact_refs"), Mapping) else {}
        if findings is None:
            findings_ref = None
            if isinstance(artifact_refs, Mapping) and "findings" in artifact_refs:
                findings_ref = artifact_refs.get("findings")
            elif isinstance(artifacts, Mapping) and isinstance(artifacts.get("findings"), dict):
                findings_ref = artifacts.get("findings")
            if findings_ref is not None:
                loaded = registry.read_verified_artifact(target, run_id, findings_ref)
                if loaded is not None:
                    findings = {"findings": list(loaded)} if isinstance(loaded, tuple) else loaded
        if citation_audit is None:
            audit_ref = None
            if isinstance(artifact_refs, Mapping) and "citation_audit" in artifact_refs:
                audit_ref = artifact_refs.get("citation_audit")
            elif isinstance(artifacts, Mapping) and isinstance(artifacts.get("citation_audit"), dict):
                audit_ref = artifacts.get("citation_audit")
            if audit_ref is not None:
                loaded = registry.read_verified_artifact(target, run_id, audit_ref)
                if is_dataclass(loaded) and not isinstance(loaded, type):
                    citation_audit = asdict(loaded)
                elif loaded is not None:
                    citation_audit = loaded
    sources, sources_verification = _show_sources_projection(target, run_id, rec, research)
    return {
        "schema": "brigade.research.show.v2",
        "schema_version": 2,
        "run": _sanitize_projection_value(dict(rec)),
        "research": _sanitize_projection_value(research) if research is not None else None,
        "findings": _sanitize_projection_value(findings) if findings is not None else None,
        "citation_audit": _sanitize_projection_value(citation_audit) if citation_audit is not None else None,
        "sources": sources,
        "sources_verification": sources_verification,
        "report": _show_report_projection(target, run_id, rec),
        "artifact_verification": _show_artifact_verification(target, run_id, rec, research),
    }


def cli_init(*, target: Path, profile: Optional[str] = None, json_output: bool = False) -> int:
    from .research.types import BUILTIN_PROFILES

    target = target.expanduser().resolve()
    path = target / ".brigade" / "research.toml"
    selected = "grounded"
    if profile is not None:
        if profile not in BUILTIN_PROFILES:
            print(f"error: unknown research profile: {profile}", file=sys.stderr)
            return 2
        selected = profile
    body = _RESEARCH_INIT_TEMPLATE.replace('default_profile = "grounded"', f'default_profile = "{selected}"', 1)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(body)
    except FileExistsError:
        print(f"error: research config already exists: {path}", file=sys.stderr)
        return 2
    payload = {"path": str(path), "default_profile": selected, "created": True}
    if json_output:
        print(_json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"wrote research config: {path}")
    return 0


def cli_doctor(*, target: Path, profile: Optional[str] = None, json_output: bool = False) -> int:
    from .research.doctor import doctor_payload

    try:
        payload = doctor_payload(target, profile_name=profile)
    except (ValueError, FileNotFoundError, OSError) as exc:
        return _emit_command_failure(_invalid_config_failure(detail=str(exc)), json_output=json_output)
    if json_output:
        print(_json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"research doctor: {target}")
        print(f"status: {payload.get('status')}")
        print(f"profile: {payload.get('profile')}")
        for lane in _list_or_empty(payload.get("lanes")):
            if not isinstance(lane, Mapping):
                continue
            print(
                f"- [{lane.get('status')}] {lane.get('capability')} "
                f"seat={lane.get('seat')} auth={lane.get('auth_status')} - {lane.get('detail')}"
            )
    status = payload.get("status")
    return 1 if status == "fail" else 0


def cli_run(
    *,
    target: Path,
    question: str,
    corpus: Optional[str],
    sources: List[str],
    web: bool,
    overrides: Dict[str, Any],
    provider: Optional[str] = None,
    browser_ai_research: bool = False,
    profile: Optional[str] = None,
    synthesizer: Optional[str] = None,
    reviewer: Optional[str] = None,
    category: Optional[str] = None,
    json_output: bool = False,
) -> int:
    try:
        rid = run(
            target=target,
            question=question,
            corpus=corpus,
            sources=sources,
            web=web,
            overrides=overrides,
            provider=provider,
            browser_ai_research=browser_ai_research,
            profile=profile,
            synthesizer=synthesizer,
            reviewer=reviewer,
            category=category,
        )
    except ResearchCommandFailure as failure:
        return _emit_command_failure(failure, json_output=json_output)
    rec = registry.show_run(target, rid) or {"run_id": rid}
    if json_output:
        print(_json.dumps(_sanitize_projection_value(rec), indent=2, sort_keys=True))
    else:
        print(f"research run: {rid}")
        print(f"status: {rec.get('status')}")
        for b in rec.get("blockers", []):
            print(f"blocker: {b}")
    return _pinned_research_exit_code(rec)


def cli_status(
    *,
    target: Path,
    run_id: Optional[str] = None,
    all_runs: bool = False,
    json_output: bool = False,
) -> int:
    if all_runs or run_id is None:
        runs = registry.list_runs(target)
        payload = _status_payload(target=target, runs=runs)
        if json_output:
            print(_json.dumps(payload, indent=2, sort_keys=True))
            return 0
        print(f"research runs: {target}")
        for r in _list_or_empty(payload.get("runs")):
            if not isinstance(r, Mapping):
                continue
            legacy = " legacy" if r.get("legacy") else ""
            print(f"- {r.get('run_id')} [{r.get('status')}] {r.get('question')}{legacy}")
        return 0

    try:
        run_id = _require_cli_run_id(run_id)
    except SystemExit as exc:
        return _emit_cli_run_id_error(exc, json_output=json_output)

    rec = registry.show_run(target, run_id)
    if rec is None:
        return _emit_missing_run_error(run_id, json_output=json_output)
    payload = _status_payload(target=target, runs=[rec])
    if json_output:
        print(_json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"run: {rec.get('run_id')}")
    print(f"status: {rec.get('status')}")
    print(f"question: {rec.get('question')}")
    print(f"legacy: {bool(rec.get('legacy'))}")
    return 0


def cli_list(*, target: Path, json_output: bool = False) -> int:
    # Compatibility alias for `research status --all` for one release window.
    return cli_status(target=target, run_id=None, all_runs=True, json_output=json_output)


def cli_show(*, target: Path, run_id: str, json_output: bool = False) -> int:
    try:
        run_id = _require_cli_run_id(run_id)
    except SystemExit as exc:
        return _emit_cli_run_id_error(exc, json_output=json_output)
    rec = registry.show_run(target, run_id)
    if rec is None:
        return _emit_missing_run_error(run_id, json_output=json_output)
    if json_output:
        print(_json.dumps(_show_payload(target=target, run_id=run_id, rec=rec), indent=2, sort_keys=True))
        return 0
    print(f"run: {rec.get('run_id')}")
    print(f"status: {rec.get('status')}")
    print(f"question: {rec.get('question')}")
    artifacts = rec.get("artifacts", {})
    if isinstance(artifacts, Mapping):
        for name, rel in artifacts.items():
            if isinstance(rel, Mapping):
                rel_path = rel.get("path")
                if isinstance(rel_path, str):
                    print(f"{name}: {registry.run_dir(target, run_id) / rel_path}")
            elif isinstance(rel, str):
                print(f"{name}: {registry.run_dir(target, run_id) / rel}")
    handoff_state = next(
        (
            item
            for item in handoff_status_payload(target=target).get("runs", [])
            if isinstance(item, dict) and item.get("run_id") == run_id
        ),
        None,
    )
    if handoff_state:
        print(f"handoff_export_status: {handoff_state.get('status')}")
        print(f"handoff_export_count: {handoff_state.get('export_count')}")
        if handoff_state.get("suggested_next_command"):
            print(f"handoff_export_command: {handoff_state.get('suggested_next_command')}")
    for b in rec.get("blockers", []):
        print(f"blocker: {b}")
    return 0


def cli_export_handoff(
    *,
    target: Path,
    run_id: str,
    inbox: str | None,
    handoff_inbox: Path | None,
    force: bool = False,
    json_output: bool = False,
) -> int:
    try:
        run_id = _require_cli_run_id(run_id)
    except SystemExit as exc:
        return _emit_cli_run_id_error(exc, json_output=json_output)
    payload = export_handoff(
        target=target,
        run_id=run_id,
        inbox=inbox,
        handoff_inbox=handoff_inbox,
        force=force,
    )
    if json_output:
        print(_json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload.get("status") == "exported" else 1
    if payload.get("status") != "exported":
        print(f"research handoff export blocked: {run_id}")
        for blocker in payload.get("blockers", []):
            print(f"blocker: {blocker}")
        return 1
    print(f"research handoff exported: {run_id}")
    print(f"inbox: {payload.get('inbox')}")
    print(f"path: {payload.get('path')}")
    print(f"source_fingerprint: {payload.get('source_fingerprint')}")
    return 0


def cli_cancel(*, target: Path, run_id: str, json_output: bool = False) -> int:
    try:
        run_id = _require_cli_run_id(run_id)
    except SystemExit as exc:
        return _emit_cli_run_id_error(exc, json_output=json_output)
    if registry.show_run(target, run_id) is None:
        return _emit_missing_run_error(run_id, json_output=json_output)
    try:
        cancel(target=target, run_id=run_id)
    except (OSError, ValueError) as exc:
        return _emit_command_failure(
            _invalid_sidecar_failure(run_id=run_id, detail=f"research sidecar is missing or invalid: {exc}"),
            json_output=json_output,
        )
    rec = registry.show_run(target, run_id) or {"run_id": run_id, "status": "cancelled"}
    if json_output:
        print(_json.dumps({"run_id": run_id, "status": rec.get("status", "cancelled")}, indent=2, sort_keys=True))
        return 0
    print(f"cancelled: {run_id}")
    return 0


def cli_resume(
    *,
    target: Path,
    run_id: str,
    overrides: Dict[str, Any],
    json_output: bool = False,
    refresh: bool = False,
) -> int:
    try:
        run_id = _require_cli_run_id(run_id)
        resume(target=target, run_id=run_id, overrides=overrides, refresh=refresh)
    except ResearchCommandFailure as failure:
        return _emit_command_failure(failure, json_output=json_output)
    except SystemExit as e:
        message = str(e)
        if "invalid run_id" in message.lower():
            return _emit_cli_run_id_error(e, json_output=json_output)
        if message.startswith("no such run:"):
            return _emit_missing_run_error(run_id, json_output=json_output)
        # Typed admission path owns non-resumable statuses; leftover SystemExit stays exit 2.
        if json_output:
            print(
                _json.dumps(
                    {
                        "run_id": run_id,
                        "status": "failed",
                        "failure_kind": "admission",
                        "failure_phase": "admission",
                        "detail": message,
                        "blockers": [message],
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print(message)
        return 2
    rec = registry.show_run(target, run_id) or {"run_id": run_id}
    if json_output:
        print(_json.dumps(_sanitize_projection_value(rec), indent=2, sort_keys=True))
    else:
        print(f"resumed: {run_id}")
        print(f"status: {rec.get('status')}")
    return _pinned_research_exit_code(rec)


def cli_open(*, target: Path, run_id: str, json_output: bool = False) -> int:
    try:
        run_id = _require_cli_run_id(run_id)
    except SystemExit as exc:
        return _emit_cli_run_id_error(exc, json_output=json_output)
    rec = registry.show_run(target, run_id)
    if rec is None:
        return _emit_missing_run_error(run_id, json_output=json_output)
    rel = rec.get("artifacts", {}).get("report_html")
    if not rel:
        message = f"no report for run: {run_id}"
        if json_output:
            print(
                _json.dumps(
                    {
                        "run_id": run_id,
                        "status": "failed",
                        "failure_kind": "missing-artifact",
                        "failure_phase": "admission",
                        "detail": message,
                        "error": message,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print(message)
        return 1
    path = registry.run_dir(target, run_id) / rel
    if json_output:
        print(_json.dumps({"run_id": run_id, "report_html": str(path)}, indent=2, sort_keys=True))
        return 0
    print(str(path))
    return 0


def cli_sources_list(*, target: Path, json_output: bool = False) -> int:
    payload = sources_payload(target=target)
    if json_output:
        print(_json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"research sources: {target}")
    for route in payload["routes"]:
        print(
            f"- [{route.get('status')}] {route.get('type')}:{route.get('id')} ({route.get('trust')}) - {route.get('detail')}"
        )
    return 0


def cli_sources_doctor(*, target: Path, json_output: bool = False) -> int:
    payload = sources_payload(target=target)
    if json_output:
        print(_json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"research sources doctor: {target}")
        print(f"status: {payload['status']}")
        for route in payload["routes"]:
            print(f"- [{route.get('status')}] {route.get('type')}:{route.get('id')} - {route.get('detail')}")
    return 1 if payload["status"] == "fail" else 0
