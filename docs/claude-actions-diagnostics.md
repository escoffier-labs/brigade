# Passive Claude Action configuration diagnostics

`brigade.claude_actions_diagnostics.diagnose` returns JSON-compatible facts from
supplied workflow definitions and independent metadata. It performs no IO,
persists nothing, and applies no recommendations. Issue #1602 remains open.

## Caller boundary

Create an `Inventory` with provenance scoped to one canonical `owner/repo`,
`.github/workflows`, and an exact 40-character commit SHA. Each `Definition`
has its own `Provenance`, exact repository/path/revision, observation timestamp,
and `workflow` or `composite` kind. The caller supplies `body` as a literal
mapping normalized from the definition, or supplies `None`/text to record
uncertainty. No YAML parsing occurs in this module.

```python
from brigade.claude_actions_diagnostics import (
    Definition, Inventory, Provenance, Scope, diagnose,
)

repository = "example/project"
revision = "a" * 40
observed_at = "2026-10-06T12:00:00Z"
path = ".github/workflows/automation.yml"
source = Provenance(
    "caller-normalized-definition",
    Scope(repository, path, revision),
    f"https://github.com/{repository}/blob/{revision}/{path}",
    observed_at,
)
definition = Definition(source, {
    "on": {"issue_comment": {"types": ["created"]}},
    "permissions": {"contents": "read"},
    "jobs": {"automation": {"steps": [{
        "uses": "anthropics/claude-code-action@v1",
        "with": {"anthropic_api_key": "${{ secrets.API_KEY }}"},
    }]}},
})
inventory_source = Provenance(
    source.authority,
    Scope(repository, ".github/workflows", revision),
    f"https://github.com/{repository}/tree/{revision}/.github/workflows",
    observed_at,
)
result = diagnose(Inventory(inventory_source, [definition], complete=True))
```

`Definition.complete=True` is a caller attestation that normalization preserved
all action references, jobs and steps. A normalizer encountering duplicate keys,
anchors, aliases, merges, expression-generated structures, or unsupported YAML
must set `complete=False` or withhold the mapping. A mapping does not prove the
original YAML was parsed correctly. Raw YAML text is always uncertain.

Accepted workflow structure is `jobs` containing at most 32 literal job mappings
with literal `steps` lists of at most 64 mappings, or a literal reusable `uses`.
Composite definitions require `runs.using: composite` and a literal steps list.
Names, prompts, environment values and script bodies are not exported. Scripts
are not inspected for CLI invocations. `on` exports only recognized event names,
never event filters or schedules. Permissions export only known permission
keys with read/write/none values, or read-all/write-all. Root and job permissions
are separate declarations, without calculating inherited effective permissions.
Job and step conditions are reported as unknown execution conditions. None are
evaluated. Trigger declarations do not prove a workflow can execute.

Only exact `anthropics/claude-code-action@REF` step uses confirm the action.
`REF` must be a full hexadecimal SHA or a numeric release tag such as `v1`,
`v1.2`, `v1.2.3`, or a numeric tag with a prerelease suffix. This validates ref
syntax, not existence, trust, or feature support at that action version. Workflow
and job names have no effect. `main`, `beta`, expressions, Docker actions and
unresolved references remain unknown. A validated `actions/checkout` use is
treated as a known standalone non-Claude action. Other actions can hide a
composite reference and require exact supplied definitions before absence can
be established.

## References and bounds

Local reusable references such as `./.github/workflows/shared.yml` use the
calling definition's repository and exact revision. Reusable workflow paths must
point directly to a `.yml` or `.yaml` file under `.github/workflows`. Local composite references
such as `./actions/shared` require a supplied `action.yml` or `action.yaml` at
that same revision. External reusable and composite references require a full
SHA in `uses` and an independently supplied matching definition. External tags
and branches are never resolved. Inputs and secrets passed through a reusable
workflow are not substituted. An input reference remains a declaration with
unknown readiness.

Default bounds are 20 supplied files, definition depth 4, and 512 visited
definition/job/step nodes. Roots count at depth 1, and depth 0 withholds all
definition traversal. `Bounds` can reduce these ceilings. Duplicate identities,
cycles, exhausted budgets, inaccessible bodies and unresolved references mark
the result partial. Positive discoveries within a partial scope remain positive
observations. Partial results cannot establish absence. Definitions outside the
inventory revision are only consumed as exact referenced dependencies. An
explicit complete inventory is required even when the definition list is empty.

Supply `default_revision` independently to distinguish default from non-default
revision observations. Without that value, configured-default state is unknown.
A confirmed action on a non-default revision never confirms default configuration.

## Facts, authority and redaction

Every fact carries `authority`, repository/path/revision scope, a validated
source URL and `observed_at`. Times must include a timezone and are normalized
to UTC. Observation time does not stand in for provider publication/update time.
Source URLs are restricted to exact scoped GitHub repository, blob/tree,
installation/workflow metadata, issue/PR and run links. Credentials, queries,
fragments, unrelated repositories and arbitrary URLs are rejected without
echoing the value. A caller attestation validates syntax and provenance only.

`Observation` accepts independent app-installation, app-access, workflow-enabled
and managed-review evidence. Operator reports retain `operator-report` authority
and `operator-reported` state. Confirmed metadata requires the caller to attest
`supported-authorized-metadata` authority. Constructing an observation does not
make an unsupported API supported. Repository/revision mismatches are excluded,
and workflow-enabled evidence must match an inventoried workflow path. Absence
requires `complete_scope=True` for the observation's exact scope.

States are `confirmed`, `operator-reported`, `absent-in-complete-scope`, `unknown`,
`unsupported` and `access-denied`. A supplied definition HTTP status of 403 means
access denied. 404 and 429 mean unknown, without inferring deletion, absence,
installation or auth validity. Metadata success never promotes another facet.
Disabled workflow metadata remains separate from definition discovery.

Auth declarations recognize `anthropic_api_key`, `claude_code_oauth_token`,
`anthropic_federation_rule_id` with `anthropic_organization_id`, and literal true
flags `use_bedrock`, `use_vertex`, `use_foundry`. Federation identifiers are
never exported. Only simple `secrets.NAME` and `inputs.NAME` expressions are
classified as references. Reference names are omitted. Literal credential inputs
produce `literal-redacted`, without retaining their values in the report.
Complex expressions or incomplete federation declarations remain unknown.
Multiple modes are listed without selecting an effective authentication mode.

Auth absence means no recognized action input declaration in the supplied step.
It says nothing about environment-based configuration. Secret availability,
Console/provider setup, OIDC exchanges, credential validity and auth readiness
remain unknown. The module reads no credential files or environment variables,
requests no secret values and performs no authentication probe.

Execution accepts only independent `ActionRunEvidence` objects from the existing
run normalizer, matching the inventory repository and definition revision.
These observations do not require a current definition to be available. They
retain their own observation time and normalized run identity/status/conclusion,
without proving workflow discovery, review completion, findings or merge
readiness. At most 20 run observations and 32 metadata observations are consumed.
No deduplication, refresh ordering or history storage is supplied here.

Managed-review evidence is independent. Claude cloud lifecycle is unsupported.
`findings_state` and `merge_readiness` remain unknown. Recommendations describe
setup gaps and never install apps, write workflows/secrets, dispatch, rerun,
post comments, enable reviews or invoke a paid service.

## Remaining #1602 acceptance and verification

The run/jobs/artifact importer, supported live GitHub reads, persistence, CLI
and FleetHub readouts remain open. General YAML normalization and provider
auth readiness require supported evidence before those facets can be confirmed. This slice adds no runtime dependencies or existing-module changes.

Tests in `tests/test_claude_actions_diagnostics.py` cover app-only reports,
auth declarations and redaction, absence/completeness, disabled metadata,
false-positive names, pinned refs, reusable/composite definitions, access errors,
cycles/budgets, non-default revisions and independent execution/review evidence.
The IO regression traps file opens, sockets and subprocess invocation.

Run `./scripts/verify-focused tests/test_claude_actions_diagnostics.py
tests/test_claude_actions_evidence.py` on one line. Full repository verification remains required before publication.

## Primary sources

Official documentation was retrieved on 2026-10-06. These pages supplied no
publication date.

- [Claude Code GitHub Actions](https://code.claude.com/docs/en/github-actions)
  documents separate app, workflow and authentication setup, API-key/OAuth
  inputs, federation inputs, and the separate managed-review/cloud products.
- [Cloud providers](https://code.claude.com/docs/en/github-actions-cloud-providers)
  documents provider setup beyond the action's declared provider flag.
- [Official action input manifest](https://raw.githubusercontent.com/anthropics/claude-code-action/main/action.yml)
  documents the exact auth and provider input names inspected for this slice.
  This retrieved moving-main documentation is not a resolved action revision
  and is never used to fetch or authorize definitions during diagnosis.
