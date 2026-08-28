"""CLI parser / inventory contract (#1229).

The parser, docs/command-inventory.md, and the roadmap missing-command
check must agree after alias normalization. Paths with zero direct test
invocations are reported, not failed.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

from brigade import cli, roadmap_cmd


REPO_ROOT = Path(__file__).resolve().parents[1]
COMMAND_INVENTORY = REPO_ROOT / "docs" / "command-inventory.md"


def _inventory_command_paths(text: str) -> list[str]:
    """Parse leaf command paths from generated command-inventory.md."""
    commands_section = text.split("## Commands", 1)[-1]
    paths: list[str] = []
    for line in commands_section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("- `brigade "):
            continue
        body = stripped[3:]
        if body.endswith("`"):
            body = body[:-1]
        elif " `" in body:
            continue
        command = body.split("`")[0].strip()
        if command.startswith("brigade "):
            paths.append(command)
    return paths


def _direct_test_invocations(tests_root: Path) -> set[str]:
    """Return parser-shaped paths invoked via cli.main([...]) in tests."""
    invoked: set[str] = set()
    for path in sorted(tests_root.glob("test_*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            is_main = (isinstance(func, ast.Attribute) and func.attr == "main") or (
                isinstance(func, ast.Name) and func.id == "main"
            )
            if not is_main or not node.args:
                continue
            first = node.args[0]
            if not isinstance(first, ast.List) or not first.elts:
                continue
            tokens: list[str] = []
            for elt in first.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    if elt.value.startswith("-"):
                        break
                    tokens.append(elt.value)
                else:
                    break
            if tokens:
                invoked.add(" ".join(["brigade", *tokens]))
    return invoked


def untested_parser_paths_report(*, tests_root: Path | None = None) -> dict[str, object]:
    """Build a non-failing report of parser paths with no direct test invocation."""
    tests_root = tests_root or Path(__file__).resolve().parent
    parser_paths = roadmap_cmd._cli_command_paths()
    invoked = _direct_test_invocations(tests_root)
    covered = set()
    for path in parser_paths:
        if path in invoked or any(item == path or item.startswith(f"{path} ") for item in invoked):
            covered.add(path)
    untested = [path for path in parser_paths if path not in covered]
    return {
        "parser_path_count": len(parser_paths),
        "invoked_count": len(invoked),
        "untested_count": len(untested),
        "untested": untested,
    }


def test_parser_inventory_and_missing_cli_agree_after_alias_normalization():
    parser_paths = roadmap_cmd._cli_command_paths()
    inventory_paths = _inventory_command_paths(COMMAND_INVENTORY.read_text(encoding="utf-8"))

    assert "brigade run cloud status" in parser_paths
    assert "brigade run-cloud status" in parser_paths
    assert "brigade harness fragments" in parser_paths
    assert "brigade openclaw-fragments" in parser_paths
    assert "brigade hermes-fragments" in parser_paths
    assert "brigade run" in parser_paths

    parser_normalized = {roadmap_cmd._apply_command_alias(path) for path in parser_paths}
    inventory_normalized = {roadmap_cmd._apply_command_alias(path) for path in inventory_paths}
    assert parser_normalized == inventory_normalized

    payload = roadmap_cmd.audit_payload(REPO_ROOT)
    missing = next(item for item in payload["checks"] if item["name"] == "roadmap_documented_command_missing_cli")
    assert missing["status"] == "ok"
    assert payload["missing_cli_commands"] == []


def test_untested_parser_paths_report_is_well_formed_and_non_failing():
    report = untested_parser_paths_report()
    assert isinstance(report["untested"], list)
    assert report["untested_count"] == len(report["untested"])
    assert report["parser_path_count"] >= report["untested_count"]
    # Presence of untested paths is a maintenance signal, not a failure.


def test_run_cloud_is_a_native_parser_branch():
    parser = cli._build_parser()
    run_parser = parser._subparsers._group_actions[0].choices["run"]
    cloud = None
    for action in run_parser._actions:
        if isinstance(action, argparse._SubParsersAction) and "cloud" in action.choices:
            cloud = action.choices["cloud"]
            break
    assert cloud is not None
    assert "status" in {
        name for action in cloud._actions if isinstance(action, argparse._SubParsersAction) for name in action.choices
    }


def test_run_cloud_alias_prints_deprecation_notice(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(
        "brigade.cloud_tracker.observe_providers", lambda target, **kwargs: ({}, {"branches": [], "prs": []}, False)
    )
    rc = cli.main(["run-cloud", "status", "--target", str(tmp_path), "--json"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "deprecated" in err
    assert "brigade run cloud" in err


def test_harness_fragments_writes_shared_backend(tmp_path):
    out = tmp_path / "frags"
    assert cli.main(["harness", "fragments", "--harness", "openclaw", "--out", str(out)]) == 0
    assert (out / "README.md").is_file()
    assert cli.main(["harness", "fragments", "--harness", "hermes", "--out", str(tmp_path / "hermes")]) == 0


def test_fragment_aliases_still_dispatch(tmp_path):
    assert cli.main(["openclaw-fragments", "--out", str(tmp_path / "oc")]) == 0
    assert cli.main(["hermes-fragments", "--out", str(tmp_path / "hm")]) == 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report parser paths with zero direct test invocations.")
    parser.add_argument("--json", action="store_true", help="Emit the untested-path report as JSON.")
    args = parser.parse_args(argv)
    report = untested_parser_paths_report()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"untested parser paths: {report['untested_count']} / {report['parser_path_count']}")
        for path in report["untested"]:
            print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
