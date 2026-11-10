"""The connector memory write path, end to end over a temporary workspace (#2473 item 5).

`handoff_write` runs the real `brigade handoff lint`, publishes into the profile's writer inbox, the
real `brigade ingest` promotes the card, and the real `brigade memory search` finds it. Nothing here
is faked except the clock and the child process boundary: the runner calls the CLI entry point in
this process and hands back its stdout, the way the connector reads a child's stdout.
"""

from __future__ import annotations

import contextlib
import io
import json
from dataclasses import replace
from pathlib import Path

import pytest

from brigade import cli
from brigade.config import Config, write_config
from brigade.dot.contracts import DotError
from brigade.dot.memory_tools import MemoryTools
from brigade.dot.process import CommandResult, CommandRunner
from brigade.dot.profile import DOT, DUET, ConnectorProfile
from brigade.dot.runtime_config import parse_runtime
from brigade.selection import WRITER_INBOXES, Selection
from tests.dot_support import requires_local_state
from tests.test_dot_runtime import _document
from tests.test_dot_state import NOW

BRIGADE = "/fake/bin/brigade"
CARD_NAME = "connector-write-path-probe.md"
# One word no other card or fixture holds, so a search hit can only be the ingested card.
MARKER = "quokkaferry"
CARD = f"""---
topic: connector-write-path-probe
category: foundation
tags: [memory, handoff, connector]
---

# Connector write path probe

A connector handoff reaches canonical memory through lint and ingest. Marker: {MARKER}.
"""
HANDOFF = f"""# Memory Handoff

## Type

workflow

## Title

Connector write path probe

## Summary

Proves that a connector handoff becomes a searchable card.

## Durable facts

- A connector handoff is linted, lands in the writer inbox and is promoted by ingest.

## Evidence

- commands run: integration test

## Recommended memory action

create-card

## Target card

{CARD_NAME}

## Suggested card content

{CARD}"""
# The header passes the connector's argument check, the missing sections fail the real linter.
INVALID = "# Memory Handoff\n\n## Title\n\nNo type, no summary, no action.\n"


class InProcessBrigade(CommandRunner):
    """Run the real `brigade` CLI in this process and return its stdout as the child capture would."""

    def __init__(self) -> None:
        self.argvs: list[list[str]] = []

    def run(self, argv, *, stdin=b"", timeout, max_output, env=None):
        assert argv[0] == BRIGADE
        self.argvs.append(list(argv))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                code = cli.main(argv[1:])
            except SystemExit as exit_:
                code = exit_.code if isinstance(exit_.code, int) else 1
        return CommandResult(code, out.getvalue().encode())


def brigade(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = cli.main(list(argv))
    return code, out.getvalue()


@pytest.fixture
def workspace(dot_private_dir) -> Path:
    """A temporary workspace that selects the claude and codex writer harnesses, like a control host."""
    root = dot_private_dir.parent / "ws"
    (root / "memory" / "cards").mkdir(parents=True)
    write_config(root, Config(version=1, selection=Selection("workspace", ["claude", "codex"], "claude")))
    for directory in (root, root / "memory", root / "memory" / "cards"):
        directory.chmod(0o755)
    return root


def connector(profile: ConnectorProfile, workspace: Path, state_dir: Path) -> tuple[MemoryTools, InProcessBrigade]:
    inbox = workspace / profile.handoff_inbox
    inbox.mkdir(parents=True)
    for directory in (inbox.parent, inbox):
        directory.chmod(0o755)
    runtime = parse_runtime(_document())
    paths = replace(
        runtime.paths,
        workspace=workspace,
        state_dir=state_dir,
        bin=replace(runtime.paths.bin, brigade=BRIGADE),
    )
    runner = InProcessBrigade()
    memory = MemoryTools(replace(runtime, paths=paths), commands=runner, profile=profile)
    memory.clock = lambda: NOW
    return memory, runner


def search(workspace: Path) -> list[dict]:
    code, out = brigade("memory", "search", "--target", str(workspace), "--json", "--", MARKER)
    assert code == 0
    return json.loads(out)["matches"]


def pending(inbox: Path) -> list[str]:
    return sorted(path.name for path in inbox.iterdir() if path.is_file())


@requires_local_state
@pytest.mark.parametrize("profile", [DOT, DUET], ids=lambda profile: profile.pack_id)
def test_connector_handoff_is_linted_ingested_and_searchable(profile, workspace, dot_private_dir):
    memory, runner = connector(profile, workspace, dot_private_dir)
    inbox = workspace / profile.handoff_inbox
    name = f"{NOW.astimezone():%Y-%m-%d-%H%M}-{profile.handoff_prefix}-write-path.md"

    # A handoff the real linter rejects is refused, and nothing reaches the inbox.
    refused = memory.handoff_write("write-path", INVALID)
    assert refused["error"] == "handoff_invalid"
    assert refused["lint_errors"]
    assert pending(inbox) == []

    # A valid handoff lands in the profile's writer inbox, byte for byte.
    written = memory.handoff_write("write-path", HANDOFF)
    assert written["ok"] is True
    assert written["filename"] == name
    assert written["path"] == f"{profile.handoff_inbox.as_posix()}/{name}"
    assert written["action"] == "create-card"
    assert pending(inbox) == [name]
    assert (inbox / name).read_bytes() == HANDOFF.encode()
    # Both writes were judged by `brigade handoff lint` against this workspace, on a private copy.
    assert [argv[1:6] for argv in runner.argvs] == [["handoff", "lint", "--json", "--target", str(workspace)]] * 2
    assert all(Path(argv[6]).parent == dot_private_dir and not Path(argv[6]).exists() for argv in runner.argvs)

    # A second write to the same name never replaces the first, and is refused before it is linted.
    again = memory.handoff_write("write-path", HANDOFF.replace("Proves", "Overwrites"))
    assert again == {"error": "handoff_exists", "filename": name}
    assert (inbox / name).read_bytes() == HANDOFF.encode()
    assert len(runner.argvs) == 2

    # The inbox the connector wrote to is one the workspace's ingest reads.
    assert WRITER_INBOXES[{"dot": "codex", "duet": "claude"}[profile.pack_id]] == profile.handoff_inbox.as_posix()
    card = workspace / "memory" / "cards" / CARD_NAME
    assert not card.exists()
    assert search(workspace) == []

    code, report = brigade("ingest", "--target", str(workspace), "--promote-cards", "--route-documents")
    assert code == 0, report
    assert "Promoted 1" in report and "Failed 0" in report
    assert "Status ok" in report
    assert pending(inbox) == []
    assert (inbox / "processed" / name).read_bytes() == HANDOFF.encode()
    assert MARKER in card.read_text()

    matches = search(workspace)
    assert [match["path"] for match in matches] == [f"memory/cards/{CARD_NAME}"]

    # The connector's own memory_search and memory_card read the card it filed.
    found = memory.memory_search(MARKER, limit=5)
    assert [match["path"] for match in found["matches"]] == [f"memory/cards/{CARD_NAME}"]
    assert MARKER in memory.memory_card(CARD_NAME.removesuffix(".md"))["content"]


@requires_local_state
@pytest.mark.parametrize("profile", [DOT, DUET], ids=lambda profile: profile.pack_id)
def test_connector_without_a_writer_inbox_refuses_instead_of_writing_elsewhere(profile, workspace, dot_private_dir):
    """A workspace that never set up the profile's writer inbox gets a refusal, not a file ingest never reads."""
    memory, runner = connector(profile, workspace, dot_private_dir)
    inbox = workspace / profile.handoff_inbox
    inbox.rmdir()
    with pytest.raises(DotError, match="^handoff_unavailable$"):
        memory.handoff_write("write-path", HANDOFF)
    assert not inbox.exists()
    assert runner.argvs == []
    assert search(workspace) == []
