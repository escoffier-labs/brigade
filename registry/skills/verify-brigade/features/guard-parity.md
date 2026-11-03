---
status: active
proof: drive
surfaces: guard scan, guard redact
drive: guard-parity
last-drive: 2026-11-03 31d5c549d
---

# Guard scan and redact parity

`brigade guard scan` and `brigade guard redact` are the content guard's two
read paths: one reports findings, the other rewrites the text. The regex
engine behind them is expected to change (PCRE2 with JIT, likely inside the
native Rust engine) while the CLI stays the same. This drive is the parity
gate that swap has to pass. Its goldens come only from CLI output, so they hold
for any engine that keeps the CLI contract.

## Sub-features

- `brigade guard scan [target] --policy <file> --json` prints `ok`,
  `blocked`, `changed`, `counts_by_action`, `counts_by_category`, and a
  `findings` array. Each finding carries `rule_id`, `action`, `category`,
  `line` and `column` (1-based), `start` and `end` (0-based offsets into the
  input, `end` exclusive), `match`, `replacement`, and `source`.
- `brigade guard redact [target] --policy <file>` writes the redacted text to
  stdout. With no target it reads stdin. `--in-place` rewrites the file
  instead.
- Both exit 1 when any finding's action is `block`, else 0. The drive's policy
  sets every category to `redact`, so a healthy run exits 0 on all four calls.
- Not driven: `--baseline`, `--no-allow-comments`, `--opf`, `--opf-bin`,
  `--opf-device`, `--scan-frontmatter`, and `--skip-code-blocks`. The goldens
  hold for the default options only.
- The policy's `rules` map overrides a rule's action. `example-phone-555`
  carries a built-in `warn` override, so the parity policy sets it to `redact`
  explicitly.

## How to get to it (user POV)

A user scrubbing a draft before it leaves the machine runs:

```bash
brigade guard scan draft.md --json
brigade guard redact draft.md > draft.clean.md
```

and reads `findings` for what was caught and the redacted text for what will
ship. An engine swap that moves an offset, drops a match, or changes a
replacement shows up in exactly those two outputs.

## Driving it with control-brigade

```bash
C=registry/skills/verify-brigade/control-brigade.py
TARGET=$($C new-target | python3 -c 'import json,sys; print(json.load(sys.stdin)["target"])')
$C guard-parity --target "$TARGET" --dry-run   # the four commands, nothing written
$C guard-parity --target "$TARGET"
$C evidence --target "$TARGET" --label guard-parity
$C cleanup
```

`guard-parity` writes `fixture.txt`, `policy.json`, and `in-place.txt` under
`$TARGET/guard-parity/`, then runs four CLI calls against them:

1. `guard scan --policy policy.json --json fixture.txt`
2. `guard redact --policy policy.json fixture.txt`
3. `guard redact --policy policy.json` with the fixture on stdin
4. `guard redact --policy policy.json --in-place in-place.txt`

It compares the results to goldens kept in the helper
(`GUARD_PARITY_GOLDEN_FINDINGS`, `GUARD_PARITY_GOLDEN_REDACTED`):

- the scan findings as `(line, column, start, end, rule_id, action)` rows,
  exact list and order
- each finding's `match` equals `fixture[start:end]`
- both redact stdouts equal the golden text
- the bytes `--in-place` wrote equal the golden text encoded as UTF-8. Stdout
  is decoded text, so this is the byte-for-byte check
- all four calls exit 0

Proof, from the helper's output:

- helper exit 0, `"ok": true`, `"mismatches": []`
- `"rule_ids"` is `example-phone-555`, `home-path`, `localhost-bare`,
  `private-hostname`, `root-path`, `us-phone`, `windows-home-path`
- `"findings"` has 11 rows

A drift is helper exit 3, `"ok": false`, and a `mismatches` entry per failed
check: `scan findings` with `missing` and `unexpected` rows, `scan match`,
`redact stdout`, `redact stdin stdout`, `redact --in-place bytes`, or
`<call> exit code`.

Observed on main `b18e6f51d`, and again on `31d5c549d`: exit 0,
`"mismatches": []`, 11 findings. The
goldens were pasted from that run and checked by hand against each rule.

### What the fixture covers

Fixture lines are numbered as the scan reports them. The fixture itself lives
in `control-brigade.py` (`GUARD_PARITY_FIXTURE_LINES`), built from string
fragments, so this page describes the cases instead of quoting them.

| Line | Rule | Construct | Case | Observed |
|---|---|---|---|---|
| 2 | `example-phone-555` | `(?<!\w)` lookbehind | 555 number after a space | redacted |
| 3 | `example-phone-555` | `(?<!\w)` and `(?!\w)` | word character before the number; a fifth digit after it | no finding |
| 4 | `localhost-bare` | `(?<![\w.-])` lookbehind | bare local host name after a space | redacted |
| 5 | `localhost-bare` | `(?<![\w.-])` and `(?![\w.-])` | word character before; hyphen after | no finding |
| 6 | `home-path` | `(?<![\w.-])` lookbehind | `/home/<user>/...` after a space | redacted |
| 7 | `home-path` | lookbehind and tool-dir lookahead | `/srv` before `/home`; a `.cache` first segment | no finding |
| 8 | `us-phone` | `(?<!\w)` lookbehind | dashed and parenthesized numbers | redacted twice |
| 9 | `us-phone` | `(?<!\w)` lookbehind | underscore before the number | no finding |
| 10 | `private-hostname` | `(?<![\w.-])` lookbehind | `.internal` host in prose | redacted |
| 11 | `private-hostname` | lookbehind and `(?!\.[\w-])` lookahead | hyphen before; a further dotted label after | no finding |
| 12 | `windows-home-path` | `(?P=windows_quote)` backreference | double-quoted path with spaces | redacted, quotes kept |
| 13 | `windows-home-path` | `(?(windows_quote)...)` unquoted branch | unquoted path | redacted |
| 14 | `windows-home-path` | `(?P=windows_quote)` backreference | opens with `"`, closes with `'` | **no finding** (see Gotchas) |
| 15 | `windows-home-path` | `(?<![\w.-])` lookbehind | word character before the drive letter | no finding |
| 16 | `root-path` | `(?P=root_quote)` backreference | single-quoted path with spaces | redacted, quotes kept |
| 17 | `root-path` | unquoted alternative | unquoted path | redacted |
| 18 | `root-path` | `(?P=root_quote)` backreference | opens with `"`, closes with `'` | first component only |
| 19 | `root-path` | `(?<![\w.-])` lookbehind | `/srv` before `/root` | no finding |

## Gotchas

- **Line 14 is a recall gap in the guard, recorded as observed.** A Windows
  home path that opens with one quote character and closes with another gets
  no finding at all, so the username in it is not redacted. The quoted branch
  needs the same quote to close the path, and the unquoted branch is ruled out
  by `(?<![\"'\`])` because a quote precedes the drive letter. `root-path`
  handles the same input by falling back to its unquoted branch (line 18). The
  golden keeps the observed miss; the drive does not fix guard rules. A fix
  changes line 14 in the scan golden and the redact golden together.
- Line 18 redacts the path only up to its first space and leaves the rest of
  the spaced path in the text. That follows the rule's documented
  intent, where unquoted paths stop at whitespace, but it is still a partial
  redaction.
- `private-hostname` is not regex-only. The engine's hostname context
  heuristic decides whether a dotted name is a host or code, so line 10 also
  pins that heuristic. A swap that moves the regex but drops the heuristic
  fails here.
- The goldens are policy-relative. They use the parity policy the helper
  writes, not the default policy. Under the default policy the 555 number
  warns instead of redacting and several rules block, which makes both calls
  exit 1.
- `start` and `end` are offsets into the decoded text. The fixture is ASCII, so
  character and byte offsets agree. A non-ASCII case added later needs its
  goldens read with that difference in mind.
- Changing a fixture line shifts the offsets of every finding after it. Re-run
  the drive on main, paste the new goldens, and check each row by hand against
  its rule again.
- Never point the drive at a real document. The fixture is synthetic, and the
  target lives under the state root, where `cleanup` takes it away.
