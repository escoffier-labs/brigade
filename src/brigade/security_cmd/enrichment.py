"""Read-only security scanner for agent workspaces."""
# ruff: noqa: E402,F401,F403,F811,F821

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlparse

from .. import work_cmd
from ..selection import WRITER_INBOXES
from ..untrusted import PROMPT_INJECTION_RE, scan_untrusted
from .. import localio
from ..localio import read_json_dict as _read_json, utc_now_iso_z as _utc_iso, write_json as _write_json

from . import models as _family_base

globals().update({name: value for name, value in vars(_family_base).items() if not name.startswith("__")})


def _load_enrichment_payload(output_dir: Path) -> dict[str, Any] | None:
    path = output_dir.expanduser().resolve() / "security-enrichment.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {"ready": False, "path": str(path), "reason": "invalid JSON"}
    if not isinstance(data, dict):
        return {"ready": False, "path": str(path), "reason": "security-enrichment.json must contain an object"}
    return data


def _indicator_source(finding: dict[str, Any]) -> dict[str, Any]:
    return {
        "fingerprint": finding.get("fingerprint"),
        "title": finding.get("title"),
        "path": finding.get("path"),
        "line": finding.get("line"),
        "category": finding.get("category"),
    }


def _add_indicator(
    indicators: list[dict[str, Any]],
    seen: set[tuple[str, str]],
    *,
    kind: str,
    value: str,
    finding: dict[str, Any],
) -> None:
    value = value.strip().strip(".,);]")
    if not value:
        return
    key = (kind, value.lower())
    if key in seen:
        for indicator in indicators:
            if indicator["type"] == kind and indicator["value"].lower() == value.lower():
                indicator["sources"].append(_indicator_source(finding))
                return
    seen.add(key)
    indicators.append({"type": kind, "value": value, "sources": [_indicator_source(finding)]})


def _extract_enrichment_indicators(report: dict[str, Any]) -> list[dict[str, Any]]:
    indicators: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for finding in list(report.get("findings", [])) + list(report.get("suppressed_findings", [])):
        if not isinstance(finding, dict):
            continue
        evidence = str(finding.get("evidence") or "")
        title = str(finding.get("title") or "")
        for match in INDICATOR_URL_RE.finditer(evidence):
            url = match.group(0)
            _add_indicator(indicators, seen, kind="url", value=url, finding=finding)
            parsed = urlparse(url)
            if parsed.hostname:
                _add_indicator(indicators, seen, kind="domain", value=parsed.hostname.lower(), finding=finding)
        npx_match = INDICATOR_NPX_RE.search(evidence)
        if npx_match:
            _add_indicator(indicators, seen, kind="npm-package", value=npx_match.group(1), finding=finding)
        action_match = INDICATOR_GITHUB_ACTION_RE.search(evidence)
        if action_match and "GitHub Action" in title:
            _add_indicator(indicators, seen, kind="github-action", value=action_match.group(1), finding=finding)
    return indicators


def _local_enrich(indicators: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for indicator in indicators:
        results.append(
            {
                "provider": "local",
                "type": indicator["type"],
                "value": indicator["value"],
                "status": "observed",
                "match_count": 0,
                "cache_hit": False,
                "summary": "Observed in the local security report; no external lookup was performed.",
                "source_fingerprints": [
                    source.get("fingerprint") for source in indicator["sources"] if source.get("fingerprint")
                ],
            }
        )
    return results


def _cache_file(target: Path, config: SecurityEnrichmentConfig) -> Path:
    return target / config.cache_path


def _read_enrichment_cache(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _write_enrichment_cache(path: Path, cache: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n")


def _misp_query_indicator(
    *,
    base_url: str,
    api_key: str,
    timeout_seconds: int,
    indicator: dict[str, Any],
) -> dict[str, Any]:
    endpoint = base_url.rstrip("/") + "/attributes/restSearch"
    body = json.dumps({"returnFormat": "json", "value": indicator["value"]}).encode()
    req = urlrequest.Request(
        endpoint,
        data=body,
        headers={
            "Authorization": api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlrequest.urlopen(req, timeout=timeout_seconds) as response:
        raw = response.read().decode("utf-8", errors="replace")
    payload = json.loads(raw) if raw.strip() else {}
    attributes = _misp_attributes(payload)
    tags = sorted(
        {
            str(tag.get("name"))
            for attribute in attributes
            if isinstance(attribute, dict)
            for tag in attribute.get("Tag", [])
            if isinstance(tag, dict) and tag.get("name")
        }
    )
    return {
        "provider": "misp",
        "type": indicator["type"],
        "value": indicator["value"],
        "status": "hit" if attributes else "miss",
        "match_count": len(attributes),
        "tags": tags[:10],
        "cache_hit": False,
        "summary": f"MISP returned {len(attributes)} attribute match(es).",
        "source_fingerprints": [
            source.get("fingerprint") for source in indicator["sources"] if source.get("fingerprint")
        ],
    }


def _misp_attributes(payload: object) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        response = payload.get("response", payload)
        if isinstance(response, dict):
            attributes = response.get("Attribute", [])
            return [item for item in attributes if isinstance(item, dict)] if isinstance(attributes, list) else []
        if isinstance(response, list):
            return [item for item in response if isinstance(item, dict)]
    return []


def _misp_enrich(
    target: Path, config: SecurityEnrichmentConfig, indicators: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if not config.misp_url:
        raise ValueError("enrichment.misp_url is required when provider is misp")
    api_key = os.environ.get(config.misp_api_key_env)
    if not api_key:
        raise ValueError(f"environment variable {config.misp_api_key_env} is required when provider is misp")
    cache_path = _cache_file(target, config)
    cache = _read_enrichment_cache(cache_path)
    results: list[dict[str, Any]] = []
    changed = False
    for indicator in indicators:
        cache_key = f"misp:{indicator['type']}:{indicator['value'].lower()}"
        cached = cache.get(cache_key)
        if isinstance(cached, dict):
            result = dict(cached)
            result["cache_hit"] = True
            results.append(result)
            continue
        try:
            result = _misp_query_indicator(
                base_url=config.misp_url,
                api_key=api_key,
                timeout_seconds=config.timeout_seconds,
                indicator=indicator,
            )
        except (OSError, urlerror.URLError, json.JSONDecodeError) as exc:
            result = {
                "provider": "misp",
                "type": indicator["type"],
                "value": indicator["value"],
                "status": "error",
                "match_count": 0,
                "cache_hit": False,
                "summary": f"MISP lookup failed: {exc}",
                "source_fingerprints": [
                    source.get("fingerprint") for source in indicator["sources"] if source.get("fingerprint")
                ],
            }
        cache[cache_key] = result
        changed = True
        results.append(result)
    if changed:
        _write_enrichment_cache(cache_path, cache)
    return results


def _render_enrichment_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Brigade Security Enrichment",
        "",
        f"- provider: `{payload['provider']}`",
        f"- generated_at: `{payload['generated_at']}`",
        f"- report: `{payload['report']}`",
        f"- indicators: `{payload['indicator_count']}`",
        f"- hits: `{payload['hit_count']}`",
        f"- errors: `{payload['error_count']}`",
        "",
        "## Results",
        "",
    ]
    if not payload["results"]:
        lines.append("No enrichment indicators were extracted.")
    for result in payload["results"]:
        lines.extend(
            [
                f"### {result['type']} - {result['value']}",
                "",
                f"- status: `{result['status']}`",
                f"- matches: `{result['match_count']}`",
                f"- cache_hit: `{result.get('cache_hit', False)}`",
                f"- summary: {result['summary']}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _render_enrichment_summary(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            ENRICHMENT_MARKDOWN_START,
            "## Enrichment",
            "",
            f"- provider: `{payload['provider']}`",
            f"- generated_at: `{payload['generated_at']}`",
            f"- indicators: `{payload['indicator_count']}`",
            f"- hits: `{payload['hit_count']}`",
            f"- errors: `{payload['error_count']}`",
            "- details: `security-enrichment.md`",
            ENRICHMENT_MARKDOWN_END,
            "",
        ]
    )


def _upsert_report_enrichment_summary(output_dir: Path, payload: dict[str, Any]) -> None:
    report_markdown = output_dir / "security-report.md"
    if not report_markdown.is_file():
        return
    existing = report_markdown.read_text()
    summary = _render_enrichment_summary(payload)
    start = existing.find(ENRICHMENT_MARKDOWN_START)
    end = existing.find(ENRICHMENT_MARKDOWN_END)
    if start != -1 and end != -1 and end > start:
        end += len(ENRICHMENT_MARKDOWN_END)
        updated = existing[:start].rstrip() + "\n\n" + summary + existing[end:].lstrip()
    else:
        updated = existing.rstrip() + "\n\n" + summary
    report_markdown.write_text(updated)


def write_enrichment_bundle(payload: dict[str, Any], output_dir: Path) -> Path:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = dict(payload)
    payload["artifacts"] = str(output_dir)
    (output_dir / "security-enrichment.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    (output_dir / "security-enrichment.md").write_text(_render_enrichment_markdown(payload))
    _upsert_report_enrichment_summary(output_dir, payload)
    return output_dir


def enrichment_health(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    config = load_config(target)
    if config is None:
        return {"configured": False, "provider": None, "status": "missing config"}
    provider = config.enrichment.provider
    if not provider:
        return {"configured": False, "provider": None, "status": "missing provider"}
    if provider == "local":
        return {"configured": True, "provider": provider, "status": "offline local provider"}
    if provider == "misp":
        missing = []
        if not config.enrichment.misp_url:
            missing.append("misp_url")
        if not os.environ.get(config.enrichment.misp_api_key_env):
            missing.append(config.enrichment.misp_api_key_env)
        return {
            "configured": not missing,
            "provider": provider,
            "status": "ready" if not missing else f"missing {', '.join(missing)}",
        }
    return {"configured": False, "provider": provider, "status": "unsupported provider"}


def enrich(
    *,
    target: Path,
    output_dir: Path | None = None,
    report_path: Path | None = None,
    provider: str | None = None,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    artifacts_dir = output_dir.expanduser().resolve() if output_dir is not None else default_artifacts_dir(target)
    report_file = (
        report_path.expanduser().resolve() if report_path is not None else artifacts_dir / "security-report.json"
    )
    try:
        loaded = load_config(target)
    except ValueError as exc:
        print(f"error: invalid security config: {exc}", file=sys.stderr)
        return 2
    config = loaded.enrichment if loaded is not None else SecurityEnrichmentConfig()
    provider_name = provider or config.provider
    if provider_name is None:
        print(
            "error: security enrichment provider is not configured; run `brigade security init` or pass `--provider local`",
            file=sys.stderr,
        )
        return 2
    if provider_name not in ENRICHMENT_PROVIDERS:
        print("error: --provider must be one of: local, misp", file=sys.stderr)
        return 2
    if provider is not None:
        config = SecurityEnrichmentConfig(
            provider=provider,
            misp_url=config.misp_url,
            misp_api_key_env=config.misp_api_key_env,
            timeout_seconds=config.timeout_seconds,
            cache_path=config.cache_path,
        )
    try:
        report = _load_report_file(report_file)
    except FileNotFoundError as exc:
        print(f"error: security report not found: {exc}", file=sys.stderr)
        return 2
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"error: invalid security report: {exc}", file=sys.stderr)
        return 2

    indicators = _extract_enrichment_indicators(report)
    try:
        if provider_name == "local":
            results = _local_enrich(indicators)
        else:
            results = _misp_enrich(target, config, indicators)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    payload = {
        "target": str(target),
        "report": str(report_file),
        "provider": provider_name,
        "generated_at": _utc_iso(),
        "indicator_count": len(indicators),
        "result_count": len(results),
        "hit_count": len([item for item in results if item.get("status") == "hit"]),
        "error_count": len([item for item in results if item.get("status") == "error"]),
        "indicators": indicators,
        "results": results,
    }
    artifacts_path = write_enrichment_bundle(payload, artifacts_dir)
    payload["artifacts"] = str(artifacts_path)

    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"security enrich: {target}")
        print(f"provider: {provider_name}")
        print(f"report: {report_file}")
        print(f"artifacts: {artifacts_path}")
        print(f"indicators: {payload['indicator_count']}")
        print(f"hits: {payload['hit_count']}")
        print(f"errors: {payload['error_count']}")
    return 1 if payload["error_count"] else 0
