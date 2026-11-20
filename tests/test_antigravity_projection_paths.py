"""Antigravity projections live under .agents/ and Brigade-owned .antigravity/ ones are pruned (#2810)."""

import hashlib
import json
from pathlib import Path

from brigade import install as install_mod
from brigade.config import load_config
from brigade.install import install_selection, prune_legacy_antigravity_projections
from brigade.reconfigure import reconfigure
from brigade.selection import Selection
from brigade.skills_cmd import HARNESS_ADAPTERS
from brigade.templates import template_root
from brigade.tools_cmd import constants

MARKER = constants.PROJECTION_MARKER


def _sel(*harnesses: str) -> Selection:
    return Selection(depth="repo", harnesses=list(harnesses), owner=harnesses[0], includes=[])


def _bundled(skill_id: str) -> bytes:
    return (template_root() / "skills" / skill_id / "SKILL.md").read_bytes()


def test_antigravity_skill_adapter_targets_agents_skills():
    assert HARNESS_ADAPTERS["antigravity"]["install_path"] == ".agents/skills/{skill_id}"


def test_antigravity_tool_projections_target_agents_dir():
    paths = [
        tool["projections"]["antigravity"]
        for tool in constants.DEFAULT_TOOLS
        if "antigravity" in tool.get("projections", {})
    ]
    assert paths, "expected at least one antigravity tool projection"
    for rel in paths:
        assert rel.startswith((".agents/commands/", ".agents/superpowers/")), rel


def test_init_wires_default_skills_under_agents_not_antigravity(tmp_path):
    assert install_selection(tmp_path, _sel("antigravity")) == 0
    for skill_id in install_mod.DEFAULT_WIRED_SKILLS:
        assert (tmp_path / ".agents" / "skills" / skill_id / "SKILL.md").is_file()
        assert not (tmp_path / ".antigravity" / "skills" / skill_id).exists()
    # The presence hook shares .agents/ and the handoff inbox stays put.
    assert (tmp_path / ".agents" / "hooks.json").is_file()
    assert (tmp_path / ".antigravity" / "memory-handoffs" / "TEMPLATE.md").is_file()


def _seed_legacy(tmp_path: Path) -> dict[str, Path]:
    legacy = tmp_path / ".antigravity"
    files = {
        "pristine_skill": legacy / "skills" / "brigade-work" / "SKILL.md",
        "edited_skill": legacy / "skills" / "ultra-work-scout" / "SKILL.md",
        "user_skill": legacy / "skills" / "my-own" / "SKILL.md",
        "owned_command": legacy / "commands" / "simplify.md",
        "user_command": legacy / "commands" / "mine.md",
        "owned_superpowers": legacy / "superpowers" / "superpowers.md",
    }
    for path in files.values():
        path.parent.mkdir(parents=True, exist_ok=True)
    files["pristine_skill"].write_bytes(_bundled("brigade-work"))
    files["edited_skill"].write_bytes(b"# operator edited this\n")
    files["user_skill"].write_bytes(b"# not a brigade skill\n")
    files["owned_command"].write_text(f"<!-- {MARKER} {{}} -->\nbody\n", encoding="utf-8")
    files["user_command"].write_text("# my own command\n", encoding="utf-8")
    files["owned_superpowers"].write_text(f"<!-- {MARKER} {{}} -->\nbody\n", encoding="utf-8")
    return files


def test_init_prunes_only_brigade_owned_legacy_projections(tmp_path, capsys):
    files = _seed_legacy(tmp_path)
    handoffs = tmp_path / ".antigravity" / "memory-handoffs"
    handoffs.mkdir(parents=True, exist_ok=True)
    keep = handoffs / "note.md"
    keep.write_text("keep me\n", encoding="utf-8")

    assert install_selection(tmp_path, _sel("antigravity")) == 0

    assert not files["pristine_skill"].exists()
    assert not files["owned_command"].exists()
    assert not files["owned_superpowers"].exists()
    assert not (tmp_path / ".antigravity" / "superpowers").exists()
    # User-owned and edited files are untouched.
    assert files["edited_skill"].read_bytes() == b"# operator edited this\n"
    assert files["user_skill"].read_bytes() == b"# not a brigade skill\n"
    assert files["user_command"].read_text(encoding="utf-8") == "# my own command\n"
    assert keep.read_text(encoding="utf-8") == "keep me\n"
    assert (handoffs / "TEMPLATE.md").is_file()
    out = capsys.readouterr().out
    assert "removed legacy Antigravity projection: .antigravity/skills/brigade-work/SKILL.md" in out


def test_reconfigure_adding_antigravity_prunes_legacy_projections(tmp_path):
    assert install_selection(tmp_path, _sel("claude")) == 0
    files = _seed_legacy(tmp_path)

    assert reconfigure(tmp_path, new_selection=_sel("claude", "antigravity"), prune=False) == 0

    assert "antigravity" in load_config(tmp_path).selection.harnesses
    assert not files["pristine_skill"].exists()
    assert not files["owned_command"].exists()
    assert files["user_command"].is_file()
    assert (tmp_path / ".agents" / "skills" / "brigade-work" / "SKILL.md").is_file()


def test_prune_skips_legacy_skill_recorded_in_wired_json_only_when_unedited(tmp_path):
    skill = tmp_path / ".antigravity" / "skills" / "custom" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_bytes(b"# recorded text\n")
    rel = ".antigravity/skills/custom/SKILL.md"
    recorded = {rel: {"sha256": hashlib.sha256(b"# recorded text\n").hexdigest()}}
    assert prune_legacy_antigravity_projections(tmp_path, recorded) == [rel]

    skill.parent.mkdir(parents=True)
    skill.write_bytes(b"# recorded text, then edited\n")
    assert prune_legacy_antigravity_projections(tmp_path, recorded) == []
    assert skill.is_file()


def test_prune_never_follows_links(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "simplify.md"
    victim.write_text(f"<!-- {MARKER} {{}} -->\n", encoding="utf-8")
    legacy = tmp_path / "repo" / ".antigravity"
    legacy.mkdir(parents=True)
    (legacy / "commands").symlink_to(outside, target_is_directory=True)
    link_dir = legacy / "superpowers"
    link_dir.mkdir()
    (link_dir / "superpowers.md").symlink_to(victim)

    assert prune_legacy_antigravity_projections(tmp_path / "repo") == []
    assert victim.is_file()
    assert (link_dir / "superpowers.md").is_symlink()


def test_prune_without_legacy_dir_is_a_noop(tmp_path):
    assert prune_legacy_antigravity_projections(tmp_path) == []
    assert not (tmp_path / ".antigravity").exists()


def test_gitignore_block_ignores_agents_projections_but_not_hooks():
    block = install_mod.build_gitignore_block(_sel("antigravity"))
    for pattern in (".agents/commands/", ".agents/superpowers/"):
        assert pattern in block
    # .agents/skills/ is a shared cross-harness root that teams commit to.
    assert ".agents/skills/" not in block
    assert ".agents/hooks.json" not in block
    assert ".agents/\n" not in block
    assert ".antigravity/commands/" not in block
    assert ".antigravity/superpowers/" not in block


def test_wired_json_records_new_agents_path(tmp_path):
    assert install_selection(tmp_path, _sel("antigravity")) == 0
    wired = json.loads((tmp_path / ".brigade" / "skills" / "wired.json").read_text())["skills"]
    assert ".agents/skills/brigade-work/SKILL.md" in wired
    assert not any(key.startswith(".antigravity/") for key in wired)
