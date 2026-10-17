# Fleet master roster

This document defines the first safe contract for issue #1259. It is separate
from the `/preference` routing pins and the versioned `/models` admission roster.
Those existing surfaces remain unchanged.

## Version 1 document

The JSON document contains `schema`, a positive integer `revision` no greater
than `9007199254740991`, and a list of `seats`. Each seat has a unique `name`,
one executable `cli` name, a `model`, a `purpose` label and `caveats` labels, a
list of named `fallbacks`, and an `enabled` flag. The parser rejects unknown
properties so future versions must add fields deliberately.

```json
{
  "schema": "brigade.fleet_master_roster.v1",
  "revision": 1,
  "seats": [
    {
      "name": "implementer",
      "cli": "codex",
      "model": "gpt-6.1-sol",
      "purpose": "implementation",
      "caveats": ["focused-verification"],
      "fallbacks": [],
      "enabled": true
    }
  ]
}
```

The contract caps documents at 256 KiB, seats at 128, and every string value
at 160 characters. Descriptive fields use exact, case-sensitive labels. There
is no free-text field or pattern-based leak detector. Old prose values must be
replaced with these labels before validation.

| Field | Allowed values |
| --- | --- |
| `purpose` | `implementation`, `review`, `research`, `planning`, `triage`, `documentation`, `security-review` |
| `caveats` | `focused-verification`, `authentication-required`, `read-only`, `manual-approval` |

`purpose` is required and describes the seat's primary role. `caveats` defaults
to an empty list, accepts at most 16 entries, and rejects duplicate or unknown
values. Caveat order is preserved in the canonical document. The labels
express declarations. Validation does not enforce a verification gate,
authentication, read-only execution, or approval.

Identifiers use ASCII grammars, matched against the entire value:

- Seat names, fallback names, and executable names match
  `[a-z][a-z0-9._-]{0,63}`. `cli` is one bare executable name. Arguments stay local.
- Model IDs contain one to four slash-separated components. Each component
  matches `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`. One optional context suffix,
  matching `\[[0-9]{1,4}[km]?\]`, may follow the final component. Examples are
  `openai/gpt-6.1-sol` and `claude-opus-5-5[1m]`. The complete ID must fit
  within 160 characters. Colons, leading dots, empty components, whitespace,
  URLs, assignments, environment syntax, and command quoting are excluded.

Identifiers are published as written and are not screened for secrets. Use
public seat, executable, and model identifiers. Keep credentials, local profile
names, and filesystem locations in the local execution roster. A slash in a
model ID denotes a provider namespace. The grammar cannot distinguish that ID
from a similarly spelled relative path.

The parser also rejects unknown fields, duplicate JSON keys or seat names,
invalid revisions, and invalid fallback references. Revision values must be
integers, with booleans and floats rejected. Fallbacks cannot repeat, refer to
their own seat, or target a seat that defines its own fallbacks. Invalid text
is rejected without including its value in an error message.

Validate a candidate file locally before sharing it:

```bash
brigade fleet roster validate --file master-roster.json --json
```

The command reports the revision, seat count, and canonical digest. The digest
is SHA-256 over compact JSON with sorted object keys, encoded as UTF-8 without
ASCII escaping. It does not publish the document or change the local execution
roster.

## Remaining work

This contract is the first independently useful slice. The follow-up must add
admin-only publication to the Hub, enrolled-node retrieval and a private
last-known-good cache, local seat availability checks, runtime overlay rules,
and hub-down behavior. The existing local roster remains the source for
machine-specific executable and authentication availability.
