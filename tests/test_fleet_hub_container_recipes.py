import importlib.util
import os
import re
import sqlite3
import subprocess
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests._home import set_home


ROOT = Path(__file__).resolve().parents[1]
CONTAINER = ROOT / "ops" / "fleet-hub"
SMOKE = ROOT / "tests" / "fleet_hub_container_smoke.py"


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        pytest.param((503, {}, b""), "503", id="http-503"),
        pytest.param(urllib.error.URLError("proxy unavailable"), "proxy unavailable", id="request-error"),
    ],
)
def test_wait_for_health_delays_retries_and_reports_last_failure(monkeypatch, failure, expected):
    spec = importlib.util.spec_from_file_location("fleet_hub_container_smoke", SMOKE)
    assert spec is not None and spec.loader is not None
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)

    def request(*_args, **_kwargs):
        if isinstance(failure, BaseException):
            raise failure
        return failure

    times = iter((0.0, 1.0, 20.0))
    delays = []
    monkeypatch.setattr(smoke, "request", request)
    monkeypatch.setattr(
        smoke,
        "time",
        SimpleNamespace(monotonic=lambda: next(times), sleep=delays.append),
    )

    with pytest.raises(RuntimeError, match=re.escape(expected)):
        smoke.wait_for_health("http://hub.test")

    assert delays == [0.25]


def test_hub_image_is_non_root_and_has_a_single_writable_state_directory():
    text = (CONTAINER / "Containerfile").read_text()

    assert "ARG BRIGADE_REF=e6009f6969dc6e45db2ab93790aa3e407c791409" in text
    assert (
        "FROM python:3.12.14-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e"
        in text
    )
    assert "ARG BRIGADE_SHA256=68014a8e99c61f5e547d8bf223a5f3ad7ea31f60355a4c30356bffd78e15c168" in text
    assert "setuptools==75.8.0 wheel==0.45.1" in text
    assert "--no-build-isolation /tmp/brigade.tar.gz" in text
    assert 'org.opencontainers.image.revision="${BRIGADE_REF}"' in text
    assert "USER 10001:10001" in text
    assert 'VOLUME ["/var/lib/brigade"]' in text
    assert '"--db", "/var/lib/brigade/fleet-hub.db"' in text


def test_compose_recipe_keeps_host_port_private_and_token_out_of_environment():
    text = (CONTAINER / "compose.yaml").read_text()
    dockerignore = (ROOT / ".dockerignore").read_text().splitlines()
    containerfile = (CONTAINER / "Containerfile").read_text().splitlines()

    assert '"127.0.0.1:3774:3774"' in text
    assert "target: admin_token" in text
    assert dockerignore == ["**", "!ops/", "!ops/fleet-hub/", "!ops/fleet-hub/Containerfile"]
    assert not any(line.startswith(("ADD ", "COPY ")) for line in containerfile)
    assert "BRIGADE_FLEET_TOKEN" not in text
    assert "restart: unless-stopped" in text
    assert "healthcheck:" in text
    assert "brigade-state:/var/lib/brigade" in text


def test_compose_runbook_pins_the_built_local_image_id():
    runbook = (ROOT / "docs" / "runbooks" / "fleet-hub-containers.md").read_text()

    assert "docker image inspect --format '{{.Id}}'" in runbook
    assert "image: ${image_id}" in runbook
    assert "previous_id=\"$(sed -n 's/^[[:space:]]*image: //p' secrets/compose.image-id.yaml)\"" in runbook
    assert "secrets/compose.image-id.yaml up -d --no-build" in runbook
    assert re.search(
        r"Docker restart policies\s+apply only when a container terminates\.",
        runbook,
    )
    assert "docker compose -f compose.yaml -f secrets/compose.image-id.yaml restart hub" in runbook
    assert runbook.count("--wait --wait-timeout 120") == 4
    assert "deadline=$((SECONDS + 120))" in runbook
    assert 'curl --fail --connect-timeout 2 --max-time "$remaining"' in runbook
    assert "rebuild the versioned tag, record its new local image ID" in runbook
    assert 'docker image save --output "$archive" "$previous_id"' in runbook
    assert 'docker image load --input "$archive"' in runbook
    assert 'test "$restored_id" = "$previous_id"' in runbook
    assert "image: ${previous_id}" in runbook
    rollback = runbook.split("To roll back, load the saved image", 1)[1].split("```bash\n", 1)[1].split("\n```", 1)[0]
    assert rollback.index("source_connection.backup(target_connection)") < rollback.index(
        "cat > secrets/compose.image-id.yaml"
    )
    assert '--entrypoint python "$previous_id" -c' in rollback
    assert '--volumes-from "$(docker compose -f compose.yaml -f secrets/compose.image-id.yaml ps -aq hub)"' in rollback
    assert "For an upgrade, follow the backup and stop sequence below before rebuilding" in runbook
    assert "secrets/compose.image-id.yaml up -d --no-build" in runbook
    assert "docker compose -f compose.yaml -f secrets/compose.image-id.yaml stop hub" in runbook
    assert "source.backup(target)" in runbook
    assert (
        'docker cp "$(docker compose -f compose.yaml -f secrets/compose.image-id.yaml ps -q hub):/var/lib/brigade/fleet-hub-pre-upgrade.db" "$backup_path"'
        in runbook
    )
    assert '"$PWD/$backup_path:/restore/fleet-hub.db:ro"' in runbook
    assert "--user 0" in runbook
    assert "os.chown(database, 10001, 10001)" in runbook
    assert "PRAGMA integrity_check" in runbook
    assert runbook.count("if not os.path.isfile(source) or os.path.getsize(source) == 0") == 2
    assert runbook.count('tempfile.mkstemp(prefix=".fleet-hub-restore-"') == 2
    assert runbook.count('if result != "ok": raise RuntimeError(result)') == 2
    assert runbook.count("source_connection.backup(target_connection)") == 2
    assert "Docker Engine 28.0.0 or newer" in runbook
    assert "docker version --format '{{.Server.Version}}'" in runbook
    assert 'python3 -c \'import re,sys; m=re.fullmatch(r"(\\d+)\\.(\\d+)\\.(\\d+)", sys.argv[1])' in runbook
    assert 'generated_token="$(python3 -c' in runbook
    assert "printf '%s\\n' \"$generated_token\" > secrets/admin-token.txt" in runbook
    assert 're.match(r"^(\\d+)\\.(\\d+)\\.(\\d+)", sys.argv[1])' not in runbook


@pytest.mark.parametrize(
    ("version", "accepted"),
    [
        ("27.9.9", False),
        ("28.0.0-rc.1", False),
        ("28.0.0-beta.1", False),
        ("28.0.0", True),
        ("28.0.1", True),
    ],
)
def test_compose_runbook_version_check_rejects_prereleases(version, accepted):
    runbook = (ROOT / "docs" / "runbooks" / "fleet-hub-containers.md").read_text()
    match = re.search(r"python3 -c '([^']+)' \"\$engine_version\"", runbook)
    assert match is not None

    result = subprocess.run(
        [sys.executable, "-c", match.group(1), version],
        check=False,
        capture_output=True,
        text=True,
    )
    assert (result.returncode == 0) is accepted, result.stderr


def test_quadlet_runbook_archives_and_restores_the_recorded_image():
    runbook = (ROOT / "docs" / "runbooks" / "fleet-hub-containers.md").read_text()
    podman_runbook = runbook.split("## Rootless Podman Quadlet", maxsplit=1)[1]

    assert 'podman image save --format oci-archive --output "$archive" "$previous_ref"' in podman_runbook
    assert 'podman image load --input "$archive"' in podman_runbook
    assert 'test "$restored_id" = "$previous_id"' in podman_runbook
    rollback = podman_runbook.split("# Set these from the saved archive name", 1)[1]
    assert rollback.index('test "$restored_id" = "$previous_id"') < rollback.index(
        "systemctl --user stop brigade-fleet.service"
    )
    assert "REPLACE_WITH_RECORDED_DIGEST" in podman_runbook
    assert 'previous_id="REPLACE_WITH_RECORDED_IMAGE_ID"' not in podman_runbook
    assert 'previous_id="sha256:REPLACE_WITH_RECORDED_IMAGE_ID"' not in podman_runbook
    assert 'podman secret create brigade-fleet-admin-token "$token_file"' in podman_runbook
    assert "no unit was installed" in podman_runbook
    assert "if podman secret inspect brigade-fleet-admin-token" in podman_runbook
    assert 'token_file="$token_dir/admin-token"' in podman_runbook
    assert 'generated_token="$(python3 -c' in podman_runbook
    assert 'printf \'%s\\n\' "$generated_token" > "$token_file"' in podman_runbook
    assert 'elif [ -e "$token_file" ] || [ -L "$token_file" ]; then' in podman_runbook
    assert "podman exec brigade-fleet-hub python -c" in podman_runbook
    assert 'podman cp brigade-fleet-hub:/var/lib/brigade/fleet-hub-pre-upgrade.db "$backup_path"' in podman_runbook
    assert "systemctl --user stop brigade-fleet.service" in podman_runbook
    assert '"$backup_path:/restore/fleet-hub.db:ro,Z"' in podman_runbook
    assert "--volume brigade-fleet-state:/var/lib/brigade" in podman_runbook
    assert "os.chown(database, 10001, 10001)" in podman_runbook
    assert "PRAGMA integrity_check" in podman_runbook
    assert 'image_id_file="${archive%.tar}.image-id"' in podman_runbook
    assert 'previous_id="$(cat "$image_id_file")"' in podman_runbook
    assert 'chmod 0600 "$archive" "$image_id_file"' in podman_runbook
    assert podman_runbook.count("systemctl --user restart brigade-fleet.service") == 2
    assert "systemctl --user start brigade-fleet.service" not in podman_runbook
    assert 'loginctl enable-linger "$USER"' in podman_runbook
    assert 'loginctl show-user "$USER" --property=Linger --value' in podman_runbook


@pytest.mark.parametrize("existing_config", [False, True], ids=["fresh-client", "enrolled-client"])
@pytest.mark.skipif(sys.platform != "linux", reason="Rootless Quadlet setup targets Linux")
def test_quadlet_setup_starts_hub_without_enabling_or_replacing_local_client(tmp_path, monkeypatch, existing_config):
    runbook = (ROOT / "docs" / "runbooks" / "fleet-hub-containers.md").read_text()
    quadlet = runbook.split("## Rootless Podman Quadlet", maxsplit=1)[1]
    setup = re.search(r"```bash\n(.*?)\n```", quadlet, re.DOTALL)
    assert setup is not None
    home = tmp_path / "home"
    home.mkdir()
    config = home / ".brigade" / "fleet.toml"
    original = '[fleet]\nhub_url = "http://hub.test"\nnode_token_file = "node-token"\n'
    if existing_config:
        config.parent.mkdir()
        config.write_text(original)
    commands = tmp_path / "bin"
    commands.mkdir()
    stubs = {
        "podman": (
            'case "$1 $2" in\n  "image inspect") printf "sha256:%064d\\n" 0 ;;\n  "secret inspect") exit 1 ;;\nesac\n'
        ),
        "loginctl": "printf 'yes\\n'\n",
        "systemctl": 'printf "%s\\n" "$*" >> "$HOME/service-actions"\n',
        "curl": "exit 0\n",
    }
    for name, body in stubs.items():
        executable = commands / name
        executable.write_text("#!/bin/sh\n" + body)
        executable.chmod(0o755)
    set_home(monkeypatch, home)
    monkeypatch.setenv("USER", "test-operator")
    monkeypatch.setenv("PATH", str(commands) + os.pathsep + os.environ["PATH"])

    result = subprocess.run(["bash"], input=setup.group(1), cwd=ROOT, capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert (home / "service-actions").read_text().splitlines() == [
        "--user daemon-reload",
        "--user restart brigade-fleet.service",
    ]
    if existing_config:
        assert config.read_text() == original
    else:
        assert not config.exists()


def test_runbook_restore_keeps_live_database_when_sqlite_backup_fails(tmp_path):
    runbook = (ROOT / "docs" / "runbooks" / "fleet-hub-containers.md").read_text()
    commands = re.findall(
        r'--entrypoint python (?:hub|"\$previous_ref"|"\$previous_id") -c \'(.*?)\'\n',
        runbook,
        re.DOTALL,
    )
    assert len(commands) == 2

    for index, command in enumerate(commands):
        source = tmp_path / f"backup-{index}.db"
        database = tmp_path / f"live-{index}.db"
        with sqlite3.connect(source) as connection:
            connection.execute("CREATE TABLE records (value TEXT)")
            connection.execute("INSERT INTO records VALUES ('backup')")
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE records (value TEXT)")
            connection.execute("INSERT INTO records VALUES ('live')")

        executable = command.replace('database="/var/lib/brigade/fleet-hub.db"', f"database={str(database)!r}").replace(
            'source="/restore/fleet-hub.db"', f"source={str(source)!r}"
        )
        executable = executable.replace(
            "target_connection=sqlite3.connect(database)",
            'target_connection=sqlite3.connect(f"file:{database}?mode=ro", uri=True)',
        )
        result = subprocess.run([sys.executable, "-c", executable], capture_output=True, text=True)

        assert result.returncode != 0
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT value FROM records").fetchone() == ("live",)


@pytest.mark.skipif(
    sys.platform == "win32", reason="Runbook restore executes chown inside the documented Linux container"
)
def test_runbook_restore_replaces_live_database_from_valid_backup(tmp_path):
    runbook = (ROOT / "docs" / "runbooks" / "fleet-hub-containers.md").read_text()
    commands = re.findall(
        r'--entrypoint python (?:hub|"\$previous_ref"|"\$previous_id") -c \'(.*?)\'\n',
        runbook,
        re.DOTALL,
    )
    assert len(commands) == 2

    for index, command in enumerate(commands):
        source = tmp_path / f"valid-backup-{index}.db"
        database = tmp_path / f"restored-{index}.db"
        with sqlite3.connect(source) as connection:
            connection.execute("CREATE TABLE records (value TEXT)")
            connection.execute("INSERT INTO records VALUES ('backup')")
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE records (value TEXT)")
            connection.execute("INSERT INTO records VALUES ('live')")

        executable = command.replace('database="/var/lib/brigade/fleet-hub.db"', f"database={str(database)!r}").replace(
            'source="/restore/fleet-hub.db"', f"source={str(source)!r}"
        )
        executable = executable.replace(
            "os.chown(database, 10001, 10001)",
            "os.chown(database, os.getuid(), os.getgid())",
        )
        result = subprocess.run([sys.executable, "-c", executable], capture_output=True, text=True)

        assert result.returncode == 0, result.stderr
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT value FROM records").fetchone() == ("backup",)


def test_quadlet_recipe_has_equivalent_persistence_health_and_restart_policy():
    container = (CONTAINER / "quadlet" / "brigade-fleet.container").read_text()
    volume = (CONTAINER / "quadlet" / "brigade-fleet.volume").read_text()

    assert "Image=localhost/brigade-fleet-hub@REPLACE_WITH_RECORDED_DIGEST" in container
    assert "PublishPort=127.0.0.1:3774:3774" in container
    assert "Volume=brigade-fleet.volume:/var/lib/brigade" in container
    assert "Secret=brigade-fleet-admin-token,target=admin_token" in container
    assert "HealthCmd=" in container
    assert "HealthOnFailure=kill" in container
    assert "Notify=healthy" in container
    assert "Pull=never" in container
    assert "TimeoutStartSec=120" in container
    assert "Restart=on-failure" in container
    assert "User=10001" in volume
    assert "Group=10001" in volume
    assert "UID=10001" not in volume
    assert "GID=10001" not in volume
