# Skill revision contracts and exact fleet approval

`brigade skills inbox add SOURCE --behavioral-contract-json JSON` accepts a
version 1 behavioral contract. Its fields are:

- `reason` and `addressed_failure`: nonempty text, at most 4,000 characters each.
- `change_category`: `behavior`, `bugfix`, `clarification`, `safety`, or
  `compatibility`.
- `baseline_fingerprint`: the current registry package identity, or `null` for
  a skill absent from that registry.
- `expected_observable_behavior`: 1 to 100 objects with unique `id` values and
  nonempty `description` values, each at most 4,000 characters.
- `schema_version`: integer `1`.

For example, a new skill can declare:

```json
{
  "schema_version": 1,
  "reason": "Report failed verification accurately",
  "addressed_failure": "A command timeout was reported as passing",
  "change_category": "behavior",
  "baseline_fingerprint": null,
  "expected_observable_behavior": [
    {"id": "timeout", "description": "Report a timeout as incomplete"}
  ]
}
```

Show and diff expose the contract. Proposals without one display `incomplete`.
When no explicit contract is supplied, a contract embedded in source `skill.json`
is validated and reviewed as the proposal contract, including its baseline.
Brigade does not infer a failure or expected behavior. Acceptance preserves the
contract in registry `skill.json`. Semantic metadata defaults and the contract
are finalized before recording candidate identity, so acceptance preserves the
reviewed package identity. A separate contract digest detects changed or removed
proposal contracts. Acceptance refuses changed candidate or baseline
identities, including helper and semantic metadata changes. `--force` does not
bypass those checks. Legacy proposals without recorded package identity require
a new proposal before acceptance.

Package identity composes the existing collected-file fingerprint with semantic
root metadata and nested metadata file digests. Root metadata formatting and
managed `imported_at`, `updated_at`, `created_at`, `fingerprint`, and `source`
fields do not change identity. The source selector is bound separately.

## Fleet report and review compatibility

Report schema 2 carries a closed, versioned `binding` object per copy. Schema 1
reports remain readable, with no mutation approval. Missing receipt provenance
also leaves binding absent. Paths, package text, prompts, and credentials do not
enter the report. Workspace identity is a SHA-256 digest of the resolved local
workspace path. Node identity comes from the authenticated report owner.

Bindings include skill, harness, verified source selector, installed and target
whole-package identities, a separate source-package identity, and exact rollback
snapshot identity. Target identity covers the rendered package bytes used by
installation. Contract and
evaluation digests may be null. Null means absent or pending evidence, with no
claim about evaluation success or promotion eligibility.

The browser submits a displayed copy-set token. The Hub compares it with the
current server view before saving explicit per-copy approvals. A new node or
harness, changed binding, or legacy unbound draft produces `review-stale` and
withholds executable commands. Saved defaults do not authorize new copies.
Legacy drafts remain inspectable.

Fleet commands include `--expected-binding-json`. Install, uninstall, and
rollback compare the binding with current anchored package observations before
mutation. Install checks the collected source bytes used for rendering and the
installed bytes used for rollback capture. Rollback checks the collected exact
snapshot it restores. Ordinary local invocations retain their existing behavior
when the optional precondition is absent. Filesystem operations retain existing
no-follow protections. Supported skill mutations share a workspace lock,
including ordinary imports, inbox acceptance and projection operations. This
serializes cooperative writers. It does not exclude an unrelated process that
writes installed files without taking the lock.

## Evaluation integration

The proposal's `candidate_identity` and `baseline_identity` identify the
comparison inputs. The contract's assertion IDs name observable checks. Parent
pilot code can use `skills_cmd.registry._package_identity` on the same collected
package bytes and retain the proposal contract alongside its traces.

Registry metadata may carry `evaluation_evidence_sha256` after the evaluation
owner validates the evidence. The report binds that digest when present. It
does not validate evaluator provenance or convert an audit receipt into scoring
evidence. The paired pilot and registered verifier retain those responsibilities.
