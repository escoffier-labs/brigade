from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .n8n_advisory import run_advisory_check


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="content-guard-n8n-validate",
        description="Run synthetic n8n advisory fixtures and verify expected Content Guard results.",
    )
    parser.add_argument(
        "fixtures_dir",
        nargs="?",
        default=str(_default_fixtures_dir()),
        help="directory of JSON fixtures, default: examples/n8n-validation",
    )
    parser.add_argument("--policy", help="default policy alias or JSON path when a fixture omits policy")
    parser.add_argument("--json", action="store_true", help="emit machine-readable validation results")
    parser.add_argument("--opf", action="store_true", help="run optional OPF backend")
    parser.add_argument("--opf-bin", help="path to opf binary")
    parser.add_argument("--opf-device", help="OPF device, default comes from policy or cpu")
    args = parser.parse_args(argv)

    payload = run_validation(args)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(_format_text(payload))

    return 0 if payload["ok"] else 1


def run_validation(args: argparse.Namespace) -> dict[str, Any]:
    fixtures_dir = Path(args.fixtures_dir)
    fixtures = sorted(fixtures_dir.glob("*.json"))
    if not fixtures_dir.is_dir():
        return {
            "ok": False,
            "fixtures_dir": str(fixtures_dir),
            "error": "fixtures directory does not exist",
            "fixtures": [],
        }
    if not fixtures:
        return {
            "ok": False,
            "fixtures_dir": str(fixtures_dir),
            "error": "no JSON fixtures found",
            "fixtures": [],
        }

    results = [_run_fixture(path, args) for path in fixtures]
    return {
        "ok": all(item["ok"] for item in results),
        "fixtures_dir": str(fixtures_dir),
        "fixtures": results,
    }


def _run_fixture(path: Path, args: argparse.Namespace) -> dict[str, Any]:
    try:
        fixture = json.loads(path.read_text())
        if not isinstance(fixture, dict):
            raise ValueError("fixture must be a JSON object")

        request = fixture.get("request")
        if not isinstance(request, dict):
            raise ValueError("fixture must include object field 'request'")

        expected = fixture.get("expected")
        if not isinstance(expected, dict):
            raise ValueError("fixture must include object field 'expected'")

        request = _public_request(request)
        result = run_advisory_check(
            request,
            argparse.Namespace(policy=args.policy, opf=args.opf, opf_bin=args.opf_bin, opf_device=args.opf_device),
        )
        failures = _expectation_failures(result, expected)
        return {
            "ok": not failures,
            "path": str(path),
            "name": result["name"],
            "blocked": result["blocked"],
            "changed": result["changed"],
            "finding_rule_ids": _rule_ids(result),
            "failures": failures,
        }
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {
            "ok": False,
            "path": str(path),
            "failures": [str(exc)],
        }


def _public_request(request: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in request.items() if not key.startswith("_")}


def _expectation_failures(result: dict[str, Any], expected: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    for key in ("blocked", "changed"):
        if key in expected and result[key] != expected[key]:
            failures.append(f"expected {key}={expected[key]!r}, got {result[key]!r}")

    expected_rule_ids = expected.get("finding_rule_ids")
    if expected_rule_ids is not None:
        actual_rule_ids = _rule_ids(result)
        if actual_rule_ids != expected_rule_ids:
            failures.append(f"expected finding_rule_ids={expected_rule_ids!r}, got {actual_rule_ids!r}")

    return failures


def _rule_ids(result: dict[str, Any]) -> list[str]:
    return [finding["rule_id"] for finding in result.get("findings", [])]


def _format_text(payload: dict[str, Any]) -> str:
    lines = [f"Content Guard n8n validation: {'pass' if payload['ok'] else 'fail'}"]
    if payload.get("error"):
        lines.append(payload["error"])

    for fixture in payload["fixtures"]:
        status = "PASS" if fixture["ok"] else "FAIL"
        lines.append(f"{status} {fixture['path']}")
        if "blocked" in fixture:
            lines.append(
                "  "
                f"name={fixture['name']} "
                f"blocked={str(fixture['blocked']).lower()} "
                f"changed={str(fixture['changed']).lower()} "
                f"rules={','.join(fixture['finding_rule_ids']) or '-'}"
            )
        for failure in fixture["failures"]:
            lines.append(f"  {failure}")

    return "\n".join(lines)


def _default_fixtures_dir() -> Path:
    return Path(__file__).resolve().parent / "examples" / "n8n-validation"


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
