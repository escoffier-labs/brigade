#!/usr/bin/env python3
"""Aggregate `shadow-selection` artifacts into the numbers behind a skip decision.

Offline and stdlib only. Point it at a directory of downloaded artifacts (any
nesting; every `shadow-selection.json` found is one run), or let it pull them:

    python scripts/ci_shadow_evaluate.py --dir ./artifacts
    python scripts/ci_shadow_evaluate.py --gh-runs 200 --repo OWNER/NAME
    python scripts/ci_shadow_evaluate.py --dir ./artifacts --selector-digest 3f9a1c

Only `status: ok` runs count toward escape and saving statistics. Incomplete and
error runs are tallied separately and never read as a clean pass.

Seconds are summed junit testcase durations. They are not wall time: the shards
run in parallel and each uses xdist workers, so a saving in test seconds is an
upper bound on a saving in latency and says nothing about job setup cost.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Sequence


def load_runs(root: Path) -> list[dict]:
    runs = []
    for path in sorted(root.rglob("shadow-selection.json")):
        try:
            runs.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            runs.append({"status": "error", "errors": [f"unreadable: {path.parent.name}"], "policies": {}})
    return runs


def download(repo: str | None, limit: int, into: Path) -> list[str]:
    """Pull `shadow-selection` for recent pull_request runs with `gh`. Returns notes about gaps."""
    base = ["gh"] + (["--repo", repo] if repo else [])
    listing = subprocess.run(
        [*base, "run", "list", "--workflow", "ci.yml", "--event", "pull_request", "--limit", str(limit)]
        + ["--json", "databaseId"],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    notes = []
    for item in json.loads(listing.stdout):
        run_id = str(item["databaseId"])
        got = subprocess.run(
            [*base, "run", "download", run_id, "-n", "shadow-selection", "-D", str(into / run_id)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if got.returncode != 0:
            notes.append(f"run {run_id}: no shadow-selection artifact")
    return notes


def pct(value: float) -> str:
    return f"{100 * value:.1f}%"


def pr_key(run: dict) -> str:
    """Identify the pull request a run belongs to: its number, else the head SHA. Never a user or branch."""
    return str(run.get("pr_number") or run.get("sha") or run.get("run_id") or "?")


def evaluate(runs: list[dict], selector_digest: str | None = None) -> dict:
    """Aggregate runs. `selector_digest` keeps only runs whose plan came from that selector version."""
    digests = Counter(r.get("selector_digest") or "unknown" for r in runs)
    if selector_digest:
        runs = [r for r in runs if (r.get("selector_digest") or "").startswith(selector_digest)]
    ok = [r for r in runs if r.get("status") == "ok"]
    result: dict = {
        "selector_digests": digests,
        "selector_digest_filter": selector_digest,
        "runs": len(runs),
        "ok": len(ok),
        "incomplete": sum(r.get("status") == "incomplete" for r in runs),
        "error": sum(r.get("status") not in ("ok", "incomplete") for r in runs),
        "incomplete_causes": Counter(
            e.split(":")[0][:60] for r in runs if r.get("status") != "ok" for e in r.get("errors", [])
        ),
    }
    full = [r for r in ok if r.get("mode") != "selective"]
    selective = [r for r in ok if r.get("mode") == "selective"]
    result["full_mode_runs"] = len(full)
    result["full_mode_rate"] = len(full) / len(ok) if ok else None
    result["full_reasons"] = Counter(reason for r in full for reason in r.get("full_reasons", []))
    result["full_unmapped_top"] = Counter(p for r in full for p in r.get("full_paths", [])).most_common(15)
    result["failing_runs"] = sum(bool(r["failing_files"]) for r in ok)
    result["selective_failing_runs"] = sum(bool(r["failing_files"]) for r in selective)
    result["selective_failing_prs"] = len({pr_key(r) for r in selective if r["failing_files"]})
    total_seconds = sum(r.get("total_test_seconds", 0) for r in ok)
    policies = {}
    names = sorted({name for r in ok for name in r.get("policies", {})})
    for name in names:
        rows = [r["policies"][name] for r in ok if name in r.get("policies", {})]
        sel_rows = [r["policies"][name] for r in selective if name in r.get("policies", {})]
        escaping = [r for r in selective if (p := r.get("policies", {}).get(name)) and p["escape_files"]]
        escapes = [
            (r.get("run_id", "?"), r["policies"][name]["escape_files"], r["policies"][name]["escape_nodeids"])
            for r in escaping
        ]
        failing = [r for r in selective if r["failing_files"]]
        policies[name] = {
            "median_selected_fraction_all": statistics.median(p["selected_fraction"] for p in rows) if rows else None,
            "median_selected_fraction_selective": statistics.median(p["selected_fraction"] for p in sel_rows)
            if sel_rows
            else None,
            "selected_seconds_share": sum(p["selected_seconds"] for p in rows) / total_seconds
            if total_seconds
            else None,
            "escape_runs": len(escapes),
            "escape_prs": len({pr_key(r) for r in escaping}),
            "escape_rate_per_failing_selective_run": len(escapes) / len(failing) if failing else None,
            "escapes": escapes,
        }
    result["policies"] = policies
    return result


def render(result: dict) -> str:
    out = [
        "selector digests seen: "
        + (", ".join(f"{k[:12]} x{v}" for k, v in result["selector_digests"].most_common()) or "none")
        + (f" (filtered to {result['selector_digest_filter']})" if result["selector_digest_filter"] else ""),
        f"runs: {result['runs']} (ok {result['ok']}, incomplete {result['incomplete']}, error {result['error']})",
    ]
    if result["incomplete_causes"]:
        out.append(
            "  incomplete causes: " + ", ".join(f"{k} x{v}" for k, v in result["incomplete_causes"].most_common(6))
        )
    if result["ok"]:
        out.append(
            f"full-mode rate: {pct(result['full_mode_rate'])} ({result['full_mode_runs']} of {result['ok']} ok runs)"
        )
        out.append(
            "  full reasons: " + (", ".join(f"{k} x{v}" for k, v in result["full_reasons"].most_common()) or "none")
        )
        if result["full_unmapped_top"]:
            out.append(
                "  most frequent triggering paths: "
                + ", ".join(f"{p} x{n}" for p, n in result["full_unmapped_top"][:8])
            )
    out.append(
        f"runs with failing tests: {result['failing_runs']} (selective-mode: {result['selective_failing_runs']} runs, "
        f"{result['selective_failing_prs']} distinct PRs)"
    )
    for name, p in result["policies"].items():
        out.append(f"policy {name}:")
        if p["median_selected_fraction_all"] is not None:
            out.append(
                f"  median selected fraction: {pct(p['median_selected_fraction_all'])} all ok runs, "
                f"{pct(p['median_selected_fraction_selective'] or 0)} selective-mode runs"
            )
        if p["selected_seconds_share"] is not None:
            out.append(
                f"  would-be test-seconds share: {pct(p['selected_seconds_share'])} of summed junit durations "
                "(not wall time)"
            )
        rate = p["escape_rate_per_failing_selective_run"]
        out.append(
            f"  escapes: {p['escape_runs']} runs, {p['escape_prs']} distinct PRs; rate per failing selective run: "
            f"{'n/a' if rate is None else pct(rate)}"
        )
        for run_id, files, nodes in p["escapes"][:10]:
            out.append(f"    run {run_id}: {', '.join(files)} ({len(nodes)} test ids)")
    return "\n".join(out)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", type=Path, help="directory holding downloaded shadow-selection artifacts")
    parser.add_argument("--gh-runs", type=int, help="download the newest N pull_request runs' artifacts with gh")
    parser.add_argument("--repo", help="OWNER/NAME for gh; defaults to the current checkout")
    parser.add_argument("--selector-digest", help="keep only runs whose plan has this selector_digest (prefix match)")
    parser.add_argument("--json", action="store_true", help="print the aggregate as JSON")
    args = parser.parse_args(argv)
    if args.dir is None and args.gh_runs is None:
        parser.error("give --dir or --gh-runs")
    notes: list[str] = []
    with tempfile.TemporaryDirectory() as scratch:
        root = args.dir or Path(scratch)
        if args.gh_runs is not None:
            notes = download(args.repo, args.gh_runs, root)
        result = evaluate(load_runs(root), args.selector_digest)
    result["download_notes"] = len(notes)
    if args.json:
        print(json.dumps(result, indent=1, sort_keys=True, default=list))
    else:
        print(render(result))
        if notes:
            print(f"({len(notes)} listed runs had no artifact, e.g. runs before this job existed or docs-only runs)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
