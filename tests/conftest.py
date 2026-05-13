import pytest


@pytest.fixture
def tmp_target(tmp_path):
    """Return a clean temp directory to act as a workspace target."""
    return tmp_path / "ws"
