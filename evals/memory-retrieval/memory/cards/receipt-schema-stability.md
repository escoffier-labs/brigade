---
title: Receipt Schema Stability
tags: [receipts, schema, compatibility]
description: Machine receipts keep explicit schema versions; readers must tolerate older shapes.
---

# Receipt Schema Stability

Bump the schema version when fields change meaning. Closeout and run artifacts are replayed months later; silent field renames break audits. Prefer additive fields over renames.
