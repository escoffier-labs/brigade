---
topic: workspace-file-maintenance
category: foundation
tags: [memory, bootstrap, maintenance, handoff]
---

# Workspace File Maintenance

Which canonical file to update when a fact changes. Keep this table out of `AGENTS.md` so the bootstrap file stays under budget.

| File | Update when |
|------|-------------|
| `USER.md` | Personal info, project change, preference learned |
| `SOUL.md` | Personality or voice evolves (rare, ask first) |
| `MEMORY.md` | New card categories, major architecture shift |
| `TOOLS.md` | New service, port change, host change, infra change |
| `SAFETY_RULES.md` | New safety lesson, new device, new restriction |
| `IDENTITY.md` | Name, emoji, vibe changes (rare) |
| `HEARTBEAT.md` | Periodic check-in behavior changes |
| `rules/*.md` | Workflow correction, pipeline rule |
| `.learnings/*.md` | Errors hit, lessons learned |
| `memory/cards/*.md` | New durable knowledge |

**End of session:** Did the user correct you? Did infra change? Did a workflow change? Did you learn personal info? If you are the memory owner, update the relevant memory file. If you are a side harness, write a Memory Handoff that routes the update.

If you learned it, write it down. If it changed, update the file only when you own canonical memory; otherwise hand it off.
