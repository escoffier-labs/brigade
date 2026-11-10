"""Workspace-scoped memory, handoff, policy and installed skill access."""

from __future__ import annotations
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path, PurePath
import re
import stat
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Callable, Iterator
from uuid import uuid4
from ..fleet_client import FleetClientError
from ..fleet_client_message_credentials import MessageAuthority
from .contracts import MAX_CARD_FILE_BYTES, DotError, require
from .hub_tools import BRIGADE_TIMEOUT, MAX_SMALL_CAPTURE
from .process import CommandRunner, child_environment, child_path, parse_json_bytes
from .profile import DOT, ConnectorProfile
from .prompt_scan import egress_refusal
from .runtime_config import Runtime
from .safe_state import private_directory, trusted_directory, redact_credentials
from .state import utc_now

if TYPE_CHECKING:
    from ..memory_hub_client import MemoryHubClient

CARD_CHUNK_CHARS = 30000
HANDOFF_ID_WINDOW_MS = 600_000
TOKEN_COPY_PREFIX = ".memory-token-"
# Well over the 30 second child timeout, so a sweep during a call never removes a live copy.
TOKEN_COPY_MAX_AGE_SECONDS = 300
# The hub's handoff refusals worth showing the caller. Anything else comes back as plain `refused`.
HANDOFF_REFUSAL_CODES = frozenset(
    {
        "memory_grant_missing",
        "memory_request_invalid",
        "memory_client_event_invalid",
        "memory_client_event_expired",
        "memory_client_event_conflict",
        "memory_card_too_large",
        # Rewrite the text (blocked), or try again later (scan failed): the caller needs to tell them apart.
        "memory_content_blocked",
        "memory_scan_failed",
    }
)
POLICY_PATHS = (Path(".codex/AGENTS.md"), Path("AGENTS.md"), Path("ai-tools/AGENTS.md"))
# Dot and Duet hub memory with no memory hub on this host: the fleet hub serves no memory.
NO_MEMORY_HUB = "no-memory-hub"
# The hub's `search` limit. A larger request is capped there and says so.
HUB_SEARCH_LIMIT = 20
_LOCAL_KEYS: tuple[str, ...] = ("card_id", "title", "path", "score", "summary", "tags", "card_aliases")
_HUB_KEYS: tuple[str, ...] = _LOCAL_KEYS + ("source", "scope", "revision", "trust")


class MemoryTools:
    def __init__(
        self,
        runtime: Runtime,
        *,
        commands: CommandRunner,
        profile: ConnectorProfile = DOT,
        authority: Callable[[], MessageAuthority] | None = None,
    ) -> None:
        self.runtime, self.commands, self.profile = runtime, commands, profile
        # A principal profile reads and files handoffs under the authority the listener pinned at startup.
        self._authority = authority
        self.workspace = runtime.paths.workspace
        self.cards = self.workspace / "memory" / "cards"
        self.handoff_inbox = self.workspace / profile.handoff_inbox
        self.clock = utc_now
        self._path = child_path(runtime.paths.bin)
        self.environment = child_environment(self._path)

    def _principal_authority(self) -> MessageAuthority:
        """The authority a principal profile's memory calls use, refused when it drifted from startup.

        The listener's inbox pins the credential it authenticated at startup. Asking it for the current
        authority rechecks the token file and hub URL against that pin, so a swapped file or a moved hub
        is refused here, before any transport, instead of being adopted silently.
        """
        if self._authority is not None:
            return self._authority()
        try:
            return MessageAuthority.capture(str(self.runtime.hub.message_token_file))
        except FleetClientError:
            raise DotError("hub_credential_invalid") from None

    def sweep_token_copies(self, older_than: float) -> None:
        """Unlink leftover pinned-token copies in the state directory that are at least `older_than` seconds old.

        A copy outlives a crash that skipped its `finally`, and a rotated token would then sit in the
        directory indefinitely. Startup passes 0, since nothing is in flight. A call passes a margin well
        over the child timeout, so a copy another thread is using is never removed.
        """
        state_fd = private_directory(self.runtime.paths.state_dir, create=True)
        try:
            now = time.time()
            for entry in os.scandir(state_fd):
                if not entry.name.startswith(TOKEN_COPY_PREFIX):
                    continue
                try:
                    if now - os.stat(entry.name, dir_fd=state_fd, follow_symlinks=False).st_mtime >= older_than:
                        os.unlink(entry.name, dir_fd=state_fd)
                except OSError:
                    continue
        finally:
            os.close(state_fd)

    @contextmanager
    def _pinned_token_file(self, authority: MessageAuthority) -> Iterator[str]:
        """A private copy of the pinned token, so a child reads exactly the authority that was just checked."""
        self.sweep_token_copies(TOKEN_COPY_MAX_AGE_SECONDS)
        state_fd = private_directory(self.runtime.paths.state_dir, create=True)
        name = f"{TOKEN_COPY_PREFIX}{uuid4().hex}"
        try:
            descriptor = os.open(
                name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=state_fd
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(authority.token.encode())
            yield str(Path(self.runtime.paths.state_dir) / name)
        finally:
            try:
                os.unlink(name, dir_fd=state_fd)
            except OSError:
                pass
            os.close(state_fd)

    def _run_brigade(self, tail: list[str], *, timeout: float, max_output: int) -> tuple[Any, str | None]:
        argv = [self.runtime.paths.bin.brigade, *tail]
        if not self.profile.principal_memory:
            return self._parsed(self.commands.run(argv, timeout=timeout, max_output=max_output, env=self.environment))
        from ..memory_hub_client import (
            ENV_HUB_TOKEN_FILE,
            ENV_HUB_URL,
            ENV_REPLICA_PRINCIPAL,
            HubConfigError,
            hub_mode,
        )

        # Local cards are never served to this profile, so hub memory must be on, and a mode that cannot
        # be read is never answered from the host's node credential.
        try:
            mode = hub_mode(self.workspace)
        except (HubConfigError, ValueError, OSError):
            return None, "hub_credential_unavailable"
        if mode == "off":
            return None, "hub_memory_required"
        authority = self._principal_authority()
        try:
            with self._pinned_token_file(authority) as token_file:
                environment = child_environment(
                    self._path,
                    {
                        ENV_HUB_URL: authority.hub,
                        ENV_HUB_TOKEN_FILE: token_file,
                        # Offline retrieval serves only a replica this principal pulled itself.
                        ENV_REPLICA_PRINCIPAL: self.runtime.hub.principal,
                    },
                )
                return self._parsed(self.commands.run(argv, timeout=timeout, max_output=max_output, env=environment))
        except OSError:
            return None, "memory_unavailable"

    @staticmethod
    def _parsed(result: Any) -> tuple[Any, str | None]:
        if result.timed_out:
            return None, "brigade_timeout"
        if result.overflow:
            return None, "output_too_large"
        value = parse_json_bytes(result.stdout)
        if value is None:
            return None, "brigade_unavailable"
        return value, None

    def _brigade_json(
        self, tail: list[str], *, timeout: float = BRIGADE_TIMEOUT, max_output: int = MAX_SMALL_CAPTURE
    ) -> tuple[Any, str | None]:
        return self._run_brigade(tail, timeout=timeout, max_output=max_output)

    def memory_search(self, query: str, limit: int = 10) -> dict[str, Any]:
        """Search memory cards. With hub memory on, Dot and Duet search the memory hub in process.

        Hub matches come from `_principal_search`, never the fleet hub, a fleet credential or the replica. A
        principal memory profile (Grok Bot memory) runs `brigade memory search` with its own pinned
        credential, grant-scoped rather than push-restricted. With hub memory off, the search CLI
        reads local cards only.
        """
        if not self.profile.principal_memory:
            from ..memory_hub_client import HubConfigError, hub_mode

            try:
                hub_on = hub_mode(self.workspace) != "off"
            except (HubConfigError, ValueError, OSError):
                return {"error": "memory_unavailable"}
            if hub_on:
                return self._principal_search(query, limit)
        value, error = self._brigade_json(
            ["memory", "search", "--target", str(self.workspace), "--limit", str(limit), "--json", "--", query],
            timeout=30,
        )
        if error or not isinstance(value, dict) or not isinstance(value.get("matches"), list):
            return {"error": error or "memory_unavailable"}
        keys: tuple[str, ...] = _LOCAL_KEYS
        if "retrieval" in value:
            # Hub memory is on for this workspace (S14): keep where each match came from.
            keys = _HUB_KEYS
        matches = []
        dropped_local = False
        for match in value["matches"][:limit]:
            if not isinstance(match, dict):
                continue
            if self.profile.principal_memory and match.get("source") not in {"hub", "replica"}:
                # Only what this principal's own grants opened: a local workspace card is never served.
                dropped_local = True
                continue
            matches.append(self._shown(match, keys))
        match_count = value.get("match_count")
        payload = dict(
            query=query,
            match_count=(
                match_count
                if not dropped_local and (not self.profile.prompt_load_scan or _is_count(match_count))
                else None
            ),
            matches=matches,
            workspace=self._workspace_label(),
            next="Read a card with memory_card(name=<path basename without .md>).",
        )
        if isinstance(value.get("retrieval"), dict):
            payload["next"] = (
                "Read a card with memory_card(name=<path basename without .md>); "
                "for a hub or replica match, also pass scope=<its scope>."
            )
            if self.profile.prompt_load_scan:
                # Hub JSON passes through the search CLI, so only known keys and values leave.
                retrieval = _known_retrieval(value["retrieval"])
                if self.profile.principal_memory and "fallback" in retrieval:
                    # Local cards are never served here, so an answer must not say they stood in.
                    retrieval["fallback"] = [source for source in retrieval["fallback"] if source != "local"]
                payload["retrieval"] = retrieval
                code = _warning_code(value.get("warning"), retrieval)
                if code:
                    payload["warning"] = code
            else:
                payload["retrieval"] = value["retrieval"]
                if isinstance(value.get("warning"), str):
                    payload["warning"] = value["warning"]
        return payload

    def _principal_search(self, query: str, limit: int) -> dict[str, Any]:
        """Dot and Duet search with hub memory on (S23).

        Hub matches come from the memory hub's `search` with `query_source: "connector"`, sent with the
        `[memory_hub]` read credential (`_memory_hub_client`), so they hold only what `memory_hits` could
        send: no other writer's unreviewed card and no pending, flagged, private or retired version. The
        fleet hub, the node and message credentials and the replica play no part. A host with no memory
        hub, or an unavailable or refusing one, answers no hub matches and says why in `retrieval`.
        Local workspace cards are searched in process and returned apart, as `local_matches`.
        """
        from .. import memory_cmd
        from ..memory_hub_client import HubConfigError, HubRefusal, HubUnavailable, valid_card_id, valid_scope
        from .memory_push import PUSHED_TRUST, RULE_RE, hub_query
        from .prompt_scan import PRIVATE_CODE

        retrieval: dict[str, Any] = {"hub": "ok", "degraded": False}
        warning = None
        hub_matches: list[dict[str, Any]] = []
        match_count = None
        try:
            body = self.connector_search(hub_query(query), min(limit, HUB_SEARCH_LIMIT))
        except HubUnavailable as exc:
            retrieval.update(hub="unavailable", degraded=True, reason=exc.reason)
            warning = WARNING_HUB_UNAVAILABLE
        except HubRefusal as exc:
            retrieval.update(hub="refused", degraded=True, reason=exc.code or f"http-{exc.status}", status=exc.status)
            warning = WARNING_HUB_REFUSED
        except HubConfigError:
            retrieval.update(hub="unavailable", degraded=True, reason="hub-config-invalid")
            warning = WARNING_HUB_UNAVAILABLE
        else:
            raw_matches = body.get("matches")
            for match in (raw_matches if isinstance(raw_matches, list) else [])[:limit]:
                if not isinstance(match, dict):
                    continue
                if "blocked" in match:
                    # The hub withheld this card's text: list it by id and rule only, as for a local refusal.
                    rule = match.get("blocked")
                    refusal = {
                        "blocked": rule if isinstance(rule, str) and RULE_RE.fullmatch(rule) else MALFORMED_MATCH
                    }
                    if match.get("code") == PRIVATE_CODE:
                        refusal["code"] = PRIVATE_CODE
                    hub_matches.append(_refused_match({"card_id": match.get("card_id")}, refusal))
                    continue
                if match.get("trust") not in PUSHED_TRUST:
                    continue
                if not (valid_scope(match.get("scope")) and valid_card_id(match.get("card_id"))):
                    continue
                hub_matches.append(self._shown({**match, "source": "hub"}, _HUB_KEYS))
            if _is_count(body.get("match_count")):
                match_count = body["match_count"]
            if limit > HUB_SEARCH_LIMIT:
                retrieval["hub_limit"] = HUB_SEARCH_LIMIT
                warning = warning or WARNING_HUB_LIMIT
        try:
            local = memory_cmd.search_cards_payload(self.workspace, query, limit=limit)["matches"]
        except (OSError, ValueError):
            local = []
        local_matches = [
            self._shown({**match, "source": "local"}, _LOCAL_KEYS + ("source",))
            for match in local[:limit]
            if isinstance(match, dict)
        ]
        payload: dict[str, Any] = dict(
            query=query,
            match_count=match_count,
            matches=hub_matches,
            local_matches=local_matches,
            workspace=self._workspace_label(),
            next=(
                "Read a hub match with memory_card(name=<card_id>, scope=<its scope>), and a local match "
                "with memory_card(name=<path basename without .md>)."
            ),
            retrieval=_known_retrieval(retrieval) if self.profile.prompt_load_scan else retrieval,
        )
        if warning:
            payload["warning"] = warning
        self._log_search(query, [*hub_matches, *local_matches])
        return payload

    def _log_search(self, query: str, shown: list[dict[str, Any]]) -> None:
        """Append the search-log record the CLI writes for an explicit search, fail-open.

        Only an explicit `memory_search` reaches here, never the unsolicited `memory_hits`. The record
        holds what the CLI logs (time, normalized query, top card ids and aliases), taken from the matches
        this response shows, so a card the hub withheld adds nothing.
        """
        from .. import memory_cmd

        try:
            memory_cmd._record_search_log(self.workspace, query=query, matches=shown)
        except Exception:
            # A log failure never changes the tool response.
            return

    def _workspace_label(self) -> str | None:
        if not self.profile.prompt_load_scan:
            return str(self.workspace)
        # The directory name only: the absolute path is local configuration, and a home path is private text.
        # A workspace that is the home directory is named by the user, so it gets no name.
        name = self.workspace.name
        return None if _is_home(self.workspace) or egress_refusal([name]) else name

    def _shown(self, match: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
        """One search match as it may leave: the known keys, scanned, or a placeholder with no text."""
        item = {key: match.get(key) for key in keys}
        if self.profile.prompt_load_scan:
            # A field the scan cannot read as text (a hub match passes its JSON through) is refused.
            if not _well_formed(item):
                return _refused_match(item, {"blocked": MALFORMED_MATCH})
            source = item.get("source")
            if source is not None and source != "local":
                # A hub or replica match: the hub's own path is free text, so it is rebuilt from the card id.
                item["path"] = _hub_card_path(item.get("card_id"))
            else:
                # A path that cannot be shown as a workspace-relative one is refused the same way.
                relative = _relative_path(item.get("path"), self.workspace)
                if relative is None and item.get("path") is not None:
                    return _refused_match(item, {"blocked": MALFORMED_MATCH})
                item["path"] = relative
            # The returned path and hub scope are scanned too: a file name or scope can carry the text.
            refusal = egress_refusal([*_card_texts(item), *_location_texts(item)])
            if refusal:
                return _refused_match(item, refusal)
            _keep_known_hub_labels(item)
            if not _is_number(item.get("score")):
                item["score"] = None
        item["content_trust"] = "untrusted"
        return item

    def push_search(self, query: str, limit: int) -> dict[str, Any]:
        """Memory hub `search` with `query_source: "push"`, with the `[memory_hub]` read credential (S23).

        That credential's grants decide what it reads, never the host node's. One call inside the search
        budget: the push is fail-open, and the caller omits `memory_hits` on any error, including a host
        with no memory hub. The hub does not count these unsolicited hits as reads.
        """
        return self._memory_hub_search(query, limit, "push")

    def connector_search(self, query: str, limit: int) -> dict[str, Any]:
        """Hub `search` with `query_source: "connector"`: the agent's own `memory_search`.

        The hub applies the same push filter as `push_search` and counts the matches as reads, so
        read statistics and never-read reports see explicit connector searches.
        """
        return self._memory_hub_search(query, limit, "connector")

    def _memory_hub_search(self, query: str, limit: int, source: str) -> dict[str, Any]:
        from ..memory_hub_client import SEARCH_BUDGET_SECONDS

        client = self._memory_hub_client()
        return client.call("search", timeout=SEARCH_BUDGET_SECONDS, query=query, limit=limit, query_source=source)

    def _memory_hub_client(self) -> MemoryHubClient:
        """The memory hub Dot and Duet search and read: an explicit user-level `[memory_hub]` only.

        The listener's message authority names the fleet hub, which serves no memory, so hub memory
        reads use the memory hub and the read credential its `[memory_hub]` table names
        (`connector_settings`). The `BRIGADE_MEMORY_HUB_*` pair, which belongs to the sync and mirror
        units, the fleet values and a relative `BRIGADE_HOME` never choose it. With no such table this
        raises `HubUnavailable("no-memory-hub")`, and nothing is sent to any hub. A `[memory_hub]`
        that cannot be read raises `HubConfigError`.
        """
        from ..memory_hub_client import HubConfigError, HubUnavailable, MemoryHubClient, connector_settings

        try:
            settings = connector_settings(self.workspace)
        except (ValueError, OSError) as exc:
            raise HubConfigError("memory hub settings could not be read") from exc
        if settings is None:
            raise HubUnavailable(NO_MEMORY_HUB)
        return MemoryHubClient(settings.hub_url, settings.token)

    def _hub_card(self, name: str, offset: int, scope: str | None = None) -> dict[str, Any] | None:
        """A card only hub memory holds, or None. Dot and Duet read it push-safe from the memory hub."""
        if self.profile.principal_memory:
            return self._cli_hub_card(name, offset, scope)
        return self._push_card(name, offset, scope)

    def _push_card(self, name: str, offset: int, scope: str | None) -> dict[str, Any] | None:
        """Memory hub `card` with `push: true`, with the `[memory_hub]` read credential (S23).

        The hub serves only a live head with a current clean scan receipt and push trust (reviewed,
        policy-promoted or imported, or unreviewed written by this principal), so `memory_card` opens no
        more than `memory_hits` could send. The fleet hub, the node and message credentials and the
        replica play no part: a card the hub withholds, no memory hub, an unavailable hub or a head that
        moves between chunks answer None.
        """
        from ..memory_hub_client import (
            HubConfigError,
            HubRefusal,
            HubUnavailable,
            content_hash,
            hub_mode,
            obj,
            valid_scope,
        )

        try:
            if hub_mode(self.workspace) == "off":
                return None
        except (HubConfigError, ValueError, OSError):
            return None
        try:
            client = self._memory_hub_client()
        except (HubUnavailable, HubConfigError):
            return None
        parts: list[str] = []
        pinned: tuple[Any, ...] | None = None
        chars = 0
        following: int | None = 0
        while following is not None:
            fields: dict[str, Any] = {"card_id": name, "push": True, "offset": following}
            if scope is not None:
                fields["scope"] = scope
            try:
                body = client.call("card", **fields)
            except (HubUnavailable, HubRefusal, HubConfigError):
                return None
            card = obj(body.get("card"))
            text = card.get("text")
            identity = (card.get("scope"), card.get("revision"), card.get("content_hash"), body.get("log_epoch"))
            if not isinstance(text, str) or (pinned is not None and identity != pinned):
                return None
            pinned = identity
            chars += len(text)
            if chars > MAX_CARD_FILE_BYTES:
                return None
            parts.append(text)
            step = card.get("next_offset")
            following = step if type(step) is int and step > following else None
        text = "".join(parts)
        found_scope, revision, sha256, _ = pinned or (None, None, None, None)
        if not isinstance(sha256, str) or content_hash(text) != sha256 or not valid_scope(found_scope):
            return None
        if self.profile.prompt_load_scan:
            # The whole card and its scope are scanned, as for a local card.
            refusal = egress_refusal([text, found_scope])
            if refusal:
                return {"card_id": name, **refusal}
        chunk = text[offset : offset + CARD_CHUNK_CHARS]
        end = offset + len(chunk)
        return dict(
            name=name,
            source="hub",
            scope=found_scope,
            revision=revision if _is_count(revision) else None,
            sha256=sha256 if _SHA256.fullmatch(sha256) else None,
            chars=len(text),
            offset=offset,
            next_offset=end if end < len(text) else None,
            content=chunk,
            content_trust="untrusted",
        )

    def _cli_hub_card(self, name: str, offset: int, scope: str | None = None) -> dict[str, Any] | None:
        """A card only hub memory holds, read through `brigade memory hub card` (S14).

        Only a principal memory profile (Grok Bot memory) reads this way, with its own pinned
        credential in the child's environment. Dot and Duet use `_push_card`. The CLI reads only the workspace's `[hub]` read scopes; `scope`, as a
        search match reports it, picks one when the id is in more than one.
        While the hub is unavailable the CLI answers from this host's replica,
        so a replica search match opens too; `source` says which answered. A
        hub refusal (401, 403 or a coded answer) never falls back, and a scope
        the hub refused is never served from the replica. The CLI reads the
        hub's `status` first and refuses an admin token in the node slot.
        """
        from ..memory_hub_client import HubConfigError, hub_mode, valid_scope

        try:
            if hub_mode(self.workspace) == "off":
                return None
        except (HubConfigError, ValueError, OSError):
            return None
        scoped = ["--scope", scope] if scope else []
        value, error = self._brigade_json(
            ["memory", "hub", "card", "--target", str(self.workspace), "--all", "--json", *scoped, "--", name],
            timeout=30,
        )
        card = value.get("card") if isinstance(value, dict) and value.get("ok") is True else None
        if error or not isinstance(card, dict) or not isinstance(card.get("text"), str):
            return None
        text = card["text"]
        scope, revision, sha256 = card.get("scope"), card.get("revision"), card.get("content_hash")
        if self.profile.prompt_load_scan:
            # Hub JSON is returned beside the text, so each label is checked: a scope must be a valid
            # one that scans with the card, a revision an integer and a hash 64 hex digits.
            scope = scope if valid_scope(scope) else None
            revision = revision if _is_count(revision) else None
            sha256 = sha256 if isinstance(sha256, str) and _SHA256.fullmatch(sha256) else None
            # The whole card is scanned, as for a local card.
            refusal = egress_refusal([text, *([scope] if scope else [])])
            if refusal:
                return {"card_id": name, **refusal}
        chunk = text[offset : offset + CARD_CHUNK_CHARS]
        following = offset + len(chunk)
        source = value.get("source") if value.get("source") in {"hub", "replica"} else "hub"
        return dict(
            name=name,
            source=source,
            scope=scope,
            revision=revision,
            sha256=sha256,
            chars=len(text),
            offset=offset,
            next_offset=following if following < len(text) else None,
            content=chunk,
            content_trust="untrusted",
        )

    def _principal_card(self, name: str, offset: int, scope: str | None) -> dict[str, Any]:
        from ..memory_hub_client import HubConfigError, hub_mode

        try:
            mode = hub_mode(self.workspace)
        except (HubConfigError, ValueError, OSError):
            return {"error": "hub_credential_unavailable"}
        if mode == "off":
            return {"error": "hub_memory_required"}
        return self._hub_card(name, offset, scope) or {"error": "card_not_found", "name": name}

    def memory_card(self, name: str, offset: int = 0, scope: str | None = None) -> dict[str, Any]:
        if self.profile.principal_memory:
            # A principal profile never opens a local card: the hub, under its own grants, is the only source.
            return self._principal_card(name, offset, scope)
        if scope is not None:
            # A hub match's scope: read that hub card, never a local file of the same name.
            return self._hub_card(name, offset, scope) or {"error": "card_not_found", "name": name}
        directory = descriptor = None
        try:
            directory = trusted_directory(self.cards, allow_group_write=True, code="memory_unavailable")
            try:
                descriptor = os.open(
                    name + ".md", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory
                )
            except FileNotFoundError:
                return self._hub_card(name, offset) or {"error": "card_not_found", "name": name}
            except OSError:
                return {"error": "card_unavailable", "name": name}
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                return {"error": "card_unavailable", "name": name}
            if info.st_size > MAX_CARD_FILE_BYTES:
                return {"error": "card_too_large", "name": name, "bytes": info.st_size}
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = None
                data = stream.read(MAX_CARD_FILE_BYTES + 1)
        finally:
            for item in (descriptor, directory):
                if item is not None:
                    os.close(item)
        if len(data) > MAX_CARD_FILE_BYTES:
            return {"error": "card_too_large", "name": name}
        try:
            text = data.decode("utf-8")
        except UnicodeError:
            return {"error": "card_unavailable", "name": name}
        if self.profile.prompt_load_scan:
            # The whole card is scanned, so no chunk boundary can split a hit.
            refusal = egress_refusal([text])
            if refusal:
                return {"card_id": name, **refusal}
        chunk = text[offset : offset + CARD_CHUNK_CHARS]
        following = offset + len(chunk)
        # A remote connector sees the path under the workspace, never the local absolute one.
        path = (
            (self.cards.relative_to(self.workspace) / (name + ".md")).as_posix()
            if self.profile.prompt_load_scan
            else str(Path(self.cards) / (name + ".md"))
        )
        return dict(
            name=name,
            path=path,
            bytes=len(data),
            chars=len(text),
            sha256=hashlib.sha256(data).hexdigest(),
            offset=offset,
            next_offset=following if following < len(text) else None,
            content=chunk,
            content_trust="untrusted",
        )

    def _hub_handoff(self, slug: str, content: str) -> dict[str, Any]:
        """Submit one handoff to hub `memory_handoffs` as the connector principal. Nothing else is written."""
        from ..memory_hub_client import HubConfigError, HubRefusal, HubUnavailable, MemoryHubClient, event_id_at

        # The same structure and instruction-override lint the file path runs, before anything leaves the host.
        _, refusal = self._lint_handoff(content.encode("utf-8"))
        if refusal is not None:
            return refusal
        # The pinned authority is checked last, then used as is: no second read of the file or the hub URL.
        authority = self._principal_authority()
        try:
            client = MemoryHubClient(authority.hub, authority.token)
        except (HubUnavailable, HubConfigError):
            return {"error": "handoff_unavailable", "reason": "hub_credential_unavailable"}
        now_ms = int(self.clock().timestamp() * 1000)
        digest = hashlib.sha256(f"{slug}\n{content}".encode()).digest()
        # One content in one ten-minute window is one id, so a retry after a lost answer is not a second handoff.
        event = event_id_at(now_ms - now_ms % HANDOFF_ID_WINDOW_MS, int.from_bytes(digest[:10], "big"))
        try:
            answer = client.call("handoff", text=content, kind=slug, client_event_id=event)
        except HubUnavailable:
            return {"error": "handoff_unavailable", "reason": "hub_unavailable"}
        except HubRefusal as hub_refusal:
            code = hub_refusal.body.get("code")
            return {"error": "handoff_refused", "code": code if code in HANDOFF_REFUSAL_CODES else "refused"}
        handoff_id = answer.get("handoff_id")
        if type(handoff_id) is not int and not isinstance(handoff_id, str):
            return {"error": "handoff_unavailable", "reason": "hub_unavailable"}
        return dict(ok=True, handoff_id=handoff_id, destination="memory_handoffs", state="pending_review")

    def _lint_handoff(self, encoded: bytes) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """`brigade handoff lint` over a private copy: `(entry, None)` when valid, else `(None, refusal)`."""
        lint_name = f"handoff-lint-{uuid4().hex}.md"
        state_fd = None
        lint_written = False
        try:
            state_fd = private_directory(self.runtime.paths.state_dir, create=True)
            descriptor = os.open(
                lint_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=state_fd
            )
            lint_written = True
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
            result = self.commands.run(
                [
                    self.runtime.paths.bin.brigade,
                    "handoff",
                    "lint",
                    "--json",
                    "--target",
                    str(self.workspace),
                    str(Path(self.runtime.paths.state_dir) / lint_name),
                ],
                timeout=30,
                max_output=MAX_SMALL_CAPTURE,
                env=self.environment,
            )
        except OSError:
            return None, {"error": "handoff_unavailable", "reason": "lint_state_unavailable"}
        finally:
            if state_fd is not None:
                if lint_written:
                    try:
                        os.unlink(lint_name, dir_fd=state_fd)
                    except OSError:
                        pass
                os.close(state_fd)
        report = parse_json_bytes(result.stdout) if not (result.timed_out or result.overflow) else None
        entries = report.get("results") if isinstance(report, dict) else None
        if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
            return None, {"error": "handoff_unavailable", "reason": "lint_unavailable"}
        assert isinstance(report, dict)
        entry = entries[0]
        if entry.get("valid") is not True or report.get("valid") is not True:
            return None, {
                "error": "handoff_invalid",
                "lint_errors": entry.get("errors"),
                "lint_warnings": entry.get("warnings"),
                "hints": entry.get("hints"),
            }
        return entry, None

    def handoff_write(self, slug: str, content: str) -> dict[str, Any]:
        if redact_credentials(content) != content:
            return {"error": "content_rejected", "reason": "credential-shaped text"}
        if self.profile.principal_memory:
            return self._hub_handoff(slug, content)
        moment = self.clock()
        require(isinstance(moment, datetime) and moment.utcoffset() is not None, "clock_unavailable")
        prefix = self.profile.handoff_prefix
        filename = f"{moment.astimezone():%Y-%m-%d-%H%M}-{prefix}-{slug}.md"
        try:
            # A umask of 0002 creates the inbox and its parent as 0775 in this user's own group.
            inbox = trusted_directory(
                self.handoff_inbox,
                allow_group_write=True,
                allow_group_write_ancestors=True,
                code="handoff_unavailable",
            )
        except DotError:
            # Name the failing step: a bare code sent the last live failure to the wrong one.
            reason = "inbox_untrusted" if os.path.isdir(self.handoff_inbox) else "inbox_missing"
            return {"error": "handoff_unavailable", "reason": reason}
        temporary = f".{prefix}-{uuid4().hex}.tmp"
        temporary_written = False
        try:
            try:
                os.stat(filename, dir_fd=inbox, follow_symlinks=False)
                return {"error": "handoff_exists", "filename": filename}
            except FileNotFoundError:
                pass
            encoded = content.encode("utf-8")
            # Lint a private copy before anything appears in the ingested inbox.
            entry, refusal = self._lint_handoff(encoded)
            if refusal is not None or entry is None:
                return refusal or {"error": "handoff_unavailable", "reason": "lint_unavailable"}
            # Publish atomically and never overwrite: write a hidden temp file, then
            # hard-link it to the final name (link fails if the name exists).
            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644, dir_fd=inbox
            )
            temporary_written = True
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, filename, src_dir_fd=inbox, dst_dir_fd=inbox, follow_symlinks=False)
            except FileExistsError:
                return {"error": "handoff_exists", "filename": filename}
            os.fsync(inbox)
            absolute = str(Path(self.handoff_inbox) / filename)
            # The file is already published, so a path that cannot be shown is null, not a failed write.
            path = self._shown_path(absolute) if self.profile.prompt_load_scan else absolute
            return dict(
                ok=True,
                filename=filename,
                path=path,
                bytes=len(encoded),
                action=entry.get("action"),
                lint_warnings=entry.get("warnings"),
            )
        except DotError:
            raise
        except OSError:
            return {"error": "handoff_unavailable", "reason": "inbox_write_failed"}
        finally:
            if temporary_written:
                try:
                    os.unlink(temporary, dir_fd=inbox)
                except OSError:
                    pass
            os.close(inbox)

    def fleet_policy(self, since_hash: str | None = None) -> dict[str, Any]:
        payload = policy(self.workspace, since_hash)
        if self.profile.prompt_load_scan:
            for source in payload.get("sources", ()):
                source["path"] = self._shown_path(source["path"])
        return payload

    def _shown_path(self, path: str) -> str | None:
        """A returned path as a workspace-relative one that scans clean, else None (prompt-load scan profiles)."""
        relative = _relative_path(path, self.workspace)
        return None if relative is None or egress_refusal([relative]) else relative

    def skill_search(self, query: str, limit: int = 20) -> dict[str, Any]:
        value, error = self._brigade_json(["skills", "search", query, "--target", str(self.workspace), "--json"])
        if error or not isinstance(value, dict):
            return {"error": error or "brigade_unavailable"}
        deny = self.runtime.skills_deny
        if not deny:
            return {"skills": value.get("skills", [])[:limit], "count": value.get("count", 0)}
        # Withheld skills are dropped from results the same way they are hidden from skill_get.
        found = value.get("skills", [])
        found = found if isinstance(found, list) else []
        kept = [item for item in found if not (isinstance(item, dict) and _skill_names(item) & deny)]
        count = value.get("count", 0)
        if type(count) is int:
            count = max(0, count - (len(found) - len(kept)))
        return {"skills": kept[:limit], "count": count}

    def skill_get(self, name: str, resource: str = "SKILL.md", offset: int = 0) -> dict[str, Any]:
        require(all(part not in (".", "..") for part in resource.split("/")))
        if name in self.runtime.skills_deny:
            # Indistinguishable from a missing skill, so the denylist itself is not disclosed.
            return {"error": "skill_not_found"}
        if self.runtime.skills_deny:
            from brigade.skills_cmd.registry import _anchored_load_registry_metadata

            directory, metadata = _anchored_load_registry_metadata(self.workspace, name)
            if _skill_names({"skill_dir": str(directory), "metadata": metadata}) & self.runtime.skills_deny:
                return {"error": "skill_not_found"}
        return skill_get(self.workspace, name, resource, offset)


_SAFE_CARD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _card_texts(item: dict[str, Any]) -> list[str]:
    # Card-authored fields of a search match; path and score come from the search itself.
    # A legacy card's id can come from its frontmatter, so it is card-authored too.
    texts = [item.get("card_id"), item.get("title"), item.get("summary")]
    for key in ("tags", "card_aliases"):
        values = item.get(key)
        if isinstance(values, list):
            texts.extend(values)
    return [text for text in texts if isinstance(text, str)]


MALFORMED_MATCH = "malformed-match"
_TEXT_KEYS = ("card_id", "title", "summary", "path", "scope")
_LIST_KEYS = ("tags", "card_aliases")


def _well_formed(item: dict[str, Any]) -> bool:
    # Every field the scan reads must be text (or absent), so nothing returned goes out unscanned.
    if any(item.get(key) is not None and not isinstance(item.get(key), str) for key in _TEXT_KEYS):
        return False
    for key in _LIST_KEYS:
        values = item.get(key)
        if values is not None and not (isinstance(values, list) and all(isinstance(v, str) for v in values)):
            return False
    return True


def _location_texts(item: dict[str, Any]) -> list[str]:
    # Where a search match lives: returned beside the card fields, so it is scanned with them.
    return [value for value in (item.get("path"), item.get("scope")) if isinstance(value, str)]


_SHA256 = re.compile(r"[0-9a-f]{64}")
# The hub's trust levels, its search sources and retrieval modes (docs/phase-v2-hub-architecture.md).
_TRUST_LEVELS = frozenset({"reviewed", "policy-promoted", "imported", "agent-unreviewed", "flagged"})
_SOURCES = frozenset({"local", "hub", "replica"})
_RETRIEVAL_HUB = frozenset({"ok", "unavailable", "refused"})
_RETRIEVAL_MODES = frozenset({"lexical", "hybrid", "lexical-fallback"})
# What a search can fail with: the client's own reasons, the 503 reasons the hub's memory route sends,
# and the coded refusals `search` returns. Card, write and sync codes (for example
# memory_card_not_found, memory_base_mismatch or memory_content_private) never come back from it.
_RETRIEVAL_REASONS = frozenset(
    {
        "no-hub-configured",
        "no-node-credential",
        NO_MEMORY_HUB,
        "timeout",
        "unreachable",
        "no-hub-memory",
        "hub-busy",
        "hub-config-invalid",
        "memory-unavailable",
        "memory-reader-timeout",
        "unauthorized",
        "memory_grant_missing",
        "memory_query_invalid",
        "memory_request_invalid",
        "memory_method_invalid",
    }
)
_HTTP_REASON = re.compile(r"http-[1-5][0-9]{2}")
# A search warning leaves only as one of these codes, never the CLI's text (it embeds a hub reason).
WARNING_HUB_REFUSED = "hub_search_refused"
WARNING_HUB_UNAVAILABLE = "hub_search_unavailable"
WARNING_HUB_LIMIT = "hub_limit_capped"


def _is_count(value: object) -> bool:
    """A non-negative integer, as a revision or a match count is. A bool is not one."""
    return type(value) is int and value >= 0


def _is_number(value: object) -> bool:
    return type(value) in (int, float)


def _is_home(path: Path) -> bool:
    try:
        return path == Path.home()
    except (RuntimeError, KeyError):
        return False


def _hub_card_path(card_id: object) -> str | None:
    """`cards/<card_id>.md` for a hub or replica match, only from a slug id that scans clean."""
    if isinstance(card_id, str) and _SAFE_CARD_ID.fullmatch(card_id) and egress_refusal([card_id]) is None:
        return f"cards/{card_id}.md"
    return None


def _relative_path(path: object, workspace: Path) -> str | None:
    """A match path as a workspace-relative POSIX path, or None when it cannot be shown as one.

    An absolute path inside the workspace is made relative. Anything else absolute, a drive or
    backslash path, or a path that climbs out with `..` is not shown.
    """
    if not isinstance(path, str) or not path or "\\" in path or "\0" in path:
        return None
    candidate = PurePath(path)
    if candidate.is_absolute():
        try:
            candidate = candidate.relative_to(workspace)
        except ValueError:
            return None
    parts = candidate.parts
    if not parts or any(part in ("", ".", "..") for part in parts) or ":" in parts[0]:
        return None
    return candidate.as_posix()


def _keep_known_hub_labels(item: dict[str, Any]) -> None:
    # `source`, `trust`, `revision` and `scope` come from hub JSON unchanged: an unknown value is dropped, not echoed.
    from ..memory_hub_client import valid_scope

    if item.get("scope") is not None and not valid_scope(item["scope"]):
        item["scope"] = None
    for key, known in (("source", _SOURCES), ("trust", _TRUST_LEVELS)):
        if key in item and not (isinstance(item[key], str) and item[key] in known):
            item[key] = None
    if "revision" in item and not _is_count(item["revision"]):
        item["revision"] = None


def _known_str(value: object, known: frozenset[str]) -> bool:
    return isinstance(value, str) and value in known


def _known_retrieval(retrieval: dict[str, Any]) -> dict[str, Any]:
    """The hub's retrieval report cut to the keys and values the CLI documents; the rest is dropped."""
    known: dict[str, Any] = {}
    # Hub JSON can hold any type, and a list or dict is unhashable, so each label is a str first.
    if _known_str(retrieval.get("hub"), _RETRIEVAL_HUB):
        known["hub"] = retrieval["hub"]
    for key in ("degraded", "stale", "replica_incomplete"):
        if isinstance(retrieval.get(key), bool):
            known[key] = retrieval[key]
    if _known_str(retrieval.get("effective"), _RETRIEVAL_MODES):
        known["effective"] = retrieval["effective"]
    reason = retrieval.get("reason")
    if isinstance(reason, str) and (reason in _RETRIEVAL_REASONS or _HTTP_REASON.fullmatch(reason)):
        known["reason"] = reason
    for key in ("status", "hub_limit"):
        value = retrieval.get(key)
        if type(value) is int and 0 <= value <= 599:
            known[key] = value
    fallback = retrieval.get("fallback")
    if isinstance(fallback, list):
        known["fallback"] = [source for source in fallback if _known_str(source, _SOURCES)]
    return known


def _warning_code(warning: object, retrieval: dict[str, Any]) -> str | None:
    """The one code for a search warning, read from the checked retrieval report, or None."""
    if not isinstance(warning, str):
        return None
    if retrieval.get("hub") == "refused":
        return WARNING_HUB_REFUSED
    if retrieval.get("hub") == "unavailable":
        return WARNING_HUB_UNAVAILABLE
    return WARNING_HUB_LIMIT if "hub_limit" in retrieval else None


def _blocked_card_id(item: dict[str, Any]) -> str | None:
    """An identifier for a blocked match that carries no card-authored free text.

    Keep the card id only when it is a plain slug, else fall back to the file stem
    memory_card reads by, and return None when neither is a plain slug.
    """
    path = item.get("path")
    for value in (item.get("card_id"), PurePath(path).stem if isinstance(path, str) else None):
        # A slug can still be the refused text itself (a private address is slug-shaped), so it must scan clean.
        if isinstance(value, str) and _SAFE_CARD_ID.fullmatch(value) and egress_refusal([value]) is None:
            return value
    return None


def _refused_match(item: dict[str, Any], refusal: dict[str, str]) -> dict[str, Any]:
    """The listing for a match whose text may not leave: id, rule id, and the hub scope.

    The title, summary, tags and aliases are card-authored and may hold the very text
    that was refused, so none of them is echoed. The scope is operator-set, and is kept
    only when it is a valid scope that scans clean, so a reader can open the card locally.
    """
    from ..memory_hub_client import valid_scope

    placeholder: dict[str, Any] = {"card_id": _blocked_card_id(item), **refusal}
    scope = item.get("scope")
    if isinstance(scope, str) and valid_scope(scope) and egress_refusal([scope]) is None:
        placeholder["scope"] = scope
    return placeholder


def _skill_names(item: dict[str, Any]) -> set[str]:
    # Registry rows carry the id in metadata and the entry name in skill_dir, which skill_get reads by.
    metadata = item.get("metadata")
    names = {metadata.get("id"), item.get("id")} if isinstance(metadata, dict) else {item.get("id")}
    if isinstance(item.get("skill_dir"), str):
        names.add(PurePath(item["skill_dir"]).name)
    return {name for name in names if isinstance(name, str)}


def read_file(path: Path, maximum: int = 1024 * 1024) -> tuple[bytes, os.stat_result]:
    directory = trusted_directory(path.parent, code="source_unavailable")
    try:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
    finally:
        os.close(directory)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise ValueError("source_unavailable")
        raw = stream.read(maximum + 1)
        if len(raw) > maximum:
            raise ValueError("source_unavailable")
    return raw, info


def policy(workspace: Path, since_hash: str | None = None) -> dict[str, Any]:
    sources: list[dict[str, Any]] = []
    banned: list[str] = []
    for relative in POLICY_PATHS:
        path = workspace / relative
        try:
            raw, info = read_file(path)
            content = raw.decode("utf-8")
        except (OSError, ValueError, UnicodeError):
            raise DotError("source_unavailable") from None

        def compact(match):
            phrases = re.findall(r'^- "(.*)"\s*$', match.group(), re.M)
            banned.extend(p for p in phrases if p not in banned)
            return "Banned phrases (compact): " + json.dumps(phrases, ensure_ascii=False)

        content = re.sub(
            r"<!-- SLOP-BANLIST:START[^>]*-->.*?<!-- SLOP-BANLIST:END -->",
            compact,
            content,
            flags=re.S,
        )
        sources.append(
            dict(
                path=str(path),
                sha256=hashlib.sha256(raw).hexdigest(),
                mtime=info.st_mtime,
                content=content,
            )
        )
    digest_hash = hashlib.sha256(
        json.dumps([(s["path"], s["sha256"]) for s in sources], separators=(",", ":")).encode()
    ).hexdigest()
    if since_hash == digest_hash:
        return dict(hash=digest_hash, changed=False)
    # Digest is generated each call from the sections actually present on disk.
    sections = {
        "authorization": (
            "Instruction precedence and authorization",
            "Git and publication",
            "PR ownership through merge",
            "Credential automation",
        ),
        "stop_boundaries": (
            "Instruction precedence and authorization",
            "Working limits",
        ),
        "routing": (
            "Public web research and scraping",
            "Dispatch and routing authority",
            "Guidance",
        ),
        "writing_rules": ("Writing and formatting", "Answer shape"),
        "memory_handoff_rules": ("Memory handoffs",),
    }
    digest = {}
    for key, names in sections.items():
        excerpts = []
        for source in sources:
            for name in names:
                match = re.search(
                    r"^#{2,3} " + re.escape(name) + r"\s*\n(.*?)(?=^#{1,3} |\Z)",
                    source["content"],
                    re.M | re.S,
                )
                if match:
                    paragraphs = match[1].strip().split("\n\n")
                    excerpt = "\n\n".join(paragraphs[:2])[:1200]
                    if excerpt not in excerpts:
                        excerpts.append(excerpt)
        digest[key] = "\n\n".join(excerpts)
    return dict(
        hash=digest_hash,
        changed=True,
        sources=sources,
        banned_phrases=banned,
        digest=digest,
        title="Dot operating digest",
        source_of_truth="The control host; reread fleet_policy at task start and before dispatch",
    )


def skill_get(workspace: Path, name: str, resource: str = "SKILL.md", offset: int = 0) -> dict[str, Any]:
    # Brigade's descriptor-anchored registry reader is the installed MCP read path.
    from brigade.skills_cmd.registry import _anchored_registry_text

    content = _anchored_registry_text(Path(workspace), name, *resource.split("/"))
    if content is None:
        return {"error": "skill_not_found"}
    raw = content.encode("utf-8")
    if len(raw) > 1024 * 1024 or offset > len(content):
        return {"error": "source_too_large_or_offset_invalid"}
    end = offset + 24000
    return dict(
        name=name,
        resource=resource,
        sha256=hashlib.sha256(raw).hexdigest(),
        content=content[offset:end],
        next_offset=end if end < len(content) else None,
    )
