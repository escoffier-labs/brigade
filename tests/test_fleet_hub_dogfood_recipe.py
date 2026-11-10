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


# The live namespace: the hub and its Python upstream on loopback, the front's
# tailscaled peerapi on every address, and Docker's embedded DNS.
NAMESPACE_TABLES = "\n".join(
    [
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode",
        _tcp_row(0, "0100007F", 3774, 101),
        _tcp_row(1, "0100007F", 34293, 102),
        _tcp_row(2, "00000000", 45045, 201),
        _tcp_row(3, "0B00007F", 34717, 301),
    ]
)


def test_listening_ports_filters_by_socket_inode():
    tables, inodes = checks.split_hub_sockets(NAMESPACE_TABLES + "\n#inodes\n101\n102\n")
    assert inodes == {"101", "102"}
    assert checks.listening_ports(tables) == {3774, 34293, 45045, 34717}
    assert checks.listening_ports(tables, inodes) == {3774, 34293}


def _access(monkeypatch, *, open_ports, bindings="{}", network_mode="brigade-hub_default", inodes="101\n102\n"):
    import argparse

    def fake_inspect(_container, template):
        if "IPAddress" in template:
            return "192.0.2.10 "
        if "NetworkMode" in template:
            return network_mode
        assert "PortBindings" in template
        return bindings

    monkeypatch.setattr(checks, "container_id", lambda service, project=checks.RECIPE: f"c-{service}")
    monkeypatch.setattr(checks, "inspect", fake_inspect)
    monkeypatch.setattr(
        checks,
        "run",
        lambda argv, **_kw: subprocess.CompletedProcess(argv, 0, NAMESPACE_TABLES + "\n#inodes\n" + inodes, ""),
    )
    monkeypatch.setattr(checks, "connect_refused", lambda host, port, timeout=2.0: (host, port) not in open_ports)
    monkeypatch.setattr(checks, "front_url", lambda: "https://front.example.test")
    monkeypatch.setattr(checks, "get", lambda url, headers=None, timeout=5: (401, b""))
    result = checks.Result("access")
    checks.check_access(argparse.Namespace(), result)
    return result


def test_access_ignores_host_loopback_services_and_the_front_peerapi(monkeypatch):
    # #2305: a desktop app on host 127.0.0.1:3774 and the front's peerapi on the
    # holder's address are not the hub. The hub ports are refused everywhere.
    result = _access(monkeypatch, open_ports={("127.0.0.1", 3774), ("192.0.2.10", 45045)})
    assert result.status == "pass"
    probed = {item["name"] for item in result.items if item["name"].startswith("host cannot connect")}
    assert not any(name.endswith(":45045") or name.endswith(":34717") for name in probed)
    (loopback,) = [item for item in result.items if item["name"] == "host cannot connect to 127.0.0.1:3774"]
    assert loopback["status"] == "INFO"


def test_access_fails_when_the_hub_port_is_reachable(monkeypatch):
    published = json.dumps({"3774/tcp": [{"HostIp": "127.0.0.1", "HostPort": "3774"}]})
    assert _access(monkeypatch, open_ports={("127.0.0.1", 3774)}, bindings=published).status == "fail"
    assert _access(monkeypatch, open_ports={("127.0.0.1", 3774)}, network_mode="host").status == "fail"
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
