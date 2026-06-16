import json
from pathlib import Path

from brigade import status as status_mod
from brigade.install import install_selection
from brigade.selection import Selection


def test_status_lists_stations_for_installed_workspace(tmp_target: Path, capsys):
    install_selection(
        tmp_target,
        Selection(depth="workspace", harnesses=["claude"], owner="claude", includes=[]),
    )
    rc = status_mod.run(target=tmp_target)
    out = capsys.readouterr().out
    assert rc == 0
    assert "core" in out
    assert "memory" in out
    assert "guard" in out
    assert "security" in out


def test_status_runs_on_empty_dir(tmp_target: Path, capsys):
    tmp_target.mkdir()
    rc = status_mod.run(target=tmp_target)
    out = capsys.readouterr().out
    # status never fails; it reports health, it does not gate
    assert rc == 0
    assert "core" in out


def test_status_json_output_is_structured(tmp_target: Path, capsys):
    install_selection(
        tmp_target,
        Selection(depth="workspace", harnesses=["claude"], owner="claude", includes=[]),
    )
    capsys.readouterr()  # drain install output so only the status JSON remains
    rc = status_mod.run(target=tmp_target, json_output=True)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["target"].endswith("ws")
    names = {row["station"] for row in payload["stations"]}
    assert {"core", "memory", "guard", "security"} <= names
    first = payload["stations"][0]
    assert {"station", "health", "ok", "warn", "fail", "summary"} <= set(first)
