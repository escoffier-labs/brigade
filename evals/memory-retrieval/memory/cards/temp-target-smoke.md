---
title: Temp Target Smoke Rule
tags: [dev, smoke, safety]
description: Never run init, ingest, operator, or quickstart against the real operator home during development.
---

# Temp Target Smoke Rule

Use `mktemp -d` or the `tmp_target` fixture. Exercising those commands on the checkout or home directory pollutes live memory and can rewrite real harness configs.
