# Portable evidence package

The first retention slice of issue #1407 exports one Brigade verify run as a
self-contained directory that a WORM store, SIEM batch, or auditor can ingest.
It is **not** a signed attestation, a legal-hold receipt, or an acknowledgement
of retention. Those are later slices. The package only proves that the copied
files still match the manifest that was written when the package was created.

## Commands

### `brigade receipts export package`

```bash
brigade receipts export package --target <dir> --run-id <id|prefix|latest> --out <dir> [--force] [--json]
```

Selects a single verify receipt using the same strict, bounded selection as
`brigade receipts export attestation` (`load_selected_receipt`). The run id may
be an exact id, an unambiguous prefix, or `latest`.

When the selection succeeds, Brigade copies **only** these files from the run
directory when they are present:

| File | Kind | Required |
| --- | --- | --- |
| `receipt.json` | `receipt` | yes |
| `changes.patch` | `patch` | no |
| `attestation.json` | `attestation` | no |
| `attestation.sigstore.json` | `cosign-bundle` | no |
| `summary.md` | `summary` | no |

Command logs, graph databases, run metadata, and any other file are **never**
copied. Files are copied byte-for-byte; the receipt `path` field is preserved
because the receipt digest covers it.

The default output prints one line:

```text
evidence package exported: <verify_run_id> (<n> entries)
```

With `--json` the output is a sorted object with schema
`brigade.evidence_package_export.v1`:

```json
{
  "schema": "brigade.evidence_package_export.v1",
  "out": "<out as given>",
  "verify_run_id": "<run id>",
  "entries": 5,
  "manifest_sha256": "<sha256 of written manifest bytes>"
}
```

Neither output mode prints file contents or absolute paths beyond the run id.

### `brigade receipts verify-package`

```bash
brigade receipts verify-package <dir> [--json]
```

Reads `manifest.json` and reports the integrity of every manifest entry. The
command exits `0` only when every entry is `present`, no extra regular files
exist, the manifest `entries_sha256` re-computes to `match`, and the receipt
digest re-derives from `receipt.json` to `match`.

Default output prints one fixed-word state per file, the manifest-entries state,
and the receipt-digest state:

```text
receipt.json: present
changes.patch: present
attestation.json: present
attestation.sigstore.json: present
summary.md: present
manifest entries: match
receipt digest: match
verification ok
```

With `--json` the output is a sorted object with schema
`brigade.evidence_package_verification.v1`:

```json
{
  "schema": "brigade.evidence_package_verification.v1",
  "manifest_sha256": "<sha256 of manifest bytes>",
  "manifest_entries": "match",
  "receipt_digest": "match",
  "entries": [
    {"path": "receipt.json", "state": "present"}
  ],
  "extras": [],
  "ok": true
}
```

`verify-package` verifies content integrity only. It does **not** verify
signatures (use `brigade receipts verify-attestation`), does **not** prove that
the package was retained or acknowledged, and does **not** prove that the
package came from a trusted producer.

## Manifest (`brigade.evidence_package.v1`)

`manifest.json` is written last and is the only file that is not copied verbatim
from the run directory. It uses schema `brigade.evidence_package.v1` and
`schema_version` `1`.

| Field | Type | Notes |
| --- | --- | --- |
| `schema` | string | `brigade.evidence_package.v1` |
| `schema_version` | integer | `1` |
| `created_at` | string | UTC ISO-8601 with `Z` suffix, second precision |
| `source` | object | Run identity and digest binding |
| `entries` | array | Sorted by `path` |
| `entries_sha256` | string | SHA-256 of the compact sorted-key JSON of `entries` |
| `limitations` | array of string | `["receipt-contains-local-paths", "integrity-only"]` |

### `source` object

| Field | Type | Notes |
| --- | --- | --- |
| `verify_run_id` | string | Directory name of the selected verify run |
| `producer_run_id` | string \| null | Orchestrator run id from the receipt, or `null` when absent |
| `tree_fingerprint` | string \| null | Git tree fingerprint from the receipt |
| `baseline_commit` | string \| null | Baseline commit from the receipt |
| `changes_patch_sha256` | string \| null | SHA-256 of `changes.patch` from the receipt |
| `receipt_sha256` | string | Canonical digest from `attestation_receipt.snapshot_receipt` over the copied receipt |

### `entries[]` object

| Field | Type | Notes |
| --- | --- | --- |
| `path` | string | Filename only; no separators or `..` |
| `sha256` | string | SHA-256 of the file bytes |
| `bytes` | integer | File size in bytes |
| `media_type` | string | Best-effort media type |
| `kind` | string | One of `receipt`, `patch`, `attestation`, `cosign-bundle`, `summary` |

The manifest never contains absolute paths, environment values, argv, hostnames,
or the target output path. It also does not contain its own digest.

## Staging behavior

Export uses a sibling staging directory named `<out>.staging-<8 hex>` with mode
`0o700`. The staging directory is created exclusively; if it already exists, the
command fails.

1. Create `<out>.staging-<8 hex>`.
2. Copy each allowed file with `localio.write_bytes_atomic`.
3. Write `manifest.json` last.
4. Fsync the staging directory on platforms that support it.
5. If `<out>` exists and `--force` is not set, remove the staging directory and
   exit `1` with a fixed message.
6. If `<out>` exists and `--force` is set, rename `<out>` to
   `<out>.replaced-<8 hex>` only after staging succeeded.
7. Rename the staging directory to `<out>`.
8. Remove the replaced backup directory.

Any failure before the final rename removes the staging directory and leaves
`<out>` untouched. If `--force` renamed an existing `<out>` aside but the final
rename then failed, the original `<out>` is restored from the replaced backup.

`--out` is refused if it resolves inside `<target>/.brigade/work/verify-runs`.

## Limitations

The package is intentionally narrow:

- `receipt-contains-local-paths`: the copied `receipt.json` still contains
  absolute workspace paths and log paths that were recorded at verify time.
  Consumers should treat those paths as opaque evidence of the original run
  rather than actionable file references.
- `integrity-only`: `verify-package` checks that the copied files have not been
  tampered with since export. It does not verify signatures, does not prove
  retention, and does not prove provenance from a trusted producer.
