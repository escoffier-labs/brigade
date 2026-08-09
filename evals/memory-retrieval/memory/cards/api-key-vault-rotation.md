---
title: API Key Vault Rotation
tags: [secrets, vault, credentials]
description: Rotate service keys in the vault on a fixed cadence with dual-key overlap.
---

# API Key Vault Rotation

Service callers keep two active keys for a grace window. Cut traffic to the new key, then retire the old one. Never embed keys in cards or handoffs; reference the vault path only.
