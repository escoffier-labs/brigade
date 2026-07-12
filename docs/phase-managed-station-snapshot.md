# Managed Station Snapshot Plan

Goal: make the reviewed sidecar manifests the declarative runtime source for first-class managed tools while keeping Brigade offline and dependency-free.

Architecture: a bundled `brigade.managed_snapshot.v1` JSON file stores the 6 reviewed manifest records with source revision and digest. A standard-library loader validates the bundle. `managed.py` keeps Python wire and doctor callables but replaces declarative fields for matching executable tools from the snapshot. A release script regenerates the bundle from explicit local manifest paths and checks the committed file without network access.

Key tech: Python standard library, canonical JSON, SHA-256, immutable dataclass replacement, and packaged template data.

## File Map

- `src/brigade/managed_snapshot.py`: build, load, validate, render, and inspect snapshots.
- `src/brigade/templates/stations/managed-snapshot.json`: generated offline bundle.
- `src/brigade/managed.py`: apply bundled declarative contracts while retaining runtime callables.
- `scripts/managed_snapshot.py`: release-time write and CI check entrypoint.
- `scripts/verify`: reject malformed or non-canonical snapshots.
- `tests/test_managed_snapshot.py`: generator, validation, lifecycle, and runtime parity coverage.
- `tests/test_managed.py`: existing managed-tool behavior remains the compatibility gate.

## Task 1: Define the missing runtime contract

- [x] Add failing tests that expect:
  - `managed_snapshot.load_snapshot()` returns schema `brigade.managed_snapshot.v1`.
  - names are GraphTrail, MiseLedger, Agent Pantry, Token Glace, Skillet, and Content Guard.
  - Content Guard is `embedded` with owner `brigade-cli`.
  - Skillet stays a `skill-roster` and is not added to `managed.all_tools()`.
  - the 4 active executable tools match `managed.resolve()` for station, command, install, and every surface field.
- [x] Run `tests/test_managed_snapshot.py` and watch it fail because the module and bundle do not exist.

## Task 2: Build and load canonical snapshots

- [x] Add `src/brigade/managed_snapshot.py` with these public functions:

```python
SCHEMA = "brigade.managed_snapshot.v1"


def _manifest_digest(manifest: Mapping[str, Any]) -> str:
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _git_revision(root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def snapshot_path() -> Path:
    return templates.template_root() / "stations" / "managed-snapshot.json"


def build_snapshot(paths: Sequence[Path]) -> dict[str, Any]:
    records = []
    names = set()
    for path in paths:
        manifest = json.loads(path.read_text())
        if not isinstance(manifest, dict) or manifest.get("schema") != "brigade.station.v1":
            raise ValueError(f"invalid station manifest: {path}")
        name = manifest.get("name")
        if not isinstance(name, str) or not name or name in names:
            raise ValueError(f"duplicate or invalid station name: {name!r}")
        names.add(name)
        records.append(
            {
                "manifest": manifest,
                "source": {
                    "repository": path.parent.name,
                    "revision": _git_revision(path.parent),
                    "manifest_sha256": _manifest_digest(manifest),
                },
            }
        )
    records.sort(key=lambda record: record["manifest"]["name"])
    return {"schema": SCHEMA, "records": records}


def render_snapshot(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def load_snapshot(path: Path | None = None) -> dict[str, Any]:
    payload = json.loads((path or snapshot_path()).read_text())
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise ValueError(f"managed snapshot schema must be {SCHEMA}")
    records = payload.get("records")
    if not isinstance(records, list):
        raise ValueError("managed snapshot records must be a list")
    names = set()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("managed snapshot record must be an object")
        manifest = record.get("manifest")
        source = record.get("source")
        if not isinstance(manifest, dict) or manifest.get("schema") != "brigade.station.v1":
            raise ValueError("managed snapshot contains an invalid manifest")
        if not isinstance(source, dict) or source.get("manifest_sha256") != _manifest_digest(manifest):
            raise ValueError("managed snapshot manifest digest mismatch")
        name = manifest.get("name")
        if not isinstance(name, str) or name in names:
            raise ValueError(f"managed snapshot duplicate or invalid name: {name!r}")
        names.add(name)
    return payload


def executable_contracts(payload: Mapping[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    snapshot = payload or load_snapshot()
    contracts = {}
    for record in snapshot["records"]:
        manifest = record["manifest"]
        if manifest.get("lifecycle", "active") != "active":
            continue
        for tool in manifest.get("tools", []):
            if tool.get("kind", "executable") == "executable":
                contracts[tool["name"]] = {"station": manifest["station"], **tool}
    return contracts
```

`build_snapshot` must:

1. Require exactly one valid `brigade.station.v1` JSON object per name.
2. Sort records by manifest name.
3. Store each original manifest under `manifest`.
4. Store `repository`, Git `revision` when available, and the manifest SHA-256 under `source`.
5. Reject duplicate names.

`load_snapshot` must validate the top-level schema, record list, source digest, manifest schema, and duplicate names. It returns data only after every record passes.

`executable_contracts` flattens active executable tools by tool name. It ignores `skill-roster` and non-active records.

## Task 3: Generate the reviewed bundle

- [x] Add `scripts/managed_snapshot.py` with:
  - `--write` with one or more manifest path arguments to build and atomically write the bundle.
  - `--check` to load the current bundle and require byte-for-byte canonical rendering.
  - no default network or sibling-repo discovery.
- [x] Generate from the 6 explicit worktree or repository paths:
  - GraphTrail
  - MiseLedger
  - Agent Pantry
  - Token Glace
  - Skillet
  - Content Guard
- [x] Assert the output contains the exact commits recorded in the station-manifest plan.

## Task 4: Consume the snapshot at runtime

- [x] In `managed.py`, add a pure conversion from one snapshot surface:

```python
def _surface_from_snapshot(raw: dict[str, object]) -> MachineSurface:
    return MachineSurface(
        kind=str(raw["kind"]),
        command=tuple(str(part) for part in raw.get("command", [])),
        read_only=bool(raw.get("read_only", True)),
        timeout_seconds=float(raw["timeout_seconds"]) if raw.get("timeout_seconds") is not None else None,
        max_chars=int(raw["max_chars"]) if raw.get("max_chars") is not None else None,
        probe=tuple(str(part) for part in raw.get("probe", [])),
        probe_contains=tuple(str(part) for part in raw.get("probe_contains", [])),
    )
```

- [x] Add `_apply_snapshot(tool, contract)` using `dataclasses.replace`. Replace only station, command, summary, install args, and surfaces. Preserve `wire` and `doctor`.
- [x] After the existing `_TOOLS` literal, load `managed_snapshot.executable_contracts()` and replace matching tools. A missing bundled snapshot is a package error and must not fail open.
- [x] Prove the snapshot controls runtime data by changing one temporary snapshot field in a unit test and asserting the converted tool uses it while retaining the original callables.

## Task 5: Gate drift

- [x] Add `managed_snapshot.py --check` to `scripts/verify` immediately after version sync.
- [x] Run the focused tests through Brigade.
- [x] Run the full Brigade gate through Brigade.
- [x] Commit `feat(stations): load reviewed managed snapshot`.
