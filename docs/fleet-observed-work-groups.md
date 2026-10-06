# Observed work groups in Command Deck

Add `observed_work_groups` to the existing Deck JSON file selected by
`--deck-config` or `BRIGADE_FLEET_DECK_CONFIG`. The server validates and freezes
this file at startup. Omitting the field preserves existing behavior.

This synthetic example retains five explicit Worklore references:

```json
{
  "stations": [{"node_id": "sample-node", "name": "Sample station", "capacity": 1}],
  "observed_work_groups": [{
    "key": "sample-source",
    "label": "Sample observations via Relay",
    "coverage": "Five retained task observations only",
    "source_ref": "opaque:sample-source",
    "proxy_ref": "opaque:relay",
    "parent_ref": "opaque:sample-parent",
    "snapshot_observed_at": "2026-01-01T00:00:00Z",
    "work_ids": [
      "wl-000000000000000000000001",
      "wl-000000000000000000000002",
      "wl-000000000000000000000003",
      "wl-000000000000000000000004",
      "wl-000000000000000000000005"
    ]
  }]
}
```

Review private configuration separately. Use only existing work IDs and keep
private labels, references and IDs out of public source and examples. Preparing
this file does not activate a group. Activation requires the operator's existing
config selection and an authorized hub restart. It creates no credential, grant,
connector or admission lane.

The optional array accepts zero to eight groups. Each group requires all fields
shown except `snapshot_observed_at`. Unknown group fields are rejected. Keys must
be unique, one to 64 lowercase ASCII letters, digits, `_` characters or hyphens, and
start with a letter or digit. Labels are one to 64 characters. Coverage and each
reference are one to 256 characters. These strings cannot be blank or contain
control characters. References display as escaped plain text, with no identity
verification or generated links. State unknown attribution explicitly if needed.

Each group accepts one to 25 unique work IDs. IDs start with an ASCII letter or
digit and contain only ASCII letters, digits, dots, `_` characters or hyphens, up to
128 characters. A snapshot observation time, when supplied, must be a
timezone-aware ISO timestamp of at most 64 characters. Omit it when the reviewed
source metadata has no observation time. The page reports unknown, future, stale
or recent snapshot freshness against the Deck's `stale_after_seconds` setting.
Recent metadata does not establish live activity. Worklore `updated_at` describes
a work-record update and does not supply snapshot freshness.

The home card links `/deck/observed/<key>`. The page accepts no query parameters
and uses existing admin bearer, dashboard session or enabled trusted proxy read
authentication, CSP, theme and no-store headers. Node API tokens do not authorize
this page. Dashboard sessions still do not authorize the bearer-only Worklore
API. When Worklore is disabled, group links disappear and group routes return
404. Unknown groups return 404 after authorization.

Reads fetch only the configured IDs through bounded item projections, with no
schema migration or writes. Missing and archived records appear as unavailable
references. Descriptions and reference summaries have the same preview bounds
as the generic work page. Counts describe configured observation records, not
active slots or runs. Groups do not change station or cloud capacity, leases,
claims, lifecycle, attempts or memory. A five-record group does not narrow the
broader Worklore data visible through `/deck/work`.
