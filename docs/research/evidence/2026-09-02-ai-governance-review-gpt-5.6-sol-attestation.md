BLUF: Brigade should emit an in-toto Statement v1 with predicate type `https://brigade.dev/attestation/agent-change/v1`, plus digest-linked Test Result, request, execution, and approval statements signed by their actual principals. Very likely, confidence High - Brigade already captures most change and verification facts; the missing pieces are human identity, workload identity, final-subject approval binding, and public-key signatures.

- Bind the Statement subject to the resulting Git tree, with `gitCommit` added only when that commit has the same tree.
- Treat the AgentChange statement as an evidence index. A Brigade exporter signature alone cannot prove that the requester or approver acted.
- Never export environment values, raw transcripts, absolute paths, or unredacted logs.
- Keep SLSA provenance and source-level claims with CI and the source-control system.

Alternative: one monolithic predicate - better if consumers cannot retrieve an attestation bundle, but it duplicates Test Result data and makes multi-party signing less clear.

Next: split issue #1404’s acceptance test into an SSHSIG profile test and a separate cosign-generated Sigstore bundle test, then add final-tree binding to issue #1405.

## 1. Proposed in-toto statement

The official Statement v1 requires `_type`, a digest-bearing `subject`, and `predicateType`. DSSE is its recommended authentication envelope. [in-toto Statement v1, accessed 2026-09-02](https://github.com/in-toto/attestation/blob/main/spec/v1/statement.md), [in-toto Envelope v1, accessed 2026-09-02](https://github.com/in-toto/attestation/blob/main/spec/v1/envelope.md).

The following is valid JSON. Hashes and identities are illustrative, valid-shape values.

```json
{
  "_type": "https://in-toto.io/Statement/v1",
  "subject": [
    {
      "name": "git+https://github.com/acme/payments",
      "uri": "git+https://github.com/acme/payments?ref=refs/heads/brigade/1404",
      "digest": {
        "gitTree": "2222222222222222222222222222222222222222"
      },
      "annotations": {
        "sourceRef": "refs/heads/brigade/1404"
      }
    }
  ],
  "predicateType": "https://brigade.dev/attestation/agent-change/v1",
  "predicate": {
    "schemaVersion": 1,
    "attestationId": "urn:brigade:agent-change:20260902-141500-a1b2c3d4:2222222222222222222222222222222222222222",
    "run": {
      "id": "20260902-141500-a1b2c3d4",
      "status": "completed",
      "startedAt": "2026-09-02T14:15:00Z",
      "finishedAt": "2026-09-02T14:24:31Z"
    },
    "repository": {
      "uri": "git+https://github.com/acme/payments",
      "ref": "refs/heads/brigade/1404",
      "baseline": {
        "gitCommit": "1111111111111111111111111111111111111111"
      },
      "result": {
        "gitTree": "2222222222222222222222222222222222222222"
      },
      "change": {
        "patch": {
          "name": "changes.patch",
          "uri": "urn:brigade:run:20260902-141500-a1b2c3d4:artifact:changes.patch",
          "digest": {
            "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
          },
          "mediaType": "text/x-diff"
        },
        "changedPaths": [
          "src/brigade/attestation.py",
          "tests/test_attestation.py"
        ]
      }
    },
    "request": {
      "requester": {
        "id": "https://identity.acme.example/users/00u-requester-7",
        "kind": "human",
        "authentication": {
          "method": "oidc-fulcio",
          "issuer": "https://login.acme.example",
          "subject": "00u-requester-7"
        }
      },
      "requestedAt": "2026-09-02T14:14:42Z",
      "task": {
        "text": "Add a signed in-toto attestation export for one agent-produced code change.",
        "digest": {
          "sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        },
        "privacyClass": "private"
      },
      "evidence": {
        "name": "agent-request.dsse.json",
        "uri": "urn:brigade:run:20260902-141500-a1b2c3d4:request",
        "digest": {
          "sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
        },
        "mediaType": "application/vnd.in-toto.agent-request+dsse"
      }
    },
    "executions": [
      {
        "principal": {
          "id": "spiffe://prod.acme.example/brigade/seat/coder",
          "kind": "workload",
          "authentication": {
            "method": "spiffe-x509-svid",
            "issuer": "spiffe://prod.acme.example",
            "subject": "spiffe://prod.acme.example/brigade/seat/coder"
          }
        },
        "seatId": "coder",
        "role": "worker",
        "harness": {
          "name": "codex",
          "version": "0.55.0"
        },
        "transport": {
          "name": "app-server",
          "version": "0.9.2"
        },
        "model": {
          "provider": "openai",
          "name": "gpt-5.6-terra",
          "version": "provider-revision-2026-08-15",
          "reasoning": "high"
        },
        "invocation": {
          "sessionId": "sess_01J7BRIGADE",
          "requestId": "req_01J7BRIGADE",
          "startedAt": "2026-09-02T14:16:02Z",
          "finishedAt": "2026-09-02T14:20:49Z",
          "status": "succeeded"
        },
        "evidence": {
          "name": "agent-execution-coder.dsse.json",
          "uri": "urn:brigade:run:20260902-141500-a1b2c3d4:execution:coder",
          "digest": {
            "sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
          },
          "mediaType": "application/vnd.in-toto.agent-execution+dsse"
        }
      }
    ],
    "verification": {
      "overallResult": "PASSED",
      "runs": [
        {
          "runId": "20260902-142100-work-verify-e5f6a7b8",
          "status": "completed",
          "startedAt": "2026-09-02T14:21:00Z",
          "finishedAt": "2026-09-02T14:23:58Z",
          "commands": [
            {
              "checkId": "unit-tests",
              "checkRole": "effectiveness",
              "display": "python3 -m pytest -q",
              "argv": [
                "python3",
                "-m",
                "pytest",
                "-q"
              ],
              "status": "completed",
              "exitCode": 0
            },
            {
              "checkId": "attestation-schema",
              "checkRole": "utility_guardrail",
              "display": "python3 -m pytest tests/test_attestation.py -q",
              "argv": [
                "python3",
                "-m",
                "pytest",
                "tests/test_attestation.py",
                "-q"
              ],
              "status": "completed",
              "exitCode": 0
            }
          ],
          "testResult": {
            "name": "test-result.dsse.json",
            "uri": "urn:brigade:verify:20260902-142100-work-verify-e5f6a7b8:test-result",
            "digest": {
              "sha256": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
            },
            "mediaType": "application/vnd.in-toto.test-result+dsse"
          }
        }
      ],
      "contract": {
        "name": "verify-manifest:attestation-export",
        "uri": "urn:brigade:verify-manifest:attestation-export",
        "digest": {
          "sha256": "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
        },
        "mediaType": "application/json"
      }
    },
    "authorization": {
      "required": true,
      "decision": "allow",
      "scope": "merge",
      "approver": {
        "id": "https://identity.acme.example/users/00u-approver-9",
        "kind": "human",
        "authentication": {
          "method": "oidc-fulcio",
          "issuer": "https://login.acme.example",
          "subject": "00u-approver-9"
        }
      },
      "approvedAt": "2026-09-02T14:24:20Z",
      "reason": "Reviewed the final patch and the required verification results.",
      "approvedSubject": {
        "gitTree": "2222222222222222222222222222222222222222"
      },
      "evidence": {
        "name": "human-approval.dsse.json",
        "uri": "urn:brigade:run:20260902-141500-a1b2c3d4:approval:merge",
        "digest": {
          "sha256": "9999999999999999999999999999999999999999999999999999999999999999"
        },
        "mediaType": "application/vnd.in-toto.human-approval+dsse"
      },
      "separationOfDuties": {
        "result": "PASSED",
        "policy": {
          "name": "acme-agent-change-sod-v1",
          "uri": "https://policy.acme.example/agent-change/sod/v1",
          "digest": {
            "sha256": "0000000000000000000000000000000000000000000000000000000000000000"
          },
          "mediaType": "application/rego"
        }
      }
    },
    "evidence": {
      "runReceipt": {
        "name": "run.json",
        "uri": "urn:brigade:run:20260902-141500-a1b2c3d4:receipt",
        "digest": {
          "sha256": "3333333333333333333333333333333333333333333333333333333333333333"
        },
        "mediaType": "application/json"
      },
      "roster": {
        "name": "roster.json",
        "uri": "urn:brigade:run:20260902-141500-a1b2c3d4:roster",
        "digest": {
          "sha256": "4444444444444444444444444444444444444444444444444444444444444444"
        },
        "mediaType": "application/json"
      },
      "plan": {
        "name": "plan.json",
        "uri": "urn:brigade:run:20260902-141500-a1b2c3d4:plan",
        "digest": {
          "sha256": "5555555555555555555555555555555555555555555555555555555555555555"
        },
        "mediaType": "application/json"
      },
      "lifecycleJournal": {
        "name": "events/lifecycle.jsonl",
        "uri": "urn:brigade:run:20260902-141500-a1b2c3d4:lifecycle",
        "digest": {
          "sha256": "6666666666666666666666666666666666666666666666666666666666666666"
        },
        "mediaType": "application/x-ndjson"
      },
      "causalReceipt": {
        "name": "causal-receipt.json",
        "uri": "urn:brigade:run:20260902-141500-a1b2c3d4:causal-receipt",
        "digest": {
          "sha256": "7777777777777777777777777777777777777777777777777777777777777777"
        },
        "mediaType": "application/json"
      }
    },
    "producer": {
      "principal": {
        "id": "spiffe://prod.acme.example/brigade/exporter",
        "kind": "workload",
        "authentication": {
          "method": "spiffe-x509-svid",
          "issuer": "spiffe://prod.acme.example",
          "subject": "spiffe://prod.acme.example/brigade/exporter"
        }
      },
      "software": {
        "name": "brigade",
        "version": "1.14.0"
      },
      "exportedAt": "2026-09-02T14:25:00Z"
    },
    "integrity": {
      "journalChainHead": {
        "sha256": "8888888888888888888888888888888888888888888888888888888888888888"
      }
    }
  }
}
```

### Types

- `DigestSet`: JSON object whose keys are algorithm identifiers and whose values are lowercase hexadecimal strings.
- `ResourceDescriptor`: `{name?: string, uri?: URI string, digest: DigestSet, mediaType?: string, annotations?: object}`.
- `Principal`: `{id: URI string, kind: "human"|"workload", authentication: {method: string, issuer: URI string, subject: string}}`.
- Timestamps: RFC 3339 UTC strings ending in `Z`.
- Optional fields are omitted, not set to `null`, except `commands[].exitCode`, where `null` preserves the existing interrupted/timed-out meaning.

### Field mapping

Mappings are against [receipt-schemas.md](/tmp/oracle-gov/receipt-schemas.md), [issue-1404.md](/tmp/oracle-gov/issue-1404.md), and [issue-1405.md](/tmp/oracle-gov/issue-1405.md).

| Predicate path | Type and meaning | Brigade source |
|---|---|---|
| `_type` | Required string, fixed Statement schema URI | Fixed at export |
| `subject[]` | Required `ResourceDescriptor[]`, immutable result being attested | `tree_fingerprint`; add `gitCommit` only after matching it to that tree |
| `subject[].uri`, `repository.uri` | Canonical `git+https` repository identity | **New capture**. `target` and `cwd` are local paths and cannot substitute |
| `subject[].annotations.sourceRef`, `repository.ref` | Fully qualified source ref | Derived from verify `git.branch`; prefix with `refs/heads/` where appropriate |
| `predicateType` | Required string, fixed predicate URI | Fixed at export |
| `schemaVersion` | Required integer, custom predicate wire version | New export field |
| `attestationId` | Required URI, deterministic change-attestation identifier | Derived from run ID and result digest |
| `run.id` | Required string | Run directory ID or verify `producer_run_id` |
| `run.status` | Required enum | `brigade.run.v1.status` |
| `run.startedAt`, `finishedAt` | Required timestamp strings | Run `started_at`, `finished_at` |
| `repository.baseline` | Required `DigestSet` | Verify `baseline_commit` |
| `repository.result` | Required `DigestSet` | Verify `tree_fingerprint`; optional matched commit from `git.head` |
| `repository.change.patch` | Required `ResourceDescriptor` | Verify `changes_patch_sha256`; URI derived from the retained `changes.patch` |
| `repository.change.changedPaths` | Optional string array, repo-relative and sorted | Verify `git.dirty_files` if it is a path list; otherwise **new normalized capture** |
| `request.requester` | Required `Principal` | **New capture** |
| `request.requestedAt` | Required timestamp | **New capture**. Run start is not necessarily the human request time |
| `request.task.text` | Required in a private bundle; optional in a public projection | Existing `run.task`, after redaction |
| `request.task.digest` | Required `DigestSet` | Derived from exact canonical task bytes before dispatch |
| `request.task.privacyClass` | Required enum `public|private|redacted` | Derived from export/redaction policy |
| `request.evidence` | Required descriptor for a requester-signed event | **New capture**, proposed predicate `https://brigade.dev/attestation/agent-request/v1` |
| `executions[]` | Required array, one entry for every seat that made a model call or produced output | Roster plus worker-results/synthesis |
| `executions[].principal` | Required workload `Principal` | **New capture**. Seat name alone is not an authenticated identity |
| `seatId`, `role` | Required strings | Roster agent key and `agents[].role` |
| `harness.name` | Required string | `agents[].cli` or verify `harness_session.harness` |
| `harness.version` | Required string when known | **New capture** |
| `transport.name`, `version` | Required name, optional version | Roster `transport`, `transport_version`; worker `acpx_version` where applicable |
| `model.name`, `reasoning` | Required strings | Roster `model`, `reasoning`, or worker `effective_model`, `reasoning` |
| `model.provider`, `model.version` | Provider and immutable revision | **New capture** for ordinary executions. Generated-patch quarantine has these only in its narrow case |
| `invocation.sessionId`, `requestId` | Optional strings | Worker/synthesis `session_id`, `request_id` |
| `invocation.startedAt`, `finishedAt` | Required timestamps | Selected `attempts[]` entry |
| `invocation.status` | Required enum `succeeded|failed|timed_out|canceled` | Derived from worker result and selected attempt |
| `executions[].evidence` | Descriptor for seat-signed execution statement | **New capture**, proposed predicate `https://brigade.dev/attestation/agent-execution/v1` |
| `verification.overallResult` | Required enum `PASSED|FAILED|INCOMPLETE` | Derived from verify receipt status, required commands, exit codes, and complete subject binding |
| `verification.runs[].runId`, timestamps, status | Required verify invocation identity | Verify receipt `run_id`, `started_at`, `completed_at`, `status` |
| `commands[].display`, `argv` | Display string required, argv optional | Verify `commands[].command`, `argv` |
| `commands[].checkId`, `checkRole` | Optional strings for manifest checks | Verify `check_id`, `check_role` |
| `commands[].status`, `exitCode` | Required status and integer/null | Verify `commands[].status`, `exit_code` |
| `testResult` | Descriptor for standard Test Result Statement | Generated from the verify receipt, then hashed |
| `verification.contract` | Descriptor for verifier policy/configuration | `verify_manifest_id`, `verification_contract`, or manifest payload digest |
| `authorization.required` | Required boolean | Policy input, **new persisted capture** |
| `decision`, `scope`, `approver`, `approvedAt`, `reason` | Required when approval is present | Planned approval event in issue #1405 |
| `authorization.approvedSubject` | Exact result digest approved by the human | **New capture beyond #1405**. Without it, approval can be replayed after changes |
| `authorization.evidence` | Descriptor for separately signed approval | **New capture**, proposed predicate `https://brigade.dev/attestation/human-approval/v1` |
| `separationOfDuties.result` | Required policy result | Derived by comparing canonical principal IDs |
| `separationOfDuties.policy` | Required policy descriptor | **New policy digest capture** |
| `evidence.*` | Digest-bearing descriptors for source receipts | Existing run/roster/plan/journal/causal files; hashes computed during export or taken from `work-run.files[]` |
| `producer.principal` | Identity signing/exporting the assembled statement | **New capture** |
| `producer.software` | Exporter name/version | Name fixed; installed Brigade version captured at export |
| `producer.exportedAt` | Required timestamp | Generated at export |
| `integrity.journalChainHead` | Digest of last authenticated lifecycle event | Run `journal_last_event_digest` |

### Required behavior

- Refuse AgentChange export when `tree_fingerprint` or patch digest is unavailable. An in-toto subject without a digest is invalid.
- Approval must cover the final result tree and the exact Test Result descriptor digests. Any later tree change invalidates it.
- Include every acting seat, including orchestrator and reviewer seats that made model calls.
- Mark `overallResult: "INCOMPLETE"` when required checks are missing or unverifiable.
- The standard Test Result companion should use `https://in-toto.io/attestation/test-result/v0.1`, with `result`, configuration descriptors, and passed/warned/failed check IDs. Exact command argv and exit codes remain in AgentChange because Test Result does not define them. [in-toto Test Result v0.1, accessed 2026-09-02](https://github.com/in-toto/attestation/blob/main/spec/predicates/test-result.md).
- A public projection may omit task text and approval reason, retaining their digests and access-controlled evidence descriptors.
- Do not copy `commands[].env`, stdout/stderr contents, raw transcript events, or absolute log paths.

## 2. Existing systems and standards

| Existing mechanism | Coverage beyond AgentChange | AgentChange coverage it lacks |
|---|---|---|
| GitHub Copilot coding agent | Forge-native commit authorship, human co-author, permanent `Agent-Logs-Url`, session navigation, and enterprise audit events with `actor_is_agent`, `agent_session_id`, and initiating `user`; audit search covers 180 days. [GitHub Changelog, 2026-03-20](https://github.blog/changelog/2026-03-20-trace-any-copilot-coding-agent-commit-to-its-session-logs/), [GitHub audit documentation, accessed 2026-09-02](https://docs.github.com/en/copilot/reference/enterprise-administrators/agentic-audit-log-events) | Portable signatures, patch/tree binding, model revision, exact test commands, independent signed approval, offline verification |
| GitLab Duo Agent Platform | Event-level user inputs, LLM requests, tool calls and outputs; audit streaming; composite human plus service-account authorization using the more restrictive permissions. Current AI audit storage is beta and disabled by default. [GitLab AI audit events, accessed 2026-09-02](https://docs.gitlab.com/user/duo_agent_platform/ai-audit-events/), [GitLab composite identity, accessed 2026-09-02](https://docs.gitlab.com/user/duo_agent_platform/composite_identity/) | A portable digest-bound statement, public-key proof over the final tree, standard Test Result links, and signed final approval |
| in-toto SCAI v0.3 | Arbitrary fine-grained security/integrity attributes, conditions, evidence collections, and compute-platform properties such as trusted execution | Requester, seat/model chronology, patch identity, command-level verification, approval semantics. SCAI remains useful for claims such as “static analysis passed” or “seat ran in an attested environment.” [SCAI v0.3, accessed 2026-09-02](https://github.com/in-toto/attestation/blob/main/spec/predicates/scai.md) |
| in-toto Test Result v0.1 | Standard test result, configuration, and passed/warned/failed test names | Human request, agent identity/model, change identity, approval. Brigade should emit it as a companion instead of inventing another general test format |
| SLSA v1.2 Source track and VSA | Source history continuity, source-control-system enforcement, protected refs, source levels, and two-person review of the final revision. A VSA summarizes the result of policy verification | Local pre-merge agent execution and its detailed receipts. Brigade is not the source-control system and must not independently assert a SLSA Source level. Source provenance remains deliberately implementation-defined. [SLSA v1.2 source requirements, accessed 2026-09-02](https://slsa.dev/spec/v1.2/source-requirements), [VSA v1, accessed 2026-09-02](https://slsa.dev/spec/v1.2/verification_summary) |
| OpenTelemetry GenAI spans | High-volume traces for `invoke_agent`, `invoke_workflow`, `plan`, and `execute_tool`, with latency, provider/model, errors, and token/tool-call metrics | Integrity, durable identity, artifact digest, approval, or non-repudiation. Reference an OTLP export or trace root by digest rather than embedding spans. Agent conventions remained `Development` on 2026-09-02. [OTel GenAI agent conventions, accessed 2026-09-02](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-agent-spans.md) |
| C2PA | Asset-embedded signed claims, ingredient/action lineage, hard and soft bindings, derived-asset handling, and redaction-aware ingredient validation | Source-control, model-seat, test, or approval semantics. Its hard-binding and ingredient-chain design is a useful analogy, not the correct coding-agent format. [C2PA 2.2 hard binding and ingredients, accessed 2026-09-02](https://spec.c2pa.org/specifications/specifications/2.2/specs/C2PA_Specification.html) |

The AgentChange predicate can later be one of the source-provenance evidence types consumed by a source-control-system VSA. Brigade should never label its own local receipt `SLSA_SOURCE_LEVEL_4`; only the system enforcing protected-branch review has enough information to make that claim.

## 3. Identity binding

A signature supplies defensible attribution only when the verifier trusts the key-to-principal registry, key custody, and signing time. It cannot prove that a person personally reviewed every line or that an unlocked device was not misused.

### What each principal must sign

- Requester: task bytes or digest, repository URI, baseline commit, allowed scope, request time, nonce, and run ID.
- Seat workload: requester evidence digest, seat ID, harness/model claims, invocation IDs, input baseline, and produced patch/result-tree digest.
- Approver: final result-tree digest, Test Result statement digests, repository and merge scope, decision, reason, time, and any expiry.
- Exporter: the assembled AgentChange statement and every evidence descriptor.

The model does not possess an identity key. The seat process or remote agent service signs, while `model.provider/name/version` remains a signed claim by that workload. Provider-authenticated model proof would require a signed provider receipt, which I could not confirm as generally available.

### Identity mechanisms

| Mechanism | Strengths | Limits | Use |
|---|---|---|---|
| SPIFFE ID and SVID | URI workload identity, short-lived X.509/JWT credentials, trust-domain roots, process-level issuance, automatic rotation | Operational infrastructure required; identifies workloads rather than humans | Best enterprise seat identity. [SPIFFE concepts, accessed 2026-09-02](https://spiffe.io/docs/latest/spiffe-about/spiffe-concepts/) |
| OIDC subject through Fulcio | Binds identity claims to a short-lived signing certificate; verifier checks certificate identity plus OIDC issuer; supports SPIFFE URI subjects | Online identity flow is normally needed at signing; identity quality depends on the issuer and claims | Best enterprise human identity and Sigstore signing. Fulcio embeds OIDC-derived identities in certificate SANs. [Sigstore Fulcio OIDC documentation, accessed 2026-09-02](https://docs.sigstore.dev/certificate_authority/oidc-in-fulcio/) |
| SSH public key | Works offline, common tooling, `allowed_signers` maps principals to keys and supports namespace/time restrictions and KRL revocation; FIDO-backed keys are available | Usually long-lived; identity enrollment and revocation are local administrative duties | Best offline OSS default. [OpenSSH `ssh-keygen`, 2026-08-07 manual](https://man.openbsd.org/ssh-keygen.1) |
| Plain email | Human-readable and easy to display | No proof of key possession, globally unstable, reusable after reassignment | Display metadata only. Never use as the canonical principal unless an OIDC certificate or trusted key registry binds it |

OIDC identity should be compared as the pair `(issuer, subject)`, not by email. OpenID Connect calls `iss` plus `sub` the locally unique, stable identifier; email is not guaranteed to have that property. [OpenID Connect Core 1.0 errata 2, 2023-12-15](https://openid.net/specs/openid-connect-core-1_0.html#ClaimStability).

Recommendations:

- Offline CLI: one SSH Ed25519 or `ed25519-sk` key per human and per seat role, with a versioned `allowed_signers` file, namespace `attestation@brigade.dev`, validity windows, and an optional KRL. Do not silently reuse a Git author email as identity.
- Enterprise: corporate OIDC `iss/sub` through Fulcio for humans; SPIFFE IDs and short-lived SVIDs for agent seats and the exporter. Preserve the Fulcio certificate, chain, transparency material, and trusted root in the verification bundle.

## 4. Signing and transparency

DSSE v1.0.2 signs `PAE(payloadType, payload)` and permits an application-specific signature format agreed between signer and verifier. [DSSE protocol, 2024-05-10](https://github.com/secure-systems-lab/dsse/blob/master/protocol.md).

Use:

```json
{
  "payloadType": "application/vnd.in-toto.agent-change+json",
  "payload": "<base64 of the exact UTF-8 Statement bytes>",
  "signatures": [
    {
      "keyid": "<unauthenticated key-selection hint>",
      "sig": "<base64 signature bytes>"
    }
  ]
}
```

`keyid` is only a hint. Trust policy chooses acceptable keys, identities, signature profiles, and threshold.

### Option comparison and verifier commands

| Option | Assessment | Verifier |
|---|---|---|
| DSSE plus `ssh-keygen -Y sign` | Recommended offline default. Brigade materializes DSSE PAE bytes, SSHSIG signs those bytes, and the complete SSHSIG object becomes the application-specific `sig`. Supports existing SSH agents and hardware keys | After Brigade extracts the exact PAE and SSHSIG: `ssh-keygen -Y verify -f allowed_signers -I requester@acme.example -n attestation@brigade.dev -s change.sshsig -r revoked.krl < change.pae` |
| DSSE plus minisign | Good portable alternative where OpenSSH signing is absent. Ed25519, simple key files, and signed trusted comments | `minisign -Vm change.pae -x change.minisig -p brigade.pub` after exact PAE/signature extraction. [Minisign documentation, accessed 2026-09-02](https://jedisct1.github.io/minisign/) |
| Cosign keyless | Recommended enterprise signer. Produces the Sigstore bundle, short-lived Fulcio certificate, identity claims, and transparency evidence understood by Sigstore tooling | `cosign verify-blob-attestation --bundle change.sigstore.json --certificate-identity 'spiffe://prod.acme.example/brigade/exporter' --certificate-oidc-issuer 'https://oidc.acme.example' --type 'https://brigade.dev/attestation/agent-change/v1' --digest 2222222222222222222222222222222222222222 --digestAlg gitTree --check-claims=true` |
| SCITT, RFC 9943 | Enterprise transparency option for private or regulated environments. The same Statement is carried in a COSE_Sign1 Signed Statement, then registered to obtain a COSE receipt. This is an alternative envelope, not a DSSE wrapper | Proposed Brigade command: `brigade receipts verify-attestation --scitt-trust-store acme-scitt-trust.json change.transparent.cose`. RFC 9943 does not standardize a CLI or registration HTTP API |

Cosign can consume a complete Statement through `cosign attest-blob --statement statement.json --bundle change.sigstore.json`. Its verifier supports URI predicate types, subject digest checks, identity, and issuer constraints. [Cosign `attest-blob`, accessed 2026-09-02](https://github.com/sigstore/cosign/blob/main/doc/cosign_attest-blob.md), [Cosign `verify-blob-attestation`, accessed 2026-09-02](https://github.com/sigstore/cosign/blob/main/doc/cosign_verify-blob-attestation.md).

Important correction to issue #1404: cosign will not natively verify an SSHSIG or minisign signature container. The two acceptance paths should be:

1. SSHSIG/minisign DSSE profile verifies through Brigade and the corresponding external verifier on another machine.
2. A cosign-produced Sigstore bundle verifies through `cosign verify-blob-attestation`.

Do not present conversion of an SSHSIG envelope into a Sigstore bundle as possible. Re-sign the same Statement with cosign.

For cosign deployments, pin a release containing the April 2026 predicate-validation fix, currently v3.0.6 or v2.6.3 or later, and retain `--check-claims=true`. [Sigstore advisory GHSA-v65m-hv3f-mmxr, 2026-04-06](https://github.com/sigstore/cosign/security/advisories/GHSA-v65m-hv3f-mmxr).

### Transparency recommendation

- OSS offline default: SSHSIG DSSE, no transparency claim. Package the Statement, signatures, `allowed_signers`, key history/KRL, and referenced evidence.
- Enterprise default: cosign keyless with corporate OIDC/SPIFFE and a private Sigstore deployment or approved public Rekor use.
- Regulated/private alternative: register a COSE version with a private SCITT transparency service. RFC 9943 receipts prove registration and policy application and can later be checked offline, but transparency does not prove the issuer’s underlying factual claim was honest. [RFC 9943, October 2025](https://www.rfc-editor.org/rfc/rfc9943.html).

The Microsoft CCF reference implementation demonstrates `scitt submit ... --transparent-statement output.cose --wait-for-commit`, but it still describes itself as a reference implementation and its CLI is not part of RFC 9943. [Microsoft SCITT CCF ledger, accessed 2026-09-02](https://github.com/microsoft/scitt-ccf-ledger).

## 5. EU AI Act roles

This is a technical reading, not legal advice. Classification depends on what the vendor markets, the configured model, intended purpose, and the deployment context.

### Role analysis

| Party and facts | Likely role | Articles |
|---|---|---|
| Vendor publishes a model-agnostic Brigade CLI under a qualifying free/open-source licence | Likely no operator role for the bare tool, or covered by the Article 2(12) open-source exclusion | The exclusion does not protect systems placed on the market as high-risk or systems covered by Articles 5 or 50 |
| Vendor distributes a ready-to-use branded agent system that integrates third-party models | Provider of the AI system and likely `downstream provider` under Article 3(68) | Article 4; possibly Article 50; high-risk duties only when the system is classified high-risk |
| Vendor merely integrates an external GPAI model | Not a GPAI model provider | Chapter V model-provider duties, including Articles 53 and 55, remain with the entity developing or having the GPAI model developed and placing it on the market |
| Company uses Brigade and a vendor model under its authority | Deployer | Article 4; Article 26 only for a high-risk deployment |
| Company builds or has the integrated system built and puts it into service under its own name for internal use | Potentially provider plus downstream provider, as well as deployer in operation | Provider duties depend on risk classification and intended purpose |
| Company rebrands, substantially modifies, or changes the intended purpose of a high-risk system | Becomes provider under Article 25 | Article 16 provider obligations and related high-risk requirements |

The formal definitions of provider, deployer, and downstream provider are in Article 3. [European Commission AI Act Service Desk, Article 3, accessed 2026-09-02](https://ai-act-service-desk.ec.europa.eu/en/ai-act/article-3). Article 25 expressly moves high-risk provider responsibility to a deployer or third party after rebranding, substantial modification, or a purpose change that makes the system high-risk. [Article 25, accessed 2026-09-02](https://ai-act-service-desk.ec.europa.eu/en/ai-act/article-25).

An ordinary coding assistant is not automatically an Annex III high-risk system. Using it while developing a high-risk product also does not automatically classify the development tool itself as high-risk. The intended purpose and whether its outputs directly perform an Annex I or Annex III function control that analysis.

If Brigade itself is part of a high-risk AI system:

- Article 12 requires technical capability for automatic event logging.
- Article 14 requires effective human oversight, including the ability to interrupt, override, or reverse.
- Article 17 requires the provider’s quality-management system.
- Article 26 requires deployer controls, assigned human oversight, monitoring, and retention of logs under the deployer’s control for at least six months.

The attestation can support those controls, but it cannot establish that oversight was meaningful or that a complete management system exists.

Article 50(2) may be relevant because it covers AI-generated text in machine-readable form. I could not confirm binding guidance that either includes or excludes source code as “text content,” or whether a coding agent’s edits qualify for the standard-editing exception. Do not claim this attestation alone satisfies Article 50.

### Digital Omnibus changes

Regulation (EU) 2026/1744 was dated 8 July 2026, published 24 July, and entered into force 27 July 2026. [Official EUR-Lex text, 2026-07-24](https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX%3A32026R1744).

It changed the relevant dates and obligations:

- Chapter III Sections 1 to 3 apply from 2 December 2027 for Annex III high-risk systems.
- They apply from 2 August 2028 for Article 6(1)/Annex I product systems.
- Public-authority high-risk systems receive a transition to 2 August 2030.
- Revised Article 4 requires providers and deployers to support development of staff AI literacy, but no longer requires them to guarantee a specific level.
- Providers of synthetic-content systems placed on the market before 2 August 2026 have until 2 December 2026 for Article 50(2).

The Omnibus did not turn an ordinary code orchestrator into a high-risk system or a GPAI model provider.

## 6. Ten likely enterprise security questions

1. **What exactly is signed?** The DSSE PAE of the exact Statement bytes and payload type, plus separately signed request, execution, test, and approval evidence.

2. **Does this prove the model wrote every changed line?** No. It proves Brigade attributed an authenticated seat execution to a patch and result tree.

3. **Can the coordinator invent a requester or approver?** Not if policy requires separately verified principal signatures; an exporter-only signature would permit that.

4. **Can an approval be reused after the code changes?** No. The signed approval binds the final Git tree and Test Result digests.

5. **How is segregation of duties enforced?** Compare canonical principal IDs and reject an approver matching any producing seat; organizations may also require requester and approver to differ.

6. **Are prompts, secrets, or logs exposed?** The public statement carries redacted summaries and digests; environment values, transcripts, stdout/stderr, and absolute paths are excluded.

7. **Can verification happen offline?** Yes for SSHSIG/minisign and bundled Sigstore or SCITT proofs, provided the verifier has the historical trust roots and revocation policy.

8. **What happens after a key is revoked?** Verification evaluates signing time, transparency/timestamp evidence, certificate validity, and the organization’s historical revocation policy rather than current validity alone.

9. **What prevents replay across repositories?** Request and approval evidence bind repository URI, baseline, result tree, run ID, scope, and test evidence.

10. **What if Brigade or a seat is compromised?** Signatures preserve attribution and tamper detection, not truth; independent verification, separate approval keys, workload isolation, and transparency reduce the single-signer trust boundary.

## Verification note

The source mapping anchor check ran successfully:

```text
$ python3 -c '<receipt and issue field assertions>'
receipt mapping anchors: OK (8/8)
```

Brigade recorded two attempted wrapper checks as `status: rejected`, exit 2, because the workspace policy did not admit the ad hoc command. Outcome capture and the required memory handoff then failed with `OSError: [Errno 30] Read-only file system`. Those attempts created two rejected verification-receipt directories under `.brigade/work/verify-runs/`; none of the four supplied documents was modified. The workspace is not a Git repository, so `git status --short` returned `fatal: not a git repository`.