---
title: Command Inventory Drift
tags: [docs, inventory, roadmap]
description: Regenerate docs/command-inventory.md from the parser when the public surface changes.
---

# Command Inventory Drift

`brigade roadmap commands --write` regenerates; `--check` fails on stale inventory. Enable extras when regenerating so gated commands appear. No surface change means no inventory churn.
