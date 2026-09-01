# Synthetic rejection fixtures retain private paths to exercise redaction.
# content-guard: allow home-path file
from __future__ import annotations

import argparse
import json
import subprocess
import types

import pytest
from brigade import cli, fleet_hub, worklore_store, worklore_validate
from brigade import worklore_github as gh
from brigade import worklore_github_sync as sync
from brigade.cli import fleet as fleet_cli
from tests.support import PRIVATE_DIRECTORY_MODE, PRIVATE_FILE_MODE, assert_private_mode

PUBLIC_ORG = [{"name": "escoffier-labs", "visibility": "public"}]
EXISTING_LINKED_ITEM = {
    "title": "Existing title",
    "acceptance": ["operator acceptance"],
    "links": [
        {
            "link_type": "github",
            "external_key": "escoffier-labs/brigade#12",
            "source_acceptance": ["existing acceptance"],
            "source_policy": "eligible",
            "url": "https://github.com/escoffier-labs/brigade/issues/12",
            "external_updated_at": "2026-08-28T12:00:00Z",
        }
    ],
}


def test_normalize_open_labeled_issue() -> None:
    observation = gh.normalize_issue(
        {
            "number": 12,
            "title": "Rotate restic password",
            "body": "## Acceptance\n- restic snapshots --latest 1 succeeds\n",
            "labels": [{"name": "burn-queue"}, {"name": "ops"}],
            "state": "OPEN",
            "url": "https://github.com/Escoffier-Labs/brigade/issues/12",
            "updatedAt": "2026-08-29T12:00:00Z",
            "repository": {"nameWithOwner": "Escoffier-Labs/brigade"},
        }
    )
    assert observation["external_key"] == "escoffier-labs/brigade#12"
    assert observation["link_type"] == "github"
    assert observation["title"] == "Rotate restic password"
    assert observation["acceptance"] == ["restic snapshots --latest 1 succeeds"]
    assert observation["source_policy"] == "eligible"
    assert observation["proposed_status"] is None
    assert observation["url"] == "https://github.com/Escoffier-Labs/brigade/issues/12"
    assert observation["external_updated_at"] == "2026-08-29T12:00:00Z"
    assert "body" not in observation
    assert "labels" not in observation
    assert "source_type" not in observation


def test_label_removal_and_close_are_observations_not_transitions() -> None:
    closed = gh.normalize_issue(
        {
            "number": 3,
            "title": "Done work",
            "body": "",
            "labels": [{"name": "burn-queue"}],
            "state": "CLOSED",
            "url": "https://github.com/escoffier-labs/brigade/issues/3",
            "updatedAt": "2026-08-29T12:00:00Z",
            "repository": {"nameWithOwner": "escoffier-labs/brigade"},
        }
    )
    assert closed["source_policy"] == "closed"
    assert closed["proposed_status"] == "completed"
    unlabeled = gh.normalize_issue(
        {
            "number": 4,
            "title": "Still open",
            "body": "",
            "labels": [{"name": "docs"}],
            "state": "OPEN",
            "url": "https://github.com/escoffier-labs/brigade/issues/4",
            "updatedAt": "2026-08-29T12:00:00Z",
            "repository": {"nameWithOwner": "escoffier-labs/brigade"},
        }
    )
    assert unlabeled["source_policy"] == "label-removed"
    assert unlabeled["proposed_status"] is None


def test_normalize_rejects_tokens_home_paths_and_http_urls() -> None:
    base = {
        "number": 1,
        "title": "Bad",
        "body": "",
        "labels": [{"name": "burn-queue"}],
        "state": "OPEN",
        "updatedAt": "2026-08-29T12:00:00Z",
        "repository": {"nameWithOwner": "escoffier-labs/brigade"},
    }
    private_keys = (
        "token",
        "secret",
        "password",
        "authorization",
        "prompt",
        "transcript",
        "receipt",
        "path",
    )
    for key in private_keys:
        with pytest.raises(gh.GitHubObservationError, match="unknown-field"):
            gh.normalize_issue(
                {
                    **base,
                    "url": "https://github.com/escoffier-labs/brigade/issues/1",
                    key: "abc",
                }
            )
    with pytest.raises(gh.GitHubObservationError, match="field-bound"):
        gh.normalize_issue({**base, "url": "http://github.com/escoffier-labs/brigade/issues/1"})
    with pytest.raises(gh.GitHubObservationError, match="private-data"):
        gh.normalize_issue(
            {
                **base,
                "url": "https://github.com/escoffier-labs/brigade/issues/1",
                "body": "see /home/operator/secret",
            }
        )
    with pytest.raises(gh.GitHubObservationError, match="private-data"):
        gh.normalize_issue(
            {
                **base,
                "url": "https://github.com/escoffier-labs/brigade/issues/1",
                "title": r"notes in C:\Users\operator",
            }
        )
    with pytest.raises(gh.GitHubObservationError, match="private-data"):
        gh.normalize_issue(
            {
                **base,
                "url": "https://github.com/escoffier-labs/brigade/issues/1",
                "body": r"see C:\Users\operator\secret",
            }
        )


def _oversized_issue(**overrides: object) -> dict[str, object]:
    body = "## Acceptance\n" + "".join(f"- criterion {index} {'x' * 500}\n" for index in range(25))
    issue: dict[str, object] = {
        "number": 12,
        "title": f"  {'T' * 400}  ",
        "body": body,
        "labels": [{"name": "burn-queue"}],
        "state": "OPEN",
        "url": "https://github.com/escoffier-labs/brigade/issues/12",
        "updatedAt": "2026-08-29T12:00:00Z",
        "repository": {"nameWithOwner": "escoffier-labs/brigade"},
    }
    issue.update(overrides)
    return issue


def test_normalize_bounds_title_and_acceptance_to_the_hub_contract() -> None:
    observation = gh.normalize_issue(_oversized_issue())
    assert observation["title"] == "T" * worklore_validate.TITLE_MAX
    assert len(observation["acceptance"]) == worklore_validate.ACCEPTANCE_MAX_ITEMS
    assert all(item.strip() for item in observation["acceptance"])
    assert all(len(item) <= worklore_validate.ACCEPTANCE_ITEM_MAX for item in observation["acceptance"])


def test_normalize_rejects_control_characters_the_hub_refuses() -> None:
    with pytest.raises(gh.GitHubObservationError, match="field-bound"):
        gh.normalize_issue(_oversized_issue(title="Rotate\x07restic"))


def test_bounded_issue_observation_is_accepted_by_the_real_store(tmp_path) -> None:
    conn = fleet_hub.init_db(tmp_path / "hub.db")

    worklore_store.ensure_schema(conn)
    result = worklore_store.import_batch(
        conn,
        adapter_id="github:escoffier-labs",
        source_type="github",
        idempotency_key="github:escoffier-labs:bounded",
        observations=[gh.normalize_issue(_oversized_issue())],
        actor_id="node-a",
        owner_node="node-a",
    )
    assert result["created"] == 1


def test_load_config_requires_explicit_orgs(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "fleet.toml").write_text('[fleet.worklore.github]\nlabel = "burn-queue"\n')
    monkeypatch.setenv("BRIGADE_HOME", str(home))
    with pytest.raises(sync.GitHubSyncError, match="orgs"):
        sync.load_github_config()


def test_discover_uses_octopool_when_present(monkeypatch) -> None:
    monkeypatch.setattr(sync.shutil, "which", lambda name: "/usr/bin/octopool" if name == "octopool" else None)
    recorded = {}

    def fake_run(argv, **kwargs):
        recorded["argv"] = argv
        recorded["env"] = kwargs["env"]
        return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")

    monkeypatch.setattr(sync.subprocess, "run", fake_run)
    sync.discover_issues(PUBLIC_ORG, label="burn-queue", state="all")
    assert recorded["argv"][:3] == ["octopool", "gh", "search"]
    assert recorded["env"]["OCTOPOOL_NO_FALLBACK"] == "1"
    assert "--token" not in recorded["argv"]
    assert "--state" not in recorded["argv"]
    assert "--limit" in recorded["argv"]
    assert recorded["argv"][recorded["argv"].index("--limit") + 1] == "500"


def test_discover_refuses_a_search_that_hits_the_hard_result_ceiling(monkeypatch) -> None:
    monkeypatch.setattr(sync.shutil, "which", lambda name: None)
    issues = [
        {
            "number": number,
            "title": "Queued work",
            "body": "",
            "labels": [{"name": "burn-queue"}],
            "state": "OPEN",
            "url": f"https://github.com/escoffier-labs/brigade/issues/{number}",
            "updatedAt": "2026-08-29T12:00:00Z",
            "repository": {"nameWithOwner": "escoffier-labs/brigade"},
        }
        for number in range(sync.BATCH_SIZE)
    ]
    monkeypatch.setattr(
        sync.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, stdout=json.dumps(issues), stderr=""),
    )

    with pytest.raises(sync.GitHubSyncError) as excinfo:
        sync.discover_issues(PUBLIC_ORG, label="burn-queue", state="all")

    assert excinfo.value.code == "search-truncated"
    assert str(excinfo.value) == "search-truncated: search result limit reached"


def test_discover_falls_back_to_gh_and_rejects_non_json(monkeypatch) -> None:
    monkeypatch.setattr(sync.shutil, "which", lambda name: "/usr/bin/gh" if name == "gh" else None)
    recorded = {}

    def fake_run(argv, **kwargs):
        recorded["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="not-json", stderr="")

    monkeypatch.setattr(sync.subprocess, "run", fake_run)
    with pytest.raises(sync.GitHubSyncError, match="retryable-adapter-failure") as excinfo:
        sync.discover_issues(PUBLIC_ORG, label="burn-queue", state="all")
    assert recorded["argv"][0] == "gh"
    assert "not-json" not in str(excinfo.value)


def test_discover_rejects_non_array_json(monkeypatch) -> None:
    monkeypatch.setattr(sync.shutil, "which", lambda name: None)

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

    monkeypatch.setattr(sync.subprocess, "run", fake_run)
    with pytest.raises(sync.GitHubSyncError, match="retryable-adapter-failure"):
        sync.discover_issues(PUBLIC_ORG, label="burn-queue", state="all")


def test_discover_subprocess_failure_is_bounded(monkeypatch) -> None:
    monkeypatch.setattr(sync.shutil, "which", lambda name: None)

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv,
            2,
            stdout="token=abc",
            stderr="authorization bearer /home/operator",
        )

    monkeypatch.setattr(sync.subprocess, "run", fake_run)
    with pytest.raises(sync.GitHubSyncError, match="retryable-adapter-failure") as excinfo:
        sync.discover_issues(PUBLIC_ORG, label="burn-queue", state="all")
    message = str(excinfo.value)
    assert "token" not in message
    assert "authorization" not in message
    assert "bearer" not in message
    assert "/home/" not in message
    assert "abc" not in message


def test_private_org_never_uses_octopool(monkeypatch) -> None:
    monkeypatch.setattr(sync.shutil, "which", lambda name: "/usr/bin/octopool")
    recorded = {}

    def fake_run(argv, **kwargs):
        recorded["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")

    monkeypatch.setattr(sync.subprocess, "run", fake_run)
    sync.discover_issues(
        [{"name": "escoffier-labs", "visibility": "private"}],
        label="burn-queue",
        state="all",
    )
    assert recorded["argv"][0] == "gh"


def test_discover_maps_explicit_state_flags(monkeypatch) -> None:
    monkeypatch.setattr(sync.shutil, "which", lambda name: None)
    recorded: list[list[str]] = []

    def fake_run(argv, **kwargs):
        recorded.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")

    monkeypatch.setattr(sync.subprocess, "run", fake_run)
    sync.discover_issues(PUBLIC_ORG, label="burn-queue", state="open")
    sync.discover_issues(PUBLIC_ORG, label="burn-queue", state="closed")
    assert recorded[0][recorded[0].index("--state") + 1] == "open"
    assert recorded[1][recorded[1].index("--state") + 1] == "closed"


def test_discover_rejects_unknown_state(monkeypatch) -> None:
    with pytest.raises(sync.GitHubSyncError, match="state"):
        sync.discover_issues(PUBLIC_ORG, label="burn-queue", state="merged")


def test_reconcile_refreshes_previously_linked_issue_missing_from_label_search(monkeypatch) -> None:
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if "search" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps(
                {
                    "number": 12,
                    "title": "Removed label",
                    "body": "",
                    "labels": [],
                    "state": "OPEN",
                    "url": "https://github.com/escoffier-labs/brigade/issues/12",
                    "updatedAt": "2026-08-29T12:00:00Z",
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(
        sync.shutil,
        "which",
        lambda name: "/usr/bin/octopool" if name == "octopool" else "/usr/bin/gh",
    )
    monkeypatch.setattr(sync.subprocess, "run", fake_run)
    observations = sync.discover_and_reconcile(
        PUBLIC_ORG,
        label="burn-queue",
        state="all",
        existing_items=[
            {
                "title": "Removed label",
                "links": [
                    {
                        "link_type": "github",
                        "external_key": "escoffier-labs/brigade#12",
                        "source_acceptance": [],
                        "source_policy": "eligible",
                        "url": "https://github.com/escoffier-labs/brigade/issues/12",
                        "external_updated_at": "2026-08-28T12:00:00Z",
                    }
                ],
            }
        ],
    )
    assert observations[0]["source_policy"] == "label-removed"
    assert observations[0]["external_key"] == "escoffier-labs/brigade#12"
    assert observations[0]["title"] == "Removed label"
    assert "body" not in observations[0]
    assert "labels" not in observations[0]
    view_calls = [argv for argv in calls if "view" in argv]
    assert view_calls
    assert all("repository" not in argv for argv in view_calls)
    assert "--repo" in view_calls[0]
    assert view_calls[0][view_calls[0].index("--repo") + 1] == "escoffier-labs/brigade"


def test_pooled_404_is_inconclusive(monkeypatch) -> None:
    def fake_run(argv, **kwargs):
        if "search" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="HTTP 404")

    monkeypatch.setattr(sync.shutil, "which", lambda name: "/usr/bin/octopool" if name == "octopool" else None)
    monkeypatch.setattr(sync.subprocess, "run", fake_run)
    observations = sync.discover_and_reconcile(
        PUBLIC_ORG,
        label="burn-queue",
        state="all",
        existing_items=[EXISTING_LINKED_ITEM],
    )
    assert observations == []


def test_direct_gh_exact_http_404_emits_stale_tombstone(monkeypatch) -> None:
    def fake_run(argv, **kwargs):
        if "search" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="HTTP 404")

    monkeypatch.setattr(sync.shutil, "which", lambda name: None)
    monkeypatch.setattr(sync.subprocess, "run", fake_run)
    observations = sync.discover_and_reconcile(
        PUBLIC_ORG,
        label="burn-queue",
        state="all",
        existing_items=[EXISTING_LINKED_ITEM],
    )
    assert len(observations) == 1
    observation = observations[0]
    assert observation["stale"] is True
    assert observation["title"] == "Existing title"
    assert observation["acceptance"] == ["existing acceptance"]
    assert observation["source_policy"] == "eligible"
    assert observation["url"] == "https://github.com/escoffier-labs/brigade/issues/12"
    assert observation["external_updated_at"] == "2026-08-28T12:00:00Z"
    assert observation["acceptance"] != EXISTING_LINKED_ITEM["acceptance"]


@pytest.mark.parametrize(
    ("stdout", "stderr"),
    [
        ("", "temporary adapter failure for project404"),
        ("upstream error message mentions 404", ""),
    ],
)
def test_direct_gh_incidental_404_text_is_retryable(monkeypatch, stdout: str, stderr: str) -> None:
    def fake_run(argv, **kwargs):
        if "search" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(sync.shutil, "which", lambda name: None)
    monkeypatch.setattr(sync.subprocess, "run", fake_run)
    with pytest.raises(sync.GitHubSyncError, match="retryable-adapter-failure"):
        sync.discover_and_reconcile(
            PUBLIC_ORG,
            label="burn-queue",
            state="all",
            existing_items=[EXISTING_LINKED_ITEM],
        )


def test_http_404_classifier_accepts_exact_signal_or_structured_status() -> None:
    assert sync._is_http_404(subprocess.CompletedProcess([], 1, stdout="", stderr="HTTP 404"))
    assert sync._is_http_404(subprocess.CompletedProcess([], 1, stdout='{"status": 404}', stderr=""))


def test_reconcile_skips_existing_link_outside_configured_orgs(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")

    monkeypatch.setattr(sync.shutil, "which", lambda name: None)
    monkeypatch.setattr(sync.subprocess, "run", fake_run)
    observations = sync.discover_and_reconcile(
        PUBLIC_ORG,
        label="burn-queue",
        state="all",
        existing_items=[
            {
                "title": "Foreign linked",
                "links": [
                    {
                        "link_type": "github",
                        "external_key": "another-org/repo#9",
                        "source_acceptance": [],
                        "source_policy": "eligible",
                        "url": "https://github.com/another-org/repo/issues/9",
                    }
                ],
            }
        ],
    )
    assert observations == []
    assert len(calls) == 1
    assert "search" in calls[0]
    assert "view" not in calls[0]
    assert calls[0][calls[0].index("--owner") + 1] == "escoffier-labs"


def test_discover_rejects_foreign_owner_from_org_search(monkeypatch) -> None:
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps(
                [
                    {
                        "number": 1,
                        "title": "Foreign",
                        "body": "",
                        "labels": [{"name": "burn-queue"}],
                        "state": "OPEN",
                        "url": "https://github.com/another-org/repo/issues/1",
                        "updatedAt": "2026-08-29T12:00:00Z",
                        "repository": {"nameWithOwner": "another-org/repo"},
                    }
                ]
            ),
            stderr="search stderr leaked",
        )

    monkeypatch.setattr(sync.shutil, "which", lambda name: None)
    monkeypatch.setattr(sync.subprocess, "run", fake_run)
    with pytest.raises(sync.GitHubSyncError, match="retryable-adapter-failure") as excinfo:
        sync.discover_issues(PUBLIC_ORG, label="burn-queue", state="all")
    message = str(excinfo.value)
    assert "another-org" not in message
    assert "payload" not in message
    assert "https://" not in message
    assert "github.com" not in message
    assert "stdout" not in message
    assert "stderr" not in message
    assert "search stderr leaked" not in message


def _linked_item(link: dict[str, object], *, title: str = "Existing title") -> dict[str, object]:
    return {"title": title, "links": [link]}


def test_tombstone_skips_and_counts_mismatched_or_non_https_url(monkeypatch) -> None:
    def fake_run(argv, **kwargs):
        if "search" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="HTTP 404")

    monkeypatch.setattr(sync.shutil, "which", lambda name: None)
    monkeypatch.setattr(sync.subprocess, "run", fake_run)
    mismatched = _linked_item(
        {
            "link_type": "github",
            "external_key": "escoffier-labs/brigade#12",
            "source_acceptance": ["existing acceptance"],
            "source_policy": "eligible",
            "url": "https://github.com/other-org/other-repo/issues/12",
        }
    )
    http_url = _linked_item(
        {
            "link_type": "github",
            "external_key": "escoffier-labs/brigade#13",
            "source_acceptance": ["existing acceptance"],
            "source_policy": "eligible",
            "url": "http://github.com/escoffier-labs/brigade/issues/13",
        }
    )
    observations = sync.discover_and_reconcile(
        PUBLIC_ORG,
        label="burn-queue",
        state="all",
        existing_items=[mismatched, http_url],
    )
    assert observations == []
    assert observations.skipped == 2


def test_tombstone_bounds_title_and_acceptance_to_the_hub_contract(monkeypatch) -> None:
    def fake_run(argv, **kwargs):
        if "search" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="HTTP 404")

    monkeypatch.setattr(sync.shutil, "which", lambda name: None)
    monkeypatch.setattr(sync.subprocess, "run", fake_run)
    observations = sync.discover_and_reconcile(
        PUBLIC_ORG,
        label="burn-queue",
        state="all",
        existing_items=[
            _linked_item(
                {
                    "link_type": "github",
                    "external_key": "escoffier-labs/brigade#12",
                    "source_acceptance": [f"criterion {index} {'x' * 500}" for index in range(25)],
                    "source_policy": "eligible",
                    "url": "https://github.com/escoffier-labs/brigade/issues/12",
                },
                title="T" * 400,
            )
        ],
    )
    tombstone = observations[0]
    assert tombstone["stale"] is True
    assert tombstone["title"] == "T" * worklore_validate.TITLE_MAX
    assert len(tombstone["acceptance"]) == worklore_validate.ACCEPTANCE_MAX_ITEMS
    assert all(len(item) <= worklore_validate.ACCEPTANCE_ITEM_MAX for item in tombstone["acceptance"])


def test_malformed_existing_github_links_are_skipped_and_counted(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")

    monkeypatch.setattr(sync.shutil, "which", lambda name: None)
    monkeypatch.setattr(sync.subprocess, "run", fake_run)
    observations = sync.discover_and_reconcile(
        PUBLIC_ORG,
        label="burn-queue",
        state="all",
        existing_items=[
            "not-an-item",
            {"title": "Bad links", "links": "not-a-list"},
            {"title": "Bad link", "links": ["not-a-link"]},
            {"title": "Bad key", "links": [{"link_type": "github", "external_key": "escoffier-labs/brigade"}]},
            {"title": "Bad number", "links": [{"link_type": "github", "external_key": "escoffier-labs/brigade#0"}]},
            {"title": "Other source", "links": [{"link_type": "brigade", "external_key": "not-parsed"}]},
        ],
    )
    assert observations == []
    assert observations.skipped == 5
    assert all("view" not in argv for argv in calls)


def test_reconcile_pages_a_truncated_link_projection_before_refreshing(monkeypatch) -> None:
    existing = {"work_id": "wl-hidden-link", "links": [], "links_truncated": True}
    hidden_link = {
        "link_type": "github",
        "external_key": "escoffier-labs/brigade#12",
        "source_acceptance": ["existing acceptance"],
        "source_policy": "eligible",
        "url": "https://github.com/escoffier-labs/brigade/issues/12",
    }
    calls: list[object] = []
    monkeypatch.setattr(sync, "discover_issues", lambda *args, **kwargs: sync._DiscoveryResult())
    monkeypatch.setattr(
        sync.worklore_client, "list_item_links_all", lambda work_id: calls.append(work_id) or [hidden_link]
    )
    monkeypatch.setattr(sync, "_refresh_missing_link", lambda **kwargs: calls.append(kwargs) or dict(SYNC_OBSERVATION))

    observations = sync.discover_and_reconcile(PUBLIC_ORG, label="burn-queue", state="all", existing_items=[existing])

    assert calls[0] == "wl-hidden-link"
    assert calls[1]["number"] == 12
    assert [item["external_key"] for item in observations] == [SYNC_OBSERVATION["external_key"]]


def test_malformed_link_does_not_block_healthy_issues(tmp_path, monkeypatch) -> None:
    healthy = {
        "number": 7,
        "title": "Healthy",
        "body": "",
        "labels": [{"name": "burn-queue"}],
        "state": "OPEN",
        "url": "https://github.com/escoffier-labs/brigade/issues/7",
        "updatedAt": "2026-08-29T12:00:00Z",
        "repository": {"nameWithOwner": "escoffier-labs/brigade"},
    }
    posts: list[dict[str, object]] = []
    monkeypatch.setattr(sync.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        sync.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, stdout=json.dumps([healthy]), stderr=""),
    )
    monkeypatch.setattr(
        sync.worklore_client,
        "list_items",
        lambda source=None, **_page: {
            "items": [{"title": "Bad key", "links": [{"link_type": "github", "external_key": 7}]}]
        },
    )
    monkeypatch.setattr(sync.worklore_client, "import_batch", lambda payload: posts.append(payload) or {})
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    result = sync.sync_github(orgs=PUBLIC_ORG)
    assert result["skipped"] == 1
    assert [obs["external_key"] for obs in posts[0]["observations"]] == ["escoffier-labs/brigade#7"]
    receipt = json.loads((tmp_path / "worklore" / "github" / "github-escoffier-labs.json").read_text())
    assert receipt["skipped"] == 1


SYNC_OBSERVATION = {
    "external_key": "escoffier-labs/brigade#12",
    "link_type": "github",
    "title": "Rotate restic password",
    "acceptance": ["restic snapshots --latest 1 succeeds"],
    "source_policy": "eligible",
    "proposed_status": None,
    "url": "https://github.com/escoffier-labs/brigade/issues/12",
    "external_updated_at": "2026-08-29T12:00:00Z",
}


def _observation(*, number: int, org: str = "escoffier-labs", title: str | None = None) -> dict[str, object]:
    owner = org.lower()
    return {
        "external_key": f"{owner}/brigade#{number}",
        "link_type": "github",
        "title": title or f"Issue {number}",
        "acceptance": [f"check {number}"],
        "source_policy": "eligible",
        "proposed_status": None,
        "url": f"https://github.com/{owner}/brigade/issues/{number}",
        "external_updated_at": "2026-08-29T12:00:00Z",
    }


def _batch_key(org: str, observations: list[dict[str, object]]) -> str:
    digest = worklore_store.observation_fingerprint(observations)
    return f"github:{org.lower()}:{digest}"


def _assert_no_leak(message: str) -> None:
    lowered = message.lower()
    for fragment in (
        "https://",
        "http://",
        "token",
        "authorization",
        "bearer",
        "rotate restic",
        "/work/imports",
        "payload",
        "exception",
    ):
        assert fragment not in lowered


def test_sync_posts_one_batch_and_skips_duplicate_revision(tmp_path, monkeypatch) -> None:
    posts: list[dict[str, object]] = []
    list_calls: list[str | None] = []
    discover_calls: list[dict[str, object]] = []
    hub_items = [EXISTING_LINKED_ITEM]

    def fake_import_batch(payload):
        posts.append(payload)
        return {"created": 1 if len(posts) == 1 else 0, "updated": 0 if len(posts) == 1 else 1}

    def fake_list_items(source=None, **_page):
        list_calls.append(source)
        return {"items": hub_items}

    def fake_discover(orgs, *, label, state, existing_items, budget=None):
        discover_calls.append({"orgs": orgs, "label": label, "state": state, "existing_items": existing_items})
        return [dict(SYNC_OBSERVATION)]

    monkeypatch.setattr(sync.worklore_client, "import_batch", fake_import_batch)
    monkeypatch.setattr(sync.worklore_client, "list_items", fake_list_items)
    monkeypatch.setattr(sync, "discover_and_reconcile", fake_discover)
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    first = sync.sync_github(orgs=PUBLIC_ORG)
    second = sync.sync_github(orgs=PUBLIC_ORG)
    assert first["posted"] == 1 and second["posted"] == 1
    assert first["observation_count"] == 1
    assert list_calls == ["github", "github"]
    assert discover_calls[0]["orgs"] == [{"name": "escoffier-labs", "visibility": "public"}]
    assert discover_calls[0]["existing_items"] == hub_items
    assert discover_calls[0]["label"] == "burn-queue"
    assert discover_calls[0]["state"] == "all"
    assert posts[0]["adapter_id"] == "github:escoffier-labs"
    assert posts[0]["source_type"] == "github"
    assert set(posts[0]) == {"adapter_id", "source_type", "idempotency_key", "observations"}
    assert posts[0]["idempotency_key"] == posts[1]["idempotency_key"]
    assert posts[0]["idempotency_key"] == _batch_key("escoffier-labs", [SYNC_OBSERVATION])
    assert posts[0]["idempotency_key"].startswith("github:escoffier-labs:")
    receipt_path = tmp_path / "worklore" / "github" / "github-escoffier-labs.json"
    receipt = json.loads(receipt_path.read_text())
    assert set(receipt) == {
        "adapter_id",
        "idempotency_key",
        "observation_count",
        "refused",
        "skipped",
        "last_error_code",
        "updated_at",
    }
    assert receipt["refused"] == 0
    assert receipt["adapter_id"] == "github:escoffier-labs"
    assert receipt["idempotency_key"] == posts[0]["idempotency_key"]
    assert receipt["observation_count"] == 1
    assert receipt["skipped"] == 0
    assert receipt["last_error_code"] is None
    assert "Rotate restic" not in json.dumps(receipt)
    assert_private_mode(receipt_path, PRIVATE_FILE_MODE)
    assert_private_mode(receipt_path.parent, PRIVATE_DIRECTORY_MODE)


def test_empty_discovery_posts_one_empty_batch(tmp_path, monkeypatch) -> None:
    posts: list[dict[str, object]] = []
    monkeypatch.setattr(sync.worklore_client, "import_batch", lambda payload: posts.append(payload) or {})
    monkeypatch.setattr(sync.worklore_client, "list_items", lambda source=None, **_page: {"items": []})
    monkeypatch.setattr(
        sync,
        "discover_and_reconcile",
        lambda orgs, *, label, state, existing_items, budget=None: [],
    )
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    result = sync.sync_github(orgs=PUBLIC_ORG)
    assert result["posted"] == 1
    assert result["observation_count"] == 0
    assert len(posts) == 1
    assert posts[0]["observations"] == []
    assert posts[0]["adapter_id"] == "github:escoffier-labs"
    assert posts[0]["idempotency_key"] == _batch_key("escoffier-labs", [])
    receipt = json.loads((tmp_path / "worklore" / "github" / "github-escoffier-labs.json").read_text())
    assert receipt["observation_count"] == 0


def test_sync_splits_501_observations_into_two_batches(tmp_path, monkeypatch) -> None:
    observations = [_observation(number=index) for index in range(1, 502)]
    posts: list[dict[str, object]] = []
    monkeypatch.setattr(sync.worklore_client, "import_batch", lambda payload: posts.append(payload) or {})
    monkeypatch.setattr(sync.worklore_client, "list_items", lambda source=None, **_page: {"items": []})
    monkeypatch.setattr(
        sync,
        "discover_and_reconcile",
        lambda orgs, *, label, state, existing_items, budget=None: list(observations),
    )
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    result = sync.sync_github(orgs=PUBLIC_ORG)
    assert result["posted"] == 2
    assert result["observation_count"] == 501
    assert [len(post["observations"]) for post in posts] == [500, 1]
    canonical = sorted(observations, key=lambda item: item["external_key"])
    assert posts[0]["observations"] == canonical[:500]
    assert posts[1]["observations"] == canonical[500:]
    assert posts[0]["idempotency_key"] == _batch_key("escoffier-labs", canonical[:500])
    assert posts[1]["idempotency_key"] == _batch_key("escoffier-labs", canonical[500:])
    assert all(len(post["idempotency_key"]) <= 256 for post in posts)
    assert posts[0]["idempotency_key"] != posts[1]["idempotency_key"]


def test_sync_posts_configured_orgs_independently(tmp_path, monkeypatch) -> None:
    orgs = [
        {"name": "escoffier-labs", "visibility": "public"},
        {"name": "Lidless-Labs", "visibility": "public"},
    ]
    posts: list[dict[str, object]] = []
    discover_orgs: list[object] = []
    list_calls: list[str | None] = []
    first_obs = _observation(number=1, org="escoffier-labs", title="First org only")
    second_obs = _observation(number=2, org="Lidless-Labs", title="Second org only")

    def fake_list_items(source=None, **_page):
        list_calls.append(source)
        return {"items": []}

    def fake_discover(discovered, *, label, state, existing_items, budget=None):
        discover_orgs.append(discovered)
        name = discovered[0]["name"].lower()
        if name == "escoffier-labs":
            return [first_obs]
        return [second_obs]

    monkeypatch.setattr(sync.worklore_client, "import_batch", lambda payload: posts.append(payload) or {})
    monkeypatch.setattr(sync.worklore_client, "list_items", fake_list_items)
    monkeypatch.setattr(sync, "discover_and_reconcile", fake_discover)
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    result = sync.sync_github(orgs=orgs)
    assert list_calls == ["github"]
    assert discover_orgs == [
        [{"name": "escoffier-labs", "visibility": "public"}],
        [{"name": "Lidless-Labs", "visibility": "public"}],
    ]
    assert [post["adapter_id"] for post in posts] == ["github:escoffier-labs", "github:lidless-labs"]
    assert posts[0]["observations"] == [first_obs]
    assert posts[1]["observations"] == [second_obs]
    assert first_obs not in posts[1]["observations"]
    assert second_obs not in posts[0]["observations"]
    assert result["posted"] == 2
    first_receipt = tmp_path / "worklore" / "github" / "github-escoffier-labs.json"
    second_receipt = tmp_path / "worklore" / "github" / "github-lidless-labs.json"
    assert json.loads(first_receipt.read_text())["adapter_id"] == "github:escoffier-labs"
    assert json.loads(second_receipt.read_text())["adapter_id"] == "github:lidless-labs"
    assert_private_mode(first_receipt, PRIVATE_FILE_MODE)
    assert_private_mode(second_receipt, PRIVATE_FILE_MODE)
    assert_private_mode(first_receipt.parent, PRIVATE_DIRECTORY_MODE)


def test_multi_org_sync_completes_link_projections_once(tmp_path, monkeypatch) -> None:
    orgs = [
        {"name": "escoffier-labs", "visibility": "public"},
        {"name": "lidless-labs", "visibility": "public"},
    ]
    items = [{"work_id": "wl-1", "links": [], "links_truncated": True}]
    completed = [{"work_id": "wl-1", "links": [], "links_truncated": False}]
    completion_calls: list[object] = []
    discovery_items: list[object] = []

    def complete(existing_items):
        completion_calls.append(existing_items)
        return completed

    def discover(_orgs, *, label, state, existing_items, budget=None):
        discovery_items.append(existing_items)
        return []

    monkeypatch.setattr(sync.worklore_client, "list_items", lambda source=None, **_page: {"items": items})
    monkeypatch.setattr(sync.worklore_client, "import_batch", lambda _payload: {})
    monkeypatch.setattr(sync, "_complete_link_projections", complete)
    monkeypatch.setattr(sync, "discover_and_reconcile", discover)
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))

    sync.sync_github(orgs=orgs)

    assert completion_calls == [items]
    assert discovery_items == [completed, completed]


def test_hub_refusals_do_not_stop_later_github_chunks_or_orgs(tmp_path, monkeypatch) -> None:
    orgs = [
        {"name": "escoffier-labs", "visibility": "public"},
        {"name": "lidless-labs", "visibility": "public"},
    ]
    first = [_observation(number=number, org="escoffier-labs") for number in range(1, 502)]
    second = [_observation(number=1, org="lidless-labs")]
    posts: list[dict[str, object]] = []

    def fake_discover(discovered, **_kwargs):
        return first if discovered[0]["name"] == "escoffier-labs" else second

    def fake_import(payload):
        posts.append(payload)
        return {"refused": 1} if len(posts) == 1 else {"created": len(payload["observations"])}

    monkeypatch.setattr(sync, "discover_and_reconcile", fake_discover)
    monkeypatch.setattr(sync.worklore_client, "list_items", lambda source=None, **_page: {"items": []})
    monkeypatch.setattr(sync.worklore_client, "import_batch", fake_import)
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))

    result = sync.sync_github(orgs=orgs)

    assert [post["adapter_id"] for post in posts] == [
        "github:escoffier-labs",
        "github:escoffier-labs",
        "github:lidless-labs",
    ]
    assert result["posted"] == 3
    assert result["observation_count"] == 502
    assert result["refused"] == 1
    assert result["skipped"] == 0
    first_receipt = json.loads((tmp_path / "worklore" / "github" / "github-escoffier-labs.json").read_text())
    second_receipt = json.loads((tmp_path / "worklore" / "github" / "github-lidless-labs.json").read_text())
    assert (first_receipt["refused"], first_receipt["last_error_code"]) == (1, "hub-partial")
    assert (second_receipt["refused"], second_receipt["last_error_code"]) == (0, None)


def test_sync_sorts_before_fingerprinting_the_full_posted_chunk(tmp_path, monkeypatch) -> None:
    first = _observation(number=1, title="Alpha title must not change the key")
    second = _observation(number=2, title="Beta title must not change the key")
    posts: list[dict[str, object]] = []
    sequence = [[first, second], [second, first]]

    def fake_discover(orgs, *, label, state, existing_items, budget=None):
        return [dict(item) for item in sequence.pop(0)]

    monkeypatch.setattr(sync.worklore_client, "import_batch", lambda payload: posts.append(payload) or {})
    monkeypatch.setattr(sync.worklore_client, "list_items", lambda source=None, **_page: {"items": []})
    monkeypatch.setattr(sync, "discover_and_reconcile", fake_discover)
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    sync.sync_github(orgs=PUBLIC_ORG)
    sync.sync_github(orgs=PUBLIC_ORG)
    canonical = sorted([first, second], key=lambda item: item["external_key"])
    expected = _batch_key("escoffier-labs", canonical)
    assert posts[0]["idempotency_key"] == expected
    assert posts[1]["idempotency_key"] == expected
    extra = dict(first)
    extra["title"] = "Different title changes the posted chunk"
    extra["url"] = "https://github.com/escoffier-labs/brigade/issues/99"
    assert _batch_key("escoffier-labs", [extra, second]) != expected
    assert len(expected) <= 256


def test_sync_canonicalizes_full_chunks_and_real_store_accepts_source_changes(tmp_path, monkeypatch) -> None:
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    from brigade import worklore_store as store

    store.ensure_schema(conn)
    first = [_observation(number=2, title="Second"), _observation(number=1, title="First")]
    changed = [dict(first[0]), dict(first[1])]
    changed[1]["acceptance"] = ["new source acceptance"]
    # A real source stamps a changed issue with a newer updatedAt, and the hub's rollback
    # guard requires exactly that before it applies the change.
    changed[1]["external_updated_at"] = "2026-08-30T12:00:00Z"
    posted: list[dict[str, object]] = []

    def fake_import(payload):
        posted.append(payload)
        return store.import_batch(conn, actor_id="node-a", owner_node="node-a", **payload)

    sequence = [first, list(reversed(changed))]
    monkeypatch.setattr(sync.worklore_client, "import_batch", fake_import)
    monkeypatch.setattr(
        sync.worklore_client, "list_items", lambda source=None, **_page: store.list_items(conn, source=source)
    )
    monkeypatch.setattr(sync, "discover_and_reconcile", lambda *args, **kwargs: sequence.pop(0))
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    sync.sync_github(orgs=PUBLIC_ORG)
    sync.sync_github(orgs=PUBLIC_ORG)
    assert [post["observations"] for post in posted] == [
        sorted(first, key=lambda item: item["external_key"]),
        sorted(changed, key=lambda item: item["external_key"]),
    ]
    assert posted[0]["idempotency_key"] != posted[1]["idempotency_key"]
    imported = store.list_items(conn, source="github")["items"]
    changed_item = next(item for item in imported if item["links"][0]["external_key"] == "escoffier-labs/brigade#1")
    assert changed_item["links"][0]["source_acceptance"] == ["new source acceptance"]


def test_sync_skips_unsafe_provider_body_and_reports_bounded_count(tmp_path, monkeypatch) -> None:
    unsafe = {
        "number": 1,
        "title": "Safe title",
        "body": "see /home/operator/secret",
        "labels": [{"name": "burn-queue"}],
        "state": "OPEN",
        "url": "https://github.com/escoffier-labs/brigade/issues/1",
        "updatedAt": "2026-08-29T12:00:00Z",
        "repository": {"nameWithOwner": "escoffier-labs/brigade"},
    }
    monkeypatch.setattr(sync.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        sync.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, stdout=json.dumps([unsafe]), stderr=""),
    )
    monkeypatch.setattr(sync.worklore_client, "list_items", lambda source=None, **_page: {"items": []})
    posts: list[dict[str, object]] = []
    monkeypatch.setattr(sync.worklore_client, "import_batch", lambda payload: posts.append(payload) or {})
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    result = sync.sync_github(orgs=PUBLIC_ORG)
    assert result["skipped"] == 1 and posts[0]["observations"] == []
    assert "see /home/operator/secret" not in json.dumps(posts)
    receipt = json.loads((tmp_path / "worklore" / "github" / "github-escoffier-labs.json").read_text())
    assert receipt["skipped"] == 1


def test_sync_retry_receipt_includes_skipped_unsafe_provider_issues(tmp_path, monkeypatch) -> None:
    unsafe = {
        "number": 1,
        "title": "Safe title",
        "body": "see /home/operator/secret",
        "labels": [{"name": "burn-queue"}],
        "state": "OPEN",
        "url": "https://github.com/escoffier-labs/brigade/issues/1",
        "updatedAt": "2026-08-29T12:00:00Z",
        "repository": {"nameWithOwner": "escoffier-labs/brigade"},
    }
    monkeypatch.setattr(sync.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        sync.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, stdout=json.dumps([unsafe]), stderr=""),
    )
    monkeypatch.setattr(sync.worklore_client, "list_items", lambda source=None, **_page: {"items": []})
    monkeypatch.setattr(
        sync.worklore_client,
        "import_batch",
        lambda payload: (_ for _ in ()).throw(sync.worklore_client.FleetClientError("hub unavailable")),
    )
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    with pytest.raises(sync.GitHubSyncError, match="hub-unavailable"):
        sync.sync_github(orgs=PUBLIC_ORG)
    receipt = json.loads((tmp_path / "worklore" / "github" / "github-escoffier-labs.json").read_text())
    assert receipt["skipped"] == 1
    assert "see /home/operator/secret" not in json.dumps(receipt)


def test_hub_unavailable_writes_retry_receipt_and_does_not_raise_raw_url(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        sync.worklore_client,
        "import_batch",
        lambda payload: (_ for _ in ()).throw(
            sync.worklore_client.FleetClientError(
                "fleet hub work failed: HTTP 503: https://hub.example/work/imports token=abc title=Rotate restic"
            )
        ),
    )
    monkeypatch.setattr(sync.worklore_client, "list_items", lambda source=None, **_page: {"items": []})
    monkeypatch.setattr(
        sync,
        "discover_and_reconcile",
        lambda orgs, *, label, state, existing_items, budget=None: [dict(SYNC_OBSERVATION)],
    )
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    with pytest.raises(sync.GitHubSyncError, match="hub-unavailable") as excinfo:
        sync.sync_github(orgs=PUBLIC_ORG)
    _assert_no_leak(str(excinfo.value))
    receipt_path = tmp_path / "worklore" / "github" / "github-escoffier-labs.json"
    receipt = json.loads(receipt_path.read_text())
    assert receipt["last_error_code"] == "hub-unavailable"
    assert receipt["adapter_id"] == "github:escoffier-labs"
    assert "Rotate restic" not in json.dumps(receipt)
    assert "https://hub.example" not in json.dumps(receipt)
    assert_private_mode(receipt_path, PRIVATE_FILE_MODE)
    assert_private_mode(receipt_path.parent, PRIVATE_DIRECTORY_MODE)


def test_list_items_fleet_error_is_bounded_and_writes_retry_receipt(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        sync.worklore_client,
        "list_items",
        lambda source=None, **_page: (_ for _ in ()).throw(
            sync.worklore_client.FleetClientError(
                "fleet hub work failed: HTTP 503: https://hub.example/work/items token=abc title=Rotate restic"
            )
        ),
    )
    monkeypatch.setattr(
        sync.worklore_client,
        "import_batch",
        lambda payload: (_ for _ in ()).throw(AssertionError("import_batch must not run")),
    )
    monkeypatch.setattr(
        sync,
        "discover_and_reconcile",
        lambda orgs, *, label, state, existing_items, budget=None: (_ for _ in ()).throw(
            AssertionError("discover must not run")
        ),
    )
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    with pytest.raises(sync.GitHubSyncError, match="hub-unavailable") as excinfo:
        sync.sync_github(orgs=PUBLIC_ORG)
    _assert_no_leak(str(excinfo.value))
    receipt_path = tmp_path / "worklore" / "github" / "github-escoffier-labs.json"
    receipt = json.loads(receipt_path.read_text())
    assert receipt["last_error_code"] == "hub-unavailable"
    assert receipt["adapter_id"] == "github:escoffier-labs"
    assert "Rotate restic" not in json.dumps(receipt)
    assert_private_mode(receipt_path, PRIVATE_FILE_MODE)


HUB_REFUSALS = [
    ("fleet hub work failed: HTTP 400: title must be 1 to 240 characters", "hub-rejected"),
    ("fleet hub work failed: HTTP 404", "hub-rejected"),
    ("fleet hub work failed: HTTP 422", "hub-rejected"),
    ("fleet hub work failed: HTTP 401", "hub-unauthorized"),
    ("fleet hub work failed: HTTP 403: adapter is owned by another node", "hub-unauthorized"),
    ("fleet hub work failed: HTTP 409: idempotency key reused with a different payload", "hub-conflict"),
    ("fleet hub work failed: HTTP 500", "hub-unavailable"),
    ("fleet hub work failed: HTTP 503: https://hub.example/work/imports token=abc", "hub-unavailable"),
    ("fleet hub work failed: invalid JSON", "hub-unavailable"),
    ("hub-unavailable", "hub-unavailable"),
]


@pytest.mark.parametrize(("message", "code"), HUB_REFUSALS)
def test_import_refusals_keep_stable_non_transient_codes(tmp_path, monkeypatch, message: str, code: str) -> None:
    monkeypatch.setattr(
        sync.worklore_client,
        "import_batch",
        lambda payload: (_ for _ in ()).throw(sync.worklore_client.FleetClientError(message)),
    )
    monkeypatch.setattr(sync.worklore_client, "list_items", lambda source=None, **_page: {"items": []})
    monkeypatch.setattr(
        sync,
        "discover_and_reconcile",
        lambda orgs, *, label, state, existing_items, budget=None: [dict(SYNC_OBSERVATION)],
    )
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    with pytest.raises(sync.GitHubSyncError) as excinfo:
        sync.sync_github(orgs=PUBLIC_ORG)
    assert excinfo.value.code == code
    _assert_no_leak(str(excinfo.value))
    receipt = json.loads((tmp_path / "worklore" / "github" / "github-escoffier-labs.json").read_text())
    assert receipt["last_error_code"] == code
    _assert_no_leak(json.dumps(receipt))


@pytest.mark.parametrize(("message", "code"), HUB_REFUSALS)
def test_list_items_refusals_keep_stable_non_transient_codes(tmp_path, monkeypatch, message: str, code: str) -> None:
    monkeypatch.setattr(
        sync.worklore_client,
        "list_items",
        lambda source=None, **_page: (_ for _ in ()).throw(sync.worklore_client.FleetClientError(message)),
    )
    monkeypatch.setattr(
        sync.worklore_client,
        "import_batch",
        lambda payload: (_ for _ in ()).throw(AssertionError("import_batch must not run")),
    )
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    with pytest.raises(sync.GitHubSyncError) as excinfo:
        sync.sync_github(orgs=PUBLIC_ORG)
    assert excinfo.value.code == code
    _assert_no_leak(str(excinfo.value))
    receipt = json.loads((tmp_path / "worklore" / "github" / "github-escoffier-labs.json").read_text())
    assert receipt["last_error_code"] == code


def test_receipt_filename_sanitizes_reserved_characters() -> None:
    raw = 'github:org/name\\x<y>"z|a?b*c'
    safe = sync._safe_adapter_id(raw)
    for character in ':/\\<>"|?*':
        assert character not in safe
    assert safe == "github-org-name-x-y--z-a-b-c"


def test_sync_rejects_case_insensitive_duplicate_orgs_before_hub_or_github(tmp_path, monkeypatch) -> None:
    list_calls: list[str | None] = []
    posts: list[dict[str, object]] = []
    discover_calls: list[object] = []
    github_calls: list[list[str]] = []

    def fake_list_items(source=None, **_page):
        list_calls.append(source)
        return {"items": []}

    def fake_import_batch(payload):
        posts.append(payload)
        return {}

    def fake_discover(orgs, *, label, state, existing_items, budget=None):
        discover_calls.append(orgs)
        return [dict(SYNC_OBSERVATION)]

    def fake_run(argv, **kwargs):
        github_calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")

    monkeypatch.setattr(sync.worklore_client, "list_items", fake_list_items)
    monkeypatch.setattr(sync.worklore_client, "import_batch", fake_import_batch)
    monkeypatch.setattr(sync, "discover_and_reconcile", fake_discover)
    monkeypatch.setattr(sync.subprocess, "run", fake_run)
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    with pytest.raises(sync.GitHubSyncError, match="field-bound") as excinfo:
        sync.sync_github(
            orgs=[
                {"name": "Escoffier-Labs", "visibility": "public"},
                {"name": "escoffier-labs", "visibility": "public"},
            ]
        )
    assert excinfo.value.code == "field-bound"
    _assert_no_leak(str(excinfo.value))
    assert list_calls == []
    assert posts == []
    assert discover_calls == []
    assert github_calls == []


def _leaky_receipt_oserror(tmp_path) -> OSError:
    leaky_path = tmp_path / "home" / "operator" / "worklore" / "github" / "github-escoffier-labs.json"
    return OSError(13, f"Permission denied: '{leaky_path}'")


def test_successful_post_receipt_write_failure_is_bounded_and_replay_safe(tmp_path, monkeypatch) -> None:
    posts: list[dict[str, object]] = []
    expected_key = _batch_key("escoffier-labs", [SYNC_OBSERVATION])
    leak = _leaky_receipt_oserror(tmp_path)

    def fake_import_batch(payload):
        posts.append(payload)
        return {"created": 1, "updated": 0}

    def failing_write(*args, **kwargs):
        raise leak

    monkeypatch.setattr(sync.worklore_client, "import_batch", fake_import_batch)
    monkeypatch.setattr(sync.worklore_client, "list_items", lambda source=None, **_page: {"items": []})
    monkeypatch.setattr(
        sync,
        "discover_and_reconcile",
        lambda orgs, *, label, state, existing_items, budget=None: [dict(SYNC_OBSERVATION)],
    )
    monkeypatch.setattr(sync, "_write_receipt", failing_write)
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    with pytest.raises(sync.GitHubSyncError, match="receipt-write-failed") as excinfo:
        sync.sync_github(orgs=PUBLIC_ORG)
    assert excinfo.value.code == "receipt-write-failed"
    assert str(excinfo.value) == "receipt-write-failed: receipt-write-failed"
    message = str(excinfo.value)
    _assert_no_leak(message)
    assert str(tmp_path) not in message
    assert "/home/" not in message
    assert "Permission denied" not in message
    assert "github-escoffier-labs.json" not in message
    assert leak.strerror not in message
    assert len(posts) == 1
    assert posts[0]["idempotency_key"] == expected_key


def test_hub_import_refusal_is_reported_as_a_partial_failure(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(sync.worklore_client, "import_batch", lambda payload: {"created": 1, "refused": 1})
    monkeypatch.setattr(sync.worklore_client, "list_items", lambda source=None, **_page: {"items": []})
    monkeypatch.setattr(
        sync,
        "discover_and_reconcile",
        lambda orgs, *, label, state, existing_items, budget=None: [dict(SYNC_OBSERVATION)],
    )
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))

    result = sync.sync_github(orgs=PUBLIC_ORG)

    assert result["refused"] == 1
    receipt = json.loads((tmp_path / "worklore" / "github" / "github-escoffier-labs.json").read_text())
    assert receipt["last_error_code"] == "hub-partial"
    assert receipt["refused"] == 1


def test_hub_unavailable_receipt_write_failure_stays_hub_unavailable(tmp_path, monkeypatch) -> None:
    leak = _leaky_receipt_oserror(tmp_path)

    def failing_write(*args, **kwargs):
        raise leak

    monkeypatch.setattr(
        sync.worklore_client,
        "import_batch",
        lambda payload: (_ for _ in ()).throw(
            sync.worklore_client.FleetClientError(
                "fleet hub work failed: HTTP 503: https://hub.example/work/imports token=abc title=Rotate restic"
            )
        ),
    )
    monkeypatch.setattr(sync.worklore_client, "list_items", lambda source=None, **_page: {"items": []})
    monkeypatch.setattr(
        sync,
        "discover_and_reconcile",
        lambda orgs, *, label, state, existing_items, budget=None: [dict(SYNC_OBSERVATION)],
    )
    monkeypatch.setattr(sync, "_write_receipt", failing_write)
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    with pytest.raises(sync.GitHubSyncError, match="hub-unavailable") as excinfo:
        sync.sync_github(orgs=PUBLIC_ORG)
    assert excinfo.value.code == "hub-unavailable"
    message = str(excinfo.value)
    _assert_no_leak(message)
    assert str(tmp_path) not in message
    assert "/home/" not in message
    assert "Permission denied" not in message
    assert "github-escoffier-labs.json" not in message
    assert leak.strerror not in message
    assert "OSError" not in message


CLI_CONFIG = {
    "orgs": [{"name": "escoffier-labs", "visibility": "public"}],
    "label": "burn-queue",
    "state": "all",
}


def test_sync_github_cli_json(monkeypatch, capsys) -> None:
    captured: dict[str, object] = {}

    def fake_sync(orgs=None, *, label, state):
        captured["orgs"] = orgs
        captured["label"] = label
        captured["state"] = state
        return {
            "posted": 1,
            "observation_count": 3,
            "title": "Rotate restic password",
            "url": "https://github.com/escoffier-labs/brigade/issues/12",
            "token": "ghp_secret",
            "observations": [{"title": "Rotate restic password"}],
        }

    monkeypatch.setattr(fleet_cli.worklore_github_sync, "load_github_config", lambda: CLI_CONFIG)
    monkeypatch.setattr(fleet_cli.worklore_github_sync, "sync_github", fake_sync)
    assert fleet_cli._dispatch_work_sync_github(argparse.Namespace(json=True)) == 0
    captured_io = capsys.readouterr()
    assert captured_io.err == ""
    payload = json.loads(captured_io.out)
    assert payload == json.loads(json.dumps(payload, sort_keys=True))
    assert captured_io.out == json.dumps(payload, indent=2, sort_keys=True) + "\n"
    assert payload["orgs"] == ["escoffier-labs"]
    assert payload["posted"] == 1
    assert payload["observation_count"] == 3
    assert payload["refused"] == payload["skipped"] == 0
    assert captured["orgs"] == CLI_CONFIG["orgs"]
    assert captured["label"] == "burn-queue"
    assert captured["state"] == "all"
    assert set(payload) == {"orgs", "posted", "observation_count", "refused", "skipped"}
    _assert_no_leak(captured_io.out)
    assert "ghp_secret" not in captured_io.out
    assert "Rotate restic" not in captured_io.out


def test_sync_github_cli_default_reports_counts_only(monkeypatch, capsys) -> None:
    monkeypatch.setattr(fleet_cli.worklore_github_sync, "load_github_config", lambda: CLI_CONFIG)
    monkeypatch.setattr(
        fleet_cli.worklore_github_sync,
        "sync_github",
        lambda orgs=None, *, label, state: {
            "posted": 2,
            "observation_count": 7,
            "title": "Rotate restic password",
            "observations": [{"title": "Rotate restic password"}],
        },
    )
    assert fleet_cli._dispatch_work_sync_github(argparse.Namespace(json=False)) == 0
    captured_io = capsys.readouterr()
    assert captured_io.err == ""
    lines = [line for line in captured_io.out.splitlines() if line.strip()]
    assert len(lines) == 1
    line = lines[0]
    assert "2" in line
    assert "7" in line
    assert "posted" in line.lower()
    assert "observation" in line.lower()
    assert "Rotate restic" not in captured_io.out
    assert "title" not in line.lower()
    _assert_no_leak(captured_io.out)


def test_sync_github_cli_surfaces_refusals_after_printing_counts(monkeypatch, capsys) -> None:
    monkeypatch.setattr(fleet_cli.worklore_github_sync, "load_github_config", lambda: CLI_CONFIG)
    monkeypatch.setattr(
        fleet_cli.worklore_github_sync,
        "sync_github",
        lambda orgs=None, *, label, state: {"posted": 2, "observation_count": 7, "refused": 1, "skipped": 3},
    )

    assert fleet_cli._dispatch_work_sync_github(argparse.Namespace(json=True)) == 1
    assert json.loads(capsys.readouterr().out) == {
        "observation_count": 7,
        "orgs": ["escoffier-labs"],
        "posted": 2,
        "refused": 1,
        "skipped": 3,
    }
    assert fleet_cli._dispatch_work_sync_github(argparse.Namespace(json=False)) == 1
    assert capsys.readouterr().out == "posted 2 batch(es), 7 observation(s), refused 1, skipped 3\n"


@pytest.mark.parametrize(
    "message",
    (
        "GitHub Worklore config is missing orgs",
        "orgs must be a non-empty list",
    ),
)
def test_sync_github_cli_config_error_returns_2(monkeypatch, capsys, message: str) -> None:
    def missing_config() -> dict[str, object]:
        raise sync.GitHubSyncError(message, code="field-bound")

    monkeypatch.setattr(fleet_cli.worklore_github_sync, "load_github_config", missing_config)
    assert fleet_cli._dispatch_work_sync_github(argparse.Namespace(json=False)) == 2
    captured_io = capsys.readouterr()
    assert captured_io.out == ""
    err_lines = [line for line in captured_io.err.splitlines() if line.strip()]
    assert err_lines == [f"field-bound: {message}"]
    _assert_no_leak(captured_io.err)


@pytest.mark.parametrize(
    ("code", "message"),
    (
        ("hub-unavailable", "hub-unavailable"),
        ("hub-rejected", "hub-rejected"),
        ("hub-unauthorized", "hub-unauthorized"),
        ("hub-conflict", "hub-conflict"),
        ("retryable-adapter-failure", "adapter command failed"),
        ("receipt-write-failed", "receipt-write-failed"),
    ),
)
def test_sync_github_cli_adapter_failures_return_1(monkeypatch, capsys, code: str, message: str) -> None:
    monkeypatch.setattr(fleet_cli.worklore_github_sync, "load_github_config", lambda: CLI_CONFIG)

    def boom(orgs=None, *, label, state):
        raise sync.GitHubSyncError(message, code=code)

    monkeypatch.setattr(fleet_cli.worklore_github_sync, "sync_github", boom)
    assert fleet_cli._dispatch_work_sync_github(argparse.Namespace(json=False)) == 1
    captured_io = capsys.readouterr()
    assert captured_io.out == ""
    err_lines = [line for line in captured_io.err.splitlines() if line.strip()]
    assert err_lines == [f"{code}: {message}"]
    _assert_no_leak(captured_io.err)
    assert "/home/" not in captured_io.err
    assert "Rotate restic" not in captured_io.err


def test_fleet_work_parser_accepts_sync_github() -> None:
    parser = cli._build_parser()
    parsed = parser.parse_args(["fleet", "work", "sync-github", "--json"])
    assert parsed.func is fleet_cli._dispatch_work_sync_github
    assert parsed.json is True
    default = parser.parse_args(["fleet", "work", "sync-github"])
    assert default.func is fleet_cli._dispatch_work_sync_github
    assert default.json is False


def _stale_link(number):
    return _linked_item(
        {
            "link_type": "github",
            "external_key": f"escoffier-labs/brigade#{number}",
            "source_acceptance": ["existing acceptance"],
            "source_policy": "eligible",
            "url": f"https://github.com/escoffier-labs/brigade/issues/{number}",
        }
    )


def _reconcile_views(monkeypatch, views):
    def fake_run(argv, **kwargs):
        if "search" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")
        views.append(argv)
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="HTTP 404")

    monkeypatch.setattr(sync.shutil, "which", lambda name: None)
    monkeypatch.setattr(sync.subprocess, "run", fake_run)


def test_reconcile_refreshes_stop_at_the_run_refresh_ceiling(monkeypatch) -> None:
    views: list[list[str]] = []
    _reconcile_views(monkeypatch, views)
    monkeypatch.setattr(sync, "RECONCILE_MAX_REFRESHES", 2)
    observations = sync.discover_and_reconcile(
        PUBLIC_ORG,
        label="burn-queue",
        state="all",
        existing_items=[_stale_link(number) for number in range(10, 20)],
    )
    assert len(views) == 2
    assert len(observations) == 2
    assert observations.skipped == 8


def test_reconcile_refreshes_stop_when_the_wall_clock_budget_is_spent(monkeypatch) -> None:
    views: list[list[str]] = []
    _reconcile_views(monkeypatch, views)
    clock = iter([0.0, 0.0, 5.0])
    monkeypatch.setattr(sync, "time", types.SimpleNamespace(monotonic=lambda: next(clock, 5.0)))
    monkeypatch.setattr(sync, "RECONCILE_BUDGET_SECONDS", 1.0)
    observations = sync.discover_and_reconcile(
        PUBLIC_ORG,
        label="burn-queue",
        state="all",
        existing_items=[_stale_link(number) for number in range(10, 15)],
    )
    assert len(views) == 1
    assert observations.skipped == 4


def test_one_refresh_budget_spans_every_configured_org(monkeypatch, tmp_path) -> None:
    views: list[list[str]] = []
    _reconcile_views(monkeypatch, views)
    monkeypatch.setattr(sync, "RECONCILE_MAX_REFRESHES", 1)
    monkeypatch.setattr(sync.worklore_client, "import_batch", lambda payload: {})
    monkeypatch.setattr(
        sync.worklore_client,
        "list_items",
        lambda source=None, **_page: {"items": [_stale_link(10), _stale_link(11)]},
    )
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    sync.sync_github(
        orgs=[
            {"name": "escoffier-labs", "visibility": "public"},
            {"name": "lidless-labs", "visibility": "public"},
        ]
    )
    assert len(views) == 1
