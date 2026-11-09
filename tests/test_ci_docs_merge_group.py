"""Merge-group documentation-only classification: trusted main, cumulative range.

`merge_group.base_sha` is the previous speculative queue entry's head, which is
proposal-controlled, and `base_sha..head_sha` holds only this entry's change.
ci.yml therefore loads scripts/ci_docs_only.py from the tip of main (the commit
the hub selector fetched by fixed refspec) and classifies from the merge base
of main and the group head, which the trusted hub selector validated. These
tests run both literal ci.yml steps, in order, against a local origin.
"""

import os
import re
import subprocess
import sys

import pytest

from tests.test_ci_hub_selection import (
    CLASSIFIER,
    ROOT,
    STEP as HUB_STEP,
    _commit,
    _enqueue,
    _make_queue,
    _replace_main,
    _run,
    _run_step,
    _select,
    _write,
)
from tests.test_ci_workflow import _workflow_job_section, _workflow_step_section

DOCS_STEP = "Classify authoritative event diff"
FORGED_CLASSIFIER = (
    "import os\nfrom pathlib import Path\n"
    "output = 'docs_only=true\\nuncertain=false\\nreason=forged\\n'\n"
    "print(output)\nPath(os.environ['GITHUB_OUTPUT']).write_text(output, encoding='utf-8')\n"
)
# Old parsing (dict over the lines) kept the last value of a repeated key, so
# this read as docs_only=true, uncertain=false.
DUPLICATED_CLASSIFIER = (
    "import os\nfrom pathlib import Path\n"
    "output = 'docs_only=false\\nuncertain=true\\nreason=duplicated\\ndocs_only=true\\nuncertain=false\\n'\n"
    "Path(os.environ['GITHUB_OUTPUT']).write_text(output, encoding='utf-8')\n"
)
UNAVAILABLE = {"docs_only": "false", "uncertain": "true", "reason": "trusted-classifier-unavailable"}
CODE = {"docs_only": "false", "uncertain": "false", "reason": "non-documentation-change"}
DOCS = {"docs_only": "true", "uncertain": "false", "reason": "documentation-only"}


@pytest.fixture
def queue(tmp_path):
    return _make_queue(tmp_path)


def _classify(queue, head, base, *, hub=None, **options):
    """Run hub_merge_group, then the docs step with the env ci.yml maps from it."""
    if hub is None:
        _select(queue, head, base, **options)
        hub = queue.selected
        queue.hub_stdout = queue.stdout
    environment = {"CI_TRUSTED_MAIN": hub["target"], "CI_MERGE_GROUP_START": hub["start"]}
    options.pop("origin", None)
    values = _run_step(queue, DOCS_STEP, head, base, **options, **environment)
    assert set(values) == {"docs_only", "uncertain", "reason"}
    assert values["reason"] in queue.stdout
    return values


def test_the_docs_step_runs_after_and_reads_the_trusted_selector_outputs():
    text = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    changes = _workflow_job_section(text, "changes")
    assert changes.index(f"      - name: {HUB_STEP}\n") < changes.index(f"      - name: {DOCS_STEP}\n")
    docs = _workflow_step_section(text, "changes", DOCS_STEP)
    assert (
        "        id: docs\n"
        "        env:\n"
        "          # Merge groups only: set by hub_merge_group above, empty otherwise.\n"
        "          CI_TRUSTED_MAIN: ${{ steps.hub_merge_group.outputs.target }}\n"
        "          CI_MERGE_GROUP_START: ${{ steps.hub_merge_group.outputs.start }}\n"
        "        run: |\n"
    ) in docs
    body = docs.split("        run: |\n", 1)[1].split("\n      - ", 1)[0]
    assert "${{" not in body
    assert "if:" not in docs.split("        run: |\n", 1)[0]
    # The classifier source in a merge group is the fixed-refspec ref, never base_sha.
    assert '"rev-parse", "--verify", "refs/ci-trusted/main^{commit}"' in body
    # The classifier gets that main commit and the start only in a merge group.
    assert '"CI_TRUSTED_MAIN": source,' in body
    assert 'if key not in {"CI_TRUSTED_MAIN", "CI_MERGE_GROUP_START"}' in body
    assert 'base_sha"' not in body
    assert '[sys.executable, "-I", "-S", str(trusted)]' in body


def test_an_entry_queued_behind_a_code_change_is_not_documentation_only(queue):
    # A changes code. B, queued behind A, changes only documentation. B's
    # base_sha is A's head, so base_sha..head_sha holds only B's docs change,
    # and with HEADGREEN a green B merges A + B even if A's own group failed.
    entry_a, entry_b = _enqueue(queue, ["src/brigade/other.py"], ["docs/guide.md"])
    assert _run(queue.seed, "diff", "--name-only", entry_a, entry_b) == "docs/guide.md"
    assert _classify(queue, entry_b, entry_a) == CODE


def test_a_forged_classifier_ahead_in_the_queue_is_never_run(queue):
    # A rewrites the classifier to always answer docs-only. B behind A is
    # docs-only by content. Old behaviour loaded A's classifier from
    # base_sha (A's head) for B's group. Main's classifier runs instead, and
    # A's script change is itself in the cumulative range.
    entry_a, entry_b = _enqueue(queue, [("scripts/ci_docs_only.py", FORGED_CLASSIFIER)], ["docs/guide.md"])
    assert _classify(queue, entry_b, entry_a) == CODE
    assert "reason=forged" not in queue.stdout


def test_a_forged_classifier_cannot_hide_code_elsewhere_in_the_range(queue):
    entry_a, entry_b, entry_c = _enqueue(
        queue,
        ["src/brigade/other.py"],
        [("scripts/ci_docs_only.py", FORGED_CLASSIFIER)],
        ["docs/guide.md"],
    )
    assert _classify(queue, entry_c, entry_b) == CODE


def test_documentation_only_entries_behind_documentation_still_skip(queue):
    entry_a, entry_b = _enqueue(queue, ["docs/one.md"], ["README.md", "docs/two.md"])
    assert _classify(queue, entry_b, entry_a) == DOCS


def test_the_first_entry_in_line_classifies_its_own_change(queue):
    (head,) = _enqueue(queue, ["docs/guide.md"])
    assert _classify(queue, head, queue.main) == DOCS


def test_once_the_code_entry_merged_the_docs_entry_behind_it_may_skip(queue):
    entry_a, entry_b = _enqueue(queue, ["src/brigade/other.py"], ["docs/guide.md"])
    # A merged: main is A's tested head, which B already contains.
    _run(queue.seed, "push", "-q", "origin", f"{entry_a}:refs/heads/main")
    assert _classify(queue, entry_b, entry_a) == DOCS


def test_the_classifier_comes_from_main_not_the_checkout_or_the_proposal(queue):
    # The head itself carries a forged classifier and a docs change.
    (head,) = _enqueue(queue, [("scripts/ci_docs_only.py", FORGED_CLASSIFIER), "docs/guide.md"])
    assert _classify(queue, head, queue.main) == CODE
    _write(queue.tmp / "work", "scripts/ci_docs_only.py", FORGED_CLASSIFIER)
    assert _classify(queue, head, queue.main) == CODE


# --- Fail closed ----------------------------------------------------------


def _oversize_classifier():
    source = CLASSIFIER.read_text(encoding="utf-8")
    return source + "#" * (131072 - len(source.encode("utf-8")) + 1) + "\n"


@pytest.mark.parametrize(
    "problem",
    [
        "missing",
        "oversize",
        "broken",
        "partial-output",
        "duplicate-key",
        pytest.param("executable", marks=pytest.mark.skipif(os.name == "nt", reason="POSIX executable bits")),
        pytest.param("symlink", marks=pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")),
    ],
)
def test_an_unusable_classifier_on_main_requires_full_ci(queue, problem):
    path = "scripts/ci_docs_only.py"
    if problem == "missing":
        _replace_main(queue, remove=[path])
    elif problem == "oversize":
        _replace_main(queue, (path, _oversize_classifier()))
    elif problem == "broken":
        _replace_main(queue, (path, "invalid python syntax!\n"))
    elif problem == "partial-output":
        _replace_main(queue, (path, FORGED_CLASSIFIER + "raise RuntimeError('fails after writing output')\n"))
    elif problem == "duplicate-key":
        _replace_main(queue, (path, DUPLICATED_CLASSIFIER))
    elif problem == "executable":
        (queue.seed / path).chmod(0o755)
        _run(queue.seed, "update-index", "--chmod=+x", path)
        _replace_main(queue)
    else:
        _run(queue.seed, "rm", "-q", "--", path)
        (queue.seed / path).symlink_to("../README.md")
        _run(queue.seed, "add", "--", path)
        _replace_main(queue)
    main = _run(queue.seed, "rev-parse", "main")
    (head,) = _enqueue(queue, ["docs/guide.md"])
    assert _classify(queue, head, main) == UNAVAILABLE
    # The hub selector itself still established the range.
    assert queue.selected["start"] == main


def test_an_unreachable_main_requires_full_ci(queue):
    (head,) = _enqueue(queue, ["docs/guide.md"])
    _classify(queue, head, queue.main)
    missing = (queue.tmp / "no-such-origin.git").as_uri()
    _run(queue.tmp / "work", "update-ref", "-d", "refs/ci-trusted/main")
    assert _classify(queue, head, queue.main, origin=missing) == UNAVAILABLE
    assert queue.selected["reason"] == "trusted-hub-selector-unavailable"


def test_a_main_with_no_merge_base_requires_full_ci(queue):
    (head,) = _enqueue(queue, ["docs/guide.md"])
    _run(queue.seed, "switch", "-q", "--orphan", "unrelated")
    _commit(
        queue.seed,
        ("scripts/ci_hub_select.py", (ROOT / "scripts/ci_hub_select.py").read_text(encoding="utf-8")),
        ("scripts/ci_hub_paths.yml", (ROOT / "scripts/ci_hub_paths.yml").read_text(encoding="utf-8")),
        ("scripts/ci_docs_only.py", CLASSIFIER.read_text(encoding="utf-8")),
        message="unrelated main",
    )
    _run(queue.seed, "push", "-qf", "origin", "HEAD:refs/heads/main")
    assert _classify(queue, head, queue.main) == UNAVAILABLE
    assert queue.selected["reason"] == "no-merge-base"


def test_several_merge_bases_require_full_ci(queue):
    # Criss-cross: main merges X into Y, the head merges Y into X, so main and
    # the head have two merge bases.
    seed = queue.seed
    _run(seed, "switch", "-qc", "x", "main")
    x = _commit(seed, "docs/x.md", message="x")
    _run(seed, "switch", "-qc", "y", "main")
    y = _commit(seed, "docs/y.md", message="y")
    _run(seed, "switch", "-q", "main")
    _run(seed, "merge", "-q", "--no-ff", "-m", "main merge", x, y)
    _run(seed, "push", "-q", "origin", "main")
    _run(seed, "switch", "-q", "x")
    _run(seed, "merge", "-q", "--no-ff", "-m", "head merge", y)
    head = _commit(seed, "docs/guide.md", message="queued docs")
    _run(seed, "push", "-qf", "origin", "HEAD:refs/heads/gh-readonly-queue/main/test")
    _run(seed, "switch", "-q", "main")
    assert len(_run(seed, "merge-base", "--all", "main", head).split()) == 2
    assert _classify(queue, head, x) == UNAVAILABLE
    assert queue.selected["reason"] == "ambiguous-merge-base"


@pytest.mark.parametrize("base_ref", ["refs/heads/release", "main"])
def test_an_unexpected_target_branch_requires_full_ci(queue, base_ref):
    (head,) = _enqueue(queue, ["docs/guide.md"])
    assert _classify(queue, head, queue.main, base_ref=base_ref) == UNAVAILABLE
    assert queue.selected["reason"] == "unexpected-target-branch"


# The classifier re-checks what it is given, even from a valid selector run.


def _valid_hub(queue, head, base):
    _select(queue, head, base)
    return dict(queue.selected)


@pytest.mark.parametrize("base_ref", ["refs/heads/release", "main"])
def test_the_classifier_refuses_another_target_branch(queue, base_ref):
    (head,) = _enqueue(queue, ["docs/guide.md"])
    hub = _valid_hub(queue, head, queue.main)
    assert _classify(queue, head, queue.main, hub=hub, base_ref=base_ref) == {
        "docs_only": "false",
        "uncertain": "true",
        "reason": "unexpected-target-branch",
    }


@pytest.mark.parametrize("start", ["", "head", "base_sha", "unrelated", "0" * 40])
def test_the_classifier_refuses_a_missing_or_wrong_range_start(queue, start):
    entry_a, entry_b = _enqueue(queue, ["docs/one.md"], ["docs/two.md"])
    hub = _valid_hub(queue, entry_b, entry_a)
    if start == "head":
        start = entry_b
    elif start == "base_sha":
        # The original non-cumulative range: below base_sha, but A is not on
        # main yet, so base_sha..head would skip A's change.
        start = entry_a
    elif start == "unrelated":
        _run(queue.seed, "switch", "-qc", "side", "main")
        start = _commit(queue.seed, "docs/side.md", message="side")
        _run(queue.seed, "push", "-q", "origin", "side")
        _run(queue.seed, "switch", "-q", "main")
    hub["start"] = start
    result = _classify(queue, entry_b, entry_a, hub=hub)
    assert result["docs_only"] == "false"
    assert result["uncertain"] == "true"
    expected = {
        "": "trusted-classifier-unavailable",
        "0" * 40: "trusted-classifier-unavailable",
    }.get(start, "cumulative-range-not-below-group-base")
    assert result["reason"] == expected


def _refusal(reason):
    return {"docs_only": "false", "uncertain": "true", "reason": reason}


@pytest.mark.parametrize(
    ("trusted_main", "start", "expected"),
    [
        (None, "selector", _refusal("missing-trusted-main")),
        ("", "selector", _refusal("missing-trusted-main")),
        ("0" * 40, "selector", _refusal("missing-or-invalid-commit")),
        ("selector", None, _refusal("missing-cumulative-range")),
        ("selector", "", _refusal("missing-cumulative-range")),
        ("selector", "selector", DOCS),
    ],
)
def test_the_classifier_refuses_a_merge_group_without_its_trusted_inputs(queue, trusted_main, start, expected):
    # The bootstrap always passes both values in a merge group; only an older
    # bootstrap could omit one. Run the classifier directly to show it refuses.
    entry_a, entry_b = _enqueue(queue, ["docs/one.md"], ["docs/two.md"])
    hub = _valid_hub(queue, entry_b, entry_a)
    output = queue.tmp / "direct-output.txt"
    environment = dict(
        os.environ,
        GITHUB_EVENT_NAME="merge_group",
        GITHUB_EVENT_PATH=str(queue.tmp / "event.json"),
        GITHUB_SHA=entry_b,
        GITHUB_OUTPUT=str(output),
    )
    for name, value, selected in (
        ("CI_TRUSTED_MAIN", trusted_main, hub["target"]),
        ("CI_MERGE_GROUP_START", start, hub["start"]),
    ):
        environment.pop(name, None)
        if value is not None:
            environment[name] = selected if value == "selector" else value
    subprocess.run(
        [sys.executable, "-I", "-S", str(CLASSIFIER)],
        cwd=queue.tmp / "work",
        env=environment,
        check=True,
        capture_output=True,
    )
    values = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
    assert values == expected


def test_a_selector_target_that_disagrees_with_the_trusted_ref_requires_full_ci(queue):
    # Nothing refetches refs/ci-trusted/main between the two steps, so this is
    # a consistency check between the selector's output and the ref.
    (head,) = _enqueue(queue, ["docs/guide.md"])
    hub = _valid_hub(queue, head, queue.main)
    hub["target"] = "1" * 40
    assert _classify(queue, head, queue.main, hub=hub) == UNAVAILABLE


def test_a_failing_selector_requires_full_ci_and_prints_its_stderr_inertly(queue):
    _replace_main(queue, ("scripts/ci_hub_select.py", "import sys\nsys.exit('::error::selector broke')\n"))
    (head,) = _enqueue(queue, ["docs/guide.md"])
    assert _classify(queue, head, queue.main) == UNAVAILABLE
    assert queue.selected["hub"] == "true"
    assert queue.selected["reason"] == "trusted-hub-selector-unavailable"
    match = re.search(r"^::stop-commands::([0-9a-f]{32})\n(.*?)\n::\1::$", queue.hub_stdout, re.MULTILINE | re.DOTALL)
    assert match, queue.hub_stdout
    assert "::error::selector broke" in match[2]
