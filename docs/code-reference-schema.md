# Structured code references

`schemas/code-reference.v1.schema.json` is Brigade's stable contract for an exact code reference. A reference names its repository, immutable revision, repository-relative file, qualified symbol, symbol kind, source span, and change kind. `schema` is always `brigade.code-reference.v1`.

Brigade emits references in receipt metadata as `item.metadata.code_references`. JSON is compact, sorted-key UTF-8 JSON. `source_span` is an exact range expressed as positive `start_line` and positive `line_count`, so it cannot represent a reversed range. Both values are bounded by `9007199254740991` (`2^53 - 1`), the largest integer represented exactly by JavaScript JSON consumers and the Go and Python implementations. JSON Schema's `integer` type accepts numeric JSON values with no fractional part, including `1.0` and `1e0`; Brigade normalizes accepted values to canonical integer JSON.

The Python mapping API accepts only native `int` and integral `Decimal` values. It rejects every native `float`, including floats that look integral after a JSON decoder rounded the original lexeme. Parse raw reference JSON with `parse_json_reference`, which preserves numeric lexemes as `Decimal` before validation.

```json
{"change_kind":"changed","file_path":"src/brigade/receipts_cmd.py","qualified_name":"brigade.receipts_cmd._metadata_with_delta","repository":"escoffier-labs/brigade","revision":{"commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},"schema":"brigade.code-reference.v1","source_span":{"line_count":3,"start_line":787},"symbol_kind":"function"}
```

Consumers match the identity fields through `change_kind`; source spans document location but do not participate in exact lookup. `symbol_kind` is one of the GraphTrail extractor kinds: `class`, `enum`, `function`, `method`, `module`, `struct`, `trait`, or `type`. Unknown fields and other schema versions are invalid. The shared integer and canonical JSON vectors in `schemas/` pin the Python and Go implementations to the same contract.
