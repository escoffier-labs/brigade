"""Seed retained local records for projection tests without production job creation."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _write(path, record):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(record), encoding="utf-8")
    path.chmod(0o600)


def enqueue(target, spec, key, *, now=None):
    now = now or datetime.now(timezone.utc)
    digest = hashlib.sha256(key.encode()).hexdigest()
    job_id = "grokbot-" + digest[:24]
    stamp = now.isoformat().replace("+00:00", "Z")
    record = dict(
        schema="brigade.grokbot.job.v1",
        job_id=job_id,
        task_hash="sha256:"
        + hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        idempotency_key_hash="sha256:" + digest,
        state="queued",
        created_at=stamp,
        updated_at=stamp,
        queued_at=stamp,
        timeout_seconds=spec["timeout_seconds"],
        item_revision=1,
        spec=spec,
    )
    _write(Path(target) / ".brigade/cloud/grokbot/jobs" / (job_id + ".json"), record)
    return {"job_id": job_id}


def claim(target, job_id, bot_id, lease_id, seconds, *, now=None):
    now = now or datetime.now(timezone.utc)
    _change(
        target,
        job_id,
        state="claimed",
        bot_id=bot_id,
        lease_id=lease_id,
        claimed_at=now.isoformat().replace("+00:00", "Z"),
        lease_expires_at=(now + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z"),
        updated_at=now.isoformat().replace("+00:00", "Z"),
    )


def transition(target, job_id, bot_id, lease_id, state, *, artifact=None, now=None):
    fields = dict(state=state, updated_at=(now or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z"))
    if artifact is not None:
        fields["result_artifact"] = artifact
    _change(target, job_id, **fields)


def _change(target, job_id, **fields):
    path = Path(target) / ".brigade/cloud/grokbot/jobs" / (job_id + ".json")
    record = json.loads(path.read_text(encoding="utf-8"))
    record.update(fields)
    record["item_revision"] += 1
    _write(path, record)
