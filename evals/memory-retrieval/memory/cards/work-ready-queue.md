---
title: Work Ready Queue Semantics
tags: [work, inbox, dependencies]
description: Ready tasks are pending items with no open readiness blockers on typed edges.
---

# Work Ready Queue Semantics

Only `blocks` edges gate readiness; `discovered-from` is provenance. Parent-child blockers propagate. `brigade work ready` returns the computable set; cycles on readiness-affecting edges are rejected at write time.
