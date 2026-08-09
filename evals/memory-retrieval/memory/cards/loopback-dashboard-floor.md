---
title: Loopback Dashboard Floor
tags: [dashboard, security, ui]
description: First UI slice is read-only, loopback-bound, CSP nonced, Host-header allowlisted.
---

# Loopback Dashboard Floor

The operator dashboard renders existing `--json` contracts only. Refuse bind beyond loopback without a token plus an explicit host list. No inline script handlers.
