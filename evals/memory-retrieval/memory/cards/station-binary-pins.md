---
title: Station Binary Pins
tags: [stations, pins, setup]
description: Component manifests pin station binaries by digest for reproducible setup.
---

# Station Binary Pins

`brigade setup` materializes pinned engines from the component manifest. Drift between declared digest and on-disk binary is a doctor FAIL. Do not casually retarget pins without regenerating provenance.
