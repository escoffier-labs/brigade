---
title: Cold Start Probe Protocol
tags: [lanes, probes, models]
description: New CLI lanes prove they can write a file in cwd; never trust reply text alone.
---

# Cold Start Probe Protocol

When onboarding a model seat, the probe asks for a concrete file write under the workspace. DNF versus worker-fail is scored from the filesystem and exit code, not from the model claiming success in prose.
