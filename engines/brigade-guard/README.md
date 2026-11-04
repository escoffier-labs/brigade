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
`unicode_version` with Python's `unicodedata.unidata_version`. Builds from this
repository always link the bundled 10.46 (see PCRE2 pin below), so all three
rows apply.

PCRE2 JIT is required by the binary. A build without available JIT fails its
request and Python falls back. Python uses a 30-second subprocess timeout.
Custom rules always use Python, including patterns with empty-match iteration
or constructs whose behavior differs between the engines.

## PCRE2 pin

Pinned PCRE2 library: **10.46 (2025-08-27), Unicode database 16.0.0**.

- `Cargo.toml` pins `pcre2 = "=0.2.11"` and `pcre2-sys = "=0.2.10"`. The
  `pcre2-sys` crate vendors the PCRE2 source; 0.2.10 vendors 10.46.
- `pcre2-sys` links a system libpcre2 whenever pkg-config finds one, so hosts
  with different system libraries would run different regex engines (Ubuntu 24.04
  ships 10.42). The repository's `.cargo/config.toml` sets
  `PCRE2_SYS_STATIC=1` with `force = true`, which makes the build script compile
  the vendored copy and ignore any system library or exported override. Cargo
  reads that file for any working directory under the repository root,
  including `--manifest-path` builds. A build run from outside the repository
  does not get it and must export `PCRE2_SYS_STATIC=1` itself.
- The unit test `linked_pcre2_is_the_pinned_bundled_release` reads the linked
  library's version and Unicode version through `pcre2_config` and fails when
  either differs from the constants next to it. It runs in `cargo test` inside
  `./scripts/verify-rust-guard` on every guard CI run, so a system-library
  build, a crate bump or a lockfile change that moves the vendored release
  cannot pass unnoticed.

To move to a newer PCRE2, bump both crate pins, update the two constants in
`src/main.rs` and the version above, and recheck the parity table.

## Verification and distribution

`./scripts/verify-rust-guard` checks format, clippy, tests, feature builds and
builds the release executable. `./scripts/verify-guard-native` runs the CLI
parity drive with Python selected, then the focused gate on all guard tests
with native selected. The binary integration test asserts raw native matching
actually ran, so an unavailable or incompatible binary cannot silently pass it.
CI also checks Rust 1.85. There are no optional crate features yet.

`scripts/benchmark-guard-native.py` times `brigade guard scan` on the Python,
native and auto backends and asserts their output is identical. Run it after
`cargo build --release`; the full run scans a 21 MiB corpus twice per backend.
`--smoke` scans inputs up to 256 KiB once per backend under a 90 second cap
(`--time-limit`) and `--output FILE` writes every record as JSON lines. The
`Native guard` workflow runs the smoke mode and uploads the file as the
`guard-native-benchmark-smoke` artifact. It checks that the benchmark runs and
the backends agree and sets no timing threshold, since shared runners are too
noisy for one.

Release artifacts, checksums, signing and `brigade setup` installation are
not wired in this PR. Local and CI builds use Cargo directly.
