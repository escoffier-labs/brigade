# Projects board

`/deck/projects` shows one stored GitHub Projects (v2) board: epics, a status
board, and a list. It is read-only. Nothing on the page, and nothing in the sync
command, writes to GitHub. The board is private, so the page follows the other
Deck pages and requires an authenticated Hub identity.

## Populate from a node

The sync runs through the node's own `gh`, which needs the `read:project` scope
(`gh auth refresh -s read:project`). A missing scope is reported by name.

```sh
brigade fleet projects sync --owner escoffier-labs --number 1
brigade fleet projects sync --owner escoffier-labs --number 1 --dry-run --json
```

One page of 100 items costs one GraphQL rate-limit point. The query pages with
cursors and stops at 500 items, which marks the snapshot incomplete. A sync is
skipped when the last successful one was under five minutes ago (`--force`
overrides it; `--dry-run` never records one). The state file is
`BRIGADE_HOME/projects-board-state.json`.

The node posts with its node token to `POST /projects/board`. The Hub validates a
closed schema, rebuilds every link from the validated owner, repository and
number, rejects control characters and bad label colors, and keeps the last good
snapshot: a failed or invalid sync never replaces it, and an older observation
never replaces a newer one. The page shows the snapshot age.

## Scheduling

There is no new daemon. Run the command from an existing authorized care flow:
`brigade care install` (systemd user timers or launchd) or a crontab line from
[scheduled-care.md](scheduled-care.md). Every 10 minutes is about 6 rate-limit
points per hour.

## Page

Views are plain links (`?view=epics`, `?view=list`, `?epic=N`), so the full board
renders without JavaScript. Script adds search (`label:name` works) and collapses
the Done column to a count. An epic is an item with sub-issues; clicking one
filters the board to that epic and its children.
