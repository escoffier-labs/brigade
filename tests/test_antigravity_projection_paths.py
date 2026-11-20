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
from brigade.tools_cmd import helpers as tools_helpers

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


def _managed_projection(body: str) -> str:
    metadata = json.dumps(
        {"projection_fingerprint": tools_helpers._text_hash(body)}, sort_keys=True, separators=(",", ":")
    )
    return f"<!-- {MARKER} {metadata} -->\n{body}"


def _seed_replacements(tmp_path: Path) -> None:
    """Managed `.agents/` projections, as `brigade tools apply` writes them."""
    for rel in ("commands/simplify.md", "superpowers/superpowers.md"):
        path = tmp_path / ".agents" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_managed_projection("body\n"), encoding="utf-8")


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
    files["owned_command"].write_text(_managed_projection("body\n"), encoding="utf-8")
    files["user_command"].write_text("# my own command\n", encoding="utf-8")
    files["owned_superpowers"].write_text(_managed_projection("body\n"), encoding="utf-8")
    return files


def test_init_prunes_only_brigade_owned_legacy_projections(tmp_path, capsys):
    files = _seed_legacy(tmp_path)
    _seed_replacements(tmp_path)
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
    _seed_replacements(tmp_path)

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
    wired = {("antigravity", "custom")}
    assert prune_legacy_antigravity_projections(tmp_path, recorded, wired) == [rel]

    skill.parent.mkdir(parents=True)
    skill.write_bytes(b"# recorded text, then edited\n")
    assert prune_legacy_antigravity_projections(tmp_path, recorded, wired) == []
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
    # Legacy paths stay ignored for one release, after the new lines.
    for pattern in (".antigravity/commands/", ".antigravity/superpowers/"):
        assert pattern in block
        assert block.index(pattern) > block.index(".agents/superpowers/")
    assert "legacy" in block.lower()
    assert ".antigravity/skills/" not in block


def test_wired_json_records_new_agents_path(tmp_path):
    assert install_selection(tmp_path, _sel("antigravity")) == 0
    wired = json.loads((tmp_path / ".brigade" / "skills" / "wired.json").read_text())["skills"]
    assert ".agents/skills/brigade-work/SKILL.md" in wired
    assert not any(key.startswith(".antigravity/") for key in wired)


def test_prune_keeps_an_edited_managed_projection(tmp_path):
    _seed_replacements(tmp_path)
    command = tmp_path / ".antigravity" / "commands" / "simplify.md"
    command.parent.mkdir(parents=True)
    # Header kept, body edited: the recorded fingerprint no longer matches.
    command.write_text(_managed_projection("body\n").replace("body", "operator edit"), encoding="utf-8")
    assert prune_legacy_antigravity_projections(tmp_path) == []
    assert command.is_file()


def test_prune_keeps_a_marker_lookalike_without_fingerprint(tmp_path):
    (tmp_path / ".agents" / "commands").mkdir(parents=True)
    (tmp_path / ".agents" / "commands" / "notes.md").write_text(_managed_projection("body\n"), encoding="utf-8")
    command = tmp_path / ".antigravity" / "commands" / "notes.md"
    command.parent.mkdir(parents=True)
    command.write_text(f"<!-- {MARKER} {{}} -->\nmy notes mention the marker\n", encoding="utf-8")
    assert prune_legacy_antigravity_projections(tmp_path) == []
    assert command.is_file()


def test_prune_does_not_descend_into_a_reparse_point(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "simplify.md"
    victim.write_text(_managed_projection("body\n"), encoding="utf-8")
    commands = tmp_path / ".antigravity" / "commands"
    commands.mkdir(parents=True)
    junction = commands / "linked"
    junction.mkdir()
    (junction / "simplify.md").write_text(_managed_projection("body\n"), encoding="utf-8")
    replacement = tmp_path / ".agents" / "commands" / "linked" / "simplify.md"
    replacement.parent.mkdir(parents=True)
    replacement.write_text(_managed_projection("body\n"), encoding="utf-8")
    real_is_plain = install_mod._is_plain

    # Stand in for a Windows junction: a directory that reports the reparse-point attribute.
    def fake_is_plain(path, *, directory):
        if path == junction:
            return False
        return real_is_plain(path, directory=directory)

    monkeypatch.setattr(install_mod, "_is_plain", fake_is_plain)
    assert prune_legacy_antigravity_projections(tmp_path) == []
    assert (junction / "simplify.md").is_file()
    assert victim.is_file()


def test_is_plain_rejects_reparse_point_attribute(tmp_path, monkeypatch):
    target = tmp_path / "d"
    target.mkdir()
    real = target.lstat()

    class Info:
        st_mode = real.st_mode
        st_file_attributes = install_mod._FILE_ATTRIBUTE_REPARSE_POINT

    monkeypatch.setattr(Path, "lstat", lambda self: Info() if self == target else real)
    assert install_mod._is_plain(target, directory=True) is False


def test_no_wire_install_prunes_nothing(tmp_path):
    files = _seed_legacy(tmp_path)
    assert install_selection(tmp_path, _sel("antigravity"), wire_skills=False) == 0
    for path in files.values():
        assert path.is_file(), path


def test_prune_keeps_legacy_skill_unless_its_replacement_was_placed(tmp_path):
    skill = tmp_path / ".antigravity" / "skills" / "brigade-work" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_bytes(_bundled("brigade-work"))
    rel = ".antigravity/skills/brigade-work/SKILL.md"

    # No placement this run (blocked, symlinked, or not wired): keep the legacy copy.
    assert prune_legacy_antigravity_projections(tmp_path) == []
    assert prune_legacy_antigravity_projections(tmp_path, wired_skills=set()) == []
    assert prune_legacy_antigravity_projections(tmp_path, wired_skills={("claude", "brigade-work")}) == []
    assert skill.is_file()

    assert prune_legacy_antigravity_projections(tmp_path, wired_skills={("antigravity", "brigade-work")}) == [rel]
    assert not skill.exists()


def test_init_keeps_legacy_skill_when_agents_placement_is_blocked(tmp_path):
    skill = tmp_path / ".antigravity" / "skills" / "brigade-work" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_bytes(_bundled("brigade-work"))
    # A symlinked destination directory makes the .agents placement SYMLINKED, not written.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / ".agents" / "skills").mkdir(parents=True)
    (tmp_path / ".agents" / "skills" / "brigade-work").symlink_to(elsewhere, target_is_directory=True)

    assert install_selection(tmp_path, _sel("antigravity")) == 0

    assert skill.is_file()
    assert not (elsewhere / "SKILL.md").exists()
    # The skill whose placement succeeded is still migrated.
    assert (tmp_path / ".agents" / "skills" / "ultra-work-scout" / "SKILL.md").is_file()


def test_prune_keeps_legacy_tool_projection_without_a_managed_replacement(tmp_path):
    legacy = tmp_path / ".antigravity"
    (legacy / "commands").mkdir(parents=True)
    (legacy / "superpowers").mkdir()
    missing = legacy / "commands" / "simplify.md"
    unmanaged = legacy / "commands" / "frontend.md"
    managed = legacy / "superpowers" / "superpowers.md"
    for path in (missing, unmanaged, managed):
        path.write_text(_managed_projection("body\n"), encoding="utf-8")
    # frontend.md has a replacement the operator took over: no marker, so not managed.
    replacement = tmp_path / ".agents" / "commands" / "frontend.md"
    replacement.parent.mkdir(parents=True)
    replacement.write_text("# operator wrote this\n", encoding="utf-8")
    new_managed = tmp_path / ".agents" / "superpowers" / "superpowers.md"
    new_managed.parent.mkdir(parents=True)
    new_managed.write_text(_managed_projection("body\n"), encoding="utf-8")

    assert prune_legacy_antigravity_projections(tmp_path) == [".antigravity/superpowers/superpowers.md"]
    assert missing.is_file()
    assert unmanaged.is_file()
    assert not managed.exists()
    assert replacement.read_text(encoding="utf-8") == "# operator wrote this\n"


def test_prune_removes_only_directories_its_removals_emptied(tmp_path):
    _seed_replacements(tmp_path)
    nested_new = tmp_path / ".agents" / "commands" / "pack" / "deep" / "tool.md"
    nested_new.parent.mkdir(parents=True)
    nested_new.write_text(_managed_projection("body\n"), encoding="utf-8")
    legacy = tmp_path / ".antigravity"
    nested_old = legacy / "commands" / "pack" / "deep" / "tool.md"
    nested_old.parent.mkdir(parents=True)
    nested_old.write_text(_managed_projection("body\n"), encoding="utf-8")
    (legacy / "commands" / "simplify.md").write_text(_managed_projection("body\n"), encoding="utf-8")
    # Pre-existing empty user directories, none of them parents of a removed file.
    user_empty = legacy / "commands" / "my-empty-dir"
    user_empty.mkdir()
    sibling_empty = legacy / "commands" / "pack" / "also-empty"
    sibling_empty.mkdir()
    superpowers_empty = legacy / "superpowers"
    superpowers_empty.mkdir()
    skills_empty = legacy / "skills" / "kept-empty"
    skills_empty.mkdir(parents=True)

    removed = prune_legacy_antigravity_projections(tmp_path)

    assert sorted(removed) == [
        ".antigravity/commands/pack/deep/tool.md",
        ".antigravity/commands/simplify.md",
    ]
    assert not (legacy / "commands" / "pack" / "deep").exists()
    assert user_empty.is_dir()
    assert sibling_empty.is_dir()
    # "pack" still holds the sibling, so the walk stopped there.
    assert (legacy / "commands" / "pack").is_dir()
    assert superpowers_empty.is_dir()
    assert skills_empty.is_dir()
    assert (legacy / "commands").is_dir()


def test_prune_removes_a_directory_chain_up_to_and_including_the_base(tmp_path):
    new = tmp_path / ".agents" / "commands" / "only" / "tool.md"
    new.parent.mkdir(parents=True)
    new.write_text(_managed_projection("body\n"), encoding="utf-8")
    old = tmp_path / ".antigravity" / "commands" / "only" / "tool.md"
    old.parent.mkdir(parents=True)
    old.write_text(_managed_projection("body\n"), encoding="utf-8")

    assert prune_legacy_antigravity_projections(tmp_path) == [".antigravity/commands/only/tool.md"]
    assert not (tmp_path / ".antigravity" / "commands").exists()
    assert (tmp_path / ".antigravity").is_dir()


def _legacy_catalog(tmp_path: Path, *, customize: str | None = None) -> Path:
    """Write a tools catalog as a pre-#2810 install did: antigravity on the old `.antigravity/` paths."""
    from brigade.tools_cmd import paths as tools_paths

    tools = []
    for tool in constants.DEFAULT_TOOLS:
        entry = {**tool, "projections": dict(tool["projections"])}
        current = entry["projections"].get("antigravity")
        if current:
            entry["projections"]["antigravity"] = current.replace(".agents/", ".antigravity/", 1)
        if customize and entry["id"] == customize:
            entry["projections"]["antigravity"] = ".antigravity/commands/my-custom-name.md"
        tools.append(entry)
    path = tools_paths.config_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tools_helpers._format_tool_entries(tools), encoding="utf-8")
    return path


def _catalog_antigravity_paths(path: Path) -> dict[str, str]:
    import tomllib

    tools = tomllib.loads(path.read_text(encoding="utf-8"))["tool"]
    return {t["id"]: t["projections"]["antigravity"] for t in tools if "antigravity" in t.get("projections", {})}


def test_install_migrates_legacy_antigravity_paths_in_the_tools_catalog(tmp_path, capsys):
    path = _legacy_catalog(tmp_path, customize="simplify")
    before = _catalog_antigravity_paths(path)
    assert before and all(v.startswith(".antigravity/") for v in before.values())

    assert install_selection(tmp_path, _sel("antigravity")) == 0

    after = _catalog_antigravity_paths(path)
    defaults = {
        t["id"]: t["projections"]["antigravity"] for t in constants.DEFAULT_TOOLS if "antigravity" in t["projections"]
    }
    for tool_id, rel in after.items():
        if tool_id == "simplify":
            # Operator-customized: untouched.
            assert rel == ".antigravity/commands/my-custom-name.md"
        else:
            assert rel == defaults[tool_id]
            assert rel.startswith(".agents/")
    assert "moved tools catalog antigravity projection" in capsys.readouterr().out


def test_catalog_migration_is_idempotent_and_leaves_other_harness_paths_alone(tmp_path):
    from brigade.tools_cmd.catalog_manage import migrate_legacy_antigravity_paths

    path = _legacy_catalog(tmp_path)
    claude_before = path.read_text(encoding="utf-8").count(".claude/commands/")

    migrated = migrate_legacy_antigravity_paths(tmp_path)
    assert migrated
    assert migrate_legacy_antigravity_paths(tmp_path) == []
    text = path.read_text(encoding="utf-8")
    assert text.count(".claude/commands/") == claude_before
    assert ".antigravity/" not in text


def test_catalog_migration_ignores_a_missing_or_invalid_catalog(tmp_path):
    from brigade.tools_cmd import paths as tools_paths
    from brigade.tools_cmd.catalog_manage import migrate_legacy_antigravity_paths

    assert migrate_legacy_antigravity_paths(tmp_path) == []
    path = tools_paths.config_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not = [valid toml\n", encoding="utf-8")
    assert migrate_legacy_antigravity_paths(tmp_path) == []
    assert path.read_text(encoding="utf-8") == "not = [valid toml\n"


def test_no_wire_install_leaves_the_tools_catalog_alone(tmp_path):
    path = _legacy_catalog(tmp_path)
    before = path.read_text(encoding="utf-8")
    assert install_selection(tmp_path, _sel("antigravity"), wire_skills=False) == 0
    assert path.read_text(encoding="utf-8") == before
