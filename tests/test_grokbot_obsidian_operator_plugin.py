"""Node syntax and VM harness for the checked-in operator adapter plugin."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "obsidian-plugin" / "grokbot-operator-adapter"
HARNESS = Path(__file__).resolve().parent / "obsidian_operator_plugin_harness.js"
BUNDLE_SCRIPT = PLUGIN / "scripts" / "bundle-zod.js"
CHECKED_ARTIFACTS = (
    "main.js",
    "manifest.json",
    "SHA256SUMS",
    "src/adapter.js",
    "vendor/zod-3.25.76.cjs",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact_digests(root: Path) -> dict[str, str]:
    return {name: _sha256(root / name) for name in CHECKED_ARTIFACTS}


def _run_bundle_zod(plugin_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("ZOD_TARBALL", None)
    return subprocess.run(
        ["node", str(plugin_root / "scripts" / "bundle-zod.js"), *args],
        check=False,
        capture_output=True,
        text=True,
        cwd=str(plugin_root),
        env=env,
    )


def test_plugin_sources_are_checked_in_commonjs():
    assert (PLUGIN / "main.js").is_file()
    assert (PLUGIN / "manifest.json").is_file()
    manifest = (PLUGIN / "manifest.json").read_text(encoding="utf-8")
    assert '"id": "grokbot-operator-adapter"' in manifest
    assert '"version": "0.1.0"' in manifest
    source = (PLUGIN / "main.js").read_text(encoding="utf-8")
    assert "module.exports" in source
    assert "require(" in source
    assert "obsidian-local-rest-api:loaded" in source
    assert "grokbot_replace_canvas_v1" in source
    assert "grokbot_replace_base_v1" in source
    assert "grokbot_replace_excalidraw_v1" in source
    assert "grokbot_lint_note_v1" in source
    assert "grokbot_auto_move_note_v1" in source
    assert "grokbot_sr_open_review_v1" in source
    assert "grokbot_homepage_open_v1" in source
    assert "grokbot_omnisearch_v1" in source
    assert "grokbot_excalidraw_open_v1" in source
    assert "grokbot_excalidraw_export_v1" in source
    assert "vault.process" in source or "vault.process(" in source
    assert "vault_write" not in source
    assert "command_execute" not in source
    assert "run_plugin_command" not in source
    assert "function inputSchema" not in source
    assert "Private canvas compare-and-swap v0.1.0" in source
    assert "zod@3.25.76" in source
    assert (PLUGIN / "NOTICE").is_file()
    assert (PLUGIN / "vendor" / "LICENSE").is_file()
    assert (PLUGIN / "vendor" / "zod-3.25.76.cjs").is_file()
    lock = (PLUGIN / "vendor" / "zod.lock.json").read_text(encoding="utf-8")
    assert '"version": "3.25.76"' in lock
    assert "sha512-gzUt/qt81nXsFGKIFcC3YnfEAx5NkunCfnDlvuBSSFS02bcXu4Lmea0AFIUwbLWxWPx3d9p8S5QoaujKcNQxcQ==" in lock


def test_node_check_and_vm_harness():
    node = shutil.which("node")
    if node is None:
        pytest.fail("node is required for the operator adapter harness")
    check = subprocess.run([node, "--check", str(PLUGIN / "main.js")], check=False, capture_output=True, text=True)
    assert check.returncode == 0, check.stderr
    harness = subprocess.run([node, str(HARNESS)], check=False, capture_output=True, text=True)
    assert harness.returncode == 0, harness.stdout + harness.stderr
    assert "obsidian operator plugin harness ok" in harness.stdout


def test_bundle_zod_check_is_network_free_and_detects_generated_drift(tmp_path: Path):
    node = shutil.which("node")
    if node is None:
        pytest.fail("node is required for the operator adapter harness")
    assert BUNDLE_SCRIPT.is_file()

    clean = tmp_path / "clean"
    shutil.copytree(PLUGIN, clean)
    before = _artifact_digests(clean)
    matched = _run_bundle_zod(clean, "--check")
    assert matched.returncode == 0, matched.stdout + matched.stderr
    assert _artifact_digests(clean) == before

    drifted = tmp_path / "plugin"
    shutil.copytree(PLUGIN, drifted)
    (drifted / "main.js").write_text(
        (drifted / "main.js").read_text(encoding="utf-8") + "\n// hand-edit\n",
        encoding="utf-8",
    )
    drifted_before = _artifact_digests(drifted)
    failed = _run_bundle_zod(drifted, "--check")
    assert failed.returncode != 0, failed.stdout + failed.stderr
    assert _artifact_digests(drifted) == drifted_before

    (drifted / "main.js").write_bytes((PLUGIN / "main.js").read_bytes())
    (drifted / "src" / "adapter.js").write_text(
        (drifted / "src" / "adapter.js").read_text(encoding="utf-8") + "\n// source-drift\n",
        encoding="utf-8",
    )
    source_before = _artifact_digests(drifted)
    source_failed = _run_bundle_zod(drifted, "--check")
    assert source_failed.returncode != 0, source_failed.stdout + source_failed.stderr
    assert _artifact_digests(drifted) == source_before

    (drifted / "src" / "adapter.js").write_bytes((PLUGIN / "src" / "adapter.js").read_bytes())
    vendor = drifted / "vendor" / "zod-3.25.76.cjs"
    vendor.write_text(vendor.read_text(encoding="utf-8") + "\n// vendor-drift\n", encoding="utf-8")
    vendor_before = _artifact_digests(drifted)
    vendor_failed = _run_bundle_zod(drifted, "--check")
    assert vendor_failed.returncode != 0, vendor_failed.stdout + vendor_failed.stderr
    assert _artifact_digests(drifted) == vendor_before
