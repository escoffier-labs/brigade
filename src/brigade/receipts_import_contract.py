"""Validate native ledger completion before acknowledging exported receipts."""

from __future__ import annotations


def _miseledger_import_summary(payload: object) -> tuple[int, bool | int]:
    """Require explicit completion, including restoration after pruning."""
    if not isinstance(payload, dict):
        raise ValueError("import summary must be an object")
    inserted = payload.get("inserted_items", payload.get("inserted"))
    known = payload.get("already_known", payload.get("known_items"))
    if type(inserted) is not int or inserted < 0:
        raise ValueError("import summary requires a nonnegative inserted_items count")
    for field in ("inserted_items", "inserted"):
        if field in payload:
            value = payload[field]
            if type(value) is not int or value < 0 or value != inserted:
                raise ValueError("inconsistent inserted count aliases")
    if not isinstance(known, bool) and (type(known) is not int or known not in (0, 1)):
        raise ValueError("import summary requires already_known")
    for field in ("already_known", "known_items"):
        if field in payload:
            value = payload[field]
            if not isinstance(value, bool) and (type(value) is not int or value not in (0, 1)):
                raise ValueError("import summary requires a valid known flag")
            if bool(value) != bool(known):
                raise ValueError("inconsistent known flag aliases")
    status = payload.get("status")
    if status not in ("complete", "already_known"):
        raise ValueError("import summary requires explicit completion from a repaired importer")
    if (status == "already_known") != bool(known):
        raise ValueError("inconsistent completion status and known flag")
    # already_known identifies a previously imported source hash. A supported
    # replay may still restore pruned items, so its inserted count can be positive.
    for field in ("warnings", "errors"):
        value = payload.get(field)
        if value is not None and not isinstance(value, list):
            raise ValueError(f"import summary requires {field} to be an array or null")
        if value:
            raise ValueError(f"import summary reports {field}")
    for field in ("failed_items", "error_count", "warning_count"):
        if field in payload:
            value = payload[field]
            if type(value) is not int or value < 0:
                raise ValueError(f"import summary requires a nonnegative {field} count")
            if value != 0:
                raise ValueError(f"import summary reports {field}")
    if "failed" in payload and (type(payload["failed"]) is not bool or payload["failed"]):
        raise ValueError("import summary requires failed to be false")
    error = payload.get("error")
    if error is not None and (not isinstance(error, str) or error):
        raise ValueError("import summary reports an invalid or nonempty error")
    return inserted, known
