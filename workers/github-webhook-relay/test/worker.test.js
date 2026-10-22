import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import test from "node:test";
import worker from "../src/index.js";

const secret = "test-webhook-secret";
const repoAllowlist = "example-org/brigade,example-user/tools";

class FakeD1 {
  rows = [];
  statements = [];

  prepare(sql) {
    const statement = {
      bind: (...values) => ({
        first: async () => {
          if (sql.startsWith("INSERT OR IGNORE")) {
            const [deliveryId, ...fields] = values;
            if (this.rows.some((row) => row.delivery_id === deliveryId)) return null;
            const row = Object.fromEntries(
              [
                "repository",
                "event",
                "action",
                "head_sha",
                "status",
                "conclusion",
                "run_id",
                "run_attempt",
                "pr_number",
                "provider_created_at",
                "provider_updated_at",
                "received_at",
              ].map((key, index) => [key, fields[index]]),
            );
            row.delivery_id = deliveryId;
            row.cursor = this.rows.length + 1;
            this.rows.push(row);
            return { cursor: row.cursor };
          }
          if (sql.startsWith("DELETE")) {
            const before = this.rows.length;
            this.rows = this.rows.filter((row) => row.received_at >= values[0]);
            return { meta: { changes: before - this.rows.length } };
          }
          return null;
        },
        all: async () => {
          const [after, limit] = values;
          return {
            results: this.rows
              .filter((row) => row.cursor > after)
              .sort((left, right) => left.cursor - right.cursor)
              .slice(0, limit),
          };
        },
        run: async () => {
          if (sql.startsWith("DELETE")) {
            const before = this.rows.length;
            this.rows = this.rows.filter((row) => row.received_at >= values[0]);
            return { meta: { changes: before - this.rows.length } };
          }
          return { success: true };
        },
      }),
    };
    this.statements.push({ sql, statement });
    return statement;
  }
}

function makeEnv(db = new FakeD1()) {
  return {
    DB: db,
    GITHUB_WEBHOOK_SECRET: secret,
    EVENTS_READ_TOKEN: "test-read-token",
    ALLOWED_REPOSITORIES: repoAllowlist,
  };
}

function signedRequest(payload, headers = {}, signingSecret = secret) {
  const body = JSON.stringify(payload);
  const signature = `sha256=${createHmac("sha256", signingSecret).update(body).digest("hex")}`;
  return new Request("https://relay.example/webhooks/github", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-github-delivery": "delivery-1",
      "x-github-event": "workflow_run",
      "x-hub-signature-256": signature,
      ...headers,
    },
    body,
  });
}

function runPayload(overrides = {}) {
  return {
    action: "completed",
    repository: { full_name: "example-org/brigade" },
    workflow_run: {
      head_sha: "a".repeat(40),
      status: "completed",
      conclusion: "success",
      id: 123,
      run_attempt: 2,
      created_at: "2026-10-21T00:00:00Z",
      updated_at: "2026-10-21T00:01:00Z",
    },
    ...overrides,
  };
}

test("accepts a signed event, normalizes fields, and omits the payload", async () => {
  const db = new FakeD1();
  const response = await worker.fetch(signedRequest(runPayload()), makeEnv(db));
  assert.equal(response.status, 202);
  assert.deepEqual(await response.json(), { accepted: true });
  assert.equal(db.rows.length, 1);
  assert.deepEqual({ ...db.rows[0], received_at: "timestamp" }, {
    cursor: 1,
    delivery_id: "delivery-1",
    repository: "example-org/brigade",
    event: "workflow_run",
    action: "completed",
    head_sha: "a".repeat(40),
    status: "completed",
    conclusion: "success",
    run_id: "123",
    run_attempt: 2,
    pr_number: null,
    provider_created_at: "2026-10-21T00:00:00Z",
    provider_updated_at: "2026-10-21T00:01:00Z",
    received_at: "timestamp",
  });
  assert.equal("payload" in db.rows[0], false);
});

test("rejects missing and invalid signatures with a constant response", async () => {
  const missing = signedRequest(runPayload(), { "x-hub-signature-256": "" });
  const invalid = signedRequest(runPayload(), { "x-hub-signature-256": `sha256=${"0".repeat(64)}` });
  for (const request of [missing, invalid]) {
    const response = await worker.fetch(request, makeEnv());
    assert.equal(response.status, 401);
    assert.deepEqual(await response.json(), { error: "unauthorized" });
  }
});

test("rejects an oversized body before parsing it", async () => {
  const request = signedRequest(runPayload(), { "content-length": "999999999" });
  const response = await worker.fetch(request, makeEnv());
  assert.equal(response.status, 413);
  assert.deepEqual(await response.json(), { error: "payload_too_large" });

  const streamed = new Request("https://relay.example/webhooks/github", {
    method: "POST",
    headers: { "x-github-event": "workflow_run" },
    body: new ReadableStream({
      start(controller) {
        controller.enqueue(new Uint8Array(600 * 1024));
        controller.enqueue(new Uint8Array(500 * 1024));
        controller.close();
      },
    }),
    duplex: "half",
  });
  assert.equal((await worker.fetch(streamed, makeEnv())).status, 413);
});

test("returns 202 for a duplicate delivery without inserting it again", async () => {
  const db = new FakeD1();
  const first = signedRequest(runPayload());
  const redelivery = signedRequest(runPayload());
  assert.equal((await worker.fetch(first, makeEnv(db))).status, 202);
  assert.equal((await worker.fetch(redelivery, makeEnv(db))).status, 202);
  assert.equal(db.rows.length, 1);
});

test("accepts a previous signing secret during rotation", async () => {
  const env = makeEnv();
  env.GITHUB_WEBHOOK_SECRET = "new-test-webhook-secret";
  env.GITHUB_WEBHOOK_SECRET_PREVIOUS = secret;
  const response = await worker.fetch(signedRequest(runPayload(), {}, secret), env);
  assert.equal(response.status, 202);
});

test("returns a stable storage error without logging exception details", async () => {
  const originalError = console.error;
  const logged = [];
  console.error = (...values) => logged.push(values);
  try {
    const env = makeEnv({
      prepare() {
        return { bind: () => ({ first: async () => { throw new Error("secret and payload detail"); } }) };
      },
    });
    const response = await worker.fetch(signedRequest(runPayload()), env);
    assert.equal(response.status, 503);
    assert.deepEqual(await response.json(), { error: "unavailable" });
    assert.deepEqual(logged, [["github-webhook-relay: failure"]]);
  } finally {
    console.error = originalError;
  }
});

test("rejects unknown events and repositories outside the allowlist", async () => {
  const unknown = signedRequest(runPayload(), { "x-github-event": "push" });
  const rejectedEvent = await worker.fetch(unknown, makeEnv());
  assert.equal(rejectedEvent.status, 400);
  assert.deepEqual(await rejectedEvent.json(), { error: "unsupported_event" });

  const rejectedRepo = await worker.fetch(
    signedRequest(runPayload({ repository: { full_name: "other-org/private" } })),
    makeEnv(),
  );
  assert.equal(rejectedRepo.status, 403);
  assert.deepEqual(await rejectedRepo.json(), { error: "repository_not_allowed" });
});

test("pages events by a stable monotonic cursor with a bounded limit", async () => {
  const db = new FakeD1();
  for (const [delivery, sha] of [["delivery-1", "1"], ["delivery-2", "2"], ["delivery-3", "3"]]) {
    const payload = runPayload({ workflow_run: { ...runPayload().workflow_run, head_sha: sha.repeat(40) } });
    assert.equal(
      (
        await worker.fetch(
          signedRequest(payload, { "x-github-delivery": delivery }),
          makeEnv(db),
        )
      ).status,
      202,
    );
  }

  const response = await worker.fetch(
    new Request("https://relay.example/v1/events?after=1&limit=1", {
      headers: { authorization: "Bearer test-read-token" },
    }),
    makeEnv(db),
  );
  assert.equal(response.status, 200);
  const page = await response.json();
  assert.equal(page.events.length, 1);
  assert.equal(page.events[0].cursor, 2);
  assert.equal(page.next_cursor, 2);
  assert.equal("payload" in page.events[0], false);
  assert.equal((await worker.fetch(new Request("https://relay.example/v1/events"), makeEnv(db))).status, 401);
});

test("scheduled purge deletes events older than fourteen days", async () => {
  const db = new FakeD1();
  db.rows = [
    { cursor: 1, received_at: "2026-10-06T23:59:59.000Z" },
    { cursor: 2, received_at: "2026-10-08T00:00:00.000Z" },
  ];
  await worker.scheduled({ scheduledTime: Date.parse("2026-10-21T00:00:00Z") }, makeEnv(db));
  assert.deepEqual(db.rows.map((row) => row.cursor), [2]);
});
