"""Six public Obsidian Operator tools with read/mutation gates."""

from __future__ import annotations

import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, NoReturn

from .adapters import NativeMcpPort, assert_safe_excalidraw_scene
from .capabilities import reconcile_capabilities
from .catalog import get_catalog_row, hash_action_content, hash_template_definition
from .content_policy import assert_safe_markdown
from .contracts import (
    ERROR_MESSAGES,
    PHASE2_ONLY_ACTION_IDS,
    PUBLIC_RESULT_BYTES,
    READ_TEXT_BYTES,
    TOOLS,
    UNTRUSTED_VAULT_CONTENT,
    ObsidianError,
    parse_action_status_input,
    parse_action_status_result,
    parse_capabilities_input,
    parse_capabilities_result,
    parse_execute_input,
    parse_operator_action,
    parse_propose_input,
    parse_read_input,
    parse_read_result,
    parse_search_input,
    parse_search_result,
    public_result_bytes,
)
from .native_client import (
    MAX_REPLACEMENT_BYTES,
    assert_outbound_tools_call_fits,
    serialize_structured_replacement,
)
from .path_policy import assert_readable, assert_static_writable, policy_for_tags
from .store import ObsidianActionStore, ObsidianActionStoreError
from .utf8 import truncate_utf8

MAX_ACTIVE_READS = 4
THIRTY_MINUTES = timedelta(minutes=30)
TEN_MINUTES = timedelta(minutes=10)
EXCALIDRAW_ROOT = "03 - Resources/Excalidraw"
REJECTION_OUTCOMES = frozenset({"denied", "expired", "replayed", "stale", "cross_bound"})
_TOOL_HANDLERS = {
    "obsidian_capabilities": "capabilities",
    "obsidian_search": "search",
    "obsidian_read": "read",
    "obsidian_action_status": "action_status",
    "obsidian_propose_action": "propose_action",
    "obsidian_execute_action": "execute_action",
}


class FailFastExecuteLock:
    def __init__(self) -> None:
        self._held = False
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            if self._held:
                raise ObsidianError("unavailable", ERROR_MESSAGES["unavailable"])
            self._held = True

    def release(self) -> None:
        with self._lock:
            self._held = False


class FairBoundedGate:
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._active = 0
        self._condition = threading.Condition()

    def acquire(self) -> None:
        with self._condition:
            while self._active >= self._limit:
                self._condition.wait()
            self._active += 1

    def release(self) -> None:
        with self._condition:
            self._active -= 1
            self._condition.notify()


def _unavailable() -> NoReturn:
    raise ObsidianError("unavailable", ERROR_MESSAGES["unavailable"])


def _protocol() -> NoReturn:
    raise ObsidianError("protocol_error", ERROR_MESSAGES["protocol_error"])


def _public(parser: Callable[[object], dict[str, Any]], value: object) -> dict[str, Any]:
    parsed = parser(value)
    if public_result_bytes(parsed) > PUBLIC_RESULT_BYTES:
        _protocol()
    return parsed


def _deny_sensitive_tags(tags: list[str], policy: Mapping[str, Any]) -> None:
    sensitive = set(policy.get("sensitiveTags", ()))
    if any(tag in sensitive for tag in tags):
        raise ObsidianError("denied", ERROR_MESSAGES["denied"])


def _existing_tags(native: NativeMcpPort, path: str) -> list[str]:
    tags = native.tags(path)
    if not isinstance(tags, list):
        raise ObsidianError("denied", ERROR_MESSAGES["denied"])
    return [tag for tag in tags if isinstance(tag, str)]


def _authorize_existing(path: str, policy: Mapping[str, Any], native: NativeMcpPort) -> str:
    tags = _existing_tags(native, path)
    _deny_sensitive_tags(tags, policy)
    return assert_readable(path, policy_for_tags(policy, tags))


def _authorize_existing_write(
    kind: str,
    path: str,
    policy: Mapping[str, Any],
    native: NativeMcpPort,
    dest: str | None = None,
) -> None:
    tags = _existing_tags(native, path)
    _deny_sensitive_tags(tags, policy)
    tagged = policy_for_tags(policy, tags)
    if dest is None:
        assert_static_writable(kind, path, tagged)
        return
    assert_static_writable(kind, path, dest, tagged)


def _require_target(target_config: Mapping[str, str] | None) -> Mapping[str, str]:
    if target_config is None:
        _protocol()
    return target_config


def _excalidraw_scene(name: str, suffix: str) -> str:
    return f"{EXCALIDRAW_ROOT}/{name}{suffix}"


def _assert_safe_action_content(action: Mapping[str, Any]) -> None:
    if action["kind"] in {"create_excalidraw", "update_excalidraw"}:
        rest = {key: value for key, value in action.items() if key != "elements"}
        assert_safe_markdown(rest)
        assert_safe_excalidraw_scene(action["elements"])
        return
    assert_safe_markdown(action)


def _assert_propose_paths(
    action: Mapping[str, Any],
    policy: Mapping[str, Any],
    target_config: Mapping[str, str] | None,
    native: NativeMcpPort | None = None,
) -> None:
    _assert_safe_action_content(action)
    kind = action["kind"]
    if kind == "create_note":
        assert_static_writable(kind, action["path"], policy)
        _deny_sensitive_tags(action["tags"], policy)
        return
    if kind in {"patch_note", "trash_note"}:
        assert_static_writable(kind, action["path"], policy)
        if native is not None:
            _authorize_existing_write(kind, action["path"], policy, native)
        return
    if kind in {"create_canvas", "create_base", "apply_template"}:
        assert_static_writable(kind, action["path"], policy)
        return
    if kind in {"copy_note", "move_note"}:
        assert_static_writable(kind, action["from"], action["to"], policy)
        if native is not None:
            _authorize_existing_write(kind, action["from"], policy, native, action["to"])
        return
    if kind == "append_flashcard":
        assert_static_writable(kind, _require_target(target_config)["flashcardNote"], policy)
        return
    if kind == "create_excalidraw":
        configured = _require_target(target_config)
        scene = _excalidraw_scene(action["name"], configured["excalidrawSuffix"])
        assert_static_writable(kind, scene, {**policy, "excalidrawSuffix": configured["excalidrawSuffix"]})
        if action.get("embed_path") is not None:
            assert_static_writable("patch_note", action["embed_path"], policy)
        return
    if kind in {"patch_canvas", "patch_base"}:
        assert_static_writable(kind, action["path"], policy)
        if native is not None:
            _authorize_existing_write(kind, action["path"], policy, native)
        _assert_phase2_outbound(action)
        return
    if kind == "update_excalidraw":
        assert_static_writable(kind, action["path"], policy)
        if action.get("embed_path") is not None:
            assert_static_writable("patch_note", action["embed_path"], policy)
        if native is not None:
            _authorize_existing_write(kind, action["path"], policy, native)
        _assert_phase2_outbound(action)
        return
    _protocol()


def _assert_phase2_outbound(action: Mapping[str, Any]) -> None:
    kind = action["kind"]
    if kind == "patch_canvas":
        name = "grokbot_replace_canvas_v1"
        replacement = serialize_structured_replacement(action["canvas"])
        path = action["path"]
    elif kind == "patch_base":
        name = "grokbot_replace_base_v1"
        replacement = serialize_structured_replacement(action["yaml"])
        path = action["path"]
    elif kind == "update_excalidraw":
        name = "grokbot_replace_excalidraw_v1"
        replacement = "A" * MAX_REPLACEMENT_BYTES
        path = action["path"]
    else:
        return
    assert_outbound_tools_call_fits(
        name,
        {
            "expected_sha256": "0" * 64,
            "path": path,
            "replacement_utf8": replacement,
        },
    )


def _canonical_digest(request_id: str, action: Mapping[str, Any], digest: str | None) -> str:
    payload: dict[str, Any] = {"request_id": request_id, "action": action}
    if digest is not None:
        payload["template_digest"] = digest
    return hash_action_content(payload)


def _same_private_action(proposal: Mapping[str, Any], approval: Mapping[str, Any]) -> bool:
    try:
        left = parse_operator_action(proposal["action"])
        right = parse_operator_action(approval["action"])
    except ObsidianError:
        return False
    if left["kind"] != proposal["kind"] or right["kind"] != approval["kind"]:
        return False
    try:
        return _canonical_digest(proposal["request_id"], left, proposal.get("template_digest")) == _canonical_digest(
            proposal["request_id"], right, proposal.get("template_digest")
        )
    except ObsidianError:
        return False


def _bound(proposal: Mapping[str, Any], approval: Mapping[str, Any]) -> bool:
    keys = (
        "action_id",
        "request_id",
        "content_hash",
        "kind",
        "target_path",
        "target_version",
        "nonce",
        "expires_at",
        "blast_radius",
    )
    return (
        all(proposal[key] == approval[key] for key in keys)
        and proposal.get("template_digest") == approval.get("template_digest")
        and proposal.get("embed_path") == approval.get("embed_path")
        and proposal.get("embed_version") == approval.get("embed_version")
        and _same_private_action(proposal, approval)
    )


def _approval_expiry(approved_at: str, expires_at: str) -> datetime:
    approved = datetime.fromisoformat(approved_at.replace("Z", "+00:00"))
    expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    return min(approved + TEN_MINUTES, expires)


def _resolve_template(catalog: list[Mapping[str, Any]] | None, template_id: str) -> dict[str, Any]:
    entries = list(catalog or [])
    ids = [entry["id"] for entry in entries]
    if len(set(ids)) != len(ids):
        raise ObsidianError("denied", ERROR_MESSAGES["denied"])
    matches = [entry for entry in entries if entry["id"] == template_id]
    if len(matches) != 1:
        raise ObsidianError("denied", ERROR_MESSAGES["denied"])
    entry = dict(matches[0])
    assert_safe_markdown(entry["body"])
    assert_safe_markdown(entry["variables"])
    return entry


class ObsidianTools:
    def __init__(
        self,
        *,
        native: NativeMcpPort,
        policy: Mapping[str, Any],
        store: ObsidianActionStore,
        reconcile: Callable[[], Mapping[str, Any]] | None = None,
        target_config: Mapping[str, str] | None = None,
        templates: list[Mapping[str, Any]] | None = None,
        executor: Any | None = None,
        clock: Callable[[], datetime] | None = None,
        random_id: Callable[[], str] | None = None,
    ):
        self.native = native
        self.policy = policy
        self.store = store
        self._reconcile = reconcile
        self.target_config = dict(target_config) if target_config is not None else None
        self.templates = list(templates or [])
        self.executor = executor
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.random_id = random_id or (lambda: secrets.token_hex(16))
        self._reads = FairBoundedGate(MAX_ACTIVE_READS)
        self._mutations = FairBoundedGate(1)
        self._execute_lock = FailFastExecuteLock()

    def call_tool(self, name: str, arguments: object) -> dict[str, Any]:
        if name not in TOOLS:
            raise ObsidianError("invalid_request", ERROR_MESSAGES["invalid_request"])
        return getattr(self, _TOOL_HANDLERS[name])(arguments if arguments is not None else {})

    def capabilities(self, raw: object) -> dict[str, Any]:
        parse_capabilities_input(raw)
        self._reads.acquire()
        try:
            try:
                reconciled = (
                    self._reconcile()
                    if self._reconcile is not None
                    else reconcile_capabilities(
                        {
                            "plugin_inventory": [],
                            "command_fingerprint": "sha256:" + ("0" * 64),
                        },
                        self.native.command_list,
                    )
                )
            except ObsidianError:
                raise
            except Exception as exc:
                raise ObsidianError("unavailable", ERROR_MESSAGES["unavailable"]) from exc
            if not isinstance(reconciled, Mapping) or reconciled.get("status") != "ok":
                _unavailable()
            return _public(parse_capabilities_result, reconciled["capabilities"])
        finally:
            self._reads.release()

    def search(self, raw: object) -> dict[str, Any]:
        parsed = parse_search_input(raw)
        self._reads.acquire()
        try:
            hits = self.native.search(parsed["query"], parsed["limit"])
            if not isinstance(hits, list):
                _unavailable()
            results = []
            for hit in hits:
                if not isinstance(hit, dict) or not isinstance(hit.get("path"), str):
                    continue
                try:
                    path = _authorize_existing(hit["path"], self.policy, self.native)
                except ObsidianError as exc:
                    if exc.code == "denied":
                        continue
                    raise
                try:
                    results.append(
                        parse_search_result(
                            {
                                "backend": "native_bounded_search",
                                "results": [
                                    {
                                        "path": path,
                                        "title": hit.get("title", ""),
                                        "snippet": hit.get("snippet", ""),
                                        "score": hit.get("score", 0),
                                        "trust": UNTRUSTED_VAULT_CONTENT,
                                    }
                                ],
                            }
                        )["results"][0]
                    )
                except ObsidianError:
                    continue
                if len(results) >= parsed["limit"]:
                    break
            return _public(parse_search_result, {"backend": "native_bounded_search", "results": results})
        finally:
            self._reads.release()

    def read(self, raw: object) -> dict[str, Any]:
        parsed = parse_read_input(raw)
        self._reads.acquire()
        try:
            path = _authorize_existing(parsed["path"], self.policy, self.native)
            if "target" not in parsed:
                got = self.native.read(path)
                if got is None:
                    raise ObsidianError("not_found", ERROR_MESSAGES["not_found"])
                text, truncated = truncate_utf8(got["bytes"].decode("utf-8"), READ_TEXT_BYTES)
                return _public(
                    parse_read_result,
                    {"path": path, "truncated": truncated, "trust": UNTRUSTED_VAULT_CONTENT, "text": text},
                )
            target = parsed["target"]
            if target["kind"] in {"heading", "block"}:
                text = self.native.read_target(path, target)
                if not isinstance(text, str):
                    _protocol()
                return _public(
                    parse_read_result,
                    {"path": path, "truncated": False, "trust": UNTRUSTED_VAULT_CONTENT, "text": text},
                )
            if target["kind"] == "frontmatter":
                value = self.native.read_target(path, target)
            elif target["kind"] == "map":
                value = self.native.document_map(path)
            elif target["kind"] == "tags":
                value = self.native.tags(path)
            elif target["kind"] == "links":
                value = self.native.links(path)
            else:
                value = self.native.backlinks(path)
            return _public(
                parse_read_result,
                {"path": path, "truncated": False, "trust": UNTRUSTED_VAULT_CONTENT, "value": value},
            )
        finally:
            self._reads.release()

    def action_status(self, raw: object) -> dict[str, Any]:
        parsed = parse_action_status_input(raw)
        self._reads.acquire()
        try:
            try:
                snapshot = self.store.snapshot(parsed["action_id"])
            except ObsidianError:
                raise
            except Exception as exc:
                raise ObsidianError("unavailable", ERROR_MESSAGES["unavailable"]) from exc
            if snapshot is None:
                raise ObsidianError("not_found", ERROR_MESSAGES["not_found"])
            return _public(parse_action_status_result, snapshot)
        finally:
            self._reads.release()

    def _phase2_allowed(self, kind: str) -> bool:
        if kind not in PHASE2_ONLY_ACTION_IDS:
            return True
        try:
            reconciled = (
                self._reconcile()
                if self._reconcile is not None
                else reconcile_capabilities(
                    {"plugin_inventory": [], "command_fingerprint": "sha256:" + ("0" * 64)},
                    self.native.command_list,
                )
            )
        except Exception:
            return False
        if not isinstance(reconciled, Mapping) or reconciled.get("status") != "ok":
            return False
        capabilities = reconciled.get("capabilities")
        if not isinstance(capabilities, Mapping):
            return False
        supported = capabilities.get("supported_action_ids")
        return isinstance(supported, list) and kind in supported

    def propose_action(self, raw: object) -> dict[str, Any]:
        parsed = parse_propose_input(raw)
        if not self._phase2_allowed(parsed["action"]["kind"]):
            raise ObsidianError("denied", ERROR_MESSAGES["denied"])
        _assert_propose_paths(parsed["action"], self.policy, self.target_config, self.native)
        digest = (
            hash_template_definition(_resolve_template(self.templates, parsed["action"]["template_id"]))
            if parsed["action"]["kind"] == "apply_template"
            else None
        )
        content_hash = _canonical_digest(parsed["request_id"], parsed["action"], digest)
        catalog = get_catalog_row(parsed["action"]["kind"])
        self._mutations.acquire()
        try:
            existing = self.store.find_proposal_by_request_id(parsed["request_id"])
            if existing is not None:
                if existing["content_hash"] != content_hash:
                    raise ObsidianError("conflict", ERROR_MESSAGES["conflict"])
                return self._public_propose(
                    existing["action_id"],
                    existing["expires_at"],
                    catalog["summary"],
                    catalog["blast_radius"],
                    True,
                )
            binding = self._resolve_binding(parsed["action"])
            now = self.clock()
            created_at = now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            expires_at = (now + THIRTY_MINUTES).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            action_id = self.random_id()
            nonce = self.random_id()
            record: dict[str, Any] = {
                "version": 1,
                "action_id": action_id,
                "request_id": parsed["request_id"],
                "content_hash": content_hash,
                "kind": parsed["action"]["kind"],
                "action": parsed["action"],
                "target_path": binding["target_path"],
                "target_version": binding["target_version"],
                "blast_radius": catalog["blast_radius"],
                "nonce": nonce,
                "created_at": created_at,
                "expires_at": expires_at,
            }
            if digest is not None:
                record["template_digest"] = digest
            if "embed_path" in binding:
                record["embed_path"] = binding["embed_path"]
                record["embed_version"] = binding["embed_version"]
            self.store.create_proposal(record)
            self.store.write_receipt(
                {
                    "version": 1,
                    "receipt_id": self.random_id(),
                    "kind": "proposal",
                    "action_id": action_id,
                    "outcome": "created",
                    "created_at": created_at,
                }
            )
            return self._public_propose(action_id, expires_at, catalog["summary"], catalog["blast_radius"])
        finally:
            self._mutations.release()

    def execute_action(self, raw: object) -> dict[str, Any]:
        parsed = parse_execute_input(raw)
        self._execute_lock.acquire()
        try:
            return self._execute_approved(parsed["action_id"], parsed["approval_receipt"])
        finally:
            self._execute_lock.release()

    def _public_propose(
        self,
        action_id: str,
        expires_at: str,
        summary: str,
        blast_radius: str,
        duplicate: bool = False,
    ) -> dict[str, Any]:
        value: dict[str, Any] = {
            "action_id": action_id,
            "expires_at": expires_at,
            "blast_radius": blast_radius,
            "summary": summary,
        }
        if duplicate:
            value["duplicate"] = True
        if public_result_bytes(value) > PUBLIC_RESULT_BYTES:
            _protocol()
        return value

    def _read_vault(self, path: str) -> dict[str, Any] | None:
        return self.native.read(path)

    def _require_absent(self, path: str) -> None:
        if self._read_vault(path) is not None:
            raise ObsidianError("conflict", ERROR_MESSAGES["conflict"])

    def _require_existing(self, path: str) -> str:
        got = self._read_vault(path)
        if got is None:
            raise ObsidianError("not_found", ERROR_MESSAGES["not_found"])
        return got["etag"]

    def _require_etag(self, path: str, if_match: str) -> None:
        if self._require_existing(path) != if_match:
            raise ObsidianError("conflict", ERROR_MESSAGES["conflict"])

    def _resolve_binding(self, action: Mapping[str, Any]) -> dict[str, str]:
        kind = action["kind"]
        if kind in {"create_note", "create_canvas", "create_base"}:
            self._require_absent(action["path"])
            return {"target_path": action["path"], "target_version": "absent"}
        if kind == "patch_note":
            self._require_etag(action["path"], action["ifMatch"])
            return {"target_path": action["path"], "target_version": action["ifMatch"]}
        if kind in {"copy_note", "move_note"}:
            etag = self._require_existing(action["from"])
            self._require_absent(action["to"])
            return {"target_path": action["from"], "target_version": etag}
        if kind == "trash_note":
            return {"target_path": action["path"], "target_version": self._require_existing(action["path"])}
        if kind == "append_flashcard":
            path = _require_target(self.target_config)["flashcardNote"]
            self._require_etag(path, action["ifMatch"])
            return {"target_path": path, "target_version": action["ifMatch"]}
        if kind in {"patch_canvas", "patch_base", "update_excalidraw"}:
            self._require_etag(action["path"], action["ifMatch"])
            binding = {"target_path": action["path"], "target_version": action["ifMatch"]}
            if kind == "update_excalidraw" and action.get("embed_path") is not None:
                binding["embed_path"] = action["embed_path"]
                binding["embed_version"] = self._require_existing(action["embed_path"])
            return binding
        if kind == "create_excalidraw":
            configured = _require_target(self.target_config)
            scene = _excalidraw_scene(action["name"], configured["excalidrawSuffix"])
            self._require_absent(scene)
            if action.get("embed_path") is not None:
                return {
                    "target_path": action["embed_path"],
                    "target_version": self._require_existing(action["embed_path"]),
                }
            return {"target_path": scene, "target_version": "absent"}
        if action.get("ifMatch") is None:
            self._require_absent(action["path"])
            return {"target_path": action["path"], "target_version": "absent"}
        self._require_etag(action["path"], action["ifMatch"])
        return {"target_path": action["path"], "target_version": action["ifMatch"]}

    def _write_audit(self, action_id: str, kind: str, outcome: str, created_at: str | None = None) -> str:
        receipt_id = self.random_id()
        self.store.write_receipt(
            {
                "version": 1,
                "receipt_id": receipt_id,
                "kind": kind,
                "action_id": action_id,
                "outcome": outcome,
                "created_at": created_at or self.clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
        )
        return receipt_id

    def _deny_execute(self, action_id: str, outcome: str) -> NoReturn:
        self._write_audit(action_id, "rejection", outcome)
        raise ObsidianError("denied", ERROR_MESSAGES["denied"])

    def _consume(self, approval: Mapping[str, Any]) -> None:
        try:
            self.store.claim_consumed(
                {
                    **approval,
                    "consumed_at": self.clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                }
            )
        except ObsidianActionStoreError as exc:
            if exc.outcome == "replayed":
                self._deny_execute(approval["action_id"], "replayed")
            raise

    def _persist_post_claim_failure(self, action_id: str) -> None:
        try:
            self._write_audit(action_id, "execution", "failed")
        except Exception:
            pass
        try:
            self._write_audit(action_id, "verification", "unverified")
        except Exception:
            pass
        _unavailable()

    def _invoke_executor(self, action: Mapping[str, Any], proposal: Mapping[str, Any]) -> object:
        if self.executor is None:
            _protocol()
        kind = action["kind"]
        if kind == "apply_template":
            raise AssertionError("apply_template uses dedicated path")
        if kind == "create_excalidraw":
            context = {} if action.get("embed_path") is None else {"embedIfMatch": proposal["target_version"]}
            return self.executor.create_excalidraw(action, context)
        if kind == "update_excalidraw":
            context = {}
            if action.get("embed_path") is not None:
                context["embedIfMatch"] = proposal["embed_version"]
            return self.executor.update_excalidraw(action, context)
        names = {
            "create_note",
            "patch_note",
            "copy_note",
            "move_note",
            "trash_note",
            "append_flashcard",
            "create_canvas",
            "create_base",
            "patch_canvas",
            "patch_base",
        }
        if kind not in names:
            _protocol()
        return getattr(self.executor, kind)(action)

    def _assert_preconditions(self, action: Mapping[str, Any], proposal: Mapping[str, Any]) -> None:
        kind = action["kind"]
        if kind in {"create_note", "create_canvas", "create_base"}:
            self._require_absent(action["path"])
            return
        if kind in {"patch_note", "patch_canvas", "patch_base", "update_excalidraw"}:
            self._require_etag(action["path"], action["ifMatch"])
            if kind == "update_excalidraw" and action.get("embed_path") is not None:
                self._require_etag(action["embed_path"], proposal["embed_version"])
            if kind in {"patch_canvas", "patch_base", "update_excalidraw"}:
                _assert_phase2_outbound(action)
            return
        if kind in {"copy_note", "move_note"}:
            self._require_etag(action["from"], proposal["target_version"])
            self._require_absent(action["to"])
            return
        if kind == "trash_note":
            if self._require_existing(action["path"]) != proposal["target_version"]:
                raise ObsidianError("conflict", ERROR_MESSAGES["conflict"])
            return
        if kind == "append_flashcard":
            self._require_etag(_require_target(self.target_config)["flashcardNote"], action["ifMatch"])
            return
        if kind == "create_excalidraw":
            configured = _require_target(self.target_config)
            scene = _excalidraw_scene(action["name"], configured["excalidrawSuffix"])
            if action.get("embed_path") is not None:
                self._require_etag(action["embed_path"], proposal["target_version"])
            if self._read_vault(scene) is not None:
                raise ObsidianError("conflict", ERROR_MESSAGES["conflict"])
            return
        if action.get("ifMatch") is None:
            self._require_absent(action["path"])
            return
        self._require_etag(action["path"], action["ifMatch"])

    def _execute_approved(self, action_id: str, approval_receipt: str) -> dict[str, Any]:
        proposal = self.store.read_proposal(action_id)
        if proposal is None:
            raise ObsidianError("denied", ERROR_MESSAGES["denied"])
        now = self.clock()
        if now >= datetime.fromisoformat(proposal["expires_at"].replace("Z", "+00:00")):
            leftover = self.store.read_approval(action_id)
            if leftover is not None:
                self._consume(leftover)
            self._deny_execute(action_id, "expired")
        approval = self.store.read_approval(action_id)
        if approval is None:
            self._deny_execute(action_id, "denied")
        if approval["approval_receipt"] != approval_receipt or not _bound(proposal, approval):
            self._deny_execute(action_id, "cross_bound")
        if now >= _approval_expiry(approval["approved_at"], proposal["expires_at"]):
            self._consume(approval)
            self._deny_execute(action_id, "expired")
        action = parse_operator_action(proposal["action"])
        if action["kind"] != proposal["kind"]:
            _protocol()
        if not self._phase2_allowed(action["kind"]):
            self._deny_execute(action_id, "denied")
        try:
            _assert_propose_paths(action, self.policy, self.target_config, self.native)
            _assert_safe_action_content(action)
        except ObsidianError as exc:
            if exc.code == "denied":
                self._deny_execute(action_id, "denied")
            raise
        try:
            self._assert_preconditions(action, proposal)
        except ObsidianError as exc:
            if exc.code in {"denied", "conflict", "not_found"}:
                self._deny_execute(action_id, "denied" if exc.code == "denied" else "stale")
            raise
        if self.executor is None:
            _protocol()
        claim_now = self.clock()
        if claim_now >= datetime.fromisoformat(
            proposal["expires_at"].replace("Z", "+00:00")
        ) or claim_now >= _approval_expiry(approval["approved_at"], proposal["expires_at"]):
            self._consume(approval)
            try:
                self._deny_execute(action_id, "expired")
            except ObsidianError as exc:
                if exc.code == "denied":
                    raise
                _unavailable()
        approved_template = None
        if action["kind"] == "apply_template":
            try:
                approved_template = _resolve_template(self.templates, action["template_id"])
                if hash_template_definition(approved_template) != proposal.get("template_digest"):
                    raise ObsidianError("conflict", ERROR_MESSAGES["conflict"])
            except ObsidianError as exc:
                if exc.code in {"denied", "conflict"}:
                    self._deny_execute(action_id, "denied" if exc.code == "denied" else "stale")
                raise
        self._consume(approval)
        try:
            if action["kind"] == "apply_template":
                if approved_template is None:
                    _protocol()
                ack = self.executor.apply_template(action, {"template": approved_template})
            else:
                ack = self._invoke_executor(action, proposal)
        except Exception:
            self._persist_post_claim_failure(action_id)
        if not isinstance(ack, Mapping) or ack.get("outcome") != "verified":
            self._persist_post_claim_failure(action_id)
        executed_at = self.clock()
        try:
            self._write_audit(
                action_id,
                "execution",
                "verified",
                executed_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            )
            receipt_ref = self._write_audit(
                action_id,
                "verification",
                "verified",
                (executed_at + timedelta(milliseconds=1)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            )
            result = {
                "action_id": action_id,
                "kind": action["kind"],
                "outcome": "verified",
                "receipt_ref": receipt_ref,
            }
            if public_result_bytes(result) > PUBLIC_RESULT_BYTES:
                _protocol()
            return result
        except Exception as exc:
            raise ObsidianError("unavailable", ERROR_MESSAGES["unavailable"]) from exc
