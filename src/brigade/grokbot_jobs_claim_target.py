"""The fleet claim key stored on a Grok Bot queue lease row (#1639).

The key is written in the same locked row write that grants the lease
(``grokbot_jobs.claim(..., claim_target=...)``). These helpers read it back,
and write it into a lease row from before the field existed. They are split
from ``grokbot_jobs`` so the storage module stays under the size ratchet.
"""

from __future__ import annotations

from pathlib import Path


def valid_claim_target(value: object) -> bool:
    """A fleet claim key: bounded text with no control characters."""
    return isinstance(value, str) and 0 < len(value) <= 512 and not any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def lease_claim_target(target: Path, job_id: str, bot_id: str, lease_id: str) -> str | None:
    """The fleet claim key stored on this lease's row, or ``None``.

    The row is the one source every renew, release and event reads, so they
    all address the claim the lease acquired under. It stays readable after a
    terminal transition, so a release that follows a failure still finds it.
    """
    from . import grokbot_jobs as jobs

    job_id = jobs._validate_job_id(job_id)
    bot_id = jobs._validate_opaque_id(bot_id, "invalid-bot-id")
    lease_id = jobs._validate_opaque_id(lease_id, "invalid-lease-id")
    if jobs.hub_authority(target):
        return None
    with jobs._storage_paths(target) as storage, jobs._queue_lock(storage):
        record = jobs._load_record(storage.jobs, job_id)
        if record.get("bot_id") == bot_id and record.get("lease_id") == lease_id:
            return record.get("claim_target")
    return None


def bind_lease_claim_target(target: Path, job_id: str, bot_id: str, lease_id: str, claim_target: str) -> str:
    """Store ``claim_target`` on this lease's row unless it has one; return the stored key.

    Only for a lease row from before the field existed: a new lease gets its
    key in the claim write itself. First writer wins under the queue lock.
    Metadata only: the item revision and ``updated_at`` stay as they are.
    """
    from . import grokbot_jobs as jobs

    job_id = jobs._validate_job_id(job_id)
    bot_id = jobs._validate_opaque_id(bot_id, "invalid-bot-id")
    lease_id = jobs._validate_opaque_id(lease_id, "invalid-lease-id")
    if not valid_claim_target(claim_target):
        raise jobs.GrokbotJobError("invalid-claim-target")
    if jobs.hub_authority(target):
        raise jobs.GrokbotJobError("hub-authority")
    with jobs._storage_paths(target) as storage, jobs._queue_lock(storage):
        record = jobs._load_record(storage.jobs, job_id)
        if record.get("bot_id") != bot_id or record.get("lease_id") != lease_id:
            raise jobs.GrokbotJobError("lease-conflict")
        stored = record.get("claim_target")
        if stored is not None:
            return stored
        record["claim_target"] = claim_target
        jobs._write_json_file(storage.jobs, f"{job_id}.json", record)
        return claim_target
