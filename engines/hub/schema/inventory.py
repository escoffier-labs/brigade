"""Record exact fresh/upgrade differences and their fixture provenance."""

import json
import pathlib
import runpy
import tempfile

root = pathlib.Path(__file__).resolve().parents[3]
module = runpy.run_path(str(root / "engines/hub/schema/lockstep.py"))
with tempfile.TemporaryDirectory() as d:
    snapshots = {name: module["snapshot"](c) for name, c in module["databases"](pathlib.Path(d))}
fresh = snapshots.pop("fresh")
inventory = {}
for source, snap in snapshots.items():
    for kind in ["tables", "triggers"]:
        for name, value in snap[kind].items():
            if value == fresh[kind].get(name):
                continue
            entries = inventory.setdefault(name, [])
            fields = (
                {
                    k: {"fresh": fresh[kind].get(name, {}).get(k), "upgraded": v}
                    for k, v in value.items()
                    if fresh[kind].get(name, {}).get(k) != v
                }
                if kind == "tables"
                else {"ddl": value}
            )
            for entry in entries:
                if entry["differences"] == fields:
                    entry["sources"].append(source)
                    break
            else:
                entries.append({"sources": [source], "differences": fields})
(root / "engines/hub/schema/variant-inventory.json").write_text(
    json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
for name, entries in inventory.items():
    print(name, len(entries), sorted(set(k for e in entries for k in e["differences"])))
