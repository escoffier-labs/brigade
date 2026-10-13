from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTAINER = ROOT / "ops" / "fleet-hub"


def test_hub_image_is_non_root_and_has_a_single_writable_state_directory():
    text = (CONTAINER / "Containerfile").read_text()

    assert "ARG BRIGADE_REF=e6009f6969dc6e45db2ab93790aa3e407c791409" in text
    assert (
        "FROM python:3.12.7-slim-bookworm@sha256:60d9996b6a8a3689d36db740b49f4327be3be09a21122bd02fb8895abb38b50d"
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

    assert '"127.0.0.1:3774:3774"' in text
    assert "target: admin_token" in text
    assert "BRIGADE_FLEET_TOKEN" not in text
    assert "restart: unless-stopped" in text
    assert "healthcheck:" in text
    assert "brigade-state:/var/lib/brigade" in text


def test_quadlet_recipe_has_equivalent_persistence_health_and_restart_policy():
    container = (CONTAINER / "quadlet" / "brigade-fleet.container").read_text()
    volume = (CONTAINER / "quadlet" / "brigade-fleet.volume").read_text()

    assert "PublishPort=127.0.0.1:3774:3774" in container
    assert "Volume=brigade-fleet.volume:/var/lib/brigade" in container
    assert "Secret=brigade-fleet-admin-token,target=admin_token" in container
    assert "HealthCmd=" in container
    assert "Restart=on-failure" in container
    assert "User=10001" in volume
    assert "Group=10001" in volume
    assert "UID=10001" not in volume
    assert "GID=10001" not in volume
