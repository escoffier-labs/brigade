"""S31 Compose recipe: setup's mode selection (D30), the mode switch, units, the front wrapper and the watchdog.

Nothing here touches the host: GPU and endpoint probes are fakes, and the
only Docker call is `docker compose config`, which validates files and starts
nothing.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import time
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


def endpoint(status=200, dims=MODEL["dimensions"], digest="sha256:" + "a" * 64):
    calls = []

    def post(url, body, headers):
        calls.append((url, body, headers))
        assert headers["Authorization"].startswith("Bearer ")
        return status, ({"x-embedder-model-digest": digest} if digest else {}), {"embeddings": [[0.1] * dims]}

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


def test_supported_gpu_writes_gpu_profile_local_backend_and_an_r1_model(project, tmp_path, capsys):
    assert _init(project, tmp_path, run=gpu_host()) == 0
    env = _env(project)
    assert env["COMPOSE_PROFILES"] == "gpu" and env["BRIGADE_RETRIEVAL"] == "hybrid"
    assert env["BRIGADE_EMBED_BACKEND"] == "local" and env["BRIGADE_EMBED_MODEL"] == MODEL["name"]
    toml = _toml(project)
    assert 'retrieval = "hybrid"' in toml and 'backend = "local"' in toml and "http://embedder:11434" in toml
    assert "hybrid, backend local" in capsys.readouterr().out


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
    toml = _toml(project)
    assert 'backend = "remote"' in toml and 'model_digest = "sha256:' in toml
    assert 'token_file = "/run/secrets/embed_token"' in toml
    assert post.calls and post.calls[0][0] == "http://gpu-host.example.ts.net:11434/api/embed"
    assert (project / "secrets" / "embed-token").read_text(encoding="utf-8").strip() == "fake-remote-embed-token"
    assert "embed_token" in (project / "compose.override.yaml").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("url", "post", "reason"),
    [
        ("https://embed.example.test", endpoint(digest=None), "reports no model digest"),
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


def test_brigade_ref_needs_its_archive_digest(project, tmp_path):
    assert _init(project, tmp_path, "--brigade-ref", "0" * 40) == 1
    assert _init(project, tmp_path, "--brigade-ref", "0" * 40, "--brigade-sha256", "f" * 64) == 0
    assert _env(project)["BRIGADE_REF"] == "0" * 40


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
    assert "AllowedCPUs=6-7" in rendered["brigade-hub.slice"]
    assert "IODeviceLatencyTargetSec=/dev/nvme0n1 10ms" in rendered["brigade-hub.slice"]
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
