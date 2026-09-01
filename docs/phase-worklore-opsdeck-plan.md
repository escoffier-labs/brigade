# Worklore Ops Deck implementation plan

## Goal

Add the Ops Deck API proxy and the Tasks page Burn Queue, Work Board, and
Sprint Log so an operator can create, edit, defer, and archive native
Worklore items without sending a Fleet Hub credential to the browser.
Execute the tasks in order and check every box.

Contract: Ops Deck talks to Fleet Hub with an enrolled operator node
token, never the shared Hub admin token. Hub authorization is the
Worklore-only operator role from `fleet.worklore.operator_nodes` (optional
bounded override `BRIGADE_WORKLORE_OPERATOR_NODES`). The admin token
remains a backwards-compatible Hub override and is not required or
deployed for Ops Deck. Native `POST /work/items` accepts `Idempotency-Key`.

Approved design: `docs/phase-worklore-ledger.md`.

This plan is independently executable in the `opsdeck-api` and `opsdeck`
repositories. Those checkouts are not siblings of every Brigade worktree.
Resolve them in this order and stop with a missing-checkout error if none
exist:

1. `WORKLORE_OPSDECK_API` and `WORKLORE_OPSDECK` when both are set and
   are directories
2. Peer directories named `opsdeck-api` and `opsdeck` next to
   `git rev-parse --show-toplevel`
3. Peer directories next to the parent of
   `git rev-parse --git-common-dir`

Do not search `$HOME`. Tests fake Fleet Hub HTTP. They do not edit
Brigade source. Read those repos' `AGENTS.md` files before any edit.
`opsdeck-api` is a single-file Express 5 server plus extracted modules
such as `auth.js`. `opsdeck` is the React 19 SPA. Commits in those repos
use conventional messages and never add `Co-Authored-By` trailers
(`opsdeck-api/AGENTS.md:36`, `opsdeck/AGENTS.md:46`).

## Architecture

- `opsdeck-api/worklore.js` validates browser input, forwards `/api/worklore`
  to Fleet Hub `/work`, and keeps a last-known-good read snapshot. It
  holds no canonical Worklore rows.
- `opsdeck-api/atomic-write.js` exports `atomicWriteFile` and
  `atomicWriteJson`. `server.js` today keeps those functions module-local
  at `server.js:188-197` and has no exports. Move them to the shared
  module and import them from `server.js` and `worklore.js`.
- `opsdeck-api/auth.js` gains a protected GET prefix for `/api/worklore`.
- `opsdeck-api/server.js` mounts the routes and leaves `/api/backlog` and
  `/api/tasks` in place.
- `opsdeck/src/lib/worklore.ts` owns types and view helpers.
- `opsdeck/src/pages/Tasks.tsx` adds three Worklore views behind
  `VITE_WORKLORE_ENABLED=1` and keeps the current sprint and portfolio
  views.

## File map

| File | Responsibility |
| --- | --- |
| Create `opsdeck-api/atomic-write.js` | Shared `atomicWriteFile` / `atomicWriteJson`. |
| Create `opsdeck-api/worklore.js` | Proxy, snapshot, input bounds, stable errors. |
| Create `opsdeck-api/worklore.test.js` | Unit tests with a fake hub. No live :8005. |
| Modify `opsdeck-api/server.js` | Import atomic-write, mount `/api/worklore`, keep backlog routes. |
| Modify `opsdeck-api/auth.js` | Require `X-API-Key` on `/api/worklore` GET. |
| Modify `opsdeck-api/auth.test.js` | GET `/api/worklore` requires the key. |
| Modify `opsdeck-api/server.test.js` | Spawned-server integration with an in-process fake hub. |
| Modify `opsdeck-api/package.json` | Add `worklore.test.js` to `npm test`. |
| Create `opsdeck/src/lib/worklore.ts` | Types, board columns, burn sort display. |
| Create `opsdeck/src/lib/__tests__/worklore.test.ts` | Pure helper tests. |
| Modify `opsdeck/src/pages/Tasks.tsx` | Burn Queue, Work Board, Sprint Log, native editor. |
| Create `opsdeck/src/pages/__tests__/Tasks.test.tsx` | View rendering and stale-mutation guards. |
| Modify `opsdeck/src/hooks/useApi.ts` | `apiFetch` keeps the JSON `code` on thrown errors. |

## Pinned decisions

- Feature flags: API routes exist only when `process.env.WORKLORE_ENABLED === '1'`.
  UI Worklore views exist only when `import.meta.env.VITE_WORKLORE_ENABLED === '1'`.
  Default is off.
- Hub settings: `FLEET_HUB_URL` and an enrolled operator node token
  (`FLEET_HUB_NODE_TOKEN` or the equivalent node-token setting) are
  environment variables on the API process. The browser never receives
  them. Do not set or deploy `FLEET_HUB_ADMIN_TOKEN` for Ops Deck.
  `useApi.ts` continues to send `X-API-Key` from `src/lib/apiKey.ts`.
- Auth: add `PROTECTED_GET_PREFIXES = ['/api/worklore']` to `auth.js`.
  `requiresApiKey` returns true for every `/api/worklore` path, including
  GET. POST and PATCH already require the key. Missing `OPSDECK_API_KEY`
  still returns 503 `{ error: "API key is not configured" }`. Current
  `auth.js:1-9` leaves ordinary GET open, so a GET-open assertion would
  pass today. The new prefix is what makes the red test real.
- Proxy timeout is 3000 ms. Connection failure, DNS failure, and timeout
  map to HTTP 503 `{ error: "hub-unavailable", code: "hub-unavailable" }`
  with no hub URL or token. Upstream 4xx JSON `code` is forwarded when it
  is one of the core stable codes. Upstream bodies are otherwise dropped.
- Read snapshot path is `process.env.WORKLORE_SNAPSHOT_PATH` or
  `{XDG_CACHE_HOME || "$HOME/.cache"}/opsdeck-api/worklore-snapshot.json`.
  Create the snapshot directory first with
  `mkdir(parent, { recursive: true, mode: 0o700 })`, then `chmod` that
  parent to `0o700` so an already-existing directory is not left at the
  umask default. `atomicWriteFile` writes `${filePath}.tmp` through
  `writeFile` with `{ mode: 0o600 }` and then renames. Do not rely on a
  post-rename `chmod 0600` as the only file-mode control. Successful GET `/work/items`, GET `/work/events`,
  and GET `/work/queue/burn` replace the snapshot. Failed GETs return the
  snapshot plus `{ stale: true, source: "snapshot" }`. Failed POST or
  PATCH never return 200 from a snapshot.
- Browser create body may include `title`, `description`, `kind`,
  `scope`, `priority`, `burn_eligible`, `burn_rank`, `token_appetite`,
  `execution_mode`, `acceptance`, `blocker`, `review_after`, and
  `spend_by` only. Unknown fields return 400 `{ code: "unknown-field" }`
  without calling the hub. `acceptance` is the operator field and stays
  `acceptance` on the Fleet Hub HTTP API. Fleet Hub maps it internally to
  the private `work_items.acceptance_json` storage column.
- Title 1-240 characters. Description max 8000. Acceptance 0-20 strings
  of max 400 characters. The API does not invent acceptance for the
  operator.
- Board columns: `captured`, `defining`, `ready`, `claimed`, `running`,
  `verifying`, `deferred`, `blocked`, `completed`, `canceled`. Archived
  items stay behind the existing archive toggle.
- `/api/backlog` and the sprint view remain until native records are
  imported and the Worklore views have passed the tests in this plan.
  Do not delete `tracking/backlog.json` handling in this slice.
- CORS already allows `X-API-Key`. In the same `server.js` edit, add
  `PATCH` to `Access-Control-Allow-Methods` at `server.js:63` and
  `If-Match` to `Access-Control-Allow-Headers` at `server.js:64`.
- `apiFetch` in `opsdeck/src/hooks/useApi.ts` currently throws
  `Error(\`${r.status} ${r.statusText}\`)` and discards the body. Change
  it so a JSON error body with a stable `code` is preserved on the thrown
  Error (message includes `code`; keep `${status} ${statusText}` only as
  fallback). Mutations keep using `apiFetch`.
- `BIND_HOST=0.0.0.0` behavior stays. Do not hardcode LAN hosts in the
  SPA. Keep `window.location.hostname` from `src/hooks/useApi.ts`.
- Verification for `opsdeck-api` unit tests does not need a process on
  port 8005. Integration tests spawn `server.js` the way
  `startCronTestServer` does in `server.test.js:53-103`. That helper
  cannot receive a JavaScript `hubHandler`. Stand up
  `http.createServer` in the parent process and pass its URL as
  `FLEET_HUB_URL` in the child env.
- `isWorkloreEnabled(flag)` takes an optional string. When omitted it
  reads `import.meta.env.VITE_WORKLORE_ENABLED`.
- Verification for `opsdeck` is `./scripts/verify` from that repo root.

## Task 1: API proxy and snapshot

**Working directory:** `opsdeck-api` repo root

**Files:**

- Create: `atomic-write.js`
- Create: `worklore.js`
- Create: `worklore.test.js`
- Modify: `package.json` `scripts.test`

- [x] Write the failing tests

```javascript
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createWorklore } from './worklore.js';

function fakeHub(handler) {
  return {
    async request(method, path, { body, headers } = {}) {
      return handler(method, path, body, headers);
    },
  };
}

test('create rejects unknown fields before calling the hub', async () => {
  const calls = [];
  const worklore = createWorklore({
    hub: fakeHub((method, path, body) => {
      calls.push({ method, path, body });
      return { status: 201, json: { item: { work_id: 'wl-1' } } };
    }),
    snapshotPath: join(await mkdtemp(join(tmpdir(), 'wl-')), 'snap.json'),
  });
  const result = await worklore.createItem({ title: 'x', kind: 'fleet', prompt: 'nope' });
  assert.equal(result.status, 400);
  assert.equal(result.json.code, 'unknown-field');
  assert.equal(calls.length, 0);
});

test('GET uses snapshot when the hub is down and marks it stale', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'wl-'));
  const snapshotPath = join(dir, 'snap.json');
  const live = createWorklore({
    hub: fakeHub(() => ({ status: 200, json: { items: [{ work_id: 'wl-1', title: 'Live' }] } })),
    snapshotPath,
  });
  await live.listItems();
  const snapshotStat = await stat(snapshotPath);
  assert.equal(snapshotStat.mode & 0o777, 0o600);
  const parentStat = await stat(dir);
  assert.equal(parentStat.mode & 0o777, 0o700);
  const down = createWorklore({
    hub: fakeHub(() => { throw new Error('connect ECONNREFUSED'); }),
    snapshotPath,
  });
  const result = await down.listItems();
  assert.equal(result.status, 200);
  assert.equal(result.json.stale, true);
  assert.equal(result.json.source, 'snapshot');
  assert.equal(result.json.items[0].work_id, 'wl-1');
});

test('POST does not report snapshot success when the hub is down', async () => {
  const worklore = createWorklore({
    hub: fakeHub(() => { throw new Error('connect ECONNREFUSED'); }),
    snapshotPath: join(await mkdtemp(join(tmpdir(), 'wl-')), 'snap.json'),
  });
  const result = await worklore.createItem({ title: 'Native', kind: 'fleet' });
  assert.equal(result.status, 503);
  assert.equal(result.json.code, 'hub-unavailable');
  assert.equal(result.json.error, 'hub-unavailable');
  assert.equal(result.json.item, undefined);
});
```

- [x] Run red from the `opsdeck-api` repo root:

```bash
brigade work verify run --target . --command "node --test worklore.test.js" --capture brigade-work
```

Expect FAIL: `Cannot find module './worklore.js'`.

- [x] Implement `createWorklore({ hub, snapshotPath })` with
  `listItems`, `getItem`, `listEvents`, `burnQueue`, `createItem`,
  `patchItem`, `transition`, and `recordAttempt`. Hub GET paths are
  `/work/items`, `/work/items/{work_id}`, `/work/items/{work_id}/events`,
  `/work/events`, and `/work/queue/burn`. Mutations use the server-held
  operator node token inside the `hub` adapter, send `Idempotency-Key` on
  native create, and forward `If-Match` on PATCH, transition, and attempt.
  Snapshot writes mkdir the parent with
  `{ recursive: true, mode: 0o700 }`, `chmod` that parent to `0o700`,
  then call `atomicWriteJson` from `./atomic-write.js`.
  `atomicWriteFile` passes `{ mode: 0o600 }` to `writeFile` for the
  `.tmp` file before rename. Errors never include `FLEET_HUB_URL` or
  the token.

- [x] Add `worklore.test.js` to the `package.json` `test` script list.

- [x] Run green with the same Brigade command. Expect `passed`.

- [x] Commit in `opsdeck-api` only:

```bash
git add atomic-write.js worklore.js worklore.test.js package.json
git commit -m "feat: add Worklore Fleet Hub proxy and snapshot"
```

  Do not add a co-author trailer.

## Task 2: mount routes and keep backlog

**Working directory:** `opsdeck-api` repo root

**Files:**

- Modify: `server.js` around the `/api/tasks` and `/api/backlog` block at
  lines 2551-2698
- Modify: `server.js:64` CORS headers
- Modify: `server.js` to import `atomicWriteJson` from `./atomic-write.js`
- Modify: `auth.js`
- Modify: `auth.test.js`
- Modify: `server.test.js`

- [x] Write the failing tests

```javascript
test('worklore GET and writes require an API key', async () => {
  const { requiresApiKey } = await import('./auth.js');
  assert.equal(requiresApiKey('GET', '/api/worklore/items'), true);
  assert.equal(requiresApiKey('GET', '/api/worklore/events'), true);
  assert.equal(requiresApiKey('POST', '/api/worklore/items'), true);
  assert.equal(requiresApiKey('PATCH', '/api/worklore/items/wl-1'), true);
});
```

Add to `auth.test.js` using the existing import style, not a second
import mechanism.

Add a spawned-server test to `server.test.js`. Create the fake hub with
`http.createServer` in the parent, then spawn `server.js` with
`FLEET_HUB_URL` pointing at that listener:

```javascript
import http from 'node:http';

async function startFakeHub(handler) {
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on('data', (chunk) => chunks.push(chunk));
    req.on('end', () => {
      const raw = Buffer.concat(chunks).toString('utf8');
      const body = raw ? JSON.parse(raw) : undefined;
      const result = handler(req.method, req.url, body, req.headers);
      res.writeHead(result.status, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(result.json));
    });
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address();
  return {
    url: `http://127.0.0.1:${port}`,
    close: () => new Promise((resolve) => server.close(resolve)),
  };
}

async function startWorkloreTestServer({ enabled, hubHandler }) {
  const hub = hubHandler ? await startFakeHub(hubHandler) : null;
  const port = 20000 + Math.floor(Math.random() * 20000);
  const child = spawn(process.execPath, ['server.js'], {
    cwd: process.cwd(),
    env: {
      ...process.env,
      BIND_HOST: '127.0.0.1',
      PORT: String(port),
      WORKLORE_ENABLED: enabled ? '1' : '0',
      FLEET_HUB_URL: hub ? hub.url : '',
      FLEET_HUB_NODE_TOKEN: 'test-node-token',
      OPSDECK_API_KEY: 'test-api-key',
    },
    stdio: ['ignore', 'ignore', 'pipe'],
  });
  const baseUrl = `http://127.0.0.1:${port}`;
  for (let attempt = 0; attempt < 50; attempt++) {
    try {
      const response = await fetch(`${baseUrl}/api/health`);
      if (response.ok) break;
    } catch {}
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  return {
    apiKey: 'test-api-key',
    hubToken: 'test-hub-token',
    async fetch(pathname, options = {}) {
      const res = await fetch(`${baseUrl}${pathname}`, options);
      const text = await res.text();
      const contentType = res.headers.get('content-type') || '';
      const json = contentType.includes('application/json') && text
        ? JSON.parse(text)
        : null;
      return { res, json };
    },
    async stop() {
      child.kill('SIGTERM');
      if (hub) await hub.close();
    },
  };
}

test('worklore routes are absent until WORKLORE_ENABLED=1', async () => {
  const server = await startWorkloreTestServer({ enabled: false });
  try {
    const { res } = await server.fetch('/api/worklore/items', {
      headers: { 'X-API-Key': server.apiKey },
    });
    assert.equal(res.status, 404);
    const backlog = await server.fetch('/api/backlog');
    assert.equal(backlog.res.status, 200);
  } finally {
    await server.stop();
  }
});

test('enabled worklore create forwards If-Match on patch and transition', async () => {
  const hubCalls = [];
  const server = await startWorkloreTestServer({
    enabled: true,
    hubHandler: (method, path, body, headers) => {
      hubCalls.push({ method, path, body, headers });
      if (method === 'POST' && path === '/work/items') {
        return { status: 201, json: { item: { work_id: 'wl-1', version: 1 } } };
      }
      return { status: 200, json: { item: { work_id: 'wl-1', version: 2 } } };
    },
  });
  try {
    const created = await server.fetch('/api/worklore/items', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-API-Key': server.apiKey },
      body: JSON.stringify({ title: 'NAS restic', kind: 'fleet' }),
    });
    assert.equal(created.res.status, 201);
    await server.fetch('/api/worklore/items/wl-1', {
      method: 'PATCH',
      headers: {
        'Content-Type': 'application/json',
        'X-API-Key': server.apiKey,
        'If-Match': '1',
      },
      body: JSON.stringify({ burn_eligible: true }),
    });
    assert.equal(hubCalls[1].headers['if-match'] || hubCalls[1].headers['If-Match'], '1');
    assert.equal(String(hubCalls[1].headers.authorization).startsWith('Bearer '), true);
    assert.equal(JSON.stringify(created.json).includes(server.hubToken), false);
  } finally {
    await server.stop();
  }
});
```

- [x] Run red:

```bash
brigade work verify run --target . --command "node --test auth.test.js worklore.test.js" --capture brigade-work
```

Expect FAIL on the new `/api/worklore` assertions.

- [x] In `auth.js`, treat any path equal to `/api/worklore` or starting
  with `/api/worklore/` as a protected GET prefix.
- [x] In `server.js`, when `WORKLORE_ENABLED === '1'`, construct the hub
  adapter with `FLEET_HUB_URL` and the enrolled operator node token and mount:

  - `GET /api/worklore/items`
  - `GET /api/worklore/items/:work_id`
  - `GET /api/worklore/items/:work_id/events`
  - `GET /api/worklore/events`
  - `GET /api/worklore/queue/burn`
  - `POST /api/worklore/items`
  - `PATCH /api/worklore/items/:work_id`
  - `POST /api/worklore/items/:work_id/transitions`
  - `POST /api/worklore/items/:work_id/attempts`

  Leave `GET /api/backlog` and `PATCH /api/backlog/:id` unchanged.
- [x] In the same CORS edit, add `PATCH` to
  `Access-Control-Allow-Methods` at `server.js:63` and `If-Match` to
  `Access-Control-Allow-Headers` at `server.js:64`.
- [x] `startWorkloreTestServer` listens on 127.0.0.1 and a random port.
  It must not change default `BIND_HOST=0.0.0.0` for production.

- [x] Run the unit tests green, then the package script:

```bash
brigade work verify run --target . --command "npm test" --capture brigade-work
```

`npm test` still needs a live server for older `server.test.js` cases
that hit `OPSDECK_API_URL`. Spawned Worklore tests do not. Report the
actual npm result. If older live-server tests fail because :8005 is
down, keep the spawned Worklore tests green and record that existing
live-server dependency.

- [x] Commit in `opsdeck-api`:

```bash
git add server.js auth.js auth.test.js server.test.js worklore.js atomic-write.js
git commit -m "feat: mount Worklore proxy routes beside the legacy backlog"
```

  No co-author trailer.

## Task 3: SPA helpers

**Working directory:** `opsdeck` repo root

**Files:**

- Create: `src/lib/worklore.ts`
- Create: `src/lib/__tests__/worklore.test.ts`

- [x] Write the failing tests

```typescript
import { describe, expect, it } from 'vitest';
import { boardColumn, burnLabel, isWorkloreEnabled } from '../worklore';

describe('worklore helpers', () => {
  it('maps statuses to board columns and leaves archived hidden', () => {
    expect(boardColumn('ready')).toBe('ready');
    expect(boardColumn('archived')).toBe(null);
  });

  it('labels burn appetite and attempt count', () => {
    expect(burnLabel({ token_appetite: 'large', attempt_count: 1 })).toBe('large · 1 attempt');
  });

  it('is off unless the Vite flag is the string 1', () => {
    expect(isWorkloreEnabled(undefined)).toBe(false);
    expect(isWorkloreEnabled('1')).toBe(true);
    expect(isWorkloreEnabled()).toBe(false);
  });
});
```

- [x] Run red:

```bash
brigade work verify run --target . --command "npx vitest --run src/lib/__tests__/worklore.test.ts" --capture brigade-work
```

Expect FAIL: `Failed to resolve import "../worklore"`.

- [x] Implement `WorkloreItem`, `WorkloreBurnQueue`, `WorkloreEvent`,
  `boardColumn`, `burnLabel`, and `isWorkloreEnabled`.
  `isWorkloreEnabled(flag?: string)` uses `flag` when passed and
  `import.meta.env.VITE_WORKLORE_ENABLED` when omitted. `boardColumn`
  returns the status string for the ten live columns and `null` for
  `archived`.

- [x] Run green with the same command. Expect `passed`.

- [x] Commit in `opsdeck`:

```bash
git add src/lib/worklore.ts src/lib/__tests__/worklore.test.ts
git commit -m "feat: add Worklore view helpers"
```

  No co-author trailer.

## Task 4: Tasks page views and native editor

**Working directory:** `opsdeck` repo root

**Files:**

- Modify: `src/pages/Tasks.tsx`
- Create: `src/pages/__tests__/Tasks.test.tsx`
- Modify: `src/hooks/useApi.ts`

- [x] Write the failing tests. Follow `src/pages/__tests__/Usage.test.tsx`
  and mock `useApi` plus `apiFetch`.

```tsx
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import Tasks from '../Tasks';

const useApiMock = vi.fn();
const apiFetchMock = vi.fn();

vi.mock('../../hooks/useApi', () => ({
  useApi: (path: string) => useApiMock(path),
  apiFetch: (...args: unknown[]) => apiFetchMock(...args),
}));

vi.mock('../../lib/worklore', async () => {
  const actual = await vi.importActual<typeof import('../../lib/worklore')>('../../lib/worklore');
  return { ...actual, isWorkloreEnabled: () => true };
});

describe('Tasks Worklore views', () => {
  beforeEach(() => {
    useApiMock.mockImplementation((path: string) => {
      if (path === '/api/worklore/queue/burn') {
        return {
          data: {
            items: [{ work_id: 'wl-1', title: 'NAS restic', token_appetite: 'small', attempt_count: 0, kind: 'fleet' }],
            exclusions: { 'acceptance-required': 1 },
            stale: false,
          },
          loading: false,
          error: null,
          refetch: vi.fn(),
        };
      }
      if (path === '/api/worklore/items') {
        return {
          data: { items: [{ work_id: 'wl-1', title: 'NAS restic', status: 'ready' }], stale: false },
          loading: false,
          error: null,
          refetch: vi.fn(),
        };
      }
      if (path === '/api/worklore/events') {
        return {
          data: {
            events: [{ event_id: 'evt-1', work_id: 'wl-1', event_type: 'created', actor_type: 'operator' }],
            stale: false,
          },
          loading: false,
          error: null,
          refetch: vi.fn(),
        };
      }
      if (path === '/api/backlog') {
        return { data: { meta: { lastUpdated: '2026-04-20', sprintName: 'Legacy' }, tasks: [] }, loading: false, error: null, refetch: vi.fn() };
      }
      if (path === '/api/tasks') {
        return { data: { projects: [] }, loading: false, error: null, refetch: vi.fn() };
      }
      return { data: null, loading: false, error: null, refetch: vi.fn() };
    });
  });

  it('shows burn queue items and the exclusion summary', async () => {
    render(<Tasks />);
    await userEvent.click(screen.getByRole('button', { name: 'Burn Queue' }));
    expect(screen.getByText('NAS restic')).toBeInTheDocument();
    expect(screen.getByText(/acceptance-required: 1/)).toBeInTheDocument();
  });

  it('keeps the legacy sprint toggle available', async () => {
    render(<Tasks />);
    expect(screen.getByRole('button', { name: 'Sprint' })).toBeInTheDocument();
  });

  it('renders the sprint log from the fleet-wide events route', async () => {
    render(<Tasks />);
    await userEvent.click(screen.getByRole('button', { name: 'Sprint Log' }));
    expect(screen.getByText(/created/)).toBeInTheDocument();
  });

  it('does not treat a failed create as success', async () => {
    apiFetchMock.mockRejectedValue(new Error('503 hub-unavailable'));
    render(<Tasks />);
    await userEvent.click(screen.getByRole('button', { name: 'New work' }));
    await userEvent.type(screen.getByLabelText('Title'), 'Native item');
    await userEvent.click(screen.getByRole('button', { name: 'Create' }));
    expect(screen.getByText(/hub-unavailable/)).toBeInTheDocument();
    expect(screen.queryByText('Created wl-')).not.toBeInTheDocument();
  });
});
```

- [x] Run red:

```bash
brigade work verify run --target . --command "npx vitest --run src/pages/__tests__/Tasks.test.tsx" --capture brigade-work
```

Expect FAIL on missing Burn Queue controls.

- [x] Extend `Tasks.tsx`. When `isWorkloreEnabled()` is true, add view
  buttons `Burn Queue`, `Work Board`, and `Sprint Log` beside Sprint and
  Portfolio (`Tasks.tsx:214-235`). Burn Queue lists eligible items with
  token appetite, attempt count, kind, and exclusion counts. Work Board
  renders the ten status columns. Sprint Log lists
  `GET /api/worklore/events`. Do not fan out one request per item.
  Native editor uses `apiFetch` for POST and PATCH and sends `If-Match`
  from `item.version` on PATCH, transition, and attempt. In
  `useApi.ts:64`, parse a JSON error body and throw an Error whose
  message includes the stable `code` when present. Keep
  `${status} ${statusText}` only when `code` is absent. Titles render
  as text, not HTML. Links use `rel="noopener noreferrer"` and `https`
  only.
- [x] Keep the stale `tracking/backlog.json` sprint view and its 30-day
  warning.

- [x] Run the page test green, then the repo gate:

```bash
brigade work verify run --target . --argv-json '["./scripts/verify"]' --capture brigade-work
```

Expect `passed` from `./scripts/verify` (tsc, eslint, vitest).

- [x] Commit in `opsdeck`:

```bash
git add src/pages/Tasks.tsx src/pages/__tests__/Tasks.test.tsx src/hooks/useApi.ts
git commit -m "feat: add Worklore burn board and sprint log to Tasks"
```

  No co-author trailer.

## Verification

`opsdeck-api` tasks run `node --test worklore.test.js` and then `npm test`
through Brigade with `--target .` at that repo root. `opsdeck` tasks run
the named vitest file, then `./scripts/verify` through Brigade with
`--target .` at that repo root. Do not claim the SPA works from a
screenshot. Do not point the proxy at a personal home path in fixtures.
Use `tmpdir()` and `wl-1` ids.
