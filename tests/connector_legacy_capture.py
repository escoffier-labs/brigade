import json
import os
from unittest.mock import patch
import sys
from pathlib import Path
from brigade import grokbot_packs as packs, grokbot_ops
from tests.test_dot_runtime import _document
from tests.test_duet_profile import _duet_document


def capture(root):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    result = {}
    for name in ("dot", "duet"):
        target = root / name
        target.mkdir(mode=0o700)
        runtime = target / "runtime.json"
        runtime.write_text(json.dumps(_document() if name == "dot" else _duet_document()))
        runtime.chmod(0o600)
        row = {}
        from brigade.dot import connector_registry as registry

        row["setup_parity"] = {}
        stray = target / ".brigade/grokbot/packs/backup.json"
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_text("{")
        row["setup_parity"]["stray"] = packs.preview_setup(target, name, runtime_path=runtime)
        stray.unlink()
        old_cwd = Path.cwd()
        try:
            os.chdir(root)
            row["setup_parity"]["relative"] = registry.apply_setup(Path(name), name, runtime_path=runtime)
        finally:
            os.chdir(old_cwd)
        row["setup_parity"]["relative_instance"] = json.loads(registry.instance_config_path(target, name).read_text())
        if os.name != "nt":
            link = root / (name + "-link")
            link.symlink_to(target, target_is_directory=True)
            try:
                row["setup_parity"]["symlink"] = registry.apply_setup(link, name, runtime_path=runtime)
            except registry.PackError as error:
                row["setup_parity"]["symlink"] = {"error": error.reason}
            link.unlink()
        for exception in (TypeError, OSError):
            with patch.object(registry, "_write", side_effect=exception("example write failure")):
                try:
                    outcome = registry.apply_setup(target, name, runtime_path=runtime)
                except registry.PackError as error:
                    outcome = {"error": error.reason}
                except TypeError:
                    outcome = {"error": "TypeError"}
                row["setup_parity"][exception.__name__] = outcome
        row["preview"] = packs.preview_setup(target, name, runtime_path=runtime)
        row["setup"] = packs.apply_setup(target, name, runtime_path=runtime)
        row["instance"] = json.loads(packs.instance_config_path(target, name).read_text())
        row["update_preview"] = packs.preview_update(target, name)
        row["update"] = packs.apply_update(target, name)
        instance = packs.instance_config_path(target, name)
        original = instance.read_text()
        row["malformed_updates"] = {}
        for key, value in (("bind", 42), ("pack_version", 42), ("bind", "0.0.0.0:8999")):
            document = json.loads(original)
            document[key] = value
            if isinstance(value, str):
                document["runtime_path"] = str(target / "missing")
            instance.write_text(json.dumps(document))
            try:
                outcome = packs.preview_update(target, name)
            except packs.PackError as error:
                outcome = {"error": error.reason}
            row["malformed_updates"][key + str(value)] = outcome
        instance.write_text(original)
        row["unsafe_neighbors"] = {}
        for kind in ("instance", "queue"):
            neighbor = (
                packs.instance_config_path(target, "grokbot-memory")
                if kind == "instance"
                else grokbot_ops.config_path(target, "operator")
            )
            neighbor.parent.mkdir(parents=True, exist_ok=True)
            neighbor.write_text(json.dumps({"bind": "0.0.0.0:8998"}))
            neighbor.chmod(0o600)
            try:
                outcome = packs.preview_setup(target, name, runtime_path=runtime)
            except packs.PackError as error:
                outcome = {"error": error.reason}
            row["unsafe_neighbors"][kind] = outcome
            neighbor.unlink()
        units = root / (name + "-units")
        row["install"] = packs.apply_install_service(target, name, out_dir=units)
        row["unit"] = next(units.glob("*.service")).read_text()
        try:
            row["remove_installed"] = packs.apply_remove(target, name, unit_dir=units)
        except packs.PackError as error:
            row["remove_installed"] = {"error": error.reason}
        # The legacy route owns the Grok-named unit, independently of the connector service.
        legacy = units / grokbot_ops.unit_name(name)
        legacy.write_text("legacy service\n")
        row["remove_preview"] = packs.preview_remove(target, name, unit_dir=units)
        row["remove"] = packs.apply_remove(target, name, unit_dir=units)
        row["remaining"] = sorted(p.name for p in units.iterdir())
        for text, replacement in (
            (str(root), "<ROOT>"),
            (sys.executable, "<PYTHON>"),
            (str(Path(packs.__file__).resolve().parents[2]), "<CWD>"),
        ):
            row = json.loads(json.dumps(row).replace(text, replacement))
        result[name] = row
    return result


if __name__ == "__main__":
    print(json.dumps(capture(Path(sys.argv[1])), indent=2, sort_keys=True))
