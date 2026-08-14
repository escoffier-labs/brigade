"""Reusable projection conformance fixture.

Production projectors import this module to share the mixed create/replace/remove
workspace and injected-failure boundaries. It does not import command modules.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from brigade.projection import kernel


def mixed_workspace(root: Path) -> tuple[Path, kernel.Plan]:
    """Build one plan that creates, replaces, and removes files."""
    workspace = root / "fixture-ws" if root.name != "fixture-ws" else root
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "b-replace.txt").write_bytes(b"old\n")
    (workspace / "c-remove.txt").write_bytes(b"gone-soon\n")
    created = b"created\n"
    replacement = b"new\n"
    plan = kernel.build_plan(
        operation_id="fixture-mixed",
        projector="conformance-fixture",
        source_fingerprint=kernel.content_digest(b"canonical-source"),
        mutations=[
            kernel.mutation(
                destination=workspace / "a-create.txt",
                mutation="create",
                expected_before=kernel.ABSENT,
                desired_after=kernel.content_digest(created),
                staged_bytes=created,
            ),
            kernel.mutation(
                destination=workspace / "b-replace.txt",
                mutation="replace",
                expected_before=kernel.content_digest(b"old\n"),
                desired_after=kernel.content_digest(replacement),
                staged_bytes=replacement,
            ),
            kernel.mutation(
                destination=workspace / "c-remove.txt",
                mutation="remove",
                expected_before=kernel.content_digest(b"gone-soon\n"),
                desired_after=kernel.ABSENT,
            ),
        ],
        target=workspace,
    )
    return workspace, plan


def inject_boundaries() -> list[str]:
    return [
        "commit:0:before",
        "commit:0:after",
        "commit:1:before",
        "commit:1:after",
        "commit:2:before",
        "commit:2:after",
    ]


def run_conformance(root: Path) -> dict[str, Any]:
    """Run the shared kernel checks. Returns a report dict for callers."""
    commit_root = root / "commit"
    commit_root.mkdir()
    workspace, plan = mixed_workspace(commit_root)
    committed = kernel.execute(plan, target=workspace)
    inject_root = root / "inject"
    inject_root.mkdir()
    inject_workspace, inject_plan = mixed_workspace(inject_root)
    restored = kernel.execute(
        inject_plan,
        target=inject_workspace,
        inject=kernel.FailureInjector(boundary="commit:1:after"),
    )
    return {
        "committed": committed.terminal_state == "committed",
        "restored_after_inject": restored.terminal_state == "restored",
        "boundaries": inject_boundaries(),
    }
