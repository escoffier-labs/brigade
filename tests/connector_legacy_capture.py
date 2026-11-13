import json
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
        row["preview"] = packs.preview_setup(target, name, runtime_path=runtime)
        row["setup"] = packs.apply_setup(target, name, runtime_path=runtime)
        row["instance"] = json.loads(packs.instance_config_path(target, name).read_text())
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
