"""publish-compat must be rerunnable: skip an identical live tarball, refuse a different one."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FEASIBILITY = ROOT / "npm/feasibility"
NAME = "@escoffier-labs/brigade"
VERSION = "0.31.0"

FAKE_NPM = """import json,sys,os
from pathlib import Path
root=Path(os.environ['PUBLISH_FIXTURE']); mode=os.environ['PUBLISH_STATE']
args=sys.argv[1:]
with (root/'calls.jsonl').open('a') as f: f.write(json.dumps(args)+'\\n')
integrity=json.loads((root/'integrity.json').read_text())
live=(root/'published').exists() or mode in ('equal','different','equal-latest-lag','latest-never')
if args[0]=='view':
    if args[2]=='versions':
        print(json.dumps(['0.30.0']+([VERSION] if live else [])));sys.exit(0)
    field=args[2]
    if mode=='unavailable': print(json.dumps({'error':{'code':'E503'}}));sys.exit(1)
    if field=='dist.integrity':
        if not live: print(json.dumps({'error':{'code':'E404'}}));sys.exit(1)
        print(json.dumps('sha512-different' if mode=='different' else integrity))
    elif field=='dist.attestations':
        if not live or mode=='no-attestation': print(json.dumps({'error':{'code':'E404'}}));sys.exit(1)
        print(json.dumps({'url':'https://registry.npmjs.org/-/npm/v1/attestations/'+NAME+'@'+VERSION}))
    elif field=='dist-tags.latest':
        print(json.dumps('0.30.0' if mode=='latest-never' else VERSION))
else:
    assert args[0]=='publish' and '--provenance' in args and '--access' in args and 'public' in args and '--tag' in args
    (root/'published').touch()
    sys.exit(1 if mode=='uncertain' else 0)
""".replace("VERSION", repr(VERSION)).replace("NAME", repr(NAME))


def run(tmp_path, state, *, repository="escoffier-labs/brigadestack", repository_id="1238165856"):
    data = b"compat tarball bytes"
    (tmp_path / "compat.tgz").write_bytes(data)
    (tmp_path / "integrity.json").write_text(
        json.dumps("sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode())
    )
    npm = tmp_path / "npm-fixture.py"
    npm.write_text("#!" + sys.executable + "\n" + FAKE_NPM)
    cli = tmp_path / "npm-cli.cjs"
    cli.write_text(
        "const r=require('node:child_process').spawnSync("
        + json.dumps(sys.executable)
        + ", ["
        + json.dumps(str(npm))
        + ",...process.argv.slice(2)],{stdio:'inherit'});process.exit(r.status ?? 1);"
    )
    result = subprocess.run(
        [
            "node",
            str(FEASIBILITY / "publish-compat.cjs"),
            "--tarball",
            str(tmp_path / "compat.tgz"),
            "--name",
            NAME,
            "--version",
            VERSION,
            "--readback-delays-ms",
            "[0,1,1]",
        ],
        env={
            **os.environ,
            "PUBLISH_FIXTURE": str(tmp_path),
            "PUBLISH_STATE": state,
            "npm_execpath": str(cli),
            "NODE_OPTIONS": "--require=" + str(FEASIBILITY / "fixtures/compat-provenance-preload.cjs"),
            "GITHUB_SHA": "a" * 40,
            "GITHUB_REPOSITORY": repository,
            "GITHUB_REPOSITORY_ID": repository_id,
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    calls = (
        [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
        if (tmp_path / "calls.jsonl").exists()
        else []
    )
    return result, [call for call in calls if call[0] == "publish"], calls


@pytest.mark.parametrize("state", ["absent", "uncertain"])
def test_absent_version_is_published_then_reconciled(tmp_path, state):
    result, publications, calls = run(tmp_path, state)
    assert result.returncode == 0, result.stderr
    assert len(publications) == 1
    assert ["verify"] in calls


def test_identical_live_tarball_skips_the_publish_and_still_verifies_provenance(tmp_path):
    # The recovery case: the publish landed but the first run failed afterwards.
    result, publications, calls = run(tmp_path, "equal")
    assert result.returncode == 0, result.stderr
    assert publications == []
    assert ["verify"] in calls
    assert "publish skipped" in result.stdout


def test_a_different_live_tarball_fails_without_publishing(tmp_path):
    result, publications, _ = run(tmp_path, "different")
    assert result.returncode == 1
    assert publications == []
    assert "different bytes" in result.stderr
    assert "bump" not in result.stderr.lower()


def test_unknown_registry_state_stops_before_any_write(tmp_path):
    result, publications, _ = run(tmp_path, "unavailable")
    assert result.returncode == 1
    assert publications == []
    assert "Cannot establish registry state" in result.stderr


def test_provenance_from_the_wrong_commit_fails(tmp_path):
    result, _, _ = run(tmp_path, "wrong-commit")
    assert result.returncode == 1
    assert "Invalid registry provenance" in result.stderr


def test_missing_attestation_fails_after_bounded_readback(tmp_path):
    result, _, _ = run(tmp_path, "no-attestation")
    assert result.returncode == 1
    assert "Registry reconciliation incomplete" in result.stderr


def test_latest_tag_must_reach_the_version(tmp_path):
    result, _, _ = run(tmp_path, "latest-never")
    assert result.returncode == 1
    assert "dist-tags.latest did not reach" in result.stderr


@pytest.mark.parametrize("repository", ["escoffier-labs/brigade", "escoffier-labs/brigadestack"])
def test_either_brigade_name_publishes_under_the_repository_id(tmp_path, repository):
    result, publications, _ = run(tmp_path, "absent", repository=repository)
    assert result.returncode == 0, result.stderr
    assert len(publications) == 1


@pytest.mark.parametrize(
    ("repository", "repository_id", "message"),
    [
        # A new repository that reuses the old name after the rename carries another ID.
        ("escoffier-labs/brigade", "999", "repository ID"),
        ("escoffier-labs/brigadestack", "", "repository ID"),
        ("someone-else/brigade", "1238165856", "accepted Brigade repository name"),
    ],
)
def test_a_foreign_repository_never_reaches_the_registry(tmp_path, repository, repository_id, message):
    result, publications, calls = run(tmp_path, "absent", repository=repository, repository_id=repository_id)
    assert result.returncode == 1
    assert message in result.stderr
    assert calls == [] and publications == []
