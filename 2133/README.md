# PR 2133 terminal captures

Before: main commit 3b347c20e3547c5bf2b113904af63045ad4017c6.
After: PR head 2e6c7d44be45055cddf0a286e1a3d017a4fb3be3.

Real xfce4-terminal captures on isolated Xvfb displays, recorded with scrot.
Viewport: 1440 by 900. Font: DejaVu Sans Mono 12. Same terminal geometry and colors for each view. No compositing or generated terminal images.

Commands run from each actual checkout:

- python -m brigade fleet msg principals grants --help
- python -m brigade fleet msg principals show --help

The grants help adds --node and makes the name positional optional. The show help transcripts are byte-identical. Commands read help only and use no Hub credentials or live sessions.

Brigade capture receipt: 20261029-084859-work-verify-8be250, exit 0. Capture is audit-only. Each image was inspected for credentials, personal data and private infrastructure before publication.

Image SHA-256:

- grants-after.png: 5c207c8679bc2e38a5f5cb19691ef4cd2a671a25705a11330111ed41176e2c11
- grants-before.png: a499079c913680ee844c4be07582973927f3c453ae4ea278dbcf0c3a03ac2fe3
- show-after.png: 7cc27bb517da73726c6763485d35679bf247cbcfa5b7f421afe147a7efb3c9f8
- show-before.png: b6d216fd00caee5e96863d3aa245a17aa6ac21d813024f987f03c0177a8d04d5
