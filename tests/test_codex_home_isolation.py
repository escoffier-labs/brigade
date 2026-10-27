"""Exercise shared pytest isolation outside the doctor's module fixtures."""

import os
from pathlib import Path
import subprocess
import sys
import textwrap


def test_shared_fixtures_isolate_inherited_codex_home(tmp_path):
    hostile = tmp_path / "inherited-config"
    hostile.mkdir()
    config = hostile / "config.toml"
    body = 'project_doc_max_bytes = "synthetic-hostile-cap"\n'
    config.write_text(body, encoding="utf-8")
    probe = tmp_path / "test_probe.py"
    probe.write_text(
        textwrap.dedent("""\
            import os
            import sys

            from brigade.doctor_instruction_chain import check_codex_instruction_chain

            def test_inherited_home_is_isolated(tmp_path):
                (tmp_path / ".git").mkdir()
                status, name, detail = check_codex_instruction_chain(tmp_path)
                assert "cap source default-assumption" in detail
                assert "32,768 bytes (32 KiB)" in detail
                assert "CODEX_HOME" not in os.environ

            def test_explicit_home_still_works(tmp_path, monkeypatch):
                (tmp_path / "config.toml").write_text("project_doc_max_bytes = 0\\n")
                monkeypatch.setenv("CODEX_HOME", str(tmp_path))
                status, name, detail = check_codex_instruction_chain(tmp_path)
                if sys.version_info < (3, 11):
                    assert status == "MANUAL"
                    assert "config_parser_unavailable" in detail
                elif hasattr(os, "O_NONBLOCK"):
                    assert status == "WARN"
                    assert "cap source user-base-config" in detail
                    assert "loading disabled" in detail
            """),
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "tests.conftest", "--confcutdir", str(tmp_path), str(probe)],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "CODEX_HOME": str(hostile)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout
    assert config.read_text(encoding="utf-8") == body
