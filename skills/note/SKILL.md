---
name: note
version: 1.0.0
description: "Create an Obsidian-formatted markdown note documenting the current session topic. Use when the user says /note, 'save this to Obsidian', 'write a note about X', or asks to document a troubleshooting session, system concept, coding pattern, or workflow. Writes to ~/notes/<slug>.md and syncs to the configured Obsidian inbox. If an argument is provided after /note, use it as the topic; otherwise review the conversation."
---

# /note - Create Obsidian-Formatted Notes

Create an Obsidian-formatted markdown note and place it in `~/notes/`. Tell the user the exact file path and confirm the sync.

If the user provided a topic argument after `/note`, use it as the topic. Otherwise, review the conversation to identify the topic.

## When to use

Two distinct modes. Pick the right one before you start writing.

| Mode | When |
|------|------|
| **Verbatim fix capture** | Troubleshooting sessions, root-causing bugs, recovering from incidents. The fix needs to be reproducible step-by-step. Future-you will paste the exact commands. |
| **Summarize / concept** | Learning something new, capturing a workflow, documenting a system. The reader needs to understand *why*, not just *how*. |

Common triggers:

- Troubleshooting sessions - issues, errors, and the fix that worked
- System concepts - how things work on Linux, networking, services, frameworks
- Coding knowledge - languages, libraries, patterns, gotchas
- Development workflows - tools, commands, configurations
- Architecture decisions - what was chosen and why

## Process

1. Decide which mode applies. If unclear, ask: "Should I write this verbatim (reproducible) or summarize the concept?"
2. Review the conversation to identify the topic and pull the relevant commands, errors, paths.
3. Write the `.md` file to `~/notes/<topic-slug>.md`.
4. Sync to the configured Obsidian inbox (see "Sync target" below).
5. Tell the user the exact file path and confirm the sync.

## Note format

### YAML frontmatter

Every note MUST start with YAML frontmatter:

```yaml
---
tags:
  - tag1
  - tag2
created: YYYY-MM-DD
---
```

- 3-5 relevant tags. Lowercase, hyphenated for multi-word.
- Date format `YYYY-MM-DD` (e.g. `2026-01-24`).
- Add `mode: verbatim` or `mode: concept` if you want the mode visible in frontmatter.

### Structure: verbatim fix

```markdown
## The problem

> [!bug] Title
> Brief symptom. Paste the actual error string verbatim.

Context. What was the system doing. What changed recently.

## The fix

> [!success] Title
> The exact command(s) that worked.

```bash
# verbatim commands
```

## How it works

Explain *why* the fix works. Link to docs if relevant. Note any preconditions.

## Gotchas

> [!warning] Title
> Anything that almost bit you. Stale state, ordering dependencies, version constraints.
```

### Structure: summarize / concept

```markdown
## Overview

What this is and why it matters. One paragraph.

## How it works

Core concepts and mechanics. Diagrams or bullet lists are fine; prose is better when the order matters.

## Example

```bash
# practical example, runnable
```

## Tips and gotchas

> [!tip] Title
> Useful patterns.

> [!warning] Title
> Things that can break or surprise you.
```

### Callout types

Use sparingly. The note should be mostly prose; callouts highlight the critical bits.

| Callout | Use for |
|---------|---------|
| `[!bug]` | Problems, errors, symptoms |
| `[!success]` | Solutions, fixes, what worked |
| `[!tip]` | Helpful hints, shortcuts |
| `[!warning]` | Dangers, things that can break |
| `[!info]` | Background context |
| `[!note]` | General annotations |
| `[!example]` | Practical examples, sample commands |
| `[!question]` | Open questions, things to investigate |

### Callout syntax

```markdown
> [!tip] Optional title
> Callout content goes here.
> Can span multiple lines.
```

## Sync target

The note skill writes to `~/notes/` and syncs to the user's Obsidian vault inbox. The exact sync command depends on how the user's Obsidian vault is wired.

Common shapes (pick what matches the user's setup, document it in `TOOLS.md`):

```bash
# Google Drive via rclone (most common):
rclone copy ~/notes/<topic-slug>.md "gdrive:My Drive/<VaultPath>/<Inbox-folder>/"

# Local Obsidian vault on disk:
cp ~/notes/<topic-slug>.md ~/Obsidian/<Vault>/<Inbox-folder>/

# Bisync timer (already running): just write to ~/notes/ and the next sync fires
```

If the user has not configured a sync target, leave the file in `~/notes/` and tell them where it landed.

## Quality rules

- Prose is the backbone. Headings, paragraphs, lists, code blocks.
- Callouts highlight the critical bits (key fix, warning, gotcha). They are not paragraph wrappers.
- A good note is mostly regular text with callouts around the parts that matter.
- Include specific commands, paths, config values, error messages. Vague notes are useless three weeks later.
- Code blocks use language tags for syntax highlighting.
- Reference-friendly, not narrative.
- Write for future-you who has zero context about this session.
- No em dashes. No AI-attribution trailers. Match the user's voice rules from `SOUL.md`.

## Output

Tell the user:

```text
Wrote note: ~/notes/<topic-slug>.md
Synced to: <sync target>
```

If sync failed, surface the error and leave the file in `~/notes/`.
