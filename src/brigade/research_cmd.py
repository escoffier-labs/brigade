# src/brigade/research_cmd.py
from __future__ import annotations
import hashlib
import json as _json
import shutil
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from .research import registry, config as rconfig
from .research.types import Caps, ResearchLanes, ResearchProfile, ResearchRunError
from .research.engine import ResearchEngine
from .research.sources import local as localsrc
from .research.sources import cli as clisrc
from .research.sources.browser_ai import BrowserAiProvider
from .research import report as reportmod, handoff as handoffmod
from .selection import WRITER_INBOXES
from .localio import utc_now_iso as _now


def _dict_or_empty(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list_or_empty(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _resolve_backend(target: Path):
    from . import roster as roster_mod
    from .research import llm

    r = roster_mod.load_roster(roster_mod.resolve_roster_path(target))
    return llm.resolve_backend(r)


class _CompatBackend:
    def __init__(self, inner: Any, *, seat: str, phase: str) -> None:
        self._inner = inner
        self.seat = seat
        self.phase = phase
        self.requested_model = getattr(inner, "requested_model", None)
        self.observed_model = getattr(inner, "observed_model", None) or "unverified"
        self.last_attempt_id: str | None = None

    def complete(self, messages, **kwargs) -> str:
        text = self._inner.complete(messages, **kwargs)
        self.observed_model = getattr(self._inner, "observed_model", None) or self.observed_model
        self.last_attempt_id = getattr(self._inner, "last_attempt_id", None) or f"{self.seat}:{self.phase}:compat"
        return text


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


def _lanes_from_single_backend(
    backend: Any,
    *,
    synthesizer: str | None = None,
    reviewer: str | None = None,
    browser_discovery: Any | None = None,
) -> ResearchLanes:
    synth_seat = synthesizer or "researcher"
    review_seat = reviewer or "researcher"
    return ResearchLanes(
        planner=_CompatBackend(backend, seat="researcher", phase="planning"),
        extractor=_CompatBackend(backend, seat="researcher", phase="extraction"),
        synthesizers=(_CompatBackend(backend, seat=synth_seat, phase="synthesis"),),
        reviewer=_CompatBackend(backend, seat=review_seat, phase="review"),
        browser_discovery=browser_discovery,
    )


def _build_run_invoker(target: Path, *, run_id: str):
    from . import roster as roster_mod
    from .proc import ProcessRegistry
    from .run_seat import SeatInvoker

    roster = roster_mod.load_roster(roster_mod.resolve_roster_path(target))
    output_dir = registry.run_dir(target, run_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    return SeatInvoker(
        roster=roster,
        cwd=target,
        run_id=run_id,
        process_registry=ProcessRegistry(),
        output_dir=output_dir,
    )


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
    # New runs always build PhaseBackend lanes from the roster/capability resolver.
    # HttpBackend/_CompatBackend remain available for the legacy-resume path only.
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

    return safe_detail(detail)


def _persist_category(target: Path, run_id: str, category: str | None) -> None:
    if category is None:
        return
    from . import localio, receipt_schema

    path = registry.run_dir(target, run_id) / registry.RESEARCH_ARTIFACT
    path.parent.mkdir(parents=True, exist_ok=True)
    # Initialize only when absent; fail closed if present but unreadable/non-object.
    if path.is_file():
        current = localio.read_json_dict(path)
        if current is None:
            raise ValueError(f"research artifact unreadable or not an object: {path}")
    else:
        current = {}
    current["run_id"] = run_id
    current["category"] = category
    localio.write_json(path, receipt_schema.stamp_research_sidecar(current))
    # Mirror into run.json for legacy readers.
    registry.update_run(target, run_id, category=category)


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
) -> str:
    cfg = rconfig.load(target)
    research_profile = cfg.profile(profile)
    # Only the explicit CLI flag or the built-in browser-ai profile may activate
    # browser-AI discovery. Repo overlays on other profiles cannot.
    browser_ai_enabled = bool(browser_ai_research) or research_profile.name == "browser-ai"
    caps_kwargs = {**cfg.caps_overrides(), **{k: v for k, v in overrides.items() if v is not None}}
    caps = Caps.build(**caps_kwargs)
    run_id = run_id or _new_run_id(question)
    paths = _resolve_sources(target, corpus, sources)
    cli_providers = clisrc.build_providers(cfg.source_adapters(), target=target)
    blockers: List[str] = []

    index = localsrc.build_index(paths) if paths else None

    web_provider = None
    if web:
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
    registry.create_run(
        target,
        question=question,
        run_id=run_id,
        caps=caps.__dict__.copy(),
        manifest=manifest,
    )
    registry.update_run(target, run_id, profile=research_profile.name, browser_ai_research=browser_ai_enabled)
    _persist_category(target, run_id, category)

    if not providers and not browser_ai_enabled:
        detail = "no local, CLI, web, or browser-AI source route available"
        registry.finish_run(
            target,
            run_id,
            status="error",
            stats={},
            artifacts={},
            blockers=blockers + [detail],
        )
        registry.update_run(
            target,
            run_id,
            failure_kind="no-source-route",
            failure_phase="admission",
            detail=detail,
        )
        return run_id

    try:
        seat_invoker = _build_run_invoker(target, run_id=run_id)
        lanes = _resolve_lanes(
            target,
            profile=research_profile,
            synthesizer=synthesizer,
            reviewer=reviewer,
            run_id=run_id,
            invoker=seat_invoker,
        )
    except Exception as e:
        detail = _operator_safe_detail(str(e))
        registry.finish_run(
            target,
            run_id,
            status="error",
            stats={},
            artifacts={},
            blockers=blockers + [detail],
        )
        registry.update_run(
            target,
            run_id,
            failure_kind="invalid-seat",
            failure_phase="admission",
            detail=detail,
        )
        return run_id

    if browser_ai_enabled:
        try:
            browser_provider = _resolve_browser_ai_provider(
                target, run_id=run_id, invoker=seat_invoker
            )
        except Exception as e:
            detail = _operator_safe_detail(str(e))
            registry.finish_run(
                target,
                run_id,
                status="error",
                stats={},
                artifacts={},
                blockers=blockers + [detail],
            )
            registry.update_run(
                target,
                run_id,
                failure_kind="browser-ai-unavailable",
                failure_phase="discovery",
                detail=detail,
            )
            return run_id
        providers.append(browser_provider)
        # Refresh manifest enabled state now that the provider exists.
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
        registry.update_run(target, run_id, manifest=manifest)

    if not providers:
        detail = "no local, CLI, web, or browser-AI source route available"
        registry.finish_run(
            target,
            run_id,
            status="error",
            stats={},
            artifacts={},
            blockers=blockers + [detail],
        )
        registry.update_run(
            target,
            run_id,
            failure_kind="no-source-route",
            failure_phase="admission",
            detail=detail,
        )
        return run_id

    eng = ResearchEngine(
        lanes=lanes,
        sources=providers,
        caps=caps,
        on_phase_started=lambda phase: registry.append_event(target, run_id, {"phase": phase, "status": "started"}),
        on_phase_completed=lambda phase, d: registry.append_event(
            target, run_id, {"phase": phase, "status": "completed", **d}
        ),
    )
    try:
        result = eng.run(question)
    except ResearchRunError as e:
        detail = _operator_safe_detail(e.detail)
        registry.finish_run(
            target,
            run_id,
            status="error",
            stats={},
            artifacts={},
            blockers=blockers + [detail],
        )
        registry.update_run(
            target,
            run_id,
            failure_kind=e.failure_kind,
            failure_phase=e.failure_phase,
            detail=detail,
        )
        return run_id
    except Exception as e:
        detail = _operator_safe_detail(str(e))
        registry.finish_run(
            target,
            run_id,
            status="error",
            stats={},
            artifacts={},
            blockers=blockers + [detail],
        )
        registry.update_run(
            target,
            run_id,
            failure_kind="worker-failed",
            failure_phase="research",
            detail=detail,
        )
        return run_id

    findings = list(result.findings)
    sources_list = list(result.sources)
    d = registry.run_dir(target, run_id)
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
    (d / "report.md").write_text(md, encoding="utf-8")
    (d / "report.html").write_text(html, encoding="utf-8")
    (d / "handoff.md").write_text(ho, encoding="utf-8")
    registry.finish_run(
        target,
        run_id,
        status="done",
        stats=result.stats,
        artifacts={"report_html": "report.html", "report_md": "report.md", "handoff": "handoff.md"},
        blockers=blockers,
    )
    if category is not None:
        _persist_category(target, run_id, category)
    return run_id


def resume(*, target: Path, run_id: str, overrides: Dict[str, Any]) -> str:
    rec = registry.show_run(target, run_id)
    if not rec:
        raise SystemExit(f"no such run: {run_id}")
    registry.set_status(target, run_id, "running")
    manifest = _dict_or_empty(rec.get("manifest"))
    restored_overrides: Dict[str, Any] = {}
    if isinstance(rec.get("caps"), dict):
        restored_overrides.update(rec["caps"])
    restored_overrides.update({k: v for k, v in overrides.items() if v is not None})
    restored_overrides["_resume"] = True
    return run(
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
    )


def cancel(*, target: Path, run_id: str) -> None:
    registry.set_status(target, run_id, "cancelled")


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
    if rec.get("status") != "done":
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
        if rec.get("status") != "done":
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
    rec = registry.show_run(target, rid) or {"run_id": rid}
    if json_output:
        print(_json.dumps(rec, indent=2, sort_keys=True))
    else:
        print(f"research run: {rid}")
        print(f"status: {rec.get('status')}")
        for b in rec.get("blockers", []):
            print(f"blocker: {b}")
    if rec.get("status") == "error":
        return 2 if rec.get("failure_kind") == "no-source-route" else 1
    return 0


def cli_list(*, target: Path, json_output: bool = False) -> int:
    runs = registry.list_runs(target)
    if json_output:
        print(_json.dumps(runs, indent=2, sort_keys=True))
        return 0
    print(f"research runs: {target}")
    status_by_run = {
        item.get("run_id"): item
        for item in handoff_status_payload(target=target).get("runs", [])
        if isinstance(item, dict)
    }
    for r in runs:
        handoff_state = status_by_run.get(r.get("run_id"))
        suffix = f" handoff={handoff_state.get('status')}" if isinstance(handoff_state, dict) else ""
        print(f"- {r.get('run_id')} [{r.get('status')}] {r.get('question')}{suffix}")
    return 0


def cli_show(*, target: Path, run_id: str, json_output: bool = False) -> int:
    rec = registry.show_run(target, run_id)
    if rec is None:
        print(f"no such run: {run_id}")
        return 1
    if json_output:
        print(_json.dumps(rec, indent=2, sort_keys=True))
        return 0
    print(f"run: {rec.get('run_id')}")
    print(f"status: {rec.get('status')}")
    print(f"question: {rec.get('question')}")
    artifacts = rec.get("artifacts", {})
    for name, rel in artifacts.items():
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
    if registry.show_run(target, run_id) is None:
        print(f"no such run: {run_id}")
        return 1
    cancel(target=target, run_id=run_id)
    if json_output:
        print(_json.dumps({"run_id": run_id, "status": "cancelled"}, indent=2, sort_keys=True))
        return 0
    print(f"cancelled: {run_id}")
    return 0


def cli_resume(*, target: Path, run_id: str, overrides: Dict[str, Any], json_output: bool = False) -> int:
    try:
        resume(target=target, run_id=run_id, overrides=overrides)
    except SystemExit as e:
        print(str(e))
        return 1
    rec = registry.show_run(target, run_id) or {"run_id": run_id}
    if json_output:
        print(_json.dumps(rec, indent=2, sort_keys=True))
        return 0
    print(f"resumed: {run_id}")
    print(f"status: {rec.get('status')}")
    return 0


def cli_open(*, target: Path, run_id: str, json_output: bool = False) -> int:
    rec = registry.show_run(target, run_id)
    if rec is None:
        print(f"no such run: {run_id}")
        return 1
    rel = rec.get("artifacts", {}).get("report_html")
    if not rel:
        print(f"no report for run: {run_id}")
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
