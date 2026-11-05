"""Pin the native guard's PCRE2 build and its CI benchmark smoke job.

The linked-library version itself is asserted in Rust
(`linked_pcre2_is_the_pinned_bundled_release`), where the binary can report it.
These tests keep the pieces that make that assertion hold, and the workflow
that exercises the benchmark, from drifting apart.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "engines/brigade-guard"
WORKFLOW = ROOT / ".github/workflows/guard-native.yml"
BENCHMARK = ROOT / "scripts/benchmark-guard-native.py"
BINARY = ENGINE / "target/release/brigade-guard"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _pinned(name: str) -> str:
    match = re.search(rf'^{re.escape(name)} = "=(\d+\.\d+\.\d+)"$', _read(ENGINE / "Cargo.toml"), re.MULTILINE)
    assert match, f"{name} must be pinned with an exact = requirement in Cargo.toml"
    return match[1]


def _locked(name: str) -> str:
    match = re.search(
        rf'\[\[package\]\]\nname = "{re.escape(name)}"\nversion = "([^"]+)"', _read(ENGINE / "Cargo.lock")
    )
    assert match, f"{name} missing from Cargo.lock"
    return match[1]


def test_pcre2_crates_are_pinned_exactly_and_match_the_lockfile():
    for crate in ("pcre2", "pcre2-sys"):
        assert _pinned(crate) == _locked(crate)


def test_bundled_pcre2_is_forced_for_every_cargo_working_directory():
    # Cargo reads .cargo/config.toml from the working directory upward, so the
    # repository root covers `--manifest-path` builds and `cd engines/...` ones.
    config = _read(ROOT / ".cargo/config.toml")
    assert re.search(r'^PCRE2_SYS_STATIC = \{ value = "1", force = true \}$', config, re.MULTILINE)
    assert not (ENGINE / ".cargo").exists(), "an engine-local config is not read by --manifest-path builds"


def test_pinned_pcre2_release_is_recorded_in_rust_and_readme():
    pinned = re.search(
        r'const PINNED_PCRE2_VERSION: &str = "(\d+\.\d+) (\d{4}-\d{2}-\d{2})";', _read(ENGINE / "src/main.rs")
    )
    assert pinned, "PINNED_PCRE2_VERSION constant missing from main.rs"
    assert f"**{pinned[1]} ({pinned[2]})" in _read(ENGINE / "README.md")


def test_guard_workflow_runs_a_bounded_benchmark_smoke_and_uploads_the_numbers():
    text = _read(WORKFLOW)
    step = text[text.index("      - name: Benchmark smoke") : text.index("      - name: Upload benchmark numbers")]
    assert "timeout-minutes: 3" in step
    assert "brigade work verify run" in step
    assert '"scripts/benchmark-guard-native.py","--smoke"' in step
    assert '"--output","guard-bench/guard-native-smoke.jsonl"' in step
    upload = text[text.index("      - name: Upload benchmark numbers") :]
    assert "if: ${{ !cancelled() }}" in upload
    assert "uses: actions/upload-artifact@v7" in upload
    assert "path: guard-bench/guard-native-smoke.jsonl" in upload
    # The step runs in the job that already built the release binary.
    assert text.index("./scripts/verify-rust-guard") < text.index("      - name: Benchmark smoke")
    # No timing threshold gates the job.
    assert "continue-on-error" not in step
    for path in ("scripts/benchmark-guard*", ".cargo/**"):
        assert text.count(f"      - '{path}'") == 2, f"{path} must trigger both pull_request and push"


@pytest.mark.skipif(not BINARY.exists(), reason="needs the release brigade-guard binary (cargo build --release)")
def test_benchmark_smoke_runs_all_backends_and_records_the_engine(tmp_path):
    output = tmp_path / "numbers.jsonl"
    result = subprocess.run(
        [sys.executable, str(BENCHMARK), "--smoke", "--output", str(output)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert records[0]["kind"] == "meta" and records[0]["smoke"] is True
    assert records[0]["engine"]["pcre2_version"].startswith("10.")
    runs = records[1:]
    assert {(r["size"], r["backend"]) for r in runs} == {
        (size, backend) for size in ("small", "64KiB", "medium") for backend in ("python", "native", "auto")
    }
    assert all(r["run"] == 1 and r["seconds"] > 0 for r in runs)
    assert result.stdout.count("\n") == len(records)
