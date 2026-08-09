---
title: OAuth Refresh Rotation
tags: [oauth, credentials, session]
description: Sliding refresh credentials replace expired access grants without a full login.
---

# OAuth Refresh Rotation

Access grants are short-lived. Before they go stale, the client exchanges a longer-lived refresh credential for a new access grant and a rotated refresh credential. Store only the latest refresh credential; reuse of a prior one is treated as theft and revokes the chain.
