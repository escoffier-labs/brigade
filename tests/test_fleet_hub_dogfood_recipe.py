"""S31 Compose recipe: setup's mode selection (D30), the mode switch, units, the front wrapper and the watchdog.

Nothing here touches the host: GPU and endpoint probes are fakes, and the
only Docker call is `docker compose config`, which validates files and starts
nothing.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import posixpath
import shutil
import stat
import subprocess
import sys
import time
import tomllib
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RECIPE = ROOT / "ops" / "fleet-hub"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


setup = _load("brigade_hub_setup", RECIPE / "bin" / "hub-setup.py")
R1 = json.loads((RECIPE / "r1-models.json").read_text(encoding="utf-8"))
MODEL = R1["models"][0]


def gpu_host(free_mib: int = 20000, runtimes: str = '{"nvidia": {}, "runc": {}}'):
    def run(argv):
        if argv[:2] == ["docker", "info"]:
            return 0, runtimes
        if argv[:2] == ["docker", "run"]:
            assert "--network" in argv and argv[argv.index("--network") + 1] == "none"
            assert "-p" not in argv and "--publish" not in argv
            return 0, f"Example GPU, {free_mib}\n"
        raise AssertionError(argv)

    return run


def no_gpu_host(argv):
    assert argv[:2] == ["docker", "info"], argv
    return 0, '{"runc": {}}'


def endpoint(status=200, dims=MODEL["dimensions"], digest="sha256:" + "a" * 64, header="x-model-digest", body=None):
    calls = []

    def post(url, request, headers):
        calls.append((url, request, headers))
        assert headers["Authorization"].startswith("Bearer ")
        vector = [0.1] * dims
        payload = {"data": [{"embedding": vector}]} if url.endswith("/v1/embeddings") else {"embeddings": [vector]}
        if body is not None:
            payload["model_digest"] = body
        return status, ({header: digest} if digest else {}), payload

    post.calls = calls
    return post


@pytest.fixture
def project(tmp_path):
    target = tmp_path / "ops" / "fleet-hub"
    shutil.copytree(RECIPE, target, ignore=shutil.ignore_patterns(".env", "config", "secrets", "compose.override.yaml"))
    return target


def _init(project, tmp_path, *extra, run=no_gpu_host, post=None, r1=None):
    argv = ["--project-dir", str(project), "init", "--backup-dir", str(tmp_path / "backups"), *extra]
    if r1 is not None:
        argv += ["--r1-file", str(r1)]
    return setup.main(argv, run=run, post=post or endpoint())


def _env(project):
    return setup.read_env(project / ".env")


def _toml(project):
    return (project / "config" / "hub.toml").read_text(encoding="utf-8")


def _token(tmp_path):
    token = tmp_path / "embed.token"
    token.write_text("fake-remote-embed-token\n", encoding="utf-8")
    return token


# ------------------------------------------------------------- mode selection


# Every key the Rust hub's embedder parser reads for an endpoint backend
# (engines/hub/src/memory/embedder.rs, Config::parse). engines/hub/tests/setup_config.py
# starts the hub itself on setup's output.
PARSER_KEYS = {
    "backend",
    "endpoint",
    "allowlist",
    "token_file",
    "shape",
    "name",
    "model_digest",
    "dimensions",
    "input_chars",
    "query_prefix",
    "query_prefix_version",
}


def _embedder(project):
    return tomllib.loads(_toml(project))["memory"]["embedder"]


def test_supported_gpu_writes_gpu_profile_local_backend_and_an_r1_model(project, tmp_path, capsys):
    assert _init(project, tmp_path, run=gpu_host()) == 0
    env = _env(project)
    assert env["COMPOSE_PROFILES"] == "gpu" and env["BRIGADE_RETRIEVAL"] == "hybrid"
    assert env["BRIGADE_EMBED_BACKEND"] == "local" and env["BRIGADE_EMBED_MODEL"] == MODEL["name"]
    toml = _toml(project)
    assert 'retrieval = "hybrid"' in toml and 'backend = "local"' in toml
    assert _embedder(project) == {
        "backend": "local",
        "endpoint": "http://embedder:11434/api/embed",
        "allowlist": ["http://embedder:11434/api/embed"],
        "token_file": "/run/brigade/embed_token",
        "shape": "api/embed",
        "name": MODEL["name"],
        "model_digest": MODEL["model_digest"],
        "dimensions": MODEL["dimensions"],
        "input_chars": MODEL["input_chars"],
        "query_prefix": MODEL["query_prefix"],
        "query_prefix_version": MODEL["query_prefix_version"],
    }
    assert "hybrid, backend local" in capsys.readouterr().out
    # The hub needs a token file for every endpoint, so the local backend gets its own.
    assert len((project / "secrets" / "embed-local-token").read_text(encoding="utf-8").strip()) >= 32
    assert not (project / "secrets" / "embed-token").exists()
    assert "file: ./secrets/embed-local-token" in (project / "compose.override.yaml").read_text(encoding="utf-8")


def test_hybrid_embedder_table_has_exactly_the_parser_keys(project, tmp_path):
    assert _init(project, tmp_path, run=gpu_host()) == 0
    assert set(_embedder(project)) == PARSER_KEYS
    project_remote = tmp_path / "remote" / "ops" / "fleet-hub"
    shutil.copytree(
        RECIPE, project_remote, ignore=shutil.ignore_patterns(".env", "config", "secrets", "compose.override.yaml")
    )
    rc = _init(
        project_remote,
        tmp_path,
        "--no-gpu",
        "--remote-url",
        "https://embed.example.test/",
        "--remote-token-file",
        str(_token(tmp_path)),
        "--remote-api",
        "v1/embeddings",
    )
    assert rc == 0
    table = _embedder(project_remote)
    assert set(table) == PARSER_KEYS
    assert table["endpoint"] == "https://embed.example.test/v1/embeddings" and table["allowlist"] == [table["endpoint"]]
    assert table["shape"] == "v1/embeddings"


def test_r1_file_without_the_schema_2_values_is_refused(project, tmp_path, capsys):
    r1 = tmp_path / "r1.json"
    old = {key: value for key, value in MODEL.items() if key not in ("input_chars", "query_prefix")}
    r1.write_text(json.dumps({"schema": 1, "models": [old]}), encoding="utf-8")
    assert _init(project, tmp_path, run=gpu_host(), r1=r1) == 1
    assert "lacks input_chars, query_prefix" in capsys.readouterr().err


def test_gpu_with_no_r1_model_starts_in_bm25_mode_and_says_why(project, tmp_path, capsys):
    r1 = tmp_path / "r1.json"
    r1.write_text(json.dumps({"schema": 1, "models": [{**MODEL, "r1": "pending"}]}), encoding="utf-8")
    assert _init(project, tmp_path, run=gpu_host(), r1=r1) == 0
    assert _env(project)["COMPOSE_PROFILES"] == "" and 'retrieval = "lexical"' in _toml(project)
    assert "no embedding model has passed R1" in capsys.readouterr().out


def test_card_without_room_falls_through_to_the_remote_endpoint(project, tmp_path, capsys):
    post = endpoint()
    rc = _init(
        project,
        tmp_path,
        "--remote-url",
        "https://embed.example.test",
        "--remote-token-file",
        str(_token(tmp_path)),
        run=gpu_host(free_mib=1000),
        post=post,
    )
    assert rc == 0 and _env(project)["BRIGADE_EMBED_BACKEND"] == "remote"
    out = capsys.readouterr().out
    assert "1000 MiB free" in out


def test_remote_endpoint_without_gpu_writes_remote_and_no_embedder(project, tmp_path):
    post = endpoint()
    rc = _init(
        project,
        tmp_path,
        "--remote-url",
        "http://gpu-host.example.ts.net:11434",
        "--remote-token-file",
        str(_token(tmp_path)),
        post=post,
    )
    assert rc == 0
    env = _env(project)
    assert env["COMPOSE_PROFILES"] == "" and env["BRIGADE_EMBED_BACKEND"] == "remote"
    table = _embedder(project)
    assert table["backend"] == "remote" and table["model_digest"] == "sha256:" + "a" * 64
    assert table["token_file"] == "/run/brigade/embed_token"
    assert table["endpoint"] == "http://gpu-host.example.ts.net:11434/api/embed"
    assert table["allowlist"] == [table["endpoint"]]
    assert post.calls and post.calls[0][0] == table["endpoint"]
    # The probe sends the request the hub sends.
    assert post.calls[0][1] == {"model": MODEL["name"], "input": [setup.PROBE_TEXT], "truncate": False}
    assert (project / "secrets" / "embed-token").read_text(encoding="utf-8").strip() == "fake-remote-embed-token"
    assert "file: ./secrets/embed-token" in (project / "compose.override.yaml").read_text(encoding="utf-8")
    assert not (project / "secrets" / "embed-local-token").exists()


def test_remote_digest_in_the_body_is_accepted(project, tmp_path):
    post = endpoint(digest=None, body="sha256:" + "b" * 64)
    rc = _init(
        project,
        tmp_path,
        "--remote-url",
        "https://embed.example.test",
        "--remote-token-file",
        str(_token(tmp_path)),
        post=post,
    )
    assert rc == 0 and _embedder(project)["model_digest"] == "sha256:" + "b" * 64


@pytest.mark.parametrize(
    ("url", "post", "reason"),
    [
        ("https://embed.example.test", endpoint(digest=None), "reports no model digest"),
        # The hub reads only `x-model-digest`, so a digest under another header would never match.
        ("https://embed.example.test", endpoint(header="x-embedder-model-digest"), "reports no model digest"),
        ("https://embed.example.test", endpoint(body="sha256:" + "c" * 64), "two different model digests"),
        ("https://embed.example.test", endpoint(dims=1024), "1024 dimensions"),
        ("https://embed.example.test", endpoint(status=401), "HTTP 401"),
        ("http://192.0.2.10:11434", endpoint(), "must be HTTPS, or HTTP to a tailnet"),
    ],
)
def test_remote_probe_failure_starts_in_bm25_mode(project, tmp_path, capsys, url, post, reason):
    rc = _init(project, tmp_path, "--remote-url", url, "--remote-token-file", str(_token(tmp_path)), post=post)
    assert rc == 0 and 'retrieval = "lexical"' in _toml(project)
    assert reason in capsys.readouterr().out
    assert not (project / "compose.override.yaml").exists()


def test_neither_prints_each_failed_check_and_never_creates_the_embedder(project, tmp_path, capsys):
    assert _init(project, tmp_path) == 0
    out = capsys.readouterr().out
    assert "BM25 mode (lexical)" in out and "supported configuration" in out
    assert "no GPU container runtime" in out and "no remote endpoint given" in out
    assert "bin/hub-setup.py switch --to hybrid" in out
    assert _env(project)["COMPOSE_PROFILES"] == ""
    assert "[memory.embedder]" not in _toml(project)


def test_secrets_are_private_and_never_overwritten(project, tmp_path):
    assert _init(project, tmp_path) == 0
    folder = project / "secrets"
    first = {name: (folder / name).read_bytes() for name in setup.SECRET_FILES}
    assert len(first["findings-key"]) == 32 and len(first["admin-token.txt"].strip()) >= 32
    if os.name == "posix":
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700
        assert stat.S_IMODE((folder / "findings-key").stat().st_mode) == 0o444
        assert stat.S_IMODE((tmp_path / "backups").stat().st_mode) == 0o700
        assert stat.S_IMODE((tmp_path / "backups" / "recovery-ledger.jsonl").stat().st_mode) == 0o600
    assert _init(project, tmp_path) == 0
    assert {name: (folder / name).read_bytes() for name in setup.SECRET_FILES} == first


posix_modes = pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits are not enforced on Windows")


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_setup_writes_an_owner_only_operator_copy_of_the_admin_token(project, tmp_path):
    assert _init(project, tmp_path) == 0
    folder = project / "secrets"
    operator = folder / setup.OPERATOR_TOKEN
    assert operator.read_bytes() == (folder / "admin-token.txt").read_bytes()
    if os.name == "posix":
        assert _mode(operator) == 0o600
        # The container copy keeps the mode the hub's own UID needs to read the bind mount.
        assert _mode(folder / "admin-token.txt") == 0o444
    # Compose never mounts the operator copy into a container.
    assert setup.OPERATOR_TOKEN not in (RECIPE / "compose.yaml").read_text(encoding="utf-8")
    assert not [p.name for p in folder.iterdir() if p.name.startswith(f".{setup.OPERATOR_TOKEN}")]


def _admin_cli(monkeypatch, tmp_path, command, token_file):
    from brigade import memory_embed
    from brigade.cli import main

    settings = memory_embed.hubc.HubSettings(mode="read", hub_url="https://hub.example.com", token="fake-node-token")
    monkeypatch.setattr(memory_embed.hubc, "load_settings", lambda _: settings)
    seen = []

    class AdminClient:
        def __init__(self, url, credential):
            seen.append(credential)

        def call(self, action, **fields):
            return {"action": action}

    monkeypatch.setattr(memory_embed.hubc, "MemoryHubClient", AdminClient)
    argv = ["memory", command, "--target", str(tmp_path), "--admin-token-file", str(token_file)]
    return main(argv), seen


@pytest.mark.parametrize("command", ["rescan", "reindex"])
def test_memory_rescan_and_reindex_accept_the_operator_copy_setup_writes(project, tmp_path, monkeypatch, command):
    assert _init(project, tmp_path) == 0
    folder = project / "secrets"
    expected = (folder / "admin-token.txt").read_text(encoding="ascii").strip()
    code, seen = _admin_cli(monkeypatch, tmp_path, command, folder / setup.OPERATOR_TOKEN)
    assert code == 0
    assert len(seen) == 1 and seen[0] == expected


@posix_modes
@pytest.mark.parametrize("command", ["rescan", "reindex"])
def test_memory_rescan_and_reindex_still_refuse_the_0444_container_copy(project, tmp_path, monkeypatch, command):
    assert _init(project, tmp_path) == 0
    code, seen = _admin_cli(monkeypatch, tmp_path, command, project / "secrets" / "admin-token.txt")
    assert code == 1 and seen == []


def test_rerunning_setup_adds_the_operator_copy_to_an_existing_install(project, tmp_path, capsys):
    """An install from before the operator copy has only the 0444 container copy."""
    assert _init(project, tmp_path) == 0
    folder = project / "secrets"
    (folder / setup.OPERATOR_TOKEN).unlink()
    container = (folder / "admin-token.txt").read_bytes()
    capsys.readouterr()
    assert _init(project, tmp_path) == 0
    out = capsys.readouterr().out
    assert f"secrets written: {setup.OPERATOR_TOKEN} (mode 0600" in out
    assert container.decode("ascii").strip() not in out
    assert (folder / "admin-token.txt").read_bytes() == container
    assert (folder / setup.OPERATOR_TOKEN).read_bytes() == container
    if os.name == "posix":
        assert _mode(folder / setup.OPERATOR_TOKEN) == 0o600
        assert _mode(folder / "admin-token.txt") == 0o444


@posix_modes
def test_rerunning_setup_resets_a_drifted_operator_copy_mode(project, tmp_path, capsys):
    assert _init(project, tmp_path) == 0
    operator = project / "secrets" / setup.OPERATOR_TOKEN
    operator.chmod(0o444)
    capsys.readouterr()
    assert _init(project, tmp_path) == 0
    assert "(mode reset to 0600)" in capsys.readouterr().out
    assert _mode(operator) == 0o600
    capsys.readouterr()
    assert _init(project, tmp_path) == 0
    assert setup.OPERATOR_TOKEN not in capsys.readouterr().out


def test_rotating_the_container_copy_refreshes_the_operator_copy(project, tmp_path, capsys):
    """Deleting admin-token.txt and rerunning setup rotates the token. The operator copy follows."""
    assert _init(project, tmp_path) == 0
    folder = project / "secrets"
    old = (folder / "admin-token.txt").read_bytes()
    (folder / "admin-token.txt").chmod(0o600)
    (folder / "admin-token.txt").unlink()
    capsys.readouterr()
    assert _init(project, tmp_path) == 0
    out = capsys.readouterr().out
    new = (folder / "admin-token.txt").read_bytes()
    assert new != old
    assert "(refreshed: admin-token.txt changed)" in out
    assert old.decode("ascii").strip() not in out and new.decode("ascii").strip() not in out
    assert (folder / setup.OPERATOR_TOKEN).read_bytes() == new
    if os.name == "posix":
        assert _mode(folder / setup.OPERATOR_TOKEN) == 0o600


def test_setup_refuses_an_operator_copy_that_is_a_symlink(project, tmp_path):
    assert _init(project, tmp_path) == 0
    folder = project / "secrets"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_text("not the token\n", encoding="ascii")
    (folder / setup.OPERATOR_TOKEN).unlink()
    try:
        (folder / setup.OPERATOR_TOKEN).symlink_to(elsewhere)
    except OSError:
        pytest.skip("symlinks are not available")
    assert _init(project, tmp_path) == 1
    assert elsewhere.read_text(encoding="ascii") == "not the token\n"


def _symlink_or_skip(link, target, **kwargs):
    try:
        link.symlink_to(target, **kwargs)
    except OSError:
        pytest.skip("symlinks are not available")


@posix_modes
def test_setup_refuses_a_symlinked_admin_token_source(project, tmp_path, capsys):
    assert _init(project, tmp_path) == 0
    folder = project / "secrets"
    elsewhere = tmp_path / "elsewhere-token"
    elsewhere.write_text("not-the-hub-token\n", encoding="ascii")
    (folder / "admin-token.txt").chmod(0o600)
    (folder / "admin-token.txt").unlink()
    (folder / setup.OPERATOR_TOKEN).unlink()
    _symlink_or_skip(folder / "admin-token.txt", elsewhere)
    capsys.readouterr()
    assert _init(project, tmp_path) == 1
    err = capsys.readouterr().err
    assert "secrets/admin-token.txt is a symlink" in err and "not-the-hub-token" not in err
    assert not (folder / setup.OPERATOR_TOKEN).exists()


@posix_modes
def test_setup_refuses_a_symlinked_secrets_directory(project, tmp_path):
    real = tmp_path / "real-secrets"
    real.mkdir(mode=0o755)
    _symlink_or_skip(project / "secrets", real, target_is_directory=True)
    assert _init(project, tmp_path) == 1
    assert list(real.iterdir()) == []
    assert _mode(real) == 0o755


@posix_modes
@pytest.mark.parametrize("kind", ["fifo", "directory"])
def test_setup_refuses_a_non_regular_operator_copy(project, tmp_path, capsys, kind):
    assert _init(project, tmp_path) == 0
    operator = project / "secrets" / setup.OPERATOR_TOKEN
    operator.unlink()
    if kind == "fifo":
        os.mkfifo(operator, 0o600)
    else:
        operator.mkdir()
    capsys.readouterr()
    # A FIFO must be refused, not opened in a way that blocks setup.
    assert _init(project, tmp_path) == 1
    assert f"secrets/{setup.OPERATOR_TOKEN} is not a regular file" in capsys.readouterr().err


@posix_modes
def test_a_swap_after_the_checks_never_chmods_the_swapped_in_file(project, tmp_path, monkeypatch):
    """Swap the operator copy for a symlink after setup has read it, before the mode repair."""
    assert _init(project, tmp_path) == 0
    folder = project / "secrets"
    operator = folder / setup.OPERATOR_TOKEN
    operator.chmod(0o444)
    victim = tmp_path / "victim"
    victim.write_text("someone else's file\n", encoding="ascii")
    victim.chmod(0o644)
    real_compare = setup.secrets.compare_digest
    swapped = []

    def swap_then_compare(left, right):
        # Both writers compare the copy with the token after reading it and before repairing its mode.
        if not swapped:
            operator.rename(folder / "aside")
            operator.symlink_to(victim)
            swapped.append(True)
        return real_compare(left, right)

    monkeypatch.setattr(setup.secrets, "compare_digest", swap_then_compare)
    setup.ensure_operator_token(folder)
    assert swapped
    assert _mode(victim) == 0o644
    assert victim.read_text(encoding="ascii") == "someone else's file\n"


@posix_modes
@pytest.mark.parametrize("umask", [0o000, 0o277])
def test_the_staged_operator_copy_is_0600_under_any_umask(project, tmp_path, monkeypatch, umask):
    staged = []
    real_replace = os.replace

    def spy(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
        info = os.stat(src, dir_fd=src_dir_fd, follow_symlinks=False)
        staged.append((os.path.basename(src), stat.S_IMODE(info.st_mode)))
        return real_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    assert _init(project, tmp_path) == 0
    folder = project / "secrets"
    (folder / setup.OPERATOR_TOKEN).unlink()
    monkeypatch.setattr(setup.os, "replace", spy)
    # Only the copy's own write runs under the test umask: setup's other files are not the subject here.
    previous = os.umask(umask)
    try:
        assert setup.ensure_operator_token(folder) is not None
    finally:
        os.umask(previous)
    modes = [mode for name, mode in staged if name.startswith(f".{setup.OPERATOR_TOKEN}.")]
    assert modes == [0o600]
    assert _mode(folder / setup.OPERATOR_TOKEN) == 0o600


PROC_FDS = Path("/proc/self/fd")
linux_fds = pytest.mark.skipif(not PROC_FDS.is_dir(), reason="counts descriptors through /proc/self/fd")


def _open_fds():
    return len(os.listdir(PROC_FDS))


def _staging_names(folder):
    return [p.name for p in folder.iterdir() if p.name.startswith(f".{setup.OPERATOR_TOKEN}.")]


@linux_fds
def test_an_fstat_failure_after_open_leaks_no_descriptor(project, tmp_path, monkeypatch):
    assert _init(project, tmp_path) == 0
    folder = project / "secrets"
    before = _open_fds()
    with monkeypatch.context() as patch:

        def broken_fstat(fd):
            raise OSError(5, "simulated fstat failure")

        patch.setattr(setup.os, "fstat", broken_fstat)
        with pytest.raises(OSError, match="simulated fstat failure"):
            setup.ensure_operator_token(folder)
    assert _open_fds() == before


@linux_fds
@pytest.mark.parametrize("step", ["fdopen", "replace"])
def test_a_staging_failure_leaks_no_descriptor_and_no_staging_file(project, tmp_path, monkeypatch, step):
    """Remove the copy so the sync stages a new one, then fail before or at the rename."""
    assert _init(project, tmp_path) == 0
    folder = project / "secrets"
    (folder / setup.OPERATOR_TOKEN).unlink()
    before = _open_fds()
    with monkeypatch.context() as patch:

        def broken(*args, **kwargs):
            raise OSError(5, f"simulated {step} failure")

        patch.setattr(setup.os, step, broken)
        with pytest.raises(OSError, match=f"simulated {step} failure"):
            setup.ensure_operator_token(folder)
    assert _open_fds() == before
    assert _staging_names(folder) == []
    assert not (folder / setup.OPERATOR_TOKEN).exists()


def _runbook_copy_block():
    runbook = (ROOT / "docs" / "runbooks" / "fleet-hub-dogfood.md").read_text(encoding="utf-8")
    start = runbook.index("(\n  set -eu\n")
    return runbook[start : runbook.index("\n)\n", start) + 2]


def test_both_runbooks_carry_the_same_operator_copy_block():
    containers = (ROOT / "docs" / "runbooks" / "fleet-hub-containers.md").read_text(encoding="utf-8")
    assert _runbook_copy_block() in containers


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="runs the runbook's GNU coreutils commands")
@pytest.mark.parametrize("failure", [None, "missing-source", "unreadable-source"])
def test_the_runbook_copy_block_installs_whole_or_leaves_nothing(tmp_path, failure):
    folder = tmp_path / "secrets"
    folder.mkdir(mode=0o700)
    source = folder / "admin-token.txt"
    if failure != "missing-source":
        source.write_text("fake-admin-token\n", encoding="ascii")
        source.chmod(0o000 if failure == "unreadable-source" else 0o444)
    if failure == "unreadable-source" and os.access(source, os.R_OK):
        pytest.skip("this user reads mode-0000 files (root)")
    done = subprocess.run(
        ["bash", "-c", _runbook_copy_block()], cwd=tmp_path, capture_output=True, text=True, timeout=30, check=False
    )
    operator = folder / "operator-admin-token.txt"
    leftovers = [p.name for p in folder.iterdir() if p.name.startswith(".operator-admin-token.")]
    assert leftovers == []
    if failure is None:
        assert done.returncode == 0, done.stderr
        assert operator.read_text(encoding="ascii") == "fake-admin-token\n"
        assert _mode(operator) == 0o600
    else:
        assert done.returncode != 0
        assert not operator.exists()


def test_brigade_ref_needs_its_archive_digest_and_version(project, tmp_path):
    ref, digest = ("--brigade-ref", "0" * 40), ("--brigade-sha256", "f" * 64)
    assert _init(project, tmp_path, *ref) == 1
    assert _init(project, tmp_path, *ref, *digest) == 1
    assert _init(project, tmp_path, *ref, *digest, "--brigade-version", "0.32.0\nX=1") == 1
    assert _init(project, tmp_path, *digest, "--brigade-version", "0.32.0") == 1
    assert not (project / ".env").exists()
    assert _init(project, tmp_path, *ref, *digest, "--brigade-version", "0.32.0") == 0
    env = _env(project)
    assert (env["BRIGADE_REF"], env["BRIGADE_SHA256"], env["BRIGADE_VERSION"]) == ("0" * 40, "f" * 64, "0.32.0")
    # Compose reads all three, so the image label follows the ref.
    compose = (RECIPE / "compose.yaml").read_text(encoding="utf-8")
    assert all(f"${{{name}:-" in compose for name in ("BRIGADE_REF", "BRIGADE_SHA256", "BRIGADE_VERSION"))


# ------------------------------------------------------------------ switching


def test_switch_hybrid_to_lexical_and_back(project, tmp_path):
    assert _init(project, tmp_path, run=gpu_host()) == 0
    secrets_before = (project / "secrets" / "admin-token.txt").read_bytes()
    assert setup.main(["--project-dir", str(project), "switch", "--to", "lexical"], run=gpu_host()) == 0
    assert _env(project)["COMPOSE_PROFILES"] == "" and 'retrieval = "lexical"' in _toml(project)
    assert "BRIGADE_EMBED_MODEL" not in _env(project)
    assert setup.main(["--project-dir", str(project), "switch", "--to", "hybrid"], run=gpu_host()) == 0
    assert _env(project)["COMPOSE_PROFILES"] == "gpu" and 'retrieval = "hybrid"' in _toml(project)
    assert (project / "secrets" / "admin-token.txt").read_bytes() == secrets_before


def test_switch_from_local_to_remote_mounts_the_operator_token(project, tmp_path):
    assert _init(project, tmp_path, run=gpu_host()) == 0
    local = (project / "secrets" / "embed-local-token").read_bytes()
    argv = ["--project-dir", str(project), "switch", "--to", "hybrid", "--no-gpu"]
    argv += ["--remote-url", "https://embed.example.test", "--remote-token-file", str(_token(tmp_path))]
    assert setup.main(argv, run=no_gpu_host, post=endpoint()) == 0
    assert _embedder(project)["backend"] == "remote"
    assert (project / "secrets" / "embed-token").read_text(encoding="utf-8").strip() == "fake-remote-embed-token"
    assert "file: ./secrets/embed-token" in (project / "compose.override.yaml").read_text(encoding="utf-8")
    assert (project / "secrets" / "embed-local-token").read_bytes() == local
    assert setup.main(["--project-dir", str(project), "switch", "--to", "lexical"]) == 0
    assert not (project / "compose.override.yaml").exists()


def _switch_remote(project, url, token, post=None):
    argv = ["--project-dir", str(project), "switch", "--to", "hybrid", "--no-gpu"]
    argv += ["--remote-url", url, "--remote-token-file", str(token)]
    return setup.main(argv, run=no_gpu_host, post=post or endpoint())


def _named_token(tmp_path, name, value):
    token = tmp_path / name
    token.write_text(value + "\n", encoding="utf-8")
    return token


def test_switch_between_remote_endpoints_replaces_the_token(project, tmp_path, capsys):
    token_a = _named_token(tmp_path, "a.token", "token-for-endpoint-a")
    rc = _init(project, tmp_path, "--remote-url", "https://a.example.test", "--remote-token-file", str(token_a))
    assert rc == 0
    post = endpoint()
    assert (
        _switch_remote(
            project, "https://b.example.test", _named_token(tmp_path, "b.token", "token-for-endpoint-b"), post
        )
        == 0
    )
    secret = project / "secrets" / "embed-token"
    # The hub sends this file to endpoint B, so it must hold B's token, never A's.
    assert secret.read_text(encoding="utf-8").strip() == "token-for-endpoint-b"
    scheme, sent = post.calls[0][2]["Authorization"].split(" ", 1)
    assert (scheme, sent) == ("Bearer", "token-for-endpoint-b")
    assert _embedder(project)["endpoint"] == "https://b.example.test/api/embed"
    assert "embed-token (replaced)" in capsys.readouterr().out
    assert not [p.name for p in (project / "secrets").iterdir() if p.name.startswith(".")]
    if os.name == "posix":
        # Readable through the bind mount by the hub container's UID, inside the 0700 directory.
        assert stat.S_IMODE(secret.stat().st_mode) == 0o444
        assert stat.S_IMODE((project / "secrets").stat().st_mode) == 0o700


@pytest.mark.parametrize("before", ["remote", "lexical"])
def test_a_failed_apply_puts_the_previous_remote_token_back(project, tmp_path, monkeypatch, before):
    if before == "remote":
        token_a = _named_token(tmp_path, "a.token", "token-for-endpoint-a")
        assert (
            _init(project, tmp_path, "--remote-url", "https://a.example.test", "--remote-token-file", str(token_a)) == 0
        )
    else:
        assert _init(project, tmp_path) == 0
    secret = project / "secrets" / "embed-token"
    before_bytes = secret.read_bytes() if secret.exists() else None

    def broken_apply(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(setup, "apply", broken_apply)
    assert _switch_remote(project, "https://b.example.test", _named_token(tmp_path, "b.token", "token-for-b")) == 1
    assert (secret.read_bytes() if secret.exists() else None) == before_bytes
    assert not [p.name for p in (project / "secrets").iterdir() if p.name.startswith(".")]


def _tree(*roots):
    """Every path under each root with its bytes and, on POSIX, its mode. A missing root maps to None."""
    seen = {}
    for root in roots:
        if not root.exists():
            seen[str(root)] = None
            continue
        for path in sorted(root.rglob("*")):
            mode = stat.S_IMODE(path.lstat().st_mode) if os.name == "posix" else None
            seen[str(path)] = ("dir", mode) if path.is_dir() else (path.read_bytes(), mode)
    return seen


# Each step setup writes at, as (module function, fail before or after its real work).
WRITE_STEPS = [
    ("write_secret", "after"),
    ("ensure_operator_token", "after"),
    ("_secrets_dir", "after"),
    ("_private_dir", "after"),
    ("_drop_remote_token", "after"),
    ("write_env", "before"),
    ("write_env", "after"),
    ("_compose_override", "after"),
    ("install_probed_token", "after"),
]


def _fail_at(monkeypatch, name, when):
    real = getattr(setup, name)
    reached = []

    def step(*args, **kwargs):
        reached.append(name)
        if when == "after":
            real(*args, **kwargs)
        raise OSError(f"injected failure at {name} ({when})")

    monkeypatch.setattr(setup, name, step)
    return reached


@pytest.mark.parametrize(("name", "when"), WRITE_STEPS)
@pytest.mark.parametrize("command", ["first-init", "re-init", "switch"])
def test_a_failure_at_any_write_step_leaves_the_endpoint_and_token_as_they_were(
    project, tmp_path, monkeypatch, capsys, command, name, when
):
    if (command, name) == ("switch", "_private_dir"):
        pytest.skip("switch has no backup directory, the only folder _private_dir makes")
    backups = tmp_path / "backups"
    if command != "first-init":
        token_a = _named_token(tmp_path, "a.token", "token-for-endpoint-a")
        assert (
            _init(project, tmp_path, "--remote-url", "https://a.example.test", "--remote-token-file", str(token_a)) == 0
        )
    before = _tree(project, backups)
    reached = _fail_at(monkeypatch, name, when)
    token_b = _named_token(tmp_path, "b.token", "token-for-endpoint-b")
    if command == "switch":
        rc = _switch_remote(project, "https://b.example.test", token_b)
    else:
        rc = _init(project, tmp_path, "--remote-url", "https://b.example.test", "--remote-token-file", str(token_b))
    assert reached, f"{name} was never called, so nothing was injected"
    assert rc == 1 and "injected failure" in capsys.readouterr().err
    # Byte-identical files with the same modes, and nothing where nothing was.
    assert _tree(project, backups) == before


ENDPOINT_OF = {
    "token-for-endpoint-a": "https://a.example.test/api/embed",
    "token-for-endpoint-b": "https://b.example.test/api/embed",
}


def _pair_matches(project):
    """The invariant: secrets/embed-token is absent, or hub.toml names the endpoint it was probed against."""
    token = project / "secrets" / "embed-token"
    if not os.path.lexists(token):
        return True
    config = project / "config" / "hub.toml"
    if not config.is_file():
        return False
    embedder = tomllib.loads(config.read_text(encoding="utf-8")).get("memory", {}).get("embedder", {})
    return embedder.get("endpoint") == ENDPOINT_OF.get(token.read_text(encoding="utf-8").strip())


def _remote_a(project, tmp_path):
    token_a = _named_token(tmp_path, "a.token", "token-for-endpoint-a")
    assert _init(project, tmp_path, "--remote-url", "https://a.example.test", "--remote-token-file", str(token_a)) == 0
    assert _pair_matches(project) and (project / "secrets" / "embed-token").exists()


def _to_remote_b(project, tmp_path, command):
    token_b = _named_token(tmp_path, "b.token", "token-for-endpoint-b")
    if command == "switch":
        return _switch_remote(project, "https://b.example.test", token_b)
    return _init(project, tmp_path, "--remote-url", "https://b.example.test", "--remote-token-file", str(token_b))


@pytest.mark.parametrize("command", ["switch", "re-init"])
@pytest.mark.parametrize(
    "death",
    [
        "apply",  # after step (a): the old token is gone, the old config stays
        "write_env",  # inside step (b): hub.toml names endpoint B, no token yet
        "install_probed_token",  # after step (b): the new config, no token yet
    ],
)
def test_process_death_after_any_forward_step_never_leaves_a_mismatched_pair(
    project, tmp_path, monkeypatch, command, death
):
    _remote_a(project, tmp_path)

    def die(*_args, **_kwargs):
        raise SystemExit("simulated process death")

    # A dead process runs no rollback.
    monkeypatch.setattr(setup.SetupFiles, "restore", lambda self: None)
    monkeypatch.setattr(setup, death, die)
    with pytest.raises(SystemExit):
        _to_remote_b(project, tmp_path, command)
    assert _pair_matches(project)


@pytest.mark.parametrize("command", ["switch", "re-init"])
@pytest.mark.parametrize("failing", ["_compose_override", "install_probed_token"])
def test_a_failed_hub_toml_restore_leaves_no_remote_token(project, tmp_path, monkeypatch, capsys, command, failing):
    _remote_a(project, tmp_path)
    _fail_at(monkeypatch, failing, "after")
    real_put = setup._put_file

    def put(path, data, mode):
        if path.name == "hub.toml":
            raise OSError("simulated restore failure")
        real_put(path, data, mode)

    monkeypatch.setattr(setup, "_put_file", put)
    assert _to_remote_b(project, tmp_path, command) == 1
    assert not os.path.lexists(project / "secrets" / "embed-token")
    err = capsys.readouterr().err
    assert "could not put back config/hub.toml" in err and "BM25" in err and "rerun" in err


@pytest.mark.parametrize("command", ["switch", "re-init"])
def test_a_base_exception_inside_the_rollback_leaves_no_remote_token(project, tmp_path, monkeypatch, command):
    _remote_a(project, tmp_path)
    _fail_at(monkeypatch, "_compose_override", "after")
    real_put = setup._put_file

    def put(path, data, mode):
        if path.name == ".env":
            raise KeyboardInterrupt
        real_put(path, data, mode)

    monkeypatch.setattr(setup, "_put_file", put)
    with pytest.raises(KeyboardInterrupt):
        _to_remote_b(project, tmp_path, command)
    assert not os.path.lexists(project / "secrets" / "embed-token")
    # The rest of the rollback still ran: hub.toml names endpoint A again.
    assert _pair_matches(project) and "https://a.example.test/api/embed" in _toml(project)


@pytest.mark.parametrize("command", ["switch", "re-init"])
@pytest.mark.parametrize("raised", [OSError, KeyboardInterrupt])
def test_a_token_that_cannot_be_removed_keeps_this_runs_config(project, tmp_path, monkeypatch, capsys, command, raised):
    _remote_a(project, tmp_path)
    # Endpoint B's token is installed, then the run fails and the rollback cannot remove that token.
    real_install = setup.install_probed_token
    real_remove = setup._remove_file
    installed = []

    def install(*args, **kwargs):
        real_install(*args, **kwargs)
        installed.append(True)
        raise OSError("injected failure after install_probed_token")

    def remove(path):
        if installed and path.name == "embed-token":
            raise raised("simulated unlink failure")
        real_remove(path)

    monkeypatch.setattr(setup, "install_probed_token", install)

    monkeypatch.setattr(setup, "_remove_file", remove)
    if raised is OSError:
        assert _to_remote_b(project, tmp_path, command) == 1
    else:
        with pytest.raises(KeyboardInterrupt):
            _to_remote_b(project, tmp_path, command)
    # Endpoint B's token stays beside endpoint B's config, never beside endpoint A's.
    assert (project / "secrets" / "embed-token").read_text(encoding="utf-8").strip() == "token-for-endpoint-b"
    assert _pair_matches(project) and "https://b.example.test/api/embed" in _toml(project)
    assert "left this run's files in place" in capsys.readouterr().err


def test_leaving_the_remote_backend_removes_its_token(project, tmp_path, capsys):
    _remote_a(project, tmp_path)
    capsys.readouterr()
    assert setup.main(["--project-dir", str(project), "switch", "--to", "lexical"]) == 0
    assert not os.path.lexists(project / "secrets" / "embed-token")
    assert "embed-token removed" in capsys.readouterr().out


@posix_modes
@pytest.mark.parametrize("which", ["config", "backups"])
def test_setup_refuses_a_symlinked_managed_directory_before_writing_anything(project, tmp_path, capsys, which):
    real = tmp_path / f"real-{which}"
    real.mkdir(mode=0o755)
    backups = tmp_path / "backups"
    _symlink_or_skip(project / "config" if which == "config" else backups, real, target_is_directory=True)
    before = _tree(project)
    assert _init(project, tmp_path) == 1
    assert "is a symlink or not a directory" in capsys.readouterr().err
    assert _tree(project) == before
    assert list(real.iterdir()) == [] and stat.S_IMODE(real.stat().st_mode) == 0o755


@posix_modes
def test_windows_read_only_attribute_comes_back_when_the_replace_fails(tmp_path, monkeypatch):
    target = tmp_path / "embed-token"
    target.write_bytes(b"old\n")
    target.chmod(0o444)
    monkeypatch.setattr(setup, "_WINDOWS", True)
    real_replace = setup.os.replace

    def refuse(src, dst):
        if Path(dst) == target:
            raise PermissionError("simulated replace failure")
        real_replace(src, dst)

    monkeypatch.setattr(setup.os, "replace", refuse)
    with pytest.raises(PermissionError):
        setup._put_file(target, b"new\n", 0o400)
    assert stat.S_IMODE(target.stat().st_mode) == 0o444 and target.read_bytes() == b"old\n"
    assert [p.name for p in tmp_path.iterdir()] == ["embed-token"]


@posix_modes
def test_windows_read_only_is_cleared_only_on_a_read_only_destination(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "_WINDOWS", True)
    writable = tmp_path / "writable"
    writable.write_bytes(b"x")
    writable.chmod(0o644)
    chmods = []
    real_chmod = setup.os.chmod
    monkeypatch.setattr(
        setup.os, "chmod", lambda path, mode: (chmods.append((Path(path).name, mode)), real_chmod(path, mode))
    )
    setup._put_file(writable, b"y", 0o600)
    assert [name for name, _ in chmods if name == "writable"] == []
    read_only = tmp_path / "read-only"
    read_only.write_bytes(b"x")
    real_chmod(read_only, 0o444)
    real_unlink = setup.os.unlink
    monkeypatch.setattr(
        setup.os,
        "unlink",
        lambda path: (
            (_ for _ in ()).throw(PermissionError("simulated")) if Path(path) == read_only else real_unlink(path)
        ),
    )
    with pytest.raises(PermissionError):
        setup._remove_file(read_only)
    assert stat.S_IMODE(read_only.stat().st_mode) == 0o444


def test_setup_refuses_a_symlinked_generated_file_before_writing_anything(project, tmp_path, capsys):
    assert _init(project, tmp_path) == 0
    elsewhere = tmp_path / "elsewhere.env"
    elsewhere.write_text("KEEP=1\n", encoding="utf-8")
    (project / ".env").unlink()
    _symlink_or_skip(project / ".env", elsewhere)
    before = _tree(project)
    token_b = _named_token(tmp_path, "b.token", "token-for-endpoint-b")
    assert _init(project, tmp_path, "--remote-url", "https://b.example.test", "--remote-token-file", str(token_b)) == 1
    assert ".env is a symlink" in capsys.readouterr().err
    assert _tree(project) == before and elsewhere.read_text(encoding="utf-8") == "KEEP=1\n"


def test_a_symlinked_remote_token_is_refused(project, tmp_path, capsys):
    assert _init(project, tmp_path) == 0
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_text("untouched\n", encoding="utf-8")
    _symlink_or_skip(project / "secrets" / "embed-token", elsewhere)
    assert _switch_remote(project, "https://b.example.test", _named_token(tmp_path, "b.token", "token-for-b")) == 1
    assert "symlink" in capsys.readouterr().err
    assert elsewhere.read_text(encoding="utf-8") == "untouched\n"


def test_probe_errors_never_quote_the_token(project, tmp_path, capsys):
    token = _named_token(tmp_path, "t.token", "secret-token-text")

    def leaky(url, request, headers):
        raise ValueError(f"Invalid header value {headers['Authorization']!r}")

    def refused(url, request, headers):
        raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))

    for post in (leaky, refused):
        assert (
            _init(
                project,
                tmp_path,
                "--remote-url",
                "https://a.example.test",
                "--remote-token-file",
                str(token),
                post=post,
            )
            == 0
        )
    captured = capsys.readouterr()
    assert "secret-token-text" not in captured.out + captured.err + _toml(project)
    assert "failed: ValueError" in captured.out and "failed: Connection refused" in captured.out


def test_a_token_with_an_inner_line_break_is_refused_before_any_request(project, tmp_path, capsys):
    token = _named_token(tmp_path, "t.token", "first-half\nsecond-half")

    def never(url, request, headers):
        raise AssertionError("no request may carry this token")

    assert (
        _init(
            project, tmp_path, "--remote-url", "https://a.example.test", "--remote-token-file", str(token), post=never
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "whitespace or control characters" in out and "first-half" not in out
    assert 'retrieval = "lexical"' in _toml(project)


def test_switch_to_hybrid_without_a_probed_embedder_changes_nothing(project, tmp_path, capsys):
    assert _init(project, tmp_path) == 0
    before = (_toml(project), (project / ".env").read_text(encoding="utf-8"))
    assert setup.main(["--project-dir", str(project), "switch", "--to", "hybrid"], run=no_gpu_host) == 1
    assert (_toml(project), (project / ".env").read_text(encoding="utf-8")) == before
    assert "mode is unchanged" in capsys.readouterr().err


# ---------------------------------------------------------------------- units


def test_units_render_every_placeholder(project, tmp_path):
    out = tmp_path / "units"
    rc = setup.main(
        [
            "--project-dir",
            str(project),
            "units",
            "--out",
            str(out),
            "--compose-user",
            "operator",
            "--hub-cpus",
            "6-7",
            "--embed-cpus",
            "5",
            "--agent-cpus",
            "0-4,8-23",
            "--db-device",
            "/dev/nvme0n1",
            "--docker",
            "/usr/bin/docker",
        ]
    )
    assert rc == 0
    rendered = {
        path.relative_to(out).as_posix(): path.read_text(encoding="utf-8") for path in out.rglob("*") if path.is_file()
    }
    assert len(rendered) == len(setup.UNIT_TEMPLATES)
    for text in rendered.values():
        assert "@" not in "".join(line for line in text.splitlines() if not line.startswith("#"))
    boot = rendered["brigade-hub-compose.service"]
    assert "BindsTo=docker.service" in boot and "WantedBy=docker.service" in boot
    assert f"compose --project-directory {project} up -d --no-build" in boot
    assert f"ConditionPathExists=!{project}/hub.fenced" in boot
    assert "AllowedCPUs=6-7" in rendered["brigade-hub.slice"]
    assert "IODeviceLatencyTargetSec=/dev/nvme0n1 10ms" in rendered["brigade-hub.slice"]
    # io.latency acts only between peers: agents in user.slice and system.slice
    # compete with brigade.slice, so the target that protects the hub is there.
    assert "IODeviceLatencyTargetSec=/dev/nvme0n1 10ms" in rendered["brigade.slice"]
    assert "AllowedCPUs=0-4,8-23" in rendered["user.slice.d/50-brigade-agent-cpus.conf"]
    assert "OnUnitActiveSec=10s" in rendered["brigade-hub-watchdog.timer"]
    assert "--profile backup run --rm --no-deps backup" in rendered["brigade-hub-backup.service"]


# ---------------------------------------------------------- compose structure


def _compose(project, *profiles):
    argv = ["docker", "compose", "--project-directory", str(project)]
    for profile in profiles:
        argv += ["--profile", profile]
    done = subprocess.run([*argv, "config", "--format", "json"], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@pytest.mark.skipif(
    shutil.which("docker") is None or os.name == "nt", reason="needs docker compose on a Linux-container host"
)
def test_compose_config_holder_layout_no_ports_and_gpu_only_in_the_profile(project, tmp_path):
    assert _init(project, tmp_path) == 0
    default = _compose(project)
    assert set(default["services"]) == {"netns", "hub", "front"}
    full = _compose(project, "gpu", "backup", "model-pull")
    assert set(full["services"]) == {"netns", "hub", "front", "embedder", "backup", "model-pull"}
    # The embedder has no egress; only the one-shot pull reaches a registry.
    assert set(full["services"]["model-pull"]["networks"]) == {"default"}
    assert "deploy" not in full["services"]["model-pull"]
    for name, service in full["services"].items():
        assert not service.get("ports"), name
    assert full["services"]["hub"]["network_mode"] == "service:netns"
    assert full["services"]["front"]["network_mode"] == "service:netns"
    assert full["services"]["netns"]["healthcheck"]["disable"] is True
    assert full["networks"]["embed"]["internal"] is True
    assert set(full["services"]["embedder"]["networks"]) == {"embed"}
    devices = full["services"]["embedder"]["deploy"]["resources"]["reservations"]["devices"]
    assert devices[0]["capabilities"] == ["gpu"]
    for name in ("netns", "hub", "front", "backup", "model-pull"):
        assert "deploy" not in full["services"][name] and not full["services"][name].get("devices"), name
    hub = full["services"]["hub"]
    assert hub["command"][:2] == ["--host", "127.0.0.1"]
    assert hub["environment"]["BRIGADE_FLEET_FINDINGS_KEY_FILE"] == "/run/secrets/findings_key"
    assert {item["source"] for item in hub["secrets"]} == {"admin_token", "findings_key"}
    assert full["services"]["backup"]["network_mode"] == "none"
    # Compose renders the Rust front as written: the dogfood target, the wrapper and memory native.
    assert hub["build"]["target"] == "dogfood"
    assert hub["entrypoint"] == ["python", "/brigade/entry.py"]
    assert hub["environment"]["BRIGADE_HUB_WATCHDOG_NETNS"] == "1"
    assert hub["command"][hub["command"].index("--route-mode") + 1] == "memory=native"
    # #2469: the hub is started on the file setup writes, and has a private tmpfs for its token copy.
    assert hub["command"][hub["command"].index("--memory-config") + 1] == "/etc/brigade/hub.toml"
    assert any(
        (v.get("target") if isinstance(v, dict) else v.split(":")[1]) == "/etc/brigade/hub.toml" for v in hub["volumes"]
    )
    assert any(str(item).startswith("/run/brigade") for item in hub["tmpfs"])
    assert full["services"]["backup"]["entrypoint"] == ["python", "/brigade/backup.py"]
    assert "--db" not in full["services"]["backup"]["command"]


# --------------------------------------------------------- front wrapper


def _wrapper(tmp_path, interfaces, wget_output, wget_rc, front_cmd="sleep 30"):
    net = tmp_path / "net"
    net.mkdir()
    for name in interfaces:
        (net / name).mkdir()
    stub = tmp_path / "stub"
    stub.mkdir()
    (stub / "message").write_text(wget_output + "\n", encoding="utf-8")
    (stub / "wget").write_text(f'#!/bin/sh\ncat "{stub / "message"}" >&2\nexit {wget_rc}\n', encoding="utf-8")
    (stub / "wget").chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{stub}:{os.environ['PATH']}",
        "BRIGADE_NET_DIR": str(net),
        "BRIGADE_FRONT_CMD": front_cmd,
        "BRIGADE_NETNS_CHECK_INTERVAL": "0.05",
    }
    started = time.monotonic()
    done = subprocess.run(
        ["sh", str(RECIPE / "front" / "front-wrapper.sh")], env=env, capture_output=True, text=True, timeout=20
    )
    return done, time.monotonic() - started


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell")
def test_front_wrapper_exits_after_three_loopback_only_checks(tmp_path):
    done, elapsed = _wrapper(tmp_path, ["lo"], "", 0)
    assert done.returncode == 70 and "no network interface but loopback after 3 checks" in done.stderr
    assert elapsed < 10


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell")
def test_front_wrapper_exits_after_three_refused_hub_checks(tmp_path):
    done, _ = _wrapper(tmp_path, ["lo", "eth0"], "wget: can't connect to remote host: Connection refused", 1)
    assert done.returncode == 70 and "refused connections in 3 checks" in done.stderr


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell")
def test_front_wrapper_stays_up_on_a_slow_hub_and_returns_the_front_status(tmp_path):
    done, _ = _wrapper(tmp_path, ["lo", "eth0"], "wget: download timed out", 1, front_cmd="sleep 0.5")
    assert done.returncode == 0 and "front-wrapper:" not in done.stderr


# ----------------------------------------------------------------- watchdog


def _fake_docker(tmp_path, exec_rc: int):
    log = tmp_path / "docker.log"
    script = tmp_path / "docker"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> "{log}"\n'
        'case "$1" in\n'
        "  compose) echo fake-hub-container ;;\n"
        "  inspect) echo true ;;\n"
        f"  exec) exit {exec_rc} ;;\n"
        "  restart) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script, log


def _watchdog(tmp_path, docker):
    env = {**os.environ, "BRIGADE_DOCKER": str(docker), "BRIGADE_WATCHDOG_STATE_DIR": str(tmp_path / "state")}
    env.pop("RUNTIME_DIRECTORY", None)
    return subprocess.run(
        ["sh", str(RECIPE / "bin" / "hub-watchdog.sh")], env=env, capture_output=True, text=True, timeout=20
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell")
@pytest.mark.parametrize(("rc", "reason"), [(10, "/health failed 3 checks"), (11, "no network interface but loopback")])
def test_watchdog_restarts_the_hub_on_the_third_failed_check(tmp_path, rc, reason):
    docker, log = _fake_docker(tmp_path, rc)
    for attempt in range(3):
        done = _watchdog(tmp_path, docker)
        assert done.returncode == 0
        restarted = "restart -t 40 fake-hub-container" in log.read_text(encoding="utf-8")
        assert restarted == (attempt == 2)
    assert reason in done.stderr


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell")
def test_watchdog_resets_its_count_on_a_healthy_check(tmp_path):
    bad, log = _fake_docker(tmp_path, 10)
    _watchdog(tmp_path, bad)
    _watchdog(tmp_path, bad)
    good, _ = _fake_docker(tmp_path, 0)
    _watchdog(tmp_path, good)
    bad, log = _fake_docker(tmp_path, 10)
    _watchdog(tmp_path, bad)
    _watchdog(tmp_path, bad)
    assert "restart" not in log.read_text(encoding="utf-8")


# ------------------------------------------------------- host check helpers

checks = _load("brigade_s31_checks", RECIPE / "checks" / "s31_checks.py")


def test_cpu_lists_and_percentiles():
    assert checks.parse_cpus("0-4,8-23") == set(range(5)) | set(range(8, 24))
    assert checks.parse_cpus("6,7\n") == {6, 7}
    assert checks.percentile([1.0, 2.0, 3.0, 100.0], 0.99) == 100.0


def test_listening_ports_reads_only_listen_rows():
    table = (
        "  sl  local_address rem_address   st\n"
        "   0: 0100007F:0EBE 00000000:0000 0A 00000000\n"
        "   1: 0100007F:A1B2 00000000:0000 0A 00000000\n"
        "   2: 0100007F:0EBE 0100007F:D431 01 00000000\n"
    )
    assert checks.listening_ports(table) == {3774, 0xA1B2}


def _tcp_row(slot: int, address: str, port: int, inode: int) -> str:
    return f"   {slot}: {address}:{port:04X} 00000000:0000 0A 00000000:00000000 00:00000000 00000000 10001 0 {inode} 1"


def _namespace_tables(hub_port: int = 3774) -> str:
    # The live namespace: the hub and its Python upstream on loopback, the front's
    # tailscaled peerapi on every address, and Docker's embedded DNS.
    return "\n".join(
        [
            "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode",
            _tcp_row(0, "0100007F", hub_port, 101),
            _tcp_row(1, "0100007F", 34293, 102),
            _tcp_row(2, "00000000", 45045, 201),
            _tcp_row(3, "0B00007F", 34717, 301),
        ]
    )


NAMESPACE_TABLES = _namespace_tables()


def test_listening_ports_filters_by_socket_inode():
    tables, inodes = checks.split_hub_sockets(NAMESPACE_TABLES + "\n#inodes\n101\n102\n")
    assert inodes == {"101", "102"}
    assert checks.listening_ports(tables) == {3774, 34293, 45045, 34717}
    assert checks.listening_ports(tables, inodes) == {3774, 34293}


def _access(
    monkeypatch,
    *,
    open_ports,
    bindings="{}",
    network_mode="brigade-hub_default",
    inodes="101\n102\n",
    hub_port=3774,
    allow=(),
    proc=None,
    euid=0,
):
    import argparse

    def fake_inspect(_container, template):
        if "IPAddress" in template:
            return "192.0.2.10 "
        if "NetworkMode" in template:
            return network_mode
        if "{{.Image}}" in template:
            return "sha256:hubimage"
        assert "PortBindings" in template
        return bindings

    tables = _namespace_tables(hub_port) + "\n#inodes\n" + inodes

    def fake_run(argv, **_kw):
        if argv[:2] == ["docker", "ps"]:
            return subprocess.CompletedProcess(argv, 0, "c-hub-sibling\n", "")
        return subprocess.CompletedProcess(argv, 0, tables, "")

    monkeypatch.setattr(checks, "HUB_PORT", hub_port)
    monkeypatch.setattr(checks, "container_id", lambda service, project=checks.RECIPE: f"c-{service}")
    monkeypatch.setattr(checks, "inspect", fake_inspect)
    monkeypatch.setattr(checks, "run", fake_run)
    monkeypatch.setattr(checks, "connect_refused", lambda host, port, timeout=2.0: (host, port) not in open_ports)
    monkeypatch.setattr(checks, "front_url", lambda: "https://front.example.test")
    monkeypatch.setattr(checks, "get", lambda url, headers=None, timeout=5: (401, b""))
    # raising=False keeps the helper usable against a check without these knobs.
    monkeypatch.setattr(checks, "PROBE_DEADLINE", 1.0, raising=False)
    monkeypatch.setattr(checks, "IDENTITY_TIMEOUT", 1.0, raising=False)
    if proc is not None:
        monkeypatch.setattr(checks, "PROC", proc, raising=False)
    monkeypatch.setattr(checks, "effective_uid", lambda: euid, raising=False)
    result = checks.Result("access")
    checks.check_access(argparse.Namespace(allow_loopback_listener=list(allow)), result)
    return result


def _http_listener(answer):
    """A real loopback HTTP server; `answer(path, headers)` returns (status, headers, body)."""
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            status, headers, body = answer(self.path, self.headers)
            self.send_response_only(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _raw_listener(serve_one):
    """A loopback TCP listener that hands each accepted connection to `serve_one(conn, stop)`."""
    import socket as socket_module
    import threading

    listener = socket_module.create_server(("127.0.0.1", 0))
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            threading.Thread(target=serve_one, args=(conn, stop), daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()
    return listener, stop


def _loopback_item(result, port):
    (item,) = [item for item in result.items if item["name"] == f"host cannot connect to 127.0.0.1:{port}"]
    return item


PAGE = (200, {"Content-Type": "text/html; charset=utf-8"}, b"<!doctype html><title>app</title>")


def _program(tmp_path, name="app"):
    """A real file standing in for a program, and its canonical path."""
    path = tmp_path / "programs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    return os.path.realpath(path)


def _fake_proc(
    tmp_path,
    port,
    *,
    holder_exe,
    comm="app",
    cgroup="0::/user.slice",
    resolvable=True,
    co_holder=None,
    vanished_pid=False,
):
    """A /proc tree with one LISTEN socket on 127.0.0.1:port held by pid 4242 running `holder_exe`.

    `co_holder` adds pid 4243 holding the same socket: a dict with `exe`,
    `comm`, and `cgroup` (None makes its cgroup unreadable, as a directory).
    `vanished_pid` adds pid 4244 with no fd table, as a process gone mid-scan.
    """
    proc = tmp_path / "proc"
    (proc / "net").mkdir(parents=True)
    (proc / "net" / "tcp").write_text(
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
        + _tcp_row(0, "0100007F", port, 777)
        + "\n",
        encoding="utf-8",
    )
    pid = proc / "4242"
    (pid / "fd").mkdir(parents=True)
    if resolvable:
        os.symlink("socket:[777]", pid / "fd" / "9")
    os.symlink(holder_exe, pid / "exe")
    (pid / "comm").write_text(comm + "\n", encoding="utf-8")
    (pid / "cgroup").write_text(cgroup + "\n", encoding="utf-8")
    if co_holder is not None:
        other = proc / "4243"
        (other / "fd").mkdir(parents=True)
        os.symlink("socket:[777]", other / "fd" / "3")
        os.symlink(co_holder["exe"], other / "exe")
        (other / "comm").write_text(co_holder["comm"] + "\n", encoding="utf-8")
        if co_holder["cgroup"] is not None:
            (other / "cgroup").write_text(co_holder["cgroup"] + "\n", encoding="utf-8")
        else:
            # Unreadable for a reason other than vanishing (IsADirectoryError).
            (other / "cgroup").mkdir()
    if vanished_pid:
        (proc / "4244").mkdir()
    return proc


def _exempted_access(monkeypatch, tmp_path, *, exempt_exe, holder_exe, bindings="{}", euid=0, **proc_kwargs):
    server = _http_listener(lambda _path, _headers: PAGE)
    port = server.server_address[1]
    try:
        result = _access(
            monkeypatch,
            open_ports={("127.0.0.1", port)},
            hub_port=port,
            bindings=bindings,
            euid=euid,
            allow=[f"{port}={exempt_exe}//desktop app on host loopback"],
            proc=_fake_proc(tmp_path, port, holder_exe=holder_exe, **proc_kwargs),
        )
    finally:
        server.shutdown()
        server.server_close()
    return result, _loopback_item(result, port)


def test_access_skips_the_front_peerapi_and_fails_an_unexempted_loopback_listener(monkeypatch):
    # #2305: the front's peerapi on the holder's address is not probed. A host
    # loopback listener on a hub port fails unless the operator exempts it.
    server = _http_listener(lambda _path, _headers: PAGE)
    port = server.server_address[1]
    try:
        result = _access(monkeypatch, open_ports={("127.0.0.1", port), ("192.0.2.10", 45045)}, hub_port=port)
    finally:
        server.shutdown()
        server.server_close()
    probed = {item["name"] for item in result.items if item["name"].startswith("host cannot connect")}
    assert not any(name.endswith(":45045") or name.endswith(":34717") for name in probed)
    item = _loopback_item(result, port)
    assert result.status == "fail" and item["status"] == "FAIL"
    assert item["detail"]["evidence"]["/health"]["status_line"].startswith("HTTP/1.0 200")


posix_proc = pytest.mark.skipif(os.name == "nt", reason="fake /proc needs POSIX symlinks")


@posix_proc
def test_access_exempts_a_loopback_listener_held_by_the_named_program(monkeypatch, tmp_path):
    app = _program(tmp_path)
    result, item = _exempted_access(monkeypatch, tmp_path, exempt_exe=app, holder_exe=app)
    assert result.status == "pass" and item["status"] == "INFO"
    assert item["detail"]["exemption"] == {"exe": app, "reason": "desktop app on host loopback"}
    assert item["detail"]["holders"] == [{"pid": 4242, "exe": app, "comm": "app"}]
    assert item["detail"].get("census") == "complete (euid 0)"


@posix_proc
@pytest.mark.parametrize("holder", ["other-program", "deleted", "renamed-copy"])
def test_access_refuses_an_exemption_when_the_holder_is_not_the_named_program(monkeypatch, tmp_path, holder):
    app = _program(tmp_path)
    holder_exe = {
        "other-program": _program(tmp_path, "socat"),
        # The kernel's exe link for an unlinked binary reads "<path> (deleted)".
        "deleted": app + " (deleted)",
        # The same program copied to another path under another name.
        "renamed-copy": _program(tmp_path / "elsewhere", "app-copy"),
    }[holder]
    result, item = _exempted_access(monkeypatch, tmp_path, exempt_exe=app, holder_exe=holder_exe)
    assert result.status == "fail" and item["status"] == "FAIL"
    assert "not the exempted path" in item["detail"]["exemption_applied"]


@posix_proc
@pytest.mark.parametrize(
    "proc_kwargs, reason",
    [
        ({"resolvable": False}, "could not be identified"),
        ({"cgroup": "0::/system.slice/docker-c-hub-sibling.scope"}, "hub's binary or image"),
    ],
    ids=["unresolved-holder", "hub-image-container"],
)
def test_access_refuses_an_exemption_it_cannot_justify(monkeypatch, tmp_path, proc_kwargs, reason):
    app = _program(tmp_path)
    result, item = _exempted_access(monkeypatch, tmp_path, exempt_exe=app, holder_exe=app, **proc_kwargs)
    assert result.status == "fail" and item["status"] == "FAIL"
    assert reason in item["detail"]["exemption_applied"]


@posix_proc
def test_access_refuses_an_exemption_when_a_co_holder_cannot_be_read(monkeypatch, tmp_path):
    # Two processes share the listening socket. The exempted app reads fine,
    # and the other, here the hub, has an unreadable cgroup: it must not be dropped.
    app = _program(tmp_path)
    hub = _program(tmp_path, "brigade-hub")
    co_holder = {"exe": hub, "comm": "brigade-hub", "cgroup": None}
    result, item = _exempted_access(monkeypatch, tmp_path, exempt_exe=app, holder_exe=app, co_holder=co_holder)
    assert result.status == "fail" and item["status"] == "FAIL"
    assert "could not be identified" in item["detail"]["exemption_applied"]


def test_loopback_holders_fail_closed_without_a_proc_tree(tmp_path):
    # Windows, or any host without /proc: no holder can be named, so no
    # exemption can apply.
    assert checks.loopback_holders(3774, proc=tmp_path / "no-proc") is None
    (tmp_path / "proc" / "net").mkdir(parents=True)
    assert checks.loopback_holders(3774, proc=tmp_path / "proc") is None


@posix_proc
def test_access_refuses_every_exemption_without_root(monkeypatch, tmp_path):
    app = _program(tmp_path)
    result, item = _exempted_access(monkeypatch, tmp_path, exempt_exe=app, holder_exe=app, euid=1000)
    assert result.status == "fail" and item["status"] == "FAIL"
    assert item["detail"]["exemption_applied"] == (
        "no: exemption needs a complete holder census; run the access check as root"
    )


def _readlink_failing_for(monkeypatch, fragment, error):
    """os.readlink raising `error` for any path containing `fragment`, as a hidden fd does."""
    real = os.readlink

    def readlink(path, *args, **kwargs):
        if fragment in str(path):
            raise error
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "readlink", readlink)


CO_HOLDER = {"comm": "app", "cgroup": "0::/user.slice"}


@posix_proc
def test_access_refuses_an_exemption_when_a_co_holder_fd_link_is_hidden(monkeypatch, tmp_path):
    # A non-dumpable child that inherited the socket: its fd links raise PermissionError even for root.
    app = _program(tmp_path)
    _readlink_failing_for(monkeypatch, f"{os.sep}4243{os.sep}fd{os.sep}", PermissionError(13, "denied"))
    result, item = _exempted_access(
        monkeypatch, tmp_path, exempt_exe=app, holder_exe=app, co_holder={"exe": app, **CO_HOLDER}
    )
    assert result.status == "fail" and item["status"] == "FAIL"
    assert "census is incomplete" in item["detail"]["exemption_applied"]
    assert item["detail"]["census"] == "incomplete: pid 4243 fd 3: PermissionError"


@posix_proc
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads any fd directory")
def test_access_refuses_an_exemption_when_a_co_holder_fd_table_is_unreadable(monkeypatch, tmp_path):
    # Another account's process: its whole fd directory is unreadable.
    app = _program(tmp_path)
    proc = None
    server = _http_listener(lambda _path, _headers: PAGE)
    port = server.server_address[1]
    try:
        proc = _fake_proc(tmp_path, port, holder_exe=app, co_holder={"exe": app, **CO_HOLDER})
        os.chmod(proc / "4243" / "fd", 0)
        result = _access(
            monkeypatch,
            open_ports={("127.0.0.1", port)},
            hub_port=port,
            allow=[f"{port}={app}//desktop app on host loopback"],
            proc=proc,
        )
    finally:
        server.shutdown()
        server.server_close()
        if proc is not None:
            os.chmod(proc / "4243" / "fd", 0o700)
    item = _loopback_item(result, port)
    assert result.status == "fail" and "census is incomplete" in item["detail"]["exemption_applied"]


CENSUS_CHANGED_REASON = "no: processes changed during the census; rerun"


@posix_proc
def test_access_refuses_an_exemption_when_an_fd_vanished_during_the_census(monkeypatch, tmp_path):
    app = _program(tmp_path)
    _readlink_failing_for(monkeypatch, f"{os.sep}4243{os.sep}fd{os.sep}", FileNotFoundError(2, "gone"))
    result, item = _exempted_access(
        monkeypatch, tmp_path, exempt_exe=app, holder_exe=app, co_holder={"exe": app, **CO_HOLDER}
    )
    assert result.status == "fail" and item["status"] == "FAIL"
    assert item["detail"]["exemption_applied"] == CENSUS_CHANGED_REASON


@posix_proc
def test_access_refuses_an_exemption_when_a_process_vanished_during_the_census(monkeypatch, tmp_path):
    app = _program(tmp_path)
    result, item = _exempted_access(monkeypatch, tmp_path, exempt_exe=app, holder_exe=app, vanished_pid=True)
    assert result.status == "fail" and item["status"] == "FAIL"
    assert item["detail"]["exemption_applied"] == CENSUS_CHANGED_REASON
    assert item["detail"]["census"] == "changed: pid 4244 vanished"


@posix_proc
def test_access_never_exempts_the_hub_binary_even_when_named(monkeypatch, tmp_path):
    hub = _program(tmp_path, "brigade-hub")
    result, item = _exempted_access(monkeypatch, tmp_path, exempt_exe=hub, holder_exe=hub, comm="brigade-hub")
    assert result.status == "fail" and "hub's binary or image" in item["detail"]["exemption_applied"]


@posix_proc
def test_access_refuses_exemptions_when_any_port_is_published(monkeypatch, tmp_path):
    # A container port published to host loopback 3774 under another key.
    app = _program(tmp_path)
    published = json.dumps({"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "3774"}]})
    result, item = _exempted_access(monkeypatch, tmp_path, exempt_exe=app, holder_exe=app, bindings=published)
    assert result.status == "fail" and item["status"] == "FAIL"
    assert "publishes a port" in item["detail"]["exemption_applied"]


@pytest.mark.parametrize("connect_cost, sends", [(1.5, []), (0.25, [0.75, 0.5])], ids=["spent", "partial"])
def test_probe_connect_and_send_share_one_budget(monkeypatch, connect_cost, sends):
    from types import SimpleNamespace

    clock = [0.0]
    seen: list[float] = []

    class Conn:
        def __init__(self, timeout):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def settimeout(self, value):
            self.timeout = value

        def sendall(self, _data):
            seen.append(round(self.timeout, 6))

        def recv(self, _size):
            return b""

    def connect(_address, timeout):
        clock[0] += connect_cost
        return Conn(timeout)

    monkeypatch.setattr(checks, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(checks, "socket", SimpleNamespace(create_connection=connect))
    evidence = checks.probe_evidence("192.0.2.1", 3774, deadline=1.0)
    assert seen == sends
    if not sends:
        assert evidence["/health"]["ended"] == "deadline"


def test_access_bounds_a_trickling_loopback_listener(monkeypatch):
    import threading

    def trickle(conn, stop):
        with conn:
            conn.recv(4096)
            while not stop.is_set():
                try:
                    conn.sendall(b"H")
                except OSError:
                    return
                stop.wait(0.05)

    listener, stop = _raw_listener(trickle)
    port = listener.getsockname()[1]
    box = {}
    worker = threading.Thread(
        target=lambda: box.setdefault("result", _access(monkeypatch, open_ports={("127.0.0.1", port)}, hub_port=port)),
        daemon=True,
    )
    try:
        worker.start()
        # Both probes share one 1 s deadline; allow generous slack, never the open-ended read.
        worker.join(timeout=8)
        finished = not worker.is_alive()
    finally:
        stop.set()
        listener.close()
    assert finished, "the probe of a trickling listener did not stop at its deadline"
    item = _loopback_item(box["result"], port)
    assert item["status"] == "FAIL"
    assert {probe["ended"] for probe in item["detail"]["evidence"].values()} <= {
        "deadline",
        "deadline before the probe",
    }


# An abortive close (SO_LINGER 0) makes Windows discard received data the
# client has not read yet, so whether the 401 survives the reset is a race there.
@pytest.mark.skipif(os.name == "nt", reason="Windows drops unread data on an incoming RST")
def test_access_keeps_a_401_that_arrived_before_a_reset(monkeypatch):
    import socket as socket_module
    import struct

    def answer_then_reset(conn, _stop):
        conn.recv(4096)
        conn.sendall(b"HTTP/1.1 401 Unauthorized\r\nServer: proxy\r\nContent-Length: 1000\r\n\r\npartial")
        conn.setsockopt(socket_module.SOL_SOCKET, socket_module.SO_LINGER, struct.pack("ii", 1, 0))
        conn.close()

    listener, stop = _raw_listener(answer_then_reset)
    port = listener.getsockname()[1]
    try:
        result = _access(monkeypatch, open_ports={("127.0.0.1", port)}, hub_port=port)
    finally:
        stop.set()
        listener.close()
    item = _loopback_item(result, port)
    assert result.status == "fail" and item["status"] == "FAIL"
    status = item["detail"]["evidence"]["/status"]
    assert status["status_line"] == "HTTP/1.1 401 Unauthorized"
    assert status["headers"]["server"] == "proxy"


@pytest.mark.parametrize(
    "value",
    [
        "3774=desktop app",
        "app=/opt/app//reason",
        "3774=opt/app//reason",
        "3774=/opt/../opt/app//reason",
        "3774=/opt/app/x/.//reason",
        "3774=/opt/app//",
        "3774=/opt/app (deleted)//reason",
        "3774=///reason",
    ],
)
def test_access_rejects_a_malformed_exemption(value):
    with pytest.raises(ValueError):
        checks.parse_exemptions([value])


def test_access_exemption_reason_may_hold_separators():
    assert checks.parse_exemptions(["3774=/opt/app/bin/app//desktop app, see https://example.test/a=b"]) == {
        3774: ("/opt/app/bin/app", "desktop app, see https://example.test/a=b")
    }


def test_access_fails_when_the_hub_port_is_reachable(monkeypatch):
    published = json.dumps({"3774/tcp": [{"HostIp": "127.0.0.1", "HostPort": "3774"}]})
    allow = ["3774=/opt/app/bin/app//claimed exemption"]
    assert _access(monkeypatch, open_ports={("127.0.0.1", 3774)}, bindings=published, allow=allow).status == "fail"
    assert _access(monkeypatch, open_ports={("127.0.0.1", 3774)}, network_mode="host", allow=allow).status == "fail"
    assert _access(monkeypatch, open_ports={("192.0.2.10", 34293)}).status == "fail"


def test_access_probes_every_listener_when_hub_inodes_are_unreadable(monkeypatch):
    result = _access(monkeypatch, open_ports={("192.0.2.10", 45045)}, inodes="")
    assert result.status == "fail"
    assert any(item["name"] == "hub socket inodes unreadable" for item in result.items)


def test_header_echo_verdicts():
    forged = checks.evaluate_echo({"Tailscale-User-Login": checks.FORGED_LOGIN, "X-Forwarded-For": checks.FORGED_FOR})
    assert forged["login_forged"] and forged["client_value_kept"]
    clean = checks.evaluate_echo({"tailscale-user-login": "operator@example.test", "x-forwarded-for": "100.64.0.7"})
    assert not clean["login_forged"] and not clean["client_value_kept"]


def test_copy_verification_and_summary_counts(tmp_path, capsys):
    from brigade import fleet_hub, fleet_hub_backup

    db = tmp_path / "fleet-hub.db"
    fleet_hub.init_db(db).close()
    report = fleet_hub_backup.take_backup(
        [db], tmp_path / "copies", disk_usage=lambda _p: type("U", (), {"free": 10**15})
    )
    files = checks.verify_copy(Path(report["copy"]))
    assert files["fleet-hub.db"]["integrity_check"] == "ok"
    record = tmp_path / "results.jsonl"
    rows = [{"check": "restart", "case": "after-reboot", "status": "pass"}] * 3
    rows += [{"check": "restart", "case": "daemon-restart", "status": "pass"}] * 2
    record.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    assert checks.main(["--record", str(record), "summary"]) == 1
    out = capsys.readouterr().out
    assert "PASS    restart:after-reboot passed at least 3 times: 3" in out
    assert "FAIL    restart:daemon-restart passed at least 3 times: 2" in out


@pytest.mark.skipif(not hasattr(os, "fdatasync"), reason="fdatasync")
def test_fdatasync_probe_records_its_runs(tmp_path, capsys):
    record = tmp_path / "results.jsonl"
    assert (
        checks.main(["--record", str(record), "fdatasync", "--path", str(tmp_path), "--seconds", "0.2", "--runs", "2"])
        == 0
    )
    (line,) = record.read_text(encoding="utf-8").splitlines()
    items = json.loads(line)["items"]
    assert [item["detail"]["run"] for item in items] == [1, 2]
    assert all(item["detail"]["writes"] > 0 for item in items)
    assert not list(tmp_path.glob("s31-fdatasync-*"))


def test_restore_check_creates_its_ledger_and_passes_against_the_real_tool(tmp_path, monkeypatch, capsys):
    import argparse
    import contextlib
    import io

    from brigade import cli, fleet_hub, fleet_hub_backup

    db = tmp_path / "fleet-hub.db"
    fleet_hub.init_db(db).close()
    copies = tmp_path / "copies"
    fleet_hub_backup.take_backup([db], copies, disk_usage=lambda _p: type("U", (), {"free": 10**15}))

    def in_process(argv, *, timeout=120, check=False):
        assert argv[0] == "brigade"
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv[1:])
        return subprocess.CompletedProcess(argv, code, out.getvalue(), err.getvalue())

    monkeypatch.setattr(checks, "run", in_process)
    result = checks.Result("restore")
    checks.check_restore(argparse.Namespace(backup_dir=str(copies), drive=False), result)
    # The docker drive needs a host, so without --drive it stays PENDING.
    failed = [item for item in result.items if item["status"] != "PASS"]
    assert failed == [
        {"status": "PENDING", "name": "full drive against a restored copy", "detail": "rerun with --drive"}
    ]
    assert any(item["name"] == "ledger holds one record per restore" for item in result.items)


def test_drive_mount_keeps_control_brigade_repo_root_depth():
    # control-brigade.py resolves REPO_ROOT as parents[3]. Mounted at /drive it
    # raised IndexError before any probe ran; the mount must keep the checkout's depth.
    from pathlib import PurePosixPath

    script = PurePosixPath(checks.DRIVE) / "control-brigade.py"
    in_repo = (ROOT / "registry" / "skills" / "verify-brigade" / "control-brigade.py").relative_to(ROOT)
    assert script.relative_to(script.parents[3]).as_posix() == in_repo.as_posix()
    source = (RECIPE / "checks" / "s31_checks.py").read_text(encoding="utf-8")
    assert ":/drive:ro" not in source and "/drive/control-brigade.py" not in source


def test_refused_connections_are_counted_through_urlerror():
    import urllib.error

    assert checks.outcome_name(urllib.error.URLError(ConnectionRefusedError(111, "refused"))) == "refused"
    assert checks.outcome_name(ConnectionRefusedError()) == "refused"
    assert checks.outcome_name(urllib.error.URLError(TimeoutError())) == "TimeoutError"


def test_bench_summary_keeps_conditions_and_drops_attempts():
    report = {
        "summary": {"p99_ms": 12.5, "errors": 0},
        "conditions": {"clients": 8},
        "attempts": [{"n": i} for i in range(500)],
    }
    stdout = "server log line\n" + json.dumps(report, indent=2) + "\n"
    kept = checks.bench_summary(stdout)
    assert kept["summary"] == {"p99_ms": 12.5, "errors": 0}
    assert kept["conditions"] == {"clients": 8}
    assert kept["attempt_count"] == 500 and "attempts" not in kept
    assert checks.bench_summary("no json here") == "no json here"


@pytest.mark.skipif(os.name == "nt", reason="runs the recipe command under /bin/sh")
def test_model_pull_stops_the_server_by_pid_and_keeps_the_pull_status(tmp_path):
    # A noninteractive dash has no job control, so `kill %1` failed after a
    # successful pull. Run the recipe's command under sh with stand-ins.
    # PyYAML is not a dependency, so read the one command line directly.
    lines = (RECIPE / "compose.yaml").read_text(encoding="utf-8").splitlines()
    start = lines.index("  model-pull:")
    marker = next(i for i in range(start, len(lines)) if lines[i].strip() == "command:")
    command = lines[marker + 1].strip().removeprefix("- ").replace("$$", "$")
    assert "%1" not in command
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for pull_status in (0, 7):
        (bin_dir / "ollama").write_text(
            f'#!/bin/sh\nif [ "$1" = serve ]; then exec sleep 60; fi\nexit {pull_status}\n',
            encoding="utf-8",
        )
        (bin_dir / "ollama").chmod(0o755)
        (bin_dir / "sleep").write_text('#!/bin/sh\nexec /bin/sleep "$@"\n', encoding="utf-8")
        (bin_dir / "sleep").chmod(0o755)
        env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "BRIGADE_EMBED_MODEL": "m"}
        done = subprocess.run(["/bin/sh", "-c", command.replace("sleep 3", "sleep 0.2")], env=env, timeout=30)
        assert done.returncode == pull_status


def test_restore_drive_gives_the_hub_and_the_drive_the_same_operator():
    import shlex

    script = checks.restore_drive_script("agent:restore-drive-ab12cd34", 8589934592)
    serve, drive = script.split(" & ", 1)
    assert shlex.split(serve)[0] == "BRIGADE_FLEET_OPERATOR_RECIPIENT=agent:restore-drive-ab12cd34"
    words = shlex.split(drive)
    assert words[words.index("--operator-recipient") + 1] == "agent:restore-drive-ab12cd34"
    # A restore raised the claim recovery floor, so the drive must be told it.
    assert words[words.index("--claim-floor") + 1] == "8589934592"


def test_restore_drive_reads_the_floor_the_restore_wrote(tmp_path):
    from brigade import fleet_hub, fleet_hub_claims

    fresh = tmp_path / "fresh.db"
    fleet_hub.init_db(fresh).close()
    assert checks.claim_floor(fresh) == 0
    restored = tmp_path / "restored.db"
    conn = fleet_hub.init_db(restored)
    try:
        conn.execute("BEGIN IMMEDIATE")
        floor = fleet_hub_claims.write_recovery_floor(conn, 0, advance=2**32, restored_at="t", copy_sha256="x")
        conn.commit()
        assert fleet_hub_claims.recovery_floor(conn) == floor
    finally:
        conn.close()
    assert checks.claim_floor(restored) == floor == 2**32


@pytest.mark.skipif(os.name == "nt", reason="runs the watchdog under /bin/sh")
def test_watchdog_never_restarts_a_fenced_hub(tmp_path):
    (tmp_path / "hub.fenced").write_text("", encoding="utf-8")
    docker = tmp_path / "docker"
    docker.write_text('#!/bin/sh\necho "$@" >> "$0.calls"\n', encoding="utf-8")
    docker.chmod(0o755)
    env = {
        "PATH": "/usr/bin:/bin",
        "BRIGADE_DOCKER": str(docker),
        "BRIGADE_HUB_PROJECT_DIR": str(tmp_path),
        "BRIGADE_WATCHDOG_STATE_DIR": str(tmp_path / "state"),
    }
    done = subprocess.run(["/bin/sh", str(RECIPE / "bin" / "hub-watchdog.sh")], env=env, timeout=30)
    assert done.returncode == 0
    assert not (tmp_path / "docker.calls").exists()


def test_restore_updates_the_hub_on_loopback_before_clients_reconnect():
    runbook = (ROOT / "docs" / "runbooks" / "fleet-hub-dogfood.md").read_text(encoding="utf-8")
    restore = runbook[runbook.index("1. Fence the hub:") : runbook.index("Rehearse the restore monthly")]
    fenced_start = restore.index("docker compose up -d --no-build --no-deps hub")
    loopback_update = restore.index("BRIGADE_FLEET_HUB_URL=http://127.0.0.1:3774")
    revocations = restore.index("Re-apply every credential revocation")
    unfence = restore.index("rm hub.fenced && docker compose up -d --no-build")
    # The hub runs alone, without its front, while the fence holds and the
    # revocations go in; only then does the fence come off.
    assert fenced_start < loopback_update < unfence
    assert fenced_start < revocations < unfence
    assert "`front` stays stopped" in restore[fenced_start:unfence]


def _log_fakes(monkeypatch, logs, started="2026-11-04T10:00:00.5Z"):
    from types import SimpleNamespace

    calls = []

    def run(argv, **_):
        calls.append(argv)
        if argv[:2] == ["docker", "logs"]:
            # `--until <start>` drops the current run's lines.
            text = logs.split("CURRENT RUN\n")[0] if "--until" in argv else logs.replace("CURRENT RUN\n", "")
            return SimpleNamespace(stdout=text, stderr="", returncode=0)
        if argv[:2] == ["docker", "inspect"]:
            return SimpleNamespace(stdout=started + "\n", stderr="", returncode=0)
        # `compose ps -a -q hub` still lists the stopped container.
        return SimpleNamespace(stdout="abc123\n" if "-a" in argv else "", stderr="", returncode=0)

    monkeypatch.setattr(checks, "run", run)
    return calls


START = "brigade fleet hub listening on 127.0.0.1:3774 (db x)\n"
STOP = "brigade fleet hub stopped cleanly\n"


def test_clean_exit_check_reads_a_stopped_container(monkeypatch):
    calls = _log_fakes(monkeypatch, START + STOP)
    assert checks.clean_exit_logged(container="known") is True
    assert calls[-1][-1] == "known"
    assert checks.clean_exit_logged() is True
    assert calls[-1][-1] == "abc123"


def test_an_earlier_clean_stop_does_not_vouch_for_a_killed_run(monkeypatch):
    # Run 1 stopped cleanly; run 2 was killed in the shutdown; run 3 is the current one.
    logs = START + STOP + START + "CURRENT RUN\n" + START
    _log_fakes(monkeypatch, logs)
    assert checks.clean_exit_logged(before_current_run=True) is False
    assert checks.clean_exit_logged() is False


def test_the_run_before_the_current_start_must_end_cleanly(monkeypatch):
    logs = START + "\n" + START + STOP + "CURRENT RUN\n" + START
    calls = _log_fakes(monkeypatch, logs)
    assert checks.clean_exit_logged(before_current_run=True) is True
    assert ["docker", "logs", "--until", "2026-11-04T10:00:00.5Z", "abc123"] in calls
    # Without the boundary the current run, still serving, has not stopped.
    assert checks.clean_exit_logged() is False


def _hub_kill_fakes(monkeypatch, *, restarted: bool):
    from types import SimpleNamespace

    calls = []
    state = {"killed": False}

    def run(argv, **_):
        calls.append(argv)
        if argv[:2] == ["docker", "exec"]:
            state["killed"] = True
            return SimpleNamespace(stdout="7\n", stderr="", returncode=0)
        if argv[:2] == ["docker", "inspect"] and argv[3] == "{{.RestartCount}}":
            count = "1" if state["killed"] and restarted else "0"
            return SimpleNamespace(stdout=count + "\n", stderr="", returncode=0)
        if argv[:2] == ["docker", "inspect"] and argv[3] == "{{.State.Running}}":
            return SimpleNamespace(stdout="false\n", stderr="", returncode=0)
        if "ps" in argv:
            return SimpleNamespace(stdout=f"{argv[-1]}-cid\n", stderr="", returncode=0)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(checks, "run", run)
    monkeypatch.setattr(checks, "wait_for_front", lambda limit: 2.0 if restarted else None)
    monkeypatch.setattr(checks, "one_namespace", lambda result: None)
    clock = {"now": 0.0}

    def sleep(seconds):
        clock["now"] += seconds

    monkeypatch.setattr(checks, "time", SimpleNamespace(monotonic=lambda: clock["now"], sleep=sleep))
    return calls


def test_hub_kill_signals_the_rust_front_and_never_uses_docker_kill(monkeypatch, capsys):
    import argparse

    calls = _hub_kill_fakes(monkeypatch, restarted=True)
    result = checks.Result("restart")
    checks.check_restart(argparse.Namespace(case="hub-kill"), result)
    assert not any(argv[:2] == ["docker", "kill"] for argv in calls)
    execs = [argv for argv in calls if argv[:2] == ["docker", "exec"]]
    assert execs == [["docker", "exec", "hub-cid", "python", "-c", checks.KILL_HUB]]
    script = checks.KILL_HUB
    compile(script, "kill-hub", "exec")
    # The front owns the port; the entrypoint wrapper and the Python upstream are not it.
    assert "signal.SIGKILL" in script and 'os.path.basename(argv[0]) == b"brigade-hub"' in script
    assert 'split(b"\\0")' in script
    # PID 1 (docker-init) ignores SIGKILL from inside its namespace, so it is skipped.
    assert "(1, me)" in script
    assert result.status == "pass"
    assert not any("up" in argv for argv in calls)


def test_hub_kill_restores_service_when_the_container_does_not_restart(monkeypatch, capsys):
    import argparse

    calls = _hub_kill_fakes(monkeypatch, restarted=False)
    result = checks.Result("restart")
    checks.check_restart(argparse.Namespace(case="hub-kill"), result)
    assert result.status == "fail"
    assert calls[-1][-5:] == ["up", "-d", "--no-build", "hub", "front"]


# ------------------------------------------------- Rust hub in the recipe (S31c)


def _stages(text):
    """`FROM` lines of the Containerfile in order: (image, stage name or None)."""
    stages = []
    for line in text.splitlines():
        if line.startswith("FROM "):
            parts = line.split()
            stages.append((parts[1], parts[3] if len(parts) == 4 and parts[2] == "AS" else None))
    return stages


def _stage_body(text, name):
    start = text.index(f" AS {name}\n")
    following = text.find("\nFROM ", start)
    return text[start : following if following != -1 else len(text)]


def _service_list(service, key):
    """The list items under `key:` of one compose service, read without a YAML parser."""
    text = (RECIPE / "compose.yaml").read_text(encoding="utf-8")
    block = text[text.index(f"\n  {service}:\n") :]
    block = block[: block.index("\n\n")]
    items, inside = [], False
    for line in block.splitlines():
        if line == f"    {key}:":
            inside = True
        elif inside and line.startswith("      - "):
            items.append(line[len("      - ") :].strip('"'))
        elif inside and not line.startswith("      #"):
            inside = False
    return items


def test_containerfile_builds_brigade_hub_in_a_pinned_rust_stage_and_copies_only_the_binary():
    text = (RECIPE / "Containerfile").read_text(encoding="utf-8")
    stages = _stages(text)
    names = [name for _, name in stages]
    assert names == ["source", "hub-build", "python-hub", "dogfood", None]
    images = dict((name, image) for image, name in stages)
    rust_version = (ROOT / "engines" / "hub" / "Cargo.toml").read_text(encoding="utf-8").split('rust-version = "')[1]
    rust_version = rust_version.split('"')[0]
    # The toolchain by exact version, every base image by digest.
    assert images["hub-build"].startswith(f"rust:{rust_version}.0-slim-bookworm@sha256:")
    for image, name in stages[:3]:
        tag, _, digest = image.partition("@sha256:")
        assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest), name
    assert images["source"] == images["python-hub"]
    # The Rust stage builds from the same checked archive as the Python hub.
    source = _stage_body(text, "source")
    assert "hashlib.sha256(data).hexdigest() == '${BRIGADE_SHA256}'" in source
    assert "archive/${BRIGADE_REF}.tar.gz" in source
    build = _stage_body(text, "hub-build")
    assert "COPY --from=source /src /src" in build
    assert "cargo build --locked --release --bin brigade-hub" in build
    assert "sha256sum target/release/brigade-hub > target/release/brigade-hub.sha256" in build
    dogfood = _stage_body(text, "dogfood")
    assert images["dogfood"] == "python-hub"
    copies = [line for line in dogfood.splitlines() if line.startswith("COPY ")]
    # The binary and the checksum `fleet serve --engine rust` checks it against.
    assert copies == [
        "COPY --from=hub-build /src/engines/hub/target/release/brigade-hub /usr/local/bin/brigade-hub",
        "COPY --from=hub-build /src/engines/hub/target/release/brigade-hub.sha256"
        " /usr/local/share/brigade/brigade-hub.sha256",
    ]
    assert (entry.ENGINE_BINARY, entry.ENGINE_SUM) == (
        "/usr/local/bin/brigade-hub",
        "/usr/local/share/brigade/brigade-hub.sha256",
    )
    # The dogfood stage keeps the Python image's entrypoint; compose runs hub/entry.py.
    assert "ENTRYPOINT" not in dogfood
    assert "ENV BRIGADE_HUB_PYTHON=/usr/local/bin/python3" in dogfood
    # The default target is the Python-only image. A builder that skips
    # unused stages builds it without compiling Rust; the legacy builder does not.
    assert stages[-1] == ("python-hub", None)
    assert "DOCKER_BUILDKIT=0" in text
    assert 'ENTRYPOINT ["brigade", "fleet", "serve"]' in _stage_body(text, "python-hub")


def test_compose_hub_runs_brigade_hub_with_the_s31_flags_and_memory_native():
    text = (RECIPE / "compose.yaml").read_text(encoding="utf-8")
    assert "      target: dogfood\n" in text
    assert '    entrypoint: ["python", "/brigade/entry.py"]\n' in text
    assert "      - ./hub:/brigade:ro\n" in text
    command = _service_list("hub", "command")
    assert command[:2] == ["--host", "127.0.0.1"]
    assert command[command.index("--db") + 1] == "/var/lib/brigade/fleet-hub.db"
    assert command[command.index("--token-file") + 1] == "/run/secrets/admin_token"
    assert "--trust-forwarded-proto" in command
    modes = [command[i + 1] for i, item in enumerate(command) if item == "--route-mode"]
    # Memory is the only family the dogfood hub moves; the rest keep their defaults.
    assert modes == ["memory=native"]
    assert "--upstream" not in command
    assert "BRIGADE_FLEET_FINDINGS_KEY_FILE: /run/secrets/findings_key" in text
    assert "    stop_grace_period: 40s\n" in text
    # The default source pin has the Rust hub's memory route and S31's backup.
    ref = text.split("BRIGADE_REF: ${BRIGADE_REF:-")[1].split("}")[0]
    digest = text.split("BRIGADE_SHA256: ${BRIGADE_SHA256:-")[1].split("}")[0]
    assert len(ref) == 40 and len(digest) == 64
    assert ref != "e6009f6969dc6e45db2ab93790aa3e407c791409"


def test_containerfile_stage_defaults_match_the_compose_pin():
    # `docker build --target dogfood` with no build args gets the ref Compose
    # builds, so the Rust stage finds engines/hub and both hubs come from one archive.
    text = (RECIPE / "Containerfile").read_text(encoding="utf-8")
    compose_text = (RECIPE / "compose.yaml").read_text(encoding="utf-8")
    pins = {}
    for name in ("BRIGADE_REF", "BRIGADE_SHA256", "BRIGADE_VERSION"):
        pins[name] = compose_text.split(f"{name}: ${{{name}:-")[1].split("}")[0]
    for stage in ("source", "python-hub"):
        body = _stage_body(text, stage)
        assert f"ARG BRIGADE_REF={pins['BRIGADE_REF']}\n" in body, stage
        assert f"ARG BRIGADE_SHA256={pins['BRIGADE_SHA256']}\n" in body, stage
    assert f"ARG BRIGADE_VERSION={pins['BRIGADE_VERSION']}\n" in _stage_body(text, "python-hub")
    assert pins["BRIGADE_REF"] == "d3b38d4b6d380f01c21bbaaa66d4a1c857250a1d"
    assert pins["BRIGADE_SHA256"] == "1106c7c75299720c04e34214535638afd551c13b4ac6803a57bba3fa8dd41061"


backup_job = _load("brigade_hub_backup_job", RECIPE / "hub" / "backup.py")


def _hub_files(data, *, hub=True, memory=True):
    import sqlite3

    from brigade import fleet_hub

    data.mkdir()
    if hub:
        fleet_hub.init_db(data / "fleet-hub.db").close()
    if memory:
        conn = sqlite3.connect(data / "fleet-hub-memory.db")
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE memory_meta (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute("INSERT INTO memory_meta VALUES ('log_epoch', 'fake-epoch')")
            conn.commit()
        finally:
            conn.close()


def _run_backup_job(tmp_path, data):
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), os.environ.get("PYTHONPATH", "")])}
    argv = ["--state-dir", str(data), "--dest", str(tmp_path / "copies"), "--min-free-gib", "0", "--json"]
    return subprocess.run(
        [sys.executable, str(RECIPE / "hub" / "backup.py"), *argv],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_backup_job_copies_each_file_in_its_own_call(tmp_path):
    import sqlite3

    command = _service_list("backup", "command")
    assert command[:2] == ["--dest", "/backups"] and "--db" not in command
    assert (
        "      - ./hub:/brigade:ro\n" in (RECIPE / "compose.yaml").read_text(encoding="utf-8").split("\n  backup:\n")[1]
    )
    calls = backup_job.calls(Path("/var/lib/brigade"), Path("/backups"), ["--json"])
    # Compared as paths: the job runs in the Linux image, the test on any host.
    assert [Path(argv[argv.index("--db") + 1]) for _, argv in calls] == [
        Path("/var/lib/brigade/fleet-hub.db"),
        Path("/var/lib/brigade/fleet-hub-memory.db"),
    ]
    assert [Path(argv[argv.index("--dest") + 1]) for _, argv in calls] == [Path("/backups"), Path("/backups/memory")]
    data = tmp_path / "data"
    _hub_files(data)
    done = _run_backup_job(tmp_path, data)
    assert done.returncode == 0, done.stderr
    hub_copies = checks.completed_copies(tmp_path / "copies")
    memory_copies = checks.completed_copies(tmp_path / "copies" / "memory")
    assert len(hub_copies) == 1 and len(memory_copies) == 1
    # Each copy holds its own file, checked by the verifier the S31 check uses.
    assert set(checks.verify_copy(hub_copies[0])) == {"fleet-hub.db"}
    files = checks.verify_copy(memory_copies[0])
    assert set(files) == {"fleet-hub-memory.db"} and files["fleet-hub-memory.db"]["integrity_check"] == "ok"
    copied = sqlite3.connect(memory_copies[0] / "fleet-hub-memory.db")
    try:
        assert copied.execute("SELECT value FROM memory_meta").fetchone() == ("fake-epoch",)
    finally:
        copied.close()


def test_backup_job_skips_a_missing_memory_file_and_still_copies_the_hub_file(tmp_path):
    data = tmp_path / "data"
    _hub_files(data, memory=False)
    done = _run_backup_job(tmp_path, data)
    assert done.returncode == 0, done.stderr
    assert f"memory backup skipped: {data / 'fleet-hub-memory.db'} does not exist" in done.stderr
    assert len(checks.completed_copies(tmp_path / "copies")) == 1
    assert checks.completed_copies(tmp_path / "copies" / "memory") == []


def test_backup_job_still_copies_memory_when_the_hub_copy_fails(tmp_path):
    data = tmp_path / "data"
    _hub_files(data, hub=False)
    done = _run_backup_job(tmp_path, data)
    assert done.returncode != 0
    assert "fleet-hub.db does not exist" in done.stderr
    assert checks.completed_copies(tmp_path / "copies") == []
    assert len(checks.completed_copies(tmp_path / "copies" / "memory")) == 1


def test_memory_file_probe_asks_the_hub_container(monkeypatch):
    from types import SimpleNamespace

    calls = []

    def run(argv, **_):
        calls.append(argv)
        if argv[:2] == ["docker", "exec"]:
            return SimpleNamespace(stdout="True\n", stderr="", returncode=0)
        return SimpleNamespace(stdout="hub-cid\n", stderr="", returncode=0)

    monkeypatch.setattr(checks, "run", run)
    assert checks.memory_file_present() is True
    assert calls[-1] == ["docker", "exec", "hub-cid", "python", "-c", checks.MEMORY_FILE_PRESENT]
    assert '"/var/lib/brigade/fleet-hub-memory.db"' in checks.MEMORY_FILE_PRESENT
    monkeypatch.setattr(checks, "container_id", lambda *_a, **_k: "")
    assert checks.memory_file_present() is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
def test_backup_job_reports_a_signal_death_as_a_shell_would():
    killed = [sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"]
    assert backup_job.status(killed) == 137
    assert backup_job.status([sys.executable, "-c", "raise SystemExit(3)"]) == 3


def test_backup_job_refuses_a_db_argument():
    with pytest.raises(SystemExit):
        backup_job.main(["--db", "/elsewhere.db"])


@pytest.mark.skipif(os.name == "nt", reason="the S31 backups check reads POSIX owners and modes on the Linux host")
def test_backups_check_reads_hub_and_memory_copies_apart(tmp_path, monkeypatch):
    import argparse

    data = tmp_path / "data"
    _hub_files(data, memory=False)
    assert _run_backup_job(tmp_path, data).returncode == 0
    folder = tmp_path / "copies"
    monkeypatch.setattr(checks, "env_file", lambda *_a, **_k: {"BRIGADE_BACKUP_OWNER": f"{os.getuid()}:{os.getgid()}"})
    result = checks.Result("backups")
    include = tmp_path / "include.txt"
    include.write_text(f"{folder}\n", encoding="utf-8")
    namespace = argparse.Namespace(backup_dir=str(folder), last=3, include_file=str(include), refusal=False)
    # No memory copy passes only while the memory file is absent from the volume.
    for present, expected, status in ((False, "INFO", "pass"), (True, "FAIL", "fail"), (None, "PENDING", "pending")):
        monkeypatch.setattr(checks, "memory_file_present", lambda present=present: present)
        result = checks.Result("backups")
        checks.check_backups(namespace, result)
        assert result.status == status, result.items
        memory = next(item for item in result.items if item["name"] == "memory copies")
        assert memory["status"] == expected
    # The refusal run names no --db: the job picks both files.
    source = (RECIPE / "checks" / "s31_checks.py").read_text(encoding="utf-8")
    refusal = source[source.index("if args.refusal:") :]
    refusal = refusal[: refusal.index("def check_restore")]
    assert '"--db"' not in refusal and '"--dest",\n                "/backups",' in refusal


def _memory_fakes(monkeypatch, report):
    from types import SimpleNamespace

    calls = []

    def run(argv, **_):
        calls.append(argv)
        if argv[:2] == ["docker", "exec"]:
            return SimpleNamespace(stdout=json.dumps(report) + "\n", stderr="", returncode=0)
        return SimpleNamespace(stdout="hub-cid\n", stderr="", returncode=0)

    monkeypatch.setattr(checks, "run", run)
    return calls


def test_memory_check_posts_status_with_the_admin_token_inside_the_namespace(monkeypatch, capsys):
    import argparse

    good = {"status": 200, "error": None, "reason": None, "log_epoch": True, "memory_file": True}
    calls = _memory_fakes(monkeypatch, good)
    result = checks.Result("memory")
    checks.check_memory(argparse.Namespace(), result)
    assert result.status == "pass"
    assert ["docker", "exec", "hub-cid", "python", "-c", checks.MEMORY_STATUS] in calls
    script = checks.MEMORY_STATUS
    compile(script, "memory-status", "exec")
    assert '"http://127.0.0.1:3774/memory"' in script and 'json.dumps({"action": "status"})' in script
    assert 'method="POST"' in script and '"/run/secrets/admin_token"' in script
    # The probe prints a summary, never the token or the response body.
    printed = script[script.index("print(") :]
    assert "token" not in printed and "body" not in printed
    assert "memory" in checks.CHECKS and checks.parser().parse_args(["memory"]).check == "memory"


def test_memory_check_fails_on_a_hub_without_the_native_route(monkeypatch, capsys):
    import argparse

    absent = {"status": 404, "error": "not found", "reason": None, "log_epoch": False, "memory_file": False}
    _memory_fakes(monkeypatch, absent)
    result = checks.Result("memory")
    checks.check_memory(argparse.Namespace(), result)
    assert result.status == "fail"
    first = next(item for item in result.items if item["name"].startswith("POST /memory"))
    assert first["status"] == "FAIL" and "memory=native" in first["detail"]["hint"]


entry = _load("brigade_hub_entry", RECIPE / "hub" / "entry.py")

FAKE_HUB = """
import http.server, signal, sys, threading
args = sys.argv[1:]
port = int(args[args.index("--port") + 1])
mode = args[args.index("--fake") + 1]

class Health(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == "/health" else 404)
        self.end_headers()

    def log_message(self, *_):
        pass

server = http.server.HTTPServer(("127.0.0.1", port), Health)
threading.Thread(target=server.serve_forever, daemon=True).start()
if mode == "crash":
    sys.exit(3)
done = threading.Event()
signal.signal(signal.SIGTERM, lambda *_: done.set())
# entry.py starts the hub with TERM and INT blocked; like brigade-hub, unblock them
# once the handler is in place, or the stop is never delivered.
signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM, signal.SIGINT})
done.wait(30)
sys.exit(0 if mode == "clean" else 5)
"""


def _free_port():
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _run_entry(tmp_path, mode, *, stop):
    import signal

    fake = tmp_path / "brigade-hub"
    fake.write_text(f"#!{sys.executable}\n{FAKE_HUB}", encoding="utf-8")
    fake.chmod(0o755)
    port = _free_port()
    argv = ["--host", "127.0.0.1", "--port", str(port), "--db", "/var/lib/brigade/fleet-hub.db"]
    argv += ["--route-mode", "memory=native", "--fake", mode]
    proc = subprocess.Popen(
        [sys.executable, str(RECIPE / "hub" / "entry.py"), *argv],
        env={**os.environ, "BRIGADE_HUB_BIN": str(fake)},
        stdout=subprocess.PIPE,
        text=True,
    )
    first = proc.stdout.readline()
    if stop:
        proc.send_signal(signal.SIGTERM)
    rest, _ = proc.communicate(timeout=30)
    return proc.returncode, first + rest


@pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
def test_entry_writes_the_lines_the_s31_checks_read_on_a_clean_stop(tmp_path, monkeypatch):
    code, out = _run_entry(tmp_path, "clean", stop=True)
    assert code == 0
    lines = out.splitlines()
    assert lines[0].startswith(checks.HUB_STARTED + " 127.0.0.1:")
    assert "route modes memory=native" in lines[0]
    assert lines[-1] == checks.HUB_STOPPED
    # The lines satisfy the clean-exit check as the container log.
    _log_fakes(monkeypatch, out)
    assert checks.clean_exit_logged(container="hub") is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
@pytest.mark.parametrize(("mode", "stop", "expected"), [("crash", False, 3), ("unclean", True, 5)])
def test_entry_passes_a_failure_through_without_the_clean_exit_line(tmp_path, mode, stop, expected):
    code, out = _run_entry(tmp_path, mode, stop=stop)
    assert code == expected
    assert checks.HUB_STOPPED not in out


@pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
def test_entry_forwards_a_stop_that_lands_while_the_child_is_spawning(monkeypatch, capsys):
    import signal

    sent = []

    class Child:
        def __init__(self, argv):
            # The stop lands mid-spawn: the handler runs before `child` is bound.
            os.kill(os.getpid(), signal.SIGTERM)

        def send_signal(self, signum):
            sent.append(signum)

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

    previous = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)}
    monkeypatch.setattr(entry.subprocess, "Popen", Child)
    try:
        code = entry.main(["--host", "127.0.0.1", "--port", "1"])
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    assert sent == [signal.SIGTERM]
    assert code == 0
    assert capsys.readouterr().out.strip() == checks.HUB_STOPPED


MASK_HUB = """
import signal, sys, time
from pathlib import Path
with open("/proc/self/status", encoding="utf-8") as status:
    blocked = next(line.split()[1] for line in status if line.startswith("SigBlk:"))
Path(sys.argv[-1]).write_text(blocked, encoding="utf-8")
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
# As brigade-hub does once its handlers exist: a held stop arrives now.
signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM, signal.SIGINT})
time.sleep(20)
sys.exit(4)
"""


@pytest.mark.skipif(not Path("/proc/self/status").exists(), reason="reads the child's signal mask from procfs")
def test_entry_starts_the_hub_with_stop_signals_blocked_so_a_pending_stop_waits(tmp_path, monkeypatch, capsys):
    import signal

    fake = tmp_path / "brigade-hub"
    fake.write_text(f"#!{sys.executable}\n{MASK_HUB}", encoding="utf-8")
    fake.chmod(0o755)
    real = subprocess.Popen

    def popen(argv):
        # The stop lands during the spawn; it is held, then forwarded.
        os.kill(os.getpid(), signal.SIGTERM)
        return real(argv)

    previous = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)}
    mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    monkeypatch.setattr(entry, "HUB", str(fake))
    monkeypatch.setattr(entry.subprocess, "Popen", popen)
    try:
        code = entry.main(["--host", "127.0.0.1", "--port", "1", str(tmp_path / "mask")])
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    blocked = int((tmp_path / "mask").read_text(encoding="utf-8"), 16)
    assert blocked & (1 << (signal.SIGTERM - 1)) and blocked & (1 << (signal.SIGINT - 1))
    # The forwarded stop reached the hub's handler, not its default action.
    assert code == 0
    assert capsys.readouterr().out.strip() == checks.HUB_STOPPED
    # This process's own mask is back as it was.
    assert signal.pthread_sigmask(signal.SIG_BLOCK, set()) == mask


def test_entry_reads_the_hub_flags_and_maps_signal_deaths():
    argv = ["--host", "::1", "--port=3999", "--route-mode", "memory=native", "--route-mode=claims=proxy"]
    assert entry.option(argv, "--host", "x") == "::1"
    assert entry.option(argv, "--port", "3774") == "3999"
    assert entry.option(argv, "--db", "default") == "default"
    assert entry.route_modes(argv) == ["memory=native", "claims=proxy"]
    assert entry.exit_code(-9) == 137 and entry.exit_code(2) == 2
    assert (entry.STARTED, entry.STOPPED) == (checks.HUB_STARTED, checks.HUB_STOPPED)


# ------------------------------------------- Rust engine guard (S10)


def _default_ref(name):
    """The default BRIGADE_REF that compose and the Containerfile each pin."""
    compose = (RECIPE / "compose.yaml").read_text(encoding="utf-8")
    containerfile = (RECIPE / "Containerfile").read_text(encoding="utf-8")
    return {
        "compose": compose.split("BRIGADE_REF: ${BRIGADE_REF:-", 1)[1].split("}", 1)[0],
        "containerfile": containerfile.split("ARG BRIGADE_REF=", 1)[1].split("\n", 1)[0],
    }[name]


def test_default_build_args_pin_one_ref_and_compose_leaves_the_engine_choice_to_entry():
    # compose.yaml and the Containerfile build the same default Brigade.
    assert _default_ref("compose") == _default_ref("containerfile")
    # That default pin predates `fleet serve --engine` (S10), so a default build
    # must not pass it: hub/entry.py picks the engine only when the image has it.
    compose = (RECIPE / "compose.yaml").read_text(encoding="utf-8")
    hub = compose.split("\n  hub:\n", 1)[1].split("\n  front:\n", 1)[0]
    assert '    entrypoint: ["python", "/brigade/entry.py"]\n' in hub
    settings = [line for line in hub.splitlines() if not line.strip().startswith("#")]
    assert not any("--engine" in line for line in settings)


def test_entry_falls_back_to_the_wrapper_for_a_brigade_without_the_engine(tmp_path, monkeypatch):
    checksum = tmp_path / "brigade-hub.sha256"
    checksum.write_text("a" * 64 + "  brigade-hub\n", encoding="utf-8")
    monkeypatch.setattr(entry, "ENGINE_SUM", str(checksum))
    # The default pin's Brigade has no brigade.fleet_hub_engine.
    monkeypatch.setattr(entry.importlib.util, "find_spec", lambda _name: None)
    assert entry.engine_command(["--host", "127.0.0.1"]) is None


def test_entry_falls_back_without_a_recorded_checksum(tmp_path, monkeypatch):
    monkeypatch.setattr(entry, "ENGINE_SUM", str(tmp_path / "absent.sha256"))
    assert entry.engine_command(["--host", "127.0.0.1"]) is None


def test_entry_execs_fleet_serve_with_the_rust_engine_when_the_image_has_it(tmp_path, monkeypatch):
    checksum = tmp_path / "brigade-hub.sha256"
    checksum.write_text("a" * 64 + "  brigade-hub\n", encoding="utf-8")
    monkeypatch.setattr(entry, "ENGINE_SUM", str(checksum))
    monkeypatch.setattr(entry.importlib.util, "find_spec", lambda name: object() if name.endswith("engine") else None)
    execs = []

    class Execd(Exception):
        pass

    def execv(path, argv):
        execs.append((path, argv))
        raise Execd

    monkeypatch.setattr(entry.os, "execv", execv)
    argv = ["--host", "127.0.0.1", "--port", "3774", "--route-mode", "memory=native"]
    with pytest.raises(Execd):
        entry.main(argv)
    assert execs == [
        (
            sys.executable,
            [
                sys.executable,
                "-m",
                "brigade",
                "fleet",
                "serve",
                "--engine",
                "rust",
                "--hub-binary",
                "/usr/local/bin/brigade-hub",
                "--hub-sha256-file",
                str(checksum),
                *argv,
            ],
        )
    ]


def _stand_in_check(monkeypatch):
    def stand_in(_args, result):
        """Stand-in check that records one INFO item."""
        result.add("INFO", "stand-in")

    monkeypatch.setitem(checks.CHECKS, "gpu", stand_in)


def _main_code(argv):
    try:
        return checks.main(argv)
    except OSError as exc:
        return f"raised {type(exc).__name__}"


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_record_refuses_a_symlinked_path(monkeypatch, tmp_path):
    _stand_in_check(monkeypatch)
    target = tmp_path / "elsewhere.jsonl"
    target.write_text("untouched\n", encoding="utf-8")
    link = tmp_path / "s31-results.jsonl"
    link.symlink_to(target)
    assert _main_code(["--record", str(link), "gpu"]) == 2
    assert target.read_text(encoding="utf-8") == "untouched\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX device file")
@pytest.mark.parametrize("kind", ["device", "directory"])
def test_record_refuses_a_non_regular_path(monkeypatch, tmp_path, kind):
    _stand_in_check(monkeypatch)
    path = os.devnull if kind == "device" else str(tmp_path)
    assert _main_code(["--record", path, "gpu"]) == 2


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="needs a non-root test account")
def test_record_as_root_refuses_a_file_owned_by_another_account(monkeypatch, tmp_path):
    _stand_in_check(monkeypatch)
    record = tmp_path / "s31-results.jsonl"
    record.write_text("", encoding="utf-8")
    monkeypatch.setattr(checks, "effective_uid", lambda: 0, raising=False)
    monkeypatch.delenv("SUDO_UID", raising=False)
    assert _main_code(["--record", str(record), "gpu"]) == 2
    assert record.read_text(encoding="utf-8") == ""
    # The invoking account's own file is accepted under sudo.
    monkeypatch.setenv("SUDO_UID", str(os.getuid()))
    assert _main_code(["--record", str(record), "gpu"]) == 0
    assert json.loads(record.read_text(encoding="utf-8"))["check"] == "gpu"


# ------------------------------------------------------- S31 setup check (#2468)


def _setup_check_fakes(monkeypatch, *, gpu=True, binary="/fake/brigade-hub", python_problem=None):
    """Run check_setup with setup in process on fake probes, Docker faked, and the hub start recorded."""
    import contextlib
    import io
    from types import SimpleNamespace

    starts = []

    def run(argv, **_):
        if argv[:2] == ["docker", "ps"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        assert argv[1].endswith("hub-setup.py"), argv
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = setup.main(argv[2:], run=gpu_host() if gpu else no_gpu_host, post=endpoint())
        return SimpleNamespace(returncode=code, stdout=out.getvalue(), stderr=err.getvalue())

    def hub_on_setup_config(found, python, project, *, embed):
        table = tomllib.loads((project / "config" / "hub.toml").read_text(encoding="utf-8"))["memory"]
        mode = table["retrieval"]
        starts.append((project.parents[1].name, mode, embed))
        state = "degraded" if embed else "ok"
        return {"ready": 200, "status": 200, "retrieval": {"mode": mode, "state": state}}

    monkeypatch.setattr(checks, "run", run)
    monkeypatch.setattr(checks, "hub_binary", lambda explicit: binary)
    monkeypatch.setattr(checks, "hub_python_problem", lambda python: python_problem)
    monkeypatch.setattr(checks, "hub_on_setup_config", hub_on_setup_config)
    args = SimpleNamespace(hub_binary=None, hub_python=sys.executable, remote=[])
    result = checks.Result("setup")
    checks.check_setup(args, result)
    return result, starts


def test_setup_check_starts_a_hub_after_init_and_each_switch(monkeypatch):
    result, starts = _setup_check_fakes(monkeypatch)
    # gpu: init hybrid, switch to lexical, switch back to hybrid. neither: init lexical.
    assert starts == [
        ("gpu", "hybrid", True),
        ("gpu", "lexical", False),
        ("gpu", "hybrid", True),
        ("neither", "lexical", False),
    ]
    names = [item["name"] for item in result.items if item["status"] == "PASS"]
    assert "hub accepts the config after switch to lexical (gpu)" in names
    assert "hub accepts the config after switch to hybrid (gpu)" in names
    assert "neither: switch to hybrid refused with no probed embedder, mode unchanged" in names
    # The remote path waits on its arguments. Nothing else is pending or failing.
    assert [item["name"] for item in result.items if item["status"] != "PASS" and item["status"] != "INFO"] == [
        "remote path"
    ]


@pytest.mark.parametrize(
    ("binary", "problem", "reason"),
    [(None, None, "no brigade-hub binary"), ("/fake/brigade-hub", "cannot import brigade", "cannot import brigade")],
)
def test_setup_check_fails_closed_without_a_hub_to_start(monkeypatch, binary, problem, reason):
    result, starts = _setup_check_fakes(monkeypatch, binary=binary, python_problem=problem)
    assert starts == [] and result.status == "fail"
    failed = {item["name"]: item["detail"] for item in result.items if item["status"] == "FAIL"}
    assert reason in failed["hub binary"]
    assert "hub accepts the config after init (gpu)" in failed
    assert not [item for item in result.items if item["status"] == "PENDING" and item["name"] != "remote path"]


@pytest.mark.parametrize(
    ("seen", "mode", "unreachable", "passes"),
    [
        ({"ready": 200, "retrieval": {"mode": "hybrid", "state": "degraded"}}, "hybrid", True, True),
        # Unreachable but not degraded means the embed scheduler never tried, or the mode is wrong.
        ({"ready": 200, "retrieval": {"mode": "hybrid", "state": "ok"}}, "hybrid", True, False),
        ({"ready": 200, "retrieval": {"mode": "lexical", "state": "ok"}}, "hybrid", True, False),
        ({"ready": 0, "error": "hub exit 2: embedder allowlist is required"}, "hybrid", True, False),
        ({"ready": 200, "retrieval": {"mode": "hybrid", "state": "switching"}}, "hybrid", False, True),
        ({"ready": 200, "retrieval": {"mode": "lexical", "state": "ok"}}, "lexical", False, True),
        ({"ready": 200, "retrieval": {"mode": "lexical", "state": "degraded"}}, "lexical", False, False),
    ],
)
def test_accept_hub_requires_ready_and_the_expected_mode(monkeypatch, tmp_path, seen, mode, unreachable, passes):
    monkeypatch.setattr(checks, "hub_on_setup_config", lambda *a, **k: seen)
    result = checks.Result("setup")
    checks.accept_hub(result, ("/fake/brigade-hub", sys.executable), tmp_path, "init (x)", mode, unreachable)
    assert result.items[-1]["status"] == ("PASS" if passes else "FAIL")


def test_hub_memory_config_points_the_token_at_a_private_copy_of_the_mounted_secret(project, tmp_path):
    assert _init(project, tmp_path, run=gpu_host()) == 0
    state = tmp_path / "state"
    state.mkdir()
    config = checks.hub_memory_config(project, state)
    table = tomllib.loads(config.read_text(encoding="utf-8"))["memory"]["embedder"]
    secret = project / "secrets" / "embed-local-token"
    assert table["token_file"] == str(state / "embed_token")
    assert (state / "embed_token").read_bytes() == secret.read_bytes()
    assert {key: value for key, value in table.items() if key != "token_file"} == {
        key: value for key, value in _embedder(project).items() if key != "token_file"
    }
    if os.name == "posix":
        # The mounted file is 0444 for the container's UID. The hub's rule is met by the copy.
        assert stat.S_IMODE(secret.stat().st_mode) == 0o444
        assert stat.S_IMODE((state / "embed_token").stat().st_mode) == 0o400


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes")
def test_setup_resets_a_local_token_an_earlier_setup_wrote_0400(project, tmp_path, capsys):
    """#2469: the hub container's UID cannot read a 0400 file through the bind mount."""
    assert _init(project, tmp_path, run=gpu_host()) == 0
    secret = project / "secrets" / "embed-local-token"
    before = secret.read_bytes()
    secret.chmod(0o400)
    capsys.readouterr()
    assert setup.main(["--project-dir", str(project), "switch", "--to", "hybrid"], run=gpu_host()) == 0
    assert stat.S_IMODE(secret.stat().st_mode) == 0o444 and secret.read_bytes() == before
    assert "embed-local-token (mode reset to 0444)" in capsys.readouterr().out
    # A symlink under the name is refused, and the file it points at keeps its mode.
    outside = tmp_path / "outside-token"
    outside.write_bytes(before)
    outside.chmod(0o400)
    secret.unlink()
    secret.symlink_to(outside)
    assert setup.main(["--project-dir", str(project), "switch", "--to", "hybrid"], run=gpu_host()) != 0
    assert stat.S_IMODE(outside.stat().st_mode) == 0o400


def test_hub_binary_refuses_a_missing_explicit_path(tmp_path):
    assert checks.hub_binary(str(tmp_path / "no-such-hub")) is None


# ------------------------------------------- Compose hybrid wiring (#2469)


def test_compose_starts_the_hub_on_setups_config_with_a_private_token_tmpfs():
    """No Docker needed: the hub's command names the mounted hub.toml, and the tmpfs holds the token copy."""
    compose = (RECIPE / "compose.yaml").read_text(encoding="utf-8")
    hub = compose.split("\n  hub:\n", 1)[1].split("\n  front:\n", 1)[0]
    settings = [line.strip() for line in hub.splitlines() if not line.strip().startswith("#")]
    flag = settings.index("- --memory-config")
    assert settings[flag + 1] == "- /etc/brigade/hub.toml"
    assert "- ./config/hub.toml:/etc/brigade/hub.toml:ro" in settings
    # Owned by the image's hub user and closed to everyone else, at the directory setup's token path is in.
    folder = posixpath.dirname(setup.EMBED_TOKEN_COPY)
    assert f"- {folder}:mode=0700,uid=10001,gid=10001" in settings
    assert "useradd --uid 10001 --gid 10001" in (RECIPE / "Containerfile").read_text(encoding="utf-8")
    # Setup, the entrypoint and the S31 check agree on both paths.
    assert (entry.EMBED_TOKEN_MOUNT, entry.EMBED_TOKEN_COPY) == (setup.EMBED_TOKEN_MOUNT, setup.EMBED_TOKEN_COPY)
    assert checks.EMBED_TOKEN_COPY == setup.EMBED_TOKEN_COPY


def _token_folder(tmp_path):
    folder = tmp_path / "run-brigade"
    folder.mkdir(mode=0o700)
    folder.chmod(0o700)
    return folder


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership and modes")
def test_entry_copies_the_mounted_embedder_token_to_a_hub_owned_0400_file(tmp_path):
    """The hub accepts only a token file its own user owns with mode 0400 or 0600 (token_file_permitted)."""
    source = tmp_path / "embed_token"
    source.write_text("mounted-embed-token\n", encoding="utf-8")
    # As Compose mounts it: the operator's file, world-readable inside the 0700 secrets directory.
    source.chmod(0o444)
    target = _token_folder(tmp_path) / "embed_token"
    assert entry.deliver_embed_token(str(source), str(target)) is None
    info = target.lstat()
    assert stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o400
    assert info.st_uid == os.geteuid()
    assert target.read_text(encoding="utf-8") == "mounted-embed-token\n"
    # A later start with a rotated token replaces the copy, and leaves no staging file.
    source.chmod(0o644)
    source.write_text("rotated-embed-token\n", encoding="utf-8")
    assert entry.deliver_embed_token(str(source), str(target)) is None
    assert target.read_text(encoding="utf-8") == "rotated-embed-token\n"
    assert stat.S_IMODE(target.lstat().st_mode) == 0o400
    assert [p.name for p in target.parent.iterdir()] == ["embed_token"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlinks and modes")
def test_entry_refuses_a_symlinked_or_irregular_token_and_never_copies_it(tmp_path):
    real = tmp_path / "elsewhere"
    real.write_text("some-other-secret\n", encoding="utf-8")
    source = tmp_path / "embed_token"
    source.symlink_to(real)
    target = _token_folder(tmp_path) / "embed_token"
    problem = entry.deliver_embed_token(str(source), str(target))
    assert problem is not None and "is a symlink" in problem and "some-other-secret" not in problem
    assert not target.exists()
    source.unlink()
    source.mkdir()
    assert "not a regular file" in (entry.deliver_embed_token(str(source), str(target)) or "")
    source.rmdir()
    source.write_bytes(b"x" * (entry.EMBED_TOKEN_BYTES + 1))
    assert "not a regular file" in (entry.deliver_embed_token(str(source), str(target)) or "")
    assert list(target.parent.iterdir()) == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlinks and modes")
def test_entry_replaces_a_symlink_at_the_copy_and_needs_a_private_directory(tmp_path):
    source = tmp_path / "embed_token"
    source.write_text("mounted-embed-token\n", encoding="utf-8")
    victim = tmp_path / "victim"
    victim.write_text("unrelated\n", encoding="utf-8")
    folder = _token_folder(tmp_path)
    target = folder / "embed_token"
    target.symlink_to(victim)
    assert entry.deliver_embed_token(str(source), str(target)) is None
    # The link itself was replaced. What it pointed at is untouched.
    assert not target.is_symlink() and victim.read_text(encoding="utf-8") == "unrelated\n"
    # A directory other users can enter is refused, and so is a missing one.
    folder.chmod(0o755)
    assert "mode 0700" in (entry.deliver_embed_token(str(source), str(target)) or "")
    assert "is missing" in (entry.deliver_embed_token(str(source), str(tmp_path / "absent" / "embed_token")) or "")


@pytest.mark.skipif(os.name != "posix", reason="the copy is made only on POSIX")
def test_entry_without_a_mounted_token_removes_an_earlier_copy(tmp_path):
    """BM25 mode mounts no embedder token, so no copy from a hybrid start stays behind."""
    target = _token_folder(tmp_path) / "embed_token"
    target.write_text("stale\n", encoding="utf-8")
    assert entry.deliver_embed_token(str(tmp_path / "not-mounted"), str(target)) is None
    assert not target.exists()
    # Nothing to remove, and no tmpfs at all (a host run of the wrapper), are both fine.
    assert entry.deliver_embed_token(str(tmp_path / "not-mounted"), str(target)) is None
    assert entry.deliver_embed_token(str(tmp_path / "not-mounted"), str(tmp_path / "absent" / "copy")) is None


def test_entry_stops_before_starting_a_hub_when_the_token_cannot_be_delivered(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(entry, "deliver_embed_token", lambda: "embedder token is a symlink")
    monkeypatch.setattr(entry.subprocess, "Popen", lambda *a, **k: pytest.fail("the hub started"))
    monkeypatch.setattr(entry.os, "execv", lambda *a: pytest.fail("the hub started"))
    config = tmp_path / "hub.toml"
    config.write_text(
        '[memory]\nretrieval = "hybrid"\n[memory.embedder]\nbackend = "remote"\n'
        f'endpoint = "https://embed.example.test/v1"\ntoken_file = "{entry.EMBED_TOKEN_COPY}"\n',
        encoding="utf-8",
    )
    # Fatal for the hybrid config that reads the copy (tests/test_fleet_hub_embed_token_modes.py has the other rows).
    assert entry.main(["--host", "127.0.0.1", "--port", "1", "--memory-config", str(config)]) == 2
    assert "error: embedder token is a symlink" in capsys.readouterr().err


def test_entry_passes_memory_config_only_to_an_image_that_has_the_flag(capsys):
    argv = ["--host", "127.0.0.1", "--memory-config", "/etc/brigade/hub.toml", "--route-mode", "memory=native"]
    assert entry.supported_argv(argv, True) == argv
    assert capsys.readouterr().err == ""
    # An image whose Brigade predates the flag would refuse it and never start.
    assert entry.supported_argv(argv, False) == ["--host", "127.0.0.1", "--route-mode", "memory=native"]
    assert "predates --memory-config" in capsys.readouterr().err
    assert entry.supported_argv(["--memory-config=/etc/brigade/hub.toml", "--port", "1"], False) == ["--port", "1"]
    # This checkout's `fleet serve --engine rust` has the pass-through.
    assert entry.engine_supports_memory_config() is True


def test_entry_execs_fleet_serve_with_the_memory_config_compose_names(tmp_path, monkeypatch):
    checksum = tmp_path / "brigade-hub.sha256"
    checksum.write_text("a" * 64 + "  brigade-hub\n", encoding="utf-8")
    monkeypatch.setattr(entry, "ENGINE_SUM", str(checksum))
    monkeypatch.setattr(entry, "deliver_embed_token", lambda: None)
    execs = []

    class Execd(Exception):
        pass

    def execv(path, argv):
        execs.append(argv)
        raise Execd

    monkeypatch.setattr(entry.os, "execv", execv)
    compose = (RECIPE / "compose.yaml").read_text(encoding="utf-8")
    hub = compose.split("\n  hub:\n", 1)[1].split("\n    environment:\n", 1)[0].split("\n    command:\n", 1)[1]
    command = [line.strip()[2:].strip('"') for line in hub.splitlines() if line.strip().startswith("- ")]
    with pytest.raises(Execd):
        entry.main(command)
    assert execs[0][execs[0].index("--memory-config") + 1] == "/etc/brigade/hub.toml"
    # The whole command Compose passes is one `fleet serve` accepts.
    parser = argparse.ArgumentParser()
    from brigade.cli import fleet as fleet_cli

    fleet_cli.register(parser.add_subparsers(dest="command"))
    args = parser.parse_args(execs[0][3:])
    # A container path: compare as POSIX, since Windows renders the parsed Path with backslashes.
    assert (args.engine, args.memory_config.as_posix(), args.route_mode) == (
        "rust",
        "/etc/brigade/hub.toml",
        ["memory=native"],
    )
