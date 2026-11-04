# Hub findings

The Python hub provides additive contract v1 `POST /findings`. Node tokens
and the administrator token can use it. A message principal needs an explicit
`findings` grant. The administrator sets or removes that grant with:

```json
{"action":"grant","principal_id":"agent:watcher","enabled":true}
```

Send this body to `/findings` using the administrator bearer token. Grants
belong to the current principal enrollment. Revocation followed by enrollment
requires a new grant.

A policy charter's `match.findings` lists finding kinds. Its `match.principals`
lists the message principal names that may reserve those kinds, and
`match.nodes` lists eligible node identities. Exactly one charter must match a
kind. Missing or overlapping charters permit observation but no reservation.
Open findings appear in the existing owners holdings view.

```json
{"action":"observe","kind":"email","key":"<Message@Example.Test>"}
```

Canonicalization removes email angle brackets and lowercases the Message-ID.
Job URLs use HTTPS, lowercase hosts, no fragment or trailing slash, keep any
port other than the input scheme's default, and omit
`utm_*`, `gclid`, `fbclid`, `msclkid`, `mc_cid`, `mc_eid`, `ref`, `source`,
`trk`, `trackingid`, `refid`, `gh_src`, `lever-source`, and `lever-origin`
parameters. Other query parameters, blank ones included, remain sorted. ATS identifiers remain
unchanged. The kind alias `pr` is normalized to `pull-request` in charters and observations.
Pull request keys use kind `pull-request` and `owner/repo#number`.

An observation returns `finding_id`, `first_seen`, `owner`, `state`,
`report_ref`, `reservation`, and `reservation_expires_at`. Only the session
that acquires a reservation receives its id, generation, expiry and
`report_key`. Observing again never recovers a live reservation, even with the
same principal token. Reservations last 600 seconds.

Report externally with the supplied `report_key`, then submit:

```json
{"action":"reported","finding_id":"finding-id","reservation_id":"reservation-id","generation":1,"report_ref":"issue:123"}
```

If the process crashes before submitting `reported`, observe after expiry.
Generation two has the same report key. Search the external destination for
that key first. If found, submit its existing reference. Otherwise report and
submit the new reference. This provides at-least-once delivery when the
external destination cannot be searched. The hub itself does not search an
external service.

A replaced reservation gets HTTP 409, `finding_reservation_expired`. The last
holder can still submit after expiry while no replacement exists. Duplicate
reports get HTTP 409, `finding_already_reported`, with the existing reference.
`resolve` accepts a finding id and requires a current charter principal, a live
reservation holder, or the administrator. A non-holder cannot resolve during a
live reservation, even if the charter authorizes it: HTTP 409, `finding_reserved`.
The administrator can resolve at any time. A subsequent `reported` receives
HTTP 409, `finding_resolved`. Former holders lose resolve authority at expiry
unless the current charter still authorizes them. Resolved findings never acquire another reservation.

The database stores HMAC-SHA256 fingerprints, never canonical event keys.
A private `*.db.findings-key` sidecar beside the database supplies the master
key. It uses mode 0600 on POSIX. On Windows, protection relies on the directory
ACL, which the operator must restrict to the hub account.
Fingerprint and report keys are derived separately with HMAC-SHA256 from the
master, under `brigade.findings.fingerprint.v1` and `brigade.findings.report.v1`. Back up this
file with the database. A missing key with existing findings fails closed.
Creation writes and syncs a staging file, publishes it with an exclusive hard
link, removes the staging link, and syncs the directory. Directory sync is best
effort on Windows. An interrupted staging file is removed on the next attempt.
Existing short key files fail closed even with an empty ledger. They are not
automatically replaced, so unexpected corruption remains visible.
Report keys are HMACs of `report:` plus the finding id, returned only to new
reservation holders. A charter change affects the next reservation and does
not invalidate a live holder.

Run the contract against a disposable Python hub with
`control-brigade.py hub-contract --profile findings`. An alternate hub can use
`--clock-hub-cmd` with the existing `{clock_file}` seam. The drive models a
searchable report destination and exercises recovery without duplicate sends.
