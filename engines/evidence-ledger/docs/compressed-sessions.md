# Compressed Codex sessions

Codex imports accept `.jsonl` and `.jsonl.zst` files, both as explicit inputs
and in recursive directory scans. The shared JSONL scanner stream-decodes zstd
into the existing line parser. Line limits, ordinals, malformed JSON warnings,
redaction, and record normalization use the same code as plain JSONL.

When both representations exist, the plain file takes precedence, including
when the compressed path was supplied explicitly. Metadata and sidecar files
remain excluded with either suffix. Decoder errors appear in the existing
per-file warning output with the physical path and a `zstd:` prefix. Import
continues with later files. Records emitted before a late decoding error can
remain imported. Output write failures still abort the run.

## Identity and repeat imports

The scan manifest describes the physical file: its path includes `.zst`, its
size and mtime come from the compressed file, and its content hash is SHA-256
of the compressed bytes. The incremental fast path verifies that byte hash
even when size and mtime match. Touching an unchanged compressed file skips
parsing, while a same-size rewrite with restored mtime is parsed again.

Codex item IDs use the logical `.jsonl` path, with `.zst` removed, and the
filename-based fallback session ID uses that same logical path. Existing plain
file IDs therefore remain compatible. Compressing a previously imported file
requires one parse under the new physical path, but adds no duplicate items
or collections. Tool call IDs retain their existing call-ID-based identity.
Raw references and metadata emitted for compressed inputs retain the physical
path. Record hashes remain based on decoded event content.

The archive retains historical scan rows for removed plain files. Existing
items also retain their first stored raw path because item insertion ignores
existing IDs. Compression does not rewrite that historical provenance. Newly
imported items reference the compressed file.

Regression tests generate compressed data with the zstd encoder at runtime.
They cover decoding parity, sidecar exclusion, duplicate representations,
corruption, output failures, archive deduplication, and scan identity changes.
