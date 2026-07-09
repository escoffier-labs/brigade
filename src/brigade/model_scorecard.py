"""Read-only model scorecard over Brigade run artifacts.

Aggregates roster + worker-results under ``.brigade/runs/*`` (or explicit
``--runs-dir`` roots) by ``(cli, model)``. Missing/null model is shown as the
CLI alone. Never writes, never networks.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class SkippedDir:
    path: str
    reason: str


@dataclass(frozen=True)
class ModelRow:
    cli: str
    model: str | None
    runs: int
    worker_seats: int
    orchestrator_seats: int
    worker_ok: int
    suspected_no_op: int
    total_duration_seconds: float
    first_seen: str | None
    last_seen: str | None

    @property
    def label(self) -> str:
        if self.model:
            return f"{self.cli}/{self.model}"
        return self.cli

    @property
    def seats(self) -> int:
        return self.worker_seats + self.orchestrator_seats

    @property
    def ok_rate(self) -> float:
        if self.worker_seats <= 0:
            return 0.0
        return self.worker_ok / self.worker_seats

    @property
    def mean_duration_seconds(self) -> float:
        if self.runs <= 0:
            return 0.0
        return self.total_duration_seconds / self.runs


@dataclass(frozen=True)
class Scorecard:
    models: list[ModelRow]
    scanned: int
    skipped: int
    skipped_dirs: list[SkippedDir] = field(default_factory=list)


@dataclass
class _Acc:
    cli: str
    model: str | None
    run_ids: set[str] = field(default_factory=set)
    worker_seats: int = 0
    orchestrator_seats: int = 0
    worker_ok: int = 0
    suspected_no_op_runs: set[str] = field(default_factory=set)
    total_duration_seconds: float = 0.0
    first_seen: str | None = None
    last_seen: str | None = None

    def note_seen(self, started_at: str | None, run_id: str, duration: float | None) -> None:
        if run_id not in self.run_ids:
            self.run_ids.add(run_id)
            if isinstance(duration, (int, float)):
                self.total_duration_seconds += float(duration)
        if not started_at:
            return
        if self.first_seen is None or started_at < self.first_seen:
            self.first_seen = started_at
        if self.last_seen is None or started_at > self.last_seen:
            self.last_seen = started_at

    def to_row(self) -> ModelRow:
        return ModelRow(
            cli=self.cli,
            model=self.model,
            runs=len(self.run_ids),
            worker_seats=self.worker_seats,
            orchestrator_seats=self.orchestrator_seats,
            worker_ok=self.worker_ok,
            suspected_no_op=len(self.suspected_no_op_runs),
            total_duration_seconds=self.total_duration_seconds,
            first_seen=self.first_seen,
            last_seen=self.last_seen,
        )


def _model_key(cli: str, model: Any) -> tuple[str, str | None]:
    if model is None or model == "":
        return cli, None
    return cli, str(model)


def _parse_since(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed_date = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("--since must use YYYY-MM-DD") from exc
    return datetime.combine(parsed_date, time.min, tzinfo=timezone.utc)


def _parse_started_at(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _read_json_object(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.is_file():
        return None, f"missing {path.name}"
    try:
        raw = path.read_text()
    except OSError as exc:
        return None, f"unreadable {path.name}: {exc}"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"invalid {path.name}: {exc}"
    if not isinstance(payload, dict):
        return None, f"{path.name} must be a JSON object"
    return payload, None


def _default_runs_dirs(target: Path) -> list[Path]:
    return [target.expanduser().resolve() / ".brigade" / "runs"]


def _resolve_runs_dirs(
    *,
    target: Path | None,
    runs_dirs: Iterable[Path] | None,
) -> list[Path]:
    # --runs-dir roots are EXTRA artifact roots: when a target is given its
    # default runs dir is always scanned too, so combining the flags never
    # silently drops the target's runs. Bare runs_dirs scan only themselves.
    roots: list[Path] = []
    if target is not None or not runs_dirs:
        roots.extend(_default_runs_dirs(target if target is not None else Path(".")))
    for p in runs_dirs or []:
        resolved = Path(p).expanduser().resolve()
        if resolved not in roots:
            roots.append(resolved)
    return roots


def _agent_cli_model(agent: dict[str, Any]) -> tuple[str, str | None] | None:
    cli = agent.get("cli")
    if not isinstance(cli, str) or not cli:
        return None
    model = agent.get("model")
    if model is not None and not isinstance(model, str):
        model = str(model) if model != "" else None
    if model == "":
        model = None
    return _model_key(cli, model)


def _ground_truth_empty_changes(gt: Any) -> bool:
    if not isinstance(gt, dict):
        return False
    if gt.get("available") is not True:
        return False
    changed = gt.get("changed_files")
    if changed is None:
        return True
    if not isinstance(changed, list):
        return False
    # .brigade/ entries are run housekeeping (roster edits, artifacts), not
    # task output; a run whose only changes live there still did no work.
    real = [f for f in changed if isinstance(f, str) and not f.startswith(".brigade/") and f != ".brigade"]
    return len(real) == 0


def build_scorecard(
    *,
    target: Path | None = None,
    runs_dirs: Iterable[Path] | None = None,
    since: str | None = None,
) -> Scorecard:
    """Scan run dirs and aggregate per (cli, model). Never raises on bad dirs."""
    since_dt = _parse_since(since)
    roots = _resolve_runs_dirs(target=target, runs_dirs=runs_dirs)

    acc: dict[tuple[str, str | None], _Acc] = {}
    scanned = 0
    skipped_dirs: list[SkippedDir] = []

    for root in roots:
        if not root.is_dir():
            skipped_dirs.append(SkippedDir(path=str(root), reason="runs directory not found"))
            continue
        try:
            children = sorted(root.iterdir(), key=lambda p: p.name)
        except OSError as exc:
            skipped_dirs.append(SkippedDir(path=str(root), reason=f"unreadable: {exc}"))
            continue
        for child in children:
            if not child.is_dir():
                continue
            # Snapshot acc size pattern: scan may mutate then decide to skip on
            # corrupt worker-results. Use a per-run temp acc merge instead.
            local: dict[tuple[str, str | None], _Acc] = {}
            ok, reason = _scan_run_dir(child, since=since_dt, into=local)
            if reason is not None:
                skipped_dirs.append(SkippedDir(path=str(child), reason=reason))
                continue
            if not ok:
                # Filtered by --since (or empty participation): not skipped.
                continue
            scanned += 1
            _merge_acc(acc, local)

    rows = [bucket.to_row() for bucket in acc.values()]
    rows.sort(key=lambda r: (-r.ok_rate, -r.seats, r.label))
    return Scorecard(
        models=rows,
        scanned=scanned,
        skipped=len(skipped_dirs),
        skipped_dirs=skipped_dirs,
    )


def _scan_run_dir(
    run_dir: Path,
    *,
    since: datetime | None,
    into: dict[tuple[str, str | None], _Acc],
) -> tuple[bool, str | None]:
    """Parse one run dir into ``into``. Mutates only on full success.

    Returns (scanned, skip_reason). skip_reason set => malformed/partial.
    scanned False and skip_reason None => filtered out (e.g. --since).
    """
    run_meta, err = _read_json_object(run_dir / "run.json")
    if err is not None:
        return False, err
    assert run_meta is not None

    roster, err = _read_json_object(run_dir / "roster.json")
    if err is not None:
        return False, err
    assert roster is not None

    agents_raw = roster.get("agents")
    if not isinstance(agents_raw, dict):
        return False, "roster.json agents missing or not an object"

    started_at_raw = run_meta.get("started_at")
    started_at = str(started_at_raw) if isinstance(started_at_raw, str) else None
    if since is not None:
        started_dt = _parse_started_at(started_at)
        if started_dt is None or started_dt < since:
            return False, None

    duration = run_meta.get("duration_seconds")
    duration_f = float(duration) if isinstance(duration, (int, float)) else None

    orchestrator_name = roster.get("orchestrator")
    if not isinstance(orchestrator_name, str):
        orch_meta = run_meta.get("orchestrator")
        orchestrator_name = orch_meta if isinstance(orch_meta, str) else None

    run_id = str(run_dir.resolve())
    agent_keys: dict[str, tuple[str, str | None]] = {}

    wr_path = run_dir / "worker-results.json"
    worker_payload: dict[str, Any] | None = None
    if wr_path.is_file():
        worker_payload, wr_err = _read_json_object(wr_path)
        if wr_err is not None:
            return False, wr_err

    # All inputs valid — mutate into.
    for name, agent in agents_raw.items():
        if not isinstance(name, str) or not isinstance(agent, dict):
            continue
        key = _agent_cli_model(agent)
        if key is None:
            continue
        agent_keys[name] = key
        bucket = into.get(key)
        if bucket is None:
            bucket = _Acc(cli=key[0], model=key[1])
            into[key] = bucket
        bucket.note_seen(started_at, run_id, duration_f)
        if orchestrator_name is not None and name == orchestrator_name:
            bucket.orchestrator_seats += 1

    results: list[Any] = []
    ground_truth: Any = None
    if worker_payload is not None:
        raw_results = worker_payload.get("results")
        if isinstance(raw_results, list):
            results = raw_results
        ground_truth = worker_payload.get("ground_truth")

    empty_changes = _ground_truth_empty_changes(ground_truth)
    noop_models: set[tuple[str, str | None]] = set()

    for item in results:
        if not isinstance(item, dict):
            continue
        worker_name = item.get("worker")
        if not isinstance(worker_name, str):
            continue
        key = agent_keys.get(worker_name)
        if key is None:
            continue
        bucket = into.get(key)
        if bucket is None:
            bucket = _Acc(cli=key[0], model=key[1])
            into[key] = bucket
            bucket.note_seen(started_at, run_id, duration_f)
        bucket.worker_seats += 1
        if item.get("ok") is True:
            bucket.worker_ok += 1
            if empty_changes:
                noop_models.add(key)

    for key in noop_models:
        into[key].suspected_no_op_runs.add(run_id)

    return True, None


def _merge_acc(
    dest: dict[tuple[str, str | None], _Acc],
    src: dict[tuple[str, str | None], _Acc],
) -> None:
    for key, src_bucket in src.items():
        dst = dest.get(key)
        if dst is None:
            dest[key] = _Acc(
                cli=src_bucket.cli,
                model=src_bucket.model,
                run_ids=set(src_bucket.run_ids),
                worker_seats=src_bucket.worker_seats,
                orchestrator_seats=src_bucket.orchestrator_seats,
                worker_ok=src_bucket.worker_ok,
                suspected_no_op_runs=set(src_bucket.suspected_no_op_runs),
                total_duration_seconds=src_bucket.total_duration_seconds,
                first_seen=src_bucket.first_seen,
                last_seen=src_bucket.last_seen,
            )
            continue
        for run_id in src_bucket.run_ids:
            # duration already embedded in src_bucket totals for its run_ids;
            # when merging, add duration contribution only for new run ids.
            if run_id not in dst.run_ids:
                dst.run_ids.add(run_id)
        dst.worker_seats += src_bucket.worker_seats
        dst.orchestrator_seats += src_bucket.orchestrator_seats
        dst.worker_ok += src_bucket.worker_ok
        dst.suspected_no_op_runs |= src_bucket.suspected_no_op_runs
        dst.total_duration_seconds += src_bucket.total_duration_seconds
        if src_bucket.first_seen is not None and (dst.first_seen is None or src_bucket.first_seen < dst.first_seen):
            dst.first_seen = src_bucket.first_seen
        if src_bucket.last_seen is not None and (dst.last_seen is None or src_bucket.last_seen > dst.last_seen):
            dst.last_seen = src_bucket.last_seen


def scorecard_to_dict(card: Scorecard) -> dict[str, Any]:
    """Stable JSON-serializable shape (sort_keys-friendly)."""
    return {
        "models": [
            {
                "cli": row.cli,
                "first_seen": row.first_seen,
                "label": row.label,
                "last_seen": row.last_seen,
                "mean_duration_seconds": row.mean_duration_seconds,
                "model": row.model,
                "ok_rate": row.ok_rate,
                "orchestrator_seats": row.orchestrator_seats,
                "runs": row.runs,
                "seats": row.seats,
                "suspected_no_op": row.suspected_no_op,
                "total_duration_seconds": row.total_duration_seconds,
                "worker_ok": row.worker_ok,
                "worker_seats": row.worker_seats,
            }
            for row in card.models
        ],
        "scanned": card.scanned,
        "skipped": card.skipped,
        "skipped_dirs": [{"path": s.path, "reason": s.reason} for s in card.skipped_dirs],
    }


def _format_rate(rate: float) -> str:
    return f"{rate * 100:5.1f}%"


def _format_duration(seconds: float) -> str:
    if seconds >= 100:
        return f"{seconds:.0f}s"
    return f"{seconds:.1f}s"


def format_scorecard_text(card: Scorecard, *, verbose: bool = False) -> str:
    lines: list[str] = []
    headers = (
        "model",
        "runs",
        "seats",
        "w_ok",
        "ok_rate",
        "noop",
        "mean_dur",
        "first_seen",
        "last_seen",
    )
    rows_text: list[tuple[str, ...]] = []
    for row in card.models:
        rows_text.append(
            (
                row.label,
                str(row.runs),
                f"{row.worker_seats}+{row.orchestrator_seats}",
                str(row.worker_ok),
                _format_rate(row.ok_rate),
                str(row.suspected_no_op),
                _format_duration(row.mean_duration_seconds),
                row.first_seen or "-",
                row.last_seen or "-",
            )
        )

    if not rows_text:
        lines.append("no model seats found")
    else:
        widths = [len(h) for h in headers]
        for cells in rows_text:
            for i, cell in enumerate(cells):
                widths[i] = max(widths[i], len(cell))
        lines.append("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
        lines.append("  ".join("-" * widths[i] for i in range(len(headers))))
        for cells in rows_text:
            lines.append("  ".join(cells[i].ljust(widths[i]) for i in range(len(headers))))

    lines.append("")
    lines.append(f"scanned: {card.scanned}  skipped: {card.skipped}")
    if verbose and card.skipped_dirs:
        lines.append("skipped dirs:")
        for item in card.skipped_dirs:
            lines.append(f"  - {item.path}: {item.reason}")
    return "\n".join(lines) + "\n"


def scorecard(
    *,
    target: Path = Path("."),
    runs_dirs: list[Path] | None = None,
    since: str | None = None,
    json_output: bool = False,
    verbose: bool = False,
) -> int:
    """CLI entry: print scorecard text or JSON. Read-only."""
    try:
        card = build_scorecard(target=target, runs_dirs=runs_dirs, since=since)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if json_output:
        payload = scorecard_to_dict(card)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    sys.stdout.write(format_scorecard_text(card, verbose=verbose))
    return 0


__all__ = [
    "ModelRow",
    "Scorecard",
    "SkippedDir",
    "build_scorecard",
    "format_scorecard_text",
    "scorecard",
    "scorecard_to_dict",
]
