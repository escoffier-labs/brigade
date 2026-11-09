"""Hub suite selection: the trusted merge-group selector and the shared path list."""

import ast
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import textwrap
from types import SimpleNamespace

import pytest

from tests.test_ci_workflow import ROOT, _workflow_bash_command, _workflow_step_section

SELECTOR = ROOT / "scripts/ci_hub_select.py"
PATHS = ROOT / "scripts/ci_hub_paths.yml"
STEP = "Select hub suite for the merge group from trusted main"


def _selector_module():
    spec = importlib.util.spec_from_file_location("ci_hub_select", SELECTOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ci_hub_select = _selector_module()
HUB_PATTERNS = ci_hub_select.parse_patterns(PATHS.read_text(encoding="utf-8"))


# --- Path list and matcher -------------------------------------------------


def test_the_shared_path_list_parses_strictly_and_selects_its_own_sources():
    assert len(HUB_PATTERNS) == len(set(HUB_PATTERNS)) >= 30
    for source in sorted(ci_hub_select.SELECTION_SOURCES):
        assert ci_hub_select.matches(source, HUB_PATTERNS), source


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("engines/hub/**", "engines/hub/src/lib.rs", True),
        ("engines/hub/**", "engines/hub/Cargo.toml", True),
        ("engines/hub/**", "engines/hub/.cargo/config.toml", True),
        ("engines/hub/**", "engines/hubx/Cargo.toml", False),
        ("engines/hub/**", "engines/hub2/src/lib.rs", False),
        ("src/brigade/fleet_hub*.py", "src/brigade/fleet_hub.py", True),
        ("src/brigade/fleet_hub*.py", "src/brigade/fleet_hub_messages.py", True),
        ("src/brigade/fleet_hub*.py", "src/brigade/fleet_hub/messages.py", False),
        ("src/brigade/fleet_hub*.py", "src/brigade/x/fleet_hub.py", False),
        ("src/brigade/fleet_hub*.py", "src/brigade/fleet_mesh.py", False),
        ("registry/skills/verify-brigade/**", "registry/skills/verify-brigade/features/hub.feature", True),
        ("pyproject.toml", "pyproject.toml", True),
        ("pyproject.toml", "engines/hub/pyproject.toml", False),
        (".github/workflows/ci.yml", ".github/workflows/ci.yml", True),
        (".github/workflows/ci.yml", ".github/workflows/ci.yml.bak", False),
        ("a/**/b.py", "a/b.py", True),
        ("a/**/b.py", "a/x/y/b.py", True),
    ],
)
def test_matcher_follows_picomatch_dot_semantics(pattern, path, expected):
    assert ci_hub_select.matches(path, (pattern,)) is expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "# only a comment\n",
        "hub:\n",
        "mesh:\n  - 'a/**'\n",
        "hub:\n  - 'a/**'\nmesh:\n  - 'b'\n",
        "hub:\n  - '!engines/hub/**'\n",
        "hub:\n  - 'engines/[h]ub/**'\n",
        "hub:\n  - 'engines/hub/**.rs'\n",
        "hub:\n  - '/engines/hub/**'\n",
        "hub:\n  - 'engines/../hub'\n",
        "hub:\n  - 'engines//hub'\n",
        'hub:\n  - "engines/hub/**"\n',
        "hub:\n- 'engines/hub/**'\n",
        "hub:\r\n  - 'engines/hub/**'\r\n",
        "hub:\n  - 'engines/hub/**'\n  # trailing comment\n",
        "hub: ['engines/hub/**']\n",
    ],
)
def test_path_list_parser_refuses_every_other_shape(text):
    with pytest.raises(ci_hub_select.Fail, match="invalid-path-list"):
        ci_hub_select.parse_patterns(text)


# --- Filter completeness ---------------------------------------------------


def _hub_workflow_steps():
    """Yield (working directory, run text) for every step in hub.yml."""
    text = (ROOT / ".github/workflows/hub.yml").read_text(encoding="utf-8")
    jobs = text.split("jobs:\n", 1)[1]
    for job in re.split(r"^  (?=[a-z0-9-]+:$)", jobs, flags=re.MULTILINE)[1:]:
        default = re.search(r"^      run:\n        working-directory: (\S+)$", job, re.MULTILINE)
        for step in job.split("\n      - ")[1:]:
            match = re.search(r"(?:^|\n\s*)run: (>-\n|\|\n)?(.*?)(?=\n        [a-z-]+:|\Z)", step, re.DOTALL)
            if not match:
                continue
            directory = re.search(r"^        working-directory: (\S+)$", step, re.MULTILINE)
            yield (directory or default)[1], " ".join(match[2].split())


def _hub_workflow_files():
    files = set()
    for directory, command in _hub_workflow_steps():
        command = re.sub(r"\$\{\{[^}]*\}\}", "", command)
        tokens = shlex.split(command)
        for index, token in enumerate(tokens):
            if token.startswith("-") or "=" in token or token.startswith("/"):
                continue
            path = None
            if token.endswith(".py"):
                path = os.path.normpath(os.path.join(directory, token))
            elif index and tokens[index - 1] == "-s":
                path = os.path.normpath(os.path.join(directory, token, "test_discovered.py"))
            elif index and tokens[index - 1] == "-e":
                path = os.path.normpath(os.path.join(directory, token, "pyproject.toml"))
            if path is not None:
                files.add(Path(path).as_posix())
    return files


def _brigade_module_file(module: str) -> str | None:
    relative = Path("src", *module.split("."))
    if (ROOT / relative).with_suffix(".py").is_file():
        return relative.with_suffix(".py").as_posix()
    if (ROOT / relative / "__init__.py").is_file():
        return (relative / "__init__.py").as_posix()
    return None


def _direct_brigade_modules(path: Path) -> set[str]:
    modules = set()

    def add(module):
        # Importing a.b.c also executes a/__init__ and a/b/__init__.
        parts = module.split(".")
        modules.update(".".join(parts[:index]) for index in range(1, len(parts) + 1))

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "brigade":
                    add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module.split(".")[0] != "brigade":
                continue
            add(node.module)
            for alias in node.names:
                if _brigade_module_file(f"{node.module}.{alias.name}"):
                    add(f"{node.module}.{alias.name}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # importlib.import_module("brigade.x") and `python -m brigade`.
            if re.fullmatch(r"brigade(\.[a-z_]+)+", node.value) and _brigade_module_file(node.value):
                add(node.value)
            elif node.value == "brigade" and "-m" in path.read_text(encoding="utf-8"):
                add("brigade.__main__")
                add("brigade.cli")
    return modules


def test_hub_path_list_covers_every_file_the_hub_suite_runs():
    files = _hub_workflow_files()
    # The parser must find the known entry points, or this test proves nothing.
    for known in (
        "pyproject.toml",
        "scripts/memory_scan_bundle.py",
        "engines/hub/tests/prepublish.py",
        "engines/hub/tests/memory_scan_unavailable.py",
        "engines/hub/bench/test_discovered.py",
        "registry/skills/verify-brigade/control-brigade.py",
    ):
        assert known in files, known
    assert len(files) >= 20
    missing = sorted(path for path in files if not ci_hub_select.matches(path, HUB_PATTERNS))
    assert not missing, f"add these to scripts/ci_hub_paths.yml: {missing}"


def test_hub_path_list_covers_every_brigade_module_the_hub_tests_import_directly():
    sources = [
        *sorted((ROOT / "engines/hub/tests").glob("*.py")),
        *sorted((ROOT / "engines/hub/bench").glob("*.py")),
        *sorted((ROOT / "registry/skills/verify-brigade").glob("*.py")),
    ]
    modules = set().union(*(_direct_brigade_modules(path) for path in sources))
    for known in ("brigade.fleet_client", "brigade.fleet_hub", "brigade.cli", "brigade.memory_hub_client"):
        assert known in modules, known
    files = {module: _brigade_module_file(module) for module in modules}
    assert all(files.values()), [module for module, file in files.items() if not file]
    missing = sorted(file for file in files.values() if not ci_hub_select.matches(file, HUB_PATTERNS))
    # Transitive imports are the nightly full run's job (hub-nightly.yml).
    assert not missing, f"add these to scripts/ci_hub_paths.yml: {missing}"


# --- The merge-group selector, end to end through the ci.yml bootstrap -----


def _run(cwd, *args):
    return subprocess.check_output(["git", "-C", str(cwd), *args], text=True, stderr=subprocess.STDOUT).strip()


def _write(repo, path, content="new\n"):
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)


def _commit(repo, *changes, message="change"):
    for change in changes:
        path, content = change if isinstance(change, tuple) else (change, f"{change} {message}\n")
        _write(repo, path, content)
        _run(repo, "add", "--", path)
    _run(repo, "commit", "-qm", message)
    return _run(repo, "rev-parse", "HEAD")


@pytest.fixture
def queue(tmp_path):
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    _run(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    # GitHub serves any commit by id; a local bare repo needs this to match.
    _run(origin, "config", "uploadpack.allowAnySHA1InWant", "true")
    _run(tmp_path, "init", "-q", "-b", "main", str(seed))
    _run(seed, "config", "user.email", "contributor@example.com")
    _run(seed, "config", "user.name", "Contributor")
    _run(seed, "remote", "add", "origin", origin.as_uri())
    main = _commit(
        seed,
        ("README.md", "base\n"),
        ("scripts/ci_hub_select.py", SELECTOR.read_text(encoding="utf-8")),
        ("scripts/ci_hub_paths.yml", PATHS.read_text(encoding="utf-8")),
        ("engines/hub/src/lib.rs", "pub fn hub() {}\n"),
        ("src/brigade/other.py", "VALUE = 1\n"),
        message="base",
    )
    _run(seed, "push", "-q", "origin", "main")
    return SimpleNamespace(tmp=tmp_path, origin=origin, seed=seed, main=main)


def _enqueue(queue, *entries):
    """Build speculative queue heads on top of main, as GitHub does."""
    _run(queue.seed, "switch", "-qC", "queue", "main")
    heads = [_commit(queue.seed, *changes, message=f"entry {index}") for index, changes in enumerate(entries, 1)]
    _run(queue.seed, "push", "-qf", "origin", "HEAD:refs/heads/gh-readonly-queue/main/test")
    _run(queue.seed, "switch", "-q", "main")
    return heads


def _select(queue, head, base, *, base_ref="refs/heads/main", origin=None, **environment):
    work = queue.tmp / "work"
    if not work.exists():
        _run(queue.tmp, "init", "-q", str(work))
        _run(work, "remote", "add", "origin", queue.origin.as_uri())
        # actions/checkout: the merge group head at fetch-depth 2, detached.
        _run(work, "fetch", "-q", "--no-tags", "--depth=2", "origin", head)
        _run(work, "checkout", "-q", "--detach", head)
    if origin is not None:
        _run(work, "remote", "set-url", "origin", origin)
    event = queue.tmp / "event.json"
    event.write_text(json.dumps({"merge_group": {"base_sha": base, "head_sha": head, "base_ref": base_ref}}))
    output = queue.tmp / "output.txt"
    output.write_text("")
    runner_temp = queue.tmp / "runner-temp"
    runner_temp.mkdir(exist_ok=True)
    step = _workflow_step_section((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"), "changes", STEP)
    script = textwrap.dedent(step.split("        run: |\n", 1)[1])
    result = subprocess.run(
        _workflow_bash_command(script),
        cwd=work,
        env=dict(
            os.environ,
            GITHUB_EVENT_NAME="merge_group",
            GITHUB_EVENT_PATH=str(event),
            GITHUB_SHA=head,
            GITHUB_OUTPUT=str(output),
            RUNNER_TEMP=str(runner_temp),
            **environment,
        ),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    values = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
    assert set(values) == {"hub", "reason"}
    return values


def test_an_entry_queued_behind_a_hub_change_selects_the_hub_suite(queue):
    # A changes a hub path. B, queued behind A, does not. B's head contains
    # A + B but merge_group.base_sha is A's head, so base_sha..head_sha holds
    # only B. If A's hub suite fails, HEADGREEN can still merge A + B on B's
    # green head, so B must run the suite.
    entry_a, entry_b = _enqueue(queue, ["engines/hub/src/lib.rs"], ["src/brigade/other.py"])
    assert _run(queue.seed, "diff", "--name-only", entry_a, entry_b) == "src/brigade/other.py"
    assert _select(queue, entry_b, entry_a) == {"hub": "true", "reason": "hub-path-changed"}


def test_an_entry_queued_behind_non_hub_changes_may_skip_the_hub_suite(queue):
    entry_a, entry_b = _enqueue(queue, ["docs/guide.md"], ["src/brigade/other.py"])
    assert _select(queue, entry_b, entry_a) == {"hub": "false", "reason": "no-hub-path-changed"}


def test_once_the_hub_entry_merged_the_entry_behind_it_needs_only_its_own_change(queue):
    entry_a, entry_b = _enqueue(queue, ["engines/hub/src/lib.rs"], ["src/brigade/other.py"])
    # A merged: main is A's tested head, which B already contains.
    _run(queue.seed, "push", "-q", "origin", f"{entry_a}:refs/heads/main")
    assert _select(queue, entry_b, entry_a) == {"hub": "false", "reason": "no-hub-path-changed"}


@pytest.mark.parametrize("source", sorted(ci_hub_select.SELECTION_SOURCES))
def test_a_change_to_any_selection_source_selects_the_hub_suite(queue, source):
    (head,) = _enqueue(queue, [(source, "changed\n"), "src/brigade/other.py"])
    assert _select(queue, head, queue.main) == {"hub": "true", "reason": "hub-selection-source-changed"}


def test_a_head_that_rewrites_the_filter_and_selector_cannot_skip_the_hub_suite(queue):
    forged_list = "hub:\n  - 'nothing/at/all'\n"
    forged_selector = (
        "import os\nfrom pathlib import Path\n"
        "Path(os.environ['GITHUB_OUTPUT']).write_text('hub=false\\nreason=forged\\n')\n"
    )
    (head,) = _enqueue(
        queue,
        [
            ("scripts/ci_hub_paths.yml", forged_list),
            ("scripts/ci_hub_select.py", forged_selector),
            ".github/workflows/ci.yml",
            "engines/hub/src/lib.rs",
        ],
    )
    assert _select(queue, head, queue.main) == {"hub": "true", "reason": "hub-selection-source-changed"}


def test_the_path_list_comes_from_main_not_the_checkout(queue):
    (head,) = _enqueue(queue, ["engines/hub/src/lib.rs"])
    work = queue.tmp / "work"
    _select(queue, head, queue.main)
    # Even an edited working tree file is never read.
    _write(work, "scripts/ci_hub_paths.yml", "hub:\n  - 'nothing/at/all'\n")
    assert _select(queue, head, queue.main) == {"hub": "true", "reason": "hub-path-changed"}


def _replace_main(queue, *changes, remove=()):
    _run(queue.seed, "switch", "-q", "main")
    for path in remove:
        _run(queue.seed, "rm", "-q", "--", path)
    if changes:
        for path, content in changes:
            _write(queue.seed, path, content)
            _run(queue.seed, "add", "--", path)
    _run(queue.seed, "commit", "-qm", "main change")
    _run(queue.seed, "push", "-q", "origin", "main")
    return _run(queue.seed, "rev-parse", "HEAD")


@pytest.mark.parametrize(
    ("problem", "reason"),
    [
        ("selector-missing", "trusted-hub-selector-unavailable"),
        pytest.param(
            "selector-executable",
            "trusted-hub-selector-unavailable",
            marks=pytest.mark.skipif(os.name == "nt", reason="POSIX executable bits"),
        ),
        ("selector-broken", "trusted-hub-selector-unavailable"),
        ("paths-missing", "missing-or-unsafe-path-list"),
        ("paths-oversize", "missing-or-unsafe-path-list"),
        ("paths-empty", "missing-or-unsafe-path-list"),
        ("paths-malformed", "invalid-path-list"),
    ],
)
def test_an_unusable_trusted_selector_or_path_list_selects_the_hub_suite(queue, problem, reason):
    if problem == "selector-missing":
        _replace_main(queue, remove=["scripts/ci_hub_select.py"])
    elif problem == "selector-executable":
        (queue.seed / "scripts/ci_hub_select.py").chmod(0o755)
        _run(queue.seed, "update-index", "--chmod=+x", "scripts/ci_hub_select.py")
        _replace_main(queue)
    elif problem == "selector-broken":
        _replace_main(queue, ("scripts/ci_hub_select.py", "invalid python syntax!\n"))
    elif problem == "paths-missing":
        _replace_main(queue, remove=["scripts/ci_hub_paths.yml"])
    elif problem == "paths-oversize":
        padding = "#" * 79 + "\n"
        _replace_main(queue, ("scripts/ci_hub_paths.yml", padding * 205 + PATHS.read_text(encoding="utf-8")))
    elif problem == "paths-empty":
        _replace_main(queue, ("scripts/ci_hub_paths.yml", ""))
    else:
        _replace_main(queue, ("scripts/ci_hub_paths.yml", "hub:\n  - '!engines/hub/**'\n"))
    (head,) = _enqueue(queue, ["src/brigade/other.py"])
    base = _run(queue.seed, "rev-parse", "main")
    assert _select(queue, head, base) == {"hub": "true", "reason": reason}


def test_a_failed_fetch_of_main_selects_the_hub_suite(queue):
    (head,) = _enqueue(queue, ["src/brigade/other.py"])
    _select(queue, head, queue.main)
    missing = (queue.tmp / "no-such-origin.git").as_uri()
    assert _select(queue, head, queue.main, origin=missing) == {
        "hub": "true",
        "reason": "trusted-hub-selector-unavailable",
    }


def test_a_failed_history_fetch_selects_the_hub_suite(queue):
    (head,) = _enqueue(queue, ["src/brigade/other.py"])
    # A base the remote does not have: the selector's own fetch fails.
    assert _select(queue, head, "1" * 40) == {"hub": "true", "reason": "trusted-hub-selection-failed"}


def test_a_main_with_no_merge_base_selects_the_hub_suite(queue):
    (head,) = _enqueue(queue, ["src/brigade/other.py"])
    _run(queue.seed, "switch", "-q", "--orphan", "unrelated")
    _commit(
        queue.seed,
        ("scripts/ci_hub_select.py", SELECTOR.read_text(encoding="utf-8")),
        ("scripts/ci_hub_paths.yml", PATHS.read_text(encoding="utf-8")),
        message="unrelated main",
    )
    _run(queue.seed, "push", "-qf", "origin", "HEAD:refs/heads/main")
    assert _select(queue, head, queue.main) == {"hub": "true", "reason": "trusted-hub-selection-failed"}


def test_a_group_base_below_the_merge_base_selects_the_hub_suite(queue):
    parent = queue.main
    queue.main = _replace_main(queue, ("README.md", "moved\n"))
    (head,) = _enqueue(queue, ["src/brigade/other.py"])
    # The event claims a base older than main's tip, which head contains.
    assert _select(queue, head, parent) == {"hub": "true", "reason": "merge-base-above-group-base"}


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"base_ref": "refs/heads/release"}, "unexpected-target-branch"),
        ({"base_ref": "main"}, "unexpected-target-branch"),
    ],
)
def test_an_unexpected_merge_group_target_selects_the_hub_suite(queue, change, reason):
    (head,) = _enqueue(queue, ["src/brigade/other.py"])
    assert _select(queue, head, queue.main, **change) == {"hub": "true", "reason": reason}
