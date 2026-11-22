from __future__ import annotations

import argparse
import re
from pathlib import Path

from brigade import cli


SKILL = Path(__file__).resolve().parents[1] / "src/brigade/templates/skills/orca-governed-delegation/SKILL.md"
DOC = Path(__file__).resolve().parents[1] / "docs/orca-governed-delegation.md"
ORCA_MENTION = re.compile(r"brigade orca\s+([a-z][a-z-]*)")
ORCA_COMMAND = re.compile(r"`(brigade orca [^`\n]+)`")
ORCA_VERBS = {"open", "claim", "accepted", "observe", "ack", "finalize", "cancel", "hold-resolve", "status"}


def _subparsers(parser: argparse.ArgumentParser) -> argparse._SubParsersAction:
    return next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))


def test_documented_orca_commands_and_flags_exist_in_cli_parser():
    root = _subparsers(cli._build_parser())
    orca = _subparsers(root.choices["orca"])
    documents = (SKILL.read_text(encoding="utf-8"), DOC.read_text(encoding="utf-8"))
    mentions = [match.group(1) for document in documents for match in ORCA_MENTION.finditer(document)]
    commands = [match.group(1) for document in documents for match in ORCA_COMMAND.finditer(document)]

    assert mentions and commands
    for verb in mentions:
        assert verb in orca.choices, f"undocumented Orca CLI verb: {verb}"

    documented_verbs = set(mentions)
    for command in commands:
        match = re.fullmatch(r"brigade orca\s+([a-z][a-z-]*)(.*)", command)
        assert match, f"invalid documented Orca command: {command}"
        verb, remainder = match.groups()
        assert verb in orca.choices, f"undocumented Orca CLI verb: {verb}"
        parser = orca.choices[verb]
        option_strings = {option for action in parser._actions for option in action.option_strings}
        for flag in re.findall(r"--[a-z][a-z0-9-]*", remainder):
            assert flag in option_strings, f"unsupported flag {flag} for brigade orca {verb}"

    assert ORCA_VERBS <= documented_verbs


SKILL_RULES = (
    "reuse the same `--retry-request`",
    "never start a new orca run or task to get around the circuit breaker",
    "`inbox --full`, never `check`",
    "`worker-show` alone is not",
    "brief may not have been submitted",
    "report every receipt to brigade, including refusals",
    "start-refused",
    "circuit-broken",
    "start-failed",
    "return a `result_id`",
    "awaiting-ack",
    "abandoned-unknown",
    "frees no slot",
    "operator must follow up",
    "stop_unknown",
    "outcome_unknown",
    "same printed argv",
    "do not use `hold-resolve` to settle a worker stop",
    "orca-unsupported",
    "release-unverified",
    "for a cancelled worker, after a confirmed stop, use the fresh full show alone to verify release",
    "a cancelled worker may not produce a worker_done report or transcript",
    "accept-conflict",
    "--transcript-json",
)
DOC_RULES = (
    "reuse the same `--retry-request`",
    "`inbox --full`, never `check`",
    "`worker-show` alone is not acceptance",
    "report every receipt to brigade, refusals included",
    "`hold-resolve` cannot settle a stop",
    "abandoned-unknown",
    "release-unverified",
    "for a cancelled worker, after a confirmed stop, use the fresh full show alone to verify release",
    "a cancelled worker may not produce a worker_done report or transcript",
)


def _normalized(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").lower().split())


def test_skill_documents_orca_lifecycle_safety_rules():
    # SKILL.md is what a coordinator reads, so each rule must be in it on its own, not only in the doc.
    skill = _normalized(SKILL)
    missing = [phrase for phrase in SKILL_RULES if phrase not in skill]
    assert not missing, f"SKILL.md is missing safety rules: {missing}"


def test_doc_documents_orca_lifecycle_safety_rules():
    doc = _normalized(DOC)
    missing = [phrase for phrase in DOC_RULES if phrase not in doc]
    assert not missing, f"docs/orca-governed-delegation.md is missing safety rules: {missing}"
