"""Child coverage must survive the exact per-shard files uploaded by CI."""

import ast
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

from coverage import CoverageData

ROOT = Path(__file__).resolve().parents[1]


def test_ci_coverage_reports_after_fixture_sources_are_removed(tmp_path):
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    command = next(
        line.split("run: ", 1)[1]
        for line in workflow.splitlines()
        if "run: python scripts/ci_pytest_shard.py" in line and "--cov=" in line
    )
    command = command.replace("${{ matrix.shard }}", "0")
    argv = [argument.replace("$PWD", str(ROOT)) for argument in shlex.split(command)]
    argv[0] = sys.executable
    # One bounded shard exercises every selected fixture; keep CI's source argument.
    argv[argv.index("--shard-count") + 1] = "1"
    fixtures = tmp_path / "fixtures"
    shard = tmp_path / ".coverage.3.12.0"
    environment = dict(os.environ, COVERAGE_FILE=str(shard))
    result = subprocess.run(
        [
            *argv,
            f"--cov-config={ROOT / 'pyproject.toml'}",
            f"--basetemp={fixtures}",
            str(ROOT / "tests/test_isolated_exec.py"),
            str(ROOT / "tests/test_journal_export.py") + "::test_real_cli_stdout_is_original_utf8_and_lf_bytes",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    uploaded = tmp_path / "uploaded"
    uploaded.mkdir()
    shutil.copy2(shard, uploaded / shard.name)
    # The reporting job has the checkout and uploaded data, not pytest's fixtures.
    shutil.rmtree(fixtures)
    combined = tmp_path / ".coverage"
    environment = dict(os.environ, COVERAGE_FILE=str(combined))
    for arguments in (["combine", str(uploaded)], ["report"]):
        result = subprocess.run(
            [sys.executable, "-m", "coverage", *arguments],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=45,
        )
        assert result.returncode == 0, result.stdout + result.stderr
    data = CoverageData(basename=str(combined))
    data.read()
    source = ROOT / "src/brigade"
    assert all(Path(filename).is_relative_to(source) for filename in data.measured_files())
    entrypoint = source / "__main__.py"
    child_only_calls = {
        node.lineno
        for node in ast.walk(ast.parse(entrypoint.read_text()))
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
    }
    assert child_only_calls
    assert child_only_calls <= set(data.lines(str(entrypoint)) or ())


def test_nested_subprocess_coverage_survives_shard_upload_and_combine(tmp_path):
    probe = tmp_path / "coverage_probe.py"
    probe.write_text(
        "def child(case):\n"
        "    if case == 0:\n"
        '        return "child-zero"\n'
        '    return "child-one"\n'
        "\n"
        "def grandchild(case):\n"
        "    if case == 0:\n"
        '        return "grandchild-zero"\n'
        '    return "grandchild-one"\n'
    )
    suite = tmp_path / "test_probe.py"
    # Run twice against the same source with disjoint executed lines. This also
    # detects child data leaking between isolated coverage runs.
    for case in (0, 1):
        suffix = "zero" if case == 0 else "one"
        grandchild = f"from coverage_probe import grandchild; assert grandchild({case}) == 'grandchild-{suffix}'"
        child = (
            f"from coverage_probe import child; assert child({case}) == 'child-{suffix}'; "
            f"import subprocess, sys; subprocess.run([sys.executable, '-c', {grandchild!r}], check=True)"
        )
        suite.write_text(
            "import subprocess, sys, pytest\n"
            "@pytest.mark.parametrize('sample', range(8))\n"
            "def test_nested(sample):\n"
            f"    subprocess.run([sys.executable, '-c', {child!r}], check=True)\n"
        )
        run = tmp_path / f"run-{case}"
        run.mkdir()
        uploaded = run / "uploaded"
        uploaded.mkdir()
        expected_lines = {3, 8} if case == 0 else {4, 9}
        other_lines = {4, 9} if case == 0 else {3, 8}
        for shard in range(2):
            data_file = run / f".coverage.3.12.{shard}"
            env = dict(os.environ, COVERAGE_FILE=str(data_file), PYTHONPATH=str(tmp_path))
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/ci_pytest_shard.py"),
                    "--shard-index",
                    str(shard),
                    "--shard-count",
                    "2",
                    "--",
                    "-q",
                    "--cov=coverage_probe",
                    "--cov-report=",
                    f"--cov-config={ROOT / 'pyproject.toml'}",
                    str(suite),
                ],
                cwd=tmp_path,
                env=env,
                capture_output=True,
                text=True,
                timeout=45,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            data = CoverageData(basename=str(data_file))
            data.read()
            lines = set(data.lines(str(probe)) or ())
            assert expected_lines <= lines
            assert not other_lines & lines
            # Copy only the exact artifact path, never any child sidecar files.
            shutil.copy2(data_file, uploaded / data_file.name)
        combined = run / ".coverage"
        result = subprocess.run(
            [sys.executable, "-m", "coverage", "combine", str(uploaded)],
            cwd=tmp_path,
            env=dict(os.environ, COVERAGE_FILE=str(combined)),
            capture_output=True,
            text=True,
            timeout=45,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        data = CoverageData(basename=str(combined))
        data.read()
        lines = set(data.lines(str(probe)) or ())
        assert expected_lines <= lines
        assert not other_lines & lines
