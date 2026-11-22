from __future__ import annotations

import argparse
from pathlib import Path

from brigade import cli


SKILL = Path(__file__).resolve().parents[1] / "src/brigade/templates/skills/orca-governed-delegation/SKILL.md"
ORCA_VERBS = {
    "open",
    "claim",
    "accepted",
    "observe",
    "ack",
    "finalize",
    "cancel",
    "hold-resolve",
    "status",
}


def _subparsers(parser: argparse.ArgumentParser) -> argparse._SubParsersAction:
    return next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))


def test_documented_orca_verbs_exist_in_cli_parser():
    root = _subparsers(cli._build_parser())
    orca = _subparsers(root.choices["orca"])

    assert ORCA_VERBS <= set(orca.choices)
    documented = {f"`{verb}`" for verb in ORCA_VERBS}
    assert all(token in SKILL.read_text() for token in documented)
