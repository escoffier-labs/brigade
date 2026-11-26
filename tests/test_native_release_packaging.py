"""Local producer contracts only: never invoke publication or native build jobs."""

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import textwrap

import pytest

from tests.test_ci_workflow import _workflow_bash_command, _workflow_job_section, _workflow_step_section

ROOT = Path(__file__).resolve().parents[1]
PLATFORMS = ("linux-amd64", "linux-arm64", "darwin-amd64", "darwin-arm64", "windows-amd64")
BINARIES = (
    "agent-notify",
    "sessionfind",
    "brigade-code-graph",
    "brigade-code-graph-mcp",
    "brigade-evidence",
)


# brigade-hub ships for Linux only.
HUB_PLATFORMS = ("linux-amd64", "linux-arm64")


def native_names():
    return {
        f"{binary}-{platform}" + (".exe" if platform == "windows-amd64" else "")
        for binary in BINARIES
        for platform in PLATFORMS
    } | {f"brigade-hub-{platform}" for platform in HUB_PLATFORMS}


def workflow():
    return (ROOT / ".github/workflows/publish.yml").read_text()


def test_native_builds_compile_and_stage_distinct_targets_for_every_platform():
    rust = _workflow_job_section(workflow(), "build-rust-native")
    assert set(re.findall(r"platform: ([\w-]+)", rust)) == set(PLATFORMS)
    builds = re.findall(r"run: (cargo build [^\n]+)", rust)
    assert len(builds) == 2  # Unix and PowerShell both build the real targets.
    for build in builds:
        args = shlex.split(build)
        assert "--release" in args and "--locked" in args
        assert {args[i + 1] for i, arg in enumerate(args) if arg == "--bin"} == {
            "brigade-code-graph",
            "brigade-code-graph-mcp",
        }
    for binary in ("brigade-code-graph", "brigade-code-graph-mcp"):
        assert (
            f'cp target/release/{binary} "$GITHUB_WORKSPACE/release-assets/{binary}-${{{{ matrix.platform }}}}"' in rust
        )
        assert (
            f'Copy-Item target/release/{binary}.exe "$env:GITHUB_WORKSPACE/release-assets/{binary}-${{{{ matrix.platform }}}}.exe"'
            in rust
        )
    go = _workflow_job_section(workflow(), "build-go-native")
    assert set(re.findall(r"platform: ([\w-]+)", go)) == set(PLATFORMS)
    assert "CGO_ENABLED: '0'" in go
    assert "GOOS: ${{ matrix.goos }}" in go and "GOARCH: ${{ matrix.goarch }}" in go
    assert set(re.findall(r"\./cmd/([\w-]+)", go)) == {"sessionfind", "brigade-evidence"}
    for binary in ("sessionfind", "brigade-evidence"):
        assert (
            f'go build -trimpath -o "$GITHUB_WORKSPACE/release-assets/{binary}-${{{{ matrix.platform }}}}$suffix" ./cmd/{binary}'
            in go
        )


def test_hub_builds_linux_targets_and_runs_prepublication_acceptance_before_staging():
    hub = _workflow_job_section(workflow(), "build-hub-native")
    assert set(re.findall(r"platform: ([\w-]+)", hub)) == set(HUB_PLATFORMS)
    assert "runner: ubuntu-24.04-arm" in hub
    build = re.findall(r"run: (cargo build [^\n]+)", hub)
    assert len(build) == 1
    args = shlex.split(build[0])
    assert "--release" in args and "--locked" in args
    assert args[args.index("--bin") + 1] == "brigade-hub"
    acceptance = hub.index("run: python engines/hub/tests/prepublish.py")
    assert (
        hub.index("cargo build") < acceptance < hub.index('cp engines/hub/target/release/brigade-hub "release-assets/')
    )
    assert 'release-assets/brigade-hub-${{ matrix.platform }}"' in hub
    assert "name: rust-hub-${{ matrix.platform }}" in hub
    assemble = _workflow_job_section(workflow(), "assemble-release")
    assert "build-hub-native" in assemble.split("\n", 2)[1]


@pytest.mark.parametrize("inventory", ["complete", "legacy", "missing", "duplicate"])
def test_assembly_rejects_incomplete_or_duplicate_native_downloads(tmp_path, inventory):
    names = sorted(native_names())
    if inventory == "legacy":
        names = [name for name in names if not name.startswith("brigade-")]
    elif inventory == "missing":
        names.pop()
    for name in names:
        path = tmp_path / "downloaded" / "native" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
    if inventory == "duplicate":
        path = tmp_path / "downloaded" / "other" / names.pop()
        path.parent.mkdir()
        path.write_bytes(b"duplicate")
        # Keep count at 27, so only the duplicate-basename guard can reject it.
        (tmp_path / "downloaded" / "native" / names.pop()).unlink()
    step = _workflow_step_section(workflow(), "assemble-release", "Generate and validate complete release inventory")
    script = textwrap.dedent(step.split("        run: |\n", 1)[1]).split("python scripts/", 1)[0]
    result = subprocess.run(_workflow_bash_command(script, "-e"), cwd=tmp_path, capture_output=True, text=True)
    assert (result.returncode == 0) == (inventory == "complete"), result.stderr
    if inventory == "complete":
        assert {path.name for path in (tmp_path / "release-assets").iterdir()} == native_names()


def test_attestation_covers_every_native_asset_and_primary_manifest():
    assemble = _workflow_job_section(workflow(), "assemble-release")
    subjects = assemble.split("          subject-path: |\n", 1)[1].split("      - uses:", 1)[0]
    paths = [line.strip() for line in subjects.splitlines() if line.strip()]
    expected = native_names() | {"component-manifest-v1.json"}
    assert len(paths) == 28
    assert set(paths) == {f"release-assets/{name}" for name in expected}
    for job in ("assemble-release", "release-asset-gate"):
        assert re.search(r"find release-assets[^\n]+-eq 29", _workflow_job_section(workflow(), job))


@pytest.fixture
def release_cli(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("release job uses a POSIX runner and fake gh shebang")

    from scripts import generate_component_manifest

    monkeypatch.setattr(generate_component_manifest, "ACCEPTED_REPOSITORIES", ("fixture/project",))

    assets_dir = tmp_path / "release-assets"
    assets_dir.mkdir()
    for name in native_names():
        (assets_dir / name).write_bytes(name.encode())
    generate_component_manifest.generate_manifest(
        tag="v1.2.3",
        commit="a" * 40,
        assets_dir=assets_dir,
        output=assets_dir / "component-manifest-v1.json",
        checksums_output=assets_dir / "checksums.txt",
        repository="fixture/project",
    )
    expected = [
        {"name": path.name, "digest": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()}
        for path in sorted(assets_dir.iterdir())
    ]
    gh = tmp_path / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        + textwrap.dedent(r"""
            import hashlib
            import json
            import sys
            from pathlib import Path
            from urllib.parse import parse_qs, urlsplit

            state_file = Path("state.json")
            state = json.loads(state_file.read_text())
            args = sys.argv[1:]
            with Path("calls.jsonl").open("a") as log:
                log.write(json.dumps(args) + "\n")
            releases = state["releases"]
            base = "repos/fixture/project/releases"
            if args[:2] == ["release", "create"]:
                assert args[2:] == [
                    "v1.2.3", "--repo", "fixture/project", "--verify-tag", "--draft",
                    "--title", "v1.2.3", "--notes", "Fixture release",
                ]
                assert not releases
                releases.append({"id": 42, "tag_name": "v1.2.3", "draft": True, "assets": []})
                state_file.write_text(json.dumps(state))
                sys.exit(0)
            assert args[0] == "api"
            endpoint = args[1]
            if endpoint == base + "/tags/v1.2.3":
                assert "--include" in args
                published = next((r for r in releases if not r["draft"]), None)
                status = state.get("status", 200 if published else 404)
                if status == "network":
                    sys.exit("connection failed (message includes HTTP 404)")
                print(f"HTTP/2.0 {status} Fixture\nContent-Type: application/json\n")
                print(json.dumps(published if status == 200 else {"message": "Fixture error"}))
                if status != 200:
                    sys.exit(1)
            elif endpoint == base + "?per_page=100":
                assert args[2:] == ["--paginate", "--slurp"]
                if state.get("deny_list"):
                    sys.exit("gh: Forbidden (HTTP 403)")
                print(json.dumps([[{"id": 8, "tag_name": "v0.0.1", "draft": True}], *([r] for r in releases)]))
            else:
                release = releases[0]
                if endpoint == base + "/42":
                    if "--method" in args:
                        assert args[2:] == ["--method", "PATCH", "-F", "draft=false", "-f", "make_latest=true"]
                        assert release["draft"]
                        release["draft"] = False
                        state_file.write_text(json.dumps(state))
                        if state.get("interrupt"):
                            sys.exit("interrupted after publish")
                    if state.get("omit_last_on_verify"):
                        state["rechecking"] = True
                        state_file.write_text(json.dumps(state))
                    print(json.dumps(release))
                elif endpoint == base + "/42/assets?per_page=100":
                    assert args[2:] == ["--paginate", "--slurp"]
                    assets = release["assets"]
                    if state.get("rechecking"):
                        assets = assets[:-1]
                    print(json.dumps([[asset] for asset in assets] or [[]]))
                else:
                    url = urlsplit(endpoint)
                    assert url.netloc == "uploads.github.com"
                    assert url.path == f"/{base}/42/assets"
                    assert args[2:6] == ["--method", "POST", "-H", "Content-Type: application/octet-stream"]
                    assert args[6] == "--input" and len(args) == 8
                    name = parse_qs(url.query)["name"][0]
                    assert release["draft"] and name not in [asset["name"] for asset in release["assets"]]
                    path = Path(args[7])
                    assert path.name == name
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                    release["assets"].append({"name": name, "digest": "sha256:" + digest})
                    state_file.write_text(json.dumps(state))
            """).lstrip()
    )
    gh.chmod(0o755)
    git = tmp_path / "git"
    git.write_text("#!/bin/sh\nexit 99\n")
    git.chmod(0o755)

    def run(releases, **state):
        (tmp_path / "state.json").write_text(json.dumps({"releases": releases, **state}))
        log = tmp_path / "calls.jsonl"
        log.unlink(missing_ok=True)
        result = subprocess.run(
            [sys.executable, "-I", str(ROOT / "scripts/publish_github_release.py"), "Fixture release"],
            cwd=tmp_path,
            env={"PATH": str(tmp_path), "GITHUB_REPOSITORY": "fixture/project", "TAG": "v1.2.3"},
            capture_output=True,
            text=True,
            timeout=15,
        )
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return result, calls, json.loads((tmp_path / "state.json").read_text())["releases"]

    return expected, run


def _release(assets, *, draft):
    return {"id": 42, "tag_name": "v1.2.3", "draft": draft, "assets": assets}


def _mutations(calls):
    return [call for call in calls if call[:2] == ["release", "create"] or "--method" in call]


@pytest.mark.parametrize("case", ["fresh", "partial-draft", "complete-draft", "published", "interrupted"])
def test_release_publish_and_resume(release_cli, case):
    assets, run = release_cli
    missing = {"fresh": assets, "partial-draft": assets[1:]}.get(case, [])
    existing = (
        []
        if case == "fresh"
        else [_release(assets[:1] if case == "partial-draft" else assets, draft=case != "published")]
    )
    result, calls, releases = run(existing, interrupt=case == "interrupted")
    if case == "interrupted":
        assert "interrupted after publish" in result.stderr
        result, retry_calls, _ = run(releases)
        assert _mutations(retry_calls) == []
    assert result.returncode == 0, result.stderr
    assert releases == [_release(assets, draft=False)]
    mutations = _mutations(calls)
    assert sum(call[:2] == ["release", "create"] for call in mutations) == (case == "fresh")
    assert [Path(call[-1]).name for call in mutations if "POST" in call] == [asset["name"] for asset in missing]
    if case == "published":
        assert mutations == []
    else:
        assert "PATCH" in calls[-1]
        assert calls[-2][1].endswith("/42/assets?per_page=100")
        assert calls[-3] == ["api", "repos/fixture/project/releases/42"]


@pytest.mark.parametrize("case", ["missing", "unexpected", "duplicate", "engine-digest", "checksums-digest"])
def test_bad_published_inventory_never_mutates_release(release_cli, case):
    assets, run = release_cli
    if case == "missing":
        assets.pop()
    elif case == "unexpected":
        assets.append({"name": "unexpected", "digest": "sha256:" + "0" * 64})
    elif case == "duplicate":
        assets.append(assets[0])
    else:
        name = "checksums.txt" if case == "checksums-digest" else "brigade-evidence-windows-amd64.exe"
        next(asset for asset in assets if asset["name"] == name)["digest"] = "sha256:" + "0" * 64
    result, calls, _ = run([_release(assets, draft=False)])
    assert result.returncode != 0
    assert case.split("-")[-1] in result.stderr
    assert calls and _mutations(calls) == []


@pytest.mark.parametrize("status", [401, 403, 429, "network"])
def test_release_lookup_failure_never_creates_a_draft(release_cli, status):
    _, run = release_cli
    result, calls, _ = run([], status=status)
    assert result.returncode != 0
    assert len(calls) == 1 and _mutations(calls) == []


def test_release_list_failure_after_tag_404_never_creates_a_draft(release_cli):
    _, run = release_cli
    result, calls, _ = run([], deny_list=True)
    assert result.returncode != 0
    assert "HTTP 403" in result.stderr
    assert len(calls) == 2 and _mutations(calls) == []


def test_draft_with_mismatched_asset_stops_before_upload(release_cli):
    assets, run = release_cli
    asset = {**assets[0], "digest": "sha256:" + "0" * 64}
    result, calls, _ = run([_release([asset], draft=True)])
    assert result.returncode != 0
    assert "digest differs" in result.stderr
    assert calls and _mutations(calls) == []


def test_draft_stays_unpublished_if_final_inventory_is_incomplete(release_cli):
    assets, run = release_cli
    result, calls, releases = run([_release(assets[:-1], draft=True)], omit_last_on_verify=True)
    assert result.returncode != 0
    assert "missing assets" in result.stderr
    assert [call[3] for call in _mutations(calls)] == ["POST"]
    assert releases[0]["draft"] is True


def test_release_lookup_rejects_two_drafts_for_same_tag(release_cli):
    _, run = release_cli
    result, calls, _ = run([_release([], draft=True), {**_release([], draft=True), "id": 43}])
    assert result.returncode != 0
    assert "multiple releases" in result.stderr
    assert calls and _mutations(calls) == []


def test_release_gate_binds_every_attestation_to_source_commit_and_ref(tmp_path):
    assets = tmp_path / "release-assets"
    assets.mkdir()
    subjects = native_names() | {"component-manifest-v1.json"}
    (assets / "checksums.txt").write_text("".join(f"{'a' * 64}  {name}\n" for name in sorted(subjects)), newline="\n")
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "assert args[:2] == ['attestation', 'verify']\n"
        "for flag, key in [('--source-digest', 'GITHUB_SHA'), ('--source-ref', 'GITHUB_REF')]:\n"
        "    assert flag in args, f'missing attestation constraint: {flag}'\n"
        "    assert args[args.index(flag)+1] == os.environ[key]\n"
        "assert args[-2:] == ['--format', 'json']\n"
        "with Path(os.environ['ATTEST_LOG']).open('a') as output:\n"
        "    output.write(json.dumps(args) + '\\n')\n"
        "certificate = {'sourceRepositoryIdentifier': os.environ.get('ATTEST_CERT_ID', '1238165856')}\n"
        "print(json.dumps([{'verificationResult': {'signature': {'certificate': certificate}}}]))\n"
    )
    step = _workflow_step_section(
        workflow(), "release-asset-gate", "Redownload, checksum, provenance, and attestation gate"
    )
    loop = textwrap.dedent(step.split("        run: |\n", 1)[1]).split("while read -r", 1)[1]
    # The loop also runs the repository's certificate check; run the real script, not a copy.
    script = (
        'gh() { python3 "$ATTEST_PROBE" "$@"; }\npython() { python3 "$VERIFY_SCRIPT" "${@:2}"; }\nwhile read -r' + loop
    )
    log = tmp_path / "attestations.jsonl"
    env = dict(
        os.environ,
        GITHUB_SHA="a" * 40,
        GITHUB_REF="refs/tags/v1.2.3",
        GITHUB_REPOSITORY="example/project",
        RUNNER_TEMP=str(tmp_path),
        VERIFY_SCRIPT=str(ROOT / "scripts/verify_component_manifest_provenance.py"),
        ATTEST_PROBE=str(probe),
        ATTEST_LOG=str(log),
    )
    result = subprocess.run(_workflow_bash_command(script, "-e"), cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(calls) == len(native_names()) + 1
    assert {Path(args[2]).name for args in calls} == subjects


def test_release_gate_rejects_an_attestation_signed_for_another_repository_id(tmp_path):
    assets = tmp_path / "release-assets"
    assets.mkdir()
    (assets / "checksums.txt").write_text(f"{'a' * 64}  component-manifest-v1.json\n", newline="\n")
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import json\n"
        "certificate = {'sourceRepositoryIdentifier': '999'}\n"
        "print(json.dumps([{'verificationResult': {'signature': {'certificate': certificate}}}]))\n"
    )
    step = _workflow_step_section(
        workflow(), "release-asset-gate", "Redownload, checksum, provenance, and attestation gate"
    )
    loop = textwrap.dedent(step.split("        run: |\n", 1)[1]).split("while read -r", 1)[1]
    script = (
        'gh() { python3 "$ATTEST_PROBE" "$@"; }\npython() { python3 "$VERIFY_SCRIPT" "${@:2}"; }\nwhile read -r' + loop
    )
    env = dict(
        os.environ,
        GITHUB_SHA="a" * 40,
        GITHUB_REF="refs/tags/v1.2.3",
        GITHUB_REPOSITORY="example/project",
        RUNNER_TEMP=str(tmp_path),
        VERIFY_SCRIPT=str(ROOT / "scripts/verify_component_manifest_provenance.py"),
        ATTEST_PROBE=str(probe),
    )

    result = subprocess.run(_workflow_bash_command(script, "-e"), cwd=tmp_path, env=env, capture_output=True, text=True)

    assert result.returncode != 0
    assert "repository ID" in result.stderr
