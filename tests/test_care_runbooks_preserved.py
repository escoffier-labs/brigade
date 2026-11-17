"""Writing shipped memory-care runbooks never deletes an operator's runbooks.

Before this fix, ``brigade care install`` on a workspace that lacked one newly
shipped runbook replaced the whole ``.brigade/memory-care/runbooks`` directory
with the templates, deleting every other runbook (evidence crawl, closeout,
refresh) and overwriting customized shipped ones.
"""

from __future__ import annotations

from pathlib import Path

from brigade import care_cmd, memory_cmd


def _runbook_dir(target: Path) -> Path:
    return memory_cmd._memory_care_runbooks_dir(target)


def _seed(target: Path) -> tuple[Path, Path, bytes]:
    assert memory_cmd.init(target=target, force=True, update_gitignore=False, with_runbooks=True) == 0
    runbooks = _runbook_dir(target)
    own = runbooks / "evidence-crawl.json"
    own.write_text('{"id": "evidence-crawl", "operator": "own runbook"}\n', encoding="utf-8")
    customized = runbooks / memory_cmd.MEMORY_CARE_RUNBOOK_NAMES[1]
    custom_bytes = customized.read_bytes() + b"\n"
    customized.write_bytes(custom_bytes)
    return own, customized, custom_bytes


def test_care_install_adds_a_missing_shipped_runbook_without_touching_the_rest(tmp_path, capsys):
    own, customized, custom_bytes = _seed(tmp_path)
    missing = _runbook_dir(tmp_path) / memory_cmd.MEMORY_CARE_RUNBOOK_NAMES[0]
    missing.unlink()
    capsys.readouterr()

    assert care_cmd._ensure_memory_care_runbooks(tmp_path) == 0

    assert missing.is_file()
    assert own.read_text(encoding="utf-8") == '{"id": "evidence-crawl", "operator": "own runbook"}\n'
    assert customized.read_bytes() == custom_bytes


def test_memory_care_init_with_runbooks_refreshes_shipped_files_and_keeps_others(tmp_path, capsys):
    own, customized, custom_bytes = _seed(tmp_path)
    capsys.readouterr()

    assert memory_cmd.init(target=tmp_path, force=True, update_gitignore=False, with_runbooks=True) == 0

    # The explicit init refreshes the shipped templates but never deletes a runbook it does not ship.
    assert own.is_file()
    assert customized.read_bytes() != custom_bytes
    for name in memory_cmd.MEMORY_CARE_RUNBOOK_NAMES:
        assert (_runbook_dir(tmp_path) / name).is_file()
