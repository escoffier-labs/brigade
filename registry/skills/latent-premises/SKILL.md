---
name: latent-premises
description: Use when reviewing a diff or module for hidden assumptions - things the code takes for granted that nothing guarantees - or when asked "what could break this later", "what does this assume", or to harden code that works today. Complements bug-hunt: bug-hunt finds defects with triggers today; this finds the ones waiting for tomorrow.
---

# latent-premises

An unguarded premise is an undeclared allergen. The dish is fine for most diners, every night, until the wrong one orders it, and nothing on the menu warned anybody. Code carries the same hazard: a premise that holds on every input the system produces today, enforced by nothing, documented nowhere, that fails silently the day the premise breaks.

**Core principle:** a premise is only a finding when it is genuinely unenforced AND you can name what concretely breaks when it fails. Enforced premises are fine. Consequences you cannot name are hypotheses, not findings.

**Read-only.** This is a review lens. Fixing is a separate engagement.

## The taxonomy

Hunt in five categories. Each finding gets exactly one.

| Category | The code assumes | Examples |
|----------|------------------|----------|
| input | boundary or external data is well-formed, with nothing upstream guaranteeing it | a non-empty array (`[0]`), an existing key, a successful parse, a non-null string, an in-range number |
| contract | callee behavior the signature or types do not promise | a result is sorted, a call is idempotent, a value the type says is nullable is never null, a specific error type |
| environment | the world outside the process is arranged | an env var is set, a path exists, a service is reachable, a timezone or locale, an OS behavior |
| ordering | events happen in one sequence | init-before-use, single-threaded access to shared state, no interleaving between read and write, "this runs once" |
| cardinality | a shape or scale property holds | uniqueness assumed but not enforced, one-to-one where the data allows one-to-many, "the list is always small" |

## Process

1. **Pin the scope.** Default to the diff or the recently changed code, same as [reduce](../reduce/SKILL.md).
2. **Enumerate the premises.** For each changed function, list what it takes for granted, category by category.
3. **Check enforcement before flagging.** Read enough surrounding code to tell whether the type system, a framework contract, or a validated boundary upstream already guarantees the premise. Enforced means NOT a finding, and re-guarding it is over-defensive noise.
4. **Name the break.** State the concrete failure when the premise stops holding: which input or state change, which wrong result or crash. Cannot name it? Drop it.
5. **Classify honestly.** If you can construct a failing input from code visible today, it is a present defect. Hand it to [bug-hunt](../bug-hunt/SKILL.md) severity rules, not this report. If the premise holds on everything the system can currently produce, it is latent. Report it here.
6. **Point at one resolution.** Every finding ends with exactly one arrow: **guard it** (add the check), **document it** (state the precondition where the next caller will read it), or **encode it in the type** (make the compiler enforce it). Pick the cheapest one that actually closes the hazard.

## Report contract

```markdown
# latent-premises report: <scope> (<date>)

## Verdict
Paragraph: how much unguarded weight the code carries, the scariest premise.

## Findings
### [category] Short imperative title
- **Where:** file:line
- **Premise:** what the code takes for granted
- **Unenforced because:** what you checked (types, upstream validation, framework contract)
- **Breaks when:** the concrete input or state change, and what happens
- **Resolution:** guard it | document it | encode it in the type - with the specific move

## Not findings
Premises checked and found enforced (count and one-liners), and premises the plan or a comment explicitly accepted.
```

## Common mistakes

- Flagging a premise the types or an upstream check already enforce. Read the callers before writing the finding.
- "This might cause issues" with no nameable break. A consequence you cannot state concretely is not a finding.
- Reporting a constructible present defect as latent. If the failing input exists today, it is a bug, and it outranks everything in this report.
- Flagging a premise the approved plan explicitly accepted or scoped out. Recording a decision is not discovering a hazard.
- Suggesting all three resolutions at once. Pick the one that closes the hazard cheapest. A wall of options is a punt.

---

The premise taxonomy is adapted from the correctness reviewer in [alp-river](https://github.com/alp82/alp-river) (MIT, Alper Ortac).
