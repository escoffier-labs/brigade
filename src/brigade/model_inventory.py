"""Advisory live model inventory checks for roster doctor."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from . import proc

InventoryState = Literal["exact", "fuzzy-resolved", "missing", "unavailable"]

_LIST_COMMANDS = {
    "cursor": ["cursor-agent", "models"],
    "grok": ["grok", "models"],
    "ollama": ["ollama", "list"],
}
_EFFORT_SUFFIX = re.compile(r"-(?:none|low|medium|high|xhigh|extra-high|max)$")
_OLLAMA_OPERATIONAL_ERRORS = (
    "authentication",
    "connection",
    "dial tcp",
    "i/o timeout",
    "network",
    "no such host",
    "temporary failure",
    "timed out",
    "timeout",
    "tls",
    "unauthorized",
)


@dataclass(frozen=True)
class ModelInventoryResult:
    state: InventoryState
    requested: str
    matches: tuple[str, ...]
    detail: str


@dataclass(frozen=True)
class _HarnessInventory:
    models: tuple[str, ...] = ()
    error: str = ""


class ModelInventoryInspector:
    """Inspect supported harness inventories, caching results for one doctor run."""

    def __init__(self) -> None:
        self._inventories: dict[str, _HarnessInventory] = {}
        self._results: dict[tuple[str, str], ModelInventoryResult] = {}

    def inspect(self, cli_ref: str, requested: str) -> ModelInventoryResult | None:
        key = (cli_ref, requested)
        if key in self._results:
            return self._results[key]
        if cli_ref in _LIST_COMMANDS:
            result = self._inspect_listed(cli_ref, requested)
        elif cli_ref.startswith("ollama:"):
            result = self._inspect_ollama(requested)
        else:
            return None
        self._results[key] = result
        return result

    def _inspect_listed(self, cli_ref: str, requested: str) -> ModelInventoryResult:
        inventory = self._inventory(cli_ref)
        harness = "Cursor" if cli_ref == "cursor" else "Grok"
        if inventory.error:
            return ModelInventoryResult(
                "unavailable",
                requested,
                (),
                f"could not inspect live {harness} inventory: {inventory.error}",
            )
        advertised = _advertised_model_id(cli_ref, requested)
        if advertised in inventory.models:
            return ModelInventoryResult(
                "exact",
                requested,
                (advertised,),
                f"model {requested!r} exactly matches live {harness} inventory",
            )
        family = _model_family(advertised)
        related = (
            tuple(sorted(model for model in inventory.models if _model_family(model) == family))
            if re.search(r"\d", family)
            else ()
        )
        if related:
            return ModelInventoryResult(
                "fuzzy-resolved",
                requested,
                related,
                f"model {requested!r} is not exact; related live {harness} IDs: {', '.join(related)}",
            )
        return ModelInventoryResult(
            "missing",
            requested,
            (),
            f"model {requested!r} is absent from live {harness} inventory",
        )

    def _inventory(self, cli_ref: str) -> _HarnessInventory:
        cached = self._inventories.get(cli_ref)
        if cached is not None:
            return cached
        result = proc.run(_LIST_COMMANDS[cli_ref], timeout=15.0)
        if result.code != 0:
            diagnostic = result.stderr.strip() or result.stdout.strip() or f"exit {result.code}"
            inventory = _HarnessInventory(error=diagnostic[:160])
        else:
            recognized, models = _parse_model_list(cli_ref, f"{result.stdout}\n{result.stderr}")
            if not recognized:
                inventory = _HarnessInventory(error="command returned an unrecognized inventory shape")
            elif not models and cli_ref != "ollama":
                inventory = _HarnessInventory(error="command returned no model IDs")
            else:
                inventory = _HarnessInventory(models=models)
        self._inventories[cli_ref] = inventory
        return inventory

    def _inspect_ollama(self, requested: str) -> ModelInventoryResult:
        inventory = self._inventory("ollama")
        if inventory.error:
            return ModelInventoryResult(
                "unavailable",
                requested,
                (),
                f"could not inspect live Ollama inventory: {inventory.error}",
            )
        wanted = {requested} if ":" in requested else {requested, f"{requested}:latest"}
        matches = tuple(sorted(wanted & set(inventory.models)))
        if not matches:
            return ModelInventoryResult(
                "missing",
                requested,
                (),
                f"ollama model {requested!r} is not listed locally; Brigade never auto-pulls it",
            )
        if not requested.endswith(":cloud"):
            return ModelInventoryResult(
                "exact",
                requested,
                matches,
                f"model {requested!r} is pulled locally and exactly matches Ollama inventory",
            )

        result = proc.run(["ollama", "show", requested], timeout=15.0)
        if result.code == 0:
            return ModelInventoryResult(
                "exact",
                requested,
                matches,
                f"cloud model {requested!r} is listed locally and available from Ollama",
            )
        diagnostic = result.stderr.strip() or result.stdout.strip() or f"exit {result.code}"
        state: InventoryState = "missing" if _ollama_model_is_missing(diagnostic) else "unavailable"
        return ModelInventoryResult(state, requested, (), diagnostic[:200])


def _parse_model_list(cli_ref: str, output: str) -> tuple[bool, tuple[str, ...]]:
    lines = output.splitlines()
    header_index = _inventory_header_index(cli_ref, lines)
    if header_index is None:
        return False, ()
    models: set[str] = set()
    for line in lines[header_index + 1 :]:
        if cli_ref == "cursor":
            if line.startswith("Tip:"):
                break
            match = re.match(r"^([a-z0-9][a-z0-9._:/\[\],=-]*)\s+-\s+.+$", line)
        elif cli_ref == "grok":
            match = re.match(r"^\s*\*\s+([^\s(]+)", line)
        else:
            if not line.strip():
                continue
            model = _parse_ollama_row(line)
            if model is None:
                return False, ()
            models.add(model)
            continue
        if match is not None:
            models.add(match.group(1))
    return True, tuple(sorted(models))


def _inventory_header_index(cli_ref: str, lines: list[str]) -> int | None:
    for index, line in enumerate(lines):
        stripped = line.strip()
        if cli_ref == "cursor" and stripped == "Available models":
            return index
        if cli_ref == "grok" and stripped == "Available models:":
            return index
        if cli_ref == "ollama" and stripped.startswith("NAME") and " ID " in f" {stripped} ":
            return index
    return None


def _advertised_model_id(cli_ref: str, requested: str) -> str:
    if cli_ref == "cursor" and requested.endswith("]") and "[" in requested:
        return requested.split("[", 1)[0]
    return requested


def _parse_ollama_row(line: str) -> str | None:
    columns = line.split()
    if len(columns) < 5:
        return None
    model, model_id, size = columns[:3]
    if re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._/-]*(?::[a-zA-Z0-9][a-zA-Z0-9._-]*)?", model) is None:
        return None
    if re.fullmatch(r"[0-9a-fA-F]{12}", model_id) is None:
        return None
    if size == "-":
        return model
    if re.fullmatch(r"\d+(?:\.\d+)?", size) is None:
        return None
    if columns[3].upper() not in {"B", "KB", "MB", "GB", "TB"}:
        return None
    return model


def _ollama_model_is_missing(diagnostic: str) -> bool:
    normalized = diagnostic.lower()
    if any(marker in normalized for marker in _OLLAMA_OPERATIONAL_ERRORS):
        return False
    if "retired" in normalized or "does not exist" in normalized:
        return True
    if re.search(r"\bmodel\b.*\bnot found\b", normalized):
        return True
    return "pull model manifest" in normalized and ("404" in normalized or "not found" in normalized)


def _model_family(model: str) -> str:
    normalized = model.lower()
    if normalized.startswith("cursor-"):
        normalized = normalized[len("cursor-") :]
    if normalized.endswith("-fast"):
        normalized = normalized[: -len("-fast")]
    return _EFFORT_SUFFIX.sub("", normalized)
