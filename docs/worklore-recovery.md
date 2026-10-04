# Reading Worklore history for recovery

The supported Python client can read one bounded page of existing Worklore
history and resume a traversal using the Hub's returned opaque cursor. This is
a client prerequisite for replacement owners in #1595. The ownership protocol
remains unfinished.

Use the existing fleet configuration for the Hub URL and authentication. Reads
prefer the configured node token and fall back to the admin token only when no
node token exists. A refused node request does not retry with admin authority.
The Hub's existing authorization and Worklore availability checks still apply.

```python
from brigade import worklore_client

work_id = "wl-example"
first = worklore_client.list_events(work_id, limit=25)
events = first["events"]
cursor = first["next_cursor"]

# A later caller can reuse the cursor with the same work item.
if cursor is not None:
    second = worklore_client.list_events(work_id, limit=25, cursor=cursor)
    events.extend(second["events"])
```

`list_events(work_id, *, limit=None, cursor=None)` uses the existing
`/work/items/{work_id}/events` endpoint. Item history follows insertion sequence
in ascending order. `list_all_events(*, work_id=None, event_type=None,
limit=None, cursor=None)` uses `/work/events`. Global history is newest first
by `occurred_at`, then insertion sequence. Global reads can filter by work item,
event type, or both. The global cursor does not encode or enforce filter
identity. Callers must preserve the original filters when following a returned
cursor.
Filtered pages can contain fewer events than the limit, including none, while
still returning a cursor for remaining candidates. Follow `next_cursor` to
determine whether the traversal continues.

Each call makes one request and returns the existing `events` and `next_cursor`
response without rewriting event states, identifiers, or cursors. Omitted
arguments retain the existing query-free calls. The Hub defaults to 50 events
and accepts limits from 1 to 100. The client forwards supplied limits, including
zero, for server validation. Invalid cursors or limits and authorization
refusals propagate through `WorkloreClientError`, retaining the server error
code when supplied.

These cursors describe history pages. They are not durable forward subscription
watermarks. `next_cursor` being null means this traversal has reached its end.
Future events can still arrive. Concurrent mutation has no snapshot isolation
guarantee. The client does not persist cursors, retry requests, or automatically
fetch more pages. A resumed caller still needs valid authority and must retain
the cursor and request context itself.

This slice only reads history. It does not transfer ownership, grant authority,
resolve competing owners, fence a previous owner, or infer current work status
from historical events. Those recovery and ownership rules remain work for
#1595.
