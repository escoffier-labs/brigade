"""Production task recall and deterministic linked-worktree retrieval evidence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brigade import cli


@pytest.mark.parametrize("missing", [False, True])
def test_production_recall_accepts_task_query_without_redirecting_target(tmp_path: Path, capsys, missing: bool):
    hub = tmp_path / "hub"
    cwd = tmp_path / "generated-task-1563"
    cwd.mkdir()
    # A tempting cwd card must never redirect recall away from the supplied hub.
    for root, title in ((cwd, "Comet outside target"), (hub, "Comet task notes")):
        if root == hub and missing:
            continue
        cards = root / "memory" / "cards"
        cards.mkdir(parents=True)
        (cards / "comet.md").write_text(f"---\ntitle: {title}\n---\nPRIVATE_BODY\n")
    assert (
        cli.main(
            [
                "memory",
                "recall",
                "--target",
                str(hub),
                "--cwd",
                str(cwd),
                "--query",
                "\x00comet\n",
                "--limit",
                "99",
                "--json",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["query"] == "comet"
    assert payload["query_source"] == "explicit"
    assert payload["target"] == str(hub)
    assert payload["limit"] == 5
    assert payload["status"] == ("missing-target" if missing else "ok")
    assert [match["title"] for match in payload["matches"]] == ([] if missing else ["Comet task notes"])
    assert "PRIVATE_BODY" not in output


def test_task_worktree_fixture_matches_real_recall_and_existing_eval(tmp_path: Path, capsys):
    import os
    import shutil
    import subprocess

    from brigade import memory_hooks
    from brigade.memory_retrieval_eval.harness import run_eval
    from brigade.memory_retrieval_eval.metrics import aggregate_scores
    from brigade.memory_retrieval_eval.quality import evaluate_memory_quality

    root = Path(__file__).resolve().parents[1] / "evals/memory-retrieval/task-recall"
    cases = json.loads((root / "contexts.json").read_text())
    report = run_eval(fixture_root=root, adapters=["current", "grep"])
    rows = {row["id"]: row for row in report["adapters"]["current"]["queries"]}
    repo = tmp_path / "orbit-kit"
    # Isolate fixture setup from global config, templates, hooks and signing.
    empty = tmp_path / "empty-git-config"
    empty.mkdir()
    git_env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    git_env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
    git_argv = ["git", "-c", f"core.hooksPath={empty}", "-c", "commit.gpgSign=false", "-c", f"init.templateDir={empty}"]
    # Real Git-common-directory identity, with distinct linked worktrees.
    for argv in (
        ["init", "-q", str(repo)],
        [
            "-C",
            str(repo),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "fixture",
        ],
    ):
        subprocess.run([*git_argv, *argv], env=git_env, check=True, capture_output=True)
    retrievals = {"basename": [], "candidate": []}
    for case in cases:
        cwd = tmp_path / case["cwd"]
        if case["repo"]:
            subprocess.run(
                [*git_argv, "-C", str(repo), "worktree", "add", "-q", "--detach", str(cwd)],
                env=git_env,
                check=True,
                capture_output=True,
            )
        else:
            cwd.mkdir()
        baseline_query = memory_hooks.query_from_cwd(cwd, memory_root=root)
        assert baseline_query == rows["basename:" + case["id"]]["query"]
        assert (
            cli.main(
                [
                    "memory",
                    "recall",
                    "--target",
                    str(root),
                    "--cwd",
                    str(cwd),
                    "--json",
                    *(["--query", case["task"]] if case["task"] is not None else []),
                ]
            )
            == 0
        )
        payload = json.loads(capsys.readouterr().out)
        candidate = rows["candidate:" + case["id"]]
        assert payload["query"] == candidate["query"]
        assert [match["card_id"] for match in payload["matches"]] == candidate["ranked"]
        assert payload["target"] == str(root)
        for mode in retrievals:
            retrievals[mode].append(
                {
                    "id": case["id"],
                    "scope": {
                        "repository": case["repo"] or case["cwd"],
                        "task": "token-refresh" if case["gold"] == ["active-token"] else None,
                    },
                    "results": rows[mode + ":" + case["id"]]["ranked"],
                }
            )
    summary = {}
    quality_root = tmp_path / "quality"
    shutil.copytree(root / "memory", quality_root / "memory")
    for mode in retrievals:
        summary[mode] = aggregate_scores([row for key, row in rows.items() if key.startswith(mode + ":")])
        (quality_root / "eval.json").write_text(
            json.dumps(
                {
                    "schema": "memory-retrieval-quality-input.v1",
                    "as_of": "2026-10-13",
                    "retrievals": retrievals[mode],
                }
            )
        )
        summary[mode]["quality"] = evaluate_memory_quality(quality_root)
    # Print measurement before assertions so unexpected fixture results stay inspectable.
    print(json.dumps(summary, sort_keys=True))
    assert summary["basename"]["recall_at_k"] == 1 / 7
    assert summary["candidate"]["recall_at_k"] == 6 / 7
    assert summary["basename"]["quality"]["stale_recall"]["violation_count"] == 1
    assert summary["candidate"]["quality"]["stale_recall"]["violation_count"] == 6
    assert summary["basename"]["quality"]["scope_leakage"]["violation_count"] == 4
    assert summary["candidate"]["quality"]["scope_leakage"]["violation_count"] == 15
    assert rows["candidate:vague-task"]["ranked"] == []
    # This is fixture relevance evidence, never a manifest-backed verification score.
