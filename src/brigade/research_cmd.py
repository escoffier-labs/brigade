# src/brigade/research_cmd.py
from __future__ import annotations
import hashlib
import json as _json
import shutil
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from .research import registry, config as rconfig
from .research.types import Caps
from .research.engine import DeepResearcher
from .research.sources import local as localsrc
from .research.sources import cli as clisrc
from .research import report as reportmod, handoff as handoffmod
from .selection import WRITER_INBOXES
from .localio import utc_now_iso as _now


def _resolve_backend(target: Path):
    from . import roster as roster_mod
    from .research import llm

    r = roster_mod.load_roster(target / ".brigade" / "roster.toml")
    return llm.resolve_backend(r)


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
    ]
    return {
        "target": str(target),
        "corpus": corpus,
        "sources": list(sources),
        "local_paths": list(paths),
        "web_enabled": bool(web),
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
) -> str:
    cfg = rconfig.load(target)
    caps_kwargs = {**cfg.caps_overrides(), **{k: v for k, v in overrides.items() if v is not None}}
    caps = Caps.build(**caps_kwargs)
    run_id = run_id or _new_run_id(question)
    paths = _resolve_sources(target, corpus, sources)
    cli_providers = clisrc.build_providers(cfg.source_adapters(), target=target)
    manifest = _manifest(
        target=target,
        cfg=cfg,
        corpus=corpus,
        sources=sources,
        paths=paths,
        web=web,
        provider=provider,
        cli_providers=cli_providers,
    )
    registry.create_run(target, question=question, run_id=run_id, caps=caps.__dict__.copy(), manifest=manifest)
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

    try:
        backend = _resolve_backend(target)
    except Exception as e:
        registry.finish_run(target, run_id, status="error", stats={}, artifacts={}, blockers=blockers + [str(e)])
        return run_id

    eng = DeepResearcher(
        llm=backend,
        local_index=index,
        web=web_provider,
        caps=caps,
        external_sources=cli_providers,
        on_checkpoint=lambda cp: registry.save_checkpoint(target, run_id, cp),
        on_event=lambda phase, d: registry.append_event(target, run_id, {"phase": phase, **d}),
    )
    prior = registry.load_checkpoint(target, run_id) if overrides.get("_resume") else None
    try:
        result = eng.research(question, prior=prior)
    except Exception as e:
        registry.finish_run(target, run_id, status="error", stats={}, artifacts={}, blockers=blockers + [str(e)])
        return run_id

    d = registry.run_dir(target, run_id)
    md = reportmod.render_markdown(question=question, markdown_report=result.report, findings=result.findings)
    html = reportmod.render_html(
        question=question, markdown_report=result.report, findings=result.findings, stats=result.stats
    )
    ho = handoffmod.render_handoff(
        question=question, markdown_report=result.report, findings=result.findings, stats=result.stats
    )
    (d / "report.md").write_text(md)
    (d / "report.html").write_text(html)
    (d / "handoff.md").write_text(ho)
    registry.finish_run(
        target,
        run_id,
        status="done",
        stats=result.stats,
        artifacts={"report_html": "report.html", "report_md": "report.md", "handoff": "handoff.md"},
        blockers=blockers,
    )
    return run_id


def resume(*, target: Path, run_id: str, overrides: Dict[str, Any]) -> str:
    rec = registry.show_run(target, run_id)
    if not rec:
        raise SystemExit(f"no such run: {run_id}")
    registry.set_status(target, run_id, "running")
    manifest = rec.get("manifest") if isinstance(rec.get("manifest"), dict) else {}
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
        "caps": rec.get("caps") if isinstance(rec.get("caps"), dict) else {},
        "manifest": rec.get("manifest") if isinstance(rec.get("manifest"), dict) else {},
        "stats": rec.get("stats") if isinstance(rec.get("stats"), dict) else {},
        "handoff_artifact_fingerprint": _fingerprint_text(handoff_text),
    }


def _format_manifest_evidence(rec: dict[str, Any], source_fingerprint: str) -> list[str]:
    manifest = rec.get("manifest") if isinstance(rec.get("manifest"), dict) else {}
    caps = rec.get("caps") if isinstance(rec.get("caps"), dict) else {}
    routes = manifest.get("routes") if isinstance(manifest.get("routes"), list) else []
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
        handoff_text = artifact_path.read_text(errors="replace")
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
    if out_path.exists() and out_path.read_text(errors="replace") != export_text and not force:
        return {
            "status": "blocked",
            "run_id": run_id,
            "destination": str(destination),
            "path": str(out_path),
            "inbox": inbox_label,
            "blockers": ["export path already exists with different content; pass --force to replace"],
        }
    out_path.write_text(export_text)

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
            text = artifact_path.read_text(errors="replace")
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

    imported, skipped, skipped_dismissed = work_cmd._append_import_records(target, records, dry_run=dry_run)
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
    )
    rec = registry.show_run(target, rid) or {"run_id": rid}
    if json_output:
        print(_json.dumps(rec, indent=2, sort_keys=True))
        return 0
    print(f"research run: {rid}")
    print(f"status: {rec.get('status')}")
    for b in rec.get("blockers", []):
        print(f"blocker: {b}")
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
