# Brigade feature map

The maintained list of user-facing Brigade surfaces this skill knows how to
drive. A proof that only exercises one entry point is incomplete when the
change touched another one listed here.

Every file below answers the same four questions: what the feature is
(`Sub-features`), how a user reaches it (`How to get to it (user POV)`), the
exact helper calls (`Driving it with control-brigade`), and what bites
(`Gotchas`).

| Feature | File | Drive with |
|---|---|---|
| Install and wiring health | [init-and-doctor.md](init-and-doctor.md) | `new-target`, `init`, `doctor-target` |
| Work verification receipts | [work-verify-receipts.md](work-verify-receipts.md) | `work-verify` |
| Grok Bot connector packs | [grokbot-pack-lifecycle.md](grokbot-pack-lifecycle.md) | `grokbot-pack-setup/-doctor/-canary/-remove` |
| Grok Bot job queue and feed | [run-cloud-grokbot-queue.md](run-cloud-grokbot-queue.md) | `grokbot-feed`, `grokbot-status` |
| Handoff inbox lint | [handoff-lint.md](handoff-lint.md) | `handoff-lint` |

Not yet mapped, and worth adding when a change lands there: `brigade memory`,
`brigade security`, `brigade scrub` / `guard`, `brigade outcome`,
`brigade evidence`, `brigade code`, `brigade skills`, `brigade daily`,
`brigade fleet`. Add a file with the same four H2s rather than stretching an
existing one.

Setup shared by every file:

```bash
C=registry/skills/verify-brigade/control-brigade.py
TARGET=$($C new-target | python3 -c 'import json,sys; print(json.load(sys.stdin)["target"])')
```

Teardown, always:

```bash
$C evidence --target "$TARGET" --label <what-you-proved>
$C cleanup
```
