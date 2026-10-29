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
    "graphtrail",
    "graphtrail-mcp",
    "miseledger",
    "sessionfind",
    "brigade-code-graph",
    "brigade-code-graph-mcp",
    "brigade-evidence",
)


def native_names():
    return {
        f"{binary}-{platform}" + (".exe" if platform == "windows-amd64" else "")
        for binary in BINARIES
        for platform in PLATFORMS
    }


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
            "graphtrail",
            "graphtrail-mcp",
            "brigade-code-graph",
            "brigade-code-graph-mcp",
        }
    for binary in ("graphtrail", "graphtrail-mcp", "brigade-code-graph", "brigade-code-graph-mcp"):
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
    for binary in ("miseledger", "sessionfind", "brigade-evidence"):
        assert (
            f'go build -trimpath -o "$GITHUB_WORKSPACE/release-assets/{binary}-${{{{ matrix.platform }}}}$suffix" ./cmd/{binary}'
            in go
        )


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
        # Keep count at 40, so only the duplicate-basename guard can reject it.
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
    assert len(paths) == 41
    assert set(paths) == {f"release-assets/{name}" for name in expected}
    for job in ("assemble-release", "release-asset-gate"):
        assert re.search(r"find release-assets[^\n]+-eq 42", _workflow_job_section(workflow(), job))


@pytest.mark.parametrize(
    "mutation", ["none", "missing-alias", "alias-digest", "checksums-digest", "duplicate", "extra"]
)
def test_release_resume_validates_full_inventory_digests(tmp_path, mutation):
    assets_dir = tmp_path / "assets"
    assets_dir.mkdir()
    names = native_names() | {"component-manifest-v1.json", "checksums.txt"}
    assets = []
    for name in sorted(names):
        data = name.encode()
        (assets_dir / name).write_bytes(data)
        assets.append({"name": name, "digest": "sha256:" + hashlib.sha256(data).hexdigest()})
    alias = "brigade-evidence-windows-amd64.exe"
    if mutation == "missing-alias":
        assets = [asset for asset in assets if asset["name"] != alias]
    elif mutation.endswith("-digest"):
        name = alias if mutation == "alias-digest" else "checksums.txt"
        next(asset for asset in assets if asset["name"] == name)["digest"] = "sha256:" + "0" * 64
    elif mutation == "duplicate":
        assets.append(assets[0])
    elif mutation == "extra":
        assets.append({"name": "unexpected", "digest": "sha256:" + "0" * 64})
    release = tmp_path / "release.json"
    release.write_text(json.dumps({"assets": assets}))
    missing = tmp_path / "missing.txt"
    step = _workflow_step_section(
        workflow(), "create-release", "Create or strictly resume the one immutable Brigade release"
    )
    program = textwrap.dedent(step.split("<<'PY'\n", 1)[1].split("\n          PY", 1)[0])
    result = subprocess.run(
        [sys.executable, "-", str(assets_dir), str(release), str(missing)],
        input=program,
        capture_output=True,
        text=True,
        env=os.environ,
    )
    assert (result.returncode == 0) == (mutation in {"none", "missing-alias"}), result.stderr
    if result.returncode == 0:
        assert missing.read_text().strip() == (alias if mutation == "missing-alias" else "")


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
        "with Path(os.environ['ATTEST_LOG']).open('a') as output:\n"
        "    output.write(json.dumps(args) + '\\n')\n"
    )
    step = _workflow_step_section(
        workflow(), "release-asset-gate", "Redownload, checksum, provenance, and attestation gate"
    )
    loop = textwrap.dedent(step.split("        run: |\n", 1)[1]).split("while read -r", 1)[1]
    script = 'gh() { python3 "$ATTEST_PROBE" "$@"; }\nwhile read -r' + loop
    log = tmp_path / "attestations.jsonl"
    env = dict(
        os.environ,
        GITHUB_SHA="a" * 40,
        GITHUB_REF="refs/tags/v1.2.3",
        GITHUB_REPOSITORY="example/project",
        ATTEST_PROBE=str(probe),
        ATTEST_LOG=str(log),
    )
    result = subprocess.run(_workflow_bash_command(script, "-e"), cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(calls) == 41
    assert {Path(args[2]).name for args in calls} == subjects
