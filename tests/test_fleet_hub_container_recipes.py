from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTAINER = ROOT / "ops" / "fleet-hub"


def test_hub_image_is_non_root_and_has_a_single_writable_state_directory():
    text = (CONTAINER / "Containerfile").read_text()

    assert "ARG BRIGADE_VERSION=0.27.0" in text
    assert '"brigade-cli==${BRIGADE_VERSION}"' in text
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
    assert "User=10001:10001" in volume
