const ALLOWED_EVENTS = new Set(["workflow_run", "check_suite", "check_run", "pull_request", "status"]);
const MAX_BODY_BYTES = 1024 * 1024;
const MAX_PAGE_SIZE = 100;
const RETENTION_DAYS = 14;
const INSERT_DEADLINE_MS = 8000;
const encoder = new TextEncoder();
const EVENT_COLUMNS = [
  "cursor",
  "delivery_id",
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
];

function jsonResponse(status, value) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
  });
}

function constantTimeEqual(left, right) {
  const a = encoder.encode(left);
  const b = encoder.encode(right);
  let difference = a.length ^ b.length;
  const length = Math.max(a.length, b.length);
  for (let index = 0; index < length; index += 1) {
    difference |= (a[index] ?? 0) ^ (b[index] ?? 0);
  }
  return difference === 0;
}

async function hmacSignature(secret, body) {
  const key = await crypto.subtle.importKey("raw", encoder.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const digest = new Uint8Array(await crypto.subtle.sign("HMAC", key, body));
  return `sha256=${Array.from(digest, (byte) => byte.toString(16).padStart(2, "0")).join("")}`;
}

async function verifySignature(request, body, env) {
  const provided = request.headers.get("x-hub-signature-256") ?? "";
  const secrets = [env.GITHUB_WEBHOOK_SECRET, env.GITHUB_WEBHOOK_SECRET_PREVIOUS].filter(
    (secret) => typeof secret === "string" && secret.length > 0,
  );
  if (secrets.length === 0 || !/^sha256=[0-9a-f]{64}$/.test(provided)) return false;
  const results = await Promise.all(secrets.map(async (secret) => constantTimeEqual(await hmacSignature(secret, body), provided)));
  return results.reduce((matched, result) => matched | Number(result), 0) === 1;
}

async function readBoundedBody(request) {
  const contentLength = request.headers.get("content-length");
  if (contentLength !== null && /^\d+$/.test(contentLength) && Number(contentLength) > MAX_BODY_BYTES) {
    return null;
  }
  if (!request.body) return new Uint8Array();
  const reader = request.body.getReader();
  const chunks = [];
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_BODY_BYTES) {
        await reader.cancel();
        return null;
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const body = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return body;
}

function allowedRepositories(env) {
  if (typeof env.ALLOWED_REPOSITORIES !== "string") return [];
  return env.ALLOWED_REPOSITORIES.split(",").map((repository) => repository.trim().toLowerCase()).filter(Boolean);
}

function object(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function boundedString(value, maximum = 128) {
  return typeof value === "string" && value.length > 0 && value.length <= maximum ? value : null;
}

function numericId(value) {
  if ((typeof value !== "string" && typeof value !== "number") || !/^\d{1,24}$/.test(String(value))) return null;
  return String(value);
}

function positiveInteger(value) {
  return Number.isSafeInteger(value) && value > 0 ? value : null;
}

function providerTimestamp(value) {
  if (typeof value !== "string" || value.length > 64 || !Number.isFinite(Date.parse(value))) return null;
  return value;
}

function normalizeEvent(payload, event, deliveryId, receivedAt) {
  const root = object(payload);
  const data = object(root[event]);
  const repository = object(root.repository);
  const pullRequest = object(root.pull_request);
  const pullRequestHead = object(pullRequest.head);
  const runPullRequests = Array.isArray(data.pull_requests) ? data.pull_requests : [];
  const firstRunPullRequest = object(runPullRequests[0]);
  const status = event === "status" ? root.state : event === "pull_request" ? pullRequest.state : data.status;
  const headSha = event === "pull_request" ? pullRequestHead.sha : event === "status" ? root.sha : data.head_sha;
  const timestamps = event === "status" ? root : data;
  const action = boundedString(root.action, 100);
  const head = boundedString(headSha, 64);
  const prNumber = event === "pull_request" ? pullRequest.number : firstRunPullRequest.number;
  return {
    delivery_id: deliveryId,
    repository: boundedString(repository.full_name, 255),
    event,
    action,
    head_sha: head && /^[0-9a-f]{40}(?:[0-9a-f]{24})?$/i.test(head) ? head : null,
    status: boundedString(status, 100),
    conclusion: boundedString(data.conclusion, 100),
    run_id: numericId(data.id),
    run_attempt: positiveInteger(data.run_attempt),
    pr_number: positiveInteger(prNumber),
    provider_created_at: providerTimestamp(timestamps.created_at),
    provider_updated_at: providerTimestamp(timestamps.updated_at),
    received_at: receivedAt,
  };
}

function parseCursor(value, fallback) {
  if (value === null) return fallback;
  if (!/^\d{1,15}$/.test(value)) return null;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) ? parsed : null;
}

function parseLimit(value) {
  if (value === null) return 50;
  if (!/^\d{1,4}$/.test(value)) return null;
  const parsed = Number(value);
  return parsed > 0 ? Math.min(parsed, MAX_PAGE_SIZE) : null;
}

function bearerMatches(request, expected) {
  const authorization = request.headers.get("authorization") ?? "";
  const match = /^Bearer ([A-Za-z0-9._~+/-]+=*)$/.exec(authorization);
  return typeof expected === "string" && expected.length > 0 && match !== null && constantTimeEqual(match[1], expected);
}

async function receiveWebhook(request, env) {
  const body = await readBoundedBody(request);
  if (body === null) return jsonResponse(413, { error: "payload_too_large" });
  if (!(await verifySignature(request, body, env))) return jsonResponse(401, { error: "unauthorized" });

  const event = request.headers.get("x-github-event") ?? "";
  if (!ALLOWED_EVENTS.has(event)) return jsonResponse(400, { error: "unsupported_event" });
  const deliveryId = request.headers.get("x-github-delivery") ?? "";
  if (!/^[A-Za-z0-9-]{1,128}$/.test(deliveryId)) return jsonResponse(400, { error: "invalid_delivery" });

  let payload;
  try {
    payload = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(body));
  } catch {
    return jsonResponse(400, { error: "invalid_payload" });
  }

  const repository = boundedString(object(object(payload).repository).full_name, 255);
  if (!repository || !allowedRepositories(env).includes(repository.toLowerCase())) {
    return jsonResponse(403, { error: "repository_not_allowed" });
  }
  if (!env.DB || typeof env.DB.prepare !== "function") return jsonResponse(503, { error: "unavailable" });

  try {
    const record = normalizeEvent(payload, event, deliveryId, new Date().toISOString());
    const insert = env.DB.prepare(
      `INSERT OR IGNORE INTO events (
        delivery_id, repository, event, action, head_sha, status, conclusion,
        run_id, run_attempt, pr_number, provider_created_at, provider_updated_at, received_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING cursor`,
    )
      .bind(
        record.delivery_id,
        record.repository,
        record.event,
        record.action,
        record.head_sha,
        record.status,
        record.conclusion,
        record.run_id,
        record.run_attempt,
        record.pr_number,
        record.provider_created_at,
        record.provider_updated_at,
        record.received_at,
      )
      .first();
    let timeoutId;
    const timeout = new Promise((resolve) => {
      timeoutId = setTimeout(() => resolve("timeout"), INSERT_DEADLINE_MS);
    });
    let result;
    try {
      result = await Promise.race([insert, timeout]);
    } finally {
      clearTimeout(timeoutId);
    }
    if (result === "timeout") {
      void insert.catch(() => {});
      console.error("github-webhook-relay: failure");
      return jsonResponse(503, { error: "unavailable" });
    }
    return jsonResponse(202, { accepted: true });
  } catch {
    console.error("github-webhook-relay: failure");
    return jsonResponse(503, { error: "unavailable" });
  }
}

async function listEvents(request, env) {
  if (!bearerMatches(request, env.EVENTS_READ_TOKEN)) return jsonResponse(401, { error: "unauthorized" });
  if (!env.DB || typeof env.DB.prepare !== "function") return jsonResponse(503, { error: "unavailable" });

  const url = new URL(request.url);
  const after = parseCursor(url.searchParams.get("after"), 0);
  const limit = parseLimit(url.searchParams.get("limit"));
  if (after === null || limit === null) return jsonResponse(400, { error: "invalid_pagination" });

  try {
    const result = await env.DB.prepare(
      `SELECT ${EVENT_COLUMNS.join(", ")} FROM events WHERE cursor > ? ORDER BY cursor ASC LIMIT ?`,
    )
      .bind(after, limit)
      .all();
    const events = Array.isArray(result.results) ? result.results : [];
    const nextCursor = events.length > 0 ? events[events.length - 1].cursor : after;
    return jsonResponse(200, { events, next_cursor: nextCursor });
  } catch {
    console.error("github-webhook-relay: failure");
    return jsonResponse(503, { error: "unavailable" });
  }
}

async function purgeExpired(env, now) {
  const cutoff = new Date(now - RETENTION_DAYS * 24 * 60 * 60 * 1000).toISOString();
  await env.DB.prepare("DELETE FROM events WHERE received_at < ?").bind(cutoff).run();
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "POST" && url.pathname === "/webhooks/github") {
      return receiveWebhook(request, env);
    }
    if (request.method === "GET" && url.pathname === "/v1/events") {
      return listEvents(request, env);
    }
    return jsonResponse(404, { error: "not_found" });
  },

  async scheduled(controller, env) {
    try {
      await purgeExpired(env, controller.scheduledTime);
    } catch {
      console.error("github-webhook-relay: failure");
      throw new Error("github-webhook-relay failure");
    }
  },
};
