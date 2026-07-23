import json
from pathlib import Path

import pytest

from brigade import cli
from brigade.install import install_selection
from brigade.selection import Selection, SurfaceInstallRefusal, SurfaceRecord


FIXTURES = Path(__file__).parents[1] / "docs" / "research" / "fixtures" / "harness-contract.v1"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def _surface(name: str, availability: dict) -> SurfaceRecord:
    fixture = _fixture(name)
    return SurfaceRecord.from_fixture(
        fixture,
        projection_harness="cursor",
        availability=availability,
    )


def _selection(*surfaces: SurfaceRecord) -> Selection:
    return Selection(depth="repo", harnesses=["cursor"], owner="cursor", surfaces=list(surfaces))


def test_native_available_surface_installs_unchanged(tmp_path: Path) -> None:
    surface = _surface("cursor-cli", {"state": "available"})

    assert install_selection(tmp_path, _selection(surface), wire_skills=False) == 0

    assert (tmp_path / ".cursor" / "memory-handoffs" / "TEMPLATE.md").is_file()
    persisted = json.loads((tmp_path / ".brigade" / "surface-evidence.json").read_text())
    evidence = persisted["surfaces"]["cursor-cli"]
    assert evidence["projection_harness"] == "cursor"
    assert evidence["runtime_present"] is True
    assert evidence["install_mode"] == "native"


def test_binary_not_found_refusal_happens_before_any_filesystem_write(tmp_path: Path) -> None:
    surface = _surface("cursor-cli", {"state": "externally_blocked", "reason": "binary_not_found"})

    with pytest.raises(SurfaceInstallRefusal, match="cursor-cli.*binary_not_found"):
        install_selection(tmp_path, _selection(surface), wire_skills=False)

    assert list(tmp_path.iterdir()) == []


def test_binary_not_found_projection_only_opt_in_writes_without_runtime_claim(tmp_path: Path) -> None:
    surface = _surface("cursor-cli", {"state": "externally_blocked", "reason": "binary_not_found"})

    assert install_selection(tmp_path, _selection(surface), projection_only=True, wire_skills=False) == 0

    evidence = json.loads((tmp_path / ".brigade" / "surface-evidence.json").read_text())["surfaces"]["cursor-cli"]
    assert evidence["runtime_present"] is False
    assert evidence["install_mode"] == "projection_only"


def test_external_only_surface_projection_only_opt_in_writes_brigade_projections(tmp_path: Path) -> None:
    surface = _surface("cursor-gui", {"state": "external_only"})

    assert install_selection(tmp_path, _selection(surface), projection_only=True, wire_skills=False) == 0

    assert (tmp_path / ".cursor" / "memory-handoffs" / "TEMPLATE.md").is_file()
    persisted = json.loads((tmp_path / ".brigade" / "surface-evidence.json").read_text())
    evidence = persisted["surfaces"]["cursor-gui"]
    assert evidence["runtime_present"] is False
    assert evidence["install_mode"] == "projection_only"


def test_external_only_surface_without_projection_only_raises(tmp_path: Path) -> None:
    surface = _surface("cursor-gui", {"state": "external_only"})

    with pytest.raises(SurfaceInstallRefusal, match="cursor-gui.*projection-only"):
        install_selection(tmp_path, _selection(surface), wire_skills=False)

    assert list(tmp_path.iterdir()) == []


def test_other_externally_blocked_reason_is_not_refused(tmp_path: Path) -> None:
    surface = _surface("cursor-cli", {"state": "externally_blocked", "reason": "output_overflow"})

    assert install_selection(tmp_path, _selection(surface), wire_skills=False) == 0
    persisted = json.loads((tmp_path / ".brigade" / "surface-evidence.json").read_text())
    assert persisted["surfaces"]["cursor-cli"]["runtime_present"] is False


def test_cursor_cli_and_cursor_gui_preserve_separate_fixture_capability_evidence(tmp_path: Path) -> None:
    cli_fixture = _fixture("cursor-cli")
    gui_fixture = _fixture("cursor-gui")
    cli_surface = SurfaceRecord.from_fixture(
        cli_fixture,
        projection_harness="cursor",
        availability={"state": "available"},
    )
    gui_surface = SurfaceRecord.from_fixture(
        gui_fixture,
        projection_harness="cursor",
        availability={"state": "external_only"},
    )
    cli_fixture["capabilities"][0]["claim"] = "mutated after selection"
    gui_fixture["capabilities"][0]["scope"] = "mutated after selection"

    assert (
        install_selection(
            tmp_path,
            _selection(cli_surface, gui_surface),
            projection_only=True,
            wire_skills=False,
        )
        == 0
    )

    persisted = json.loads((tmp_path / ".brigade" / "surface-evidence.json").read_text())["surfaces"]
    assert set(persisted) == {"cursor-cli", "cursor-gui"}
    assert persisted["cursor-cli"]["capabilities"][0]["scope"] == "project"
    assert persisted["cursor-gui"]["capabilities"][0]["scope"] == "desktop"
    assert persisted["cursor-cli"]["capabilities"][0]["claim"] != "mutated after selection"
    assert persisted["cursor-gui"]["capabilities"][0]["scope"] != "mutated after selection"
    assert persisted["cursor-cli"]["runtime_present"] is True
    assert persisted["cursor-gui"]["runtime_present"] is False
    assert persisted["cursor-cli"]["install_mode"] == "native"
    assert persisted["cursor-gui"]["install_mode"] == "projection_only"


def test_harness_install_cursor_user_scope_requires_projection_only_for_gui_projection(capsys) -> None:
    rc = cli.main(["harness", "install", "cursor", "--scope", "user", "--surface", "cursor-gui"])
    captured = capsys.readouterr()

    assert rc == 2
    assert "projection-only" in captured.err
    assert "Traceback" not in captured.err


def test_harness_native_cursor_cli_surface_keeps_existing_install_path(monkeypatch) -> None:
    calls: list[dict] = []

    def native_install(*, write: bool, json_output: bool) -> int:
        calls.append({"write": write, "json_output": json_output})
        return 31

    monkeypatch.setattr("brigade.selection.shutil.which", lambda command: f"/fixture-bin/{command}")
    monkeypatch.setattr("brigade.cursor_user_cmd.install", native_install)

    rc = cli.main(["harness", "install", "cursor", "--scope", "user", "--surface", "cursor-cli", "--write"])

    assert rc == 31
    assert calls == [{"write": True, "json_output": False}]


def test_harness_blocked_cursor_cli_surface_refuses_without_projection_only(monkeypatch, capsys) -> None:
    monkeypatch.setattr("brigade.selection.shutil.which", lambda command: None)

    rc = cli.main(["harness", "install", "cursor", "--scope", "user", "--surface", "cursor-cli"])
    captured = capsys.readouterr()

    assert rc == 2
    assert "externally_blocked: binary_not_found" in captured.err
    assert "Traceback" not in captured.err


def test_harness_install_cursor_user_scope_keeps_legacy_behavior_without_surface(monkeypatch) -> None:
    calls: list[dict] = []

    def legacy_install(*, write: bool, json_output: bool) -> int:
        calls.append({"write": write, "json_output": json_output})
        return 23

    monkeypatch.setattr("brigade.cursor_user_cmd.install", legacy_install)

    assert cli.main(["harness", "install", "cursor", "--scope", "user", "--write", "--json"]) == 23
    assert calls == [{"write": True, "json_output": True}]


def test_harness_explicit_surface_projection_only_delegates_to_user_installer_without_repo_write(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[dict] = []

    def user_install(*, write: bool, json_output: bool) -> int:
        calls.append({"write": write, "json_output": json_output})
        return 29

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("brigade.selection.shutil.which", lambda command: None)
    monkeypatch.setattr("brigade.cursor_user_cmd.install", user_install)

    rc = cli.main(
        [
            "harness",
            "install",
            "cursor",
            "--scope",
            "user",
            "--surface",
            "cursor-gui",
            "--projection-only",
            "--write",
            "--json",
        ]
    )

    assert rc == 29
    assert calls == [{"write": True, "json_output": True}]
    assert list(tmp_path.iterdir()) == []


def test_harness_projection_only_requires_an_explicit_surface(capsys) -> None:
    rc = cli.main(["harness", "install", "cursor", "--scope", "user", "--projection-only"])
    captured = capsys.readouterr()

    assert rc == 2
    assert "requires --surface" in captured.err
    assert "Traceback" not in captured.err


def test_harness_surface_refusal_is_normal_cli_error(monkeypatch, capsys) -> None:
    def refuse(*args, **kwargs) -> int:
        raise SurfaceInstallRefusal("cursor-cli", "availability is externally_blocked: binary_not_found")

    monkeypatch.setattr("brigade.cursor_user_cmd.install", refuse)

    rc = cli.main(
        [
            "harness",
            "install",
            "cursor",
            "--scope",
            "user",
            "--surface",
            "cursor-cli",
            "--projection-only",
        ]
    )
    captured = capsys.readouterr()

    assert rc == 2
    assert "binary_not_found" in captured.err
    assert "Traceback" not in captured.err
