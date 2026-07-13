"""Privacy-safe projections of Brigade run receipts into telemetry conventions."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from . import localio

OTEL_MAPPING = {
    "name": "otel-genai",
    "mapping_version": 1,
    "upstream_revision": "opentelemetry-semconv-1.43.0",
}
OPENINFERENCE_MAPPING = {
    "name": "openinference",
    "mapping_version": 1,
    "upstream_revision": "audited-2026-07-12",
}


def _object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _ids(run_id: str, seat: str, ordinal: int) -> tuple[str, str]:
    trace = hashlib.sha256(f"brigade:{run_id}".encode()).hexdigest()[:32]
    span = hashlib.sha256(f"brigade:{run_id}:{seat}:{ordinal}".encode()).hexdigest()[:16]
    return trace, span


def _provider(cli: str) -> str | None:
    if cli == "codex":
        return "openai"
    if cli == "claude":
        return "anthropic"
    if cli == "grok":
        return "x_ai"
    return None


def _error_type(result: dict[str, Any]) -> str | None:
    if result.get("ok") is True:
        return None
    if result.get("timed_out") is True:
        return "timeout"
    exit_code = result.get("exit_code")
    if isinstance(exit_code, int) and not isinstance(exit_code, bool):
        return "process_error"
    return "worker_error"


def _base(run_id: str, run: dict[str, Any], seat: str, result: dict[str, Any], ordinal: int) -> dict[str, Any]:
    trace_id, span_id = _ids(run_id, seat, ordinal)
    return {
        "trace_id": trace_id,
        "span_id": span_id,
        "name": "brigade.run.worker",
        "start_time": run.get("started_at"),
        "end_time": run.get("finished_at"),
        "duration_seconds": result.get("duration_seconds"),
        "status": "OK" if result.get("ok") is True else "ERROR",
        "error_type": _error_type(result),
        "seat": seat,
        "adapter": result.get("transport") or "cli",
        "requested_model": result.get("requested_model"),
        "effective_model": result.get("effective_model"),
        "reasoning": result.get("reasoning"),
        "stop_reason": result.get("stop_reason"),
        "exit_code": result.get("exit_code"),
    }


def _otel(base: dict[str, Any], cli: str) -> dict[str, Any]:
    attributes: dict[str, Any] = {
        "gen_ai.operation.name": "invoke_agent",
        "brigade.seat.name": base["seat"],
        "brigade.adapter.name": base["adapter"],
    }
    provider = _provider(cli)
    if provider is not None:
        attributes["gen_ai.provider.name"] = provider
    if base["requested_model"] is not None:
        attributes["gen_ai.request.model"] = base["requested_model"]
    if base["effective_model"] is not None:
        attributes["gen_ai.response.model"] = base["effective_model"]
    if base["stop_reason"] is not None:
        attributes["gen_ai.response.finish_reasons"] = [base["stop_reason"]]
    if base["error_type"] is not None:
        attributes["error.type"] = base["error_type"]
    return {"schema": "brigade.otel_genai_projection.v1", "mapping": OTEL_MAPPING, **base, "attributes": attributes}


def _openinference(base: dict[str, Any]) -> dict[str, Any]:
    attributes: dict[str, Any] = {
        "openinference.span.kind": "AGENT",
        "brigade.seat.name": base["seat"],
        "brigade.adapter.name": base["adapter"],
    }
    model = base["effective_model"] or base["requested_model"]
    if model is not None:
        attributes["llm.model_name"] = model
    return {
        "schema": "brigade.openinference_projection.v1",
        "mapping": OPENINFERENCE_MAPPING,
        **base,
        "attributes": attributes,
    }


def records(target: Path, projection: str) -> list[dict[str, Any]]:
    target = target.expanduser().resolve()
    rows: list[dict[str, Any]] = []
    root = target / ".brigade" / "runs"
    for run_dir in sorted(root.iterdir()) if root.is_dir() else []:
        if not run_dir.is_dir():
            continue
        run = _object(run_dir / "run.json")
        roster = _object(run_dir / "roster.json")
        workers = _object(run_dir / "worker-results.json")
        if run is None or roster is None or workers is None:
            continue
        agents = roster.get("agents")
        if not isinstance(agents, dict):
            continue
        for ordinal, result in enumerate(workers.get("results", []), start=1):
            if not isinstance(result, dict) or not isinstance(result.get("worker"), str):
                continue
            seat = result["worker"]
            agent = agents.get(seat)
            if not isinstance(agent, dict):
                continue
            cli = str(agent.get("cli") or "unknown")
            base = _base(run_dir.name, run, seat, result, ordinal)
            rows.append(_otel(base, cli) if projection == "otel-genai" else _openinference(base))
    return rows


def export(*, target: Path, projection: str, out: str = "-") -> int:
    rows = records(target, projection)
    if not rows:
        print("error: no Brigade worker receipts found", file=sys.stderr)
        return 1
    rendered = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
    if out == "-":
        sys.stdout.write(rendered)
    else:
        localio.write_text_atomic(Path(out).expanduser(), rendered)
    return 0
