"""Fleet Hub wire contract goldens: refusal text and sealed tombstones (v2 slice S1b).

`hub-contract-golden.json` holds two tables captured from the in-repo Python
hub by `control-brigade.py hub-contract --write-golden`:

- `refusals`: for every case whose expected status is 4xx, the exact `error`,
  `code` and `reason` values the hub answered (or the body text, for a
  refusal that is not JSON). Run-specific values are replaced before the
  compare: the drive's random tag becomes `{tag}` and ISO timestamps become
  `{timestamp}`. Ids and every other key are left to the shape checks.
- `tombstones`: the envelope text Python's own purge wrote for the lifecycle
  cases' sealed messages. The lifecycle envelopes are deterministic, so the
  bytes are the same on every run and on any hub that matches Python.

A hub under test is compared to this file, never to strings copied from the
hub's source.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

GOLDEN_VERSION = 1
GOLDEN_PATH = Path(__file__).resolve().parent / "hub-contract-golden.json"
SECTIONS = ("core", "lifecycle")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?")


def normalize(value: Any, tag: str | None) -> Any:
    if isinstance(value, str):
        if tag:
            value = value.replace(tag, "{tag}")
        return _TIMESTAMP.sub("{timestamp}", value)
    if isinstance(value, dict):
        return {key: normalize(item, tag) for key, item in value.items()}
    if isinstance(value, list):
        return [normalize(item, tag) for item in value]
    return value


def entry(status: int, response: Any, tag: str | None) -> dict:
    """The part of one refusal the golden pins."""
    found: dict[str, Any] = {"status": status}
    if isinstance(response, dict):
        for key in ("error", "code", "reason"):
            if key in response:
                found[key] = normalize(response[key], tag)
    else:
        found["body"] = normalize(response, tag)
    return found


def refusals(result: dict, tag: str | None) -> dict[str, dict]:
    """Every refusal the drive saw at its expected status, keyed by case."""
    expected = {case["case"]: case.get("expected_status") for case in result["cases"]}
    seen: dict[str, dict] = {}
    for exchange in result["exchanges"]:
        status = expected.get(exchange["case"])
        if isinstance(status, int) and status >= 400 and exchange.get("status") == status:
            seen[exchange["case"]] = entry(status, exchange.get("response"), tag)
    return seen


def load(path: Path = GOLDEN_PATH) -> dict:
    try:
        golden = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    return golden if isinstance(golden, dict) else {}


def write(path: Path, observed: dict[str, dict[str, dict]], tombstones: dict[str, str]) -> None:
    golden = {
        "golden_version": GOLDEN_VERSION,
        "contract_version": 1,
        "source": "control-brigade.py hub-contract --write-golden on the in-repo Python hub",
        "refusals": {section: dict(sorted(observed.get(section, {}).items())) for section in SECTIONS},
        "tombstones": dict(sorted(tombstones.items())),
    }
    path.write_text(json.dumps(golden, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def compare(
    golden: dict,
    observed: dict[str, dict[str, dict]],
    ran: dict[str, set[str]],
    tombstones: dict[str, str] | None,
    tombstone_cases: dict[str, str] | None = None,
) -> list[dict]:
    """One mismatch per refusal or tombstone that differs from the golden.

    `ran` names every case each section ran, so a golden refusal whose case
    never ran is reported, while one whose case ran at the wrong status is
    left to that case's own status mismatch. A section that did not run is
    skipped; `tombstones` is None when the lifecycle cases did not run.
    """
    problems: list[dict] = []
    tables = golden.get("refusals") if isinstance(golden.get("refusals"), dict) else {}
    for section, seen in observed.items():
        table = tables.get(section) if isinstance(tables.get(section), dict) else {}
        for case, found in seen.items():
            want = table.get(case)
            if want != found:
                problems.append(
                    {
                        "case": case,
                        "check": "error-text",
                        "expected": want if want is not None else "an entry in hub-contract-golden.json",
                        "observed": found,
                    }
                )
        missing = sorted(case for case in table if case not in ran.get(section, set()))
        if missing:
            problems.append(
                {
                    "case": f"error-text golden ({section})",
                    "check": "invariant",
                    "expected": "every golden refusal is sent by the drive",
                    "observed": {"never sent": missing},
                }
            )
    if tombstones is not None:
        recorded = golden.get("tombstones") if isinstance(golden.get("tombstones"), dict) else {}
        for client_id in sorted(set(recorded) | set(tombstones)):
            if recorded.get(client_id) != tombstones.get(client_id):
                problems.append(
                    {
                        "case": (tombstone_cases or {}).get(client_id, f"lifecycle tombstone {client_id}"),
                        "check": "tombstone",
                        "expected": recorded.get(client_id, "an entry in hub-contract-golden.json"),
                        "observed": tombstones.get(client_id, "<not returned>"),
                    }
                )
    return problems
