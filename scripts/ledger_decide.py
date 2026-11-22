#!/usr/bin/env python3
"""Compute or publish a scheduled ledger proposal from a checkout of main."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from brigade import ci_attestation, outcome_ledger as ledger, outcome_ledger_plan as planner  # noqa: E402
from brigade.outcome_ledger_actions import EvidenceError, parse_time  # noqa: E402
from brigade.outcome_ledger_git import GitError, Repo, is_sha  # noqa: E402

PLAN_FILE = "plan.json"
BRANCH_CONFIG = (
    "-c",
    "user.name=github-actions[bot]",
    "-c",
    "user.email=41898282+github-actions[bot]@users.noreply.github.com",
    "-c",
    "commit.gpgSign=false",
)


class PublicationError(Exception):
    """The scheduled proposal could not safely be published."""


class RetryPublication(PublicationError):
    """Main or a proposal moved while the workflow was running."""


def say(message: str) -> None:
    clean = re.sub(r"[^\x20-\x7e]", "?", message).replace("::", ":")
    print(f"ledger-decide: {clean}", flush=True)


def restores(proposed: dict[str, Any], definitions: dict[str, ledger.SubjectDef]) -> dict[str, str]:
    found = {}
    for key, body in proposed["subjects"].items():
        restore = body.get("restore")
        if restore is None:
            continue
        if not isinstance(restore, dict) or not is_sha(restore.get("to_commit")):
            raise PublicationError("the rollback has no readable target commit")
        found[ledger.skill_dir(definitions[key].subject_path)] = restore["to_commit"]
    return found


def bundle_files(repo: Repo, restore_map: dict[str, str]) -> dict[str, bytes]:
    return {
        f"{directory}/{path}": data
        for directory, commit in restore_map.items()
        for path, data in repo.snapshot(commit, directory).items()
    }


def compute(ev: ledger.Evidence, main: str, artifact: Path, now: dt.datetime) -> None:
    plan = planner.main_plan(ev, main, now)
    say(plan.reason)
    artifact.mkdir()
    metadata = {"main": plan.main, "state": plan.state}
    (artifact / PLAN_FILE).write_bytes(ledger.canonical_bytes(metadata))
    if plan.proposed is not None:
        definitions = ledger.defs_from_ledger(plan.proposed)
        output = {
            ledger.LEDGER_PATH: ledger.canonical_bytes(plan.proposed),
            **bundle_files(ev.repo, restores(plan.proposed, definitions)),
        }
        for name, data in output.items():
            path = artifact / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)


def command(
    *args: str,
    cwd: Path,
    env: dict[str, str] | None = None,
    data: bytes | None = None,
) -> bytes:
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        args, cwd=cwd, env=env, input=data, capture_output=True, timeout=120, check=False
    )
    if result.returncode:
        error = result.stderr.decode("utf-8", "replace").strip()[-1200:]
        raise PublicationError(f"{args[0]} {args[1]} failed ({result.returncode}): {error}")
    return result.stdout


def candidate_commit(repo: Repo, main: str, raw: bytes, restore_map: dict[str, str], *, title: str) -> str:
    """Build a disposable candidate with a private index and exact Git tree modes."""
    with tempfile.TemporaryDirectory(prefix="ledger-index-") as directory:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(directory, "index")))

        def git(*args: str, data: bytes | None = None) -> str:
            output = command(
                "git", "--no-replace-objects", "--literal-pathspecs", *args, cwd=repo.path, env=env, data=data
            )
            return output.decode("ascii").strip()

        git("read-tree", main)
        for path, source in restore_map.items():
            git("restore", "--staged", f"--source={source}", "--", path)
        blob = git("hash-object", "-w", "--stdin", data=raw)
        git("update-index", "--add", "--cacheinfo", f"100644,{blob},{ledger.LEDGER_PATH}")
        tree = git("write-tree")
        return git(*BRANCH_CONFIG, "commit-tree", tree, "-p", main, "-m", title)


def artifact_files(root: Path) -> set[str]:
    files = set()
    if root.is_symlink() or not root.is_dir():
        raise PublicationError("the artifact is not a directory")
    for path in root.rglob("*"):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise PublicationError("the artifact has an unsupported entry")
        if path.is_file():
            files.add(path.relative_to(root).as_posix())
    return files


def verify_artifact(
    ev: ledger.Evidence, main: str, artifact: Path, now: dt.datetime
) -> tuple[planner.Plan, bytes | None, str | None]:
    files = artifact_files(artifact)
    metadata = json.loads((artifact / PLAN_FILE).read_bytes())
    if not isinstance(metadata, dict) or set(metadata) != {"main", "state"} or not is_sha(metadata["main"]):
        raise PublicationError("the plan metadata is invalid")
    if metadata["main"] != main:
        raise RetryPublication("main advanced between compute and publish")
    state = metadata["state"]
    if state in {"pending", "stale"}:
        if files != {PLAN_FILE}:
            raise PublicationError("a deferred plan contains proposed files")
        return planner.Plan(main, state, f"the compute job deferred a {state} plan"), None, None
    if state not in {"update", "equal"}:
        raise PublicationError("the plan state is invalid")
    raw = (artifact / ledger.LEDGER_PATH).read_bytes()
    proposed = ledger.parse_ledger(raw)
    if proposed["covers"]["main_sha"] != main:
        raise PublicationError("the proposed ledger covers a different commit")
    previous_raw = ev.repo.show(main, ledger.LEDGER_PATH)
    previous = ledger.parse_ledger(previous_raw) if previous_raw is not None else None
    definitions = (
        ledger.defs_from_ledger(previous)
        if previous
        else {planner.NOTE_KEY: planner._genesis_definition(ev.repo, main)}
    )
    if ledger.defs_from_ledger(proposed) != definitions:
        raise PublicationError("the computed plan changes subject definitions or approvals")
    expected_state = "equal" if previous and planner.same_decisions(previous, proposed) else "update"
    if state != expected_state:
        raise PublicationError("the plan state disagrees with its proposed ledger")
    restore_map = restores(proposed, definitions)
    bundle = bundle_files(ev.repo, restore_map)
    if files != {PLAN_FILE, ledger.LEDGER_PATH, *bundle}:
        raise PublicationError("the artifact files disagree with the proposed plan")
    if any((artifact / path).read_bytes() != data for path, data in bundle.items()):
        raise PublicationError("the artifact has a different copy of a restored bundle")
    commit = candidate_commit(ev.repo, main, raw, restore_map, title="chore(outcome): update the CI outcome ledger")
    errors = ledger.verify_ledger(
        ev,
        raw=raw,
        main_raw=previous_raw,
        main_ref=main,
        head_ref=commit,
        context=ledger.EventContext("workflow_dispatch", None),
        now=now,
    )
    if errors:
        if planner.retryable_errors(errors):
            raise RetryPublication(" | ".join(errors))
        # A rerun can make a recorded verdict pending and change the rebuild at the original cutoff.
        if "ledger differs from the recomputed ledger" in errors:
            current = planner.main_plan(ev, main, now)
            if current.state == "pending":
                raise RetryPublication(current.reason)
        raise PublicationError(" | ".join(errors))
    return planner.Plan(main, state, f"the verified plan is {state}", proposed), previous_raw, commit


class Publisher:
    def __init__(self, ev: ledger.Evidence, main: str) -> None:
        self.ev = ev
        self.main = main

    def _gh(self, *args: str, data: bytes | None = None) -> bytes:
        return command("gh", *args, cwd=self.ev.repo.path, data=data)

    def _ref(self, branch: str) -> str | None:
        endpoint = f"repos/{self.ev.slug}/git/ref/heads/{quote(branch, safe='/')}"
        try:
            result = ci_attestation.default_gh_api(endpoint)
        except ci_attestation.GhNotFound:
            return None
        obj = result.get("object") if isinstance(result, dict) else None
        if not isinstance(obj, dict) or obj.get("type") != "commit" or not is_sha(obj.get("sha")):
            raise PublicationError("the remote branch ref is unreadable")
        return obj["sha"]

    def _check_main(self) -> None:
        if self._ref("main") != self.main:
            raise RetryPublication("main advanced before publication")

    def _queued(self, pull: dict[str, Any]) -> bool:
        node = pull.get("node_id")
        if not isinstance(node, str) or not node:
            raise PublicationError("the pull request has no node id")
        query = "query($id: ID!) { node(id: $id) { ... on PullRequest { state headRefOid isInMergeQueue } } }"
        result = json.loads(self._gh("api", "graphql", "-f", f"query={query}", "-f", f"id={node}"))
        data = result.get("data") if isinstance(result, dict) else None
        row = data.get("node") if isinstance(data, dict) else None
        if not isinstance(row, dict) or not isinstance(queued := row.get("isInMergeQueue"), bool):
            raise PublicationError("the pull request queue state is unreadable")
        if row.get("state") != "OPEN" or row.get("headRefOid") != pull["head"]["sha"]:
            raise RetryPublication("the pull request changed before publication")
        return queued

    def _decision(self, branch: str) -> tuple[planner.BranchAction, dict[str, Any] | None]:
        # Publication reads live PR state. Evidence reads used during recompute are memoized.
        rows = planner.branch_prs(ci_attestation.default_gh_api, self.ev.slug, branch)
        pull = next((row for row in rows if row["state"] == "open"), None)
        queued = pull is not None and self._queued(pull)
        return planner.branch_action(branch, [row["state"].upper() for row in rows], queued=queued), pull

    def _push(self, branch: str, commit: str, pull: dict[str, Any] | None) -> None:
        self._check_main()
        old = self._ref(branch)
        if pull is not None:
            if old != pull["head"]["sha"]:
                raise RetryPublication("the branch head changed before publication")
            if self._queued(pull):
                raise RetryPublication(f"{branch} is in the merge queue")
        else:
            action, _ = self._decision(branch)
            if action != "create":
                raise RetryPublication(f"{branch} now has a pull request")
        ref = f"refs/heads/{branch}"
        command(
            "git", "push", f"--force-with-lease={ref}:{old or ''}", "origin", f"{commit}:{ref}", cwd=self.ev.repo.path
        )

    def _open(self, branch: str, title: str, body: str) -> dict[str, Any]:
        payload = ledger.canonical_bytes({"head": branch, "base": "main", "title": title, "body": body, "draft": False})
        result = json.loads(
            self._gh("api", "--method", "POST", f"repos/{self.ev.slug}/pulls", "--input", "-", data=payload)
        )
        if not isinstance(result, dict) or type(result.get("number")) is not int:
            raise PublicationError("the pull request creation response is unreadable")
        return result

    def _dispatch(self, branch: str) -> None:
        self._gh("workflow", "run", "ci.yml", "--repo", self.ev.slug, "--ref", branch)
        say(f"dispatched ci.yml on {branch}")

    def _dispatch_if_missing(self, branch: str, head: str) -> None:
        query = urlencode({"event": "workflow_dispatch", "branch": branch, "head_sha": head, "per_page": 1})
        endpoint = f"repos/{self.ev.slug}/actions/workflows/ci.yml/runs?{query}"
        runs = ci_attestation.default_gh_api(endpoint)
        if not isinstance(runs, dict) or type(runs.get("total_count")) is not int:
            raise PublicationError("the CI run listing is unreadable")
        if runs["total_count"] == 0:
            self._dispatch(branch)

    def auto(self, commit: str) -> None:
        branch = planner.AUTO_BRANCH
        action, pull = self._decision(branch)
        if action == "queued":
            say(f"{branch} is in the merge queue. Leaving its branch alone.")
            return
        self._push(branch, commit, pull)
        if pull is None:
            pull = self._open(
                branch,
                "chore(outcome): update the CI outcome ledger",
                "Updates the outcome ledger from completed attest jobs on main. The required recompute checks the proposed ledger and any restored bundle before it can merge.",
            )
        number = pull["number"]
        say(f"automatic ledger pull request #{number}")
        self._dispatch(branch)
        self._gh(
            "pr",
            "merge",
            str(number),
            "--repo",
            self.ev.slug,
            "--auto",
            "--squash",
            "--match-head-commit",
            commit,
        )

    def close_obsolete_auto(self) -> None:
        branch = planner.AUTO_BRANCH
        _, pull = self._decision(branch)
        if pull is None:
            return
        self._check_main()
        self._gh(
            "pr",
            "close",
            str(pull["number"]),
            "--repo",
            self.ev.slug,
            "--comment",
            "Closing this automatic proposal because the recomputed ledger matches main.",
        )

    def _refresh_approval(
        self, pull: dict[str, Any], previous_raw: bytes, approval: planner.Approval, now: dt.datetime
    ) -> bool:
        # Full-history checkout fetches the existing approval branches from origin.
        try:
            head = self.ev.repo.resolve(pull["head"]["sha"])
        except GitError:
            raise RetryPublication("the approval head commit is missing locally") from None
        raw = self.ev.repo.show(head, ledger.LEDGER_PATH)
        if raw is None:
            say("the approval head has no ledger. Rebuilding the branch.")
            return True
        try:
            prior = ledger.parse_ledger(raw)
        except ledger.LedgerError:
            say("the approval head has an unreadable ledger. Rebuilding the branch.")
            return True
        cutoff = parse_time(prior["covers"]["cutoff_at"])
        approved = prior["subjects"].get(approval.key, {}).get("approved", [])
        has_row = any(
            row.get("pr") == pull["number"] and row.get("fingerprint") == approval.fingerprint for row in approved
        )
        return (
            not has_row
            or prior["prev_ledger_sha256"] != ledger.sha256_hex(previous_raw)
            or cutoff is None
            or now - cutoff >= ledger.MAX_LEDGER_AGE
        )

    def approval(
        self,
        previous_raw: bytes,
        proposed: dict[str, Any],
        approval: planner.Approval,
        now: dt.datetime,
        *,
        allow_create: bool = True,
    ) -> bool:
        """Publish or refresh an approval and report whether a new PR was opened."""
        try:
            branch = approval.branch
        except ledger.LedgerError as exc:
            say(f"LedgerError: {exc}")
            return False
        title = f"chore(outcome): approve {approval.key.removeprefix('skill:')} {approval.fingerprint[:12]}"
        action, pull = self._decision(branch)
        if action in {"closed", "queued"}:
            say(f"{branch} is {action}. Leaving its branch alone.")
            return False
        if pull is not None and not self._refresh_approval(pull, previous_raw, approval, now):
            say(f"{branch} already has a current approval pull request")
            self._dispatch_if_missing(branch, pull["head"]["sha"])
            return False
        if pull is None and not allow_create:
            return False
        body = approval_body(self.ev, proposed, approval)

        def build(number: int) -> str:
            candidate = planner.with_approval(self.ev, previous_raw, proposed, approval, number)
            definitions = ledger.defs_from_ledger(candidate)
            return candidate_commit(
                self.ev.repo,
                self.main,
                ledger.canonical_bytes(candidate),
                restores(candidate, definitions),
                title=title,
            )

        existing = pull is not None
        if pull is None:
            # The approval row needs the number GitHub assigns to the pull request.
            # This first head cannot pass recompute because it lacks that number.
            provisional = build(0)
            self._push(branch, provisional, None)
            opened = self._open(branch, title, body)
            pull = {**opened, "head": {"sha": provisional}}
        final = build(pull["number"])
        self._push(branch, final, pull)
        if existing:
            self._gh(
                "api",
                "--method",
                "PATCH",
                f"repos/{self.ev.slug}/pulls/{pull['number']}",
                "--input",
                "-",
                data=ledger.canonical_bytes({"body": body}),
            )
        say(f"owner approval requested in pull request #{pull['number']}")
        self._dispatch(branch)
        return not existing


def approval_body(ev: ledger.Evidence, proposed: dict[str, Any], approval: planner.Approval) -> str:
    subject = proposed["subjects"][approval.key]
    source = subject["approved"][-1]["commit"]
    target = proposed["covers"]["main_sha"]
    directory = ledger.skill_dir(subject["subject_path"])
    change = ev.repo._run(
        "--literal-pathspecs", "diff", "--no-ext-diff", "--no-textconv", source, target, "--", directory
    ).stdout.decode("utf-8", "replace")
    # Indentation keeps all bundle text inside a Markdown code block.
    diff_limit = 22000
    diff = "\n".join(f"    {line}" for line in change[:diff_limit].splitlines())
    compare = f"https://github.com/{ev.slug}/compare/{source}...{target}"
    comparison = (
        f"Diff truncated. See the [full comparison]({compare})."
        if len(change) > diff_limit
        else f"[Full comparison]({compare})"
    )
    names = ev.repo._run(
        "--literal-pathspecs", "diff", "--name-only", "--no-renames", source, target, "--", directory
    ).stdout.decode("utf-8", "replace")
    files = "\n".join(f"    {path}" for path in names.splitlines())
    rows = ["| Commit | Verdict | Jobs |", "| --- | --- | --- |"]
    for trial in subject["trials"]:
        if trial["fingerprint"] != approval.fingerprint:
            continue
        links = [
            f"[run {job['run_id']}, attempt {job['attempt']}, job {job['job_id']}]"
            f"(https://github.com/{ev.slug}/actions/runs/{job['run_id']}/job/{job['job_id']})"
            for job in trial["jobs"]
        ]
        verdict = trial["outcome"]
        if trial["reason"]:
            verdict += f" ({trial['reason']})"
        rows.append(f"| `{trial['sha'][:12]}` | {verdict} | {', '.join(links) or 'No job recorded'} |")
    trials = "\n".join(rows)
    return (
        f"Requests owner approval for `{approval.key}` at fingerprint `{approval.fingerprint}`.\n\n"
        "Review the final head, then rerun failed CI jobs after submitting the approval if recompute ran before the review. "
        "Once CI passes, add this pull request to the merge queue.\n\n"
        "## Bundle diff\n\n"
        f"Changed files\n\n{files}\n\n"
        f"{comparison}\n\n{diff}\n\n## Attest trials\n\n{trials}\n"
    )


def publish(ev: ledger.Evidence, main: str, artifact: Path, now: dt.datetime) -> None:
    plan, previous_raw, commit = verify_artifact(ev, main, artifact, now)
    if plan.proposed is None:
        say(plan.reason)
        return
    publisher = Publisher(ev, main)
    if plan.state == "update":
        assert commit is not None
        publisher.auto(commit)
        return
    say("the automatic ledger matches main")
    publisher.close_obsolete_auto()
    assert previous_raw is not None
    created = False
    for approval in planner.approvals(plan.proposed):
        if publisher.approval(previous_raw, plan.proposed, approval, now, allow_create=not created):
            created = True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["compute", "publish"])
    parser.add_argument("--artifact", required=True, type=Path)
    args = parser.parse_args()
    try:
        repo = Repo(Path.cwd())
        sha = repo.resolve("HEAD")
        ev = planner.evidence(repo, ci_attestation.default_gh_api, os.environ["GITHUB_REPOSITORY"])
        now = dt.datetime.now(dt.timezone.utc)
        operation = compute if args.stage == "compute" else publish
        operation(ev, sha, args.artifact, now)
        return 0
    except RetryPublication as exc:
        say(f"{exc}. The next run will retry.")
        return 0
    except (
        GitError,
        ledger.LedgerError,
        EvidenceError,
        PublicationError,
        ci_attestation.GhUnavailable,
        ci_attestation.GhNotFound,
        subprocess.SubprocessError,
        OSError,
        KeyError,
        ValueError,
        TypeError,
    ) as exc:
        say(f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
