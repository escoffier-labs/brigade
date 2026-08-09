---
title: Version Sync Triple
tags: [release, version, templates]
description: Bumps must update pyproject, package __init__, and every template _brigade_version together.
---

# Version Sync Triple

`./scripts/verify` runs the sync check. A lone pyproject bump will fail CI even if tests pass. Template fields that advertise the installed Brigade version must move in lockstep.
