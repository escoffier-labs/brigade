---
title: Roster Worktree Fallback
tags: [roster, worktree, seats]
description: Linked worktrees inherit the parent clone roster before the user roster.
---

# Roster Worktree Fallback

A fresh worktree without its own `.brigade/roster.toml` resolves seats from the parent clone (`worktree-parent` source), then `~/.brigade/roster.toml`. Scaffolding a starter roster would shadow that chain; refuse unless `--force`.
