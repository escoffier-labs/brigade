# Brigade station conformance kit

This directory is a local example for a `brigade.station.v1` manifest. It is
safe to inspect and verify: the manifest contains an install command that must
not be run by verification, and the verifier only needs the fixture executable
on `PATH`.

Run from this directory:

```bash
PATH="$PWD/fixtures:$PATH" brigade stations verify .
PATH="$PWD/fixtures:$PATH" pytest -q tests/test_station_contract.py
```

Files:

- `station.json` declares one active executable station tool.
- `fixtures/example-station` is a tiny local executable fixture.
- `tests/test_station_contract.py` shows a pytest contract check for the
  manifest and Brigade verifier payload.
