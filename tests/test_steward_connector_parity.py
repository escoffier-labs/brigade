"""Compare surviving profiles with behavior captured from origin/overhaul."""

import json
from pathlib import Path

from tests.dot_support import requires_connector_extra, requires_local_state
from tests.connector_steward_capture import capture


@requires_connector_extra
@requires_local_state
def test_surviving_connector_lifecycle_matches_base(dot_private_dir):
    expected = json.loads((Path(__file__).parent / "fixtures/steward_connector_base.json").read_text())
    assert capture(dot_private_dir.parent / "capture") == expected


@requires_connector_extra
def test_dot_and_duet_work_without_steward_imports(monkeypatch):
    import builtins
    from tests.test_cloud_agent_parity import inventory
    from brigade.dot.profile import DOT, DUET

    original = builtins.__import__

    def without_stewards(name, *args, **kwargs):
        assert not name.startswith(
            ("brigade.grokbot_fleet", "brigade.grokbot_obsidian", "brigade.grokbot_t3", "brigade.grokbot_wazuh")
        )
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_stewards)
    for profile in (DOT, DUET):
        assert set(inventory(profile)) == profile.tools


def test_profiles_import_without_steward_packages():
    import subprocess
    import sys

    code = """
import sys
class Block:
    def find_spec(self, fullname, *args):
        if fullname.startswith(("brigade.grokbot_fleet", "brigade.grokbot_obsidian", "brigade.grokbot_t3", "brigade.grokbot_wazuh")):
            raise AssertionError(fullname)
sys.meta_path.insert(0, Block())
from brigade.dot import DOT, DUET, CLOUD_AGENT, lifecycle
from brigade.dot.adapter import DotTools
assert {p.pack_id for p in (DOT, DUET, CLOUD_AGENT)} == {"dot", "duet", "cloud-agent"}
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
