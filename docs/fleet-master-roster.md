# Fleet master roster

This document defines the first safe contract for issue #1259. It is separate
from the `/preference` routing pins and the versioned `/models` admission roster.
Those existing surfaces remain unchanged.

## Version 1 document

The JSON document contains `schema`, a positive integer `revision`, and a list
of `seats`. Each seat has a unique `name`, one executable `cli` name, a `model`,
human-readable `purpose` and `caveats`, a list of named `fallbacks`, and an
`enabled` flag. The parser rejects unknown properties so future versions must
add fields deliberately.

```json
{
  "schema": "brigade.fleet_master_roster.v1",
  "revision": 1,
  "seats": [
    {
      "name": "implementer",
      "cli": "codex",
      "model": "gpt-5.6",
      "purpose": "Implement repository changes.",
      "caveats": [],
      "fallbacks": [],
      "enabled": true
    }
  ]
}
```

The contract caps documents at 256 KiB, seats at 128, and free text at 512
characters. It rejects credentials, environment assignments, home paths,
command wrappers, unknown fields, duplicate seat names, and invalid fallback
references. `cli` is a bare executable name; launch arguments stay local.
Tokens, environment values, auth profile names, and filesystem paths are never
part of the master document.

Validate a candidate file locally before sharing it:

```bash
brigade fleet roster validate --file master-roster.json --json
```

The command reports the revision, seat count, and canonical digest. It does not
publish the document or change the local execution roster.

## Remaining work

This contract is the first independently useful slice. The follow-up must add
admin-only publication to the Hub, enrolled-node retrieval and a private
last-known-good cache, local seat availability checks, runtime overlay rules,
and hub-down behavior. The existing local roster remains the source for
machine-specific executable and authentication availability.
