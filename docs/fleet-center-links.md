# Offline Center URL metadata and links

A node file may contain an optional top-level `center_url`:

```toml
center_url = "https://center.example.invalid:8443/Center/"
```

The example is synthetic. Default initialization leaves the URL unset and writes
no `center_url` entry. This slice loads existing metadata and renders an optional
anchor. It adds no reporting, Hub persistence, schema, machine-card wiring, or
Center provisioning.

## Identity and selection

`brigade node --target <workspace>` reads the workspace-local identity.
`brigade node --machine` reads the existing home machine authority, selected by
`fleet_client.home_identity_target()` through `BRIGADE_HOME`, or the default
home `.brigade/node.toml`. Existing fleet reporting uses that home machine UUID
regardless of the workspace from which it runs. Future workspace metadata must
not override that machine identity or select another machine's Center link.

Loading optional metadata preserves the node UUID, hostname, roles, and platform.
An invalid optional value does not make an otherwise valid identity unavailable,
regenerate its UUID, or rewrite the file. Invalid TOML syntax and invalid required
identity fields retain their existing errors.

The four-argument `NodeIdentity` constructor remains supported. A fifth optional
`center_url` argument accepts a configured URL. Its public `center_url_status` is:

- `unset`: the key is absent or the constructor value is `None`.
- `invalid`: the supplied value fails the contract, including an empty string.
- `configured`: supported syntax, with reachability and authentication unverified.

`center_url` on the loaded identity is the original valid string, or `None` for
unset/invalid metadata. Unsafe input is not echoed into diagnostics. Unset JSON
and text keep their legacy shape. Configured JSON adds `center_url` and
`center_url_status`. Invalid JSON adds a null URL and the `invalid` status.
Text output labels a configured URL `unverified` and an invalid URL `link suppressed`.

## Supported URL contract

`brigade.fleet_center_links.validate_center_url(value)` returns the original
supported URL or `None`. Validation is offline and uses only the standard library.

- An absolute HTTP or HTTPS URL, at most 2048 ASCII characters.
- A DNS host of at most 253 characters, with nonempty labels of at most 63
  characters. Labels contain letters, digits, or hyphens and begin/end with a
  letter or digit. Single-label hosts are supported. Trailing dots, IP literals,
  and numeric or hexadecimal numeric final labels are unsupported to avoid
  browser IPv4 reinterpretation.
- An optional explicit decimal port from 1 through 65535.
- No raw whitespace, controls, Unicode, backslashes, credentials/userinfo,
  percent-encoded authority, query, or fragment. Even empty `?` and `#`
  delimiters are rejected. Ambiguous authorities and unsupported schemes fail.
- A path may contain ASCII punctuation and well-formed percent escapes. Raw
  Unicode paths must be supplied as percent-encoded UTF-8, such as
  `https://center.example.invalid/%E2%98%83`. Validation does not decode escapes
  or change path spelling, scheme case, host case, or port spelling.

There is no trimming, credential stripping, or query removal to repair a URL.
Host validation establishes supported syntax, without resolving a name or
asserting that an endpoint exists. HTTPS alone does not prove authorization.

## Conditional HTML boundary

`render_center_link(value)` returns `<a href="…">Open Center</a>` only for a
supported URL, otherwise an empty string. It escapes the attribute value,
including quotes, ampersands, and markup characters. `center_url_status(value)`
provides the same offline classification for callers with raw optional metadata.

An ordinary browser link does not supply a bearer header. Endpoint and browser
authentication choices remain external to this utility. Actual remote
reachability/authentication and report-to-Hub-to-card acceptance remain open
under #1495. Future persistence must coordinate additive fields and clear/replay
ordering after #1576 schema 25. Deck integration must coordinate with its owner.
