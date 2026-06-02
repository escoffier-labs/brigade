# src/brigade/research/registry.py
from __future__ import annotations
import json, re
from pathlib import Path
from typing import Any, Dict, List, Optional

def _root(target: Path) -> Path:
    return target / ".brigade" / "research"

def _dir(target: Path, run_id: str) -> Path:
    return _root(target) / run_id

def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:40] or "run")

def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")

def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    return json.loads(path.read_text())

def create_run(target: Path, *, question: str, run_id: str, caps: Dict[str, Any]) -> str:
    d = _dir(target, run_id)
    d.mkdir(parents=True, exist_ok=True)
    _write_json(d / "run.json", {
        "run_id": run_id, "question": question, "status": "running",
        "caps": caps, "stats": {}, "artifacts": {}, "blockers": [],
    })
    return run_id

def show_run(target: Path, run_id: str) -> Optional[Dict[str, Any]]:
    return _read_json(_dir(target, run_id) / "run.json")

def list_runs(target: Path) -> List[Dict[str, Any]]:
    root = _root(target)
    if not root.exists():
        return []
    out = []
    for child in root.iterdir():
        rec = _read_json(child / "run.json")
        if rec:
            out.append(rec)
    out.sort(key=lambda r: str(r.get("run_id")), reverse=True)
    return out

def _update(target: Path, run_id: str, **fields: Any) -> None:
    p = _dir(target, run_id) / "run.json"
    rec = _read_json(p) or {}
    rec.update(fields)
    _write_json(p, rec)

def set_status(target: Path, run_id: str, status: str) -> None:
    _update(target, run_id, status=status)

def finish_run(target: Path, run_id: str, *, status: str, stats: Dict[str, Any],
               artifacts: Dict[str, Any], blockers: Optional[List[str]] = None) -> None:
    _update(target, run_id, status=status, stats=stats, artifacts=artifacts,
            blockers=blockers or [])

def save_checkpoint(target: Path, run_id: str, cp: Dict[str, Any]) -> None:
    _write_json(_dir(target, run_id) / "checkpoint.json", cp)

def load_checkpoint(target: Path, run_id: str) -> Optional[Dict[str, Any]]:
    return _read_json(_dir(target, run_id) / "checkpoint.json")

def run_dir(target: Path, run_id: str) -> Path:
    return _dir(target, run_id)

def append_event(target: Path, run_id: str, event: Dict[str, Any]) -> None:
    p = _dir(target, run_id) / "events.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as fh:
        fh.write(json.dumps(event) + "\n")
