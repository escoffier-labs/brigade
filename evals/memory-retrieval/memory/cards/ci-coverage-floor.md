---
title: CI Coverage Floor
tags: [ci, pytest, coverage]
description: verify runs pytest with a coverage floor; do not lower the floor to land a feature.
---

# CI Coverage Floor

Failed tests after a change mean fix the code. Weakening, skipping, or deleting tests to go green is forbidden. If a test itself is wrong, say so explicitly before touching it.
