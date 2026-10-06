# Local model-seat lease denials

A local worker can pass its startup roster check and still lose its model-seat
lease at launch. The lease uses `POST /models` with `action: acquire`.
Hosted cloud admission uses separate `/cloud` operations. A cloud provider's
enabled flag or an empty cloud lease list does not explain a local seat refusal.

For the local lease path, worker preflight reports the seat, its adapter's provider,
and a bounded reason. For example:

```text
fleet model policy denied seat 'seat-alpha' (provider 'cursor'): seat-disabled
```

The same diagnostic reaches the failed worker's run receipt with failure phase
`preflight` and failure kind `fleet-model-policy`. The worker stops before calling
its provider. This diagnostic change adds no retries or policy changes.

Enrolled session preflight retains failure kind `lease-denied` and reports:

```text
model lease denied: seat-disabled (provider 'openai', seat 'seat-alpha')
```

The reason appears first. Provider and seat labels are limited to 64 characters
each, and the diagnostic is bounded to the agents' 200-character detail limit.
Both labels come from the acknowledged launch context. A refusal stops before
provider invocation and does not consult cloud admission.

| Reason | Meaning | Next check |
| --- | --- | --- |
| `seat-disabled` | The current hub policy disables the requested seat, with matching provider and canonical model. | Review the current model-seat policy and the startup roster snapshot. |
| `seat-capacity-exhausted` | Current seat usage meets or exceeds its configured concurrency limit. | Review active model leases and the seat limit. |
| `malformed-response` | An HTTP 200/409 lease response has an invalid decoded envelope or acquire result. Malformed or oversized HTTP 409 bodies become `{}` and receive this reason. | Check hub/client compatibility and the lease response shape. |
| `refused` | The denial has no recognized model-seat reason, or uses an unexpected status. | Compare the requested seat/provider/model with current policy and inspect bounded hub diagnostics. |
| `auth-failed` | The hub returned HTTP 401/403. | Check enrollment through the approved operator workflow. |
| `hub-unavailable` | Configuration, transport, or the bounded request failed. HTTP 200 body decoding or size-limit failures also receive this reason. | Check hub reachability and client compatibility. |
| `no-identity` | The local node identity cannot be used for a lease. | Check enrollment through the approved operator workflow. |

The client accepts only `seat-disabled` and `seat-capacity-exhausted` from the
response's `reason_code` field. Unknown codes, including cloud-provider codes,
remain `refused`. Older hubs can supply the exact literal
`model policy capacity is exhausted`, which the client maps to the capacity code
when no reason code is supplied. An older hub's generic
`model policy denied lease` remains `refused`: it cannot establish that a seat
was disabled.

Seat and provider labels come from the worker configuration used for the lease
request. Arbitrary response error text and response identity fields never enter
the diagnostic. Holder tokens, bearer tokens, and response bodies must stay out
of logs. Auth failures retain precedence over response reason codes.

The hub keeps its admission predicates, HTTP statuses, lease writes, fencing,
capacity accounting, and release behavior. It adds a reason code to its existing
disabled-seat and exhausted-capacity responses. Successful leases and releases
retain their existing semantics.

## Offline regression evidence

`tests/test_model_seat_denial.py` drives a local worker's preflight through the
real model-seat lease client and an isolated SQLite hub handler. Its startup
snapshot declares an enabled seat. The launch-time hub fixture either disables
that seat or holds its only available lease. Runtime admission is stubbed so
the test reaches the lease boundary. Network transport is replaced with the
offline handler and provider invocation fails the test if reached.

Both cases previously reported `refused`. They now report their specific reason
with the locally selected provider in stderr and the failure receipt. The tests
also check one acquire attempt, unchanged release cleanup, unchanged lease
counts, and redaction of injected private response text. Parser cases cover
unknown and malformed codes, malformed decoded envelopes, old-hub capacity
responses, auth precedence, and successful acquire/release behavior.

These fixtures reproduce diagnostic loss in the local lease path. They do not
establish which hub condition caused the originally reported worker refusal.
`tests/test_fleet_session_bootstrap.py` also drives enrolled agent preflight
through prepare, acknowledgement, launch admission, and the real lease client
with an offline HTTP 409 transport. Disabled and capacity cases retain
`lease-denied`, the bounded reason, and the trusted provider and seat. Maximum
identity lengths exercise the detail boundary. The tests check one acquire,
no successful lease or provider invocation, no cloud admission, and redaction
of private response and admission payload text.

Run the development slice through Brigade with `./scripts/verify-focused`,
selecting this regression and the existing model roster, admission, authority
proof, policy migration, and session bootstrap tests. Use an allowlisted clean
environment with isolated HOME and TMPDIR and no provider keys.
