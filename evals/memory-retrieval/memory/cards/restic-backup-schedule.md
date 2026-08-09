---
title: Restic Snapshot Cadence
tags: [backup, restic, ops]
description: Nightly restic snapshots with weekly prune and monthly offsite copy.
---

# Restic Snapshot Cadence

Run restic backup nightly against the operator workspace exclude list. Prune weekly with a keep-daily/weekly/monthly policy. Copy one restic snapshot per month to the offsite bucket; verify restore quarterly.
