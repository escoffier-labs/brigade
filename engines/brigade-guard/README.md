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
The request contains `protocol: 1`, `text`, and ordered `rules`, each with
`id`, `pattern`, and integer Python `flags`. Rules come from Python on every
invocation. The binary stores no rule patterns.

The response contains `protocol: 1` and ordered `rules`, each with `id` and
`spans`. Every nonempty span has half-open `byte_start`, `byte_end` UTF-8 offsets
and `start`, `end` character offsets. The current compatibility set is ASCII,
so the units coincide. Python validates version, rule order and span bounds
before accepting results. Unsupported inputs, flags, malformed contracts,
compile errors, JIT errors, match errors, process failures and timeouts cause
fallback. Stderr never contains input text or patterns.

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
