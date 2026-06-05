# src/brigade/research_cmd.py
from __future__ import annotations
import json as _json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional
from .research import registry, config as rconfig
from .research.types import Caps
from .research.engine import DeepResearcher
from .research.sources import local as localsrc
from .research.sources import cli as clisrc
from .research import report as reportmod, handoff as handoffmod

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
            "trust": str(item.get("trust") or ("cli" if adapter_type == "cli" else adapter_type)),
        }
        if command:
            safe_item["command"] = Path(command[0]).name
            safe_item["accepts_query"] = any("{query}" in part for part in command)
        if item.get("cwd"):
            safe_item["cwd"] = str(item.get("cwd"))
        safe.append(safe_item)
    return safe

def _manifest(*, target: Path, cfg: rconfig.ResearchConfig, corpus: Optional[str],
              sources: List[str], paths: List[str], web: bool, provider: Optional[str],
              cli_providers: List[Any]) -> Dict[str, Any]:
    web_provider = str(provider or cfg.search_settings().get("research_search_provider") or "playwright").strip()
    routes = [
        {"id": "local", "type": "local", "enabled": bool(paths), "trust": "local"},
        {"id": "configured-cli", "type": "cli", "enabled": bool(cli_providers), "trust": "cli"},
        {"id": web_provider, "type": "browser" if web_provider in {"playwright", "browser", ""} else "web",
         "enabled": bool(web), "trust": "browser" if web_provider in {"playwright", "browser", ""} else "web"},
    ]
    return {
        "target": str(target),
        "corpus": corpus,
        "sources": list(sources),
        "local_paths": list(paths),
        "web_enabled": bool(web),
        "provider": web_provider,
        "source_adapters": _safe_source_adapters(cfg.source_adapters()),
        "cli_sources": [getattr(provider, "source_id", "cli-source") for provider in cli_providers],
        "routes": routes,
    }

def run(*, target: Path, question: str, sources: List[str], web: bool,
        overrides: Dict[str, Any], corpus: Optional[str] = None,
        provider: Optional[str] = None, run_id: Optional[str] = None) -> str:
    cfg = rconfig.load(target)
    caps_kwargs = {**cfg.caps_overrides(), **{k: v for k, v in overrides.items() if v is not None}}
    caps = Caps.build(**caps_kwargs)
    run_id = run_id or _new_run_id(question)
    paths = _resolve_sources(target, corpus, sources)
    cli_providers = clisrc.build_providers(cfg.source_adapters(), target=target)
    manifest = _manifest(target=target, cfg=cfg, corpus=corpus, sources=sources,
                         paths=paths, web=web, provider=provider,
                         cli_providers=cli_providers)
    registry.create_run(target, question=question, run_id=run_id,
                        caps=caps.__dict__.copy(), manifest=manifest)
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
                    "Playwright not installed. Run: pip install 'brigade[research]' "
                    "&& playwright install chromium")
        except Exception as e:
            blockers.append(str(e))
            web_provider = None

    try:
        backend = _resolve_backend(target)
    except Exception as e:
        registry.finish_run(target, run_id, status="error", stats={},
                            artifacts={}, blockers=blockers + [str(e)])
        return run_id

    eng = DeepResearcher(
        llm=backend, local_index=index, web=web_provider, caps=caps,
        external_sources=cli_providers,
        on_checkpoint=lambda cp: registry.save_checkpoint(target, run_id, cp),
        on_event=lambda phase, d: registry.append_event(target, run_id, {"phase": phase, **d}),
    )
    prior = registry.load_checkpoint(target, run_id) if overrides.get("_resume") else None
    try:
        result = eng.research(question, prior=prior)
    except Exception as e:
        registry.finish_run(target, run_id, status="error", stats={}, artifacts={},
                            blockers=blockers + [str(e)])
        return run_id

    d = registry.run_dir(target, run_id)
    md = reportmod.render_markdown(question=question, markdown_report=result.report,
                                   findings=result.findings)
    html = reportmod.render_html(question=question, markdown_report=result.report,
                                 findings=result.findings, stats=result.stats)
    ho = handoffmod.render_handoff(question=question, markdown_report=result.report,
                                   findings=result.findings, stats=result.stats)
    (d / "report.md").write_text(md)
    (d / "report.html").write_text(html)
    (d / "handoff.md").write_text(ho)
    registry.finish_run(target, run_id, status="done", stats=result.stats,
                        artifacts={"report_html": "report.html", "report_md": "report.md",
                                   "handoff": "handoff.md"}, blockers=blockers)
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
    routes.append({
        "id": "playwright",
        "type": "browser",
        "status": "ok" if browser_ok else "warn",
        "detail": "available for --web browser search" if browser_ok else "install brigade[research] and chromium for --web browser search",
        "trust": "browser",
    })
    if web_provider == "searxng" or settings.get("searxng_url"):
        routes.append({
            "id": "searxng",
            "type": "web",
            "status": "ok" if settings.get("searxng_url") else "fail",
            "detail": "configured web search endpoint" if settings.get("searxng_url") else "missing search.searxng_url",
            "trust": "web",
        })

    for raw, item in zip(adapters, _safe_source_adapters(adapters)):
        if item.get("type") != "cli":
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
        exists = bool(executable) and (executable_path.exists() if "/" in executable else shutil.which(executable) is not None)
        routes.append({
            **item,
            "status": "ok" if exists else "fail",
            "detail": "configured CLI source ready" if exists else "configured CLI executable not found",
        })

    statuses = [route["status"] for route in routes]
    return {
        "target": str(target),
        "status": "fail" if "fail" in statuses else ("warn" if "warn" in statuses else "ok"),
        "routes": routes,
    }


# --- CLI presentation helpers (return process exit codes) ---

def cli_run(*, target: Path, question: str, corpus: Optional[str], sources: List[str],
            web: bool, overrides: Dict[str, Any], provider: Optional[str] = None,
            json_output: bool = False) -> int:
    rid = run(target=target, question=question, corpus=corpus, sources=sources,
              web=web, overrides=overrides, provider=provider)
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
    for r in runs:
        print(f"- {r.get('run_id')} [{r.get('status')}] {r.get('question')}")
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
    for b in rec.get("blockers", []):
        print(f"blocker: {b}")
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


def cli_resume(*, target: Path, run_id: str, overrides: Dict[str, Any],
               json_output: bool = False) -> int:
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
        print(f"- [{route.get('status')}] {route.get('type')}:{route.get('id')} ({route.get('trust')}) - {route.get('detail')}")
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
