# brigade-guard

Optional PCRE2 JIT backend for Python guard. Build with
`cargo build --release --locked --manifest-path engines/brigade-guard/Cargo.toml`.
Put the binary on PATH or set `BRIGADE_GUARD_BIN` to its absolute path.
Python has no new runtime dependencies.

`BRIGADE_GUARD_BACKEND` accepts `auto` (default), `python`, or `native`.
Auto uses Python below 65,536 characters to avoid process startup overhead.
Native bypasses that threshold for testing. All modes retain Python for
non-ASCII subjects, Python's extra ASCII space controls, and custom rules.
This avoids differences in Unicode versions, character classes, case folding,
and syntax. Native errors emit one warning and repeat matching with Python.
No policy, precedence, allow comment, redaction, or CLI formatting runs in Rust.

## Protocol 1

One JSON request on stdin and one response on stdout, followed by process exit.
The request contains `protocol: 1`, an optional `mode` (`ascii` or `utf`),
`text`, and ordered `rules`, each with
`id`, `pattern`, and integer Python `flags`. Rules come from Python on every
invocation. The binary stores no rule patterns.

The response contains `protocol: 1` and ordered `rules`, each with `id` and
`spans`. Every nonempty span has half-open `byte_start`, `byte_end` UTF-8 offsets
and `start`, `end` character offsets. In the default ASCII mode the units
coincide. Python validates version, rule order and span bounds
before accepting results. Unsupported inputs, flags, malformed contracts,
compile errors, JIT errors, match errors, process failures and timeouts cause
fallback. Stderr never contains input text or patterns.

### UTF mode

A request with `"mode": "utf"` compiles every rule with PCRE2 UTF and UCP, so
`\d`, `\s`, `\w`, `\b` and caseless matching use Unicode properties, and accepts
any text. Character offsets count Unicode code points, as Python `str` indexes
do. The response adds `engine` with the linked library's `pcre2_version` and
`unicode_version`. UTF mode refuses empty matches and Python's `ASCII` flag.
A request without `mode`, or with `"mode": "ascii"`, is answered byte for byte
as before, and ASCII responses carry no `engine` field.

UTF mode is for the Hub, which has no Python fallback. The local guard still
sends only ASCII subjects. `brigade.guard.utf_parity` lists the default rules
whose UTF-mode spans differ from Python `re`, and why:

| Reason | Rules |
|---|---|
| `\s`/`\S`: PCRE2 counts U+180E as space, Python counts U+001C to U+001F | `example-phone-555`, `ssh-private-target`, `port-reference`, `coauthored-by-trailer`, `email`, `us-phone`, `bearer-token`, `api-key-assignment` |
| Caseless `i`: Python folds U+0130 and U+0131 to `i`, PCRE2 does not | `example-email-reserved`, `windows-home-path`, `email`, `bearer-token`, `api-key-assignment`, `bare-token-prefix`, `private-hostname` |
| PCRE2 10.43 and later: combining marks (Mn) and connector punctuation (Pc) are `\w` | every rule with `\w` or `\b`, which leaves only `private-key-block` |

Checked against PCRE2 10.42 (Ubuntu 24.04's system library) and the bundled
10.46. On 10.42 the other eight default rules match Python span for span on
the guard's Unicode fixture (`tests/guard/unicode_fixture.py`). Every character in that fixture was assigned by Unicode 3.2. Characters assigned in
only one of the two Unicode databases still classify differently, so compare
`unicode_version` with Python's `unicodedata.unidata_version`. A build links
the system PCRE2 when pkg-config finds one and otherwise builds the bundled
copy, so the build decides which rows apply. `PCRE2_SYS_STATIC=1` forces the
bundled copy.

PCRE2 JIT is required by the binary. A build without available JIT fails its
request and Python falls back. Python uses a 30-second subprocess timeout.
Custom rules always use Python, including patterns with empty-match iteration
or constructs whose behavior differs between the engines.

## Verification and distribution

`./scripts/verify-rust-guard` checks format, clippy, tests, feature builds and
builds the release executable. `./scripts/verify-guard-native` runs the CLI
parity drive with Python selected, then the focused gate on all guard tests
with native selected. The binary integration test asserts raw native matching
actually ran, so an unavailable or incompatible binary cannot silently pass it.
CI also checks Rust 1.85. There are no optional crate features yet.

Release artifacts, checksums, signing and `brigade setup` installation are
not wired in this PR. Local and CI builds use Cargo directly.
