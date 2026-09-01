# Worklore GitHub adapter implementation plan

## Goal

Synchronize configured `burn-queue` GitHub issues into Worklore through
`POST /work/imports` so the same issue observed twice creates one item,
one link, and no duplicate observation event for an identical source
revision. Execute the tasks in order and check every box.

Approved design: `docs/phase-worklore-ledger.md`.

This plan is independently executable for normalize and discover. Tasks 3
and 4 import `worklore_client.import_batch` and
`worklore_client.list_items` from the core slice. Do not add those
functions here. Start Task 3 only after core Task 5 is on the branch.
Tests use a fake `import_batch` and in-memory observation fixtures. They
do not edit Fleet Hub tables, the Brigade adapter, or Ops Deck.

## Architecture

- `worklore_github.py` normalizes one issue into an import observation and
  rejects credential-shaped fields, home paths, prompts, and transcripts.
- `worklore_github_sync.py` lists labeled issues from configured
  organizations through the approved GitHub read path, writes a local
  retry receipt, and posts one idempotent batch.
- `worklore_client.import_batch` and `worklore_client.list_items` from
  core Task 5 are the only hub writers and readers.
- GitHub credentials stay on the node. The hub never receives a token.

## File map

| File | Responsibility |
| --- | --- |
| Create `src/brigade/worklore_github.py` | Issue to observation mapping and source-policy rules. |
| Create `src/brigade/worklore_github_sync.py` | Org discovery, batching, retry receipt. |
| Create `tests/test_worklore_github.py` | Normalizer, privacy, discovery, and fake-hub tests. |
| Modify `src/brigade/cli/fleet.py` | `brigade fleet work sync-github`. |
| Modify `src/brigade/work_cmd/ledger/import_model.py` | Public alias `extract_issue_acceptance = _extract_issue_acceptance`. |

Do not modify `worklore_client.py`.

## Pinned decisions

- External key is `owner/repository#number` with the repository name
  lowercased and without `.git`.
- First observation creates a Worklore item with default scheduling
  (`burn_eligible=false`, `burn_rank=1000`, `token_appetite=medium`,
  `execution_mode=manual`, `status=captured`). Later observations update
  only GitHub-authoritative fields: title, source acceptance, open or
  closed state, URL, source timestamp, and `source_policy`. Operator
  `acceptance` is never sent and never overwritten.
- Removing `burn-queue` marks `source_policy=label-removed`. The adapter
  still sends the observation. It does not delete the item or transition
  it. Burn eligibility on the hub treats any non-eligible link as
  excluded (`exclusions["source-policy"]`).
- Closing an issue sends `source_policy=closed` and
  `proposed_status=completed`. The adapter never POSTs a transition.
- Config lives in `~/.brigade/fleet.toml`:

```toml
[fleet.worklore.github]
label = "burn-queue"
state = "all"
orgs = [
  { name = "escoffier-labs", visibility = "public" },
]
```

  Missing config is a CLI error, not a home-directory crawl. `label`
  defaults to `burn-queue`. `state` defaults to `all`, meaning open and
  closed. That config value does not become a `gh` flag.
- Each org requires `visibility` of `public` or `private`.
- Discovery command for a public org, in order: if
  `shutil.which("octopool")` succeeds, run
  `OCTOPOOL_NO_FALLBACK=1 octopool gh search issues --owner {org} --label {label} --limit 500 --json number,title,body,labels,state,url,updatedAt,repository`.
  Otherwise run the same argv with `gh`. Never pass `--state all`.
  Installed `gh search issues --state` accepts only `open` or `closed`
  (default page size 30). Omit `--state` when config `state` is `all`.
  Pass `--state open` or `--state closed` only when config says so.
  Never add `--token`. Never log stdout that contains `token`,
  `authorization`, or `bearer`.
- Private orgs never use octopool. They run `gh search issues` with the
  same flags.
- Label removal is reconciled explicitly because a label-filtered search
  cannot return an issue after the label disappears. Before discovery,
  read existing GitHub items with
  `worklore_client.list_items(source="github")`. Pass the returned `items`
  into reconciliation so each existing GitHub link retains its item title,
  source acceptance, URL, external timestamp, and source policy. For any
  existing external key
  absent from the search result, run
  `octopool gh issue view {number} --repo {owner/repository} --json number,title,body,labels,state,url,updatedAt`
  for a public org when octopool is present, else the equivalent `gh`
  command. `gh issue view --json` has no `repository` field. Synthesize
  `repository.nameWithOwner` from the `--repo` argument. Search results
  do include `repository` and use `repository.nameWithOwner` when
  present.
- A 404 through octopool is inconclusive. Do not set `stale` and do not
  change `source_policy`. A 404 through direct `gh` emits a tombstone
  observation derived from the existing item and link, preserving its title,
  source acceptance, URL, external timestamp, and source policy while setting
  `stale=true`.
- Batch size is at most 500 observations. Larger result sets split into
  consecutive batches. Every posted batch, including an empty one, uses
  the same idempotency-key format
  `github:{org}:{sha256(sorted fingerprints)[:24]}` over the observations
  in that posted batch. Do not use `{adapter_id}:{cursor}:{chunk_index}`.
- Retry receipt path is
  `$BRIGADE_HOME/worklore/github/{safe_adapter_id}.json` with mode 0600.
  `safe_adapter_id` replaces `:`, `/`, `\`, `<`, `>`, `"`, `|`, `?`, and
  `*` with `-`. Adapter id `github:escoffier-labs` becomes
  `github-escoffier-labs.json`. The receipt stores `adapter_id`,
  `idempotency_key`, `observation_count`, `skipped`, `last_error_code`,
  and `updated_at`. It does not store issue bodies or hub payloads.
- Adapter id is `github:{org}`.
- Acceptance criteria come from
  `brigade.work_cmd.ledger.import_model.extract_issue_acceptance`. Add
  that public alias next to `_extract_issue_acceptance` at
  `src/brigade/work_cmd/ledger/import_model.py:628`. Import
  `from brigade.work_cmd.ledger.import_model import extract_issue_acceptance`.
  Do not add the alias to `ledger/__init__.py` or `_OWNERS`. Do not
  import the `_name` across module families. Do not copy a second parser.
- URL must be `https://github.com/{owner}/{repo}/issues/{number}`. Other
  hosts are rejected.
- `discover_issues(orgs, *, label, state)` takes `orgs` as
  `[{"name": "escoffier-labs", "visibility": "public"}]`. Every test
  uses that signature. `sync_github` calls `discover_and_reconcile`, not
  `discover_issues` alone.
- Titles and acceptance are bounded to the hub contract before a batch is
  posted, using `TITLE_MAX`, `ACCEPTANCE_MAX_ITEMS`, and
  `ACCEPTANCE_ITEM_MAX` imported from `worklore_validate` so the two
  cannot drift. A title is stripped and truncated to `TITLE_MAX`;
  acceptance items are truncated to `ACCEPTANCE_ITEM_MAX`, items that are
  blank or carry control characters are dropped, and the list is capped at
  `ACCEPTANCE_MAX_ITEMS`. A title the
  adapter cannot bound, such as one carrying control characters the hub
  refuses, is `field-bound` and skips that issue alone. Tombstones bound
  the existing item title and `source_acceptance` the same way.
- Isolation is per issue and per link. One issue the adapter cannot
  normalize, and one malformed existing github link, are skipped and
  counted in `skipped`; neither aborts the org batch or the sync. A link
  is malformed when the existing item is not an object, its `links` is not
  an array, the link is not an object, or its `external_key` does not
  parse as `owner/repo#number`. Links of another `link_type` are not
  counted as malformed.
- Hub refusals keep a stable code instead of collapsing to
  `hub-unavailable`. `401` and `403` are `hub-unauthorized`, `409` is
  `hub-conflict`, any other `4xx` is `hub-rejected`, and transport
  failures, `5xx`, and unparsable refusals stay `hub-unavailable`. The
  code is raised on `GitHubSyncError` and recorded as the receipt
  `last_error_code`; the hub message itself is never surfaced. Only
  `hub-unavailable` means retrying the same batch can succeed.
- In-house Brigade commits include a coding-agent `Co-Authored-By`
  trailer. Known-good identities are in `AGENTS.md`.

## Task 1: normalize issues and reject unsafe fields

**Files:**

- Create: `src/brigade/worklore_github.py`
- Create: `tests/test_worklore_github.py`
- Modify: `src/brigade/work_cmd/ledger/import_model.py`

- [x] Write the failing tests

```python
from brigade import worklore_github as gh


def test_normalize_open_labeled_issue():
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
    assert observation["acceptance"] == ["restic snapshots --latest 1 succeeds"]
    assert observation["source_policy"] == "eligible"
    assert observation["proposed_status"] is None
    assert "body" not in observation
    assert "labels" not in observation


def test_label_removal_and_close_are_observations_not_transitions():
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


def test_normalize_rejects_tokens_home_paths_and_http_urls():
    base = {
        "number": 1,
        "title": "Bad",
        "body": "",
        "labels": [{"name": "burn-queue"}],
        "state": "OPEN",
        "updatedAt": "2026-08-29T12:00:00Z",
        "repository": {"nameWithOwner": "escoffier-labs/brigade"},
    }
    with pytest.raises(gh.GitHubObservationError, match="unknown-field"):
        gh.normalize_issue({**base, "url": "https://github.com/escoffier-labs/brigade/issues/1", "token": "abc"})
    with pytest.raises(gh.GitHubObservationError, match="field-bound"):
        gh.normalize_issue({**base, "url": "http://github.com/escoffier-labs/brigade/issues/1"})
    with pytest.raises(gh.GitHubObservationError, match="field-bound"):
        gh.normalize_issue({**base, "url": "https://github.com/escoffier-labs/brigade/issues/1", "body": "see /workspace/notes/secret"})
```

- [x] Run red through Brigade:

```bash
brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_worklore_github.py"]' --capture brigade-work
```

Expect FAIL: `ModuleNotFoundError: No module named 'brigade.worklore_github'`.

- [x] Implement `normalize_issue`. Pull acceptance through
  `from brigade.work_cmd.ledger.import_model import extract_issue_acceptance`.
  Drop `body` and `labels` from the
  observation after derivation. Reject any key in
  `{token,secret,password,authorization,prompt,transcript,receipt,path}`.
  Reject a body or title that contains `/home/` or `C:\\Users\\`.

- [x] Run green through Brigade. Expect `passed`.

- [x] Commit:

```bash
git add src/brigade/worklore_github.py src/brigade/work_cmd/ledger/import_model.py tests/test_worklore_github.py
git commit -m "$(cat <<'EOF'
feat: normalize GitHub burn-queue issues for Worklore

EOF
)"
```

## Task 2: discovery through the approved read path

**Files:**

- Create: `src/brigade/worklore_github_sync.py`
- Modify: `tests/test_worklore_github.py`

- [x] Write the failing tests

```python
from brigade import worklore_github_sync as sync

PUBLIC_ORG = [{"name": "escoffier-labs", "visibility": "public"}]


def test_load_config_requires_explicit_orgs(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "fleet.toml").write_text("[fleet.worklore.github]\nlabel = \"burn-queue\"\n")
    monkeypatch.setenv("BRIGADE_HOME", str(home))
    with pytest.raises(sync.GitHubSyncError, match="orgs"):
        sync.load_github_config()


def test_discover_uses_octopool_when_present(monkeypatch):
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


def test_discover_falls_back_to_gh_and_rejects_non_json(monkeypatch):
    monkeypatch.setattr(sync.shutil, "which", lambda name: "/usr/bin/gh" if name == "gh" else None)

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="not-json", stderr="")

    monkeypatch.setattr(sync.subprocess, "run", fake_run)
    with pytest.raises(sync.GitHubSyncError, match="retryable-adapter-failure"):
        sync.discover_issues(PUBLIC_ORG, label="burn-queue", state="all")


def test_private_org_never_uses_octopool(monkeypatch):
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


def test_reconcile_refreshes_previously_linked_issue_missing_from_label_search(monkeypatch):
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

    monkeypatch.setattr(sync.shutil, "which", lambda name: "/usr/bin/octopool" if name == "octopool" else "/usr/bin/gh")
    monkeypatch.setattr(sync.subprocess, "run", fake_run)
    observations = sync.discover_and_reconcile(
        PUBLIC_ORG,
        label="burn-queue",
        state="all",
        existing_items=[{
            "title": "Removed label",
            "links": [{
                "link_type": "github",
                "external_key": "escoffier-labs/brigade#12",
                "source_acceptance": [],
                "source_policy": "eligible",
                "url": "https://github.com/escoffier-labs/brigade/issues/12",
                "external_updated_at": "2026-08-28T12:00:00Z",
            }],
        }],
    )
    assert observations[0]["source_policy"] == "label-removed"
    assert any("view" in argv for argv in calls)
    assert all("repository" not in argv for argv in calls if "view" in argv)


def test_pooled_404_is_inconclusive(monkeypatch):
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
        existing_items=[{
            "title": "Existing title",
            "links": [{
                "link_type": "github",
                "external_key": "escoffier-labs/brigade#12",
                "source_acceptance": ["existing acceptance"],
                "source_policy": "eligible",
                "url": "https://github.com/escoffier-labs/brigade/issues/12",
                "external_updated_at": "2026-08-28T12:00:00Z",
            }],
        }],
    )
    assert observations == []
```

Add a direct-`gh` 404 test with the same existing item fixture. It must return
one observation with `stale is True`, `title == "Existing title"`,
`acceptance == ["existing acceptance"]`, and unchanged `source_policy`.

- [x] Run red through Brigade with `tests/test_worklore_github.py`. Expect
  FAIL: `ModuleNotFoundError: No module named 'brigade.worklore_github_sync'`.

- [x] Implement `load_github_config` by reading
  `fleet.worklore.github` the same way `fleet_export.load_sink_config`
  reads `fleet.sink` in `src/brigade/fleet_export.py:443-456`. Require a
  non-empty `orgs` list of tables with `name` matching
  `^[A-Za-z0-9][A-Za-z0-9-]{0,38}$` and `visibility` in
  `{public, private}`. Implement `discover_issues` with a 30 second
  timeout, `stdout=PIPE`, and no token flags. Parse the search response
  as a JSON array and individual refresh responses as JSON objects.
  Implement `discover_and_reconcile` so previously linked GitHub links absent
  from search are refreshed individually. Its `existing_items` argument is the
  `items` array returned by `list_items(source="github")`; it returns normalized
  Worklore observations, never raw GitHub issue objects.

- [x] Run green through Brigade. Expect `passed`.

- [x] Commit:

```bash
git add src/brigade/worklore_github_sync.py tests/test_worklore_github.py
git commit -m "$(cat <<'EOF'
feat: discover labeled GitHub issues for Worklore imports

EOF
)"
```

## Task 3: idempotent batch post and retry receipt

**Files:**

- Modify: `src/brigade/worklore_github_sync.py`
- Modify: `tests/test_worklore_github.py`

Do not modify `worklore_client.py`.

- [x] Write the failing tests

```python
from tests.support import PRIVATE_FILE_MODE, assert_private_mode


def test_sync_posts_one_batch_and_skips_duplicate_revision(tmp_path, monkeypatch):
    posts = []

    def fake_import_batch(payload):
        posts.append(payload)
        return {"created": 1 if len(posts) == 1 else 0, "updated": 0 if len(posts) == 1 else 1}

    monkeypatch.setattr(sync.worklore_client, "import_batch", fake_import_batch)
    monkeypatch.setattr(
        sync.worklore_client,
        "list_items",
        lambda source=None: {"items": []},
    )
    monkeypatch.setattr(
        sync,
        "discover_and_reconcile",
        lambda orgs, *, label, state, existing_items: [{
            "external_key": "escoffier-labs/brigade#12",
            "link_type": "github",
            "title": "Rotate restic password",
            "acceptance": ["restic snapshots --latest 1 succeeds"],
            "source_policy": "eligible",
            "proposed_status": None,
            "url": "https://github.com/escoffier-labs/brigade/issues/12",
            "external_updated_at": "2026-08-29T12:00:00Z",
        }],
    )
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    first = sync.sync_github(orgs=PUBLIC_ORG)
    second = sync.sync_github(orgs=PUBLIC_ORG)
    assert first["posted"] == 1 and second["posted"] == 1
    assert posts[0]["adapter_id"] == "github:escoffier-labs"
    assert posts[0]["source_type"] == "github"
    assert set(posts[0]) >= {"adapter_id", "source_type", "idempotency_key", "observations"}
    assert posts[0]["idempotency_key"] == posts[1]["idempotency_key"]
    assert posts[0]["idempotency_key"].startswith("github:escoffier-labs:")
    receipt_path = tmp_path / "worklore" / "github" / "github-escoffier-labs.json"
    receipt = json.loads(receipt_path.read_text())
    assert receipt["last_error_code"] is None
    assert "Rotate restic" not in json.dumps(receipt)
    assert_private_mode(receipt_path, PRIVATE_FILE_MODE)


def test_hub_unavailable_writes_retry_receipt_and_does_not_raise_raw_url(tmp_path, monkeypatch):
    monkeypatch.setattr(
        sync.worklore_client,
        "import_batch",
        lambda payload: (_ for _ in ()).throw(sync.worklore_client.FleetClientError("hub-unavailable")),
    )
    monkeypatch.setattr(sync.worklore_client, "list_items", lambda source=None: {"items": []})
    monkeypatch.setattr(
        sync,
        "discover_and_reconcile",
        lambda orgs, *, label, state, existing_items: [],
    )
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    with pytest.raises(sync.GitHubSyncError, match="hub-unavailable"):
        sync.sync_github(orgs=PUBLIC_ORG)
    receipt_path = tmp_path / "worklore" / "github" / "github-escoffier-labs.json"
    receipt = json.loads(receipt_path.read_text())
    assert receipt["last_error_code"] == "hub-unavailable"
    assert_private_mode(receipt_path, PRIVATE_FILE_MODE)
```

- [x] Run red through Brigade. Expect FAIL on `sync_github` missing.

- [x] Implement `sync_github`. Call `list_items(source="github")` then
  `discover_and_reconcile`. Fingerprint each observation with SHA-256 of
  `external_key`, `external_updated_at`, `source_policy`, and
  acceptance. POST `{adapter_id, source_type, idempotency_key, observations}`
  with `source_type="github"`. The idempotency key is
  `github:{org}:{sha256(sorted fingerprints)[:24]}` over the observations
  in that posted batch. On
  `FleetClientError("hub-unavailable")` write the receipt and raise
  `GitHubSyncError("hub-unavailable")` with no URL, token, or exception
  text. Empty discovery still posts an empty batch so the hub cursor
  advances. A same-fingerprint replay still updates hub cursor
  `last_success_at` and `updated_at`. Write receipts with mode 0600.

- [x] Run green through Brigade. Expect `passed`.

- [x] Commit:

```bash
git add src/brigade/worklore_github_sync.py tests/test_worklore_github.py
git commit -m "$(cat <<'EOF'
feat: post idempotent GitHub Worklore import batches

EOF
)"
```

## Task 4: CLI

**Files:**

- Modify: `src/brigade/cli/fleet.py`
- Modify: `tests/test_worklore_github.py`

- [x] Write the failing test

```python
from brigade.cli import fleet as fleet_cli


def test_sync_github_cli_json(monkeypatch, capsys):
    monkeypatch.setattr(
        fleet_cli.worklore_github_sync,
        "sync_github",
        lambda **kwargs: {"orgs": ["escoffier-labs"], "posted": 1, "created": 1},
    )
    args = argparse.Namespace(json=True)
    assert fleet_cli._dispatch_work_sync_github(args) == 0
    assert "escoffier-labs" in capsys.readouterr().out
```

- [x] Run red through Brigade. Expect FAIL: attribute missing.

- [x] Add `brigade fleet work sync-github` next to the core `fleet work`
  parsers at the end of `register()` (`src/brigade/cli/fleet.py:45-270`).
  Import `worklore_github_sync` as a module. `_dispatch_work_sync_github`
  reads config, calls `worklore_github_sync.sync_github`, prints JSON on
  `--json`, and
  returns 2 on missing orgs, 1 on `hub-unavailable`, 0 on success. Do not
  print issue titles at default verbosity. Do not add a separate `_impl`
  function.

- [x] Run green through Brigade:

```bash
brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_worklore_github.py"]' --capture brigade-work
```

Expect `passed`.

- [x] Commit:

```bash
git add src/brigade/cli/fleet.py tests/test_worklore_github.py
git commit -m "$(cat <<'EOF'
feat: add brigade fleet work sync-github

EOF
)"
```

## Verification

All tasks use `./scripts/verify-focused tests/test_worklore_github.py`
through `brigade work verify run --capture brigade-work`. Do not run live
`gh` or `octopool` against real organizations in this slice.
