"""Capture surviving connector behavior around the steward helper extraction."""

import json
from pathlib import Path
import sys
from unittest.mock import patch

from brigade.dot import connector_registry as registry, lifecycle
from tests.test_cloud_agent_profile import cloud_document
from tests.connector_legacy_capture import capture as legacy_capture


def capture(root):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    result = {"legacy": legacy_capture(root / "legacy")}
    target = root / "cloud"
    target.mkdir(mode=0o700)
    runtime = target / "runtime.json"
    runtime.write_text(json.dumps(cloud_document(target)))
    runtime.chmod(0o600)
    row = {}
    row["preview"] = registry.preview_setup(target, "cloud-agent", runtime_path=runtime)
    row["setup"] = registry.apply_setup(target, "cloud-agent", runtime_path=runtime)
    row["update"] = registry.apply_update(target, "cloud-agent")
    row["unit"] = registry.render_install_service(target, "cloud-agent")
    with (
        patch.object(lifecycle, "_probe", return_value=(401, None)),
        patch.object(lifecycle, "authenticated_authority", return_value=None),
    ):
        row["doctor"] = registry.doctor(target, "cloud-agent")
        row["canary"] = registry.canary(target, "cloud-agent")
    row["remove"] = registry.apply_remove(target, "cloud-agent")
    row["retained"] = sorted(str(p.relative_to(target)) for p in target.rglob("*") if p.is_file())
    encoded = json.dumps(row).replace(str(root), "<ROOT>").replace(sys.executable, "<PYTHON>")
    encoded = encoded.replace(str(Path(lifecycle.__file__).resolve().parents[2]), "<CWD>")
    result["cloud-agent"] = json.loads(encoded)
    return result


if __name__ == "__main__":
    print(json.dumps(capture(Path(sys.argv[1])), indent=2, sort_keys=True))
